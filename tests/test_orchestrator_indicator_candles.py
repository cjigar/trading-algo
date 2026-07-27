from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from algo_trading.domain.enums import ExchangeSegment
from algo_trading.domain.models import Tick

# Reuse the orchestrator test harness helpers.
from tests.test_orchestrator import INDEX_TOKEN, _build

IST = ZoneInfo("Asia/Kolkata")


def _tick(price, ts):
    return Tick(instrument_token=INDEX_TOKEN, exchange_segment=ExchangeSegment.NSE_FO,
               ltp=Decimal(price), timestamp=ts, is_index=True)


@freeze_time("2025-01-15")
def test_closed_5m_candle_is_persisted(engine):
    orch, _sm = _build(engine)
    base = datetime(2025, 1, 15, 9, 15, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    # Two ticks in the 09:15 bucket, then one in 09:20 -> closes the 09:15 5m candle.
    orch.publish_tick(_tick("100", base))
    orch.publish_tick(_tick("110", base + timedelta(minutes=1)))
    orch.publish_tick(_tick("120", base + timedelta(minutes=6)))
    got = orch.repo.nifty_candles("NIFTY", 5, lookback_days=5)
    assert len(got) == 1
    assert got[0].high == Decimal(110)
