# Option-chain display window (config-driven, default ±7)

**Date:** 2026-07-24
**Status:** Approved design, ready for implementation plan

## Goal

Make the dashboard's Option Chain view **clear** by limiting it to a config-driven window of
strikes around ATM (default **±7**, i.e. 7 below + ATM + 7 above = 15 strikes), and compute
**Total CE OI**, **Total PE OI**, and the **Higher-OI side** over exactly that window.

## Current state (baseline)

Almost everything is already wired — the gap is the window, not the metrics:

- The Option Chain tab already renders four metrics — **ATM strike**, **Total CE OI**,
  **Total PE OI**, **Higher-OI side** — plus the chain table (`apps/web/app/dashboard/page.tsx:168-173`).
- `Higher-OI side` = `ChainSummary.selected_side` = `"CE" if ce_total > pe_total else "PE" else "—"`
  (`reporting.py:201`).
- `ce_oi_total` / `pe_oi_total` / `selected_side` are computed in `summarize_chain`
  (`reporting.py:147-202`) and exposed via `ChainOut` (`apps/api/app/schemas.py:137-143`,
  `chain_out` at `schemas.py:290-308`).
- Both `/api/chain` and the SSE stream build the chain the same way, through
  `routes._chain_out_with_trends → chain_out → summarize_chain` (`apps/api/app/routes.py:110-131,178-203`).

**The gap:** `summarize_chain` pivots and sums over **all** rows it is given — currently the full
captured chain (`chain_feed_window = 20` → up to 41 strikes). The table shows all of them
(cluttered), and the totals/selected side reflect the full ±20, not a focused view. There is no
display-window setting (only `strike_window = 5` for the strategy's OI band and
`chain_feed_window = 20` for capture).

## Chosen approach: window inside `summarize_chain` (Approach A)

Resolve ATM from the full captured set, then slice to ATM ±N by **position** in the sorted-strike
list (step-agnostic — works for NIFTY-50 and SENSEX-100), and sum the totals + selected side over
the slice. Single source of truth: both `/chain` and the SSE stream inherit it because both call
`chain_out → summarize_chain`.

### Rejected alternatives

- **B. Slice in the API route** after the summary — re-sums totals outside the tested reporting
  logic and splits the windowing across two places.
- **C. Limit rows at the DB/bridge read** — ATM is not known until the chain is pivoted, and it
  would affect other `bridge.chain()` consumers.

## Design

### 1. Config

- New setting in `config/settings.py`: `chain_display_window: int = 7` (env
  `ALGO_CHAIN_DISPLAY_WINDOW`). Strikes each side of ATM to **display**.
- `0` = show the full captured chain (escape hatch; totals then span everything captured).
- Purely a view concern — independent of `strike_window` (strategy ±5 OI band) and
  `chain_feed_window` (capture ±20). Placed next to those in settings for discoverability.

### 2. `summarize_chain` — apply the window

Add a parameter `display_window: int | None = None` to `summarize_chain` (`reporting.py:147`).

Flow (unchanged up to ATM resolution):

1. Build `per_strike` for every strike in `rows` (as today).
2. Resolve `atm` from the **full** `per_strike` (`_resolve_atm`) — so ATM is never distorted by
   the slice, even when ATM sits near a captured edge.
3. **If** `display_window` is a positive int **and** `atm` is not None: find the ATM strike's
   index in the strike-sorted `per_strike`, and keep `per_strike[idx - N : idx + N + 1]`
   (clamped to list bounds). Otherwise keep all strikes (covers `display_window` None/0 and the
   no-ATM edge case).
4. Compute `ce_oi_total` / `pe_oi_total` by summing `ce_oi` / `pe_oi` over the **kept** slice, and
   `selected_side` from those windowed totals.
5. Return `ChainSummary(per_strike=slice, ce_oi_total, pe_oi_total, selected_side, atm)`.

`is_atm` marking is applied before/with the slice so the ATM row is present and flagged. Note the
current code sums totals inside the build loop; this moves the CE/PE totalling to run over the
kept slice (a small internal reorder), leaving all other behavior identical.

### 3. Expose the window value + plumb the setting

- Add `display_window: int` to `ChainSummary` and to `ChainOut` (`schemas.py`) so the UI can label
  the metrics accurately (it is the effective window used, echoing the setting).
- `chain_out(..., display_window)` passes the setting through to `summarize_chain` and copies it
  onto `ChainOut`.
- `routes._chain_out_with_trends` passes `display_window=settings.chain_display_window` — covering
  `/api/chain` and the SSE stream identically.

### 4. Web UI

- No structural change: the four metrics and the table already render whatever `ChainOut`
  returns, so they automatically reflect the ±7 window and the shorter table.
- Small clarity touch: label the three OI metrics with the window, e.g. `Total CE OI (ATM ±7)`,
  `Total PE OI (ATM ±7)`, `Higher-OI side (ATM ±7)`, using `chain.display_window` from the
  response (when `> 0`; when `0`, omit the suffix / show "full"). Purely cosmetic.

### 5. Docs

- Document `ALGO_CHAIN_DISPLAY_WINDOW=7` in `.env.example`, next to `ALGO_CHAIN_FEED_WINDOW`, with
  a one-line note that it only affects the dashboard view (not capture or the strategy).

## Testing

- **reporting** (`tests/` for `summarize_chain`): given rows spanning ATM ±20, with
  `display_window=7`, assert the returned `per_strike` is exactly the 15 ATM-centred strikes;
  `ce_oi_total`/`pe_oi_total` equal the sums over just those 15; `selected_side` follows the
  windowed totals (add a case where the full-chain winner and the windowed winner differ, proving
  the totals are truly windowed); ATM is centred; edge clamping when ATM is near a captured edge
  (fewer than N strikes on one side); `display_window=0`/`None` returns the full chain unchanged
  (regression guard for existing callers).
- **API** (`tests/` for the chain route / stream): with `chain_display_window` set, `/api/chain`
  and the stream payload both return a `per_strike` of the windowed length and `display_window`
  echoing the setting.

## Out of scope

- No change to the OI-selling **strategy** or its `strike_window` aggregation.
- No change to **capture** (`chain_feed_window` stays ±20 — the wider band is still recorded; only
  the view is narrowed).
- No new OI-derived metrics (PCR, bias labels) — only the three the request named.
