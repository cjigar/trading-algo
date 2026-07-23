"""Read-only option-chain capture: stream the live NIFTY chain (OI/LTP/volume) into the DB.

This authenticates and attaches the live websocket feed, but **never evaluates the strategy and
never places an order** — it exists solely to populate the option-chain time series (and the
dashboard's Option Chain tab) so you can validate OI capture safely before ever arming shorts.
A PaperBroker is wired as a belt-and-braces guard so nothing can reach the live order API.

Run:
    python -m algo_trading.entrypoints.run_capture
    docker compose run --rm algo python -m algo_trading.entrypoints.run_capture

Requires the Kotak SDK + credentials + ALGO_NIFTY_INDEX_TOKEN (so ATM/chain can resolve).
Best run during market hours (09:15–15:30 IST) so quotes stream.
"""

from __future__ import annotations

import signal
import time

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.loader import load_scrip_master
from algo_trading.observability.logging import configure_logging, get_logger

log = get_logger("run_capture")


def build_capture_orchestrator() -> Orchestrator:
    settings = get_settings()
    # Capture is inherently the option-chain path.
    object.__setattr__(settings, "strategy", "oi_selling")

    secrets = load_secrets()
    if not secrets.is_complete():
        raise SystemExit(f"❌ credentials incomplete: {secrets.missing_fields()}")

    from algo_trading.broker.auth import SessionManager  # local import: SDK only when capturing

    log.info("authenticating")
    neo = SessionManager(settings, secrets).login()
    scrip = load_scrip_master(settings, neo_client=neo)

    # PaperBroker => no order can ever reach the live API, even if code tried to.
    return Orchestrator(
        settings, scrip_master=scrip, broker=PaperBroker(), neo_client=neo, secrets=secrets
    )


def main() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.nifty_index_token:
        log.warning(
            "nifty_index_token_missing",
            hint="set ALGO_NIFTY_INDEX_TOKEN so ATM/chain can resolve; capture will be idle without it",
        )

    orch = build_capture_orchestrator()
    orch.start_session()
    attached = orch.attach_live_feeds()  # index + option-chain quotes stream into the chain manager
    log.info("capture_started", feeds_attached=attached,
             note="NO strategy evaluation · NO orders · chain snapshots -> DB")

    stop = {"flag": False}

    def _handle_sigterm(*_a):  # pragma: no cover
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while not stop["flag"]:  # pragma: no cover - long-running loop
            orch.flush_snapshots()  # persist buffered chain snapshots
            orch.recover_stale_feed()  # a subscription lost to the connect race self-heals here
            orch.write_index_spots()  # live NIFTY/SENSEX spot for the dashboard ticker
            time.sleep(2.0)
    finally:
        orch.flush_snapshots()
        orch.stop_session()
        log.info("capture_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
