## 1. Feed model & config

- [x] 1.1 Extend the market-data model to carry Open Interest + volume (`oi`, `volume` on `Tick`/new `OptionQuote`); update `normalize_tick` with OI candidate keys (`oi`, `openInterest`, `OI`) and volume
- [x] 1.2 Add config settings: `strategy` (`vwap_breakout|oi_selling`), `strike_window` (5), `otm_strikes` (3), `strike_step` (50), `chain_eval_seconds`, `snapshot_min_interval_seconds`, `allowed_weekdays` (Fri,Mon,Tue), margin buffer — placeholders flagged must-confirm-before-live
- [x] 1.3 Add a `chain(underlying, atm, width)` helper to `WeeklyOptionResolver` returning the ATM ±width CE/PE contracts for the current weekly expiry

## 2. Option-chain persistence

- [x] 2.1 Add `OptionChainSnapshotRow` table (trading_day, timestamp, underlying, strike, option_type, instrument_token, oi, ltp, volume) with indexes
- [x] 2.2 Repository: batched snapshot writer (buffer + flush every N ticks / T seconds) + query for latest chain state and time-series read
- [x] 2.3 Retention/pruning helper (drop snapshots older than a configured number of days)
- [x] 2.4 Unit-test snapshot round-trip, batched flush, latest-state query, and pruning

## 3. Option-chain manager

- [x] 3.1 Implement `feed/option_chain.py` `OptionChainManager`: resolve ATM from NIFTY spot (nearest step, with hysteresis), resolve ATM ±5 CE/PE contracts, maintain desired token set
- [x] 3.2 Drive dynamic subscribe/unsubscribe via `LiveFeedCoordinator` on ATM shift (diff desired vs current); handle re-subscribe on reconnect
- [x] 3.3 Maintain in-memory latest chain state (OI/LTP/volume per strike/side) and a per-option session VWAP (reuse `SessionVWAP`); feed snapshots to the batched writer
- [x] 3.4 Unit-test ATM rounding + hysteresis, window diff on shift, chain-state updates, and per-option VWAP

## 4. OI selling strategy

- [x] 4.1 Implement `strategy/oi_selling.py`: aggregate CE vs PE OI over ATM ±5 from chain state; select higher-OI side; resolve the 3-OTM contract; emit a short `Signal` (Side.SELL)
- [x] 4.2 Add trading-day gating (Fri/Mon/Tue, IST) + an NSE-holiday guard; no entries on other days
- [x] 4.3 Evaluate on a timer (`chain_eval_seconds`); one open short at a time (max positions)
- [x] 4.4 Unit-test side selection (CE/PE dominance, tie=no signal), 3-OTM strike (CE=ATM+150, PE=ATM−150), and day-of-week gating (use `freezegun`)

## 5. Short-position execution & exit

- [x] 5.1 Extend `position_tracker` for shorts: sell-to-open (negative qty at sell price), buy-to-close realizes +(sell−buy)×qty; short unrealized P&L
- [x] 5.2 Extend `exit_manager` with a short-aware VWAP-cross stop-loss: exit (buy-to-close) when short option LTP crosses above its session VWAP; arm only post-entry to avoid whipsaw
- [x] 5.3 Ensure `signal_translator` builds sell-to-open and buy-to-close orders for shorts; wire independent time square-off (reuse scheduler) to flatten shorts
- [x] 5.4 Add a margin/short-exposure pre-check to `risk_manager` using `limits()`/`margin_required()` before shorting
- [x] 5.5 Unit-test short P&L, VWAP-cross exit (arming + trigger), time square-off of a short, and the margin block path

## 6. Orchestration & wiring

- [x] 6.1 Add `STRATEGY` selection to the orchestrator: build `oi_selling` + wire the `OptionChainManager` (chain state → strategy) instead of the candle path when selected
- [x] 6.2 Subscribe the NIFTY index + option chain on start (live) / seed from cache (paper); persist snapshots continuously regardless of trading day
- [x] 6.3 Gate entries to allowed weekdays at the orchestrator level; keep feed/persistence running on all days
- [x] 6.4 In-process integration test (paper): synthetic chain ticks → OI aggregation → short entry → VWAP-cross exit → short P&L, on an allowed vs disallowed day

## 7. Dashboard & confirmation

- [x] 7.1 Add an "Option Chain" view/tab: latest OI/LTP per strike (ATM ±5, CE/PE) and the current CE-vs-PE OI aggregate + selected side
- [~] 7.2 Verify OI/LTP capture and chain persistence live — **read-only capture-only mode built** (`make capture` / `run_capture`: live feed → DB, no strategy, no orders). **NEEDS OPERATOR to run during market hours** and watch the Option Chain tab.
- [x] 7.3 Confirm the OI quote source with the live Kotak API — ✅ **RESOLVED live**: OI streams on the websocket as `oi` (token=`tk`, volume=`v`); messages nest under `{type, data:[...]}` (now unwrapped by `_unwrap`). REST `quotes(quote_type='all')` also carries it as `open_int` (fallback). No poll needed.
- [~] 7.4 Confirm exact parameters with the operator (window, OTM offset, eval cadence, VWAP-cross confirmation, square-off time, lots, margin buffer, holiday list) — **NEEDS OPERATOR**. All wired as `ALGO_*` config placeholders
- [x] 7.5 Run `openspec validate add-oi-option-selling-strategy` + full test suite; ensure ruff + mypy pass — ✅ ruff + mypy clean, 116 tests pass, openspec valid
- [~] 7.6 Validate short entry/exit + time square-off in paper mode across Fri/Mon/Tue, then go live — **OPERATOR go-live step** (small lots + conservative kill-switch)

## 8. Multi-underlying (SENSEX) extension

- [x] 8.1 Per-underlying OI config: `oi_underlyings`, `sensex_weekdays` (Wed/Thu), `sensex_strike_step` (100); `weekdays_for`/`strike_step_for` helpers
- [x] 8.2 Strategy + chain manager derive weekdays/step from the underlying; orchestrator builds one chain+strategy per underlying and routes ticks; each self-gates to its days
- [x] 8.3 Look up index tokens live: NIFTY=26000 (nse_cm), SENSEX=1 (bse_cm); set in .env / .env.example
- [x] 8.4 Tests: SENSEX shorts Wed/Thu at step-100 strikes; orchestrator trades only the day's underlying; both chains captured. 126 tests pass; ruff + mypy clean
