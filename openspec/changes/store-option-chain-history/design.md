## Context

The websocket → persistence → display pipeline for the option chain **already exists** and works in production:

- **Write path**: `LiveFeedCoordinator._dispatch` → `FeedHandler.handle_raw` → `orchestrator._handle_tick` → `OptionChainManager.on_option_tick` → `SnapshotWriter` (buffered, per-token min-interval dedup) → `Repository.write_chain_snapshots` → `option_chain_snapshots` (SQLModel table, append-only, columns: `trading_day`, `underlying`, `strike`, `option_type`, `instrument_token`, `oi`, `ltp`, `volume`, `timestamp`).
- **Read path**: `StateBridge` → `Repository.latest_chain_state` (latest per token via `max(id)`) and `chain_day_open_oi` (first-of-day OI via `min(id)`), pivoted by `reporting.summarize_chain` into per-strike CE/PE with `chg_oi` **vs day-open only**.
- **Serve path**: `GET /api/chain` (`ChainOut`/`ChainStrikeOut`) and SSE `GET /api/stream` (`build_stream_payload`, ~3s cadence) — both use the same `chain_out` builder.
- **Display**: dashboard Option Chain tab, `OptionChainTable`, currently polling `api.chain()` every 5s and also receiving the chain over SSE.

The gap is narrow: there is **no rolling-window OI trend**. `chg_oi` is the only "change" and it is anchored to the day open (`reporting.py:96`, `repositories.py:171-188`). The `option_chain_snapshots` time series already holds enough data to compute 1/3/5/15-min trends; it just is not read that way, and the table lacks a composite index for point-in-time-per-token lookups (only single-column indexes on `db.py:145-153`).

Constraints: SQLModel + `create_all` (no Alembic) must stay portable across SQLite (tests/local) and Postgres (containers); the `algo` container writes, the `api` container reads the same DB; the browser receives data only via SSE + REST (no browser websocket).

## Goals / Non-Goals

**Goals:**
- Compute per-strike, per-side OI trend (Up/Down/Flat + signed delta) over configurable windows (default 1/3/5/15 min) from the existing snapshot history.
- Surface trends through the existing `/api/chain` and `/api/stream` (no new endpoint) and render them in the existing chain table, live off SSE.
- Keep the day-open `chg_oi` reading intact.
- Add the composite index and a point-in-time OI read so window queries stay cheap.

**Non-Goals:**
- No change to tick capture/normalization or broker calls.
- No per-tick raw persistence — snapshots stay periodic.
- No new streaming transport (SSE stays; no browser websocket).
- No Alembic/migration framework — index added via `create_all`.
- No historical backfill UI or charting beyond the four trend arrows.

## Decisions

**1. Compute trends server-side in `summarize_chain`, not in the browser.**
The pivot and day-open `chg_oi` already live in `reporting.summarize_chain`; adding window trends there means both `/api/chain` and `/api/stream` get them for free via the shared `chain_out` builder, and the browser stays a thin renderer. Alternative (compute in the web client from a stream of raw snapshots) rejected: it would require shipping history to every client and duplicating the anchor logic in TypeScript.

**2. Trend basis = current OI − OI at `now − N` (point-in-time anchor).**
Direction is the sign of that difference against a flat threshold. Chosen for simplicity and because it directly answers "is OI building or unwinding over this window". Alternatives considered: linear-regression slope (smoother but heavier and harder to explain) and summed ΔOI over the window (risks double-counting if broker ΔOI is cumulative). The point-in-time diff needs only one extra row lookup per token per window.

**3. Anchor = latest snapshot at-or-before the target time.**
A new repository method returns, per token, the OI of the most recent snapshot with `timestamp <= now − N`. Using at-or-before (not nearest) keeps the read a simple indexed `ORDER BY timestamp DESC LIMIT 1` per token and never anchors on a future point. When no row precedes the target (early session / newly-windowed strike), the window is **unavailable** — explicitly distinct from Flat (spec: `option-oi-trends` missing-history requirement).

