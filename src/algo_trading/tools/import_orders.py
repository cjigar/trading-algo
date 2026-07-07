"""Import today's order book from the Kotak account into the DB (read-only; NO orders placed).

Connects to Kotak, reads the order report (``order_report``), normalizes each order, and upserts
it into the DB the dashboard reads — so the "Orders" tab shows your real order book. Keyed by
broker order id, so re-running refreshes status/fills. Calls ONLY login + order_report; never
place/modify/cancel.

Run:
    python -m algo_trading.tools.import_orders
    docker compose run --rm algo python -m algo_trading.tools.import_orders
"""

from __future__ import annotations

from typing import Any

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.observability.logging import configure_logging, get_logger
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository

log = get_logger("tools.import_orders")

# our-field -> candidate order-report column names (case-insensitive, first match wins)
ORDER_FIELD_CANDIDATES: dict[str, list[str]] = {
    "order_id": ["nOrdNo", "order_id", "orderId", "ordNo", "nsOrdNo"],
    "trading_symbol": ["trdSym", "pTrdSymbol", "tradingsymbol", "sym", "symbol"],
    "side": ["trnsTp", "buySell", "transaction_type", "side", "bs"],
    "quantity": ["qty", "quantity", "ordQty", "totQty"],
    "filled_quantity": ["fldQty", "filledQty", "fillQty", "cumQty"],
    "price": ["prc", "avgPrc", "price", "ordPrc"],
    "order_type": ["prcTp", "order_type", "ordTyp", "prctype"],
    "product": ["prod", "product", "prodType"],
    "status": ["ordSt", "status", "orderStatus"],
    "order_time": ["ordDtTm", "flDtTm", "orderTime", "ordEntTm", "hsUpTm"],
}


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


def _norm_side(v: Any) -> str:
    s = str(v).strip().upper()
    if s in ("B", "BUY", "1", "+1"):
        return "B"
    if s in ("S", "SELL", "-1"):
        return "S"
    return s[:1] or "B"


def normalize_order_row(raw: dict) -> dict | None:
    """Normalize a broker order-report row into fields for record_broker_order, or None."""
    order_id = _pick(raw, ORDER_FIELD_CANDIDATES["order_id"])
    symbol = _pick(raw, ORDER_FIELD_CANDIDATES["trading_symbol"])
    if not order_id or not symbol:
        return None
    return {
        "order_id": str(order_id),
        "trading_symbol": str(symbol),
        "side": _norm_side(_pick(raw, ORDER_FIELD_CANDIDATES["side"])),
        "quantity": _to_int(_pick(raw, ORDER_FIELD_CANDIDATES["quantity"])),
        "filled_quantity": _to_int(_pick(raw, ORDER_FIELD_CANDIDATES["filled_quantity"])),
        "price": str(_pick(raw, ORDER_FIELD_CANDIDATES["price"]) or "0"),
        "order_type": str(_pick(raw, ORDER_FIELD_CANDIDATES["order_type"]) or ""),
        "product": str(_pick(raw, ORDER_FIELD_CANDIDATES["product"]) or ""),
        "status": str(_pick(raw, ORDER_FIELD_CANDIDATES["status"]) or ""),
        "order_time": str(_pick(raw, ORDER_FIELD_CANDIDATES["order_time"]) or ""),
    }


def import_orders(repo: Repository, rows: list[dict]) -> dict:
    """Upsert normalized order-report rows. Returns counts."""
    inserted = updated = unparsed = 0
    for raw in rows:
        fields = normalize_order_row(raw)
        if fields is None:
            unparsed += 1
            log.warning("order_row_unparsed", keys=list(raw.keys()))
            continue
        if repo.record_broker_order(fields):
            inserted += 1
        else:
            updated += 1
    return {"inserted": inserted, "updated": updated, "unparsed": unparsed, "total": len(rows)}


def main() -> int:
    configure_logging()
    settings = get_settings()
    secrets = load_secrets()

    try:
        from algo_trading.broker.kotak_client import KotakClient, _load_neo_api

        _load_neo_api()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Kotak SDK not available: {exc}")
        return 1
    if not secrets.is_complete():
        print(f"❌ credentials incomplete; missing={secrets.missing_fields()}")
        return 1

    from algo_trading.broker.auth import SessionManager

    print("Authenticating (read-only)…")
    neo = SessionManager(settings, secrets).login()
    client = KotakClient(settings, neo_client=neo)

    print("Fetching order report…")
    rows = client.order_report()
    print(f"  broker returned {len(rows)} order rows")

    repo = Repository(create_engine_from_settings(settings))
    summary = import_orders(repo, rows)

    print("\n=== Import summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\nOpen the dashboard 'Orders' tab. No orders placed.")
    if summary["unparsed"]:
        print("Some rows were unparsed; check logs for their keys and adjust "
              "ORDER_FIELD_CANDIDATES in tools/import_orders.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
