"""Execution-layer tests: order lifecycle, freeze-qty split, paper fills, positions, exits."""

from __future__ import annotations

from decimal import Decimal

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import Side
from algo_trading.domain.models import Trade
from algo_trading.execution.exit_manager import ExitManager, ExitReason
from algo_trading.execution.order_manager import OrderManager, split_for_freeze
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.execution.signal_translator import new_client_tag
from algo_trading.persistence.repositories import Repository
from tests.conftest import make_instrument, make_order_request

# -- Freeze-quantity splitting ---------------------------------------------------------


def test_split_for_freeze_even():
    assert split_for_freeze(150, 75, 1800) == [150]  # under freeze


def test_split_for_freeze_splits_into_lot_aligned_legs():
    # a real total is always lots * lot_size; 53 lots * 75 = 3975
    legs = split_for_freeze(3975, 75, 1800)  # max leg = floor(1800/75)*75 = 1800
    assert legs == [1800, 1800, 375]
    assert sum(legs) == 3975
    assert all(leg % 75 == 0 for leg in legs)


# -- Order manager + paper broker ------------------------------------------------------


def test_order_manager_paper_fill_flow(engine):
    repo = Repository(engine)
    fills: list[Trade] = []
    broker = PaperBroker()
    om = OrderManager(broker, repo, get_settings(), on_fill=fills.append)

    req = make_order_request(client_tag="e1", price="100")
    ids = om.submit(req)

    assert len(ids) == 1
    assert repo.get_order_state("e1") == "FILLED"
    assert len(fills) == 1 and fills[0].price == Decimal("100")
    # PENDING -> ACK -> FILLED all appended as immutable events
    trades = repo.trades_for_day()
    assert len(trades) == 1


def test_order_manager_persists_before_submit_is_idempotent(engine):
    repo = Repository(engine)
    broker = PaperBroker()
    om = OrderManager(broker, repo, get_settings())
    req = make_order_request(client_tag="e2", price="50")
    om.submit(req)
    # resubmitting the same tag must not create a second order row/fill duplicate
    om.submit(req)
    assert repo.get_order_state("e2") == "FILLED"


def test_order_manager_splits_large_order(engine):
    repo = Repository(engine)
    broker = PaperBroker()
    om = OrderManager(broker, repo, get_settings())
    inst = make_instrument(lot_size=75)
    # 4000 qty -> 3 legs (1800,1800,400)
    req = make_order_request(client_tag="big", quantity=4000, price="10", instrument=inst)
    ids = om.submit(req)
    assert len(ids) == 3


def test_reconcile_marks_terminal_from_report(engine):
    repo = Repository(engine)

    class ReportingBroker(PaperBroker):
        def order_report(self):
            return [{"tag": "open1", "ordSt": "complete", "fldQty": "75", "qty": "75",
                     "avgPrc": "80", "nOrdNo": "B1"}]

    broker = ReportingBroker()
    om = OrderManager(broker, repo, get_settings())
    # a locally-open order that the broker reports as complete
    repo.record_new_order(make_order_request(client_tag="open1"))
    summary = om.reconcile()
    assert summary["reconciled_terminal"] == 1
    assert repo.get_order_state("open1") == "FILLED"


# -- Position tracker ------------------------------------------------------------------


def _trade(side, qty, price, inst=None, tag=None):
    return Trade(
        client_tag=tag or new_client_tag(),
        broker_order_id="B",
        instrument=inst or make_instrument(),
        side=side,
        quantity=qty,
        price=Decimal(price),
    )


def test_position_tracker_entry_and_unrealized():
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(_trade(Side.BUY, 75, "100", inst))
    pt.on_price(inst.instrument_token, Decimal("120"))
    positions = pt.open_positions()
    assert len(positions) == 1
    assert pt.unrealized_pnl() == Decimal("1500")  # (120-100)*75
    assert pt.open_position_count() == 1


def test_position_tracker_realized_on_exit():
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(_trade(Side.BUY, 75, "100", inst))
    pt.on_fill(_trade(Side.SELL, 75, "130", inst))  # close for +30 pts
    assert pt.realized_pnl() == Decimal("2250")  # (130-100)*75
    assert pt.open_position_count() == 0
    assert pt.unrealized_pnl() == Decimal("0")


# -- Exit manager ----------------------------------------------------------------------


def _exit_settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "target_points", Decimal("30"))
    object.__setattr__(s, "trail_points", Decimal("10"))
    object.__setattr__(s, "stoploss_points", Decimal("15"))
    return s


def test_exit_stop_loss():
    em = ExitManager(_exit_settings())
    inst = make_instrument()
    em.register(inst, 75, Decimal("100"))
    assert em.evaluate(inst.trading_symbol, Decimal("90")) is None  # above stop (85)
    assert em.evaluate(inst.trading_symbol, Decimal("85")) is ExitReason.STOP_LOSS


def test_exit_target_then_trailing():
    em = ExitManager(_exit_settings())
    inst = make_instrument()
    em.register(inst, 75, Decimal("100"))  # target 130, trail 10
    assert em.evaluate(inst.trading_symbol, Decimal("130")) is None  # target hit -> trail armed at 120
    assert em.evaluate(inst.trading_symbol, Decimal("145")) is None  # trail rises to 135
    assert em.evaluate(inst.trading_symbol, Decimal("150")) is None  # trail rises to 140
    assert em.evaluate(inst.trading_symbol, Decimal("139")) is ExitReason.TRAILING_STOP  # <=140


def test_exit_not_triggered_before_target_no_stop():
    em = ExitManager(_exit_settings())
    inst = make_instrument()
    em.register(inst, 75, Decimal("100"))
    assert em.evaluate(inst.trading_symbol, Decimal("110")) is None
