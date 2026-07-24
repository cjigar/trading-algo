"""NSE India VIX parsing (pure; no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from algo_trading.feed.india_vix import parse_india_vix

_ALL_INDICES = {
    "data": [
        {"index": "NIFTY 50", "last": 23671.95, "previousClose": 23869.60, "previousDay": "23-Jul-2026"},
        {"index": "INDIA VIX", "last": 14.45, "previousClose": 13.48, "previousDay": "23-Jul-2026"},
    ]
}


def test_parse_india_vix_extracts_the_vix_row():
    q = parse_india_vix(_ALL_INDICES)
    assert q is not None
    assert q.last == Decimal("14.45")
    assert q.prev_close == Decimal("13.48")
    assert q.prev_day == date(2026, 7, 23)


def test_parse_india_vix_none_when_absent():
    assert parse_india_vix({"data": [{"index": "NIFTY 50", "last": 1, "previousClose": 1}]}) is None
    assert parse_india_vix({}) is None


def test_parse_india_vix_none_on_missing_prices():
    # A VIX row without a usable last/previousClose must not yield a half-populated quote.
    assert parse_india_vix({"data": [{"index": "INDIA VIX", "last": None, "previousClose": 13.48}]}) is None


def test_parse_india_vix_tolerates_missing_prev_day():
    q = parse_india_vix({"data": [{"index": "INDIA VIX", "last": 14.45, "previousClose": 13.48}]})
    assert q is not None and q.prev_day is None
