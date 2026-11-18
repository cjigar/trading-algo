# Positions tab -> live Kotak account M2M (REST-sourced LTP)

**Date:** 2026-11-18
**Status:** Approved (design)
**Supersedes:** the *LTP source* decision in
`2026-07-23-live-broker-mtm-pnl-design.md`. That document chose the quote **websocket** on the
stated grounds that "there is no REST quotes method in the codebase, and none is reliably exposed
by the wired SDK". Both halves of that premise are now false (see Problem). Everything else in
that document — the P&L formula, the `mtm_pending` principle, the non-goals — still stands and is
not restated here.

## Problem

1. **The websocket LTP source is gone.** Kotak decommissioned the legacy HS market feed on
   2026-09-15; `wss://mlhsm.kotaksecurities.com`, hard-coded at `neo_api_client/urls.py:4`, is
   NXDOMAIN from the production host. The replacement uses a different protocol, so the v2 SDK
   cannot stream quotes at all. `_mark_open_positions` therefore subscribes tokens that never tick,
   `self._ltp` stays empty, nothing reaches `live_quotes`, and every open position reads
   `mtm_pending`. Observed in production 2026-11-18: `/api/broker-pnl` -> `mtm_pending_count=4`.
2. **A REST quotes method does exist.** `NeoAPI.quotes(instrument_tokens, quote_type)` ->
   `QuotesAPI.get_quotes` is a plain REST `GET` (`api/quotes_neo_symbol_api.py:23`), unrelated to the
   websocket. Verified live against the account's four open positions: `quote_type="ltp"` returned
   LTPs for all four, and `"all"` additionally returns OHLC, volume, change, 5-level depth and
   `open_int`.
3. **The Positions tab is dead weight.** It renders the *algo's own* positions, which are
   permanently zero while the strategy cannot signal.

## Goal

Replace the Positions tab with the **Kotak account** view, matching the Neo positions screen:
a summary strip (Profit/Loss, Unrealised P/L, Realised P/L, Net traded value) above a table of all
positions — Name, Type, Quantity, Avg. cost, LTP, Profit/loss — with squared-off rows de-emphasised
but still priced. Updates live over the existing SSE stream.

## Non-goals / anti-goals

- **Does not revive trading.** No candles and no option chain, so the strategy still cannot
  generate a signal. This is a monitoring change only.
- Not a pixel copy of the Kotak app; it adopts the dashboard's existing dark style.
- No change to the Orders / Trades / P&L / Option Chain tabs.
- **Anti-goal:** a confident wrong number. A position without a quote stays visibly `mtm_pending`
  (the principle already set in the 07-23 design), never silently realized-only as if complete.
- Charges/brokerage remain unmodelled, as before — Kotak's own M2M is pre-charges.
- Carry-forward/NRML valuation remains the previously flagged follow-up (`cf* = 0` for intraday MIS).

## Constraints

- `apps/api` must never hold a broker session. Quotes are fetched by the **loop**, published to
  `live_quotes`, and read by the API exactly as today.
- One **batched** `quotes()` call per refresh (~10 tokens), through `broker/ratelimit.py`.
- Operational, from the 07-23 doc: **deploy after 15:30 IST**, because a rebuild restarts the live
  loop.

## Approaches

| Approach | Verdict |
|---|---|
| API fetches quotes per request | **Rejected** — breaches the no-broker-session-in-the-web-app rule that keeps the dashboard incapable of placing orders. |
| A separate quote-poller process | **Rejected** — a second broker session and another unit to supervise, for no gain. |
| **Extend the loop's existing broker refresh** | **Chosen** — `_mark_open_positions` already runs every `broker_refresh_seconds`, already iterates these positions, and already writes `live_quotes`. Smallest change, in the unit that owns the job. |

## Design

### LTP resolution order

Per token: use `self._ltp` (websocket) when present, else the REST batch. Websocket-preferred means
the future `kotakneoapi` 3.x migration restores sub-second pricing with **no change here**, while
REST remains the fallback. Today the first branch is always empty, so REST serves every token.

### Units changed

1. **`core/orchestrator.py` — `_mark_open_positions`**
   - Price **all** position tokens, not only open ones: the Kotak screen shows an LTP for squared
     rows too. Net-zero rows still need no quote for their P&L (it reduces to `sell−buy`), so a
     failed quote there is cosmetic, never a `mtm_pending`.
   - Collect tokens lacking a websocket LTP, issue one batched REST `quotes(quote_type="ltp")`, and
     merge the results before the existing `upsert_live_quotes` call.
   - Keep the existing per-step `try/except` fail-safe: a quotes failure must degrade to
     `mtm_pending`, never stall the refresh or the loop.
   - Unchanged in paper mode (no authenticated client).

2. **`reporting.py` / `schemas.py`** — add `total_unrealized` and `net_traded_value` to the summary
   so the strip has all four tiles. `total_unrealized = total_pnl − total_realized`.
   `net_traded_value` is to be **derived empirically** against the reference screenshot's
   **−38,351** before being fixed in code, not assumed.

3. **`apps/web` — Positions tab** — rewrite against `/api/broker-pnl`, which already carries every
   column. Summary strip + table; squared rows de-emphasised; `mtm_pending` marked per row.

## Left to the builder

Column widths, number formatting, red/green thresholds, greyed-row styling, and how the
`mtm_pending` marker reads.

## Calls made in design (not the user's)

Quote all tokens rather than only open ones; reuse the 5s `broker_refresh_seconds` cadence;
websocket-preferred LTP ordering.

## Tests

Extend the existing patterns in `tests/test_broker_refresh.py` (FakeBroker/repo):

- websocket LTP present -> REST not called for that token;
- websocket empty -> batched REST quotes fetched once and merged into `upsert_live_quotes`;
- REST quotes raising -> refresh completes, open positions read `mtm_pending`;
- squared-only account -> unchanged totals, no quote required;
- `total_unrealized` / `net_traded_value` arithmetic, including the reference figures.

## Verification

`make check` + `pytest apps/api/tests` (note: `testpaths` excludes it from bare `pytest`) +
`tsc --noEmit`. Live: the four open positions show non-null LTP, `mtm_pending_count` reaches 0, and
the strip's Realised holds at the already-correct +7,212 while Unrealised tracks the Kotak app.
