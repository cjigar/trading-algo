"""Entry point for the trading loop process.

Builds the orchestrator (paper by default; live requires ALGO_MODE=live + ALGO_CONFIRM_LIVE=YES),
starts the session, wires the daily scheduler, and runs a control loop that (in live mode) is
driven by the websocket feeds and (in both modes) polls dashboard control commands.

Run: ``python -m algo_trading.entrypoints.run_algo``
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import UTC, date, datetime

from algo_trading.config.secrets import load_secrets
from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import LiveModeNotArmedError, Orchestrator
from algo_trading.core.scheduler import (
    IST,
    MarketScheduler,
    in_trading_window,
    is_trading_day,
    should_hard_recover,
)
from algo_trading.domain.enums import TradingMode
from algo_trading.instruments.loader import load_scrip_master
from algo_trading.observability.logging import configure_logging, get_logger

log = get_logger("run_algo")


def reexec_process(reason: str) -> None:  # pragma: no cover - replaces the process image
    """Restart this process in place for a clean, fully re-authenticated start.

    A day-scoped broker token cannot be refreshed by resubscribing an existing websocket, and
    swapping a live SDK client mid-process is the code path that previously leaked sockets and
    threads. Re-exec instead: ``execv`` replaces the image, so every old socket/thread/FD is
    discarded and the proven cold-start path (login -> attach feeds -> arm) runs from scratch.
    Under tini (PID 1) the container stays up; the Python child is simply replaced.
    """
    log.warning("reexec", reason=reason)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def compute_purge_date(now_utc: datetime) -> date:
    """The current IST calendar date — the boundary the expiry purge keys off (NIFTY rolls
    Wednesday, SENSEX Friday, both in IST)."""
    return now_utc.astimezone(IST).date()


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
    # Operator alerting (Telegram) — no-op unless ALGO_ALERTS_ENABLED + TELEGRAM_BOT_TOKEN + chat id.
    # Init before the first log so the 'starting' event can alert (doubles as a crash-loop signal).
    from algo_trading.observability import alerts

    alerts.init_alerts(settings)
    log.info("starting", mode=settings.mode.value, live_armed=settings.live_armed)

    orch = build_orchestrator()
    # The daily lifecycle is automatic: arm only inside the trading window (weekday, not a holiday,
    # 09:15-15:15 IST). A boot/redeploy mid-session resumes trading; outside the window we still
    # reconcile broker state for the dashboard but stay IDLE until the 09:15 market-open job arms.
    # start_session() itself refuses to override a persisted HALTED (manual stop / kill-switch).
    if in_trading_window(datetime.now(UTC), settings):
        orch.start_session()
        log.info("boot_armed", reason="inside trading window")
    else:
        orch.reconcile()
        log.info("boot_idle", reason="outside trading window; awaiting market_open")
    # Attach the live Kotak websocket feeds (no-op in paper mode without an authenticated client).
    orch.attach_live_feeds()

    # Two-way Telegram control (/clear -> flatten, /stop -> stop). No-op unless explicitly enabled;
    # honours only the operator's chat id. Enqueues the same control commands the dashboard buttons
    # do, which the loop consumes via process_control_commands().
    from algo_trading.observability import telegram_commands

    telegram_commands.start_listener(settings, on_command=orch.repo.enqueue_command)

    def _on_market_open() -> None:
        # Cron fires Mon-Fri; skip holidays (and a same-day HALTED is respected by start_session).
        if in_trading_window(datetime.now(UTC), settings):
            orch.start_session()
        else:
            log.info("market_open_skipped", reason="not a trading day")

    def _on_squareoff() -> None:
        # Flatten and also block new entries through the 15:15-15:30 tail.
        orch.square_off_all("scheduled square-off")
        orch.stop_session()

    def _on_premarket_login() -> None:
        # Fresh, day-scoped broker login before the 09:15 open. Re-exec so the cold-start path
        # runs (login -> attach feeds); the 09:15 market_open job then arms. Holidays are skipped.
        if is_trading_day(datetime.now(UTC).astimezone(IST).date(), settings):
            reexec_process("daily pre-market re-login")
        else:
            log.info("premarket_login_skipped", reason="not a trading day")

    scheduler = MarketScheduler(
        settings,
        on_premarket_login=_on_premarket_login,
        on_market_open=_on_market_open,
        on_squareoff=_on_squareoff,
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
    # The dashboard process has no broker session, so it can only show live P&L if we publish
    # ours — and the open positions' prices — to the shared database on a timer.
    pnl_every = max(1, settings.pnl_snapshot_seconds)
    since_pnl = 0
    # Poll the live broker account (positions/orders/trades) onto the DB so the dashboard's broker
    # views track the real Kotak account instead of a boot-time snapshot.
    broker_every = max(1, settings.broker_refresh_seconds)
    since_broker = 0
    # India VIX is not on the Kotak feed, so it is pulled from NSE's public API on its own (slow)
    # cadence — it updates only every few minutes.
    vix_every = max(1, settings.india_vix_seconds)
    since_vix = vix_every  # fetch on the first tick so the chip appears without waiting a cycle
    # Monotonic timestamp the feed first went stale (None while healthy), for hard recovery.
    stale_since: float | None = None
    # Expiry-aligned retention: purge each week's snapshots once its expiry passes. Runs once at
    # startup and then once per IST day (a cheap idempotent DELETE). No-op in "days" retention mode.
    last_purge: date | None = None
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
                today_ist = compute_purge_date(datetime.now(UTC))
                if today_ist != last_purge:
                    deleted = orch.purge_expired_snapshots(today_ist)
                    log.info("chain_purge", deleted=deleted, as_of=str(today_ist))
                    last_purge = today_ist
            except Exception:  # noqa: BLE001
                log.exception("chain_purge_failed")
            try:
                # Reconnects a feed that went quiet — including a subscription the SDK dropped
                # because it was issued before the websocket finished connecting.
                orch.recover_stale_feed()
                # Hard recovery: if the feed stays stale through the trading window despite those
                # resubscribes, the broker session is dead (e.g. token expired) — re-exec for a
                # fresh login. Gated to the trading window so a quiet pre/post-market never trips
                # it. Stays reset outside the window and whenever ticks are flowing.
                if in_trading_window(datetime.now(UTC), settings) and orch.feed_is_stale():
                    stale_since = stale_since if stale_since is not None else time.monotonic()
                    if should_hard_recover(
                        stale_since, time.monotonic(), settings.feed_hard_recover_seconds
                    ):
                        reexec_process("feed stale beyond hard-recover threshold; session likely expired")
                else:
                    stale_since = None
            except Exception:  # noqa: BLE001
                log.exception("feed_recovery_failed")
            elapsed += 1
            if settings.strategy == "oi_selling" and elapsed >= eval_every:
                try:
                    orch.evaluate_oi()
                except Exception:  # noqa: BLE001
                    log.exception("oi_evaluation_failed")
                elapsed = 0
            since_pnl += 1
            if since_pnl >= pnl_every:
                try:
                    orch.write_pnl_snapshot()
                except Exception:  # noqa: BLE001
                    log.exception("pnl_snapshot_failed")
                try:
                    orch.write_index_spots()  # live NIFTY/SENSEX spot for the dashboard ticker
                except Exception:  # noqa: BLE001
                    log.exception("index_spot_publish_failed")
                since_pnl = 0
            since_broker += 1
            if since_broker >= broker_every:
                try:
                    orch.refresh_broker_account()
                except Exception:  # noqa: BLE001
                    log.exception("broker_account_refresh_failed")
                since_broker = 0
            since_vix += 1
            if since_vix >= vix_every:
                try:
                    orch.refresh_india_vix()  # NSE-sourced India VIX for the dashboard ticker
                except Exception:  # noqa: BLE001
                    log.exception("india_vix_refresh_failed")
                since_vix = 0
            time.sleep(1.0)
    finally:
        scheduler.shutdown()
        orch.stop_session()
        log.info("stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
