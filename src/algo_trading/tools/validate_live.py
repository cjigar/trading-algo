"""Read-only live-API validation harness for Kotak Neo.

Run this AFTER installing the SDK and filling credentials + index tokens. It exercises the real
API end to end and reports what it finds, so we can reconcile the parsing/routing code with the
live response shapes.

IT PLACES NO ORDERS. Every call here is read-only (login, limits, positions, reports, scrip
master, market-data subscribe). It never calls place_order/modify/cancel.

Run:
    python -m algo_trading.tools.validate_live
    # or in Docker (image built with INSTALL_BROKER=1):
    docker compose run --rm algo python -m algo_trading.tools.validate_live

Best run during market hours so the index feed produces ticks.

NOTE: output may contain account/order identifiers from your account. Review before sharing.
"""

from __future__ import annotations

import json
import time
from typing import Any

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import OptionType, Underlying
from algo_trading.instruments.loader import required_segments
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.observability.logging import register_secret

PASS, FAIL, WARN, INFO = "✅ PASS", "❌ FAIL", "⚠️  WARN", "  •"


def _h(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _keys(obj: Any) -> Any:
    """Summarize a response as its structure (keys), not full values."""
    if isinstance(obj, dict):
        return {k: type(v).__name__ for k, v in obj.items()}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]"] + ([_keys(obj[0])] if obj else [])
    return type(obj).__name__


def _sample(obj: Any, limit: int = 600) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s[:limit] + (" …(truncated)" if len(s) > limit else "")


def main() -> int:  # noqa: C901 - a linear diagnostic script
    settings = get_settings()
    secrets = load_secrets()
    results: dict[str, str] = {}

    _h("0. Environment")
    print(f"{INFO} mode={settings.mode.value}  live_armed={settings.live_armed}")
    print(f"{INFO} underlyings={[u.value for u in settings.underlyings]}")
    print(f"{INFO} index tokens: NIFTY={bool(settings.nifty_index_token)} "
          f"SENSEX={bool(settings.sensex_index_token)}")

    # --- 0a. SDK present? ---
    try:
        from algo_trading.broker.kotak_client import KotakClient, _load_neo_api

        _load_neo_api()
        print(f"{PASS} neo_api_client import")
        results["sdk"] = PASS
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} SDK not installed: {exc}")
        print("      Install with: pip install \".[broker]\"  (or build image with INSTALL_BROKER=1)")
        return 1

    # --- 0b. credentials complete? ---
    if not secrets.is_complete():
        print(f"{FAIL} credentials incomplete; missing={secrets.missing_fields()}")
        return 1
    print(f"{PASS} credentials complete (TOTP flow: mobile + UCC + MPIN)")

    # --- 1. Login (login -> session_2fa) ---
    _h("1. Authentication  (login -> session_2fa)")
    from algo_trading.broker.auth import SessionManager

    session = SessionManager(settings, secrets)
    try:
        neo = session.login()
        print(f"{PASS} authenticated; trade token obtained")
        results["login"] = PASS
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} login failed: {exc}")
        print("      If it needs a phone OTP (not the MPIN), unattended login isn't possible —")
        print("      switch to the TOTP flow. See broker/auth.py _do_login/_do_2fa to adjust kwargs.")
        return 1

    client = KotakClient(settings, neo_client=neo)

    # --- 2. Read-only account calls (NO ORDERS) ---
    _h("2. Read-only account calls  (no orders placed)")
    for name, fn in [("limits", client.limits), ("positions", client.positions),
                     ("order_report", client.order_report), ("trade_report", client.trade_report)]:
        try:
            resp = fn()
            print(f"{PASS} {name}: structure={_keys(resp)}")
            results[name] = PASS
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {name}: {exc}")
            results[name] = FAIL

    # --- 3. Scrip master: columns, parsing, resolution ---
    _h("3. Scrip master  (columns + weekly-option resolution)")
    masters = []
    for seg in required_segments(settings):
        try:
            sm = ScripMaster.download(neo, seg)
            masters.append(sm)
            example = sm.instruments[0]
            print(f"{PASS} {seg.value}: parsed {len(sm)} option contracts")
            print(f"     e.g. {example.trading_symbol} strike={example.strike} "
                  f"{example.option_type.value} exp={example.expiry} lot={example.lot_size}")
            results[f"scrip:{seg.value}"] = PASS
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {seg.value}: {exc}")
            print("      -> confirm real CSV column names and update COLUMN_CANDIDATES in "
                  "instruments/scrip_master.py")
            results[f"scrip:{seg.value}"] = FAIL

    if masters:
        combined = ScripMaster([i for m in masters for i in m.instruments])
        resolver = WeeklyOptionResolver(combined)
        for u in settings.underlyings:
            try:
                spot = _guess_spot(client, settings, u)
                inst = resolver.resolve(u, spot, OptionType.CE)
                print(f"{PASS} resolved {u.value} ATM CE @spot~{spot}: {inst.trading_symbol} "
                      f"(token={inst.instrument_token})")
            except Exception as exc:  # noqa: BLE001
                print(f"{WARN} resolve {u.value}: {exc}")

    # --- 4. Live index feed: raw shapes + normalized ticks ---
    _h("4. Live market-data feed  (index quotes; ~8s sample)")
    print(f"{INFO} (needs market hours for ticks; routing/keys are validated regardless)")
    from algo_trading.broker.live_feed import LiveFeedCoordinator

    ticks: list = []
    order_events: list = []
    raw_samples: list = []

    coordinator = LiveFeedCoordinator(
        settings, neo, on_tick=ticks.append, on_order_event=order_events.append
    )
    subscribed = coordinator.start()
    print(f"{INFO} subscribed index feeds for: {[u.value for u in subscribed]}")

    # sniff a few raw messages to reveal the real shape (forwarding to the real dispatcher)
    dispatch = neo.on_message

    def _sniff(message: Any) -> None:
        recs = message if isinstance(message, list) else [message]
        for r in recs:
            if isinstance(r, dict) and len(raw_samples) < 4:
                raw_samples.append(r)
        dispatch(message)

    neo.on_message = _sniff
    time.sleep(8)

    for i, raw in enumerate(raw_samples):
        print(f"{INFO} raw msg[{i}] keys={list(raw.keys())}")
        print(f"       sample={_sample(raw)}")
    print(f"{'✅' if ticks else '⚠️ '} normalized ticks received: {len(ticks)}"
          + (f" (e.g. token={ticks[0].instrument_token} ltp={ticks[0].ltp})" if ticks else
             " — 0 (market closed, wrong index token, or routing mismatch)"))
    if order_events:
        print(f"{INFO} order-feed events received: {len(order_events)}")

    # --- Summary ---
    _h("Summary")
    for k, v in results.items():
        print(f"  {v}  {k}")
    print("\nNo orders were placed. If ticks=0 during market hours, check the index tokens and the")
    print("is_order_message()/normalize_tick() keys against the raw msg samples above.")
    # register any tokens seen so a subsequent log line can't leak them
    for raw in raw_samples:
        for v in raw.values():
            if isinstance(v, str) and len(v) > 12:
                register_secret(v)
    return 0


def _guess_spot(client: Any, settings: Any, underlying: Underlying) -> Any:
    """Best-effort underlying spot for resolution; falls back to a nominal value."""
    from decimal import Decimal

    # Without a dedicated LTP call wired here, use a nominal spot just to exercise resolution.
    return Decimal("23000") if underlying is Underlying.NIFTY else Decimal("75000")


if __name__ == "__main__":
    raise SystemExit(main())
