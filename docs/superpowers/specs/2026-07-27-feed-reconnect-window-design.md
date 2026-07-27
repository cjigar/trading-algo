# Gate quote-feed reconnection to market hours

## Context

The live loop wedged over a weekend: `can't start new thread` / `[Errno 24] Too many open files`.
The websocket reconnect path leaks one socket **and** thread per attempt — `neo.subscribe()` stands
up a fresh websocket without closing the previous one. The re-entry guard in `FeedHandler.reconnect`
already stopped the *exponential* fan-out, but a **linear** leak remains: every drop→reconnect
orphans a socket+thread.

Reconnect fires from three places, and two run regardless of market hours:
1. SDK `on_error` / `on_close` → `reconnect()` — fires each time Kotak drops the idle socket after hours.
2. `recover_stale_feed()` — called every loop iteration, ungated.
3. Hard-recover re-exec — already gated to the trading window (fine).

The daily pre-market re-login re-execs the process (clearing all leaked resources) **only on trading
days**. So across Fri→Mon the process runs continuously, and after-hours/weekend WS drops trigger
endless leaky reconnects → thread/FD exhaustion by Monday's open.

## Fix — gate reconnection to a feed window

Reconnect only inside a **feed window**: a trading day, from the pre-market login time through market
close. Outside it, `reconnect()` is a no-op. A dead feed is harmless when no trading happens, and each
trading day starts fresh via the pre-market re-login — so an idle socket Kotak drops is never chased
with leaky reconnects. This eliminates the weekend accumulation entirely (zero reconnects Fri-close →
Mon-open). In-window drops still reconnect as today: at most a few leaks/day, cleared by the next
morning's re-exec.

Not doing: closing the old socket per reconnect (SDK-internal, riskier — deferred), or an absolute-age
watchdog re-exec (unnecessary once reconnects are gated — nothing accumulates).

## Changes

- **`core/scheduler.py`** — add `in_feed_window(now, settings)`: `is_trading_day` and
  `premarket_login_time <= t_IST <= market_close`. (Wider than `in_trading_window`, which stops at
  square-off — the feed should stay live through the full close and from pre-open warmup so it is up
  for the 09:15 open.)
- **`broker/market_data.py`** — `FeedHandler` gains a `reconnect_allowed: Callable[[], bool]`
  predicate (default `lambda: True`, so existing tests/behaviour are unchanged). `reconnect()`
  returns early (logs `quote_ws_reconnect_skipped_off_hours`) when it returns False. This single gate
  covers every reconnect path (on_error/on_close and `recover_stale_feed` → `coordinator.reconnect`
  → `feed.reconnect`). Keeps `FeedHandler` unaware of market hours — the policy is injected.
- **`broker/live_feed.py`** — wire the predicate:
  `FeedHandler(..., reconnect_allowed=lambda: in_feed_window(datetime.now(UTC), settings))`.
- **`entrypoints/run_algo.py`** — also gate the `recover_stale_feed()` *call* with `in_feed_window`
  (the reconnect would no-op anyway; this just avoids off-hours "stale_reconnecting" log noise and
  wasted work), mirroring how the loop already gates hard-recover.

Initial `subscribe()` at attach is unaffected — only recovery `reconnect()` is gated.

## Verification

- **Unit:** `in_feed_window` true at 09:00/12:00/15:30 IST on a weekday, false at 08:00, 16:00, and
  all day Sat/Sun and on a configured holiday. `FeedHandler.reconnect()` returns False without calling
  `neo.subscribe` when `reconnect_allowed` is False; reconnects normally when True (inject a fake clock
  / predicate — no real time).
- **Suite:** full `pytest` + `apps/api` green; ruff + mypy clean.
- **Live (after close / pre-open):** on next deploy, confirm the algo restarts clean, and after market
  close the logs show `quote_ws_reconnect_skipped_off_hours` instead of a reconnect storm; FD/thread
  count stays flat overnight (spot-check `/proc/1/fd`).
