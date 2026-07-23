"""End-to-end OI-selling pipeline (paper): chain ticks -> OI -> short -> VWAP-cross exit."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import Orchestrator
from algo_trading.domain.enums import ExchangeSegment, Side, TradingMode, Underlying
from algo_trading.domain.models import Tick
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository

IDX = "NIFTY-IDX"
FRIDAY = datetime(2025, 1, 17, 10, 0, tzinfo=UTC)
WEDNESDAY = datetime(2025, 1, 15, 10, 0, tzinfo=UTC)


@pytest.fixture()
def scrip():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}", "pSymbolName": "NIFTY",
                         "pExpiryDate": "2099-01-30", "dStrikePrice": k, "pOptionType": ot,
                         "lLotSize": 75})
    return ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)


def _oi_settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strategy", "oi_selling")
    object.__setattr__(s, "mode", TradingMode.PAPER)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "otm_strikes", 3)
    object.__setattr__(s, "allowed_weekdays", [4, 0, 1])
    object.__setattr__(s, "market_holidays", [])
    object.__setattr__(s, "max_positions", 1)
    object.__setattr__(s, "snapshot_min_interval_seconds", 0)
    return s


def _build(scrip, engine):
    s = _oi_settings()
    orch = Orchestrator(s, scrip_master=scrip, broker=PaperBroker(), repo=Repository(engine))
    orch.register_index_token(IDX, Underlying.NIFTY)
    orch.start_session()
    return orch


def _idx(price):
    return Tick(instrument_token=IDX, exchange_segment=ExchangeSegment.NSE_CM, ltp=Decimal(price),
                timestamp=datetime(2025, 1, 15, tzinfo=UTC), is_index=True)


def _opt(token, price, oi=1000, vol=100):
    return Tick(instrument_token=token, exchange_segment=ExchangeSegment.NSE_FO, ltp=Decimal(price),
                timestamp=datetime(2025, 1, 15, tzinfo=UTC), oi=oi, volume=vol)


def _feed_chain(orch, ce_oi, pe_oi):
    orch.publish_tick(_idx("23050"))  # ATM = 23050, window 22800..23300
    for k in range(22800, 23350, 50):
        orch.publish_tick(_opt(f"{k}CE", "100", oi=ce_oi, vol=100))
        orch.publish_tick(_opt(f"{k}PE", "100", oi=pe_oi, vol=100))


def test_oi_short_entry_and_vwap_exit(scrip, engine):
    orch = _build(scrip, engine)
    _feed_chain(orch, ce_oi=5000, pe_oi=1000)  # CE OI dominates -> short a CE

    orch.evaluate_oi(now=FRIDAY)
    # short opened at the 3-OTM CE (23200)
    assert orch.positions.open_position_count() == 1
    pos = orch.positions.position_for("NIFTY23200CE")
    assert pos is not None and pos.side is Side.SELL

    # drive the shorted option's VWAP-cross exit: dip below VWAP (arm), then cross above
    orch.publish_tick(_opt("23200CE", "95", oi=5000))    # below VWAP(100) -> arm
    orch.publish_tick(_opt("23200CE", "115", oi=5000))   # above VWAP -> buy-to-close

    assert orch.positions.open_position_count() == 0     # short flattened
    trades = orch.repo.trades_for_day()
    assert len(trades) == 2                                # sell-to-open + buy-to-close
    assert {t.side for t in trades} == {Side.SELL, Side.BUY}


def test_oi_pe_side_when_pe_oi_dominates(scrip, engine):
    orch = _build(scrip, engine)
    _feed_chain(orch, ce_oi=1000, pe_oi=5000)  # PE OI dominates -> short a PE
    orch.evaluate_oi(now=FRIDAY)
    assert orch.positions.position_for("NIFTY22900PE") is not None  # ATM 23050 - 3*50


class _ExplodingBroker(PaperBroker):
    """A broker whose place_order fails the way Kotak did (unparseable response)."""

    def place_order(self, request):  # type: ignore[override]
        from algo_trading.broker.base import BrokerError

        raise BrokerError("Could not extract order id from response")


def test_oi_broker_error_does_not_crash_or_count_entry(scrip, engine):
    # Regression: a broker failure on sell-to-open must not propagate out of evaluate_oi
    # (which would kill the trading loop) and must not be recorded as an entry.
    s = _oi_settings()
    orch = Orchestrator(s, scrip_master=scrip, broker=_ExplodingBroker(), repo=Repository(engine))
    orch.register_index_token(IDX, Underlying.NIFTY)
    orch.start_session()
    _feed_chain(orch, ce_oi=5000, pe_oi=1000)

    orch.evaluate_oi(now=FRIDAY)  # must not raise

    assert orch.positions.open_position_count() == 0
    assert len(orch.repo.trades_for_day()) == 0
    assert orch.risk.entries_today == 0  # a failed submit is not an entry
    # a second evaluation still works (loop survived) and still opens nothing
    orch.evaluate_oi(now=FRIDAY)
    assert orch.positions.open_position_count() == 0


def test_oi_no_entry_on_disallowed_day(scrip, engine):
    orch = _build(scrip, engine)
    _feed_chain(orch, ce_oi=5000, pe_oi=1000)
    orch.evaluate_oi(now=WEDNESDAY)  # Wednesday not in Fri/Mon/Tue
    assert orch.positions.open_position_count() == 0


def test_oi_snapshots_persisted(scrip, engine):
    orch = _build(scrip, engine)
    _feed_chain(orch, ce_oi=5000, pe_oi=1000)
    orch._writer.flush()  # force flush the batched writer
    state = orch.repo.latest_chain_state()
    assert len(state) == 22  # 11 strikes × CE/PE


def test_orchestrator_purges_expired_snapshots(scrip, engine):
    s = _oi_settings()
    object.__setattr__(s, "chain_retention_mode", "expiry")
    repo = Repository(engine)
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23000", "option_type": "CE",
                                 "instrument_token": "EXP", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 21), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23050", "option_type": "CE",
                                 "instrument_token": "LIVE", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 28), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    orch = Orchestrator(s, scrip_master=scrip, broker=PaperBroker(), repo=repo)
    assert orch.purge_expired_snapshots(today=date(2025, 1, 22)) == 1
    assert {r.instrument_token for r in repo.latest_chain_state()} == {"LIVE"}


def test_orchestrator_purge_noop_in_days_mode(scrip, engine):
    s = _oi_settings()
    object.__setattr__(s, "chain_retention_mode", "days")
    repo = Repository(engine)
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23000", "option_type": "CE",
                                 "instrument_token": "EXP", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 21), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    orch = Orchestrator(s, scrip_master=scrip, broker=PaperBroker(), repo=repo)
    assert orch.purge_expired_snapshots(today=date(2025, 1, 22)) == 0
    assert {r.instrument_token for r in repo.latest_chain_state()} == {"EXP"}
