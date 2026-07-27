"""Unit tests for ``normalize_position_row`` — the pure broker-position normalizer used by the
broker-sourced flatten. Covers the Kotak shape, the PaperBroker shape, net-zero, and bad input."""

from __future__ import annotations

from algo_trading.broker.report_normalize import normalize_position_row


def test_normalizes_kotak_long_position():
    row = normalize_position_row(
        {"trdSym": "NIFTY23200CE", "tok": "23200-CE", "exSeg": "nse_fo",
         "flBuyQty": "75", "flSellQty": "0"}
    )
    assert row == {
        "instrument_token": "23200-CE",
        "trading_symbol": "NIFTY23200CE",
        "exchange_segment": "nse_fo",
        "underlying": "NIFTY",
        "option_type": "CE",
        "net_qty": 75,
    }


def test_normalizes_kotak_short_position_as_negative_net():
    row = normalize_position_row(
        {"trdSym": "SENSEX80000PE", "tok": "t1", "exSeg": "bse_fo",
         "flBuyQty": "0", "flSellQty": "20"}
    )
    assert row["net_qty"] == -20
    assert row["underlying"] == "SENSEX"
    assert row["option_type"] == "PE"
    assert row["exchange_segment"] == "bse_fo"


def test_normalizes_paperbroker_shape_via_explicit_netqty():
    # PaperBroker positions carry a signed ``netQty`` and no token/segment.
    row = normalize_position_row({"trading_symbol": "NIFTY23200CE", "netQty": 75, "avg": "100"})
    assert row["net_qty"] == 75
    assert row["trading_symbol"] == "NIFTY23200CE"
    assert row["instrument_token"] == ""          # no token in the paper shape
    assert row["exchange_segment"] == "nse_fo"     # default when absent


def test_explicit_netqty_takes_precedence_over_buy_sell():
    row = normalize_position_row(
        {"trdSym": "X", "netQty": -5, "flBuyQty": "10", "flSellQty": "0"}
    )
    assert row["net_qty"] == -5


def test_net_zero_position_is_returned_not_dropped():
    row = normalize_position_row({"trdSym": "X", "flBuyQty": "75", "flSellQty": "75"})
    assert row is not None
    assert row["net_qty"] == 0


def test_missing_trading_symbol_returns_none():
    assert normalize_position_row({"tok": "t1", "flBuyQty": "75"}) is None
