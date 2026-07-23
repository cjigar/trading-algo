# Live account M2M P&L (match the Kotak app, realtime)

**Date:** 2026-07-23
**Status:** Approved (design)

## Problem

The dashboard's "Broker account P&L (live)" does not match the Kotak app's P&L. Two causes:

1. **Structural — no MTM.** `summarize_broker_positions` (`reporting.py:215`) computes only *realized
   on squared quantity* from `buyAmt`/`sellAmt`/`flBuyQty`/`flSellQty`. The Kotak positions payload
   carries **no LTP and no MTM field** (verified against live data), so unrealized mark-to-market on
   open positions is omitted. The Kotak app shows realized **+ unrealized MTM**, so the numbers
   cannot agree while a position is open.
2. **No live quotes.** The `live_quotes` table is empty in practice — quotes are published only for
   the *algo's own in-memory* positions, which reset to empty on a process restart. Nothing prices
   the account's open positions (incl. instruments opened outside the algo, e.g. a `CRAMC-EQ` equity).

## Goal

The dashboard broker P&L equals the Kotak app's **account-level live M2M** (realized + unrealized),
across the **whole account**, updating in realtime. For each position:

```
position P&L = (sellAmt − buyAmt) + netQty × liveLTP
```

where `netQty = flBuyQty − flSellQty`. For a squared position `netQty=0`, this reduces to
`sellAmt − buyAmt`, i.e. today's realized number — which already matches. The account total is the
sum over all positions.

## Design

### LTP source: the live websocket feed (not REST)

There is **no REST quotes method** in the codebase, and none is reliably exposed by the wired SDK —
the only proven realtime quote path is the Kotak quote **websocket** (`broker/market_data.py`,
subscribed via `LiveFeedCoordinator.subscribe_option`). We reuse it: subscribe every open
broker-position token, let ticks populate `self._ltp`, and publish those to `live_quotes`. This is
sub-second realtime and covers equities too, because `ExchangeSegment` values
(`nse_fo`/`bse_fo`/`nse_cm`/`bse_cm`) match the raw `exSeg` strings exactly, so
`subscribe_option(tok, ExchangeSegment(exSeg))` works for any instrument.

### Units changed

1. **`core/orchestrator.py`** — in the broker-account refresh path, after polling positions:
   - Collect `(tok, exSeg)` for every position; **subscribe new ones** to the feed via the
     coordinator (track a `set` of already-subscribed position tokens; the feed's `_resubscribe_all`
     re-establishes them after a reconnect, so subscribe-once is enough).
   - **Publish** `self._ltp` for those tokens to `live_quotes` via the existing
     `Repository.upsert_live_quotes(...)`. Newly-subscribed tokens have no tick on the first cycle —
     they price in on the next (~one-cycle lag), which is acceptable.
   - Guarded fail-safe like the rest of `refresh_broker_account()`. No-op in paper mode (no
     coordinator / empty positions).

2. **`reporting.py` — `summarize_broker_positions(rows, quotes)`** — add a `quotes: dict[token, LTP]`
   argument. Per position compute `total_pnl = (sell_val − buy_val) + net × ltp`; keep `realized`
   (matched-qty) for display. When a quote is missing for an open position, fall back to
   `total_pnl = realized` and set `mtm_pending = True` (a missing price must read as "MTM pending",
   never a silent wrong number). Account total = Σ `total_pnl`. Extend `BrokerPositionPnL` /
   `BrokerPnLSummary` with `total_pnl`, `ltp`, `mtm_pending`, and an account `total_pnl`.

3. **API — `schemas.py` / `routes.py` / `state_bridge.py`** — `broker_pnl_out` takes the position
   rows **and** the live quotes for their tokens (bridge reads `live_quotes` for the position
   tokens). Extend `BrokerPnLOut` / `BrokerPositionPnLOut` with `total_pnl` / `ltp` / `mtm_pending`.
   Broker P&L already rides the 3s SSE stream, so the dashboard updates live with no frontend change
   beyond rendering the new total.

4. **Frontend — `apps/web`** — show the account **live M2M total** prominently (realized +
   unrealized), per-position `total_pnl` and a small "MTM pending" marker when `mtm_pending`. Update
   the caption from "realized on squared qty … MTM not included" to "live account M2M".

### Data flow

`refresh_broker_account` (5s): positions → subscribe tokens + publish LTPs to `live_quotes`.
Websocket ticks (sub-second) keep `self._ltp` current between polls. Read side: `broker_pnl_out`
marks positions at `live_quotes` → SSE (3s) → dashboard.

### Error handling

Every broker read / subscribe / publish independently `try/except` (mirrors `refresh_broker_account`).
Missing quote → realized-only fallback + `mtm_pending`. Feed reconnect re-subscribes via the feed's
existing `_resubscribe_all`.

## Non-goals / edge cases

- **Carry-forward / overnight (NRML)**: today's account is intraday MIS (`cf* = 0`), so the
  filled-qty formula matches. True CF valuation (previous-close basis for carried qty) is a
  follow-up; flagged, not built.
- Charges/brokerage are not modelled — the Kotak app's M2M is likewise pre-charges, so this matches.

## Tests

- `summarize_broker_positions` with quotes: net-short and net-long MTM (`(sell−buy)+net×ltp`);
  squared position unchanged; missing-quote → realized fallback + `mtm_pending`; account total.
- Orchestrator: the refresh subscribes each open-position token once (incl. an `nse_cm` equity) and
  upserts present `self._ltp` values to `live_quotes`; fail-safe when the coordinator/feed errors;
  paper-mode no-op.
- API: `broker_pnl_out` includes `total_pnl` and marks from `live_quotes`; SSE `broker_pnl` carries it.
- Reuse the `FakeBroker`/`repo` patterns from `tests/test_broker_refresh.py` and `tests/test_execution.py`.

## Verification

- `make check` + `apps/api` tests + `apps/web` `tsc --noEmit`.
- Live (market hours): dashboard broker M2M total tracks the Kotak app within a few seconds as prices
  move; open positions show a live per-position P&L that changes tick-to-tick; squared-only total is
  unchanged. Deploy after 15:30 IST (rebuild restarts the live loop).
