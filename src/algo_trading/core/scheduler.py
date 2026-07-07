"""Market-hours gating and the daily lifecycle timers.

Pure helpers (``is_market_open``, ``is_after``) are unit-testable. :class:`MarketScheduler`
wires them onto APScheduler for the live service: a pre-market login job, an independent
end-of-day square-off job, and a market-close logout job. The square-off job is deliberately
independent of the strategy/feed path so it fires even if that path is degraded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from algo_trading.config.settings import Settings
from algo_trading.observability.logging import get_logger

log = get_logger("core.scheduler")
IST = ZoneInfo("Asia/Kolkata")


def is_market_open(now: datetime, settings: Settings) -> bool:
    local = now.astimezone(IST).time()
    return settings.market_open <= local < settings.market_close


def is_after(now: datetime, when: time) -> bool:
    return now.astimezone(IST).time() >= when


class MarketScheduler:
    def __init__(
        self,
        settings: Settings,
        *,
        on_premarket_login: Callable[[], None],
        on_squareoff: Callable[[], None],
        on_logout: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._on_premarket_login = on_premarket_login
        self._on_squareoff = on_squareoff
        self._on_logout = on_logout
        self._scheduler = None

    def start(self) -> None:  # pragma: no cover - exercised only in a live run
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        s = self._settings
        sched = BackgroundScheduler(timezone=IST)
        sched.add_job(
            self._safe(self._on_premarket_login, "premarket_login"),
            CronTrigger(hour=s.premarket_login_time.hour, minute=s.premarket_login_time.minute,
                        day_of_week="mon-fri"),
            id="premarket_login",
        )
        sched.add_job(
            self._safe(self._on_squareoff, "squareoff"),
            CronTrigger(hour=s.squareoff_time.hour, minute=s.squareoff_time.minute,
                        day_of_week="mon-fri"),
            id="squareoff",
        )
        sched.add_job(
            self._safe(self._on_logout, "logout"),
            CronTrigger(hour=s.market_close.hour, minute=s.market_close.minute,
                        day_of_week="mon-fri"),
            id="logout",
        )
        sched.start()
        self._scheduler = sched
        log.info("scheduler_started")

    def shutdown(self) -> None:  # pragma: no cover
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    @staticmethod
    def _safe(fn: Callable[[], None], name: str) -> Callable[[], None]:
        def _wrapped() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("scheduled_job_failed", job=name)

        return _wrapped
