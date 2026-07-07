"""In-process publish/subscribe event bus.

Decouples producers (feeds) from consumers (candle builder, order manager, tracker). The bus
is synchronous and thread-safe: broker websockets deliver messages on their own threads, so a
lock-guarded synchronous dispatch is simpler and safer here than mixing asyncio with those
thread callbacks. Handlers are isolated — one raising does not stop the others.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from enum import Enum
from typing import Any

from algo_trading.observability.logging import get_logger

log = get_logger("core.events")


class Topic(str, Enum):
    TICK = "tick"
    CANDLE = "candle"
    ORDER_EVENT = "order_event"
    SIGNAL = "signal"
    CONTROL = "control"


Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[Topic, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: Topic, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append(handler)

    def publish(self, topic: Topic, event: Any) -> None:
        with self._lock:
            handlers = list(self._subs.get(topic, ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - never let one consumer break the bus
                log.exception("event_handler_error", topic=topic.value)
