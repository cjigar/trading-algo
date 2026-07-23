"""Live broker-account poller: refresh_broker_account persists positions/orders/trades, is
fail-safe per endpoint, and the broker-trades store + schema round-trip cleanly."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import EDITABLE_FIELDS, get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import ExchangeSegment, TradingMode
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository


class FakeCoord:
    """Records feed subscriptions instead of touching a websocket."""

    def __init__(self) -> None:
        self.subscribed: list[tuple] = []

    def subscribe_option(self, token, segment) -> None:
        self.subscribed.append((token, segment))

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
    # RAW_POSITIONS is squared (net 0) so nothing is marked for M2M this cycle.
    assert summary == {"positions": 1, "orders": 2, "trades": 1, "marked": 0}

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


# -- Live M2M: subscribe open-position tokens + publish their LTPs --------------------

OPEN_POSITIONS = [
    # open short option (priced below) — should subscribe on bse_fo and publish
    {"trdSym": "SENSEX77500PE", "tok": "835470", "exSeg": "bse_fo",
     "flBuyQty": "0", "flSellQty": "200", "buyAmt": "0", "sellAmt": "31324"},
    # open long equity — should subscribe on nse_cm (whole-account coverage), no tick yet
    {"trdSym": "CRAMC-EQ", "tok": "999", "exSeg": "nse_cm",
     "flBuyQty": "10", "flSellQty": "0", "buyAmt": "2660", "sellAmt": "0"},
    # squared — no subscription, no quote needed
    {"trdSym": "SQUARED", "tok": "111", "exSeg": "bse_fo",
     "flBuyQty": "50", "flSellQty": "50", "buyAmt": "100", "sellAmt": "120"},
]


def _open_broker():
    class OpenBroker(PaperBroker):
        def positions(self):
            return OPEN_POSITIONS

        def order_report(self):
            return []

        def trade_report(self):
            return []

    return OpenBroker()


def test_refresh_subscribes_open_tokens_and_publishes_priced_quotes(scrip, engine):
    orch = _orch(scrip, engine, _open_broker())
    coord = FakeCoord()
    orch._coordinator = coord
    orch._ltp["835470"] = Decimal("150")  # option has a live tick; equity 999 does not yet

    orch.refresh_broker_account()

    subbed = dict(coord.subscribed)
    # open option (bse_fo) + open equity (nse_cm) subscribed; squared token is not
    assert subbed == {"835470": ExchangeSegment.BSE_FO, "999": ExchangeSegment.NSE_CM}
    assert "111" not in subbed
    # only the token with a live LTP is published to live_quotes
    assert Repository(engine).live_quotes(["835470", "999", "111"]) == {"835470": Decimal("150")}


def test_refresh_subscribes_each_token_once(scrip, engine):
    orch = _orch(scrip, engine, _open_broker())
    coord = FakeCoord()
    orch._coordinator = coord
    orch.refresh_broker_account()
    orch.refresh_broker_account()  # second cycle must not resubscribe
    assert len(coord.subscribed) == 2  # still just the two open tokens


def test_refresh_bad_segment_is_fail_safe(scrip, engine):
    bad = [{"trdSym": "X", "tok": "5", "exSeg": "bogus_seg",
            "flBuyQty": "0", "flSellQty": "10", "buyAmt": "0", "sellAmt": "100"}]

    class B(PaperBroker):
        def positions(self):
            return bad

        def order_report(self):
            return []

        def trade_report(self):
            return []

    orch = _orch(scrip, engine, B())
    orch._coordinator = FakeCoord()
    # invalid exchange segment must not raise out of the poller
    summary = orch.refresh_broker_account()
    assert summary["marked"] == 0
