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
    def __init__(self, strike, ot, oi, ltp, token=""):
        self.strike = strike
        self.option_type = ot
        self.oi = oi
        self.ltp = ltp
        self.instrument_token = token


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


# -- OI trend classification (pure function) -------------------------------------------


def test_oi_trend_up_down_flat_na():
    from algo_trading.reporting import TREND_DOWN, TREND_FLAT, TREND_NA, TREND_UP, oi_trend
    assert oi_trend(1500, 1000).direction == TREND_UP
    assert oi_trend(1500, 1000).delta == 500
    assert oi_trend(800, 1000).direction == TREND_DOWN
    assert oi_trend(800, 1000).delta == -200
    assert oi_trend(1000, 1000).direction == TREND_FLAT
    # no anchor -> na, delta None (NOT flat-by-default)
    assert oi_trend(1500, None).direction == TREND_NA
    assert oi_trend(1500, None).delta is None


def test_oi_trend_flat_threshold():
    from algo_trading.reporting import TREND_FLAT, TREND_UP, oi_trend
    # within threshold -> flat; beyond -> directional
    assert oi_trend(1050, 1000, flat_threshold=50).direction == TREND_FLAT
    assert oi_trend(1051, 1000, flat_threshold=50).direction == TREND_UP


def test_summarize_chain_computes_per_window_trends():
    from algo_trading.reporting import TREND_DOWN, TREND_NA, TREND_UP, summarize_chain
    rows = [
        _Row("23000", "CE", 5000, "120", token="CE1"),
        _Row("23000", "PE", 1000, "80", token="PE1"),
    ]
    anchors = {
        1: {"CE1": 4000, "PE1": 1200},  # CE up 1000, PE down 200
        3: {"CE1": 4500},               # PE1 missing -> na for PE in 3m
        5: {},                          # both missing -> na
    }
    s = summarize_chain(rows, oi_anchors=anchors, trend_windows=[1, 3, 5])
    strike = s.per_strike[0]
    assert strike.ce_oi_trends[1].direction == TREND_UP
    assert strike.ce_oi_trends[1].delta == 1000
    assert strike.pe_oi_trends[1].direction == TREND_DOWN
    assert strike.ce_oi_trends[3].direction == TREND_UP
    assert strike.pe_oi_trends[3].direction == TREND_NA   # anchor missing
    assert strike.ce_oi_trends[5].direction == TREND_NA
    # all configured windows present for both sides
    assert set(strike.ce_oi_trends) == {1, 3, 5}
    assert set(strike.pe_oi_trends) == {1, 3, 5}


def test_summarize_chain_preserves_day_open_chg_oi_with_trends():
    from algo_trading.reporting import summarize_chain
    rows = [_Row("23000", "CE", 5000, "120", token="CE1")]
    s = summarize_chain(
        rows,
        oi_baseline={"CE1": 4000},          # day-open baseline -> chg 1000
        oi_anchors={1: {"CE1": 4800}},      # 1m trend -> up 200
        trend_windows=[1],
    )
    strike = s.per_strike[0]
    assert strike.ce_chg_oi == 1000        # unchanged day-open behavior
    assert strike.ce_oi_trends[1].delta == 200


def test_summarize_chain_without_anchors_has_empty_trends():
    from algo_trading.reporting import summarize_chain
    rows = [_Row("23000", "CE", 5000, "120", token="CE1")]
    s = summarize_chain(rows)  # no anchors/windows -> backward compatible
    assert s.per_strike[0].ce_oi_trends == {}

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


def test_broker_positions_live_m2m_marks_open_short():
    # Open short 200 @156.62 avg; buyback LTP 150 -> unrealized +6.62*200 = 1324.
    rows = [{"trdSym": "SENSEX77500PE", "tok": "835470", "flBuyQty": "0", "flSellQty": "200",
             "buyAmt": "0.00", "sellAmt": "31324.00"}]
    s = summarize_broker_positions(rows, {"835470": Decimal("150")})
    p = s.per_position[0]
    # (sellAmt - buyAmt) + net*ltp = 31324 + (-200)*150 = 1324
    assert p.total_pnl == Decimal("1324.00")
    assert p.ltp == Decimal("150") and not p.mtm_pending
    assert s.total_pnl == Decimal("1324.00")
    assert s.total_realized == Decimal("0")  # nothing squared yet
    assert s.mtm_pending_count == 0


def test_broker_positions_live_m2m_marks_open_long():
    # Open long 100 @100 avg; LTP 130 -> unrealized +30*100 = 3000.
    rows = [{"trdSym": "X", "tok": "t1", "flBuyQty": "100", "flSellQty": "0",
             "buyAmt": "10000.00", "sellAmt": "0.00"}]
    s = summarize_broker_positions(rows, {"t1": Decimal("130")})
    # (0 - 10000) + 100*130 = 3000
    assert s.per_position[0].total_pnl == Decimal("3000.00")


def test_broker_positions_squared_needs_no_quote():
    rows = [{"trdSym": "S", "tok": "t2", "flBuyQty": "200", "flSellQty": "200",
             "buyAmt": "36662.00", "sellAmt": "37917.00"}]
    s = summarize_broker_positions(rows, {})  # no quote
    p = s.per_position[0]
    assert p.total_pnl == Decimal("1255.00") == p.realized_pnl  # sell-buy, no MTM term
    assert not p.mtm_pending and p.ltp is None


def test_broker_positions_open_without_quote_is_pending():
    rows = [{"trdSym": "P", "tok": "t3", "flBuyQty": "0", "flSellQty": "200",
             "buyAmt": "0.00", "sellAmt": "31324.00"}]
    s = summarize_broker_positions(rows, {})  # open, unpriced
    p = s.per_position[0]
    assert p.mtm_pending and p.ltp is None
    assert p.total_pnl == p.realized_pnl == Decimal("0")  # falls back to realized
    assert s.mtm_pending_count == 1


def test_summarize_broker_positions_attaches_vwap_by_token():
    from decimal import Decimal

    from algo_trading.reporting import summarize_broker_positions

    rows = [
        {"tok": "T1", "trdSym": "NIFTY23000CE", "flBuyQty": "0", "flSellQty": "75",
         "buyAmt": "0", "sellAmt": "7500"},   # open short, has vwap
        {"tok": "T2", "trdSym": "NIFTY23100PE", "flBuyQty": "75", "flSellQty": "75",
         "buyAmt": "7000", "sellAmt": "7500"},  # squared, no vwap in map
    ]
    s = summarize_broker_positions(rows, quotes={"T1": Decimal("90")}, vwaps={"T1": Decimal("88.5")})
    by_sym = {p.symbol: p for p in s.per_position}
    assert by_sym["NIFTY23000CE"].vwap == Decimal("88.5")
    assert by_sym["NIFTY23100PE"].vwap is None  # absent from vwaps -> None, not 0


def test_summarize_chain_maps_vwap_per_side():
    from types import SimpleNamespace

    from algo_trading.reporting import summarize_chain

    rows = [
        SimpleNamespace(strike="100", option_type="CE", oi=10, ltp="5.5", instrument_token="c", vwap="5.0"),
        SimpleNamespace(strike="100", option_type="PE", oi=20, ltp="4.5", instrument_token="p", vwap=None),
    ]
    cs = summarize_chain(rows)
    row = cs.per_strike[0]
    assert row.ce_vwap == Decimal("5.0")
    assert row.pe_vwap is None
