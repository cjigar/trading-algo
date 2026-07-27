"""Pure, batch technical indicators for the display-only Nifty panel.

Every function takes a plain list of closed candles / closes (oldest -> newest) and returns the
LATEST value(s), or None when there is not enough data to be meaningful (warm-up). No DB, no
network, no mutation of the live trading loop. Wilder's ATR and session VWAP are reused from the
existing incremental classes in strategy/indicators.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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


@dataclass(frozen=True)
class Cell:
    """One indicator's rendered state: a trend label plus named numeric values (JSON-ready floats)."""

    label: str
    values: dict[str, float]


def _f(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def orb_levels(
    candles: list[Candle], market_open: time, orb_minutes: int, tz: ZoneInfo
) -> tuple[Decimal, Decimal] | None:
    """(high, low) of the opening range. None if no candle falls in the window yet."""
    open_dt_by_day: dict = {}
    window: list[Candle] = []
    for c in candles:
        local = c.start.astimezone(tz)
        day = local.date()
        if day not in open_dt_by_day:
            open_dt_by_day[day] = datetime.combine(day, market_open, tzinfo=tz)
        start_of_range = open_dt_by_day[day]
        end_of_range = start_of_range + timedelta(minutes=orb_minutes)
        if start_of_range <= local < end_of_range:
            window.append(c)
    if not window:
        return None
    return max(c.high for c in window), min(c.low for c in window)


def orb_cell(or_high: Decimal | None, or_low: Decimal | None, price: Decimal | None) -> Cell:
    values = {"or_high": _f(or_high), "or_low": _f(or_low), "price": _f(price)}
    if or_high is None or or_low is None or price is None:
        return Cell(NA, values)
    if price > or_high:
        return Cell(BULLISH, values)
    if price < or_low:
        return Cell(BEARISH, values)
    return Cell(NEUTRAL, values)


def compute_timeframe(candles: list[Candle], price: Decimal | None) -> dict[str, Cell]:
    closes = [c.close for c in candles]
    px = price if price is not None else (closes[-1] if closes else None)

    e9, e21, e50 = ema_last(closes, 9), ema_last(closes, 21), ema_last(closes, 50)
    ema = Cell(ema_label(e9, e21, e50, px), {"ema9": _f(e9), "ema21": _f(e21), "ema50": _f(e50)})

    vw = vwap_last(candles)
    vwap = Cell(vwap_label(px, vw), {"vwap": _f(vw)})

    r = rsi_last(closes, 14)
    rsi = Cell(rsi_label(r), {"rsi": _f(r)})

    m = macd_last(closes)
    if m is None:
        macd = Cell(NA, {"macd": None, "signal": None, "hist": None})
    else:
        macd = Cell(macd_label(*m), {"macd": _f(m[0]), "signal": _f(m[1]), "hist": _f(m[2])})

    b = bollinger_last(closes)
    if b is None or px is None:
        bollinger = Cell(NA, {"upper": None, "mid": None, "lower": None})
    else:
        upper, mid, lower = b
        label = BULLISH if px > upper else (BEARISH if px < lower else NEUTRAL)
        bollinger = Cell(label, {"upper": _f(upper), "mid": _f(mid), "lower": _f(lower),
                                 "bandwidth": _f(upper - lower)})

    a = atr_last(candles, 14)
    atr = Cell(NEUTRAL if a is not None else NA,
               {"atr": _f(a), "atr_pct": _f((a / px * Decimal(100)) if (a is not None and px) else None)})

    st = supertrend_last(candles)
    if st is None:
        supertrend = Cell(NA, {"line": None})
    else:
        line, direction = st
        supertrend = Cell(supertrend_label(px, line, direction), {"line": _f(line)})

    return {"ema": ema, "vwap": vwap, "rsi": rsi, "macd": macd,
            "bollinger": bollinger, "atr": atr, "supertrend": supertrend}


def composite_of(cells: list[Cell]) -> tuple[str, str]:
    bull = sum(1 for c in cells if c.label == BULLISH)
    bear = sum(1 for c in cells if c.label == BEARISH)
    tally = f"{bull} bull / {bear} bear"
    if bull > bear:
        return BULLISH, tally
    if bear > bull:
        return BEARISH, tally
    return NEUTRAL, tally


@dataclass(frozen=True)
class IndicatorPanel:
    timeframes: dict[int, dict]
    orb: Cell
    as_of: datetime | None


def compute_panel(
    candles_by_tf: dict[int, list[Candle]],
    price: Decimal | None,
    market_open: time,
    orb_minutes: int,
    tz: ZoneInfo,
    as_of: datetime | None = None,
) -> IndicatorPanel:
    # ORB is a session-level construct: compute it once from the finest timeframe available.
    orb = Cell(NA, {"or_high": None, "or_low": None, "price": _f(price)})
    if candles_by_tf:
        finest = candles_by_tf[min(candles_by_tf)]
        levels = orb_levels(finest, market_open, orb_minutes, tz)
        orb = orb_cell(levels[0], levels[1], price) if levels else orb_cell(None, None, price)

    timeframes: dict[int, dict] = {}
    for tf, candles in candles_by_tf.items():
        cells = compute_timeframe(candles, price)
        directional = [cells["ema"], cells["vwap"], cells["rsi"], cells["macd"],
                       cells["supertrend"], orb]
        label, tally = composite_of(directional)
        timeframes[tf] = {"cells": cells, "composite": label, "composite_tally": tally}
    return IndicatorPanel(timeframes=timeframes, orb=orb, as_of=as_of)
