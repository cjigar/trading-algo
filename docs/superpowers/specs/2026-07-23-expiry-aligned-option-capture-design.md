# Expiry-aligned weekly option capture (NIFTY + SENSEX, ±20 strikes)

**Date:** 2026-07-23
**Status:** Approved design, ready for implementation plan

## Goal

Continuously capture the option chain for **both NIFTY and SENSEX** across the entire
trading week, for a **±20-strike band (CE + PE)** that follows spot, and manage storage by
**deleting each week's data once its expiry passes** — not by a rolling calendar window.

The lifecycle mirrors the weekly contract cycle:

- **NIFTY** — weekly expiry Tuesday; new contract from Wednesday. On the Wednesday roll the
  just-expired week's snapshots are purged.
- **SENSEX** — weekly expiry Thursday; new contract from Friday. On the Friday roll its
  just-expired week is purged.

Because the two indices expire on **different days** but share the same daily storage
partitions, purge must be keyed by `(underlying, expiry)`, not by time.

## Current state (baseline)

- **Capture is NIFTY-only** — `oi_underlyings` defaults to `[NIFTY]`; SENSEX is configured
  for trading days but never captured into `option_chain_snapshots`.
- **Window is ±5 and follows spot** — `strike_window = 5` (11 strikes × CE/PE = 22 contracts);
  `OptionChainManager._rewindow` re-centres on ATM as spot moves.
- **Retention is rolling 30-day time policy** — `chain_retention_days = 30`, a TimescaleDB
  drop-chunks policy. There is **no `expiry` column** on `option_chain_snapshots`.
- Snapshot rows are written from `OptionChainManager.on_option_tick` (`feed/option_chain.py`),
  and `Instrument.expiry: date` (`domain/models.py:36`) is already in scope at write time.
- Chunks are partitioned on `timestamp` only (one chunk per trading day), so a single day's
  chunk holds **both** NIFTY and SENSEX rows.

## Chosen approach: row-level DELETE by expiry (Approach A)

Add an `expiry` column, stamp it on every snapshot, and purge with a single row-level
`DELETE ... WHERE expiry < today`. Each index self-purges the morning after its own expiry
because its expiry date crosses `< today` then — no per-underlying special-casing.

### Rejected alternatives

- **B. Time-based chunk retention only.** Incorrect: daily chunks hold both indices, so
  dropping chunks after NIFTY's Tuesday expiry would delete SENSEX rows whose Thursday expiry
  is still live.
- **C. Per-underlying space partitioning + chunk drop.** Correct but over-engineered for a
  ~100 MB store; adds partitioning complexity for no real benefit.

## Design

### 1. Schema — add `expiry` to the snapshot row

- Add `expiry: date` (indexed) to `OptionChainSnapshotRow` in `persistence/db.py`.
- `persistence/bootstrap.py` `_ensure_chain_columns` gains an idempotent
  `ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS expiry ...` so existing
  databases upgrade in place.
- Legacy rows get `NULL` expiry. The purge treats `NULL` as "unknown" and leaves them to the
  time-based backstop (below) to reap.

### 2. Write path — stamp the expiry

- In `OptionChainManager.on_option_tick` (`feed/option_chain.py`), add
  `"expiry": inst.expiry` to the snapshot dict. `inst.expiry` is already on the resolved
  `Instrument` — no new lookups.

### 3. Capture scope — both indices, ±20 (settings only, no code)

- `oi_underlyings = [NIFTY, SENSEX]` (was `[NIFTY]`).
- `chain_feed_window = 20` (was `0`, i.e. = `strike_window`). This widens the **capture/view**
  window to ±20 while leaving `strike_window = 5` (the strategy's OI-aggregation band)
  untouched — `Settings.feed_window()` already models this split.

### 4. Retention — replace rolling-30-day with expiry purge

- New repository method in `persistence/repositories.py`:
  `purge_expired_chain_snapshots(today) ->` executes
  `DELETE FROM option_chain_snapshots WHERE expiry IS NOT NULL AND expiry < :today`.
  One statement covers both underlyings; each self-purges after its own expiry.
- `bootstrap.py`: **disable compression** on the chain table (`_ensure_compression` becomes a
  no-op and removes any existing policy) — data lives < 1 week, so compress-then-row-delete is
  wasteful and complicates deletes.
- Set the time-based retention to a **14-day backstop** (`chain_retention_days` default
  30 → 14) so anything the purge misses (e.g. NULL-expiry legacy rows) still ages out.
- New setting `chain_retention_mode: "expiry"` (default) vs `"days"` to keep the old
  time-only behavior available.

### 5. Purge trigger

- In the `run_capture` loop (`entrypoints/run_capture.py`), call
  `purge_expired_chain_snapshots(date.today())` once at startup and once per **day-rollover**,
  guarded by a stored "last purge date" so it runs as a single cheap idempotent DELETE per day.
  This is the "Wednesday morning" (and "Friday morning" for SENSEX) cleanup.

### 6. Continuous aggregate

- `chain_oi_1m` sits on top of the hypertable; DELETEs invalidate its old buckets. Its own
  retention trims old buckets, so purged data's aggregate buckets age out naturally. Left as-is;
  noted as a low-risk follow-up if stale buckets ever surface.

## Feed capacity — confirmed within limit

Kotak Neo websocket cap: **max 200 scrips per subscribe request** (official doc).

- ±20 × (CE + PE) = **82 contracts per index**
- × 2 indices = **164 option scrips** + 2 index spot feeds = **166 scrips concurrent**
- **166 ≤ 200** — ~34 scrips (17%) headroom.

Two supporting facts:

- The window follows spot, but the live set stays at ~164 options (old strikes drop as new
  ones enter via `_rewindow`) — it does **not** grow across the week.
- Subscription is **incremental** (one token per `_rewindow` call), so we are never near the
  200-per-request cap either; 166 is the concurrent total.

Fallback: window size is just `chain_feed_window`, so dialing back to ±10 (84 option scrips)
is a one-setting change if Kotak's real-world behavior differs.

## Verification / monitor steps

- **Live-market validation run** of `run_capture` with both indices at ±20: confirm all 166
  subscriptions are accepted and quotes stream for the full band (no silent drops).
- Confirm the `expiry` column is populated for new rows and that
  `purge_expired_chain_snapshots` deletes exactly the expired week per underlying while leaving
  the live week (both indices) intact.
- Confirm the 14-day backstop retention and disabled compression are reconciled correctly by
  `bootstrap_schema` on an existing database (idempotent restart is a no-op).

## Out of scope

- No change to the OI-selling **strategy** logic or its ±5 aggregation band.
- No change to order placement — capture remains read-only (`run_capture` wires a PaperBroker).
- No backfill of `expiry` for pre-existing NULL rows; the backstop retention handles them.
