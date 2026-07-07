"""Broker client interface.

Both the live Kotak wrapper and the paper-mode engine implement this protocol, so the
execution layer is broker-agnostic. Methods return normalized domain objects / plain dicts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from algo_trading.domain.models import OrderRequest


class BrokerError(Exception):
    """Base error raised by broker implementations."""


class AuthError(BrokerError):
    """Raised when the session is missing/expired and re-authentication is required."""


class OrderRejected(BrokerError):
    """Raised when the broker rejects an order. ``retryable`` classifies retry-vs-abort."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class BrokerClient(Protocol):
    """Minimal surface the execution layer depends on."""

    def place_order(self, request: OrderRequest) -> str:
        """Submit an order. Returns the broker order id. Raises :class:`OrderRejected`."""
        ...

    def modify_order(
        self, broker_order_id: str, *, price: str | None = None, quantity: str | None = None
    ) -> None: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def positions(self) -> list[dict]: ...

    def limits(self) -> dict: ...

    def order_report(self) -> list[dict]: ...

    def trade_report(self) -> list[dict]: ...
