"""Telegram alerting — near-real-time operator notifications from the trading loop.

Log-driven: a structlog processor (:mod:`observability.logging`) hands each redacted event to
:func:`alert_event`, which classifies it into one of four categories and enqueues a formatted
message on a background :class:`TelegramAlerter`. The sender is best-effort and isolated — a Telegram
outage, a bad token, or a network error can never raise into or stall the trading loop.

The bot token is read from ``TELEGRAM_BOT_TOKEN`` in the environment (never from Settings, never
committed) and registered for log redaction. Enabled only when ``alerts_enabled`` + a token + a
chat id are all present; otherwise every entry point here is a silent no-op.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import urllib.request
from collections.abc import Callable
from time import monotonic
from typing import Any

import structlog

log = structlog.get_logger("observability.alerts")

# event name -> category. Levels handle the "unexpected errors" catch-all separately.
_CRITICAL = {"kill_switch", "manual_halt", "order_rejected"}
_HEALTH = {"starting", "boot_armed", "reexec", "session_stopped"}
_TRADE = {"fill_recorded", "exit_triggered", "short_exit_triggered"}

_PREFIX = {"critical": "🔴", "health": "🟡", "error": "⚠️", "trade": "💹"}

# Fields worth surfacing in a message, in priority order (only those present are shown).
_FIELDS = (
    "reason", "symbol", "side", "quantity", "price", "day_pnl", "cap",
    "mode", "live_armed", "strategy", "status", "order_id",
)


def _http_post(url: str, data: dict[str, Any]) -> None:
    """POST form-encoded ``data`` to ``url``; raise on transport/HTTP error."""
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - fixed api.telegram.org host
        if resp.status >= 400:
            raise RuntimeError(f"telegram http {resp.status}")


class TelegramAlerter:
    """Queued, throttled, fail-safe Telegram sender. ``send`` only enqueues; a daemon thread
    delivers. ``transport``/``clock`` are injectable for tests."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        throttle_seconds: int = 300,
        rate_limit_per_min: int = 20,
        trade_fills_enabled: bool = True,
        transport: Callable[[str, dict], None] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._throttle = throttle_seconds
        self._rate_limit = rate_limit_per_min
        self.trade_fills_enabled = trade_fills_enabled
        self._transport = transport or _http_post
        self._clock = clock
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1000)
        self._last_sent: dict[str, float] = {}  # dedup key -> last-send monotonic ts
        self._window_start = clock()
        self._window_count = 0
        self._suppressed = 0
        self._thread = threading.Thread(target=self._run, name="telegram-alerter", daemon=True)

    def start(self) -> None:  # pragma: no cover - thread lifecycle
        self._thread.start()

    def send(self, text: str, key: str | None = None) -> None:
        """Enqueue a message. Never blocks; drops silently if the queue is full."""
        with contextlib.suppress(queue.Full):  # backpressure guard — never block the loop
            self._queue.put_nowait((text, key or text))

    def _run(self) -> None:  # pragma: no cover - exercised via _deliver in tests
        while True:
            text, key = self._queue.get()
            self._deliver(text, key)

    def _deliver(self, text: str, key: str, now: float | None = None) -> bool:
        """Throttle/rate-limit then send. Returns True if a message went out. Pure w.r.t. the
        injected clock/transport, so tests drive it directly without the thread."""
        now = self._clock() if now is None else now
        last = self._last_sent.get(key)
        if last is not None and (now - last) < self._throttle:
            self._suppressed += 1
            return False
        if now - self._window_start >= 60:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self._rate_limit:
            self._suppressed += 1
            return False
        body = text
        if self._suppressed:
            body = f"{text}\n(+{self._suppressed} more suppressed)"
            self._suppressed = 0
        try:
            self._transport(self._url, {"chat_id": self._chat_id, "text": body})
        except Exception as exc:  # noqa: BLE001 - best-effort; must not raise into the loop
            # WARNING (not ERROR) so a send failure can never re-trigger the error-catch-all alert.
            log.warning("telegram_send_failed", error=str(exc))
            return False
        self._last_sent[key] = now
        self._window_count += 1
        return True


# -- Module singleton + classification ------------------------------------------------

_alerter: TelegramAlerter | None = None


def _format(prefix: str, event: str, ed: dict[str, Any]) -> str:
    parts = [f"{k}={ed[k]}" for k in _FIELDS if ed.get(k) not in (None, "")]
    return f"{prefix} {event}" + (" — " + " ".join(parts) if parts else "")


def alert_event(event_dict: dict[str, Any]) -> None:
    """Classify a (redacted) structlog event and enqueue an alert. No-op when disabled."""
    a = _alerter
    if a is None:
        return
    event = str(event_dict.get("event", ""))
    level = str(event_dict.get("level", ""))
    if event in _CRITICAL:
        cat = "critical"
    elif event in _HEALTH:
        cat = "health"
    elif event in _TRADE:
        if not a.trade_fills_enabled:
            return
        cat = "trade"
    elif level in ("error", "critical"):
        cat = "error"
    else:
        return
    key = f"{cat}:{event}"
    if cat == "trade":  # distinct fills must not dedup into one another
        key += f":{event_dict.get('symbol')}:{event_dict.get('side')}:{event_dict.get('quantity')}:{event_dict.get('price')}"
    a.send(_format(_PREFIX[cat], event, event_dict), key=key)


def init_alerts(settings: Any) -> TelegramAlerter | None:
    """Build and start the module alerter from settings + env. No-op (returns None) unless
    ``alerts_enabled`` and both ``TELEGRAM_BOT_TOKEN`` and a chat id are present."""
    global _alerter
    import os

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    enabled = bool(getattr(settings, "alerts_enabled", False))
    if not (enabled and token and chat_id):
        _alerter = None
        log.info("alerts_disabled", enabled=enabled, has_token=bool(token), has_chat=bool(chat_id))
        return None
    # Scrub the token from all future log output (lazy import avoids an import cycle).
    from algo_trading.observability.logging import register_secret

    register_secret(token)
    a = TelegramAlerter(
        token, chat_id,
        throttle_seconds=int(getattr(settings, "alert_throttle_seconds", 300)),
        rate_limit_per_min=int(getattr(settings, "alert_rate_limit_per_min", 20)),
        trade_fills_enabled=bool(getattr(settings, "alert_trade_fills", True)),
    )
    a.start()
    _alerter = a
    log.info("alerts_enabled_ok", chat_id=chat_id)  # chat id is not a secret
    return a
