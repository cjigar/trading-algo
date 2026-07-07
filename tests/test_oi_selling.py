"""OI selling strategy: side selection, 3-OTM strike, day-of-week gating."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest
from freezegun import freeze_time

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment, OptionType, Side
from algo_trading.domain.models import Tick
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.strategy.oi_selling import OiSellingStrategy


@pytest.fixture()
def resolver():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}", "pSymbolName": "NIFTY",
                         "pExpiryDate": "2099-01-30", "dStrikePrice": k, "pOptionType": ot,
                         "lLotSize": 75})
    sm = ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)
    return WeeklyOptionResolver(sm)


def _settings(**over):
    s = get_settings(reload=True)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "otm_strikes", 3)
    object.__setattr__(s, "allowed_weekdays", [4, 0, 1])  # Fri, Mon, Tue
    object.__setattr__(s, "market_holidays", [])
    for k, v in over.items():
        object.__setattr__(s, k, v)
    return s


def _build(resolver, settings, ce_oi, pe_oi):
    m = OptionChainManager(settings, resolver)
    m.on_index_tick(Tick(instrument_token="IDX", exchange_segment=ExchangeSegment.NSE_CM,
                         ltp=Decimal("23050"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), is_index=True))
    for k in range(22800, 23350, 50):
        m.on_option_tick(Tick(instrument_token=f"{k}CE", exchange_segment=ExchangeSegment.NSE_FO,
                              ltp=Decimal("100"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=ce_oi))
        m.on_option_tick(Tick(instrument_token=f"{k}PE", exchange_segment=ExchangeSegment.NSE_FO,
                              ltp=Decimal("100"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=pe_oi))
    return m


# 2025-01-17 is a Friday (allowed); 2025-01-15 is a Wednesday (disallowed)
FRIDAY = datetime(2025, 1, 17, 10, 0, tzinfo=UTC)
WEDNESDAY = datetime(2025, 1, 15, 10, 0, tzinfo=UTC)


def test_ce_oi_dominant_sells_ce_3_otm(resolver):
    s = _settings()
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=5000, pe_oi=1000))
    sigs = strat.evaluate(FRIDAY)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side is Side.SELL and sig.option_type is OptionType.CE
    assert sig.target_strike == Decimal("23200")  # ATM 23050 + 3*50


def test_pe_oi_dominant_sells_pe_3_otm(resolver):
    s = _settings()
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=1000, pe_oi=5000))
    sigs = strat.evaluate(FRIDAY)
    assert sigs[0].option_type is OptionType.PE
    assert sigs[0].target_strike == Decimal("22900")  # ATM 23050 - 3*50


def test_tie_no_signal(resolver):
    s = _settings()
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=3000, pe_oi=3000))
    assert strat.evaluate(FRIDAY) == []


def test_disallowed_weekday_no_signal(resolver):
    s = _settings()
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=5000, pe_oi=1000))
    assert strat.evaluate(WEDNESDAY) == []  # Wednesday not in Fri/Mon/Tue


def test_holiday_no_signal(resolver):
    s = _settings(market_holidays=["2025-01-17"])  # mark the Friday as a holiday
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=5000, pe_oi=1000))
    assert strat.evaluate(FRIDAY) == []


def test_translator_uses_target_strike(resolver):
    from algo_trading.execution.signal_translator import SignalTranslator
    s = _settings()
    strat = OiSellingStrategy(s, _build(resolver, s, ce_oi=5000, pe_oi=1000))
    sig = strat.evaluate(FRIDAY)[0]
    with freeze_time("2099-01-27"):
        req = SignalTranslator(s, resolver).translate(sig)
    assert req.instrument.strike == Decimal("23200")
    assert req.side is Side.SELL
