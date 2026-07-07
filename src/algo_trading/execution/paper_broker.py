"""Paper-mode broker: a simulated fill engine implementing the BrokerClient surface.

Fills orders immediately at the order's limit price (or a provided LTP for market orders) and
emits a FILLED :class:`OrderEvent` through the same event path the live order feed uses, so the
downstream tracking code is identical in paper and live modes. Also maintains a simple internal
positions book so ``positions()``/``reconcile`` behave.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from algo_trading.domain.enums import OrderState, Side
from algo_trading.domain.models import OrderEvent, OrderRequest
from algo_trading.observability.logging import get_logger

log = get_logger("execution.paper")

LtpProvider = Callable[[str], Decimal | None]  # instrument_token -> ltp


class PaperBroker:
    def __init__(self, ltp_provider: LtpProvider | None = None) -> None:
        self._ltp_provider = ltp_provider or (lambda _t: None)
        self._events: deque[OrderEvent] = deque()
        self._book: dict[str, dict] = {}  # trading_symbol -> position dict
        self._counter = 0

    # -- BrokerClient surface ----------------------------------------------------------

    def place_order(self, request: OrderRequest) -> str:
        self._counter += 1
        order_id = f"PAPER{self._counter:06d}"
        fill_price = self._fill_price(request)
        # emit an immediate full fill on the same event path as the live order feed
        self._events.append(
            OrderEvent(
                client_tag=request.client_tag,
                broker_order_id=order_id,
                state=OrderState.FILLED,
                filled_quantity=request.quantity,
                average_price=fill_price,
                timestamp=datetime.now(UTC),
                message="paper fill",
            )
        )
        self._update_book(request, fill_price)
        log.info("paper_order_filled", client_tag=request.client_tag, order_id=order_id,
                 price=str(fill_price), qty=request.quantity, side=request.side.value)
        return order_id

    def modify_order(self, broker_order_id: str, *, price=None, quantity=None) -> None:
        return None

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def positions(self) -> list[dict]:
        return [p for p in self._book.values() if p["netQty"] != 0]

    def limits(self) -> dict:
        return {"mode": "paper"}

    def order_report(self) -> list[dict]:
        return []

    def trade_report(self) -> list[dict]:
        return []

    # -- Paper-specific ----------------------------------------------------------------

    def poll_events(self) -> list[OrderEvent]:
        """Drain and return pending simulated fill events."""
        events = list(self._events)
        self._events.clear()
        return events

    def _fill_price(self, request: OrderRequest) -> Decimal:
        if request.price and request.price > 0:
            return request.price
        ltp = self._ltp_provider(request.instrument.instrument_token)
        return ltp if ltp and ltp > 0 else Decimal("1")  # fallback nominal price

    def _update_book(self, request: OrderRequest, price: Decimal) -> None:
        sym = request.instrument.trading_symbol
        pos = self._book.setdefault(
            sym, {"trading_symbol": sym, "netQty": 0, "avg": Decimal(0)}
        )
        signed = request.quantity if request.side is Side.BUY else -request.quantity
        pos["netQty"] += signed
