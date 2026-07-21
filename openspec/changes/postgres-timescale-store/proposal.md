## Why

The persistence layer is dual-dialect: SQLite locally and in tests, PostgreSQL in Docker. That means every query must stay in the lowest common denominator, tests never exercise the backend that actually runs in production, and the fastest-growing table — `option_chain_snapshots`, an append-only tick series written every few seconds per option token — sits in a plain heap table with no partitioning, compression, or retention beyond a manual `prune_snapshots` delete. The rolling 1/3/5/15-minute OI trends already scan that table on every chain refresh, and the scan cost grows linearly with the trading day.

## What Changes

- **BREAKING**: Remove SQLite entirely. `ALGO_DATABASE_URL` becomes required and must be a PostgreSQL URL; `ALGO_DB_PATH`, `create_db_engine()`, and the SQLite fallback in `resolved_database_url()` are deleted. Startup fails fast with a clear error on a missing or non-Postgres URL.
- Run PostgreSQL with the **TimescaleDB** extension (compose image swapped to `timescale/timescaledb:latest-pg16`) and treat time-series tables as first-class.
- Convert `option_chain_snapshots` to a **hypertable** partitioned on `timestamp`, with a compression policy on chunks older than a configured age and a retention policy replacing the hand-rolled `prune_snapshots` delete.
- Add a **continuous aggregate** for per-token OI/LTP buckets so the rolling OI-trend anchor lookups (`oi_at_or_before`, `oi_anchors_for_windows`) read pre-bucketed rows instead of scanning raw ticks, with the raw table as fallback for the newest, not-yet-materialized bucket.
- Also make `pnl_snapshots` a hypertable (append-only, time-ordered, same access pattern).
- Introduce a **schema/migration bootstrap** step that creates the extension, converts tables to hypertables, and (re)applies compression/retention/continuous-aggregate policies idempotently at startup — `SQLModel.metadata.create_all` alone can no longer express the schema.
- **Local dev and tests run against PostgreSQL/TimescaleDB.** `tests/conftest.py` and `apps/api/tests/conftest.py` provision a throwaway database per test session against a compose-provided instance; `tests/test_database_config.py` and `tests/test_dashboard_bridge.py` drop their SQLite assumptions.
- Make `psycopg` a required dependency rather than a `[postgres]` extra; document the local Postgres bring-up in the README and `make` targets.

## Capabilities

### New Capabilities
- `timeseries-store`: PostgreSQL/TimescaleDB as the sole persistence backend — connection/config contract, schema bootstrap and policy management, hypertable layout for snapshot series, compression/retention behavior, and the continuous-aggregate-backed read path for rolling OI windows.

### Modified Capabilities
<!-- None: openspec/specs/ is empty, so there are no existing capability specs to delta. -->

## Impact

- **Code**: `src/algo_trading/persistence/db.py` (engine construction, schema bootstrap), `src/algo_trading/persistence/repositories.py` (snapshot writes, `oi_at_or_before`, `oi_anchors_for_windows`, `chain_day_open_oi`, `prune_snapshots`), `src/algo_trading/config/settings.py` (`database_url`, `db_path`, new retention/compression/bucket settings), `src/algo_trading/dashboard/state_bridge.py`, `src/algo_trading/core/orchestrator.py`, `src/algo_trading/tools/import_orders.py`, `src/algo_trading/tools/import_trades.py`.
- **Tests**: `tests/conftest.py`, `apps/api/tests/conftest.py`, `tests/test_database_config.py`, `tests/test_dashboard_bridge.py`, `tests/test_option_chain_persistence.py`, `tests/test_persistence.py` — plus every test that builds an engine from a tmp path.
- **Deployment**: `docker-compose.yml` (Timescale image, named volume reuse for the existing `pgdata`), `deploy/`, `Dockerfile`/`apps/api/Dockerfile` (psycopg no longer optional), `.env` templates (`ALGO_DATABASE_URL` mandatory, `ALGO_DB_PATH` removed).
- **Operations**: existing deployments keep their data (the extension is added in place on the same PG16 volume); any developer relying on a local `data/algo.db` must move to a local Postgres container. Existing SQLite files are not migrated.
- **Docs**: `README.md` architecture diagram and the persistence/go-live sections.
