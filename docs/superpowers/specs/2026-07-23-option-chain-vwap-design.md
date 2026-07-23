# Per-option VWAP in the option chain

## Context

The OI-selling operator reads the option chain to decide entries/exits. The chain currently shows
per-strike OI, change-in-OI, LTP, and rolling OI-trend arrows — but not VWAP, so there is no way
to see whether a call/put premium is trading above or below its volume-weighted average for the
session. Traders use LTP-vs-VWAP as a quick momentum/fair-value read.

The engine already computes exactly this value: `OptionChainManager` maintains a per-option
session `TickVWAP` (volume-delta weighted, reset at session start) and exposes `vwap_for(token)`.
It is used for the VWAP-cross exit but is never persisted or surfaced. This change surfaces that
existing value into the chain view for both CALL and PUT.

Outcome: each side of the chain shows a VWAP column, and each side's LTP is tinted (↑ green above
VWAP / ↓ red below) so the operator sees at a glance where premium sits relative to its VWAP.

## Decisions (confirmed with operator)

- **Source:** reuse the in-session `TickVWAP` the chain manager already computes (volume-weighted,
  session-anchored). No new VWAP math. Accepted caveat: it is in-memory, so a mid-session process
  re-exec (daily 09:00 re-login / feed-death hard-recover) resets that option's VWAP for the rest
  of the day. This matches the existing exit-VWAP behavior. (The exchange's own average-price feed
  field was considered as a more accurate, restart-safe source but is out of scope for now.)
- **Display:** a `CE VWAP` column (left of strike) and `PE VWAP` column (right of strike); each
  side's LTP colored green/red with an ↑/↓ marker vs its VWAP. VWAP renders as a ₹ premium price
  (two decimals), NOT in Lk/Cr (that formatting is for OI counts only).

## Data flow

One value threaded through the existing snapshot pipeline; the dashboard is a separate process
that reads `option_chain_snapshots` from the DB, so VWAP must be persisted there (exposing it only
from the in-memory manager would never reach the dashboard).

```
on_option_tick → snapshot dict {..., vwap} → write_chain_snapshots → option_chain_snapshots.vwap
  → latest_chain_state → summarize_chain (ce_vwap/pe_vwap) → ChainStrikeOut → /chain + SSE → UI
```

## Changes

### Backend
- **`src/algo_trading/persistence/db.py`** — add `vwap: str | None = None` to
  `OptionChainSnapshotRow`.
- **`src/algo_trading/persistence/bootstrap.py`** — `option_chain_snapshots` is an existing
  **hypertable**, and `SQLModel.metadata.create_all` does not ALTER existing tables, so add an
  idempotent `ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS vwap varchar` in the
  bootstrap DDL. The `chain_oi_1m` continuous aggregate is not touched (VWAP is not part of the OI
  anchor path).
- **`src/algo_trading/feed/option_chain.py`** — in `on_option_tick`, add
  `"vwap": str(vwap.value) if vwap.value is not None else None` to the snapshot dict (the `vwap`
  object is already computed a few lines above).
- **`src/algo_trading/persistence/repositories.py`** — `write_chain_snapshots` stores the `vwap`
  field on `OptionChainSnapshotRow`.
- **`src/algo_trading/reporting.py`** — `summarize_chain` reads `r.vwap` and sets `ce_vwap` /
  `pe_vwap` on the per-strike view (from each side's latest snapshot, alongside LTP). Add the two
  fields to the `ChainStrike` dataclass.
- **`apps/api/app/schemas.py`** — `ChainStrikeOut` gains `ce_vwap: float | None` and
  `pe_vwap: float | None`; `chain_out` maps them (Decimal→float, None passthrough). The SSE stream
  reuses `chain_out`, so no separate change.

### Frontend
- **`apps/web/lib/api.ts`** — `ChainStrike` type gains `ce_vwap?: number | null`,
  `pe_vwap?: number | null`.
- **`apps/web/app/dashboard/page.tsx`** — `OptionChainTable`: add a `VWAP` column header on each
  side; render `ce_vwap` / `pe_vwap` as ₹ prices (`—` when null); tint the CE and PE LTP cells
  green when `ltp >= vwap`, red when `ltp < vwap`, with an ↑/↓ marker. No coloring when VWAP is
  null.

## Edge cases
- VWAP is `null` until a token's first tick → render `—`. After the first tick it is always
  present (computed on every option tick, equal-weight fallback when volume is absent), so no
  carry-forward logic is needed (unlike OI, whose LTP-only ticks carry NULL).
- Pre-migration snapshot rows have `NULL` vwap → render `—` until fresh ticks arrive.
- A mid-session re-exec resets the in-memory VWAP; the column simply reflects the post-restart
  session VWAP from that point. Documented, accepted.

## Testing
- **Persistence:** a snapshot written with a `vwap` round-trips through `latest_chain_state`.
- **summarize_chain:** `vwap` is mapped to `ce_vwap` for a CE row and `pe_vwap` for a PE row; a row
  with no vwap yields `None`.
- **Bootstrap:** `ADD COLUMN IF NOT EXISTS vwap` runs on a fresh DB and is a no-op on a second run
  (idempotent); a pre-existing populated table gains the column without losing rows.
- **API:** `/chain` returns `ce_vwap`/`pe_vwap`, present when set and `null` otherwise.
- Full suite + `apps/api` tests + `ruff` + `mypy` green (against TimescaleDB via `make db-up`);
  `tsc --noEmit` clean for the web app.

## Verification (manual)
Rebuild `algo` + `api` + `web`; on the dashboard Option Chain tab confirm each side shows a VWAP
price and the LTP is tinted/arrowed relative to it, on live data.

## Out of scope
- The exchange's own average-price (VWAP) feed field as an alternative source.
- Restart-safe VWAP (SQL-from-snapshots or persisted accumulators).
- Historical/rolling VWAP, and VWAP on the CE/PE totals row.
