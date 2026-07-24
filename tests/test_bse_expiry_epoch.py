"""BSE (SENSEX) scrip-master expiries are Unix-epoch seconds; NSE uses the NNF (1980) epoch.

Regression for the SENSEX greeks bug: a BSE weekly parsed against the NNF epoch landed ~10 years
in the future (T off by a decade -> ~1% IV). The parser must pick the epoch by segment.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from algo_trading.domain.enums import ExchangeSegment, Underlying
from algo_trading.instruments.scrip_master import (
    _UNIX_EPOCH,
    ScripMaster,
    default_expiry_parser,
)

# The exact raw value observed corrupted on production for SENSEX: a Unix timestamp for 2026-07-30.
PROD_BSE_RAW = 1_785_369_600
# A NNF-epoch (seconds since 1980-01-01) value for a NIFTY weekly on 2026-07-28.
NSE_NNF_RAW = 1_469_664_000


def _df(symbol_prefix: str, underlying_name: str, raw_expiry: int) -> pd.DataFrame:
    rows = []
    for k in (23400, 23450, 23500):
        for ot in ("CE", "PE"):
            rows.append({
                "pTrdSymbol": f"{symbol_prefix}{k}{ot}", "pSymbol": f"{k}{ot}",
                "pSymbolName": underlying_name, "pExpiryDate": raw_expiry,
                "dStrikePrice": k, "pOptionType": ot, "lLotSize": 20,
            })
    return pd.DataFrame(rows)


def test_bse_expiry_uses_unix_epoch():
    sm = ScripMaster.from_dataframe(_df("SENSEX", "SENSEX", PROD_BSE_RAW), ExchangeSegment.BSE_FO)
    assert sm.expiries(Underlying.SENSEX) == [date(2026, 7, 30)]


def test_nse_expiry_uses_nnf_epoch():
    sm = ScripMaster.from_dataframe(_df("NIFTY", "NIFTY", NSE_NNF_RAW), ExchangeSegment.NSE_FO)
    assert sm.expiries(Underlying.NIFTY) == [date(2026, 7, 28)]


def test_parser_epoch_base_regression():
    # Under the (wrong) NNF default the prod value lands 10 years out; the Unix base fixes it.
    assert default_expiry_parser(PROD_BSE_RAW) == date(2036, 7, 29)                  # the old bug
    assert default_expiry_parser(PROD_BSE_RAW, epoch=_UNIX_EPOCH) == date(2026, 7, 30)  # the fix


def test_string_expiry_still_parses():
    assert default_expiry_parser("2026-07-30") == date(2026, 7, 30)
    assert default_expiry_parser("30-Jul-2026") == date(2026, 7, 30)
