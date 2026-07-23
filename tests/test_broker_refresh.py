"""Live broker-account poller: refresh_broker_account persists positions/orders/trades, is
fail-safe per endpoint, and the broker-trades store + schema round-trip cleanly."""

from __future__ import annotations

import pandas as pd
import pytest

from algo_trading.config.settings import EDITABLE_FIELDS, get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import ExchangeSegment, TradingMode
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository

# Raw Kotak-style report rows (camelCase, as the SDK returns them).
RAW_POSITIONS = [
    {"trdSym": "SENSEX24500CE", "netQty": "-20", "flBuyQty": "0", "flSellQty": "20",
     "buyAmt": "0", "sellAmt": "16000", "prod": "NRML"},
]
RAW_ORDERS = [
    {"nOrdNo": "B1", "trdSym": "SENSEX24500CE", "trnsTp": "S", "qty": "20", "fldQty": "20",
     "avgPrc": "80", "ordSt": "complete"},
    {"nOrdNo": "B2", "trdSym": "NIFTY23000PE", "trnsTp": "B", "qty": "75", "fldQty": "0",
     "ordSt": "open"},
]
RAW_TRADES = [
    {"flTrdId": "T1", "nOrdNo": "B1", "pTrdSymbol": "SENSEX24500CE", "trnsTp": "S",
     "fldQty": "20", "avgPrc": "80", "flDtTm": "23-Jul-2026 11:30:00"},
]


class AccountBroker(PaperBroker):
    """A paper broker that also returns canned account reports."""

    def positions(self):
        return RAW_POSITIONS

    def order_report(self):
        return RAW_ORDERS

    def trade_report(self):
        return RAW_TRADES


@pytest.fixture()
def scrip():
    rows = [{"pTrdSymbol": "NIFTY23000PE", "pSymbol": "23000PE", "pSymbolName": "NIFTY",
             "pExpiryDate": "2099-01-30", "dStrikePrice": 23000, "pOptionType": "PE",
             "lLotSize": 75}]
    return ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)


def _orch(scrip, engine, broker):
    s = get_settings(reload=True)
    object.__setattr__(s, "mode", TradingMode.PAPER)
    return Orchestrator(s, scrip_master=scrip, broker=broker, repo=Repository(engine))


def test_refresh_populates_all_three_stores(scrip, engine):
    orch = _orch(scrip, engine, AccountBroker())
    summary = orch.refresh_broker_account()
    assert summary == {"positions": 1, "orders": 2, "trades": 1}

    repo = Repository(engine)
    assert {p["trdSym"] for p in repo.latest_broker_positions()} == {"SENSEX24500CE"}
    assert {o.order_id for o in repo.broker_orders_for_day()} == {"B1", "B2"}
    assert {t["flTrdId"] for t in repo.latest_broker_trades()} == {"T1"}


def test_refresh_is_wipe_and_replace_for_trades(scrip, engine):
    """A later poll with fewer trades overwrites, never appends (point-in-time snapshot)."""
    orch = _orch(scrip, engine, AccountBroker())
    orch.refresh_broker_account()

    class OneLess(AccountBroker):
        def trade_report(self):
            return []

    _orch(scrip, engine, OneLess()).refresh_broker_account()
    assert Repository(engine).latest_broker_trades() == []


def test_refresh_is_fail_safe_per_endpoint(scrip, engine):
    """One failing broker read must not block the others."""

    class PartlyBroken(AccountBroker):
        def order_report(self):
            raise RuntimeError("boom")

    orch = _orch(scrip, engine, PartlyBroken())
    summary = orch.refresh_broker_account()
    # orders failed (stayed 0) but positions and trades still refreshed
    assert summary["orders"] == 0
    assert summary["positions"] == 1
    assert summary["trades"] == 1
    assert len(Repository(engine).latest_broker_trades()) == 1


def test_refresh_paper_mode_is_harmless(scrip, engine):
    """Plain PaperBroker returns its book / empty reports — no error, empty order/trade stores."""
    orch = _orch(scrip, engine, PaperBroker())
    summary = orch.refresh_broker_account()
    assert summary["orders"] == 0
    assert summary["trades"] == 0
    assert Repository(engine).latest_broker_trades() == []


def test_broker_trades_store_round_trip(engine):
    repo = Repository(engine)
    assert repo.replace_broker_trades(RAW_TRADES) == 1
    stored = repo.latest_broker_trades()
    assert stored[0]["pTrdSymbol"] == "SENSEX24500CE"


def test_broker_refresh_seconds_setting_and_editable():
    s = get_settings(reload=True)
    assert s.broker_refresh_seconds == 5
    assert "broker_refresh_seconds" in EDITABLE_FIELDS
