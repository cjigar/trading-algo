from datetime import datetime, timedelta
from decimal import Decimal

from algo_trading.analytics import indicators as ind
from algo_trading.domain.models import Candle


def _c(o, h, lo, c, i=0):
    start = datetime(2025, 1, 15, 9, 15) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c),
                  volume=Decimal(1))


def _closes(vals):
    return [Decimal(v) for v in vals]


def test_ema_of_constant_series_is_the_constant():
    assert ind.ema_last(_closes([10, 10, 10, 10]), 3) == Decimal(10)


def test_ema_insufficient_data_returns_none():
    assert ind.ema_last(_closes([10, 11]), 3) is None


def test_rsi_all_gains_is_100():
    assert ind.rsi_last(_closes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]), 14) == Decimal(100)


def test_rsi_all_losses_is_0():
    assert ind.rsi_last(_closes(list(range(20, 5, -1))), 14) == Decimal(0)


def test_macd_of_constant_series_is_zero():
    macd, signal, hist = ind.macd_last(_closes([50] * 40))
    assert macd == Decimal(0) and signal == Decimal(0) and hist == Decimal(0)


def test_bollinger_of_constant_series_collapses_to_the_mean():
    upper, mid, lower = ind.bollinger_last(_closes([100] * 20), 20, Decimal(2))
    assert upper == Decimal(100) and mid == Decimal(100) and lower == Decimal(100)


def test_supertrend_uptrend_is_up_and_below_price():
    candles = [_c(100 + i, 101 + i, 99 + i, 100.5 + i, i) for i in range(30)]
    line, direction = ind.supertrend_last(candles, 10, Decimal(3))
    assert direction == "up"
    assert line < candles[-1].close


def test_rsi_label_boundaries():
    assert ind.rsi_label(Decimal(65)) == ind.BULLISH
    assert ind.rsi_label(Decimal(35)) == ind.BEARISH
    assert ind.rsi_label(Decimal(50)) == ind.NEUTRAL
    assert ind.rsi_label(None) == ind.NA


def test_ema_label_stacked_up_is_bullish():
    assert ind.ema_label(Decimal(21), Decimal(20), Decimal(19), Decimal(22)) == ind.BULLISH
    assert ind.ema_label(Decimal(19), Decimal(20), Decimal(21), Decimal(18)) == ind.BEARISH
    assert ind.ema_label(Decimal(20), Decimal(20), Decimal(20), Decimal(20)) == ind.NEUTRAL
    assert ind.ema_label(None, Decimal(20), Decimal(21), Decimal(20)) == ind.NA
