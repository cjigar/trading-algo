# India VIX on the rate ticker

## Context

The top rate ticker shows NIFTY / BANKNIFTY / SENSEX spot (with day-change) and each
index's near-month futures. The operator wants **India VIX** shown beside SENSEX.

India VIX is a **separately-quoted NSE index instrument** (NSE volatility index), subscribed by
its own token — *not* an implied vol computed from the option chain. So the unused Black-76 IV
module (`src/algo_trading/analytics/greeks.py`) is irrelevant here; this reuses the existing
index-spot machinery: subscribe a token, publish its LTP, render a ticker chip.

## Approach

VIX flows through the same path as the index spots, keyed by the string `"INDIAVIX"` rather than
a tradeable `Underlying`.

**Why a string key, not the `Underlying` enum:** VIX is never traded — no options, futures, or
candles. `index_spots.underlying` is already a free-form string PK, and `prev_index_closes` /
`_spot_out` (apps/api/app/schemas.py) are generic over that string, so VIX gets **spot +
day-change vs previous close for free**. Adding it to `Underlying` (as BankNifty was) would drag
in the NSE/BSE segment maps, `_infer_underlying`, `near_month_future`, and candle builders — all
meaningless for VIX. Keep it out of the enum.

**Data path:**
```
setting india_vix_token (NSE India VIX, nse_cm, standard token 26017)
  → orchestrator subscribes it on the index feed (self._vix_token)
  → write_index_spots() also publishes VIX LTP under key "INDIAVIX"
  → index_spots row (underlying="INDIAVIX") → StateBridge.read_state → API spots[]
  → SpotTicker renders an "INDIA VIX" chip after SENSEX (day-change, no futures line)
```

## Components to change

- **`src/algo_trading/config/settings.py`** — add `india_vix_token: str = ""` (feed/display-only,
  like the `*_index_token` settings).
- **`src/algo_trading/broker/live_feed.py`** — add `subscribe_index(token, segment)` mirroring
  `subscribe_option` but `is_index=True` (VIX is an index, not an F&O contract). `start()` is
  unchanged; VIX is subscribed after it, like the futures.
- **`src/algo_trading/core/orchestrator.py`**
  - add `self._vix_token: str | None`.
  - in `attach_live_feeds` (after `_subscribe_index_futures`), if `settings.india_vix_token` is
    set, subscribe it via `coordinator.subscribe_index(token, ExchangeSegment.NSE_CM)` and record
    the token. Fail-soft: a VIX subscribe error must not disturb the index/option/futures feeds.
  - in `write_index_spots`, add `"INDIAVIX": self._ltp[vix_token]` to the `spots` dict when the
    token has ticked. `_handle_tick` already stores any tick's LTP in `self._ltp` and builds no
    candle for a token absent from `self._underlying_token`, so VIX needs no tick-handling change.
- **`apps/web/lib/api.ts`** — no type change (VIX rides the existing `IndexSpot` shape).
- **`apps/web/components/ui.tsx` `SpotTicker`**
  - **Display order:** render spots in a canonical order `[NIFTY, BANKNIFTY, SENSEX, INDIAVIX]`
    so VIX sits after SENSEX (the current `index_spots()` read has no `ORDER BY`, so order is
    otherwise undefined). Unknown underlyings sort last.
  - **Label:** show `"INDIA VIX"` for the `INDIAVIX` entry.
  - **No futures line:** suppress the `Fut` line when the entry has no futures (`fut_ltp == null`
    and never had one) — VIX has no future, and today every chip always renders `Fut …`. Simplest
    rule: only render the `Fut` line for entries whose underlying is a real index (NIFTY /
    BANKNIFTY / SENSEX), or equivalently hide it for `INDIAVIX`.

## No schema / persistence change

VIX reuses the `index_spots` table (string `underlying` PK). No new table, no column, no
migration. `StateBridge` (separate process, read-only) is unaffected — it already returns every
`index_spots` row.

## Cold start / previous close

Like BankNifty, VIX has no prior-day row until it records a session, so its day-change falls back
to `day_open` on day one, then self-heals the next session. If the operator wants a correct
day-change immediately, seed a prior-day `index_spots` row (`underlying='INDIAVIX'`) with India
VIX's previous close — the same one-row seed used for BankNifty.

## Testing

- **Orchestrator:** with `india_vix_token` set and a seeded `self._ltp[vix_token]`,
  `write_index_spots()` publishes an `INDIAVIX` row; without a VIX tick it publishes none
  (mirrors the futures tests in `tests/test_orchestrator.py`).
- **live_feed:** `subscribe_index` issues an `is_index=True` subscribe for the given token
  (unit test against a fake feed handler).
- **Persistence/schema:** an `INDIAVIX` `index_spots` row round-trips and `_spot_out` produces a
  day-change against a seeded prior-day close (reuse the existing index-spot tests).
- **Web:** `SpotTicker` renders four chips in canonical order with VIX last and no `Fut` line for
  it; `npx tsc --noEmit` clean.

## Deploy notes

- Set `ALGO_INDIA_VIX_TOKEN` on the server (standard NSE India VIX token `26017`, `nse_cm`) — the
  operator must confirm/provide it, as with `ALGO_BANKNIFTY_INDEX_TOKEN`.
- Rebuild `algo` (feed change) + `web` (ticker). Restart `algo` **after 15:30 IST** — a restart
  interrupts the live session.
- VIX shows `—` until the token is set and the feed ticks; optionally seed its previous close for
  an immediate day-change.
