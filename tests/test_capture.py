"""Capture-only mode: live feed -> chain snapshots in DB, and NO orders ever placed."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import ExchangeSegment, TradingMode
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository


class FakeNeo:
    """Minimal NeoAPI stand-in: settable callbacks + subscribe/orderfeed, like the real SDK."""

    def __init__(self):
        self.on_message = None
        self.on_error = None
        self.on_close = None
        self.on_open = None
        self.orderfeed = False

    def subscribe(self, instrument_tokens, isIndex=False, isDepth=False):
        pass

    def subscribe_to_orderfeed(self):
        self.orderfeed = True


@pytest.fixture()
def scrip():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}", "pSymbolName": "NIFTY",
                         "pExpiryDate": "2099-01-30", "dStrikePrice": k, "pOptionType": ot,
                         "lLotSize": 75})
    return ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)


def _capture_orch(scrip, engine, neo):
    s = get_settings(reload=True)
    object.__setattr__(s, "strategy", "oi_selling")
    object.__setattr__(s, "mode", TradingMode.PAPER)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "snapshot_min_interval_seconds", 0)
    object.__setattr__(s, "nifty_index_token", "NIFTY-IDX")
    orch = Orchestrator(s, scrip_master=scrip, broker=PaperBroker(), neo_client=neo,
                        repo=Repository(engine))
    orch.start_session()
    orch.attach_live_feeds()  # wires the coordinator to the fake neo (sets neo.on_message)
    return orch


def _socket(records):
    return {"type": "stock_feed", "data": records}


def test_capture_streams_chain_and_places_no_orders(scrip, engine):
    neo = FakeNeo()
    orch = _capture_orch(scrip, engine, neo)

    # 1) index quote arrives on the socket -> ATM resolves, chain window subscribes
    neo.on_message(_socket([{"tk": "NIFTY-IDX", "ltp": "23050", "e": "nse_cm"}]))

    # 2) option quotes with OI stream in (the real Kotak socket shape: tk/ltp/oi/v/e)
    for k in range(22800, 23350, 50):
        neo.on_message(_socket([
            {"tk": f"{k}CE", "ltp": "100", "oi": "5000", "v": "1000", "e": "nse_fo"},
            {"tk": f"{k}PE", "ltp": "100", "oi": "1000", "v": "1000", "e": "nse_fo"},
        ]))

    written = orch.flush_snapshots()
    assert written == 22  # 11 strikes × CE/PE captured

    # chain state persisted with OI
    state = orch.repo.latest_chain_state()
    assert len(state) == 22
    assert any(r.oi == 5000 for r in state) and any(r.oi == 1000 for r in state)

    # CRITICAL: capture placed NO orders (strategy never evaluated)
    assert orch.positions.open_position_count() == 0
    assert orch.repo.trades_for_day() == []


def test_capture_never_shorts_even_with_dominant_oi(scrip, engine):
    # Even with a strong OI imbalance, capture must not open a position (no evaluate_oi call).
    neo = FakeNeo()
    orch = _capture_orch(scrip, engine, neo)
    neo.on_message(_socket([{"tk": "NIFTY-IDX", "ltp": "23050", "e": "nse_cm"}]))
    for k in range(22800, 23350, 50):
        neo.on_message(_socket([{"tk": f"{k}CE", "ltp": "100", "oi": "99999", "v": "1", "e": "nse_fo"}]))
    orch.flush_snapshots()
    assert orch.positions.open_position_count() == 0
    assert orch.repo.trades_for_day() == []
