## ADDED Requirements

### Requirement: PostgreSQL is the only supported backend
The system SHALL use PostgreSQL as its sole persistence backend. `ALGO_DATABASE_URL` SHALL be required and SHALL be a PostgreSQL SQLAlchemy URL. The system SHALL NOT provide a SQLite fallback, and `ALGO_DB_PATH` SHALL NOT exist.

#### Scenario: Postgres URL configured
- **WHEN** `ALGO_DATABASE_URL` is set to a `postgresql+psycopg://…` URL and the server is reachable
- **THEN** the engine is created against that URL and the process starts normally

#### Scenario: Missing database URL
- **WHEN** the process starts with `ALGO_DATABASE_URL` unset or blank
- **THEN** startup fails with a configuration error naming `ALGO_DATABASE_URL`, and no engine is created

#### Scenario: Non-Postgres URL rejected
- **WHEN** `ALGO_DATABASE_URL` is set to a non-PostgreSQL URL (e.g. `sqlite:///data/algo.db`)
- **THEN** startup fails with an error stating that only PostgreSQL URLs are supported

#### Scenario: Database not yet accepting connections
- **WHEN** the database is still booting and refuses connections
- **THEN** the system retries up to `ALGO_DB_CONNECT_RETRIES` times with backoff before failing

### Requirement: TimescaleDB extension and idempotent schema bootstrap
On startup the system SHALL ensure the `timescaledb` extension exists, create all tables, convert the designated time-series tables to hypertables, and apply their compression, retention, and continuous-aggregate policies. Bootstrap SHALL be idempotent: running it against an already-initialized database SHALL succeed and make no destructive change.

#### Scenario: Fresh database
- **WHEN** bootstrap runs against an empty database
- **THEN** the extension, all tables, the hypertables, the continuous aggregate, and all policies exist afterwards

#### Scenario: Repeat startup
- **WHEN** bootstrap runs a second time against the same database
- **THEN** it completes without error and existing chunks, data, and policies are unchanged

#### Scenario: Extension unavailable
- **WHEN** the connected PostgreSQL server cannot provide the `timescaledb` extension
- **THEN** startup fails with an error identifying the missing extension rather than silently degrading

### Requirement: Snapshot series stored as hypertables
`option_chain_snapshots` and `pnl_snapshots` SHALL be TimescaleDB hypertables partitioned on their `timestamp` column with a configured chunk interval. Any unique or primary key on these tables SHALL include `timestamp`. Existing rows SHALL be preserved when an existing plain table is converted.

#### Scenario: Writes land in time chunks
- **WHEN** option-chain snapshots are written across a trading day
- **THEN** rows are stored in time-partitioned chunks and remain readable through the existing repository methods

#### Scenario: Conversion of a populated table
- **WHEN** bootstrap converts an existing populated `option_chain_snapshots` table to a hypertable
- **THEN** every pre-existing row is still queryable after conversion

### Requirement: Compression and retention policies replace manual pruning
The system SHALL apply a compression policy that compresses chunks older than `ALGO_CHAIN_COMPRESS_AFTER_DAYS` and a retention policy that drops chunks older than `ALGO_CHAIN_RETENTION_DAYS`. The manual `prune_snapshots` delete SHALL be removed from the scheduled maintenance path.

#### Scenario: Old data aged out
- **WHEN** a chunk contains only data older than the retention window and the retention policy runs
- **THEN** the chunk is dropped and its rows are no longer returned by queries

#### Scenario: Compressed data still readable
- **WHEN** a chunk has been compressed by the compression policy
- **THEN** rolling OI-window queries over that period still return the same values as before compression

#### Scenario: Policy settings changed
- **WHEN** the configured retention or compression window changes and the process restarts
- **THEN** bootstrap updates the existing policy to the new window instead of creating a duplicate

### Requirement: Rolling OI anchors read from a continuous aggregate
Point-in-time OI anchor lookups (`oi_at_or_before`, `oi_anchors_for_windows`) SHALL read from a continuous aggregate that buckets OI per `instrument_token` per time bucket, using the aggregate only for buckets that closed at or before the target time and the raw hypertable for the remaining tail. Results SHALL keep today's contract: the value is the OI of that token's latest snapshot at or before the target time, and a token with no snapshot before the target SHALL be omitted from the result.

#### Scenario: Anchor inside aggregated history
- **WHEN** an anchor is requested for a target time whose preceding buckets are covered by the continuous aggregate
- **THEN** the aggregate supplies the value and the returned OI equals the latest snapshot's OI at or before that target

#### Scenario: Anchor inside the bucket containing the target
- **WHEN** the target falls partway through a bucket, so that bucket's aggregated value would reflect data written *after* the target
- **THEN** the raw hypertable supplies the value for that tail and no reading later than the target is returned

#### Scenario: Token with no prior snapshot
- **WHEN** a token's first snapshot is later than the requested target time
- **THEN** that token is absent from the returned mapping (not present with OI `0`)

#### Scenario: Multiple windows in one call
- **WHEN** anchors are requested for the 1/3/5/15-minute windows at once
- **THEN** each window returns its own token→OI mapping resolved against its own target time

### Requirement: OI reads use the latest reported OI, not the latest row
The broker sends open interest in a token's first full packet and NULL in the LTP-only ticks that follow. Chain reads SHALL therefore resolve OI from the most recent snapshot that carries one: `latest_chain_state` SHALL report the newest row's LTP with the last known OI, `chain_day_open_oi` SHALL use the day's first snapshot that carries an OI, and the anchor lookups SHALL ignore NULL-OI rows in both the aggregate and raw branches. A token that has never reported an OI SHALL keep a NULL OI rather than being reported as zero.

#### Scenario: LTP-only ticks follow an OI reading
- **WHEN** a token reports OI 1000 at 10:00 and then LTP-only ticks at 10:01 and 10:02
- **THEN** the current chain state shows the 10:02 LTP together with OI 1000

#### Scenario: Anchor lands on an LTP-only tick
- **WHEN** an anchor is requested for a target whose nearest preceding row has a NULL OI
- **THEN** the last OI actually reported before the target is returned, not zero

#### Scenario: Token has never reported OI
- **WHEN** every snapshot for a token has a NULL OI
- **THEN** its OI is reported as unknown (NULL), not as zero

### Requirement: Tests and local development run on PostgreSQL
The test suite and local development SHALL run against a PostgreSQL/TimescaleDB instance. Each test session SHALL use an isolated database or schema that is created before and dropped after the run, so tests never share state with a developer's working database.

#### Scenario: Test session isolation
- **WHEN** the test suite runs
- **THEN** it provisions its own database/schema, applies the full bootstrap, and removes it at the end

#### Scenario: No database available
- **WHEN** the test suite is started with no reachable PostgreSQL instance
- **THEN** it fails with a message explaining how to start the local database, rather than falling back to another backend

## REMOVED Requirements

### Requirement: SQLite fallback backend
**Reason**: Dual-dialect support forced lowest-common-denominator SQL, left tests running on a backend that is never used in production, and blocks time-series features (hypertables, compression, retention, continuous aggregates).
**Migration**: Set `ALGO_DATABASE_URL` to a PostgreSQL URL and start the compose `db` service; `ALGO_DB_PATH` and `create_db_engine()` are removed. Existing local `data/algo.db` files are not migrated — they hold local dev/audit data only. Containerized deployments already use PostgreSQL and need no data migration.
