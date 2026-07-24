"""Persistence round-trips and the append-only audit guarantee."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from algo_trading.domain.enums import AlgoState, OrderState, Side
from algo_trading.domain.models import OrderEvent, Trade
from algo_trading.persistence.db import AuditEventRow, LiveQuoteRow, OrderEventRow
from algo_trading.persistence.repositories import Repository
from tests.conftest import make_instrument, make_order_request


def test_record_new_order_is_idempotent(repo: Repository, engine):
    req = make_order_request(client_tag="dup")
    repo.record_new_order(req)
    repo.record_new_order(req)  # retry must not create a duplicate

    with Session(engine) as s:
        events = s.exec(select(OrderEventRow).where(OrderEventRow.client_tag == "dup")).all()
    # exactly one PENDING created event despite two calls
    assert sum(1 for e in events if e.state == "PENDING") == 1
    assert repo.get_order_state("dup") == "PENDING"


def test_order_state_transitions_append_events(repo: Repository, engine):
    req = make_order_request(client_tag="t1")
    repo.record_new_order(req)
    for state, filled in [
        (OrderState.ACKNOWLEDGED, 0),
        (OrderState.PARTIALLY_FILLED, 25),
        (OrderState.FILLED, 75),
    ]:
        repo.apply_order_event(
            OrderEvent(
                client_tag="t1",
                broker_order_id="B123",
                state=state,
                filled_quantity=filled,
                average_price=Decimal("101.0"),
                timestamp=datetime(2025, 1, 15, 10, 1),
            )
        )
    assert repo.get_order_state("t1") == "FILLED"
    with Session(engine) as s:
        events = s.exec(select(OrderEventRow).where(OrderEventRow.client_tag == "t1")).all()
    # PENDING + 3 transitions = 4 append-only events
    assert len(events) == 4
    assert [e.state for e in events][-1] == "FILLED"


def test_trade_roundtrip(repo: Repository):
    inst = make_instrument()
    trade = Trade(
        client_tag="t1",
        broker_order_id="B1",
        instrument=inst,
        side=Side.BUY,
        quantity=75,
        price=Decimal("100.25"),
        timestamp=datetime(2025, 1, 15, 10, 2),
    )
    repo.record_trade(trade)
    out = repo.trades_for_day()
    assert len(out) == 1
    assert out[0].price == Decimal("100.25")
    assert out[0].instrument.trading_symbol == inst.trading_symbol
    assert out[0].side is Side.BUY


def test_pnl_snapshot_roundtrip(repo: Repository):
    repo.record_pnl(Decimal("100"), Decimal("-40"))
    repo.record_pnl(Decimal("120"), Decimal("30"))
    assert repo.latest_pnl() == Decimal("150")


def test_latest_pnl_snapshot_carries_components_and_time(repo: Repository):
    repo.record_pnl(Decimal("120"), Decimal("30"))
    snap = repo.latest_pnl_snapshot()
    assert snap is not None
    # The dashboard shows realized and unrealized separately, so the split has to survive the trip.
    assert (snap.realized, snap.unrealized, snap.total) == (
        Decimal("120"), Decimal("30"), Decimal("150"),
    )
    # Freshness is what tells the UI the loop is alive; a just-written snapshot must read as new.
    assert (datetime.utcnow() - snap.at).total_seconds() < 60


def test_latest_pnl_snapshot_is_none_before_the_loop_reports(repo: Repository):
    assert repo.latest_pnl_snapshot() is None


def test_live_quotes_upsert_replaces_previous_price(repo: Repository):
    repo.upsert_live_quotes({"11536": Decimal("100.5"), "11537": Decimal("80")})
    repo.upsert_live_quotes({"11536": Decimal("101.25")})
    # One row per token: the newer price replaces the old rather than accumulating.
    assert repo.live_quotes() == {"11536": Decimal("101.25"), "11537": Decimal("80")}


def test_index_spots_day_open_set_once_ltp_updates(repo: Repository):
    repo.upsert_index_spots({"NIFTY": Decimal("23800"), "SENSEX": Decimal("76000")})
    repo.upsert_index_spots({"NIFTY": Decimal("23912.5")})  # later tick, same day
    rows = {r.underlying: r for r in repo.index_spots()}
    # ltp advances but day_open stays the day's first price (survives a re-exec).
    assert rows["NIFTY"].ltp == "23912.5"
    assert rows["NIFTY"].day_open == "23800"
    assert rows["SENSEX"].ltp == "76000" and rows["SENSEX"].day_open == "76000"


def test_index_spots_empty_is_noop(repo: Repository):
    assert repo.upsert_index_spots({}) == 0
    assert repo.index_spots() == []


def test_index_spots_carry_futures_ltp(repo: Repository):
    repo.upsert_index_spots(
        {"NIFTY": Decimal("23800"), "SENSEX": Decimal("76000")},
        futures={"NIFTY": Decimal("23850")},  # SENSEX has no futures tick yet
    )
    rows = {r.underlying: r for r in repo.index_spots()}
    assert rows["NIFTY"].fut_ltp == "23850" and rows["NIFTY"].fut_updated_at is not None
    # An underlying with no futures reading stays unset (renders as "—", not a frozen price).
    assert rows["SENSEX"].fut_ltp == "0" and rows["SENSEX"].fut_updated_at is None


def test_index_spots_missing_futures_tick_keeps_last_value(repo: Repository):
    repo.upsert_index_spots({"NIFTY": Decimal("23800")}, futures={"NIFTY": Decimal("23850")})
    # A later cycle with no futures reading must NOT blank the stored futures value.
    repo.upsert_index_spots({"NIFTY": Decimal("23810")})
    row = {r.underlying: r for r in repo.index_spots()}["NIFTY"]
    assert row.ltp == "23810"  # spot advanced
    assert row.fut_ltp == "23850" and row.fut_updated_at is not None  # futures preserved


def test_prev_index_closes_picks_most_recent_prior_day(repo: Repository):
    from datetime import date

    repo.upsert_index_spots({"NIFTY": Decimal("100")}, trading_day=date(2026, 7, 20))
    repo.upsert_index_spots({"NIFTY": Decimal("110")}, trading_day=date(2026, 7, 22))  # more recent
    repo.upsert_index_spots({"NIFTY": Decimal("999")}, trading_day=date(2026, 7, 23))  # today, excluded
    got = repo.prev_index_closes(trading_day=date(2026, 7, 23))
    assert got == {"NIFTY": Decimal("110")}  # most recent day strictly before today (handles gaps)


def test_prev_index_closes_empty_when_no_prior_day(repo: Repository):
    from datetime import date

    repo.upsert_index_spots({"NIFTY": Decimal("100")}, trading_day=date(2026, 7, 23))
    assert repo.prev_index_closes(trading_day=date(2026, 7, 23)) == {}


def test_live_quotes_filters_to_requested_tokens(repo: Repository):
    repo.upsert_live_quotes({"11536": Decimal("100"), "11537": Decimal("80")})
    assert repo.live_quotes(["11537"]) == {"11537": Decimal("80")}
    # An explicitly empty token list means "nothing to mark", not "everything".
    assert repo.live_quotes([]) == {}


def test_live_quotes_drops_readings_older_than_max_age(repo: Repository, engine):
    repo.upsert_live_quotes({"11536": Decimal("100")})
    # Backdate the reading: a feed that died must not keep marking positions at its last price.
    with Session(engine) as s:
        row = s.exec(select(LiveQuoteRow)).one()
        row.timestamp = datetime.utcnow() - timedelta(seconds=300)
        s.add(row)
        s.commit()
    assert repo.live_quotes(max_age_seconds=60) == {}
    assert repo.live_quotes(max_age_seconds=600) == {"11536": Decimal("100")}


def test_upsert_live_quotes_ignores_an_empty_batch(repo: Repository):
    assert repo.upsert_live_quotes({}) == 0


def test_audit_is_append_only(repo: Repository, engine):
    repo.record_audit("kill_switch", "cap breached", {"pnl": -5000})
    repo.record_audit("manual_halt", "operator halt")
    events = repo.audit_events()
    assert len(events) == 2
    # Verify rows are only ever inserted (ids strictly increasing, no updates in the DAO surface)
    with Session(engine) as s:
        rows = s.exec(select(AuditEventRow).order_by(AuditEventRow.id)).all()
    assert [r.event_type for r in rows] == ["kill_switch", "manual_halt"]
    assert rows[0].payload == '{"pnl": -5000}'


def test_algo_state_persist_and_reload(repo: Repository, engine):
    assert repo.get_algo_state() is AlgoState.IDLE
    repo.set_algo_state(AlgoState.HALTED, reason="daily loss cap")
    # A fresh repository over the same engine (simulating a restart) sees the persisted state.
    reloaded = Repository(engine)
    assert reloaded.get_algo_state() is AlgoState.HALTED


def test_control_command_queue_consumes_once(repo: Repository):
    repo.enqueue_command("stop")
    repo.enqueue_command("flatten", {"reason": "manual"})
    first = repo.pop_pending_commands()
    assert [c.command for c in first] == ["stop", "flatten"]
    # Already consumed -> nothing returned on a second poll.
    assert repo.pop_pending_commands() == []


def test_latest_vwap_for_returns_newest_nonnull_per_token(repo: Repository):
    base = {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "volume": 10}
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T1", "oi": 1000, "ltp": "100", "vwap": "98.0",
         "timestamp": datetime(2025, 1, 15, 10, 0)},
    ])
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T1", "oi": 1000, "ltp": "101", "vwap": "99.5",
         "timestamp": datetime(2025, 1, 15, 10, 5)},  # newer
    ])
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T2", "oi": 1000, "ltp": "50",  # no vwap key -> NULL
         "timestamp": datetime(2025, 1, 15, 10, 0)},
    ])
    got = repo.latest_vwap_for(["T1", "T2", "T3"])
    assert got == {"T1": Decimal("99.5")}  # T1 newest non-null; T2 null-only omitted; T3 absent


def test_latest_vwap_for_empty_tokens_is_empty(repo: Repository):
    assert repo.latest_vwap_for([]) == {}


def test_replace_broker_positions_survives_unserializable_value(repo: Repository):
    """A broker report value whose str() raises (e.g. __str__ returns None) must not crash the
    persist — it froze the whole broker refresh in production every cycle."""
    class _BadStr:
        def __str__(self):
            return None  # type: ignore[return-value]  # makes str() raise TypeError

    n = repo.replace_broker_positions([{"tok": "T1", "weird": _BadStr(), "trdSym": "X"}])
    assert n == 1
    got = repo.latest_broker_positions()
    assert got and got[0]["tok"] == "T1"  # persisted despite the hostile value


def test_replace_broker_trades_survives_unserializable_value(repo: Repository):
    class _BadStr:
        def __str__(self):
            return None  # type: ignore[return-value]

    assert repo.replace_broker_trades([{"tok": "T1", "weird": _BadStr()}]) == 1
