from decimal import Decimal

from algo_trading.config.settings import get_settings


def test_risk_free_rate_default():
    s = get_settings(reload=True)
    assert s.risk_free_rate == Decimal("0.065")
