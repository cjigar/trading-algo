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

from algo_trading.broker.report_normalize import ORDER_FIELD_CANDIDATES, normalize_order_row
from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.observability.logging import configure_logging, get_logger
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository

log = get_logger("tools.import_orders")

# Normalizer (and its candidate-key table) now lives in broker.report_normalize so the live
# broker-account poller can share it; re-exported here for backwards compatibility.
__all__ = ["ORDER_FIELD_CANDIDATES", "normalize_order_row", "import_orders", "main"]


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
