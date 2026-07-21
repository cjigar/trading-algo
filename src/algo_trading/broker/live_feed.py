"""Live feed coordinator: connects the Kotak websockets to the orchestrator.

The Kotak SDK multiplexes quote and order/trade updates through a single ``on_message``
callback. This coordinator installs one dispatcher that routes each message to either the
quote :class:`FeedHandler` (ticks -> candles/exits) or the :class:`OrderFeedHandler`
(order/trade updates -> order state machine), wires error/close to the quote feed's reconnect,
subscribes the underlying index feeds and the order feed, and lets the orchestrator subscribe
option tokens dynamically as positions open.

All SDK interaction is confined here and in the two handlers, so a change in the SDK's message
shape or callback wiring only touches this file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from algo_trading.broker.market_data import FeedHandler
from algo_trading.broker.order_feed import OrderFeedHandler, is_order_message
from algo_trading.config.settings import Settings
from algo_trading.domain.enums import ExchangeSegment, Underlying
from algo_trading.domain.models import OrderEvent, Tick
from algo_trading.observability.logging import get_logger

log = get_logger("broker.live_feed")

TickCallback = Callable[[Tick], None]
OrderEventCallback = Callable[[OrderEvent], None]


def _unwrap(message: Any) -> tuple[str, list]:
    """Return (message_type, records) from a Kotak socket message. Handles the nested
    ``{"type": ..., "data": [ ... ]}`` envelope as well as bare dicts/lists."""
    if isinstance(message, dict):
        msg_type = str(message.get("type", "")).lower()
        payload = message.get("data")
        if isinstance(payload, list):
            return msg_type, payload
        if isinstance(payload, dict):
            return msg_type, [payload]
        return msg_type, [message]
    if isinstance(message, list):
        return "", message
    return "", []


class LiveFeedCoordinator:
    def __init__(
        self,
        settings: Settings,
        neo_client: Any,
        on_tick: TickCallback,
        on_order_event: OrderEventCallback,
    ) -> None:
        self._settings = settings
        self._neo = neo_client
        self._feed = FeedHandler(settings, on_tick=on_tick)
        self._feed._neo = neo_client  # bind data without letting FeedHandler own on_message
        self._order_feed = OrderFeedHandler(on_event=on_order_event)
        self._order_feed.bind(neo_client)

    # -- Lifecycle ---------------------------------------------------------------------

    def start(self) -> list[Underlying]:
        """Install the message dispatcher and subscribe index + order feeds.

        Returns the underlyings whose index tokens were found (and therefore subscribed).
        Underlyings without a configured index token are skipped with a warning.
        """
        self._neo.on_message = self._dispatch
        self._neo.on_error = self._on_error
        self._neo.on_close = self._on_close
        self._neo.on_open = lambda *_a: log.info("ws_open")

        index_subs: list[dict] = []
        subscribed: list[Underlying] = []
        for u in self._settings.underlyings:
            token = self._settings.index_token_for(u)
            if not token:
                log.warning("index_token_missing", underlying=u.value)
                continue
            index_subs.append(
                {
                    "instrument_token": token,
                    "exchange_segment": ExchangeSegment.index_for_underlying(u).value,
                }
            )
            subscribed.append(u)

        if index_subs:
            self._feed.subscribe(index_subs, is_index=True)
        self._order_feed.subscribe()
        self._feed.mark_started()
        log.info("live_feed_started", index_subscriptions=len(index_subs))
        return subscribed

    def subscribe_option(self, instrument_token: str, exchange_segment: ExchangeSegment) -> None:
        """Subscribe to an option contract's quotes (called when a position opens)."""
        self._feed.subscribe(
            [{"instrument_token": instrument_token, "exchange_segment": exchange_segment.value}],
            is_index=False,
        )

    def is_stale(self) -> bool:
        return self._feed.is_stale()

    def reconnect(self) -> bool:
        """Re-establish the quote subscription (and the order feed with it). Returns True on
        success. Shares the reconnect path used by the socket's error/close handlers, including
        its re-entry guard."""
        ok = self._feed.reconnect()
        if ok:
            self._order_feed.resubscribe()
        return ok

    # -- Dispatch ----------------------------------------------------------------------

    def _dispatch(self, message: Any) -> None:
        # Kotak wraps updates as {"type": "stock_feed"|"order_feed"|..., "data": [ {..}, .. ]}.
        msg_type, records = _unwrap(message)
        is_order = "order" in msg_type or "trade" in msg_type
        for raw in records:
            if not isinstance(raw, dict):
                continue
            if is_order or is_order_message(raw):
                self._order_feed.handle_message(raw)
            else:
                self._feed.handle_raw(raw)

    def _on_error(self, error: Any) -> None:
        log.warning("ws_error", error=str(error))
        if self._feed.reconnect():
            self._order_feed.resubscribe()

    def _on_close(self, *args: Any) -> None:
        log.warning("ws_closed")
        if self._feed.reconnect():
            self._order_feed.resubscribe()
