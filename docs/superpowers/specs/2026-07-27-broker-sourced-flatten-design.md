# Broker-sourced flatten

**Date:** 2026-07-27
**Status:** Approved — implementing

## Problem

Pressing **🧹 Flatten** on the dashboard (or Telegram `/clear`) enqueues a `flatten` command that
the orchestrator consumes via `square_off_all()`. That method loops over the orchestrator's
**in-memory `PositionTracker`** only:

```python
for position in self._positions.open_positions():   # orchestrator.py
```

The tracker is a fresh `PositionTracker()` fed exclusively by live fills seen during *this* process
run (`on_fill`). It is **never rehydrated** from the broker or persisted fills on startup, and
`reconcile()` / `start_session()` reconcile *orders* (and persist broker positions to the DB) but do
not feed positions back into the tracker.

**Consequence:** after a process restart (`run_algo.py` restarts in place for clean re-auth), or for a
position opened in a previous run / manually, the in-memory tracker is empty. `square_off_all()` then
iterates over nothing and places **no exit orders** — yet still calls `record_audit("square_off")` and
logs `square_off_all`, so it *looks* successful. Observed symptom: **flatten places no exit order and
the option position is not cleared.**

## Approach (chosen: broker-sourced flatten)

Make `square_off_all()` source its close-list from the **broker** (the source of truth) instead of the
in-memory tracker. The Kotak place-order path (`kotak_client._to_place_params`) needs only
`trading_symbol` + `exchange_segment` + side + qty + price — not `instrument_token` — so an exit order
can be reconstructed from a raw broker position dict.

### Data flow

```
flatten command
  -> square_off_all(reason)
       positions = _fetch_broker_positions()          # fresh broker read, persisted-snapshot fallback
       for each: normalize_position_row -> (instrument, signed net qty)   # skip net == 0
       union in-memory tracker positions the broker didn't report (belt-and-braces), keyed by symbol
       for each target: build_exit(opposite side, abs(net), ltp)  -> orders.submit
       record_audit + log include the count; warn when count == 0
```

## Changes

### 1. `broker/report_normalize.py` — new pure normalizer
Add `POSITION_FIELD_CANDIDATES` and `normalize_position_row(raw) -> dict | None`, mirroring the
existing `normalize_order_row` / `normalize_trade_row`. Handles both the Kotak shape
(`flBuyQty`/`flSellQty`/`tok`/`trdSym`/`exSeg`) and the PaperBroker shape
(`netQty`/`trading_symbol`). Returns
`{instrument_token, trading_symbol, exchange_segment, underlying, option_type, net_qty}`
(net signed: + long, − short; `underlying`/`option_type` via existing `_parse_symbol`), or `None`
when the trading symbol is missing. Net qty prefers an explicit `netQty`, else `buy − sell`.

### 2. `core/orchestrator.py` — rewrite `square_off_all()`
- Build `targets: dict[trading_symbol -> (Instrument, signed_net)]` from `_fetch_broker_positions()`,
  skipping `net == 0`.
- Union in any in-memory tracker position whose symbol the broker didn't report.
- For each target: `position_side = BUY if net > 0 else SELL`; submit
  `build_exit(instrument, abs(net), ltp, position_side)`. `ltp = self._ltp.get(token)` when a token is
  known, else `None` → `build_exit` falls back to a MARKET order (desirable for a guaranteed flatten).
- `record_audit` / log now include the count; emit `square_off_all_empty` warning when nothing closed.
- New helpers: `_fetch_broker_positions()` (fresh `self._broker.positions()`, fall back to
  `self._repo.latest_broker_positions()` on failure) and `_instrument_from_position(row)` (safe-enum
  coercion, `strike=0` / `expiry=today` / `lot_size=0` — all unused for placing).

### 3. Tests
- `normalize_position_row`: Kotak long/short, PaperBroker shape, net-zero, missing-symbol (→ None).
- **Regression:** empty in-memory tracker + broker reports a net-long position → exactly one SELL exit
  submitted (the case that would have caught this bug).
- Short → BUY to close; qty = `abs(net)`.
- Dedup: same symbol in broker + tracker → one order.
- Tracker-only symbol (not at broker) → still included.
- Broker read raises → falls back to persisted snapshot.
- Empty everywhere → zero orders, no crash, `square_off_all_empty` logged.

## Known limitation (out of scope)

Pressing Flatten twice while exits are still in flight can double-submit, since the broker still
reports the position until the close fills. This risk exists in the current code too; not guarded here
to keep the change tight. A future "skip symbols with a live pending exit" guard would address it.

## Files touched

- `src/algo_trading/broker/report_normalize.py`
- `src/algo_trading/core/orchestrator.py`
- `tests/` (new coverage)
