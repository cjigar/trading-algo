from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from algo_trading.domain.models import Candle
from algo_trading.persistence.repositories import Repository


def _candle(i, day=date(2025, 1, 15)):
    start = datetime(day.year, day.month, day.day, 9, 15) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(100), high=Decimal(101), low=Decimal(99),
                  close=Decimal(100 + i), volume=Decimal(1))


# upsert_nifty_candle trims on every write using a wall-clock cutoff (utcnow() - keep_days), so the
# test's wall clock must sit near the fixed 2025-01-15 candle dates or the trim step deletes rows
# the same test just wrote. Pinned to match repo convention (see tests/test_orchestrator.py).
@freeze_time("2025-01-15")
def test_upsert_and_read_back(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 5, _candle(1), trading_day=date(2025, 1, 15))
    got = repo.nifty_candles("NIFTY", 5, lookback_days=5)
    assert [c.close for c in got] == [Decimal(100), Decimal(101)]


@freeze_time("2025-01-15")
def test_upsert_is_idempotent_on_start(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))  # same start
    assert len(repo.nifty_candles("NIFTY", 5, lookback_days=5)) == 1


@freeze_time("2025-01-15")
def test_timeframe_isolation(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 15, _candle(0), trading_day=date(2025, 1, 15))
    assert len(repo.nifty_candles("NIFTY", 5, lookback_days=5)) == 1
    assert len(repo.nifty_candles("NIFTY", 15, lookback_days=5)) == 1


@freeze_time("2025-01-15")
def test_candle_start_roundtrips_as_utc_instant(engine):
    # Production candles from CandleBuilder carry IST-aware boundaries. The round-trip must preserve
    # the instant and store naive UTC, so the read-side ORB/session bucketing and the wall-clock
    # retention cutoff are correct regardless of the host/container/DB timezone (finding #3).
    ist = ZoneInfo("Asia/Kolkata")
    start = datetime(2025, 1, 15, 9, 15, tzinfo=ist)  # 09:15 IST market open
    candle = Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                    open=Decimal(100), high=Decimal(101), low=Decimal(99),
                    close=Decimal(100), volume=Decimal(1))
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, candle, trading_day=date(2025, 1, 15))

    got = repo.nifty_candles("NIFTY", 5, lookback_days=5)
    assert len(got) == 1
    # Same instant, expressed back in IST, regardless of the process/DB timezone.
    assert got[0].start.astimezone(ist) == start
    # And it is stored as UTC wall-clock: 09:15 IST == 03:45 UTC.
    assert got[0].start.astimezone(UTC).replace(tzinfo=None) == datetime(2025, 1, 15, 3, 45)
