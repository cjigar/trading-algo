"""Live feed wiring: message routing (quote vs order), subscriptions, and end-to-end flow.

Uses a fake NeoAPI so no network/SDK is required. The fake mimics the SDK contract: settable
on_message/on_error/on_close callbacks, subscribe(instrument_tokens, isIndex, isDepth), and
subscribe_to_orderfeed().
"""

from __future__ import annotations

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
