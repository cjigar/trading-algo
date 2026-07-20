"""Live feed wiring: message routing (quote vs order), subscriptions, and end-to-end flow.

Uses a fake NeoAPI so no network/SDK is required. The fake mimics the SDK contract: settable
on_message/on_error/on_close callbacks, subscribe(instrument_tokens, isIndex, isDepth), and
subscribe_to_orderfeed().
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from algo_trading.broker.live_feed import LiveFeedCoordinator
from algo_trading.broker.order_feed import is_order_message
from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import (
    ExchangeSegment,
    OptionType,
    OrderState,
    TradingMode,
    Underlying,
)
from algo_trading.domain.models import Instrument, Tick
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository
from algo_trading.strategy.vwap_breakout import VwapBreakoutStrategy

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


class FakeNeo:
    def __init__(self):
        self.on_message = None
        self.on_error = None
        self.on_close = None
        self.on_open = None
        self.subscriptions = []  # list of (tokens tuple, is_index)
        self.orderfeed = False

    def subscribe(self, instrument_tokens, isIndex=False, isDepth=False):
        self.subscriptions.append(
            (tuple(t["instrument_token"] for t in instrument_tokens), isIndex)
        )

    def subscribe_to_orderfeed(self):
        self.orderfeed = True


def _settings(**overrides):
    s = get_settings(reload=True)
    object.__setattr__(s, "strategy", "vwap_breakout")  # this test drives the candle pipeline
    object.__setattr__(s, "underlyings", [Underlying.NIFTY])
    object.__setattr__(s, "nifty_index_token", "256265")
    object.__setattr__(s, "sensex_index_token", "")
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


# -- is_order_message ------------------------------------------------------------------


def test_is_order_message_detects_order_vs_quote():
    assert is_order_message({"ordSt": "complete", "nOrdNo": "B1"}) is True
    assert is_order_message({"type": "order"}) is True
    assert is_order_message({"instrument_token": "256265", "ltp": "23050"}) is False


# -- Coordinator subscriptions & routing ----------------------------------------------


def test_coordinator_subscribes_index_and_orderfeed():
    neo = FakeNeo()
    coord = LiveFeedCoordinator(_settings(), neo, on_tick=lambda t: None, on_order_event=lambda e: None)
    coord.start()
    assert neo.orderfeed is True
    # index subscription present with isIndex=True and the configured token
    assert any(isidx and "256265" in toks for toks, isidx in neo.subscriptions)


def test_coordinator_skips_missing_index_token():
    neo = FakeNeo()
    coord = LiveFeedCoordinator(
        _settings(nifty_index_token=""), neo, on_tick=lambda t: None, on_order_event=lambda e: None
    )
    coord.start()
    assert neo.orderfeed is True  # order feed still subscribed
    assert all(not isidx for _toks, isidx in neo.subscriptions)  # no index sub


def test_coordinator_routes_quote_and_order_messages():
    neo = FakeNeo()
    ticks, events = [], []
    coord = LiveFeedCoordinator(_settings(), neo, on_tick=ticks.append, on_order_event=events.append)
    coord.start()

    neo.on_message({"instrument_token": "256265", "ltp": "23050.5", "exchange_segment": "nse_cm"})
    neo.on_message(
        {"tag": "alg1", "ordSt": "complete", "fldQty": "75", "qty": "75", "avgPrc": "100", "nOrdNo": "B1"}
    )

    assert len(ticks) == 1 and ticks[0].ltp == Decimal("23050.5")
    assert len(events) == 1 and events[0].state is OrderState.FILLED
    assert events[0].client_tag == "alg1"


def test_coordinator_subscribe_option():
    neo = FakeNeo()
    coord = LiveFeedCoordinator(_settings(), neo, on_tick=lambda t: None, on_order_event=lambda e: None)
    coord.start()
    coord.subscribe_option("11536", ExchangeSegment.NSE_FO)
    assert any("11536" in toks and not isidx for toks, isidx in neo.subscriptions)


# -- End-to-end: live ticks drive the orchestrator pipeline ----------------------------


def _nifty_chain(expiry, strikes):
    inst = []
    for k in strikes:
        for ot in (OptionType.CE, OptionType.PE):
            inst.append(
                Instrument(
                    underlying=Underlying.NIFTY, exchange_segment=ExchangeSegment.NSE_FO,
                    trading_symbol=f"NIFTY{k}{ot.value}", instrument_token=f"{k}-{ot.value}",
                    expiry=expiry, strike=Decimal(k), option_type=ot, lot_size=75,
                )
            )
    return ScripMaster(inst)


@freeze_time("2025-01-27")
def test_live_feed_drives_orchestrator_pipeline(engine):
    from datetime import date

    sm = _nifty_chain(date(2025, 1, 30), [str(s) for s in range(22800, 23400, 50)])
    settings = _settings()
    object.__setattr__(settings, "mode", TradingMode.PAPER)
    neo = FakeNeo()
    # paper broker + a fake authenticated client so attach_live_feeds wires the coordinator
    orch = Orchestrator(
        settings, scrip_master=sm, broker=PaperBroker(), neo_client=neo,
        strategy=VwapBreakoutStrategy(settings, breakout_window=2), repo=Repository(engine),
    )
    orch.start_session()
    assert orch.attach_live_feeds() is True

    # 1) A quote pushed through the SDK's on_message reaches the orchestrator (coordinator path).
    neo.on_message({"instrument_token": "256265", "ltp": "23000", "exchange_segment": "nse_cm"})
    assert orch._ltp.get("256265") == Decimal("23000")

    # 2) Drive a bullish breakout with timestamped index ticks (deterministic candle boundaries);
    #    the SDK stamps arrival time in reality, but the pipeline logic is identical. Use the
    #    frozen date so these ticks sort AFTER the arrival-stamped quote from step 1.
    base = datetime(2025, 1, 27, 9, 30, tzinfo=IST).astimezone(UTC)
    for i, price in enumerate(["23000", "23000", "23000", "23200", "23200"]):
        orch.publish_tick(
            Tick(instrument_token="256265", exchange_segment=ExchangeSegment.NSE_CM,
                 ltp=Decimal(price), timestamp=base + timedelta(minutes=5 * i), is_index=True)
        )

    # Entry placed & filled via the paper broker...
    assert orch.positions.open_position_count() == 1
    # ...and the coordinator subscribed the option's quotes for exit tracking.
    assert any(not isidx and any(t.endswith("CE") for t in toks)
               for toks, isidx in neo.subscriptions)


# -- Real Kotak socket envelope (from live probe) -------------------------------------


def test_unwrap_nested_stock_feed():
    from algo_trading.broker.live_feed import _unwrap
    msg = {"type": "stock_feed", "data": [{"tk": "44612", "ltp": "597.30", "oi": "467870"}]}
    mtype, recs = _unwrap(msg)
    assert mtype == "stock_feed" and len(recs) == 1 and recs[0]["oi"] == "467870"


def test_dispatch_captures_oi_from_socket_envelope():
    neo = FakeNeo()
    ticks: list = []
    coord = LiveFeedCoordinator(_settings(), neo, on_tick=ticks.append, on_order_event=lambda e: None)
    coord.start()
    # exact shape seen from the live feed (nested data array; token=tk, volume=v, oi=oi)
    neo.on_message({"type": "stock_feed", "data": [
        {"tk": "44612", "ltp": "597.30", "oi": "467870", "v": "298740", "e": "nse_fo"},
        {"tk": "44613", "name": "ack"},  # subscription-ack record -> ignored
    ]})
    assert len(ticks) == 1
    assert ticks[0].instrument_token == "44612"
    assert ticks[0].oi == 467870
    assert ticks[0].volume == 298740


def test_dispatch_order_envelope_routes_to_order_feed():
    neo = FakeNeo()
    events: list = []
    coord = LiveFeedCoordinator(_settings(), neo, on_tick=lambda t: None, on_order_event=events.append)
    coord.start()
    neo.on_message({"type": "order_feed", "data": [
        {"tag": "alg1", "ordSt": "complete", "fldQty": "75", "qty": "75", "nOrdNo": "B1"}]})
    assert len(events) == 1 and events[0].state is OrderState.FILLED


# -- Reconnect storm (regression) -------------------------------------------------------


class DeadSocketNeo:
    """Mimics the SDK after an *abrupt* connection loss.

    On a dead socket the SDK's ``subscribe()`` tries to stand up a fresh websocket. That
    attempt fails, fires ``on_error`` (re-entering our handler), then raises. This is the
    shape that produced the 14k-thread / FD-exhaustion storm on the live server.
    """

    def __init__(self, cap: int = 500):
        self.on_message = self.on_error = self.on_close = self.on_open = None
        self.subscribe_calls = 0
        self.max_nesting = 0
        self._depth = 0
        self._cap = cap

    def subscribe(self, instrument_tokens, isIndex=False, isDepth=False):
        self.subscribe_calls += 1
        if self.subscribe_calls > self._cap:
            raise RuntimeError("runaway guard tripped")  # keeps a buggy impl from hanging
        self._depth += 1
        self.max_nesting = max(self.max_nesting, self._depth)
        try:
            if self.on_error is not None:
                self.on_error("Connection to remote host was lost.")
        finally:
            self._depth -= 1
        raise RuntimeError("can't start new thread")

    def subscribe_to_orderfeed(self):
        pass


def _wire_dead_socket():
    neo = DeadSocketNeo()
    coord = LiveFeedCoordinator(
        _settings(), neo, on_tick=lambda t: None, on_order_event=lambda e: None
    )
    coord._feed._sleep = lambda *_: None  # no real backoff sleeps in tests
    coord._feed._subscriptions = {
        "256265": {"instrument_token": "256265", "exchange_segment": "nse_cm"}
    }
    neo.on_error = coord._on_error  # what start() installs
    return neo, coord


def test_abrupt_ws_loss_does_not_reenter_reconnect():
    """A reconnect triggered from on_error must not recurse when the retry itself errors."""
    neo, coord = _wire_dead_socket()

    with contextlib.suppress(RuntimeError):  # runaway guard; a correct impl never trips it
        coord._on_error("Connection to remote host was lost.")

    assert neo.max_nesting == 1, f"reconnect re-entered itself (depth {neo.max_nesting})"


def test_abrupt_ws_loss_bounds_total_subscribe_attempts():
    """One connection loss must cost a bounded number of connection attempts, not 6**depth."""
    neo, coord = _wire_dead_socket()

    with contextlib.suppress(RuntimeError):
        coord._on_error("Connection to remote host was lost.")

    assert neo.subscribe_calls <= 6, (
        f"one drop caused {neo.subscribe_calls} connection attempts; "
        "each leaks a socket + thread on the real SDK"
    )
