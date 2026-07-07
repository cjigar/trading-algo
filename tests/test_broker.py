"""Broker-layer unit tests that run without the Kotak SDK (fake NeoAPI + pure helpers)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algo_trading.broker.base import AuthError, OrderRejected
from algo_trading.broker.kotak_client import KotakClient
from algo_trading.broker.market_data import FeedHandler, normalize_tick
from algo_trading.broker.order_feed import normalize_order_event
from algo_trading.broker.ratelimit import RateLimiter
from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment, OrderState
from tests.conftest import make_order_request

# -- Rate limiter (deterministic clock) -----------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.slept += secs
        self.t += secs


def test_rate_limiter_throttles_per_key():
    clk = FakeClock()
    rl = RateLimiter(10, 1.0, clock=clk.now, sleep=clk.sleep)
    for _ in range(10):
        assert rl.acquire("nse_fo") == 0.0  # first 10 within the window are free
    slept = rl.acquire("nse_fo")  # 11th must wait ~1s
    assert slept == pytest.approx(1.0)


def test_rate_limiter_keys_are_independent():
    clk = FakeClock()
    rl = RateLimiter(10, 1.0, clock=clk.now, sleep=clk.sleep)
    for _ in range(10):
        rl.acquire("nse_fo")
    assert rl.acquire("bse_fo") == 0.0  # separate exchange not throttled


# -- Tick normalization ----------------------------------------------------------------


def test_normalize_tick_parses_common_keys():
    tick = normalize_tick(
        {"instrument_token": "11536", "ltp": "123.45", "exchange_segment": "bse_fo"},
        now=datetime(2025, 1, 15, tzinfo=UTC),
    )
    assert tick is not None
    assert tick.instrument_token == "11536"
    assert tick.ltp == Decimal("123.45")
    assert tick.exchange_segment is ExchangeSegment.BSE_FO


def test_normalize_tick_rejects_non_quote():
    assert normalize_tick({"foo": "bar"}) is None


# -- Stale-feed detection --------------------------------------------------------------


def test_feed_stale_detection():
    clk = FakeClock()
    settings = get_settings()
    fh = FeedHandler(settings, on_tick=lambda t: None, clock=clk.now, sleep=clk.sleep)
    fh.mark_started()
    assert fh.is_stale(now=clk.now()) is False
    assert fh.is_stale(now=settings.stale_feed_seconds + 1) is True


# -- Order event normalization ---------------------------------------------------------


def test_normalize_order_event_complete():
    ev = normalize_order_event(
        {"tag": "abc", "ordSt": "complete", "fldQty": "75", "qty": "75", "avgPrc": "101.5",
         "nOrdNo": "B99"},
        now=datetime(2025, 1, 15, tzinfo=UTC),
    )
    assert ev is not None
    assert ev.state is OrderState.FILLED
    assert ev.client_tag == "abc"
    assert ev.broker_order_id == "B99"
    assert ev.average_price == Decimal("101.5")


def test_normalize_order_event_partial():
    ev = normalize_order_event({"tag": "abc", "ordSt": "complete", "fldQty": "25", "qty": "75"})
    assert ev is not None and ev.state is OrderState.PARTIALLY_FILLED


def test_normalize_order_event_rejected_carries_reason():
    ev = normalize_order_event({"tag": "x", "ordSt": "rejected", "rejRsn": "insufficient margin"})
    assert ev is not None and ev.state is OrderState.REJECTED
    assert "margin" in ev.message


# -- KotakClient with a fake NeoAPI ----------------------------------------------------


class FakeNeo:
    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.reject = False

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        if self.reject:
            return {"stat": "Not_Ok", "errMsg": "RMS: insufficient margin"}
        return {"stat": "Ok", "data": {"nOrdNo": "ORD1"}}

    def positions(self):
        return {"data": [{"trading_symbol": "NIFTY", "netQty": "75"}]}


def test_kotak_client_place_order_converts_and_returns_id():
    client = KotakClient(get_settings(), neo_client=FakeNeo())
    req = make_order_request(client_tag="ct1")
    order_id = client.place_order(req)
    assert order_id == "ORD1"
    sent = client._neo.placed[0]  # type: ignore[attr-defined]
    assert sent["quantity"] == "75"  # SDK wants strings
    assert sent["exchange_segment"] == "nse_fo"  # NIFTY -> nse_fo
    assert sent["transaction_type"] == "B"
    assert sent["tag"] == "ct1"  # idempotency tag propagated


def test_kotak_client_raises_on_rejection():
    neo = FakeNeo()
    neo.reject = True
    client = KotakClient(get_settings(), neo_client=neo)
    with pytest.raises(OrderRejected):
        client.place_order(make_order_request(client_tag="ct2"))


def test_kotak_client_requires_auth():
    client = KotakClient(get_settings())
    with pytest.raises(AuthError):
        client.positions()
