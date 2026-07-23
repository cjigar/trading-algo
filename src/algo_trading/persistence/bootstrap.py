"""Idempotent PostgreSQL/TimescaleDB schema bootstrap.

``SQLModel.metadata.create_all`` cannot express hypertables, compression, retention, or
continuous aggregates, so schema setup runs through :func:`bootstrap_schema` at startup:

1. ``CREATE EXTENSION IF NOT EXISTS timescaledb``
2. ``create_all`` for the ordinary tables
3. primary-key fixup on the snapshot tables (Timescale requires the partitioning column in
   every unique index), for databases created before this change
4. hypertable conversion (``migrate_data`` keeps rows already in a plain table)
5. compression + retention policies, reconciled against the configured intervals
6. the ``chain_oi_1m`` continuous aggregate backing rolling OI-trend anchor lookups

Every step is ``IF NOT EXISTS``-shaped or guarded by a catalog lookup, so a restart — or two
containers booting at once — is a no-op. All statements run with AUTOCOMMIT: hypertable
conversion with ``migrate_data`` and continuous-aggregate creation cannot run inside a
transaction block.
"""

from __future__ import annotations

import time as _time

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError
from sqlmodel import SQLModel

from algo_trading.observability.logging import get_logger

log = get_logger("persistence.bootstrap")

# Table -> the column it is partitioned on.
HYPERTABLES: dict[str, str] = {
    "option_chain_snapshots": "timestamp",
    "pnl_snapshots": "timestamp",
}

CHAIN_TABLE = "option_chain_snapshots"
CHAIN_AGG_VIEW = "chain_oi_1m"

# Defaults mirroring Settings, for callers that bootstrap without a settings object (tests).
DEFAULT_CHUNK_INTERVAL_DAYS = 1
DEFAULT_COMPRESS_AFTER_DAYS = 2
DEFAULT_RETENTION_DAYS = 30
DEFAULT_AGG_BUCKET_SECONDS = 60


class TimescaleUnavailableError(RuntimeError):
    """The connected server cannot provide the timescaledb extension."""


def bootstrap_schema(engine: Engine, *, settings=None, retries: int = 1) -> None:
    """Create/upgrade the full schema on ``engine``. Safe to call repeatedly."""
    chunk_days = _setting(settings, "chain_chunk_interval_days", DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_days = _setting(settings, "chain_compress_after_days", DEFAULT_COMPRESS_AFTER_DAYS)
    retention_days = _setting(settings, "chain_retention_days", DEFAULT_RETENTION_DAYS)
    bucket_seconds = _setting(settings, "chain_agg_bucket_seconds", DEFAULT_AGG_BUCKET_SECONDS)

    _with_retry(engine, retries, lambda conn: _install_extension(conn))
    SQLModel.metadata.create_all(engine)
    _ensure_declared_indexes(engine)

    with _autocommit(engine) as conn:
        for table, time_column in HYPERTABLES.items():
            _ensure_primary_key(conn, table, time_column)
            _ensure_hypertable(conn, table, time_column, chunk_days)
        _ensure_chain_columns(conn)
        _ensure_compression(conn, CHAIN_TABLE, compress_days)
        _ensure_retention(conn, CHAIN_TABLE, retention_days)
        _ensure_chain_aggregate(conn, bucket_seconds)

    log.info(
        "db_bootstrap_done",
        chunk_days=chunk_days,
        compress_after_days=compress_days,
        retention_days=retention_days,
        agg_bucket_seconds=bucket_seconds,
    )


def agg_bucket_seconds(engine: Engine) -> int:
    """The continuous aggregate's bucket width in seconds, as it exists in the database.

    The read path needs this to split an anchor lookup into "complete buckets" (served by the
    aggregate) and "the tail since the last complete bucket" (served by the raw hypertable).
    """
    # The public information views don't expose the bucket width, so this reads the catalog the
    # aggregate itself is defined from.
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM CAST(b.bucket_width AS interval)) "
                "FROM _timescaledb_catalog.continuous_aggs_bucket_function b "
                "JOIN _timescaledb_catalog.continuous_agg c "
                "  ON c.mat_hypertable_id = b.mat_hypertable_id "
                "WHERE c.user_view_name = :view"
            ),
            {"view": CHAIN_AGG_VIEW},
        ).first()
    if row is None or row[0] is None:
        return DEFAULT_AGG_BUCKET_SECONDS
    return int(row[0])


def has_chain_aggregate(engine: Engine) -> bool:
    """True when the continuous aggregate exists (false on a plain-Postgres/legacy database)."""
    with engine.connect() as conn:
        return _view_exists(conn, CHAIN_AGG_VIEW)


