"""OptionChainManager computes per-strike greeks from the parity forward."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment
from algo_trading.domain.models import Tick
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster

# WeeklyOptionResolver.current_week_expiry() defaults to the real date.today() (it isn't given the
# tick timestamp), so the fixture expiry must always be in the future relative to wall-clock time
# -- mirrors the fixed far-future expiry used in tests/test_option_chain.py.
_EXPIRY = date.today() + timedelta(days=14)
_TS = datetime.combine(_EXPIRY - timedelta(days=7), datetime.min.time().replace(hour=4), tzinfo=UTC)


@pytest.fixture()
def resolver():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}",
                         "pSymbolName": "NIFTY", "pExpiryDate": _EXPIRY.isoformat(),
                         "dStrikePrice": k, "pOptionType": ot, "lLotSize": 75})
    sm = ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)
    return WeeklyOptionResolver(sm)


def _settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "risk_free_rate", Decimal("0.065"))
    return s


def _captured(writer_rows, token):
    return next(r for r in reversed(writer_rows) if r["instrument_token"] == token)


def test_greeks_for_populated_and_written(resolver):
    written: list[dict] = []
    m = OptionChainManager(_settings(), resolver,
                           snapshot_writer=type("W", (), {"add": lambda self, r: written.append(r)})())
    ts = _TS  # 7 days before expiry
    m.on_index_tick(Tick(instrument_token="IDX", exchange_segment=ExchangeSegment.NSE_CM,
                         ltp=Decimal("23000"), timestamp=ts, is_index=True))
    for token, ltp in [("23000CE", "120"), ("23000PE", "115"), ("23100CE", "70")]:
        m.on_option_tick(Tick(instrument_token=token, exchange_segment=ExchangeSegment.NSE_FO,
                              ltp=Decimal(ltp), timestamp=ts, oi=1000))

    g = m.greeks_for("23100CE")
    assert g is not None
    assert 0.0 < g.iv < 3.0
    assert 0.0 < g.delta < 1.0
    assert g.gamma > 0 and g.vega > 0 and g.theta < 0

    row = _captured(written, "23100CE")
    assert row["iv"] is not None and float(row["iv"]) == pytest.approx(g.iv, rel=1e-9)
    assert row["delta"] is not None


def test_greeks_none_before_forward_available(resolver):
    m = OptionChainManager(_settings(), resolver)
    ts = _TS
    m.on_index_tick(Tick(instrument_token="IDX", exchange_segment=ExchangeSegment.NSE_CM,
                         ltp=Decimal("23000"), timestamp=ts, is_index=True))
    m.on_option_tick(Tick(instrument_token="23100CE", exchange_segment=ExchangeSegment.NSE_FO,
                          ltp=Decimal("70"), timestamp=ts, oi=1000))  # only CE leg -> no forward
    assert m.greeks_for("23100CE") is None
