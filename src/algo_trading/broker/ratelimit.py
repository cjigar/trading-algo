"""Per-key sliding-window rate limiter.

Used to enforce the regulatory cap of at most N order operations per second per exchange.
The clock and sleep functions are injectable so the limiter is deterministically testable.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_events: int,
        per_seconds: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max = max_events
        self._window = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        q = self._events[key]
        while q and now - q[0] >= self._window:
            q.popleft()

    def acquire(self, key: str) -> float:
        """Block until an event is permitted for ``key``. Returns the seconds slept."""
        now = self._clock()
        self._prune(key, now)
        q = self._events[key]
        slept = 0.0
        if len(q) >= self._max:
            wait = self._window - (now - q[0])
            if wait > 0:
                self._sleep(wait)
                slept = wait
                now = self._clock()
                self._prune(key, now)
        self._events[key].append(now)
        return slept

    def would_block(self, key: str) -> bool:
        now = self._clock()
        self._prune(key, now)
        return len(self._events[key]) >= self._max
