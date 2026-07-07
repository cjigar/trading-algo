"""Candle builder, indicators, and VWAP-breakout strategy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment, OptionType, Side, Underlying
from algo_trading.domain.models import Candle, Tick
from algo_trading.strategy.candle_builder import IST, CandleBuilder, bucket_start
from algo_trading.strategy.indicators import ATR, RollingExtrema, SessionVWAP
from algo_trading.strategy.vwap_breakout import VwapBreakoutStrategy


def _tick(price: str, ts: datetime) -> Tick:
    return Tick(
        instrument_token="NIFTY-IDX",
        exchange_segment=ExchangeSegment.NSE_FO,
        ltp=Decimal(price),
        timestamp=ts,
        is_index=True,
    )


def _candle(symbol: str, o: str, h: str, lo: str, c: str, start: datetime, vol: str = "0") -> Candle:
    return Candle(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=5),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=Decimal(vol),
    )


# -- Candle builder --------------------------------------------------------------------


def test_bucket_start_aligns_to_ist_timeframe():
    ts = datetime(2025, 1, 15, 9, 47, 30, tzinfo=IST)  # 09:47:30 IST
    assert bucket_start(ts, 5) == datetime(2025, 1, 15, 9, 45, tzinfo=IST)


def test_candle_emits_only_on_boundary_cross():
    cb = CandleBuilder("NIFTY-IDX", 5)
    base = datetime(2025, 1, 15, 9, 45, tzinfo=IST).astimezone(UTC)
    # three ticks within the same 5-min bucket -> no close yet
    assert cb.add_tick(_tick("100", base)) is None
    assert cb.add_tick(_tick("105", base + timedelta(minutes=1))) is None
    assert cb.add_tick(_tick("102", base + timedelta(minutes=2))) is None
    # a tick in the next bucket closes the first candle
    closed = cb.add_tick(_tick("110", base + timedelta(minutes=5)))
    assert closed is not None
    assert closed.open == Decimal("100")
    assert closed.high == Decimal("105")
    assert closed.low == Decimal("100")
    assert closed.close == Decimal("102")


def test_candle_ignores_late_tick():
    cb = CandleBuilder("NIFTY-IDX", 5)
    base = datetime(2025, 1, 15, 9, 45, tzinfo=IST).astimezone(UTC)
    cb.add_tick(_tick("100", base + timedelta(minutes=5)))  # opens 09:50 bucket
    # a tick from the earlier 09:45 bucket is late -> ignored, no close
    assert cb.add_tick(_tick("99", base)) is None


# -- Indicators ------------------------------------------------------------------------


def test_session_vwap_resets():
    v = SessionVWAP()
    start = datetime(2025, 1, 15, 9, 45, tzinfo=IST)
    v.update(_candle("X", "10", "12", "8", "10", start))  # typical 10
    v.update(_candle("X", "20", "22", "18", "20", start))  # typical 20
    assert v.value == Decimal(15)
    v.reset()
    assert v.value is None


def test_atr_computes_after_period():
    atr = ATR(period=2)
    start = datetime(2025, 1, 15, 9, 45, tzinfo=IST)
    assert atr.update(_candle("X", "10", "12", "8", "11", start)) is None  # first, warming up
    val = atr.update(_candle("X", "11", "15", "10", "14", start))
    assert val is not None and val > 0


def test_rolling_extrema_ready():
    re = RollingExtrema(2)
    start = datetime(2025, 1, 15, 9, 45, tzinfo=IST)
    re.update(_candle("X", "10", "12", "8", "10", start))
    assert re.ready is False
    re.update(_candle("X", "10", "15", "9", "10", start))
    assert re.ready is True
    assert re.high() == Decimal("15")
    assert re.low() == Decimal("8")


# -- Strategy --------------------------------------------------------------------------


def _feed(strategy, closes, symbol="NIFTY-IDX"):
    start = datetime(2025, 1, 15, 9, 30, tzinfo=IST)
    out = []
    for i, close in enumerate(closes):
        c = _candle(symbol, close, close, close, close, start + timedelta(minutes=5 * i))
        out.append(strategy.on_candle(c))
    return out


def test_bullish_breakout_emits_ce_buy():
    strat = VwapBreakoutStrategy(get_settings(), breakout_window=2)
    # flat then a decisive up-move above VWAP and prior highs
    results = _feed(strat, ["100", "100", "100", "130"])
    signals = [s for r in results for s in r]
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side is Side.BUY and sig.option_type is OptionType.CE
    assert sig.underlying is Underlying.NIFTY


def test_bearish_breakout_emits_pe_buy():
    strat = VwapBreakoutStrategy(get_settings(), breakout_window=2)
    results = _feed(strat, ["100", "100", "100", "70"])
    signals = [s for r in results for s in r]
    assert len(signals) == 1
    assert signals[0].side is Side.BUY and signals[0].option_type is OptionType.PE


def test_no_signal_when_range_bound():
    strat = VwapBreakoutStrategy(get_settings(), breakout_window=2)
    results = _feed(strat, ["100", "101", "100", "101", "100"])
    assert all(r == [] for r in results)


def test_no_signal_before_window_ready():
    strat = VwapBreakoutStrategy(get_settings(), breakout_window=3)
    # only 2 candles -> window (3) never ready -> no signal even on a jump
    results = _feed(strat, ["100", "200"])
    assert all(r == [] for r in results)