# --- steps ---------------------------------------------------------------------------


def _ensure_declared_indexes(engine: Engine) -> None:
    """Create model-declared indexes that ``create_all`` skipped on pre-existing tables.

    ``metadata.create_all`` only emits DDL for tables it actually creates; a table that already
    exists is left untouched, so an index added to a model *after* its table was first created
    (e.g. the composite ``ix_chain_snap_token_day_ts`` on the already-live snapshot table) never
    lands. Re-issue every declared index with ``checkfirst`` so bootstrap self-heals on the next
    start. On a hypertable a plain ``CREATE INDEX`` briefly locks while it propagates to chunks;
    bootstrap runs at startup (out of market hours per the deploy policy) and this fires only when
    an index is genuinely missing, so the one-time cost is acceptable.
    """
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def _ensure_chain_columns(conn: Connection) -> None:
    """Add columns introduced after the hypertable already existed. create_all() only creates
    tables, never alters them, so new columns on option_chain_snapshots are added here."""
    conn.execute(
        text(f"ALTER TABLE {CHAIN_TABLE} ADD COLUMN IF NOT EXISTS vwap varchar")
    )


def _install_extension(conn: Connection) -> None:
    available = conn.execute(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).first()
    if available is None:
        raise TimescaleUnavailableError(
            "The 'timescaledb' extension is not available on this PostgreSQL server. "
            "Use the timescale/timescaledb:latest-pg16 image (see docker-compose.yml)."
        )
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))


def _ensure_primary_key(conn: Connection, table: str, time_column: str) -> None:
    """Ensure the primary key includes the partitioning column.

    Tables created by ``create_all`` from the current models already satisfy this; tables created
    before this change have a bare ``id`` primary key that Timescale rejects.
    """
    row = conn.execute(
        text(
            "SELECT c.conname, "
            "       (SELECT array_agg(a.attname) FROM unnest(c.conkey) k "
            "        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k) AS cols "
            "FROM pg_constraint c "
            "WHERE c.conrelid = to_regclass(:table) AND c.contype = 'p'"
        ),
        {"table": table},
    ).first()
    if row is None:
        return  # no primary key at all — nothing for Timescale to reject
    conname, cols = row
    if time_column in (cols or []):
        return
    log.info("db_pk_fixup", table=table, from_columns=list(cols or []))
    conn.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{conname}"'))
    new_cols = ", ".join([time_column, *[c for c in (cols or [])]])
    conn.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY ({new_cols})"))


def _ensure_hypertable(conn: Connection, table: str, time_column: str, chunk_days: int) -> None:
    if _is_hypertable(conn, table):
        return
    interval = f"{chunk_days} days"
    conn.execute(
        text(
            "SELECT create_hypertable(:table, :column, chunk_time_interval => CAST(:interval AS interval),"
            " migrate_data => true, if_not_exists => true)"
        ),
        {"table": table, "column": time_column, "interval": interval},
    )
    log.info("db_hypertable_created", table=table, chunk_interval=interval)


def _ensure_compression(conn: Connection, table: str, compress_after_days: int) -> None:
    conn.execute(
        text(
            f"ALTER TABLE {table} SET ("
            " timescaledb.compress,"
            " timescaledb.compress_segmentby = 'instrument_token',"
            " timescaledb.compress_orderby = 'timestamp DESC'"
            ")"
        )
    )
    _reconcile_policy(
        conn,
        table=table,
        proc_name="policy_compression",
        config_key="compress_after",
        want_days=compress_after_days,
        add_sql=(
            "SELECT add_compression_policy(:table, CAST(:interval AS interval), if_not_exists => true)"
        ),
        remove_sql="SELECT remove_compression_policy(:table, if_exists => true)",
    )


def _ensure_retention(conn: Connection, table: str, retention_days: int) -> None:
    _reconcile_policy(
        conn,
        table=table,
        proc_name="policy_retention",
        config_key="drop_after",
        want_days=retention_days,
        add_sql=(
            "SELECT add_retention_policy(:table, CAST(:interval AS interval), if_not_exists => true)"
        ),
        remove_sql="SELECT remove_retention_policy(:table, if_exists => true)",
    )


