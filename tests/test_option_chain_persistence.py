"""Option-chain snapshot persistence: write, latest state, pruning, batched writer."""

from __future__ import annotations

from datetime import date, datetime

from algo_trading.persistence.repositories import Repository
from algo_trading.persistence.snapshot_writer import SnapshotWriter


def _snap(token, strike, ot="CE", oi=1000, ltp="100", ts=None):
    return {
        "underlying": "NIFTY", "strike": strike, "option_type": ot,
        "instrument_token": token, "oi": oi, "ltp": ltp, "volume": 50,
        "timestamp": ts or datetime(2025, 1, 15, 10, 0, 0),
    }


def test_write_and_latest_state(repo: Repository):
    # two snapshots for the same token -> latest_chain_state returns the newer one
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1500, ts=datetime(2025, 1, 15, 10, 5))])
    repo.write_chain_snapshots([_snap("T2", "23050", ot="PE", oi=2000)])
    state = repo.latest_chain_state()
    by_token = {r.instrument_token: r for r in state}
    assert set(by_token) == {"T1", "T2"}
    assert by_token["T1"].oi == 1500  # latest wins
    assert by_token["T2"].option_type == "PE"


def test_prune_snapshots(repo: Repository):
    old_day = date(2025, 1, 1)
    repo.write_chain_snapshots([_snap("T1", "23000")], trading_day=old_day)
    repo.write_chain_snapshots([_snap("T2", "23050")], trading_day=date(2025, 1, 15))
    # prune anything older than 5 days relative to 2025-01-15
    deleted = repo.prune_snapshots(older_than_days=5, today=date(2025, 1, 15))
    assert deleted == 1
    remaining = {r.instrument_token for r in repo.latest_chain_state(trading_day=date(2025, 1, 15))}
    assert remaining == {"T2"}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


def test_batched_writer_flushes_on_size(repo: Repository):
    clk = FakeClock()
    w = SnapshotWriter(repo, max_buffer=3, flush_seconds=999, clock=clk.now)
    w.add(_snap("A", "1"))
    w.add(_snap("B", "2"))
    assert w.pending == 2  # not yet flushed
    assert len(repo.latest_chain_state()) == 0
    w.add(_snap("C", "3"))  # hits max_buffer -> flush
    assert w.pending == 0
    assert len(repo.latest_chain_state()) == 3


def test_batched_writer_flushes_on_time(repo: Repository):
    clk = FakeClock()
    w = SnapshotWriter(repo, max_buffer=999, flush_seconds=2.0, clock=clk.now)
    w.add(_snap("A", "1"))
    clk.t = 3.0  # exceed flush window
    w.add(_snap("B", "2"))  # triggers time-based flush
    assert w.pending == 0
    assert len(repo.latest_chain_state()) == 2


def test_writer_min_interval_dedups(repo: Repository):
    clk = FakeClock()
    w = SnapshotWriter(repo, max_buffer=999, flush_seconds=999, min_interval_seconds=5, clock=clk.now)
    w.add(_snap("A", "1"))       # accepted at t=0
    w.add(_snap("A", "1"))       # dropped (within 5s)
    clk.t = 6.0
    w.add(_snap("A", "1"))       # accepted at t=6
    w.flush()
    assert len(repo.latest_chain_state()) == 1  # same token -> latest state is one row
    # but two rows were written (time series)
    from sqlmodel import Session, select

    from algo_trading.persistence.db import OptionChainSnapshotRow
    with Session(repo._engine) as s:
        assert len(s.exec(select(OptionChainSnapshotRow)).all()) == 2
