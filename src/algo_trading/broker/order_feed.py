"""Order/trade websocket feed handler.

Subscribes to the Kotak Neo order feed and normalizes raw order/trade updates into
:class:`OrderEvent` objects, mapping the broker's status strings onto our order state
machine. The mapping and normalization are pure and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from algo_trading.domain.enums import OrderState
from algo_trading.domain.models import OrderEvent
from algo_trading.observability.logging import get_logger

log = get_logger("broker.order_feed")

OrderEventCallback = Callable[[OrderEvent], None]

# Kotak order-status strings -> our state machine.
_STATUS_MAP = {
    "open": OrderState.ACKNOWLEDGED,
    "open pending": OrderState.PENDING,
    "trigger pending": OrderState.ACKNOWLEDGED,
    "put order req received": OrderState.PENDING,
    "validation pending": OrderState.PENDING,
    "modify pending": OrderState.ACKNOWLEDGED,
    "not modified": OrderState.ACKNOWLEDGED,
    "complete": OrderState.FILLED,
    "traded": OrderState.FILLED,
    "rejected": OrderState.REJECTED,
    "cancelled": OrderState.CANCELLED,
    "canceled": OrderState.CANCELLED,
}


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _to_int(value: Any) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (ValueError, TypeError):
        return 0


def normalize_order_event(raw: dict, *, now: datetime | None = None) -> OrderEvent | None:
    """Convert a raw order-feed message into an :class:`OrderEvent`, or None if unrecognized."""
    tag = raw.get("tag") or raw.get("usrOrdId") or raw.get("client_tag")
    status = str(raw.get("ordSt") or raw.get("status") or raw.get("orderStatus") or "").lower()
    if not tag and not status:
        return None
    filled = _to_int(raw.get("fldQty") or raw.get("filled_quantity") or raw.get("fillQty"))
    total = _to_int(raw.get("qty") or raw.get("quantity"))
    state = _STATUS_MAP.get(status)
    if state is None:
        # infer partial fills when status is unmapped but quantities indicate progress
        if filled and total and filled < total:
            state = OrderState.PARTIALLY_FILLED
        else:
            state = OrderState.ACKNOWLEDGED
    elif state is OrderState.FILLED and filled and total and filled < total:
        state = OrderState.PARTIALLY_FILLED
    return OrderEvent(
        client_tag=str(tag) if tag else "",
        broker_order_id=str(raw.get("nOrdNo") or raw.get("order_id") or raw.get("orderId") or "")
        or None,
        state=state,
        filled_quantity=filled,
        average_price=_to_decimal(raw.get("avgPrc") or raw.get("average_price") or raw.get("avgPr")),
        timestamp=now or datetime.now(UTC),
        message=str(raw.get("rejRsn") or raw.get("rejReason") or raw.get("message") or ""),
    )


class OrderFeedHandler:
    def __init__(self, on_event: OrderEventCallback) -> None:
        self._on_event = on_event
        self._neo: Any | None = None

    def bind(self, neo_client: Any) -> None:
        self._neo = neo_client

    def subscribe(self) -> None:
        if self._neo is None:
            return
        # Order/trade updates are delivered through the same on_message callback; the SDK
        # multiplexes them, so we install our handler and start the order feed.
        self._neo.subscribe_to_orderfeed()
        log.info("order_ws_subscribe")

    def handle_message(self, message: Any) -> None:
        records = message if isinstance(message, list) else [message]
        for raw in records:
            if not isinstance(raw, dict):
                continue
            event = normalize_order_event(raw)
            if event is not None and event.client_tag:
                try:
                    self._on_event(event)
                except Exception:  # noqa: BLE001
                    log.exception("order_event_consumer_error")

    def resubscribe(self) -> None:
        self.subscribe()
