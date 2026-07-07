"""Fill P&L summary tests."""

from __future__ import annotations

from decimal import Decimal

from algo_trading.domain.enums import Side
from algo_trading.domain.models import Trade
from algo_trading.reporting import summarize_fills
from tests.conftest import make_instrument


def _t(symbol, side, qty, price):
    inst = make_instrument()
    # override the trading symbol
    inst = inst.model_copy(update={"trading_symbol": symbol})
    return Trade(client_tag=f"{symbol}-{side.value}-{price}", broker_order_id="B",
                 instrument=inst, side=side, quantity=qty, price=Decimal(price))


def test_realized_pnl_round_trip():
    # buy 75 @100, sell 75 @130 -> realized (130-100)*75 = 2250
    s = summarize_fills([_t("NIFTYCE", Side.BUY, 75, "100"), _t("NIFTYCE", Side.SELL, 75, "130")])
    assert s.total_realized == Decimal("2250")
    assert s.per_symbol[0].matched_qty == 75
    assert s.per_symbol[0].net_qty == 0
    assert s.open_symbols == 0


def test_order_independent():
    # sell recorded before buy -> same result (average-price matching)
    s = summarize_fills([_t("X", Side.SELL, 50, "130"), _t("X", Side.BUY, 50, "100")])
    assert s.total_realized == Decimal("1500")  # (130-100)*50


def test_partial_open_position_excluded():
    # buy 100 @100, sell 40 @110 -> matched 40 -> realized (110-100)*40 = 400; net +60 open
    s = summarize_fills([_t("Y", Side.BUY, 100, "100"), _t("Y", Side.SELL, 40, "110")])
    row = s.per_symbol[0]
    assert row.matched_qty == 40
    assert row.realized_pnl == Decimal("400")
    assert row.net_qty == 60
    assert s.open_symbols == 1


def test_multi_symbol_totals_and_sorting():
    trades = [
        _t("WIN", Side.BUY, 75, "100"), _t("WIN", Side.SELL, 75, "150"),   # +3750
        _t("LOSS", Side.BUY, 75, "100"), _t("LOSS", Side.SELL, 75, "60"),  # -3000
    ]
    s = summarize_fills(trades)
    assert s.total_realized == Decimal("750")
    assert s.trade_count == 4
    assert s.matched_symbols == 2
    # sorted worst-first
    assert s.per_symbol[0].symbol == "LOSS"
    assert s.per_symbol[-1].symbol == "WIN"


def test_empty():
    s = summarize_fills([])
    assert s.total_realized == Decimal(0) and s.trade_count == 0 and s.per_symbol == []