def _ensure_chain_aggregate(conn: Connection, bucket_seconds: int) -> None:
    """Create the per-token OI/LTP rollup used by the rolling OI-trend anchor lookups.

    ``materialized_only = false`` keeps real-time aggregation on (Timescale 2.13+ defaults it to
    true), so the view always reflects rows written since the last refresh and the read path never
    has to reason about the materialization watermark.

    The broker sends OI only in a token's first (full) packet; later ticks carry LTP with a NULL
    OI. A plain ``last(oi, timestamp)`` therefore returns NULL for most buckets, so the OI
    aggregates are filtered to rows that actually carry one, and ``oi_at`` records when that
    reading was taken (the anchor's true timestamp, not the bucket's).
    """
    bucket = f"{int(bucket_seconds)} seconds"
    if _view_exists(conn, CHAIN_AGG_VIEW) and not _view_has_column(conn, CHAIN_AGG_VIEW, "oi_at"):
        # Pre-fix definition (no NULL filtering): drop it so it is recreated below. Dropping a
        # continuous aggregate discards only the rollup — the raw hypertable is untouched.
        log.info("db_continuous_aggregate_outdated", view=CHAIN_AGG_VIEW)
        conn.execute(text(f"DROP MATERIALIZED VIEW {CHAIN_AGG_VIEW}"))
    if not _view_exists(conn, CHAIN_AGG_VIEW):
        # PostgreSQL rejects bound parameters in a materialized-view definition, so the bucket
        # width is interpolated — it is coerced to int above, never free text.
        conn.execute(
            text(
                f"CREATE MATERIALIZED VIEW {CHAIN_AGG_VIEW} "
                "WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS "
                f"SELECT time_bucket(INTERVAL '{bucket}', timestamp) AS bucket, "
                "       instrument_token, trading_day, underlying, "
                "       last(oi, timestamp) FILTER (WHERE oi IS NOT NULL) AS last_oi, "
                "       max(timestamp) FILTER (WHERE oi IS NOT NULL) AS oi_at, "
                "       last(ltp, timestamp) AS last_ltp "
                f"FROM {CHAIN_TABLE} "
                "GROUP BY bucket, instrument_token, trading_day, underlying "
                "WITH NO DATA"
            )
        )
        log.info("db_continuous_aggregate_created", view=CHAIN_AGG_VIEW, bucket=bucket)
    exists = conn.execute(
        text(
            "SELECT 1 FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_name IN "
            "(SELECT materialization_hypertable_name FROM timescaledb_information.continuous_aggregates"
            " WHERE view_name = :view)"
        ),
        {"view": CHAIN_AGG_VIEW},
    ).first()
    if exists is None:
        conn.execute(
            text(
                "SELECT add_continuous_aggregate_policy(:view,"
                " start_offset => INTERVAL '1 day',"
                " end_offset => CAST(:bucket AS interval),"
                " schedule_interval => CAST(:bucket AS interval),"
                " if_not_exists => true)"
            ),
            {"view": CHAIN_AGG_VIEW, "bucket": bucket},
        )


# --- helpers -------------------------------------------------------------------------


def _reconcile_policy(
    conn: Connection,
    *,
    table: str,
    proc_name: str,
    config_key: str,
    want_days: int,
    add_sql: str,
    remove_sql: str,
) -> None:
    """Add the policy, or replace it when its configured interval no longer matches settings."""
    want = f"{want_days} days"
    existing = conn.execute(
        text(
            "SELECT config ->> :key FROM timescaledb_information.jobs "
            "WHERE proc_name = :proc AND hypertable_name = :table"
        ),
        {"key": config_key, "proc": proc_name, "table": table},
    ).first()
    if existing is not None:
        current = conn.execute(
            text("SELECT CAST(:current AS interval) = CAST(:want AS interval)"),
            {"current": existing[0], "want": want},
        ).scalar()
        if current:
            return
        log.info("db_policy_reconfigured", table=table, policy=proc_name, interval=want)
        conn.execute(text(remove_sql), {"table": table})
    conn.execute(text(add_sql), {"table": table, "interval": want})


def _is_hypertable(conn: Connection, table: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :table"
            ),
            {"table": table},
        ).first()
        is not None
    )


def _view_exists(conn: Connection, view: str) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :view"),
            {"view": view},
        ).first()
        is not None
    )


def _view_has_column(conn: Connection, view: str, column: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :view AND column_name = :column"
            ),
            {"view": view, "column": column},
        ).first()
        is not None
    )


def _autocommit(engine: Engine):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _with_retry(engine: Engine, retries: int, fn) -> None:
    """Run ``fn(connection)``, retrying while the server refuses connections (container boot)."""
    attempt = 0
    while True:
        try:
            with _autocommit(engine) as conn:
                fn(conn)
            return
        except OperationalError:
            attempt += 1
            if attempt > retries:
                raise
            wait = min(2 ** attempt, 10)
            log.warning("db_not_ready_retrying", attempt=attempt, wait_s=wait)
            _time.sleep(wait)


def _setting(settings, name: str, default):
    return getattr(settings, name, default) if settings is not None else default
