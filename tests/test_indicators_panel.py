from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from algo_trading.analytics import indicators as ind
from algo_trading.domain.models import Candle

IST = ZoneInfo("Asia/Kolkata")


def _c(i, o, h, lo, c):
    start = datetime(2025, 1, 15, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c),
                  volume=Decimal(1))


def test_orb_levels_over_first_30_min():
    # 09:15-09:45 = six 5-min candles (i=0..5); i>=6 is after the window.
    candles = [_c(i, 100, 100 + i, 90 - i, 100) for i in range(10)]
    high, low = ind.orb_levels(candles, time(9, 15), 30, IST)
    assert high == Decimal(105)   # max high in i=0..5 is 100+5
    assert low == Decimal(85)     # min low in i=0..5 is 90-5


def test_orb_cell_breakout_and_breakdown():
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(106)).label == ind.BULLISH
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(84)).label == ind.BEARISH
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(95)).label == ind.NEUTRAL
    assert ind.orb_cell(None, None, Decimal(95)).label == ind.NA


def test_compute_timeframe_warmup_gives_na_cells():
    cells = ind.compute_timeframe([_c(0, 100, 101, 99, 100)], Decimal(100))
    assert cells["ema"].label == ind.NA
    assert cells["rsi"].label == ind.NA


def test_composite_majority_bullish():
    bull = ind.Cell(ind.BULLISH, {})
    bear = ind.Cell(ind.BEARISH, {})
    neut = ind.Cell(ind.NEUTRAL, {})
    label, tally = ind.composite_of([bull, bull, bull, bear, neut])
    assert label == ind.BULLISH
    assert tally == "3 bull / 1 bear"


def test_compute_panel_shape():
    candles = [_c(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(40)]
    panel = ind.compute_panel({5: candles, 15: candles}, Decimal(140), time(9, 15), 30, IST)
    assert set(panel.timeframes.keys()) == {5, 15}
    assert "cells" in panel.timeframes[5] and "composite" in panel.timeframes[5]
    assert panel.orb.label in (ind.BULLISH, ind.BEARISH, ind.NEUTRAL, ind.NA)


def _c_day2(i, o, h, lo, c):
    start = datetime(2025, 1, 16, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c),
                  volume=Decimal(1))


def test_vwap_uses_only_latest_session():
    # Day 1 all at 100, day 2 all at 200. Session VWAP must be 200 (latest day only),
    # NOT a multi-day cumulative (~150).
    day1 = [_c(i, 100, 100, 100, 100) for i in range(6)]
    day2 = [_c_day2(i, 200, 200, 200, 200) for i in range(6)]
    assert ind.vwap_last(day1 + day2) == Decimal(200)


def test_orb_uses_only_latest_session():
    # Day 1 opening range high 150/low 50; day 2 opening range high 110/low 90.
    # ORB must reflect day 2 only, not the widest across both days.
    day1 = [_c(i, 100, 150, 50, 100) for i in range(6)]
    day2 = [_c_day2(i, 100, 110, 90, 100) for i in range(6)]
    high, low = ind.orb_levels(day1 + day2, time(9, 15), 30, IST)
    assert high == Decimal(110)
    assert low == Decimal(90)
