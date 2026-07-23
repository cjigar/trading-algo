## 1. Persistence: index and point-in-time reads

- [x] 1.1 Add a composite index `(instrument_token, trading_day, timestamp)` to `OptionChainSnapshotRow` in `src/algo_trading/persistence/db.py` (table-level `Index`, created by `create_all`; keep existing single-column indexes).
- [x] 1.2 Add a repository method in `src/algo_trading/persistence/repositories.py` that returns, per instrument token, the OI of the latest snapshot with `timestamp <= target_time` for a given trading_day/underlying (the at-or-before anchor), returning an explicit "no anchor" marker when none precedes the target.
- [x] 1.3 Add a repository method that resolves anchors for a set of windows in one grouped read per window over the windowed tokens (batched, per Decision 4).
- [x] 1.4 Unit tests (SQLite) for 1.2/1.3: exact-boundary timestamp, target before earliest row (unavailable), multiple tokens, and correct latest-before selection.

## 2. Config: windows and threshold

- [x] 2.1 Add settings to `src/algo_trading/config/settings.py`: OI trend window list (default `[1, 3, 5, 15]` minutes) and an OI flat threshold (absolute contracts, small non-zero default).
- [x] 2.2 Verify `snapshot_min_interval_seconds` is compatible with the shortest window; document the anchor-spacing relationship in the setting's comment.

## 3. Trend computation in reporting

- [x] 3.1 Add a pure function (e.g. in `src/algo_trading/reporting.py` or a helper module) that, given current OI and an anchor OI (or "no anchor") and the flat threshold, returns direction (up/down/flat/na) and signed delta.
- [x] 3.2 Extend `summarize_chain` to compute CE and PE trends for each configured window per strike, using the batched anchor reads, without altering the existing day-open `chg_oi`.
- [x] 3.3 Unit tests for the trend function: up, down, within-threshold flat, missing-anchor na, and multi-window output.
- [x] 3.4 Test that `summarize_chain` output includes all configured windows for both sides and preserves day-open `chg_oi`.

## 4. API surface

- [x] 4.1 Extend `ChainStrikeOut` in `apps/api/app/schemas.py` with `ce_oi_trends` / `pe_oi_trends` as a compact per-window map `{window: {dir, delta}}` (dir ∈ up/down/flat/na).
- [x] 4.2 Wire the trend reads through `dashboard/state_bridge.py` so `chain_out` populates the new fields for both the polled and streamed paths.
- [x] 4.3 Confirm `GET /api/chain` returns trend fields; add/extend an API test asserting their presence and shape.
- [x] 4.4 Confirm `build_stream_payload` (SSE `/api/stream`) carries identical trend fields; add/extend a test.

## 5. Web display

- [x] 5.1 Extend the `ChainStrike` (and related) types in `apps/web/lib/api.ts` to include the per-window CE/PE trend map.
- [x] 5.2 Extend `OptionChainTable` in `apps/web/app/dashboard/page.tsx` to render compact 1/3/5/15-min OI trend indicators (Up/Down/Flat) for CE and PE per strike, alongside existing OI/ΔOI/LTP, with the ATM row marked.
- [x] 5.3 Render unavailable (`na`) windows as a neutral placeholder distinct from a directional arrow.
- [x] 5.4 Ensure the table updates live from the SSE stream payload (not only the 5s poll).

## 6. Verification and rollout

- [x] 6.1 Run the full Python test suite and web build/lint; confirm green. (145 Python tests pass; `tsc --noEmit` clean; ruff clean.)
- [x] 6.2 Manually verify against the live snapshot data that trends match hand-computed OI diffs for a sample strike across the four windows. _(Verified on live prod 2026-07-23: NIFTY 23600 CE, current OI 522,860 — anchors 1m=525,070 (down), 3m=521,885 (up), 5m=519,025 (up), 15m=506,805 (up); all four windows resolved to the latest non-null OI at-or-before `now − w`, matching the trend function's direction classification.)_
- [x] 6.3 Deploy during a market-closed window; if the Postgres `option_chain_snapshots` table is large, pre-create the composite index `CONCURRENTLY` before the code lands (per Migration Plan). _(Done 2026-07-23: the app code was already deployed, but the composite index `ix_chain_snap_token_day_ts` had never landed — `create_all` skips indexes on pre-existing tables. Created it on prod with `timescaledb.transaction_per_chunk` — TimescaleDB rejects `CONCURRENTLY` on hypertables — non-locking per-chunk; verified present on the parent and all 6 chunks. Root cause fixed: `bootstrap.py` now self-heals declared indexes via `_ensure_declared_indexes`, with a regression test.)_
- [x] 6.4 After deploy, confirm the dashboard chain shows live arrows and unavailable windows fill in as the session progresses. _(Confirmed 2026-07-23 during a live session: `GET /api/chain` serves 200 continuously with `ce_oi_trends`/`pe_oi_trends` wired through, all four windows resolve against live data (see 6.2), and no read-path errors in the api/algo logs.)_
