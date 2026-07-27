from datetime import date, datetime, timedelta
from decimal import Decimal

from freezegun import freeze_time

from algo_trading.config.settings import get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.models import Candle
from algo_trading.persistence.repositories import Repository


def _seed(repo, tf, n):
    day = date(2025, 1, 15)
    for i in range(n):
        start = datetime(2025, 1, 15, 9, 15) + timedelta(minutes=tf * i)
        repo.upsert_nifty_candle("NIFTY", tf, Candle(
            symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=tf),
            open=Decimal(100 + i), high=Decimal(101 + i), low=Decimal(99 + i),
            close=Decimal(100 + i), volume=Decimal(1)), trading_day=day)


@freeze_time("2025-01-15")
def test_indicator_panel_returns_both_timeframes(engine, monkeypatch):
    repo = Repository(engine)
    _seed(repo, 5, 60)
    _seed(repo, 15, 60)
    settings = get_settings(reload=True)
    bridge = StateBridge(settings)
    monkeypatch.setattr(bridge, "_repo", repo)
    panel = bridge.indicator_panel()
    assert set(panel.timeframes.keys()) == {5, 15}
    assert panel.timeframes[5]["composite"] in ("bullish", "bearish", "neutral")
