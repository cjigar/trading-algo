"""Two-way Telegram control — receive operator commands and enqueue the matching control action.

A background daemon thread in the *algo* process long-polls the Telegram ``getUpdates`` API and, for
messages from the authorised chat only, enqueues the existing dashboard control commands:

    /clear -> "flatten"  (square off all open positions; algo keeps running)
    /stop  -> "stop"     (manual halt for the day + square off all)

Security posture (this is a live-trading control channel):
- **Authorisation:** only messages whose ``chat.id`` equals the configured operator chat are honored.
- **Defensive-only:** the sole commands reduce exposure — none can open a position, place an order,
  or move funds. That is the safety ceiling even if the bot token leaks.
- **Backlog-skip:** on start it primes the offset past any pending updates, so a stale ``/stop``
  cannot re-fire after a restart or re-exec.
- **Fail-safe:** poll/HTTP errors are logged (WARNING) and retried; they never crash the loop.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import structlog

from algo_trading.observability.alerts import _http_post

log = structlog.get_logger("observability.telegram_commands")

# first token of the message -> control command enqueued for the orchestrator
_COMMANDS = {"/clear": "flatten", "/stop": "stop"}
_HELP_CMDS = {"/help", "/start"}

_ACK = {
    "flatten": "🧹 Flattening — squaring off all open positions (algo keeps running).",
    "stop": "🛑 Stopping — halting new entries for the day and squaring off all positions.",
}
_HELP = (
    "Commands:\n"
    "/clear — exit all open positions (algo keeps running)\n"
    "/stop — halt trading for the day & square off all"
)


def parse_command(text: str) -> str | None:
    """Map a message to a control command / 'help' / None. Case-insensitive; strips a trailing
    ``@botname`` and any arguments (only the first token matters)."""
    if not text or not text.strip():
        return None
    first = text.strip().split()[0].lower().split("@")[0]
    if first in _HELP_CMDS:
        return "help"
    return _COMMANDS.get(first)


class _TelegramHTTP:
    def __init__(self, token: str, chat_id: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{self._base}/getUpdates?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:  # noqa: S310 - fixed host
            data = json.load(resp)
        return data.get("result", []) if data.get("ok") else []

    def send(self, text: str) -> None:
        _http_post(f"{self._base}/sendMessage", {"chat_id": self._chat_id, "text": text})


class TelegramCommandListener:
    """Long-polls Telegram and turns authorised commands into ``on_command(control_command)`` calls.
    ``http`` is injectable for tests."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        on_command: Callable[[str], None],
        *,
        http: Any = None,
        poll_timeout: int = 30,
    ) -> None:
        self._chat_id = str(chat_id)
        self._on_command = on_command
        self._http = http or _TelegramHTTP(token, str(chat_id))
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)

    def start(self) -> None:  # pragma: no cover - thread lifecycle
        self._prime_offset()
        self._thread.start()

    def _prime_offset(self) -> None:
        """Skip any backlog so an old command can't fire after a (re)start. ``offset=-1`` returns at
        most the latest pending update; we set the next offset just past it."""
        try:
            updates = self._http.get_updates(offset=-1, timeout=0)
            if updates:
                self._offset = int(updates[-1]["update_id"]) + 1
        except Exception as exc:  # noqa: BLE001 - best-effort; never block startup
            log.warning("telegram_commands_prime_failed", error=str(exc))

    def _run(self) -> None:  # pragma: no cover - exercised via _handle in tests
        while True:
            try:
                updates = self._http.get_updates(offset=self._offset, timeout=self._poll_timeout)
                for u in updates:
                    self._offset = int(u["update_id"]) + 1
                    self._handle(u)
            except Exception as exc:  # noqa: BLE001 - a poll error must never kill the loop
                log.warning("telegram_commands_poll_failed", error=str(exc))
                time.sleep(3)  # backoff so a persistent error can't hot-loop

    def _handle(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text") or "")
        if chat_id != self._chat_id:
            # Never act for anyone but the operator; surface the attempt.
            log.warning("telegram_command_unauthorized", chat_id=chat_id, text=text)
            return
        cmd = parse_command(text)
        if cmd is None:
            return
        if cmd == "help":
            self._safe_send(_HELP)
            return
        log.info("telegram_command", command=cmd)
        try:
            self._on_command(cmd)
        except Exception as exc:  # noqa: BLE001 - enqueue failure must not kill the poller
            log.warning("telegram_command_enqueue_failed", command=cmd, error=str(exc))
            return
        self._safe_send(_ACK[cmd])

    def _safe_send(self, text: str) -> None:
        try:
            self._http.send(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_command_reply_failed", error=str(exc))


def start_listener(settings: Any, on_command: Callable[[str], None]) -> TelegramCommandListener | None:
    """Build + start the listener when ``telegram_commands_enabled`` and a token + chat id are set.
    A separate opt-in from alerting, so enabling alerts never enables remote control."""
    import os

    if not bool(getattr(settings, "telegram_commands_enabled", False)):
        log.info("telegram_commands_disabled", reason="not enabled")
        return None
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if not (token and chat_id):
        log.info("telegram_commands_disabled", reason="no token/chat id")
        return None
    from algo_trading.observability.logging import register_secret

    register_secret(token)
    listener = TelegramCommandListener(token, chat_id, on_command)
    listener.start()
    log.info("telegram_commands_enabled", chat_id=chat_id)
    return listener
