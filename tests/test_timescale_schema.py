"""TimescaleDB schema bootstrap: hypertables, policies, the continuous aggregate, and the
compressed-chunk read path.

Tests that mutate schema-level state (policy intervals, legacy-table conversion) run against
their own throwaway database so the session database stays as the rest of the suite expects.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from algo_trading.persistence.bootstrap import (
    CHAIN_AGG_VIEW,
    CHAIN_TABLE,
    bootstrap_schema,
    has_chain_aggregate,
)
from algo_trading.persistence.db import create_engine_from_url
from algo_trading.persistence.repositories import Repository
from algo_trading.persistence.testing import SchemaTuning


def _jobs(engine, proc: str, table: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT config FROM timescaledb_information.jobs "
                "WHERE proc_name = :proc AND hypertable_name = :table"
            ),
            {"proc": proc, "table": table},
        ).all()
    return [r[0] for r in rows]


def _snap(token: str, oi: int, ts: datetime) -> dict:
    return {
        "underlying": "NIFTY", "strike": "23000", "option_type": "CE",
        "instrument_token": token, "oi": oi, "ltp": "100", "volume": 50, "timestamp": ts,
    }


# --- fresh bootstrap ------------------------------------------------------------------


def test_bootstrap_creates_hypertables_aggregate_and_policies(pg_engine):
    with pg_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT hypertable_name FROM timescaledb_information.hypertables")
            ).all()
        }
    assert {"option_chain_snapshots", "pnl_snapshots"} <= tables
    assert has_chain_aggregate(pg_engine)
    assert _jobs(pg_engine, "policy_compression", CHAIN_TABLE)
    assert _jobs(pg_engine, "policy_retention", CHAIN_TABLE)


def test_bootstrap_is_idempotent(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        before = (
            _jobs(engine, "policy_compression", CHAIN_TABLE),
            _jobs(engine, "policy_retention", CHAIN_TABLE),
        )
        bootstrap_schema(engine, settings=SchemaTuning())  # second run
        after = (
            _jobs(engine, "policy_compression", CHAIN_TABLE),
            _jobs(engine, "policy_retention", CHAIN_TABLE),
        )
        assert before == after
        assert len(after[0]) == 1 and len(after[1]) == 1  # no duplicate policies
    finally:
        engine.dispose()


def test_policies_use_configured_intervals(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning(retention_days=7, compress_after_days=1))
    try:
        assert _jobs(engine, "policy_retention", CHAIN_TABLE)[0]["drop_after"] == "7 days"
        assert _jobs(engine, "policy_compression", CHAIN_TABLE)[0]["compress_after"] == "1 day"
    finally:
        engine.dispose()


def test_changed_policy_interval_is_updated_not_duplicated(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning(retention_days=7))
    try:
        bootstrap_schema(engine, settings=SchemaTuning(retention_days=5))
        jobs = _jobs(engine, "policy_retention", CHAIN_TABLE)
        assert len(jobs) == 1
        assert jobs[0]["drop_after"] == "5 days"
    finally:
        engine.dispose()


def test_vwap_column_added_idempotently(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'option_chain_snapshots' AND column_name = 'vwap'"
            )).first()
            assert has is not None
        bootstrap_schema(engine, settings=SchemaTuning())  # second run must be a no-op
        with engine.connect() as conn:
            count = conn.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'option_chain_snapshots' AND column_name = 'vwap'"
            )).scalar()
        assert count == 1
    finally:
        engine.dispose()


def test_greeks_columns_added_idempotently(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            present = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'option_chain_snapshots'"
                )).all()
            }
        assert {"iv", "delta", "gamma", "theta", "vega"} <= present
        bootstrap_schema(engine, settings=SchemaTuning())  # second run must be a no-op
    finally:
        engine.dispose()


# --- conversion of a pre-existing (SQLite-era) table -----------------------------------


LEGACY_CHAIN_DDL = """
CREATE TABLE option_chain_snapshots (
    id SERIAL PRIMARY KEY,
    trading_day VARCHAR NOT NULL,
    underlying VARCHAR NOT NULL,
    strike VARCHAR NOT NULL,
    option_type VARCHAR NOT NULL,
    instrument_token VARCHAR NOT NULL,
    oi INTEGER,
    ltp VARCHAR NOT NULL,
    volume INTEGER,
    timestamp TIMESTAMP NOT NULL
)
"""


def test_populated_legacy_table_is_converted_without_losing_rows(fresh_db):
    """A table created before this change has a bare `id` primary key and no partitioning."""
    setup = create_engine_from_url(fresh_db, create=False)
    with setup.connect() as conn:
        conn.execute(text(LEGACY_CHAIN_DDL))
        conn.execute(
            text(
                "INSERT INTO option_chain_snapshots "
                "(trading_day, underlying, strike, option_type, instrument_token, oi, ltp, volume, timestamp)"
                " VALUES ('2025-01-15', 'NIFTY', '23000', 'CE', 'T1', 1000, '100', 50, '2025-01-15 10:00:00')"
            )
        )
        conn.commit()
    setup.dispose()

    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM option_chain_snapshots")).scalar() == 1
            pk_cols = conn.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN unnest(c.conkey) k ON true "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k "
                    "WHERE c.conrelid = 'option_chain_snapshots'::regclass AND c.contype = 'p'"
                )
            ).scalars().all()
            assert "timestamp" in pk_cols
            assert conn.execute(
                text(
                    "SELECT 1 FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = 'option_chain_snapshots'"
                )
            ).first() is not None
            assert conn.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_name = "
                "'option_chain_snapshots' AND column_name = 'vwap'"
            )).first() is not None
    finally:
        engine.dispose()


def test_bootstrap_adds_declared_index_missing_from_preexisting_table(fresh_db):
    """A model index added after the table already existed must be created by bootstrap.

    ``create_all`` skips tables that already exist, so ``ix_chain_snap_token_day_ts`` (declared on
    the snapshot model but added after the live table was first created) would otherwise never
    land — the exact gap seen in production. ``_ensure_declared_indexes`` closes it.
    """
    setup = create_engine_from_url(fresh_db, create=False)
    with setup.connect() as conn:
        conn.execute(text(LEGACY_CHAIN_DDL))  # pre-existing table, no composite index
        conn.commit()
    setup.dispose()

    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE tablename = 'option_chain_snapshots' "
                    "AND indexname = 'ix_chain_snap_token_day_ts'"
                )
            ).first() is not None
    finally:
        engine.dispose()


# --- read path over compressed chunks --------------------------------------------------


def test_anchor_query_survives_chunk_compression(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    repo = Repository(engine)
    day = (datetime.utcnow() - timedelta(days=10)).date()
    base = datetime(day.year, day.month, day.day, 10, 0)
    repo.write_chain_snapshots([_snap("T1", 1000, base)], trading_day=day)
    repo.write_chain_snapshots([_snap("T1", 1500, base + timedelta(minutes=5))], trading_day=day)

    before = repo.oi_at_or_before(base + timedelta(minutes=3), trading_day=day)
    assert before == {"T1": 1000}

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # Materialize the aggregate up to now, then compress every chunk of the raw table.
        conn.execute(text(f"CALL refresh_continuous_aggregate('{CHAIN_AGG_VIEW}', NULL, NULL)"))
        chunks = conn.execute(text(f"SELECT show_chunks('{CHAIN_TABLE}')")).scalars().all()
        assert chunks
        for chunk in chunks:
            conn.execute(text("SELECT compress_chunk(:c, if_not_compressed => true)"), {"c": chunk})

    assert repo.oi_at_or_before(base + timedelta(minutes=3), trading_day=day) == {"T1": 1000}
    assert repo.oi_at_or_before(base + timedelta(minutes=6), trading_day=day) == {"T1": 1500}
    engine.dispose()


def test_aggregate_and_raw_paths_agree(fresh_db):
    """The aggregate serves closed buckets and the raw table the tail; both must produce the
    anchor the raw data alone implies."""
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    repo = Repository(engine)
    day = datetime(2025, 1, 15).date()
    base = datetime(2025, 1, 15, 10, 0)
    for minute, oi in [(0, 1000), (1, 1100), (2, 1200), (3, 1300), (7, 1700)]:
        repo.write_chain_snapshots([_snap("T1", oi, base + timedelta(minutes=minute))], trading_day=day)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CALL refresh_continuous_aggregate('{CHAIN_AGG_VIEW}', NULL, NULL)"))

    # 10:03:30 -> last tick at or before is 10:03 (1300); the 10:07 tick must NOT leak in.
    assert repo.oi_at_or_before(base + timedelta(minutes=3, seconds=30), trading_day=day) == {"T1": 1300}
    # Deep in materialized history: 10:05 -> still the 10:03 tick.
    assert repo.oi_at_or_before(base + timedelta(minutes=5), trading_day=day) == {"T1": 1300}
    # Before any tick -> token omitted.
    assert repo.oi_at_or_before(base - timedelta(minutes=1), trading_day=day) == {}
    engine.dispose()


def test_bootstrap_adds_new_columns_to_legacy_table(fresh_db):
    """Regression: a table that predates the vwap/expiry/greeks columns must gain them on
    bootstrap. Column-adds must run before index/hypertable DDL so an indexed new column can never
    have its index created before the column exists (the ordering that crash-looped production)."""
    setup = create_engine_from_url(fresh_db, create=False)
    with setup.connect() as conn:
        conn.execute(text(LEGACY_CHAIN_DDL))  # no vwap/expiry/iv/delta/gamma/theta/vega
        conn.commit()
    setup.dispose()

    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())  # full bootstrap, must not raise
    try:
        with engine.connect() as conn:
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'option_chain_snapshots'"
                ))
            }
        for c in ("vwap", "expiry", "iv", "delta", "gamma", "theta", "vega"):
            assert c in cols, f"{c} column not added to legacy table"
    finally:
        engine.dispose()
