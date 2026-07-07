"""Probe whether Kotak delivers option Open Interest via the websocket, the quotes() REST call,
or both (read-only; NO orders). Resolves a few current-week NIFTY option tokens, then:
  1) calls quotes() on them and reports whether OI is present (works even market-closed),
  2) subscribes them on the websocket for a few seconds and reports whether OI appears in ticks
     (only meaningful during market hours).

Run: docker compose run --rm algo python -m algo_trading.tools.probe_oi
"""

from __future__ import annotations

import json
import time
from typing import Any

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.observability.logging import register_secret


def find_oi(obj: Any, path: str = "") -> list[str]:
    """Recursively find OI-looking keys anywhere in a nested dict/list. Returns 'path=value'."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("oi", "toi") or "openinterest" in kl or "interest" in kl or kl == "openint":
                hits.append(f"{path}{k}={v}")
            hits.extend(find_oi(v, f"{path}{k}."))
    elif isinstance(obj, list):
        for item in obj[:3]:
            hits.extend(find_oi(item, path))
    return hits


def _sample(obj: Any, limit: int = 500) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s[:limit] + (" …" if len(s) > limit else "")


def main() -> int:  # noqa: C901
    settings = get_settings()
    secrets = load_secrets()
    try:
        from algo_trading.broker.kotak_client import _load_neo_api

        _load_neo_api()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ SDK not available: {exc}")
        return 1
    if not secrets.is_complete():
        print(f"❌ credentials incomplete: {secrets.missing_fields()}")
        return 1

    from algo_trading.broker.auth import SessionManager

    print("Authenticating (read-only)…")
    neo = SessionManager(settings, secrets).login()

    # Resolve a few current-week NIFTY option tokens from the scrip master.
    from algo_trading.domain.enums import ExchangeSegment, OptionType, Underlying
    from algo_trading.instruments.option_resolver import WeeklyOptionResolver

    print("Downloading nse_fo scrip master…")
    sm = ScripMaster.download(neo, ExchangeSegment.NSE_FO)
    resolver = WeeklyOptionResolver(sm)
    expiry = resolver.current_week_expiry(Underlying.NIFTY)
    if expiry is None:
        print("❌ no current-week NIFTY expiry resolved")
        return 1
    ce_strikes = sm.strikes(Underlying.NIFTY, expiry, OptionType.CE)
    if not ce_strikes:
        print("❌ no NIFTY CE strikes for current expiry")
        return 1
    mid = ce_strikes[len(ce_strikes) // 2]  # a near-the-money strike (pseudo-ATM for the probe)
    probe = []
    for ot in (OptionType.CE, OptionType.PE):
        inst = sm.find(Underlying.NIFTY, expiry, mid, ot)
        if inst:
            probe.append(inst)
    tokens = [
        {"instrument_token": i.instrument_token, "exchange_segment": i.exchange_segment.value}
        for i in probe
    ]
    print(f"Probing expiry={expiry} strike={mid}: {[i.trading_symbol for i in probe]}")
    print(f"tokens: {tokens}")

    # --- 1) quotes() REST ---
    print("\n=== quotes() REST ===")
    quotes_has_oi = False
    for qtype in ("all", "market_depth", "ohlc", "ltp"):
        try:
            resp = neo.quotes(instrument_tokens=tokens, quote_type=qtype)
        except Exception as exc:  # noqa: BLE001
            print(f"  quote_type={qtype!r}: error {exc}")
            continue
        if isinstance(resp, dict) and "fault" in resp:
            print(f"  quote_type={qtype!r}: fault {resp['fault']}")
            continue
        oi = find_oi(resp)
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        first = data[0] if isinstance(data, list) and data else data
        print(f"  quote_type={qtype!r}: keys={list(first.keys()) if isinstance(first, dict) else type(first).__name__}")
        print(f"    sample: {_sample(first)}")
        if oi:
            quotes_has_oi = True
            print(f"    ✅ OI present via quotes(): {oi}")
            break

    # --- 2) websocket ---
    print("\n=== websocket (subscribe ~10s) ===")
    raw: list[dict] = []

    def on_msg(message: Any) -> None:
        recs = message if isinstance(message, list) else [message]
        for r in recs:
            if isinstance(r, dict) and len(raw) < 6:
                raw.append(r)

    neo.on_message = on_msg
    neo.on_error = lambda e: print(f"  ws error: {e}")
    neo.on_open = lambda *_a: print("  ws open")
    try:
        neo.subscribe(instrument_tokens=tokens, isIndex=False, isDepth=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  subscribe error: {exc}")
    time.sleep(8)

    socket_has_oi = False
    if not raw:
        print("  no ticks received (market likely closed — retry during 09:15–15:30 IST)")
    from algo_trading.broker.live_feed import _unwrap
    from algo_trading.broker.market_data import normalize_tick

    for i, msg in enumerate(raw):
        _, records = _unwrap(msg)
        for rec in records[:2]:
            if not isinstance(rec, dict):
                continue
            oi = find_oi(rec)
            print(f"  record[{i}] keys={list(rec.keys())}")
            tick = normalize_tick(rec)
            if tick is not None:
                print(f"    normalize_tick -> token={tick.instrument_token} ltp={tick.ltp} "
                      f"oi={tick.oi} volume={tick.volume}")
                if tick.oi is not None:
                    socket_has_oi = True
            else:
                print("    ⚠️ normalize_tick returned None (missing token/ltp key?)")
            if oi and not socket_has_oi:
                socket_has_oi = True
                print(f"    OI keys present: {oi}")

    # --- verdict ---
    print("\n=== VERDICT ===")
    print(f"OI via quotes() REST : {'YES' if quotes_has_oi else 'no/undetermined'}")
    print(f"OI via websocket     : {'YES' if socket_has_oi else 'no/undetermined (market closed?)'}")
    print("Use this to decide socket-stream vs quotes()-poll for OI in OptionChainManager.")
    for r in raw:  # redact any long values before exit
        for v in r.values():
            if isinstance(v, str) and len(v) > 12:
                register_secret(v)
    return 0


if __name__ == "__main__":
    import os
    import sys

    code = main()
    sys.stdout.flush()
    os._exit(code)  # force exit: the SDK's websocket thread is non-daemon and blocks a clean exit
