"""India VIX from NSE's public ``allIndices`` API.

Kotak Neo does not carry India VIX (its scrip search returns no VIX instrument on any segment),
so the rate ticker sources it directly from NSE. The endpoint is unofficial and browser-gated:
it rejects non-browser User-Agents and requires session cookies obtained by first hitting the
site root. This client primes those cookies, fetches the index list, and pulls the India VIX row.

Pure-ish and fail-soft: ``fetch_india_vix`` returns a :class:`VixQuote` or ``None`` (never raises),
so a throttled/blocked NSE response leaves the last published value to age to stale rather than
disturbing the loop. ``parse_india_vix`` is a pure function over the decoded JSON, unit-testable
without the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from algo_trading.observability.logging import get_logger

log = get_logger("feed.india_vix")

_BASE = "https://www.nseindia.com"
_ALL_INDICES = f"{_BASE}/api/allIndices"
# A browser User-Agent is mandatory — NSE returns 403/empty to non-browser clients.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{_BASE}/",
}


@dataclass(frozen=True)
class VixQuote:
    """A point-in-time India VIX reading from NSE, with the day's reference close."""

    last: Decimal
    prev_close: Decimal
    prev_day: date | None  # NSE's stated previous trading day for prev_close


def _dec(v: object) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_prev_day(v: object) -> date | None:
    """NSE dates the previous close as e.g. '23-Jul-2026'."""
    try:
        return datetime.strptime(str(v).strip(), "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return None


def parse_india_vix(payload: dict) -> VixQuote | None:
    """Extract India VIX from a decoded ``allIndices`` response. Pure; returns None if absent
    or malformed (last price and previous close are both required)."""
    for row in payload.get("data", []) or []:
        if not isinstance(row, dict):
            continue
        if "VIX" in str(row.get("index", "")).upper():
            last, prev = _dec(row.get("last")), _dec(row.get("previousClose"))
            if last is None or prev is None:
                return None
            return VixQuote(last=last, prev_close=prev, prev_day=_parse_prev_day(row.get("previousDay")))
    return None


def fetch_india_vix(timeout: float = 10.0) -> VixQuote | None:
    """Fetch India VIX from NSE. Fail-soft: returns None on any network/parse error (never raises).

    NSE requires session cookies from the site root before the API responds, so we prime them on a
    fresh session each call — the cookies are short-lived and this runs only every ~60s.
    """
    try:
        import requests

        with requests.Session() as s:
            s.headers.update(_HEADERS)
            s.get(_BASE, timeout=timeout)  # prime cookies
            resp = s.get(_ALL_INDICES, timeout=timeout)
            resp.raise_for_status()
            quote = parse_india_vix(resp.json())
            if quote is None:
                log.warning("india_vix_not_in_response")
            return quote
    except Exception:  # noqa: BLE001 - external, unofficial endpoint; must never raise into the loop
        log.warning("india_vix_fetch_failed", exc_info=True)
        return None
