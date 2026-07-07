"""Indicators: session-anchored VWAP, rolling high/low, and ATR.

All are incremental (fed candles/prices as they close) and reset per session where relevant.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from algo_trading.domain.models import Candle


class SessionVWAP:
    """Session-anchored VWAP. Reset at the start of each trading session.

    When candle volume is zero (e.g. index LTP feeds without traded volume), a volume of 1 is
    used so VWAP degrades to a typical-price average rather than dividing by zero.
    """

    def __init__(self) -> None:
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._value: Decimal | None = None

    def reset(self) -> None:
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._value = None

    @staticmethod
    def typical_price(candle: Candle) -> Decimal:
        return (candle.high + candle.low + candle.close) / Decimal(3)

    def update(self, candle: Candle) -> Decimal:
        vol = candle.volume if candle.volume > 0 else Decimal(1)
        self._pv += self.typical_price(candle) * vol
        self._v += vol
        self._value = self._pv / self._v
        return self._value

    @property
    def value(self) -> Decimal | None:
        return self._value


class TickVWAP:
    """Session VWAP from individual ticks, weighted by traded quantity (falls back to equal
    weighting when volume is unavailable). Used for the option VWAP-cross exit."""

    def __init__(self) -> None:
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._value: Decimal | None = None

    def reset(self) -> None:
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._value = None

    def update(self, price: Decimal, weight: Decimal | None = None) -> Decimal:
        w = weight if (weight is not None and weight > 0) else Decimal(1)
        self._pv += price * w
        self._v += w
        self._value = self._pv / self._v
        return self._value

    @property
    def value(self) -> Decimal | None:
        return self._value


class RollingExtrema:
    """Rolling high/low over the last ``window`` candles (excludes the current one by design:
    the caller updates *after* evaluating breakout against the prior window)."""

    def __init__(self, window: int) -> None:
        self._highs: deque[Decimal] = deque(maxlen=window)
        self._lows: deque[Decimal] = deque(maxlen=window)

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()

    def update(self, candle: Candle) -> None:
        self._highs.append(candle.high)
        self._lows.append(candle.low)

    @property
    def ready(self) -> bool:
        return len(self._highs) == self._highs.maxlen

    def high(self) -> Decimal | None:
        return max(self._highs) if self._highs else None

    def low(self) -> Decimal | None:
        return min(self._lows) if self._lows else None


class ATR:
    """Wilder's Average True Range over ``period`` candles."""

    def __init__(self, period: int = 14) -> None:
        self._period = period
        self._prev_close: Decimal | None = None
        self._trs: deque[Decimal] = deque(maxlen=period)
        self._value: Decimal | None = None

    def reset(self) -> None:
        self._prev_close = None
        self._trs.clear()
        self._value = None

    def update(self, candle: Candle) -> Decimal | None:
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._prev_close = candle.close
        if self._value is None:
            self._trs.append(tr)
            if len(self._trs) == self._period:
                self._value = sum(self._trs, Decimal(0)) / Decimal(self._period)
        else:
            self._value = (self._value * (self._period - 1) + tr) / Decimal(self._period)
        return self._value

    @property
    def value(self) -> Decimal | None:
        return self._value
