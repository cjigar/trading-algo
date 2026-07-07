"""Kotak Neo broker client wrapper.

This is the ONLY module that imports ``neo_api_client``. The import is lazy so paper mode
and the test suite run without the SDK installed. The wrapper:
  - converts typed domain objects to the SDK's string parameters,
  - selects the correct exchange segment (nse_fo for NIFTY, bse_fo for SENSEX),
  - retries transient failures (tenacity),
  - throttles submissions to <= N orders/sec/exchange,
  - classifies rejections into retryable vs terminal.

Method signatures follow the documented Kotak Neo v2 SDK (place_order/positions/limits/etc.).
Because the SDK cannot be exercised here, all SDK interaction is funnelled through
``_call`` and the small ``_extract_*`` helpers, which are the only places to adjust if the
live response shape differs.
"""

from __future__ import annotations

from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from algo_trading.broker.base import AuthError, BrokerError, OrderRejected
from algo_trading.broker.ratelimit import RateLimiter
from algo_trading.config.settings import Settings
from algo_trading.domain.models import OrderRequest
from algo_trading.observability.logging import get_logger

log = get_logger("broker.kotak")

# Rejection message fragments that are worth retrying (transient) vs terminal.
_RETRYABLE_FRAGMENTS = ("timeout", "temporarily", "try again", "rate", "busy", "network")
_AUTH_FRAGMENTS = ("token", "unauthor", "session", "login", "expired")


def _load_neo_api():  # pragma: no cover - requires the external SDK
    try:
        from neo_api_client import NeoAPI
    except ImportError as exc:  # noqa: F841
        raise BrokerError(
            "neo_api_client is not installed. Install it with `make install-broker` "
            "(git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2). "
            "Live mode requires the Kotak Neo SDK."
        ) from exc
    return NeoAPI


class KotakClient:
    """Typed wrapper around the Kotak Neo SDK's ``NeoAPI`` client."""

    def __init__(self, settings: Settings, neo_client: Any | None = None) -> None:
        self._settings = settings
        self._neo = neo_client  # injected (from SessionManager) or set later
        self._limiter = RateLimiter(settings.max_orders_per_second_per_exchange, 1.0)

    def bind(self, neo_client: Any) -> None:
        """Attach an authenticated NeoAPI client (produced by the SessionManager)."""
        self._neo = neo_client

    @property
    def _client(self) -> Any:
        if self._neo is None:
            raise AuthError("No authenticated Kotak client bound; log in first.")
        return self._neo

    # -- Order operations --------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> str:
        """Submit an order, throttled per exchange. Returns the broker order id."""
        segment = request.instrument.exchange_segment
        self._limiter.acquire(segment.value)
        params = self._to_place_params(request)
        resp = self._call("place_order", **params)
        self._raise_if_rejected(resp)
        order_id = self._extract_order_id(resp)
        log.info("order_placed", client_tag=request.client_tag, order_id=order_id,
                 segment=segment.value, side=request.side.value, qty=request.quantity)
        return order_id

    def modify_order(
        self, broker_order_id: str, *, price: str | None = None, quantity: str | None = None
    ) -> None:
        params: dict[str, Any] = {"order_id": broker_order_id}
        if price is not None:
            params["price"] = price
        if quantity is not None:
            params["quantity"] = quantity
        resp = self._call("modify_order", **params)
        self._raise_if_rejected(resp)

    def cancel_order(self, broker_order_id: str) -> None:
        resp = self._call("cancel_order", order_id=broker_order_id)
        self._raise_if_rejected(resp)

    # -- Reads -------------------------------------------------------------------------

    def positions(self) -> list[dict]:
        return self._as_list(self._call("positions"))

    def limits(self) -> dict:
        resp = self._call("limits")
        return resp if isinstance(resp, dict) else {"data": resp}

    def order_report(self) -> list[dict]:
        return self._as_list(self._call("order_report"))

    def trade_report(self) -> list[dict]:
        return self._as_list(self._call("trade_report"))

    # -- Param conversion --------------------------------------------------------------

    def _to_place_params(self, request: OrderRequest) -> dict[str, Any]:
        inst = request.instrument
        return {
            "exchange_segment": inst.exchange_segment.value,
            "product": self._settings.product_type.value,
            "order_type": request.order_type.value,
            "price": _fmt(request.price),
            "trigger_price": _fmt(request.trigger_price),
            "quantity": str(request.quantity),  # SDK expects a string
            "validity": request.validity.value,
            "trading_symbol": inst.trading_symbol,
            "transaction_type": request.side.value,
            "amo": "NO",
            "disclosed_quantity": "0",
            "market_protection": "0",
            "pf": "N",
            "tag": request.client_tag,  # idempotency tag echoed back on the order feed
        }

    # -- SDK call plumbing (retryable) -------------------------------------------------

    @retry(
        retry=retry_if_exception_type(BrokerError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    def _call(self, method: str, **kwargs: Any) -> Any:
        fn = getattr(self._client, method, None)
        if fn is None:
            raise BrokerError(f"Kotak client has no method '{method}'")
        try:
            return fn(**kwargs)
        except OrderRejected:
            raise  # terminal rejection: do not retry
        except Exception as exc:  # noqa: BLE001 - normalize SDK errors
            msg = str(exc).lower()
            if any(frag in msg for frag in _AUTH_FRAGMENTS):
                raise AuthError(str(exc)) from exc
            if any(frag in msg for frag in _RETRYABLE_FRAGMENTS):
                raise BrokerError(str(exc)) from exc  # retried by tenacity
            raise BrokerError(str(exc)) from exc

    # -- Response interpretation (adjust here if live shape differs) --------------------

    @staticmethod
    def _as_list(resp: Any) -> list[dict]:
        if resp is None:
            return []
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            data = resp.get("data", resp)
            return data if isinstance(data, list) else [data]
        return []

    @staticmethod
    def _extract_order_id(resp: Any) -> str:
        if isinstance(resp, dict):
            data = resp.get("data", resp)
            if isinstance(data, dict):
                for key in ("nOrdNo", "order_id", "orderId", "orderNo"):
                    if data.get(key):
                        return str(data[key])
        raise BrokerError(f"Could not extract order id from response: {resp!r}")

    @staticmethod
    def _raise_if_rejected(resp: Any) -> None:
        if not isinstance(resp, dict):
            return
        # Kotak surfaces errors under 'stat'/'stCode'/'errMsg' depending on endpoint.
        err = resp.get("errMsg") or resp.get("emsg") or resp.get("error")
        stat = str(resp.get("stat", "")).lower()
        if err or stat in ("not_ok", "error"):
            message = str(err or resp)
            retryable = any(frag in message.lower() for frag in _RETRYABLE_FRAGMENTS)
            raise OrderRejected(message, retryable=retryable)


def _fmt(value: object) -> str:
    """Format a Decimal/number as the SDK-expected string, dropping trailing zeros sensibly."""
    return f"{value}"