**4. Batch the anchor reads per underlying, not per-strike-per-window.**
For W windows × T tokens, naive per-cell queries are W×T round trips. Instead fetch, per underlying, the snapshot rows for the day once (or a bounded time-ordered slice covering the max window) and resolve all anchors in memory; or issue one grouped query per window. Decision: start with one grouped query per window over the windowed tokens (4 queries/underlying/refresh), backed by the composite index; revisit to a single time-slice fetch if the ~3s stream cadence shows DB pressure. Keeps SQL simple and correct first.

**5. Composite index `(instrument_token, trading_day, timestamp)` via SQLModel.**
Added as a table-level index on `OptionChainSnapshotRow` so it is created by `create_all` on both SQLite and Postgres — no Alembic. Existing single-column indexes stay. This is the index the at-or-before-per-token lookup needs.

**6. Periodic cadence reuses `SnapshotWriter` + `snapshot_min_interval_seconds`.**
The user asked for periodic snapshots; the writer already flushes on buffer/interval with per-token min-interval dedup. We keep it and treat `snapshot_min_interval_seconds` as the anchor-spacing knob. No new writer. If the 1-min window needs finer anchors than the current interval, that becomes a config change, not code.

**7. Windows and flat threshold are settings.**
`settings.py` gains a window list (default `[1,3,5,15]` minutes) and an OI flat threshold (absolute contracts, default small non-zero). Exposed so operators tune horizons/sensitivity without redeploy, per the `option-oi-trends` configurability requirement.

**8. Response shape: nested per-window trend on each side.**
`ChainStrikeOut` gains `ce_oi_trends` / `pe_oi_trends` as a compact map `{ "1m": {dir, delta}, "3m": ..., "5m": ..., "15m": ... }` (dir ∈ up/down/flat/na). Nested keeps the four windows grouped and lets the window set vary without schema churn; the web `ChainStrike` type mirrors it.

## Risks / Trade-offs

- **[Sparse anchors make short windows noisy]** → If `snapshot_min_interval_seconds` is coarse relative to the 1-min window, the 1-min anchor may be the same row as a longer window, flattening it. Mitigation: document the relationship; allow tuning the interval; report unavailable when no row precedes the target rather than faking Flat.
- **[Broker ΔOI/OI semantics]** → We rely on absolute OI per snapshot (already stored); if a feed sends cumulative or resetting OI, diffs mislead. Mitigation: trends use stored absolute OI consistently with the existing day-open `chg_oi`; no new assumption beyond what already ships.
- **[Query cost at stream cadence]** → 4 grouped queries/underlying every ~3s. Mitigation: composite index; option to collapse to one time-slice fetch (Decision 4) if profiling warrants.
- **[Index build on large existing tables]** → `create_all` adds the index; on a big Postgres table the first create may lock briefly. Mitigation: off-hours deploy (market closed), or create the index `CONCURRENTLY` by hand before the code lands if the table is large.
- **[SSE payload size grows]** → Four windows × two sides × many strikes enlarges each stream frame. Mitigation: compact keys and short dir codes; windows configurable to fewer if needed.
- **[Timezone/trading-day edges]** → Anchors cross the day-open boundary awkwardly at session start. Mitigation: unavailable-when-no-prior-row rule; windows naturally fill in as the session progresses.

## Migration Plan

1. Add the composite index to `OptionChainSnapshotRow`; deploy so `create_all` applies it (prefer market-closed window; for a large Postgres table, pre-create `CONCURRENTLY`).
2. Add repository point-in-time / windowed-OI read methods with tests (SQLite).
3. Extend `summarize_chain` + `ChainStrikeOut` with trend fields; verify `/api/chain` and `/api/stream` carry them.
4. Extend `OptionChainTable` + web types to render arrows off SSE.
5. Add settings (window list, flat threshold).
6. Rollback: trend fields are additive; reverting the reporting/schema/web changes restores prior behavior. The index is harmless to leave in place; drop manually if desired. No data migration, so rollback is code-only.

## Open Questions

- Should the 1-min window be dropped or the snapshot interval tightened if `snapshot_min_interval_seconds` is currently coarser than 60s? (Resolve by reading the deployed value before shipping.)
- Do we want a per-strike ATM-relative filter on which strikes get trend arrows (all windowed strikes vs only the OI band) to bound payload size? Default: all windowed strikes.
- Should "Flat" and "unavailable" be visually distinct in the table, or share a neutral glyph? Spec requires unavailable ≠ false-direction; exact glyph is a UI detail for apply.
