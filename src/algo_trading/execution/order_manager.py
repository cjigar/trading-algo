"""Idempotent order lifecycle management.

Responsibilities:
  - Persist an order's client tag BEFORE submitting it (idempotency), so a crash/retry can be
    reconciled rather than resubmitted.
  - Split orders exceeding the exchange freeze quantity into multiple lot-aligned legs.
  - Submit via the broker (which throttles per exchange), classifying rejections.
  - Drive the order state machine from order-feed events, recording trades on fills.
  - Reconcile local state against the broker's order/position reports on startup.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC

from algo_trading.broker.base import BrokerClient, OrderRejected
from algo_trading.broker.order_feed import normalize_order_event
from algo_trading.config.settings import Settings
from algo_trading.domain.enums import OrderState
from algo_trading.domain.models import OrderEvent, OrderRequest, Trade
from algo_trading.observability.logging import get_logger
from algo_trading.persistence.repositories import Repository

log = get_logger("execution.orders")

FillCallback = Callable[[Trade], None]


def split_for_freeze(quantity: int, lot_size: int, freeze_quantity: int) -> list[int]:
    """Split ``quantity`` into legs each <= freeze qty and a multiple of the lot size."""
    if lot_size <= 0:
        return [quantity]
    max_leg = max((freeze_quantity // lot_size) * lot_size, lot_size)
    legs: list[int] = []
    remaining = quantity
    while remaining > 0:
        leg = min(remaining, max_leg)
        legs.append(leg)
        remaining -= leg
    return legs


class OrderManager:
    def __init__(
        self,
        broker: BrokerClient,
        repo: Repository,
        settings: Settings,
        on_fill: FillCallback | None = None,
    ) -> None:
        self._broker = broker
        self._repo = repo
        self._settings = settings
        self._on_fill = on_fill
        self._pending: dict[str, OrderRequest] = {}  # client_tag -> request

    # -- Submission --------------------------------------------------------------------

    def submit(self, request: OrderRequest) -> list[str]:
        """Submit an order, splitting for freeze qty. Returns placed broker order ids."""
        legs = split_for_freeze(
            request.quantity, request.instrument.lot_size, self._settings.freeze_quantity
        )
        order_ids: list[str] = []
        for i, leg_qty in enumerate(legs):
            leg = request if len(legs) == 1 else request.model_copy(
                update={"client_tag": f"{request.client_tag[:16]}{i}", "quantity": leg_qty}
            )
            order_id = self._submit_leg(leg)
            if order_id is not None:
                order_ids.append(order_id)
        self._drain_paper_fills()
        return order_ids

    def _submit_leg(self, leg: OrderRequest) -> str | None:
        # persist PENDING before the broker call -> idempotency anchor
        self._repo.record_new_order(leg)
        self._pending[leg.client_tag] = leg
        try:
            order_id = self._broker.place_order(leg)
        except OrderRejected as exc:
            self._repo.apply_order_event(
                OrderEvent(
                    client_tag=leg.client_tag,
                    broker_order_id=None,
                    state=OrderState.REJECTED,
                    timestamp=_utcnow(),
                    message=str(exc),
                )
            )
            self._repo.record_audit(
                "order_rejected",
                str(exc),
                {"client_tag": leg.client_tag, "retryable": exc.retryable},
            )
            log.warning("order_rejected", client_tag=leg.client_tag, retryable=exc.retryable)
            return None
        # acknowledged; further transitions arrive via the order feed / paper fills
        self.handle_event(
            OrderEvent(
                client_tag=leg.client_tag,
                broker_order_id=order_id,
                state=OrderState.ACKNOWLEDGED,
                timestamp=_utcnow(),
                message="submitted",
            )
        )
        return order_id

    def _drain_paper_fills(self) -> None:
        poll = getattr(self._broker, "poll_events", None)
        if callable(poll):
            for event in poll():
                self.handle_event(event)

    # -- Event handling ----------------------------------------------------------------

    def handle_event(self, event: OrderEvent) -> None:
        self._repo.apply_order_event(event)
        if event.state is OrderState.FILLED and event.filled_quantity > 0:
            self._record_fill(event)
        elif event.state is OrderState.REJECTED:
            self._repo.record_audit("order_rejected", event.message, {"client_tag": event.client_tag})

    def _record_fill(self, event: OrderEvent) -> None:
        request = self._pending.get(event.client_tag)
        if request is None:
            return  # fill for an order we don't track (e.g. reconciled externally)
        trade = Trade(
            client_tag=event.client_tag,
            broker_order_id=event.broker_order_id,
            instrument=request.instrument,
            side=request.side,
            quantity=event.filled_quantity,
            price=event.average_price if event.average_price > 0 else request.price,
            timestamp=event.timestamp,
        )
        self._repo.record_trade(trade)
        if self._on_fill is not None:
            self._on_fill(trade)
        self._pending.pop(event.client_tag, None)
        log.info("fill_recorded", client_tag=event.client_tag, qty=trade.quantity,
                 price=str(trade.price), side=trade.side.value)

    # -- Reconciliation ----------------------------------------------------------------

    def reconcile(self) -> dict:
        """Reconcile local open orders against the broker's reports before trading. Fail-safe."""
        try:
            report = self._broker.order_report()
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile_order_report_failed", error=str(exc))
            report = []
        try:
            positions = self._broker.positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile_positions_failed", error=str(exc))
            positions = []

        # Persist the live broker positions so the dashboard can surface existing exposure,
        # and log each one for immediate visibility on startup.
        try:
            self._repo.replace_broker_positions(positions)
            for p in positions:
                log.info("broker_position", **{str(k): v for k, v in p.items()})
        except Exception as exc:  # noqa: BLE001 - persistence/logging must not block reconcile
            log.warning("broker_positions_persist_failed", error=str(exc))

        # Apply any terminal states the broker reports for orders we still consider open.
        applied = 0
        by_tag = {}
        for raw in report:
            ev = normalize_order_event(raw)
            if ev is not None and ev.client_tag:
                by_tag[ev.client_tag] = ev
        for row in self._repo.open_orders():
            ev = by_tag.get(row.client_tag)
            if ev is not None and ev.state.is_terminal:
                self.handle_event(ev)
                applied += 1

        summary = {
            "broker_orders": len(report),
            "broker_positions": len(positions),
            "reconciled_terminal": applied,
        }
        self._repo.record_audit("startup_reconciliation", "reconciled on startup", summary)
        log.info("reconciled", **summary)
        return summary


def _utcnow():
    from datetime import datetime

    return datetime.now(UTC)
