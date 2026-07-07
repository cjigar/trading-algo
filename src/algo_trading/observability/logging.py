"""Structured logging with secret redaction and per-day log files.

Uses structlog to emit JSON logs. A redaction processor scrubs any value that looks
like a known secret so credentials cannot leak into logs even if passed accidentally.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

# Keys whose values must always be redacted regardless of content.
_SENSITIVE_KEY_RE = re.compile(
    r"(consumer_key|consumer_secret|mpin|totp|password|access_token|trade_token|"
    r"view_token|session|secret|mobile|ucc|auth)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"

# Extra literal secret values registered at runtime (e.g. actual token strings) to scrub
# even when they appear embedded in a free-form message.
_registered_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Register a literal secret value to be scrubbed from all future log output."""
    value = (value or "").strip()
    if len(value) >= 4:  # avoid scrubbing trivially short strings
        _registered_secrets.add(value)


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = _REDACTED
    if _registered_secrets:
        for key, val in event_dict.items():
            if isinstance(val, str):
                for secret in _registered_secrets:
                    if secret in val:
                        val = val.replace(secret, _REDACTED)
                event_dict[key] = val
    return event_dict


def _utc_timestamper(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict["ts"] = datetime.now(UTC).isoformat()
    return event_dict


def configure_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
    """Configure structlog + stdlib logging with a per-day rotating JSON file and console output."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        log_path / "algo.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    console = logging.StreamHandler()

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(console)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _utc_timestamper,
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
