"""Batched writer for option-chain snapshots.

Buffers snapshot rows and flushes to the DB when the buffer reaches ``max_buffer`` rows or
``flush_seconds`` have elapsed — so 22 continuously-streaming contracts don't overwhelm the DB
with per-tick inserts. Also enforces a per-token minimum interval to bound write volume.
The clock is injectable for deterministic tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from algo_trading.observability.logging import get_logger
from algo_trading.persistence.repositories import Repository

log = get_logger("persistence.snapshot_writer")


class SnapshotWriter:
    def __init__(
        self,
        repo: Repository,
        *,
        max_buffer: int = 100,
        flush_seconds: float = 2.0,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._max_buffer = max_buffer
        self._flush_seconds = flush_seconds
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._buffer: list[dict] = []
        self._last_flush = clock()
        self._last_per_token: dict[str, float] = {}

    def add(self, snapshot: dict) -> None:
        """Buffer a snapshot; drops it if this token was written within min_interval."""
        now = self._clock()
        token = str(snapshot["instrument_token"])
        if self._min_interval > 0:
            last = self._last_per_token.get(token)
            if last is not None and (now - last) < self._min_interval:
                return
        self._last_per_token[token] = now
        self._buffer.append(snapshot)
        if len(self._buffer) >= self._max_buffer or (now - self._last_flush) >= self._flush_seconds:
            self.flush()

    def flush(self) -> int:
        if not self._buffer:
            self._last_flush = self._clock()
            return 0
        rows, self._buffer = self._buffer, []
        self._last_flush = self._clock()
        written = self._repo.write_chain_snapshots(rows)
        log.debug("snapshots_flushed", count=written)
        return written

    @property
    def pending(self) -> int:
        return len(self._buffer)
