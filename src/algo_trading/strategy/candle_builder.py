"""Tick-to-candle aggregation with IST wall-clock-aligned boundaries.

A candle is emitted only when a tick crosses into a later interval (candle *close*), so the
strategy never evaluates a partial candle (no look-ahead). Late ticks (belonging to an already
-closed bucket) are ignored. Gaps (jumping several intervals) are allowed — the in-progress
candle closes and a new one starts at the tick's bucket.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from algo_trading.domain.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")


def bucket_start(ts: datetime, timeframe_minutes: int, tz: ZoneInfo = IST) -> datetime:
    """Floor ``ts`` to the start of its ``timeframe``-minute bucket in ``tz``."""
    local = ts.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    floored = (minutes // timeframe_minutes) * timeframe_minutes
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(minutes=floored)


class CandleBuilder:
    """Builds candles for a single symbol."""

    def __init__(self, symbol: str, timeframe_minutes: int, tz: ZoneInfo = IST) -> None:
        self.symbol = symbol
        self._tf = timeframe_minutes
        self._tz = tz
        self._start: datetime | None = None
        self._open: Decimal = Decimal(0)
        self._high: Decimal = Decimal(0)
        self._low: Decimal = Decimal(0)
        self._close: Decimal = Decimal(0)
        self._volume: Decimal = Decimal(0)

    def add_tick(self, tick: Tick) -> Candle | None:
        """Fold a tick in. Returns the just-closed candle if this tick opened a new bucket."""
        b_start = bucket_start(tick.timestamp, self._tf, self._tz)

        if self._start is None:
            self._begin(b_start, tick.ltp)
            return None

        if b_start < self._start:
            return None  # late tick for an already-closed bucket -> ignore

        if b_start == self._start:
            self._high = max(self._high, tick.ltp)
            self._low = min(self._low, tick.ltp)
            self._close = tick.ltp
            self._volume += Decimal(1)
            return None

        # tick belongs to a later bucket -> close the current candle and start a new one
        closed = self._finalize()
        self._begin(b_start, tick.ltp)
        return closed

    def flush(self) -> Candle | None:
        """Close the in-progress candle (e.g. at session end). Returns it, or None if empty."""
        if self._start is None:
            return None
        closed = self._finalize()
        self._start = None
        return closed

    def _begin(self, start: datetime, price: Decimal) -> None:
        self._start = start
        self._open = self._high = self._low = self._close = price
        self._volume = Decimal(1)

    def _finalize(self) -> Candle:
        assert self._start is not None
        return Candle(
            symbol=self.symbol,
            start=self._start,
            end=self._start + timedelta(minutes=self._tf),
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
