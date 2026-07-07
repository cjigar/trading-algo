"""Import today's trades from the Kotak account into the DB (read-only; NO orders placed).

Connects to Kotak, reads the trade report (``trade_report``), normalizes each fill, and writes
it into the same database the dashboard reads — so "Trades today" shows your real account trades.
Deduplicated by broker fill id, so it's safe to run repeatedly.

It calls ONLY login + trade_report. It never calls place_order/modify/cancel.

Run:
    python -m algo_trading.tools.import_trades
    # Docker (image built with INSTALL_BROKER=1), writing to the same Postgres the dashboard uses:
    docker compose run --rm algo python -m algo_trading.tools.import_trades

The trade-report field names vary by SDK version; the normalizer uses candidate keys. If a row
isn't parsed, its keys are logged so TRADE_FIELD_CANDIDATES below can be adjusted.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.observability.logging import configure_logging, get_logger
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository

log = get_logger("tools.import_trades")

# our-field -> candidate trade-report column names (case-insensitive, first match wins)
TRADE_FIELD_CANDIDATES: dict[str, list[str]] = {
    "trading_symbol": ["pTrdSymbol", "trdSym", "tradingsymbol", "trdSymbol", "sym", "symbol"],
    "instrument_token": ["tok", "instrument_token", "token", "pSymbol"],
    "exchange_segment": ["exSeg", "exchange_segment", "exch", "seg"],
    "side": ["trnsTp", "buySell", "transaction_type", "side", "bs"],
    "quantity": ["fldQty", "trdQty", "qty", "filledQty", "fillQty", "quantity"],
    "price": ["avgPrc", "fldPrc", "trdPrc", "fillPrice", "price"],
    "order_id": ["nOrdNo", "order_id", "orderId", "ordNo"],
    "fill_id": ["flTrdId", "trdNo", "fillId", "tradeId", "flId", "exchOrdId"],
    "timestamp": ["flDtTm", "exchTime", "fillTime", "trdTime", "exch_time", "time"],
}

# Strike is not shown in "Trades today" and can't be reliably split from a delimiter-less symbol,
# so we only extract underlying + option type (used for the tolerant display fallback).


def _pick(raw: dict, candidates: list[str]) -> Any:
    lower = {k.lower(): k for k in raw}
    for cand in candidates:
        if cand.lower() in lower:
            return raw[lower[cand.lower()]]
    return None


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _to_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _norm_side(v: Any) -> str:
    s = str(v).strip().upper()
    if s in ("B", "BUY", "1", "+1"):
        return "B"
    if s in ("S", "SELL", "-1"):
        return "S"
    return s[:1] or "B"


def _parse_symbol(symbol: str) -> tuple[str, str, str]:
    """Best-effort (underlying, option_type, strike) from a trading symbol (strike always '0')."""
    up = (symbol or "").upper()
    underlying = "SENSEX" if "SENSEX" in up else ("NIFTY" if "NIFTY" in up else "NA")
    option_type = "CE" if up.endswith("CE") else ("PE" if up.endswith("PE") else "NA")
    return underlying, option_type, "0"


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    text = str(v).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_trade_row(raw: dict) -> dict | None:
    """Normalize a broker trade-report row into fields for record_broker_trade, or None."""
    symbol = _pick(raw, TRADE_FIELD_CANDIDATES["trading_symbol"])
    qty = _pick(raw, TRADE_FIELD_CANDIDATES["quantity"])
    price = _pick(raw, TRADE_FIELD_CANDIDATES["price"])
    if not symbol or qty is None or price is None:
        return None
    underlying, option_type, strike = _parse_symbol(str(symbol))
    fill_id = _pick(raw, TRADE_FIELD_CANDIDATES["fill_id"])
    order_id = _pick(raw, TRADE_FIELD_CANDIDATES["order_id"])
    tag_seed = fill_id or f"{order_id}-{symbol}-{qty}-{price}"
    return {
        "client_tag": f"trd-{tag_seed}",
        "broker_order_id": str(order_id) if order_id else None,
        "trading_symbol": str(symbol),
        "instrument_token": str(_pick(raw, TRADE_FIELD_CANDIDATES["instrument_token"]) or ""),
        "exchange_segment": str(_pick(raw, TRADE_FIELD_CANDIDATES["exchange_segment"]) or "nse_fo"),
        "underlying": underlying,
        "option_type": option_type,
        "strike": strike,
        "side": _norm_side(_pick(raw, TRADE_FIELD_CANDIDATES["side"])),
        "quantity": _to_int(qty),
        "price": _to_decimal(price),
        "timestamp": _parse_ts(_pick(raw, TRADE_FIELD_CANDIDATES["timestamp"])),
    }


def import_trades(repo: Repository, rows: list[dict]) -> dict:
    """Import normalized trade-report rows. Returns counts. Pure w.r.t. the broker (rows given)."""
    imported = skipped = unparsed = 0
    for raw in rows:
        fields = normalize_trade_row(raw)
        if fields is None:
            unparsed += 1
            log.warning("trade_row_unparsed", keys=list(raw.keys()))
            continue
        if repo.record_broker_trade(fields):
            imported += 1
        else:
            skipped += 1
    return {"imported": imported, "skipped_duplicate": skipped, "unparsed": unparsed,
            "total": len(rows)}


def main() -> int:
    configure_logging()
    settings = get_settings()
    secrets = load_secrets()

    # SDK + credentials
    try:
        from algo_trading.broker.kotak_client import KotakClient, _load_neo_api

        _load_neo_api()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Kotak SDK not available: {exc}")
        print('   Install with: pip install ".[broker]"  (or build image with INSTALL_BROKER=1)')
        return 1
    if not secrets.is_complete():
        print(f"❌ credentials incomplete; missing={secrets.missing_fields()}")
        return 1

    # Login (read-only) and fetch the trade report — NO orders.
    from algo_trading.broker.auth import SessionManager

    print("Authenticating (read-only)…")
    session = SessionManager(settings, secrets)
    neo = session.login()
    client = KotakClient(settings, neo_client=neo)

    print("Fetching trade report…")
    rows = client.trade_report()
    print(f"  broker returned {len(rows)} trade rows")

    repo = Repository(create_engine_from_settings(settings))
    summary = import_trades(repo, rows)

    print("\n=== Import summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\nOpen the dashboard — today's trades now appear under 'Trades today'. No orders placed.")
    if summary["unparsed"]:
        print("Some rows were unparsed; check logs for their keys and adjust "
              "TRADE_FIELD_CANDIDATES in tools/import_trades.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
