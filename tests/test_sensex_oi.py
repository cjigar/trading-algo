"""SENSEX OI strategy (Wed/Thu, step 100) and multi-underlying orchestrator routing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import ExchangeSegment, OptionType, TradingMode, Underlying
from algo_trading.domain.models import Tick
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository
from algo_trading.strategy.oi_selling import OiSellingStrategy

WED = datetime(2025, 1, 15, 10, 0, tzinfo=UTC)   # Wednesday
THU = datetime(2025, 1, 16, 10, 0, tzinfo=UTC)   # Thursday
FRI = datetime(2025, 1, 17, 10, 0, tzinfo=UTC)   # Friday
NIFTY_IDX, SENSEX_IDX = "NIFTY-IDX", "SENSEX-IDX"


def _rows(name, lo, hi, step):
    rows = []
    for k in range(lo, hi, step):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"{name}{k}{ot}", "pSymbol": f"{name}{k}{ot}",
                         "pSymbolName": name, "pExpiryDate": "2099-01-30", "dStrikePrice": k,
                         "pOptionType": ot, "lLotSize": 20 if name == "SENSEX" else 75})
    return rows


@pytest.fixture()
def combined_scrip():
    nifty = ScripMaster.from_dataframe(pd.DataFrame(_rows("NIFTY", 22000, 24000, 50)), ExchangeSegment.NSE_FO)
    sensex = ScripMaster.from_dataframe(pd.DataFrame(_rows("SENSEX", 74000, 78000, 100)), ExchangeSegment.BSE_FO)
    return ScripMaster([*nifty.instruments, *sensex.instruments])


def _settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strategy", "oi_selling")
    object.__setattr__(s, "mode", TradingMode.PAPER)
    object.__setattr__(s, "oi_underlyings", [Underlying.NIFTY, Underlying.SENSEX])
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "sensex_strike_step", Decimal("100"))
    object.__setattr__(s, "otm_strikes", 3)
    object.__setattr__(s, "allowed_weekdays", [4, 0, 1])   # NIFTY Fri/Mon/Tue
    object.__setattr__(s, "sensex_weekdays", [2, 3])        # SENSEX Wed/Thu
    object.__setattr__(s, "market_holidays", [])
    object.__setattr__(s, "max_positions", 2)
    object.__setattr__(s, "snapshot_min_interval_seconds", 0)
    return s


def _idx(token, price):
    return Tick(instrument_token=token, exchange_segment=ExchangeSegment.BSE_CM, ltp=Decimal(price),
                timestamp=datetime(2025, 1, 15, tzinfo=UTC), is_index=True)


def _opt(token, price, oi):
    return Tick(instrument_token=token, exchange_segment=ExchangeSegment.BSE_FO, ltp=Decimal(price),
                timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=oi, volume=100)


# -- Strategy-level: SENSEX trades Wed/Thu, step 100 ----------------------------------


def test_sensex_shorts_ce_wed_step_100(combined_scrip):
    s = _settings()
    chain = OptionChainManager(s, WeeklyOptionResolver(combined_scrip), underlying=Underlying.SENSEX)
    chain.on_index_tick(_idx(SENSEX_IDX, "75000"))
    for k in range(74500, 75600, 100):  # ATM ±5 for SENSEX
        chain.on_option_tick(_opt(f"SENSEX{k}CE", "100", 5000))
        chain.on_option_tick(_opt(f"SENSEX{k}PE", "100", 1000))
    strat = OiSellingStrategy(s, chain, underlying=Underlying.SENSEX)
    sigs = strat.evaluate(WED)
    assert len(sigs) == 1
    assert sigs[0].option_type is OptionType.CE
    assert sigs[0].target_strike == Decimal("75300")  # ATM 75000 + 3*100
    assert strat.evaluate(FRI) == []  # SENSEX not on Friday


def test_sensex_no_trade_on_nifty_days(combined_scrip):
    s = _settings()
    chain = OptionChainManager(s, WeeklyOptionResolver(combined_scrip), underlying=Underlying.SENSEX)
    chain.on_index_tick(_idx(SENSEX_IDX, "75000"))
    for k in range(74500, 75600, 100):
        chain.on_option_tick(_opt(f"SENSEX{k}CE", "100", 5000))
        chain.on_option_tick(_opt(f"SENSEX{k}PE", "100", 1000))
    strat = OiSellingStrategy(s, chain, underlying=Underlying.SENSEX)
    assert strat.evaluate(FRI) == []  # Friday is a NIFTY day
    assert strat.evaluate(THU) != []  # Thursday is a SENSEX day


# -- Orchestrator: only the day's underlying trades -----------------------------------


def _orch(combined_scrip, engine):
    orch = Orchestrator(_settings(), scrip_master=combined_scrip, broker=PaperBroker(),
                        repo=Repository(engine))
    orch.register_index_token(NIFTY_IDX, Underlying.NIFTY)
    orch.register_index_token(SENSEX_IDX, Underlying.SENSEX)
    orch.start_session()
    return orch


def _feed_both(orch, nifty_ce_oi, sensex_ce_oi):
    orch.publish_tick(Tick(instrument_token=NIFTY_IDX, exchange_segment=ExchangeSegment.NSE_CM,
                           ltp=Decimal("23050"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), is_index=True))
    orch.publish_tick(_idx(SENSEX_IDX, "75000"))
    for k in range(22800, 23350, 50):
        orch.publish_tick(Tick(instrument_token=f"NIFTY{k}CE", exchange_segment=ExchangeSegment.NSE_FO,
                               ltp=Decimal("100"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=nifty_ce_oi, volume=100))
        orch.publish_tick(Tick(instrument_token=f"NIFTY{k}PE", exchange_segment=ExchangeSegment.NSE_FO,
                               ltp=Decimal("100"), timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=1000, volume=100))
    for k in range(74500, 75600, 100):
        orch.publish_tick(_opt(f"SENSEX{k}CE", "100", sensex_ce_oi))
        orch.publish_tick(_opt(f"SENSEX{k}PE", "100", 1000))


def test_orchestrator_sensex_only_on_wednesday(combined_scrip, engine):
    orch = _orch(combined_scrip, engine)
    _feed_both(orch, nifty_ce_oi=5000, sensex_ce_oi=5000)
    orch.evaluate_oi(now=WED)  # Wednesday: SENSEX day only
    assert orch.positions.open_position_count() == 1
    assert orch.positions.position_for("SENSEX75300CE") is not None  # SENSEX CE 3-OTM
    assert orch.positions.position_for("NIFTY23200CE") is None       # NIFTY does NOT trade Wed


def test_orchestrator_nifty_only_on_friday(combined_scrip, engine):
    orch = _orch(combined_scrip, engine)
    _feed_both(orch, nifty_ce_oi=5000, sensex_ce_oi=5000)
    orch.evaluate_oi(now=FRI)  # Friday: NIFTY day only
    assert orch.positions.position_for("NIFTY23200CE") is not None
    assert orch.positions.position_for("SENSEX75300CE") is None
    assert orch.positions.open_position_count() == 1


def test_orchestrator_captures_both_chains(combined_scrip, engine):
    orch = _orch(combined_scrip, engine)
    _feed_both(orch, nifty_ce_oi=5000, sensex_ce_oi=5000)
    orch.flush_snapshots()
    state = orch.repo.latest_chain_state()
    unders = {r.underlying for r in state}
    assert unders == {"NIFTY", "SENSEX"}  # both chains persisted
