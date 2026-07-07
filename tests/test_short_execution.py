"""Short-position support: signed P&L, VWAP-cross exit, buy-to-close, margin check."""

from __future__ import annotations

from decimal import Decimal

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import Side
from algo_trading.domain.models import Trade
from algo_trading.execution.exit_manager import ExitManager, ExitReason
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.execution.signal_translator import SignalTranslator
from algo_trading.persistence.repositories import Repository
from algo_trading.risk.risk_manager import RiskManager
from tests.conftest import make_instrument


def _trade(side, qty, price, inst=None):
    return Trade(client_tag=f"{side.value}{price}", broker_order_id="B",
                 instrument=inst or make_instrument(), side=side, quantity=qty, price=Decimal(price))


# -- Short P&L ------------------------------------------------------------------------


def test_short_realized_pnl():
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(_trade(Side.SELL, 75, "100", inst))  # sell-to-open at 100
    pt.on_fill(_trade(Side.BUY, 75, "70", inst))    # buy-to-close at 70 -> +30/unit
    assert pt.realized_pnl() == Decimal("2250")     # (100-70)*75
    assert pt.open_position_count() == 0


def test_short_open_position_and_unrealized():
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(_trade(Side.SELL, 75, "100", inst))  # short at 100
    pt.on_price(inst.instrument_token, Decimal("120"))  # moved against the short
    positions = pt.open_positions()
    assert len(positions) == 1
    assert positions[0].side is Side.SELL
    assert positions[0].quantity == 75
    assert pt.unrealized_pnl() == Decimal("-1500")  # (120-100) against a short = -20*75


def test_long_still_works():
    pt = PositionTracker()
    inst = make_instrument()
    pt.on_fill(_trade(Side.BUY, 75, "100", inst))
    pt.on_fill(_trade(Side.SELL, 75, "130", inst))
    assert pt.realized_pnl() == Decimal("2250")


# -- VWAP-cross exit ------------------------------------------------------------------


def test_short_vwap_cross_exit_arms_then_triggers():
    em = ExitManager(get_settings(reload=True))
    inst = make_instrument()
    em.register_short_vwap(inst, 75, Decimal("100"))
    sym = inst.trading_symbol
    # premium above VWAP at first -> NOT armed yet, no exit
    assert em.evaluate_short_vwap(sym, Decimal("105"), vwap=Decimal("100")) is None
    # premium drops to/below VWAP -> arms
    assert em.evaluate_short_vwap(sym, Decimal("98"), vwap=Decimal("100")) is None
    # crosses back above VWAP -> stop-loss
    assert em.evaluate_short_vwap(sym, Decimal("101"), vwap=Decimal("100")) is ExitReason.VWAP_CROSS


def test_short_vwap_no_exit_when_below():
    em = ExitManager(get_settings(reload=True))
    inst = make_instrument()
    em.register_short_vwap(inst, 75, Decimal("100"))
    sym = inst.trading_symbol
    em.evaluate_short_vwap(sym, Decimal("95"), vwap=Decimal("100"))  # arm
    assert em.evaluate_short_vwap(sym, Decimal("96"), vwap=Decimal("100")) is None  # still below


# -- Buy-to-close translation ---------------------------------------------------------


def test_build_exit_buys_to_close_a_short(instrument_factory):
    st = SignalTranslator(get_settings(reload=True), resolver=None)  # resolver unused here
    inst = instrument_factory()
    req = st.build_exit(inst, 75, Decimal("80"), position_side=Side.SELL)
    assert req.side is Side.BUY  # close a short by buying


def test_build_exit_sells_to_close_a_long(instrument_factory):
    st = SignalTranslator(get_settings(reload=True), resolver=None)
    inst = instrument_factory()
    req = st.build_exit(inst, 75, Decimal("80"), position_side=Side.BUY)
    assert req.side is Side.SELL


# -- Margin pre-check -----------------------------------------------------------------


def test_margin_check(engine):
    s = get_settings(reload=True)
    object.__setattr__(s, "margin_buffer", Decimal("0.1"))
    rm = RiskManager(s, Repository(engine), PositionTracker())
    assert rm.margin_ok(Decimal("100000"), Decimal("120000")) is True   # 110k needed <= 120k
    assert rm.margin_ok(Decimal("100000"), Decimal("105000")) is False  # 110k needed > 105k
    assert rm.margin_ok(Decimal("0"), Decimal("0")) is True             # no requirement


# -- Per-underlying lot-size override -------------------------------------------------


def test_effective_lot_size_override():
    from algo_trading.domain.enums import Underlying
    s = get_settings(reload=True)
    object.__setattr__(s, "nifty_lot_size", 65)
    object.__setattr__(s, "sensex_lot_size", 20)
    assert s.effective_lot_size(Underlying.NIFTY, 75) == 65   # override wins
    assert s.effective_lot_size(Underlying.SENSEX, 25) == 20
    # 0 override -> fall back to the scrip lot size
    object.__setattr__(s, "nifty_lot_size", 0)
    assert s.effective_lot_size(Underlying.NIFTY, 75) == 75


def test_translator_uses_configured_lot_size():
    from datetime import UTC, datetime
    from decimal import Decimal

    import pandas as pd
    from freezegun import freeze_time

    from algo_trading.domain.enums import ExchangeSegment, OptionType, Side, Underlying
    from algo_trading.domain.models import Signal
    from algo_trading.execution.signal_translator import SignalTranslator
    from algo_trading.instruments.option_resolver import WeeklyOptionResolver
    from algo_trading.instruments.scrip_master import ScripMaster

    rows = []
    for k in range(22800, 23400, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}", "pSymbolName": "NIFTY",
                         "pExpiryDate": "2099-01-30", "dStrikePrice": k, "pOptionType": ot, "lLotSize": 75})
    sm = ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)
    s = get_settings(reload=True)
    object.__setattr__(s, "nifty_lot_size", 65)
    object.__setattr__(s, "lots", 2)
    sig = Signal(underlying=Underlying.NIFTY, side=Side.SELL, option_type=OptionType.CE,
                 reference_price=Decimal("23000"), timestamp=datetime(2099, 1, 20, tzinfo=UTC),
                 target_strike=Decimal("23200"))
    with freeze_time("2099-01-27"):
        req = SignalTranslator(s, WeeklyOptionResolver(sm)).translate(sig)
    assert req.quantity == 130  # 2 lots × 65 (configured), not × 75 (scrip)
