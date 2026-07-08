"""Fill P&L summary tests."""

from __future__ import annotations

from decimal import Decimal

from algo_trading.domain.enums import Side
from algo_trading.domain.models import Trade
from algo_trading.reporting import summarize_broker_positions, summarize_fills
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


# -- Chain summary --------------------------------------------------------------------


class _Row:
    def __init__(self, strike, ot, oi, ltp):
        self.strike = strike
        self.option_type = ot
        self.oi = oi
        self.ltp = ltp


def test_summarize_chain_pivots_and_selects_side():
    from algo_trading.reporting import summarize_chain
    rows = [
        _Row("23000", "CE", 5000, "120"), _Row("23000", "PE", 1000, "80"),
        _Row("23050", "CE", 4000, "90"), _Row("23050", "PE", 2000, "100"),
    ]
    s = summarize_chain(rows)
    assert s.ce_oi_total == 9000
    assert s.pe_oi_total == 3000
    assert s.selected_side == "CE"  # CE OI dominant
    assert [str(x.strike) for x in s.per_strike] == ["23000", "23050"]  # sorted
    assert s.per_strike[0].ce_oi == 5000 and s.per_strike[0].pe_oi == 1000


def test_summarize_chain_tie():
    from algo_trading.reporting import summarize_chain
    rows = [_Row("23000", "CE", 1000, "1"), _Row("23000", "PE", 1000, "1")]
    assert summarize_chain(rows).selected_side == "—"


def test_summarize_chain_empty():
    from algo_trading.reporting import summarize_chain
    s = summarize_chain([])
    assert s.per_strike == [] and s.ce_oi_total == 0 and s.selected_side == "—"

def test_broker_positions_realized_and_open():
    # A squared position (buy 200 @183.31, sell 200 @189.585) and a fully-open short.
    rows = [
        {"trdSym": "SENSEX77800CE", "flBuyQty": "200", "flSellQty": "200",
         "buyAmt": "36662.00", "sellAmt": "37917.00"},
        {"trdSym": "SENSEX77500PE", "flBuyQty": "0", "flSellQty": "200",
         "buyAmt": "0.00", "sellAmt": "31324.00"},
    ]
    s = summarize_broker_positions(rows)
    assert s.total_realized == Decimal("1255.00")   # only the squared leg books P&L
    assert s.open_count == 1
    squared = next(p for p in s.per_position if p.symbol == "SENSEX77800CE")
    assert squared.net_qty == 0 and not squared.is_open
    short = next(p for p in s.per_position if p.symbol == "SENSEX77500PE")
    assert short.net_qty == -200 and short.is_open
    assert short.realized_pnl == Decimal("0")       # no matched qty yet


def test_broker_positions_handles_blank_fields():
    s = summarize_broker_positions([{"trdSym": "X", "flBuyQty": "", "flSellQty": "",
                                     "buyAmt": "", "sellAmt": ""}])
    assert s.total_realized == Decimal("0") and s.open_count == 0
