## Context

`persistence/db.py` builds a SQLAlchemy engine from `settings.resolved_database_url()`, which returns `sqlite:///{db_path}` whenever `ALGO_DATABASE_URL` is blank. Compose sets that variable for `algo`, `api`, and (indirectly) the dashboard, so production is already PostgreSQL 16 on the `pgdata` volume; local runs and the whole test suite are SQLite. Schema creation is `SQLModel.metadata.create_all` only — no migration tool is in the repo.

The hot table is `option_chain_snapshots`: one append-only row per option token per capture, gated to `snapshot_min_interval_seconds` (default 2s). With a ±5-strike window on two underlyings that is on the order of 10⁴–10⁵ rows per trading day. Reads are (a) `latest_chain_state` — newest row per token, and (b) `oi_at_or_before` / `oi_anchors_for_windows` — the newest row per token at-or-before `now − N minutes`, called once per window (1/3/5/15) on every chain refresh. Both are currently expressed as `SELECT * WHERE id IN (SELECT max(id) … GROUP BY instrument_token)`, which is portable but forces the planner through the autoincrement `id` rather than the time column. Retention is a manual `prune_snapshots` delete keyed on the `trading_day` string.

Constraints: single-node deployment, one small VPS, no DBA; the loop must not stall on maintenance work during market hours; monetary values stay `Decimal`-as-string; the existing `pgdata` volume must be preserved.

## Goals / Non-Goals

**Goals:**
- One backend everywhere — Postgres in prod, dev, and tests — so tests exercise the real dialect and Postgres-only SQL is safe.
- Time-series tables that stay cheap as the day and the history grow: time partitioning, compression, automatic retention.
- Rolling OI-window reads served from pre-bucketed data instead of full-day raw scans, with identical observable results.
- Idempotent, code-owned schema bootstrap — no manual `psql` step on deploy.

**Non-Goals:**
- Migrating existing local SQLite files into Postgres (dev/audit data only; discarded).
- Introducing Alembic or a general migration framework. Bootstrap stays `create_all` + idempotent DDL, as today.
- Multi-node, replication, or backup strategy.
- Reshaping the non-time-series tables (`orders`, `trades`, `audit_events`, …) — they stay ordinary tables.

## Decisions

### Decision 1: TimescaleDB rather than hand-rolled partitioning

Use `timescale/timescaledb:latest-pg16` and hypertables. (Not the `-ha` variant: it sets `PGDATA=/home/postgres/pgdata/data`, so a volume mounted at the stock path is ignored and an empty cluster is initialised in its place — caught during the production deploy.)

*Why:* the three things this change needs — automatic time partitioning, columnar compression of old chunks, and a maintained rolling rollup — are exactly what hypertables, compression policies, and continuous aggregates provide. Hand-rolling them on stock Postgres means a partition-creation job, a drop-old-partitions job, and a rollup table with its own refresh job — three cron-shaped things to get wrong, in a repo with no DBA.

*Alternatives:* (a) declarative `PARTITION BY RANGE (timestamp)` plus custom maintenance — more code, no compression story; (b) single table + index + retention delete — simplest, but the OI-window scans and the retention delete both degrade with history, which is the problem being solved. The image is a superset of the stock one and keeps `PGDATA=/var/lib/postgresql/data`, so `pgdata` and every existing table carry over unchanged.

### Decision 2: Hypertables on `option_chain_snapshots` and `pnl_snapshots`, keyed on `(timestamp, id)`

Timescale requires every unique index to include the partitioning column. The current `id` integer primary key does not. Change the primary key to a composite `(timestamp, id)` with `id` still a sequence-backed identity, and set `chunk_time_interval` to 1 day for `option_chain_snapshots` (one chunk per trading day, matching the query pattern) and 7 days for the far smaller `pnl_snapshots`.

*Why not drop `id`:* nothing outside the two `max(id)` subqueries depends on it, and those go away in Decision 4 — but keeping it costs nothing and preserves insertion order as a tiebreaker for snapshots sharing a timestamp.

Conversion runs via `create_hypertable(..., migrate_data => true, if_not_exists => true)` so existing production rows survive.

### Decision 3: Bootstrap = `create_all` + idempotent DDL block

A new `persistence/bootstrap.py` runs, in order, inside one connection: `CREATE EXTENSION IF NOT EXISTS timescaledb` → `SQLModel.metadata.create_all` → primary-key fixups → `create_hypertable(if_not_exists)` → `ALTER TABLE … SET (timescaledb.compress, …)` → `add_compression_policy` / `add_retention_policy` (drop-and-re-add when the configured interval differs from the installed one) → `CREATE MATERIALIZED VIEW … WITH (timescaledb.continuous)` + `add_continuous_aggregate_policy`. Every statement is `IF NOT EXISTS`-shaped or guarded by a catalog lookup (`timescaledb_information.jobs`, `.hypertables`), so restart is a no-op.

*Why in-process:* the app already owns `create_all` at startup and both `algo` and `api` containers connect to the same DB; a separate migration entrypoint would need its own ordering and healthcheck. Concurrent bootstrap from two containers is safe because every statement is idempotent and Postgres serializes the DDL.

### Decision 4: Anchor reads via `DISTINCT ON`, split on the bucket boundary

Replace `id IN (SELECT max(id) … GROUP BY token)` with Postgres-native
`SELECT DISTINCT ON (instrument_token) … ORDER BY instrument_token, <time> DESC` against a 1-minute continuous aggregate `chain_oi_1m (bucket, instrument_token, trading_day, underlying, last_oi, last_ltp)` built with `time_bucket('1 minute', timestamp)` and `last(oi, timestamp)`.

