from algo_trading.config.settings import Settings, get_settings


def test_indicator_defaults():
    s = get_settings(reload=True)
    assert s.indicators_enabled is True
    assert s.indicator_timeframes == [5, 15]
    assert s.orb_minutes == 30
    assert s.nifty_candle_retention_days == 7


def test_indicator_timeframes_parse_csv():
    s = Settings(indicator_timeframes="5,15,60")
    assert s.indicator_timeframes == [5, 15, 60]
