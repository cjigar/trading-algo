# Automatic daily trading lifecycle

**Date:** 2026-07-23
**Status:** Approved (design)

## Problem

Trading has to be re-armed by hand every morning with the dashboard **▶ Start** button.

Root cause: `run_algo.main()` calls `start_session()` unconditionally at process boot (sets
`RUNNING` regardless of the clock), and the scheduler only ever *stops* — square-off at 15:15 and
logout at 15:30 (→ `IDLE`). Nothing flips the state back to `RUNNING` at market open. So each new
morning the state is stuck at `IDLE` from yesterday's 15:30 logout, and an operator must press
Start.

Monitoring data (P&L, orders, option chain) already flows into the DB independent of that button —
the live feeds attach at boot and ticks populate the read models regardless of state; only *entries*
are gated by `RUNNING`. The gap is purely the missing daily auto-arm.

## Goal

A fully automatic daily lifecycle, no manual Start:

- **09:15** (market open) — arm trading (`RUNNING`), every trading day.
- **15:15** (square-off) — flatten all positions **and** stop new entries (`IDLE`).
- **15:30** (market close) — session teardown.
- A process (re)start **inside** the trading window auto-resumes; a restart **outside** it stays
  `IDLE` and waits for the 09:15 job.
- A manual **⏹ Stop** or the kill-switch halts for the rest of the day (persisted `HALTED`); only
  the next day's 09:15 re-arms.
- Trading days are weekday (Mon–Fri) **minus** configured `market_holidays`.

## Non-goals

- No strategy-logic changes.
- No new intraday time controls beyond the arm / square-off / stop transitions above.

## Design

### State timeline (IST, trading day)

| Time | Trigger | Action | Resulting state |
|---|---|---|---|
| boot < 09:15 | process start | reconcile + attach feeds; do **not** arm | `IDLE` |
| **09:15** | `market_open` cron | `start_session()` (if trading day & not `HALTED`) | `RUNNING` |
| boot 09:15–15:15 | process (re)start | arm if trading day & not `HALTED` | `RUNNING` |
| **15:15** | `squareoff` cron | `square_off_all()` + `stop_session()` | `IDLE` |
| **15:30** | `logout` cron | `stop_session()` (teardown) | `IDLE` |
| any time | manual Stop / kill-switch | halt for the day | `HALTED` |

`start_session()` already refuses to override a persisted `HALTED`, so the manual-Stop-stays-down
and kill-switch-stays-down invariants hold for free.

### Units changed

1. **`core/scheduler.py`** — two pure, unit-testable helpers plus one new job:
   - `is_trading_day(day, settings)` → `day.weekday() < 5 and day.isoformat() not in market_holidays`.
   - `in_trading_window(now, settings)` → trading day **and** `market_open <= t_IST < squareoff_time`.
     (Window ends at square-off, not close: no point arming for the 15:15–15:30 flatten-only gap.)
   - `MarketScheduler` gains an `on_market_open` callback wired to a `market_open` cron job
     (`market_open` time, `day_of_week="mon-fri"`). Holiday-awareness lives in the callback (below),
     since cron cannot express "skip holidays".

2. **`entrypoints/run_algo.py`**:
   - Boot: replace the unconditional `start_session()` with — if `in_trading_window(now)`:
     `start_session()`; else `orch.reconcile()` only (populate broker positions for the dashboard)
     and stay `IDLE`.
   - Wire `on_market_open` to arm only on a trading day inside the window (reuses
     `in_trading_window`), so a holiday's mon-fri cron fire is a no-op.
   - Square-off callback also calls `stop_session()` so no entry slips into the 15:15–15:30 gap.

3. **`core/orchestrator.py`** — expose a public `reconcile()` wrapper over `self._orders.reconcile()`
   so boot can populate broker positions without arming.

4. **`apps/web/app/dashboard/page.tsx`** — remove the **▶ Start** button; keep **⏹ Stop** and
   **🧹 Flatten**. Narrow `control()` to `"stop" | "flatten"` and confirm both. The backend
   `/control/start` route stays (unused by the UI, harmless).

### Data flow

Unchanged except that state transitions are now clock-driven. Feeds attach at boot regardless of
state, so P&L / orders / chain render pre-market and while `IDLE` — satisfying "these don't need the
Start button".

### Error handling

Scheduler jobs stay wrapped in `MarketScheduler._safe` (a failed job is logged, never crashes the
process). Boot arming is a single guarded call. `start_session()` remains idempotent w.r.t.
`HALTED`.

### Edge cases

- Boot 09:10 → `IDLE`, feeds live, 09:15 job arms.
- Boot 11:00 (redeploy/crash) → in window, not halted → `RUNNING`.
- Boot 16:00 / weekend / holiday → `IDLE`, no arm until the next trading day's 09:15.
- Manual Stop 11:00 → `HALTED`; restart 11:30 → `start_session()` no-ops on `HALTED` → stays down.
- Kill-switch `HALTED` → stays down for the day; next day fresh `IDLE` → 09:15 arms.
- Holiday → `market_open` cron fires (mon-fri) but the callback's `in_trading_window` check skips it.

## Tests

- `tests/test_scheduler.py` (new): `is_trading_day` (weekday, weekend, holiday), `in_trading_window`
  (before open, at open, mid-session, at square-off boundary, after close, weekend, holiday).
- Boot behaviour: outside window does not set `RUNNING`; inside window arms; `HALTED` blocks arm.
- `MarketScheduler.start()` registers a `market_open` job (alongside the existing three).
