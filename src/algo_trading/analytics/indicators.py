"""Pure, batch technical indicators for the display-only Nifty panel.

Every function takes a plain list of closed candles / closes (oldest -> newest) and returns the
LATEST value(s), or None when there is not enough data to be meaningful (warm-up). No DB, no
network, no mutation of the live trading loop. Wilder's ATR and session VWAP are reused from the
existing incremental classes in strategy/indicators.py.
"""

from __future__ import annotations

from decimal import Decimal

from algo_trading.domain.models import Candle
from algo_trading.strategy.indicators import ATR, SessionVWAP

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"
NA = "na"


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    """Full EMA series (seeded with the SMA of the first ``period`` values). Empty if too short."""
    if len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], Decimal(0)) / Decimal(period)
    out = [seed]
    for v in values[period:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def ema_last(values: list[Decimal], period: int) -> Decimal | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi_last(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder's RSI. Needs period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, cur in zip(closes, closes[1:], strict=False):
        diff = cur - prev
        gains.append(diff if diff > 0 else Decimal(0))
        losses.append(-diff if diff < 0 else Decimal(0))
    avg_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    for g, ls in zip(gains[period:], losses[period:], strict=False):
        avg_gain = (avg_gain * (period - 1) + g) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + ls) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100)
    if avg_gain == 0:
        return Decimal(0)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def macd_last(
    closes: list[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(macd, signal, histogram) from the latest bar. Needs slow + signal closes."""
    if len(closes) < slow + signal:
        return None
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    # Align the two EMA series on their tails (fast is longer; trim its head).
    offset = len(fast_ema) - len(slow_ema)
    macd_line = [f - s for f, s in zip(fast_ema[offset:], slow_ema, strict=True)]
    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return None
    macd_v = macd_line[-1]
    signal_v = signal_series[-1]
    return macd_v, signal_v, macd_v - signal_v


def bollinger_last(
    closes: list[Decimal], period: int = 20, mult: Decimal = Decimal(2)
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(upper, mid, lower) from the latest ``period`` closes. Population std-dev."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window, Decimal(0)) / Decimal(period)
    var = sum(((v - mid) ** 2 for v in window), Decimal(0)) / Decimal(period)
    std = var.sqrt()
    return mid + mult * std, mid, mid - mult * std


def atr_last(candles: list[Candle], period: int = 14) -> Decimal | None:
    if len(candles) < period + 1:
        return None
    atr = ATR(period)
    value = None
    for c in candles:
        value = atr.update(c)
    return value


def supertrend_last(
    candles: list[Candle], period: int = 10, mult: Decimal = Decimal(3)
) -> tuple[Decimal, str] | None:
    """(supertrend line, direction) where direction is 'up' or 'down'. Needs period+1 candles."""
    if len(candles) < period + 1:
        return None
    atr = ATR(period)
    atr_val: Decimal | None = None
    final_upper: Decimal | None = None
    final_lower: Decimal | None = None
    st: Decimal | None = None
    direction = "up"
    for c in candles:
        atr_val = atr.update(c)
        if atr_val is None:
            continue
        hl2 = (c.high + c.low) / Decimal(2)
        basic_upper = hl2 + mult * atr_val
        basic_lower = hl2 - mult * atr_val
        if final_upper is None:
            final_upper, final_lower, st, direction = basic_upper, basic_lower, basic_lower, "up"
            continue
        final_upper = basic_upper if (basic_upper < final_upper or c.close > final_upper) else final_upper
        final_lower = basic_lower if (basic_lower > final_lower or c.close < final_lower) else final_lower
        if c.close > final_upper:
            direction = "up"
        elif c.close < final_lower:
            direction = "down"
        st = final_lower if direction == "up" else final_upper
    if st is None:
        return None
    return st, direction


def vwap_last(candles: list[Candle]) -> Decimal | None:
    if not candles:
        return None
    vwap = SessionVWAP()
    for c in candles:
        vwap.update(c)
    return vwap.value


# --- trend labels -------------------------------------------------------------------------

_RSI_BULL = Decimal(60)
_RSI_BEAR = Decimal(40)


def rsi_label(rsi: Decimal | None) -> str:
    if rsi is None:
        return NA
    if rsi >= _RSI_BULL:
        return BULLISH
    if rsi <= _RSI_BEAR:
        return BEARISH
    return NEUTRAL


def ema_label(e9, e21, e50, price) -> str:
    if e9 is None or e21 is None or e50 is None or price is None:
        return NA
    if e9 > e21 > e50 and price > e9:
        return BULLISH
    if e9 < e21 < e50 and price < e9:
        return BEARISH
    return NEUTRAL


def macd_label(macd, signal, hist) -> str:
    if macd is None:
        return NA
    if macd > signal and hist > 0:
        return BULLISH
    if macd < signal and hist < 0:
        return BEARISH
    return NEUTRAL


def supertrend_label(price, line, direction) -> str:
    if line is None or price is None:
        return NA
    return BULLISH if direction == "up" else BEARISH


def vwap_label(price, vwap) -> str:
    if price is None or vwap is None:
        return NA
    if price > vwap:
        return BULLISH
    if price < vwap:
        return BEARISH
    return NEUTRAL
