from datetime import UTC, date, datetime

from algo_trading.entrypoints.run_algo import compute_purge_date


def test_compute_purge_date_is_ist_calendar_date():
    # 2026-07-22 20:00 UTC == 2026-07-23 01:30 IST -> IST date is the 23rd.
    assert compute_purge_date(datetime(2026, 7, 22, 20, 0, tzinfo=UTC)) == date(2026, 7, 23)


def test_compute_purge_date_before_ist_midnight():
    # 2026-07-22 17:00 UTC == 2026-07-22 22:30 IST -> still the 22nd.
    assert compute_purge_date(datetime(2026, 7, 22, 17, 0, tzinfo=UTC)) == date(2026, 7, 22)
