"""Read-only trade importer: normalization, dedup, and tolerant display."""

from __future__ import annotations

from decimal import Decimal

from algo_trading.persistence.repositories import Repository
from algo_trading.tools.import_trades import import_trades, normalize_trade_row


def _row(**kw):
    base = {
        "pTrdSymbol": "NIFTY2513023000CE", "tok": "40000", "exSeg": "nse_fo",
        "trnsTp": "B", "fldQty": "75", "avgPrc": "101.5", "nOrdNo": "ORD1",
        "flTrdId": "T1", "flDtTm": "30-Jan-2025 10:15:04",
    }
    base.update(kw)
    return base


def test_normalize_parses_option_symbol():
    f = normalize_trade_row(_row())
    assert f is not None
    assert f["trading_symbol"] == "NIFTY2513023000CE"
    assert f["underlying"] == "NIFTY"
    assert f["option_type"] == "CE"
    assert f["strike"] == "0"  # strike not parsed (not displayed)
    assert f["side"] == "B"
    assert f["quantity"] == 75
    assert f["price"] == Decimal("101.5")
    assert f["client_tag"] == "trd-T1"


def test_normalize_handles_sell_and_sensex():
    f = normalize_trade_row(_row(pTrdSymbol="SENSEX2513075000PE", trnsTp="SELL", flTrdId="T2"))
    assert f["underlying"] == "SENSEX"
    assert f["option_type"] == "PE"
    assert f["side"] == "S"


def test_normalize_returns_none_when_missing_core_fields():
    assert normalize_trade_row({"foo": "bar"}) is None


def test_import_is_idempotent_and_displays(repo: Repository):
    rows = [_row(flTrdId="T1"), _row(flTrdId="T2", trnsTp="S", avgPrc="130")]
    summary = import_trades(repo, rows)
    assert summary == {"imported": 2, "skipped_duplicate": 0, "unparsed": 0, "total": 2}

    # re-running imports nothing new (dedup by fill id)
    summary2 = import_trades(repo, rows)
    assert summary2["imported"] == 0 and summary2["skipped_duplicate"] == 2

    # the dashboard read path (trades_for_day) shows them and does not crash on parsed enums
    trades = repo.trades_for_day()
    assert len(trades) == 2
    symbols = {t.instrument.trading_symbol for t in trades}
    assert "NIFTY2513023000CE" in symbols


def test_tolerant_display_for_unmappable_symbol(repo: Repository):
    # a non-NIFTY/SENSEX symbol -> underlying/option_type stored as 'NA'; must still display
    rows = [_row(pTrdSymbol="RELIANCE25JANFUT", flTrdId="T9")]
    import_trades(repo, rows)
    trades = repo.trades_for_day()
    assert len(trades) == 1
    assert trades[0].instrument.trading_symbol == "RELIANCE25JANFUT"  # no crash on NA enums


def test_unparsed_rows_counted(repo: Repository):
    summary = import_trades(repo, [{"garbage": 1}])
    assert summary["unparsed"] == 1 and summary["imported"] == 0
