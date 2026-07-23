"""Option-chain manager: ATM + hysteresis, window diff, chain state, per-option VWAP."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment
from algo_trading.domain.models import Tick
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster


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


def _settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "chain_feed_window", 0)  # 0 -> capture window follows strike_window (ignore .env)
    object.__setattr__(s, "strike_step", Decimal("50"))
    return s


def _idx(price):
    return Tick(instrument_token="NIFTY-IDX", exchange_segment=ExchangeSegment.NSE_CM,
               ltp=Decimal(price), timestamp=datetime(2025, 1, 15, tzinfo=UTC), is_index=True)


def _opt(token, price, oi=1000, vol=None):
    return Tick(instrument_token=token, exchange_segment=ExchangeSegment.NSE_FO,
               ltp=Decimal(price), timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=oi, volume=vol)


def test_atm_resolution_and_subscription(resolver):
    subs = []
    m = OptionChainManager(_settings(), resolver, subscribe=lambda t, s: subs.append(t))
    m.on_index_tick(_idx("23062"))
    assert m.atm == Decimal("23050")
    # ATM ±5 CE+PE = 22 contracts subscribed
    assert len(subs) == 22


def test_atm_hysteresis_prevents_flapping(resolver):
    m = OptionChainManager(_settings(), resolver)
    m.on_index_tick(_idx("23050"))
    assert m.atm == Decimal("23050")
    # spot drifts to 23026 — just past midpoint (23025) but within hysteresis (10) -> no change
    m.on_index_tick(_idx("23026"))
    assert m.atm == Decimal("23050")
    # spot moves decisively to 23040 -> beyond midpoint+hysteresis -> ATM shifts to 23050? no, still 23050
    m.on_index_tick(_idx("23090"))  # nearest is 23100, |23090-23050|=40 > 25+10 -> shift
    assert m.atm == Decimal("23100")


def test_window_shift_subscribes_delta(resolver):
    subs = []
    m = OptionChainManager(_settings(), resolver, subscribe=lambda t, s: subs.append(t))
    m.on_index_tick(_idx("23050"))
    first = len(subs)
    assert first == 22
    m.on_index_tick(_idx("23150"))  # ATM -> 23150, window shifts up by 2 strikes
    # only the newly-included strikes are subscribed (delta), not the whole window again
    assert len(subs) - first < 22
    assert len(subs) - first > 0


def test_chain_state_and_oi_aggregation(resolver):
    m = OptionChainManager(_settings(), resolver)
    m.on_index_tick(_idx("23050"))
    # feed CE OI high, PE OI low across the full ATM ±5 window (22800..23300 = 11 strikes)
    for k in range(22800, 23350, 50):
        m.on_option_tick(_opt(f"{k}CE", "100", oi=5000))
        m.on_option_tick(_opt(f"{k}PE", "100", oi=1000))
    ce, pe = m.aggregate_oi()
    assert ce > pe
    assert len(m.chain_state()) == 22


def test_per_option_vwap(resolver):
    m = OptionChainManager(_settings(), resolver)
    m.on_index_tick(_idx("23050"))
    tok = "23050CE"
    m.on_option_tick(_opt(tok, "100", vol=100))   # first tick, weight defaults to 1
    m.on_option_tick(_opt(tok, "120", vol=200))   # delta vol = 100 -> weighted
    v = m.vwap_for(tok)
    assert v is not None and Decimal("100") <= v <= Decimal("120")


def test_option_tick_snapshot_includes_vwap(resolver):
    captured = []

    class _Writer:
        def add(self, row):
            captured.append(row)

    m = OptionChainManager(_settings(), resolver, subscribe=lambda t, s: None, snapshot_writer=_Writer())
    m.on_index_tick(_idx("23062"))          # ATM -> 23050, subscribes the ±5 window
    m.on_option_tick(_opt("23050CE", "100", vol=500))  # a tracked ATM contract
    assert captured, "expected a snapshot to be written"
    assert captured[-1]["instrument_token"] == "23050CE"
    assert "vwap" in captured[-1]
    assert captured[-1]["vwap"] is not None   # a tick arrived, so VWAP is set
    assert Decimal(captured[-1]["vwap"]) == Decimal("100")  # single tick -> VWAP == LTP