A bucket's `last_oi` is its value *at bucket end*, so the aggregate may only answer for buckets that have fully elapsed relative to the target — otherwise a 10:03:30 anchor would return the 10:03:59 tick, looking ahead of its own target. `oi_at_or_before(target)` therefore computes `boundary = time_bucket(interval, target)` and unions two `DISTINCT ON` branches: the aggregate for `bucket < boundary`, the raw hypertable for `[boundary, target]` (at most one bucket wide). The outer `DISTINCT ON … ORDER BY at DESC` keeps whichever is newer per token. The result is exactly what a pure raw scan would return, with the deep part of the scan served by pre-bucketed rows.

The aggregate is created with `materialized_only = false` (Timescale 2.13+ defaults it to true), so real-time aggregation covers rows written since the last refresh and the read path never has to reason about the materialization watermark.

The "token absent when it has no snapshot before target" contract is preserved by both branches — `DISTINCT ON` simply returns no row for such a token. `latest_chain_state` and `chain_day_open_oi` keep reading raw (they want the true newest/oldest tick, not a bucket).

*Alternative considered:* keep reading raw everywhere and rely on chunk exclusion alone. That helps, but a 15-minute-back anchor still scans the whole day's chunk; the aggregate turns it into a small indexed lookup and is the reason to take the Timescale dependency at all.

### Decision 5: Tests on a real Postgres, one database per session

`tests/conftest.py` and `apps/api/tests/conftest.py` gain a session-scoped fixture that connects to `ALGO_TEST_DATABASE_URL` (default `postgresql+psycopg://algo:algo@localhost:55432/algo`), creates `algo_test_<pid>`, runs bootstrap, yields the engine, and drops the database at teardown. A `make db-up` target (compose service published on `127.0.0.1:55432` — 5432 is commonly taken by another local Postgres) provides the instance; missing server = a hard failure with that instruction in the message, never a silent fallback.

One consequence of testing against real policies: fixtures write snapshots dated 2025-01-15, which the 30-day retention job would drop mid-run. The suite therefore exports `ALGO_CHAIN_RETENTION_DAYS`/`ALGO_CHAIN_COMPRESS_AFTER_DAYS` of ~100 years (`SchemaTuning` in `persistence/testing.py`) before any settings are constructed, and tests that assert on policy behavior bootstrap their own database with explicit values.

*Why not testcontainers:* an extra dependency and a Docker-in-test requirement for a repo that already ships a compose file. Per-session database (not per-test) keeps the suite fast; individual tests that need a clean slate truncate the tables they touch.

### Decision 6: Config surface

Remove `db_path`; make `database_url` required with a validator rejecting non-`postgres` URLs. Replace `chain_retention_days`' delete semantics with policy semantics under the same name, and add `chain_compress_after_days` (default 2), `chain_chunk_interval_days` (default 1), and `chain_agg_bucket_seconds` (default 60). Keep `db_connect_retries`.

## Risks / Trade-offs

- **Timescale image swap on a live volume fails or the extension is unavailable** → `timescale/timescaledb:latest-pg16` is the same PG16 major version and the same `PGDATA` path, so the volume mounts as-is; bootstrap creates the extension on first boot. Verified on a copy of the volume before touching production; rollback is switching the image tag back, since nothing in the base tables changed until hypertable conversion runs.
- **Hypertable conversion of a populated table locks the table** → conversion happens at startup, out of market hours, with `migrate_data => true`; the day's data volume is small enough (tens of MB) that the lock window is seconds. Deploy outside 09:15–15:30 IST.
- **Continuous aggregate returns a stale anchor near the watermark** → the watermark check routes tail reads to the raw table; a unit test asserts aggregate and raw paths return identical mappings for the same target.
- **Retention policy silently drops data a developer wanted** → retention is configured in days via `ALGO_CHAIN_RETENTION_DAYS` and logged at startup with its resolved value; default matches today's 30-day prune.
- **Losing SQLite makes local setup heavier** → one `make db-up`; the compose file already defines the service and the app containers already require it.
- **Two containers bootstrapping concurrently** → all DDL is idempotent and Postgres serializes it; worst case one side retries.

## Migration Plan

1. Land code with the Timescale image, mandatory `ALGO_DATABASE_URL`, bootstrap, and Postgres-only tests. No data migration is needed for containers.
2. On the server, outside market hours: `docker compose pull db`, `docker compose up -d db`, wait for healthy, then `up -d algo api` — first startup performs extension creation, PK fixup, hypertable conversion, and policy setup.
3. Verify: `timescaledb_information.hypertables` lists both tables, `jobs` lists the compression/retention/refresh policies, and the chain view renders 1/3/5/15-minute OI arrows identical to before.
4. Rollback: revert the image tag to `postgres:16-alpine` and the previous app image. Hypertable-converted tables are not readable by stock Postgres, so a rollback past step 2 requires restoring the pre-deploy `pgdata` snapshot — take one in step 2 before starting.
5. Developers: `make db-up` once; delete any local `data/algo.db`.

## Open Questions

- Should `audit_events` and `order_events` also become hypertables? They are append-only and time-ordered, but low-volume; deferred until retention on them is actually wanted.
- Retention default of 30 days on compressed chunks may be worth extending now that storage cost drops ~10×; confirm with the operator before changing the default.
