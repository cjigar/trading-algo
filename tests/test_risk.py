"""Risk-manager tests: pre-trade checks, kill-switch persistence, manual halt."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import AlgoState, OptionType, Side, Underlying
from algo_trading.domain.models import Signal, Trade
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.persistence.repositories import Repository
from algo_trading.risk.risk_manager import RiskManager
from tests.conftest import make_instrument


def _settings(**overrides):
    s = get_settings(reload=True)
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def _signal() -> Signal:
    return Signal(
        underlying=Underlying.NIFTY,
        side=Side.BUY,
        option_type=OptionType.CE,
        reference_price=Decimal("23000"),
        timestamp=datetime(2025, 1, 15, tzinfo=UTC),
    )


def test_entry_allowed_when_running(engine):
    repo = Repository(engine)
    rm = RiskManager(_settings(max_positions=1, max_trades_per_day=5), repo, PositionTracker())
    rm.start_session()
    assert rm.check_entry(_signal()).allowed is True


def test_entry_blocked_when_idle(engine):
    repo = Repository(engine)
    rm = RiskManager(_settings(), repo, PositionTracker())
    # no start_session -> state IDLE
    d = rm.check_entry(_signal())
    assert d.allowed is False and "IDLE" in d.reason


def test_max_positions_blocks_entry(engine):
    repo = Repository(engine)
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
                     quantity=75, price=Decimal("100")))
    rm = RiskManager(_settings(max_positions=1), repo, pt)
    rm.start_session()
    assert rm.check_entry(_signal()).allowed is False


def test_max_trades_per_day_blocks_entry(engine):
    repo = Repository(engine)
    rm = RiskManager(_settings(max_positions=10, max_trades_per_day=2), repo, PositionTracker())
    rm.start_session()
    rm.register_entry()
    rm.register_entry()
    assert rm.check_entry(_signal()).allowed is False


def test_kill_switch_triggers_and_persists(engine):
    repo = Repository(engine)
    pt = PositionTracker()
    inst = make_instrument()
    # open then mark a large unrealized loss
    pt.on_fill(Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
                     quantity=75, price=Decimal("100")))
    pt.on_price(inst.instrument_token, Decimal("20"))  # (20-100)*75 = -6000
    rm = RiskManager(_settings(daily_loss_cap=Decimal("5000")), repo, pt)
    rm.start_session()

    assert rm.evaluate_kill_switch() is True
    assert rm.is_halted() is True
    # Persisted: a fresh manager over the same engine sees HALTED and refuses entries.
    rm2 = RiskManager(_settings(daily_loss_cap=Decimal("5000")), repo, pt)
    assert rm2.is_halted() is True
    assert rm2.start_session() is AlgoState.HALTED  # cannot reset within the day
    assert rm2.check_entry(_signal()).allowed is False


def test_manual_halt(engine):
    repo = Repository(engine)
    rm = RiskManager(_settings(), repo, PositionTracker())
    rm.start_session()
    rm.manual_halt("operator pressed stop")
    assert rm.is_halted() is True
    assert rm.check_entry(_signal()).allowed is False
    events = [e.event_type for e in repo.audit_events()]
    assert "manual_halt" in events


def test_kill_switch_not_triggered_below_cap(engine):
    repo = Repository(engine)
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(Trade(client_tag="a", broker_order_id="B", instrument=inst, side=Side.BUY,
                     quantity=75, price=Decimal("100")))
    pt.on_price(inst.instrument_token, Decimal("90"))  # -750, under 5000 cap
    rm = RiskManager(_settings(daily_loss_cap=Decimal("5000")), repo, pt)
    rm.start_session()
    assert rm.evaluate_kill_switch() is False
    assert rm.is_halted() is False
