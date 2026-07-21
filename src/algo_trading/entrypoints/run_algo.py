"""Entry point for the trading loop process.

Builds the orchestrator (paper by default; live requires ALGO_MODE=live + ALGO_CONFIRM_LIVE=YES),
starts the session, wires the daily scheduler, and runs a control loop that (in live mode) is
driven by the websocket feeds and (in both modes) polls dashboard control commands.

Run: ``python -m algo_trading.entrypoints.run_algo``
"""

from __future__ import annotations

import signal
import time

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import LiveModeNotArmedError, Orchestrator
from algo_trading.core.scheduler import MarketScheduler
from algo_trading.domain.enums import TradingMode
from algo_trading.instruments.loader import load_scrip_master
from algo_trading.observability.logging import configure_logging, get_logger

log = get_logger("run_algo")


def build_orchestrator() -> Orchestrator:
    settings = get_settings()
    secrets = load_secrets()

    neo_client = None
    if settings.mode is TradingMode.LIVE:
        if not settings.live_armed:
            raise LiveModeNotArmedError(
                "ALGO_MODE=live requires ALGO_CONFIRM_LIVE=YES to arm real orders."
            )
        # Live: authenticate and download the scrip master via the SDK.
        from algo_trading.broker.auth import SessionManager  # local import: SDK only in live mode

        session = SessionManager(settings, secrets)
        neo_client = session.login()

    scrip_master = load_scrip_master(settings, neo_client=neo_client)
    # Pass the already-authenticated client so the orchestrator reuses it for both the broker
    # and the live websocket feeds (no second login).
    orch = Orchestrator(
        settings, scrip_master=scrip_master, secrets=secrets, neo_client=neo_client
    )
    return orch


def main() -> None:
    configure_logging()
    settings = get_settings()
    log.info("starting", mode=settings.mode.value, live_armed=settings.live_armed)

    orch = build_orchestrator()
    orch.start_session()
    # Attach the live Kotak websocket feeds (no-op in paper mode without an authenticated client).
    orch.attach_live_feeds()

    scheduler = MarketScheduler(
        settings,
        on_premarket_login=lambda: log.info("premarket_login_tick"),
        on_squareoff=lambda: orch.square_off_all("scheduled square-off"),
        on_logout=lambda: orch.stop_session(),
    )
    scheduler.start()

    stop = {"flag": False}

    def _handle_sigterm(*_a):  # pragma: no cover
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # OI-selling strategy is evaluated on a timer (not candle-driven); vwap_breakout is tick-driven.
    eval_every = max(1, settings.chain_eval_seconds)
    elapsed = 0
    log.info("running", strategy=settings.strategy)
    try:
        while not stop["flag"]:  # pragma: no cover - long-running loop
            # A transient error (e.g. a broker response we can't parse) must never kill the
            # loop — it would stop command processing and the scheduled square-off safety net.
            try:
                orch.process_control_commands()
            except Exception:  # noqa: BLE001
                log.exception("control_command_processing_failed")
            try:
                # Reconnects a feed that went quiet — including a subscription the SDK dropped
                # because it was issued before the websocket finished connecting.
                orch.recover_stale_feed()
            except Exception:  # noqa: BLE001
                log.exception("feed_recovery_failed")
            elapsed += 1
            if settings.strategy == "oi_selling" and elapsed >= eval_every:
                try:
                    orch.evaluate_oi()
                except Exception:  # noqa: BLE001
                    log.exception("oi_evaluation_failed")
                elapsed = 0
            time.sleep(1.0)
    finally:
        scheduler.shutdown()
        orch.stop_session()
        log.info("stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
