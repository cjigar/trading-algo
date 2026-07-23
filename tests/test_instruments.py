"""Scrip-master parsing and weekly-option resolution tests (in-memory, no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.domain.enums import ExchangeSegment, OptionType, StrikeSelection, Underlying
from algo_trading.domain.models import Instrument
from algo_trading.instruments.option_resolver import OptionResolutionError, WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster, ScripMasterError


def _build_nifty_chain(expiries: list[date], strikes: list[int]) -> ScripMaster:
    instruments: list[Instrument] = []
    for expiry in expiries:
        for strike in strikes:
            for ot in (OptionType.CE, OptionType.PE):
                instruments.append(
                    Instrument(
                        underlying=Underlying.NIFTY,
                        exchange_segment=ExchangeSegment.NSE_FO,
                        trading_symbol=f"NIFTY{expiry:%y%b%d}".upper() + f"{strike}{ot.value}",
                        instrument_token=f"{strike}-{ot.value}-{expiry:%Y%m%d}",
                        expiry=expiry,
                        strike=Decimal(strike),
                        option_type=ot,
                        lot_size=75,
                    )
                )
    return ScripMaster(instruments)


def test_from_dataframe_parses_options_and_skips_futures():
    df = pd.DataFrame(
        [
            {"pTrdSymbol": "NIFTY25JAN23000CE", "pSymbol": "111", "pSymbolName": "NIFTY",
             "pExpiryDate": "2025-01-30", "dStrikePrice": 23000, "pOptionType": "CE", "lLotSize": 75},
            {"pTrdSymbol": "NIFTY25JAN23000PE", "pSymbol": "112", "pSymbolName": "NIFTY",
             "pExpiryDate": "2025-01-30", "dStrikePrice": 23000, "pOptionType": "PE", "lLotSize": 75},
            {"pTrdSymbol": "NIFTY25JANFUT", "pSymbol": "113", "pSymbolName": "NIFTY",
             "pExpiryDate": "2025-01-30", "dStrikePrice": 0, "pOptionType": "XX", "lLotSize": 75},
        ]
    )
    sm = ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)
    assert len(sm) == 2  # future row skipped
    assert sm.find(Underlying.NIFTY, date(2025, 1, 30), Decimal(23000), OptionType.CE) is not None


def test_from_dataframe_handles_messy_kotak_columns():
    # Real Kotak scrip CSV headers have trailing spaces/semicolons (e.g. 'dStrikePrice;').
    df = pd.DataFrame(
        [
            {"pTrdSymbol": "NIFTY25JAN23000CE", "pSymbol": "111", "pSymbolName": "NIFTY",
             "pExpiryDate": "2025-01-30", "dStrikePrice;": 23000, "pOptionType": "CE",
             "lLotSize": 75},
        ]
    )
    sm = ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)
    assert len(sm) == 1
    inst = sm.find(Underlying.NIFTY, date(2025, 1, 30), Decimal(23000), OptionType.CE)
    assert inst is not None and inst.strike == Decimal(23000)


def test_from_dataframe_fails_closed_when_no_options():
    df = pd.DataFrame(
        [{"pTrdSymbol": "NIFTYFUT", "pExpiryDate": "2025-01-30", "dStrikePrice": 0,
          "pOptionType": "XX", "lLotSize": 75}]
    )
    with pytest.raises(ScripMasterError):
        ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)


def _future_row(symbol, token, expiry, name="NIFTY", kind="FUTIDX"):
    return {"pTrdSymbol": symbol, "pSymbol": token, "pSymbolName": name,
            "pExpiryDate": expiry, "dStrikePrice": 0, "pOptionType": "XX",
            "pInstType": kind, "lLotSize": 75}


def _option_row(symbol="NIFTY25JAN23000CE", token="1", name="NIFTY", opt="CE"):
    return {"pTrdSymbol": symbol, "pSymbol": token, "pSymbolName": name,
            "pExpiryDate": "2025-01-30", "dStrikePrice": 23000, "pOptionType": opt,
            "pInstType": "OPTIDX", "lLotSize": 75}


def test_from_dataframe_captures_futures_alongside_options():
    df = pd.DataFrame([
        _option_row(),
        _future_row("NIFTY25JANFUT", "113", "2025-01-30"),
    ])
    sm = ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)
    assert len(sm) == 1  # options only in the tradeable table
    assert len(sm.futures) == 1
    fut = sm.futures[0]
    assert fut.underlying is Underlying.NIFTY and fut.expiry == date(2025, 1, 30)
    assert fut.instrument_token == "113"


def test_near_month_future_picks_nearest_non_expired_and_rolls():
    df = pd.DataFrame([
        _option_row(),
        _future_row("NIFTY25JANFUT", "1", "2025-01-30"),
        _future_row("NIFTY25FEBFUT", "2", "2025-02-27"),
        _future_row("NIFTY25MARFUT", "3", "2025-03-27"),
    ])
    sm = ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)
    # Before the Jan expiry, the front month is Jan.
    assert sm.near_month_future(Underlying.NIFTY, today=date(2025, 1, 15)).instrument_token == "1"
    # On expiry day the contract is still valid.
    assert sm.near_month_future(Underlying.NIFTY, today=date(2025, 1, 30)).instrument_token == "1"
    # After Jan expires it rolls to Feb.
    assert sm.near_month_future(Underlying.NIFTY, today=date(2025, 2, 1)).instrument_token == "2"
    # Past every expiry -> nothing.
    assert sm.near_month_future(Underlying.NIFTY, today=date(2025, 12, 1)) is None


def test_near_month_future_none_when_no_futures_parsed():
    sm = ScripMaster.from_dataframe(pd.DataFrame([_option_row()]), ExchangeSegment.NSE_FO)
    assert sm.near_month_future(Underlying.NIFTY) is None


def test_exchange_segments_for_all_three_underlyings():
    # NIFTY and BANKNIFTY are NSE; only SENSEX is BSE.
    assert ExchangeSegment.for_underlying(Underlying.NIFTY) is ExchangeSegment.NSE_FO
    assert ExchangeSegment.for_underlying(Underlying.BANKNIFTY) is ExchangeSegment.NSE_FO
    assert ExchangeSegment.for_underlying(Underlying.SENSEX) is ExchangeSegment.BSE_FO
    assert ExchangeSegment.index_for_underlying(Underlying.NIFTY) is ExchangeSegment.NSE_CM
    assert ExchangeSegment.index_for_underlying(Underlying.BANKNIFTY) is ExchangeSegment.NSE_CM
    assert ExchangeSegment.index_for_underlying(Underlying.SENSEX) is ExchangeSegment.BSE_CM


def test_index_token_for_resolves_each_underlying():
    from algo_trading.config.settings import Settings

    s = Settings(nifty_index_token="N1", banknifty_index_token="B1", sensex_index_token="S1")
    assert s.index_token_for(Underlying.NIFTY) == "N1"
    assert s.index_token_for(Underlying.BANKNIFTY) == "B1"
    assert s.index_token_for(Underlying.SENSEX) == "S1"


def test_banknifty_rows_classify_as_banknifty_not_nifty():
    df = pd.DataFrame([
        _option_row(symbol="BANKNIFTY25JAN51000CE", name="BANKNIFTY"),
        _future_row("BANKNIFTY25JANFUT", "9", "2025-01-30", name="BANKNIFTY"),
        _future_row("NIFTY25JANFUT", "10", "2025-01-30", name="NIFTY"),
    ])
    sm = ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)
    by_underlying = {f.underlying: f for f in sm.futures}
    assert set(by_underlying) == {Underlying.BANKNIFTY, Underlying.NIFTY}
    assert by_underlying[Underlying.BANKNIFTY].instrument_token == "9"
    # The BankNifty option must not be misfiled under NIFTY (substring trap).
    assert sm.for_underlying(Underlying.BANKNIFTY) and not sm.for_underlying(Underlying.NIFTY)


def test_from_dataframe_missing_columns_raises():
    df = pd.DataFrame([{"foo": 1, "bar": 2}])
    with pytest.raises(ScripMasterError):
        ScripMaster.from_dataframe(df, ExchangeSegment.NSE_FO)


def test_resolve_atm_ce():
    sm = _build_nifty_chain([date(2025, 1, 30)], list(range(22800, 23300, 50)))
    resolver = WeeklyOptionResolver(sm)
    inst = resolver.resolve(
        Underlying.NIFTY, Decimal("23010"), OptionType.CE, StrikeSelection.ATM,
        today=date(2025, 1, 27),
    )
    assert inst.strike == Decimal(23000)  # 23010 rounds to 23000
    assert inst.option_type is OptionType.CE


def test_resolve_otm1_ce_is_higher_strike():
    sm = _build_nifty_chain([date(2025, 1, 30)], list(range(22800, 23300, 50)))
    resolver = WeeklyOptionResolver(sm)
    inst = resolver.resolve(
        Underlying.NIFTY, Decimal("23000"), OptionType.CE, StrikeSelection.OTM1,
        today=date(2025, 1, 27),
    )
    assert inst.strike == Decimal(23050)  # one step OTM above ATM for a call


def test_resolve_otm1_pe_is_lower_strike():
    sm = _build_nifty_chain([date(2025, 1, 30)], list(range(22800, 23300, 50)))
    resolver = WeeklyOptionResolver(sm)
    inst = resolver.resolve(
        Underlying.NIFTY, Decimal("23000"), OptionType.PE, StrikeSelection.OTM1,
        today=date(2025, 1, 27),
    )
    assert inst.strike == Decimal(22950)  # one step OTM below ATM for a put


def test_current_week_expiry_picks_nearest_future():
    sm = _build_nifty_chain(
        [date(2025, 1, 23), date(2025, 1, 30), date(2025, 2, 6)], [23000]
    )
    resolver = WeeklyOptionResolver(sm)
    # On the 27th, the 23rd is past -> current week is the 30th.
    assert resolver.current_week_expiry(Underlying.NIFTY, today=date(2025, 1, 27)) == date(2025, 1, 30)


def test_resolve_no_contract_raises():
    sm = _build_nifty_chain([date(2025, 1, 23)], [23000])  # only a past expiry
    resolver = WeeklyOptionResolver(sm)
    with pytest.raises(OptionResolutionError):
        resolver.resolve(
            Underlying.NIFTY, Decimal("23000"), OptionType.CE, today=date(2025, 1, 27)
        )
