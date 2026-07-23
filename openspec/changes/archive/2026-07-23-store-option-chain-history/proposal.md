## Why

Traders reading the option chain need to see whether open interest is **building or unwinding** at each strike over short horizons — the classic 1/3/5/15-minute read that tells you if writers are adding or covering. Today the dashboard shows OI, LTP and a single change-in-OI figure, but that change is measured only against the **day-open** baseline, so it cannot answer "is OI rising or falling *right now*, over the last few minutes?". The websocket feed already persists per-strike snapshots to Postgres on a throttled cadence; that stored time series is enough to compute rolling-window trends, but nothing reads it that way yet.

## What Changes

Most of the storage and display pipeline already exists and is **reused unchanged**: the websocket → `OptionChainManager` → `SnapshotWriter` → `option_chain_snapshots` write path, the `GET /api/chain` endpoint, the SSE `/api/stream` push, and the dashboard Option Chain table. This change adds windowed trend computation on top.

- **Windowed OI trend engine**: for each strike (CE and PE), compute the OI trend over 1, 3, 5 and 15-minute windows by comparing current OI against the OI at `now − N minutes`, yielding an **Up / Down / Flat** direction plus the signed delta per window. Flat when the change is within a small threshold.
- **Repository query for point-in-time OI**: a new read that, per instrument token, returns the OI from the snapshot at or just before a target timestamp — the historical anchor each window diffs against.
- **Composite index** on `option_chain_snapshots (instrument_token, trading_day, timestamp)` so the windowed lookups stay fast as the day's rows accumulate.
- **API surface**: extend the per-strike chain response (`ChainStrikeOut` / `summarize_chain`) with CE and PE trend fields for each of the four windows. These flow automatically through both `GET /api/chain` and the SSE `/api/stream` payload (same builder).
- **Dashboard display**: extend the Option Chain table so each strike shows the live OI / ΔOI / LTP alongside compact 1/3/5/15-min OI trend arrows for both CE and PE, updating live off the existing SSE stream.
- **Snapshot cadence hardening**: confirm and, if needed, formalize the periodic-snapshot write cadence (latest-per-strike flushed on a fixed interval) so trend windows have regular, well-spaced anchor points rather than bursty gaps.

Non-goals: no change to how ticks are captured or normalized; no new broker calls; no per-tick raw persistence (snapshots stay periodic).

## Capabilities

### New Capabilities
- `option-chain-history`: the persisted per-strike option-chain time series — what is captured from the websocket (OI, change-in-OI vs day-open, LTP, volume per strike/side), the periodic write cadence, indexing, and retention that make it queryable for trends.
- `option-oi-trends`: rolling-window OI trend computation (1/3/5/15-minute Up/Down/Flat per strike/side), its exposure through the chain API and SSE stream, and its display in the dashboard chain table.

### Modified Capabilities
<!-- No existing specs in openspec/specs/; nothing to modify. -->

## Impact

- **DB**: `src/algo_trading/persistence/db.py` — new composite index on `OptionChainSnapshotRow`; schema stays SQLModel/`create_all` (no Alembic). Existing `option_chain_snapshots` table and columns are unchanged.
- **Persistence reads**: `src/algo_trading/persistence/repositories.py` — new point-in-time / windowed-OI query methods alongside the existing `latest_chain_state` and `chain_day_open_oi`.
- **Reporting**: `src/algo_trading/reporting.py` (`summarize_chain`) — add per-window trend fields to the pivoted per-strike output.
- **API**: `apps/api/app/schemas.py` (`ChainStrikeOut`), `apps/api/app/routes.py` / `build_stream_payload`, and `dashboard/state_bridge.py` (trend read wiring). No new endpoint required — extends `/api/chain` and `/api/stream`.
- **Web**: `apps/web/app/dashboard/page.tsx` (`OptionChainTable`) and `apps/web/lib/api.ts` types — new trend columns/arrows.
- **Config**: `src/algo_trading/config/settings.py` — trend window list and flat-threshold as settings (reuse `snapshot_min_interval_seconds` for cadence).
- **Tests**: extend `tests/test_option_chain_persistence.py` and reporting tests; new tests for windowed-trend math (boundary, missing-anchor, flat-threshold cases).
