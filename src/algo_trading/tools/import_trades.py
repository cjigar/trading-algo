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

from algo_trading.broker.report_normalize import TRADE_FIELD_CANDIDATES, normalize_trade_row
from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.observability.logging import configure_logging, get_logger
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository

log = get_logger("tools.import_trades")

# Normalizer (and its candidate-key table) now lives in broker.report_normalize so the live
# broker-account poller can share it; re-exported here for backwards compatibility.
__all__ = ["TRADE_FIELD_CANDIDATES", "normalize_trade_row", "import_trades", "main"]


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
