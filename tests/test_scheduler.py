"""Unit tests for the market-hours helpers that drive the automatic daily lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algo_trading.config.settings import get_settings
from algo_trading.core.scheduler import (
    in_trading_window,
    is_trading_day,
    should_hard_recover,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def settings():
    # Defaults: open 09:15, square-off 15:15, close 15:30, no holidays.
    return get_settings(reload=True)


def _ist(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# --- is_trading_day -------------------------------------------------------------------

def test_is_trading_day_weekday(settings):
    # 2026-07-23 is a Thursday.
    assert is_trading_day(_ist(2026, 7, 23, 10, 0).date(), settings) is True


def test_is_trading_day_weekend(settings):
    # 2026-07-25 is a Saturday, 2026-07-26 a Sunday.
    assert is_trading_day(_ist(2026, 7, 25, 10, 0).date(), settings) is False
    assert is_trading_day(_ist(2026, 7, 26, 10, 0).date(), settings) is False


def test_is_trading_day_holiday(settings):
    settings.market_holidays = ["2026-07-23"]
    assert is_trading_day(_ist(2026, 7, 23, 10, 0).date(), settings) is False


# --- in_trading_window ----------------------------------------------------------------

@pytest.mark.parametrize(
    "hh, mm, expected",
    [
        (9, 14, False),   # one minute before open
        (9, 15, True),    # exactly at open
        (11, 0, True),    # mid-session
        (15, 14, True),   # one minute before square-off
        (15, 15, False),  # exactly at square-off — window has ended
        (15, 45, False),  # after close
        (2, 0, False),    # middle of the night
    ],
)
def test_in_trading_window_times(settings, hh, mm, expected):
    now = _ist(2026, 7, 23, hh, mm)  # Thursday
    assert in_trading_window(now, settings) is expected


def test_in_trading_window_false_on_weekend(settings):
    assert in_trading_window(_ist(2026, 7, 25, 11, 0), settings) is False  # Saturday


def test_in_trading_window_false_on_holiday(settings):
    settings.market_holidays = ["2026-07-23"]
    assert in_trading_window(_ist(2026, 7, 23, 11, 0), settings) is False


def test_in_trading_window_accepts_utc_input(settings):
    # 05:45 UTC == 11:15 IST on a Thursday -> inside the window.
    now = datetime(2026, 7, 23, 5, 45, tzinfo=ZoneInfo("UTC"))
    assert in_trading_window(now, settings) is True


# --- MarketScheduler job registration -------------------------------------------------

def test_scheduler_registers_market_open_arm_job(settings):
    from algo_trading.core.scheduler import MarketScheduler

    noop = lambda: None  # noqa: E731
    sched = MarketScheduler(
        settings,
        on_premarket_login=noop,
        on_market_open=noop,
        on_squareoff=noop,
        on_logout=noop,
    )
    sched.start()
    try:
        job_ids = {j.id for j in sched._scheduler.get_jobs()}
        assert {"premarket_login", "market_open", "squareoff", "logout"} <= job_ids
    finally:
        sched.shutdown()


def test_scheduler_jobs_fire_in_ist_not_utc(settings):
    """Guard against the APScheduler gotcha where a trigger without an explicit timezone
    captures the process's local zone (UTC in the container) and fires 5h30 late. On a UTC host
    this fails unless each CronTrigger is given timezone=IST."""
    from algo_trading.core.scheduler import MarketScheduler

    noop = lambda: None  # noqa: E731
    sched = MarketScheduler(
        settings, on_premarket_login=noop, on_market_open=noop,
        on_squareoff=noop, on_logout=noop,
    )
    sched.start()
    try:
        jobs = {j.id: j for j in sched._scheduler.get_jobs()}
        # Every job's next fire must be at IST offset (+5:30), not UTC.
        for job_id in ("premarket_login", "market_open", "squareoff", "logout"):
            off = jobs[job_id].next_run_time.utcoffset()
            assert off == timedelta(hours=5, minutes=30), f"{job_id} fires at offset {off}, want IST"
        # Square-off is 15:15 IST specifically (the safety net must land inside the session).
        assert jobs["squareoff"].next_run_time.hour == settings.squareoff_time.hour
        assert jobs["squareoff"].next_run_time.minute == settings.squareoff_time.minute
    finally:
        sched.shutdown()


# --- should_hard_recover (feed-death escalation) --------------------------------------


def test_hard_recover_false_when_feed_healthy():
    # stale_since is None while ticks are flowing -> never escalate.
    assert should_hard_recover(None, 10_000.0, 120) is False


def test_hard_recover_false_before_threshold():
    assert should_hard_recover(1_000.0, 1_000.0 + 119, 120) is False


def test_hard_recover_true_at_threshold():
    assert should_hard_recover(1_000.0, 1_000.0 + 120, 120) is True


def test_hard_recover_true_past_threshold():
    assert should_hard_recover(1_000.0, 1_000.0 + 300, 120) is True
