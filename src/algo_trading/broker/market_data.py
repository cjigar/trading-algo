"""Live quote/LTP websocket feed handler.

Wraps the Kotak Neo quote websocket: subscribes to instrument tokens, normalizes raw
messages into :class:`Tick`, tracks a heartbeat for stale-feed detection, and reconnects
with exponential backoff, resubscribing all active tokens on reconnect (the SDK's own
reconnect is unreliable).

The raw-message normalization and stale detection are pure and unit-testable; the websocket
wiring is thin glue around them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from algo_trading.domain.enums import ExchangeSegment
from algo_trading.domain.models import Tick
from algo_trading.observability.logging import get_logger

log = get_logger("broker.market_data")

TickCallback = Callable[[Tick], None]


def normalize_tick(raw: dict, *, now: datetime | None = None) -> Tick | None:
    """Convert a raw Kotak quote message into a :class:`Tick`, or None if not a quote.

    Kotak messages vary; we accept common LTP keys and an instrument-token/segment pair.
    """
    token = raw.get("instrument_token") or raw.get("tk") or raw.get("token")
    ltp_raw = raw.get("ltp") or raw.get("last_traded_price") or raw.get("lp")
    if token is None or ltp_raw is None:
        return None
    try:
        ltp = Decimal(str(ltp_raw))
    except (InvalidOperation, ValueError):
        return None
    seg_raw = raw.get("exchange_segment") or raw.get("e") or ExchangeSegment.NSE_FO.value
    try:
        segment = ExchangeSegment(seg_raw)
    except ValueError:
        segment = ExchangeSegment.NSE_FO
    return Tick(
        instrument_token=str(token),
        exchange_segment=segment,
        ltp=ltp,
        timestamp=now or datetime.now(UTC),
        is_index=bool(raw.get("is_index", False)),
        oi=_int_or_none(raw.get("oi") or raw.get("openInterest") or raw.get("OI") or raw.get("oI")),
        volume=_int_or_none(raw.get("volume") or raw.get("vol") or raw.get("v") or raw.get("ltq")),
    )


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


class FeedHandler:
    def __init__(
        self,
        settings,
        on_tick: TickCallback,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._on_tick = on_tick
        self._clock = clock
        self._sleep = sleep
        self._neo: Any | None = None
        self._subscriptions: dict[str, dict] = {}  # token -> {instrument_token, exchange_segment}
        self._last_tick_at: float | None = None
        self._lock = threading.RLock()
        self._max_backoff = 30.0
        self._reconnecting = False

    def bind(self, neo_client: Any) -> None:
        self._neo = neo_client
        self._wire_callbacks()

    def _wire_callbacks(self) -> None:
        if self._neo is None:
            return
        self._neo.on_message = self._handle_message
        self._neo.on_error = self._handle_error
        self._neo.on_close = self._handle_close
        self._neo.on_open = lambda *_: log.info("quote_ws_open")

    # -- Subscription ------------------------------------------------------------------

    def subscribe(self, tokens: list[dict], is_index: bool = False) -> None:
        """Subscribe to instrument tokens (list of {instrument_token, exchange_segment})."""
        with self._lock:
            for t in tokens:
                self._subscriptions[str(t["instrument_token"])] = t
        if self._neo is not None:
            self._neo.subscribe(instrument_tokens=tokens, isIndex=is_index, isDepth=False)
            log.info("quote_ws_subscribe", count=len(tokens), is_index=is_index)

    def _resubscribe_all(self) -> None:
        with self._lock:
            tokens = list(self._subscriptions.values())
        if tokens and self._neo is not None:
            self._neo.subscribe(instrument_tokens=tokens, isIndex=False, isDepth=False)
            log.info("quote_ws_resubscribe", count=len(tokens))

    # -- Message handling --------------------------------------------------------------

    def _handle_message(self, message: Any) -> None:
        records = message if isinstance(message, list) else [message]
        for raw in records:
            if isinstance(raw, dict):
                self.handle_raw(raw)

    def handle_raw(self, raw: dict) -> None:
        """Normalize a single raw quote record into a Tick and publish it."""
        tick = normalize_tick(raw)
        if tick is not None:
            self._last_tick_at = self._clock()
            try:
                self._on_tick(tick)
            except Exception:  # noqa: BLE001 - never let a consumer error kill the feed
                log.exception("tick_consumer_error")

    def _handle_error(self, error: Any) -> None:
        log.warning("quote_ws_error", error=str(error))
        self.reconnect()

    def _handle_close(self, *args: Any) -> None:
        log.warning("quote_ws_closed")
        self.reconnect()

    # -- Reconnect / heartbeat ---------------------------------------------------------

    def reconnect(self, max_attempts: int = 6) -> bool:
        """Reconnect with exponential backoff and resubscribe. Returns True on success.

        Guarded against re-entry. On an abrupt connection loss the SDK's ``subscribe()``
        stands up a fresh websocket; if that attempt fails it fires ``on_error`` *from
        inside this call*, which lands back here. Without the guard each level fans out
        into ``max_attempts`` more levels (6, 36, 216, ...), and since every attempt leaks
        the socket and thread of the connection it just failed to establish, the process
        exhausts its FD limit within minutes. Observed in production: 14,306 threads and
        1024/1024 FDs, which wedges the feed permanently while the process still looks
        healthy to Docker.
        """
        with self._lock:
            if self._reconnecting:
                # A reconnect is already walking its attempts; this call arrived via an
                # on_error fired by that reconnect's own subscribe(). Let the outer loop
                # own the retries.
                log.debug("quote_ws_reconnect_reentered")
                return False
            self._reconnecting = True

        try:
            backoff = 1.0
            for attempt in range(1, max_attempts + 1):
                try:
                    self._resubscribe_all()
                    log.info("quote_ws_reconnected", attempt=attempt)
                    return True
                except Exception as exc:  # noqa: BLE001
                    log.warning("quote_ws_reconnect_failed", attempt=attempt, error=str(exc))
                    self._sleep(min(backoff, self._max_backoff))
                    backoff *= 2
            return False
        finally:
            with self._lock:
                self._reconnecting = False

    def is_stale(self, now: float | None = None) -> bool:
        """True if no tick has arrived within the configured stale-feed window."""
        if self._last_tick_at is None:
            return False  # not started yet; not "stale"
        now = now if now is not None else self._clock()
        return (now - self._last_tick_at) > self._settings.stale_feed_seconds

    def mark_started(self) -> None:
        self._last_tick_at = self._clock()
