"""Option-chain snapshot persistence: write, latest state, OI anchors, batched writer.

Retention is a TimescaleDB policy now (see tests/test_timescale_schema.py), not a repository call.
"""

from __future__ import annotations

from datetime import datetime

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


# The broker sends OI in a token's first full packet and NULL in the LTP-only ticks that follow,
# so "newest row" and "newest OI reading" are different rows for nearly every token.


def test_latest_state_carries_last_known_oi_over_ltp_only_ticks(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ltp="100", ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=None, ltp="105", ts=datetime(2025, 1, 15, 10, 1))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=None, ltp="110", ts=datetime(2025, 1, 15, 10, 2))])
    row = repo.latest_chain_state()[0]
    assert row.ltp == "110"  # LTP from the genuinely newest row
    assert row.oi == 1000    # OI carried over from the last row that had one


def test_latest_state_oi_stays_none_when_never_reported(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", oi=None, ltp="100", ts=datetime(2025, 1, 15, 10, 0))])
    assert repo.latest_chain_state()[0].oi is None


def test_day_open_oi_skips_ltp_only_ticks(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", oi=None, ltp="100", ts=datetime(2025, 1, 15, 9, 15))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=900, ltp="101", ts=datetime(2025, 1, 15, 9, 16))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1200, ltp="102", ts=datetime(2025, 1, 15, 10, 0))])
    assert repo.chain_day_open_oi() == {"T1": 900}  # first row WITH an OI, not the NULL one


def test_oi_anchor_skips_ltp_only_ticks(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=None, ts=datetime(2025, 1, 15, 10, 2))])
    # The newest row before 10:03 has no OI; the anchor must be the 10:00 reading, not 0.
    assert repo.oi_at_or_before(datetime(2025, 1, 15, 10, 3)) == {"T1": 1000}


def test_oi_at_or_before_selects_latest_prior_row(repo: Repository):
    # T1 ticks at 10:00 (oi=1000) and 10:05 (oi=1500); anchor as of 10:03 must be the 10:00 row.
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1500, ts=datetime(2025, 1, 15, 10, 5))])
    anchors = repo.oi_at_or_before(datetime(2025, 1, 15, 10, 3))
    assert anchors == {"T1": 1000}


def test_oi_at_or_before_boundary_is_inclusive(repo: Repository):
    # A snapshot exactly at the target timestamp counts (at-or-before).
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    anchors = repo.oi_at_or_before(datetime(2025, 1, 15, 10, 0))
    assert anchors == {"T1": 1000}


def test_oi_at_or_before_omits_tokens_with_no_prior_row(repo: Repository):
    # Target precedes T1's only snapshot -> no anchor -> token omitted (not zero).
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 5))])
    anchors = repo.oi_at_or_before(datetime(2025, 1, 15, 10, 0))
    assert anchors == {}


def test_oi_at_or_before_multiple_tokens(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T2", "23050", oi=2000, ts=datetime(2025, 1, 15, 10, 2))])
    # T3 only appears after the target -> omitted.
    repo.write_chain_snapshots([_snap("T3", "23100", oi=3000, ts=datetime(2025, 1, 15, 10, 9))])
    anchors = repo.oi_at_or_before(datetime(2025, 1, 15, 10, 4))
    assert anchors == {"T1": 1000, "T2": 2000}


def test_oi_anchors_for_windows(repo: Repository):
    now = datetime(2025, 1, 15, 10, 15)
    # T1 history: 10:00 (oi=1000), 10:12 (oi=1400), 10:14 (oi=1600).
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1400, ts=datetime(2025, 1, 15, 10, 12))])
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1600, ts=datetime(2025, 1, 15, 10, 14))])
    anchors = repo.oi_anchors_for_windows(now, [1, 3, 5, 15])
    # now-1m=10:14 -> 1600; now-3m=10:12 -> 1400; now-5m=10:10 -> 1000; now-15m=10:00 -> 1000
    assert anchors[1] == {"T1": 1600}
    assert anchors[3] == {"T1": 1400}
    assert anchors[5] == {"T1": 1000}
    assert anchors[15] == {"T1": 1000}


def test_oi_anchors_for_windows_unavailable_when_history_too_short(repo: Repository):
    now = datetime(2025, 1, 15, 10, 15)
    # Only a 10:14 snapshot: within the 1m window there is no PRIOR row, but 3/5/15m windows
    # target times all precede it -> unavailable for those.
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1600, ts=datetime(2025, 1, 15, 10, 14))])
    anchors = repo.oi_anchors_for_windows(now, [1, 3, 5, 15])
    assert anchors[1] == {"T1": 1600}  # 10:14 <= now-1m (10:14)
    assert anchors[3] == {}
    assert anchors[5] == {}
    assert anchors[15] == {}


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
