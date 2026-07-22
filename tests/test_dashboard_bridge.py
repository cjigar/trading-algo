"""Dashboard <-> loop separation: they communicate only through the shared PostgreSQL database.

Simulates two processes by using two independent engines/repositories over the same database:
one for the orchestrator (loop) and one for the StateBridge (dashboard).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from algo_trading.config.settings import Settings, get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.enums import AlgoState, Side
from algo_trading.domain.models import Trade
from algo_trading.persistence.db import LiveQuoteRow
from algo_trading.persistence.repositories import Repository
from tests.conftest import make_instrument


def _bridge_settings(engine) -> Settings:
    """Settings pointing the dashboard at the same database, as a separate process would."""
    s = get_settings(reload=True)
    object.__setattr__(s, "database_url", engine.url.render_as_string(hide_password=False))
    return s


def test_bridge_reads_state_and_reconstructs_positions(engine):
    loop_repo = Repository(engine)
    # loop writes some state
    loop_repo.set_algo_state(AlgoState.RUNNING, "started")
    inst = make_instrument()
    loop_repo.record_trade(
        Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
              quantity=75, price=Decimal("100"))
    )
    loop_repo.record_pnl(Decimal("0"), Decimal("0"))

    # dashboard (separate process) reads via its own engine over the same database
    bridge = StateBridge(_bridge_settings(engine))
    state = bridge.read_state()
    assert state.algo_state is AlgoState.RUNNING
    assert len(state.positions) == 1
    assert state.positions[0].quantity == 75


def test_bridge_marks_positions_at_the_loops_published_prices(engine):
    """The whole point of live_quotes: without them the dashboard marks every position at its own
    fill price and unrealized P&L is structurally zero, however the market moves."""
    loop_repo = Repository(engine)
    inst = make_instrument()
    loop_repo.record_trade(
        Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
              quantity=75, price=Decimal("100"))
    )

    bridge = StateBridge(_bridge_settings(engine))
    assert bridge.read_state().unrealized_pnl == Decimal(0)  # no quote published yet

    loop_repo.upsert_live_quotes({inst.instrument_token: Decimal("120")})
    state = bridge.read_state()
    assert state.unrealized_pnl == Decimal("1500")  # (120 - 100) * 75
    assert state.positions[0].last_price == Decimal("120")
    assert state.day_pnl == Decimal("1500")


def test_bridge_ignores_a_stale_quote(engine):
    """A feed that died hours ago must not keep marking positions at its last price."""
    loop_repo = Repository(engine)
    inst = make_instrument()
    loop_repo.record_trade(
        Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
              quantity=75, price=Decimal("100"))
    )
    loop_repo.upsert_live_quotes({inst.instrument_token: Decimal("120")})
    with Session(engine) as s:
        row = s.exec(select(LiveQuoteRow)).one()
        row.timestamp = datetime.utcnow() - timedelta(hours=2)
        s.add(row)
        s.commit()

    settings = _bridge_settings(engine)
    object.__setattr__(settings, "live_quote_max_age_seconds", 60)
    state = StateBridge(settings).read_state()
    # Falls back to the fill price rather than a two-hour-old mark.
    assert state.unrealized_pnl == Decimal(0)
    assert state.positions[0].last_price == Decimal("100")


def test_bridge_reports_engine_pnl_freshness(engine):
    """The loop's own reading is carried alongside so a stalled loop is visible as an ageing
    snapshot rather than a confidently frozen number."""
    loop_repo = Repository(engine)
    bridge = StateBridge(_bridge_settings(engine))
    assert bridge.read_state().engine_pnl is None  # loop has not reported at all

    loop_repo.record_pnl(Decimal("120"), Decimal("30"))
    engine_pnl = bridge.read_state().engine_pnl
    assert engine_pnl is not None
    assert (engine_pnl.realized, engine_pnl.unrealized, engine_pnl.total) == (
        Decimal("120"), Decimal("30"), Decimal("150"),
    )
    assert 0 <= engine_pnl.age_seconds < 60


def test_orchestrator_publishes_pnl_and_open_position_prices(engine):
    """End-to-end across the process boundary: the loop publishes, the dashboard reads it back."""
    from algo_trading.core.orchestrator import Orchestrator
    from algo_trading.domain.enums import TradingMode
    from algo_trading.domain.models import Tick
    from algo_trading.instruments.scrip_master import ScripMaster

    inst = make_instrument()
    settings = get_settings(reload=True)
    object.__setattr__(settings, "mode", TradingMode.PAPER)
    orch = Orchestrator(settings, scrip_master=ScripMaster([inst]), repo=Repository(engine))
    orch.positions.on_fill(
        Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
              quantity=75, price=Decimal("100"))
    )
    orch.publish_tick(
        Tick(
            instrument_token=inst.instrument_token,
            exchange_segment=inst.exchange_segment,
            ltp=Decimal("120"),
            timestamp=datetime.utcnow(),
        )
    )

    assert orch.write_pnl_snapshot() == 1  # one open position -> one quote published

    loop_repo = Repository(engine)
    assert loop_repo.live_quotes() == {inst.instrument_token: Decimal("120")}
    snap = loop_repo.latest_pnl_snapshot()
    assert snap is not None and snap.unrealized == Decimal("1500")


def test_write_pnl_snapshot_publishes_no_quotes_without_open_positions(engine):
    """A flat book still reports P&L, but has no prices worth publishing."""
    from algo_trading.core.orchestrator import Orchestrator
    from algo_trading.domain.enums import TradingMode
    from algo_trading.instruments.scrip_master import ScripMaster

    settings = get_settings(reload=True)
    object.__setattr__(settings, "mode", TradingMode.PAPER)
    orch = Orchestrator(settings, scrip_master=ScripMaster([make_instrument()]), repo=Repository(engine))

    assert orch.write_pnl_snapshot() == 0
    assert Repository(engine).latest_pnl_snapshot() is not None


def test_bridge_control_command_reaches_orchestrator(engine):
    # The dashboard bridge and the orchestrator use SEPARATE engines over the SAME database.
    orch_repo = Repository(engine)
    from datetime import date

    from algo_trading.core.orchestrator import Orchestrator
    from algo_trading.domain.enums import ExchangeSegment, OptionType, TradingMode, Underlying
    from algo_trading.domain.models import Instrument
    from algo_trading.instruments.scrip_master import ScripMaster

    inst = Instrument(
        underlying=Underlying.NIFTY, exchange_segment=ExchangeSegment.NSE_FO,
        trading_symbol="NIFTY23000CE", instrument_token="23000-CE",
        expiry=date(2025, 1, 30), strike=Decimal("23000"), option_type=OptionType.CE, lot_size=75,
    )
    settings = get_settings(reload=True)
    object.__setattr__(settings, "mode", TradingMode.PAPER)
    orch = Orchestrator(settings, scrip_master=ScripMaster([inst]), repo=orch_repo)
    orch.start_session()

    # Dashboard enqueues a stop command over the shared database.
    bridge = StateBridge(_bridge_settings(engine))
    bridge.send_stop()

    # Orchestrator (separate engine, same database) consumes it.
    orch.process_control_commands()
    assert orch.risk.is_halted() is True
