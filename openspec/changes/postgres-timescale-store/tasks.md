## 1. Test harness on PostgreSQL

- [x] 1.1 Add a `db-up` / `db-down` make target that runs the compose `db` service with 5432 published locally
- [x] 1.2 Add a session-scoped `pg_engine` fixture in `tests/conftest.py`: read `ALGO_TEST_DATABASE_URL`, create `algo_test_<pid>`, run bootstrap, yield the engine, drop the database at teardown; fail with a "run `make db-up`" message when no server is reachable
- [x] 1.3 Mirror the same fixture in `apps/api/tests/conftest.py` (or share it from a common helper)
- [x] 1.4 Convert every test that builds an engine from a tmp path (`tests/test_persistence.py`, `tests/test_option_chain_persistence.py`, `tests/test_dashboard_bridge.py`, `tests/test_import_orders.py`, `tests/test_import_trades.py`, and the rest surfaced by grepping `create_db_engine`) to use the fixture, truncating the tables they touch

## 2. Remove SQLite from configuration

- [x] 2.1 Delete `db_path` from `Settings`; make `database_url` required and add a validator rejecting non-PostgreSQL URLs with a clear message
- [x] 2.2 Add `chain_compress_after_days` (2), `chain_chunk_interval_days` (1), `chain_agg_bucket_seconds` (60); keep `chain_retention_days` and `db_connect_retries`
- [x] 2.3 Delete `create_db_engine()` and the SQLite branches in `create_engine_from_url` / `create_engine_from_settings` in `persistence/db.py`
- [x] 2.4 Update call sites: `core/orchestrator.py`, `dashboard/state_bridge.py`, `tools/import_orders.py`, `tools/import_trades.py`
- [x] 2.5 Rewrite `tests/test_database_config.py` to cover: valid Postgres URL accepted, blank URL rejected, `sqlite:///` URL rejected, connect retries honored

## 3. Hypertable schema

- [x] 3.1 Change the primary key of `OptionChainSnapshotRow` and `PnlSnapshotRow` to composite `(timestamp, id)` with `id` sequence-backed, keeping the existing secondary indexes
- [x] 3.2 Create `persistence/bootstrap.py` with an idempotent `bootstrap_schema(engine, settings)`: `CREATE EXTENSION IF NOT EXISTS timescaledb` → `SQLModel.metadata.create_all` → PK fixup for pre-existing tables → `create_hypertable(..., migrate_data => true, if_not_exists => true)` on both snapshot tables with the configured chunk interval
- [x] 3.3 Wire `bootstrap_schema` into `create_engine_from_settings` in place of the bare `create_all`, keeping the connect-retry loop
- [x] 3.4 Test: bootstrap on an empty database creates both hypertables; a second run is a no-op; a table populated before conversion keeps every row

## 4. Compression, retention, and the continuous aggregate

- [x] 4.1 In bootstrap, enable compression on `option_chain_snapshots` (segment by `instrument_token`, order by `timestamp DESC`) and add the compression policy at `chain_compress_after_days`
- [x] 4.2 Add the retention policy at `chain_retention_days`; when an existing policy's interval differs from the configured one, drop and re-add it (look it up in `timescaledb_information.jobs`)
- [x] 4.3 Create the `chain_oi_1m` continuous aggregate (`time_bucket`, `last(oi, timestamp)`, `last(ltp, timestamp)`, grouped by bucket/token/trading_day/underlying) plus its refresh policy
- [x] 4.4 Delete `prune_snapshots` and remove its scheduled call
- [x] 4.5 Test: policies are registered with the configured intervals; changing a configured interval and re-bootstrapping updates rather than duplicates the policy; a compressed chunk still answers anchor queries correctly

## 5. Read path

- [x] 5.1 Rewrite `oi_at_or_before` to `SELECT DISTINCT ON (instrument_token) … ORDER BY instrument_token, <time> DESC`, choosing the `chain_oi_1m` aggregate or the raw hypertable by comparing the target to the materialization watermark
- [x] 5.2 Keep `oi_anchors_for_windows` batching one query per window over the new implementation
- [x] 5.3 Rewrite `latest_chain_state` and `chain_day_open_oi` with `DISTINCT ON` against the raw hypertable (drop the `max(id)` subqueries)
- [x] 5.4 Test: aggregate path and raw path return identical mappings for the same target; a token whose first snapshot is after the target is omitted (not `0`); 1/3/5/15-minute windows each resolve against their own target

## 6. Packaging and deployment

- [x] 6.1 Move `psycopg[binary]` from the `postgres` extra into required dependencies and drop the extra
- [x] 6.2 Switch the compose `db` image to `timescale/timescaledb:latest-pg16`, keeping the `pgdata` volume and healthcheck (the `-ha` variant relocates `PGDATA` and orphans the volume)
- [x] 6.3 Remove `ALGO_DB_PATH` from `.env` templates and `deploy/`; make `ALGO_DATABASE_URL` explicit for every service
- [x] 6.4 Update `README.md`: architecture diagram, persistence section, local `make db-up` setup, go-live/deploy checklist including the pre-deploy `pgdata` snapshot and the out-of-market-hours deploy window

## 7. Verification

- [x] 7.1 Run the full suite plus `ruff` and `mypy` against the Postgres-backed harness
- [ ] 7.2 On a copy of the production volume, run the image swap and first-boot bootstrap; confirm `timescaledb_information.hypertables` and `jobs` list the expected entries and no rows were lost
- [ ] 7.3 Compare chain-view 1/3/5/15-minute OI arrows before and after the change on the same captured data

## 8. OI carry-forward (found while validating against the live feed)

- [x] 8.1 Filter NULL-OI rows out of both branches of the anchor query and order the aggregate branch by the OI reading's own timestamp (`oi_at`)
- [x] 8.2 Redefine `chain_oi_1m` with `last(oi, timestamp) FILTER (WHERE oi IS NOT NULL)` plus an `oi_at` column; bootstrap drops and recreates the pre-fix view when the column is missing
- [x] 8.3 `latest_chain_state` reports the newest row's LTP with the last known OI; `chain_day_open_oi` uses the day's first row that carries an OI
- [x] 8.4 Tests: carry-forward over LTP-only ticks, NULL preserved when OI was never reported, day-open baseline skips NULL rows, anchor skips NULL rows
