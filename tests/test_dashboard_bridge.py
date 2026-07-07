"""Dashboard <-> loop separation: they communicate only through the shared SQLite DB.

Simulates two processes by using two independent engines/repositories over the same DB file:
one for the orchestrator (loop) and one for the StateBridge (dashboard).
"""

from __future__ import annotations

from decimal import Decimal

from algo_trading.config.settings import Settings, get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.enums import AlgoState, Side
from algo_trading.domain.models import Trade
from algo_trading.persistence.db import create_db_engine
from algo_trading.persistence.repositories import Repository
from tests.conftest import make_instrument


def _sqlite_settings(db_path: str) -> Settings:
    s = get_settings(reload=True)
    object.__setattr__(s, "database_url", "")
    object.__setattr__(s, "db_path", db_path)
    return s


def test_bridge_reads_state_and_reconstructs_positions(tmp_path):
    db = str(tmp_path / "shared.db")
    loop_repo = Repository(create_db_engine(db))
    # loop writes some state
    loop_repo.set_algo_state(AlgoState.RUNNING, "started")
    inst = make_instrument()
    loop_repo.record_trade(
        Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
              quantity=75, price=Decimal("100"))
    )
    loop_repo.record_pnl(Decimal("0"), Decimal("0"))

    # dashboard (separate process) reads via its own engine over the same file
    bridge = StateBridge(_sqlite_settings(db))
    state = bridge.read_state()
    assert state.algo_state is AlgoState.RUNNING
    assert len(state.positions) == 1
    assert state.positions[0].quantity == 75


def test_bridge_control_command_reaches_orchestrator(engine, tmp_path):
    # The dashboard bridge and the orchestrator use SEPARATE engines over the SAME db file.
    db = str(tmp_path / "shared.db")

    # Build the orchestrator against a repo bound to the shared db file.
    orch_repo = Repository(create_db_engine(db))
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

    # Dashboard enqueues a stop command over the shared file.
    bridge = StateBridge(_sqlite_settings(db))
    bridge.send_stop()

    # Orchestrator (separate engine, same file) consumes it.
    orch.process_control_commands()
    assert orch.risk.is_halted() is True
