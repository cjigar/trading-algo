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


# -- REST quotes (LTP source while the quote websocket is decommissioned) ---------------


class FakeQuotesNeo:
    """Minimal NeoAPI stand-in that records quotes() calls and replays a canned response."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[dict] = []

    def quotes(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


def _kotak_with_quotes(rows):
    neo = FakeQuotesNeo(rows)
    return KotakClient(get_settings(), neo_client=neo), neo


def test_quotes_maps_token_to_ltp():
    client, _ = _kotak_with_quotes([
        {"exchange_token": "835470", "ltp": "150.2500"},
        {"exchange_token": "999", "ltp": "12.0000"},
    ])
    assert client.quotes([("835470", "bse_fo"), ("999", "nse_cm")]) == {
        "835470": Decimal("150.25"),
        "999": Decimal("12"),
    }


def test_quotes_omits_unusable_prices():
    """A zero/blank/garbage LTP must read as 'no quote'.

    Marking an open short at 0 would display a enormous fabricated profit, so an unusable
    price must leave the token absent (-> mtm_pending) rather than price it at zero.
    """
    client, _ = _kotak_with_quotes([
        {"exchange_token": "A", "ltp": "0"},
        {"exchange_token": "B", "ltp": ""},
        {"exchange_token": "C", "ltp": "n/a"},
        {"exchange_token": "D", "ltp": "7.5"},
    ])
    assert client.quotes([(t, "bse_fo") for t in "ABCD"]) == {"D": Decimal("7.5")}


def test_quotes_tolerates_missing_tokens_in_response():
    """A partial response is normal; unmatched tokens are simply absent."""
    client, _ = _kotak_with_quotes([{"exchange_token": "A", "ltp": "5"}])
    assert client.quotes([("A", "bse_fo"), ("B", "bse_fo")]) == {"A": Decimal("5")}


def test_quotes_sends_one_batched_call_with_segment_pairs():
    client, neo = _kotak_with_quotes([])
    client.quotes([("A", "bse_fo"), ("B", "nse_cm")])
    assert len(neo.calls) == 1
    assert neo.calls[0]["instrument_tokens"] == [
        {"instrument_token": "A", "exchange_segment": "bse_fo"},
        {"instrument_token": "B", "exchange_segment": "nse_cm"},
    ]


def test_quotes_empty_input_makes_no_sdk_call():
    client, neo = _kotak_with_quotes([])
    assert client.quotes([]) == {}
    assert neo.calls == []


def test_paper_broker_quotes_is_empty():
    from algo_trading.execution.paper_broker import PaperBroker

    assert PaperBroker().quotes([("A", "bse_fo")]) == {}


def test_quotes_nan_does_not_abort_the_batch():
    """Decimal("NaN") parses, so a naive `ltp > 0` raises OUTSIDE the guard and loses the
    whole batch. One garbage field must not cost the good prices beside it."""
    client, _ = _kotak_with_quotes([
        {"exchange_token": "A", "ltp": "12.5"},
        {"exchange_token": "B", "ltp": "NaN"},
        {"exchange_token": "C", "ltp": "9"},
    ])
    assert client.quotes([(t, "bse_fo") for t in "ABC"]) == {
        "A": Decimal("12.5"), "C": Decimal("9"),
    }


def test_quotes_drops_infinity():
    """Infinity parses AND passes `> 0`; it would reach float() and emit bare `Infinity`,
    which is invalid JSON for the SSE consumer."""
    client, _ = _kotak_with_quotes([
        {"exchange_token": "A", "ltp": "Infinity"},
        {"exchange_token": "B", "ltp": "3"},
    ])
    assert client.quotes([("A", "bse_fo"), ("B", "bse_fo")]) == {"B": Decimal("3")}


def test_quotes_logs_when_a_response_yields_no_prices(monkeypatch):
    """The SDK returns error DICTS rather than raising (rest.py's status check is commented
    out), so the orchestrator's except never fires. Without a log an outage is silent -- the
    last one went unnoticed for two months."""
    import algo_trading.broker.kotak_client as kc

    seen: list[tuple] = []
    monkeypatch.setattr(kc.log, "warning", lambda ev, **kw: seen.append((ev, kw)))
    client, _ = _kotak_with_quotes({"error": [{"message": "Validation Errors!"}]})
    assert client.quotes([("A", "bse_fo")]) == {}
    assert any(ev == "broker_quotes_empty" for ev, _ in seen)


def test_no_operator_instruction_points_at_the_blocked_repo():
    """GitHub blocked Kotak-neo-api-v2 on 2026-11-05 and the SDK is on no package index, so
    `pip install ".[broker]"` and the git URL are both dead ends. An operator who hits one of
    these messages at 09:20 IST must be sent somewhere that works."""
    import pathlib as _p

    root = _p.Path(__file__).resolve().parents[1] / "src"
    offenders = [
        f"{path.relative_to(root)}:{i}"
        for path in root.rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "Kotak-neo-api-v2.git" in line or '".[broker]"' in line
    ]
    assert not offenders, (
        "These operator-facing instructions point at the blocked upstream repo; send them to "
        f"the vendored wheel (make install-broker) instead: {offenders}"
    )
