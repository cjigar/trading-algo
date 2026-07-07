## Context

This change adds a second, independent strategy to the existing Kotak Neo algo (`add-kotak-fno-trading-algo`): an **OI-based option-selling** strategy on NIFTY weekly options, plus the **option-chain feed** it depends on. The current system streams only LTP for a single underlying/option and captures no Open Interest or chain data, so both the feed and the strategy are new.

Reuses existing building blocks: `LiveFeedCoordinator`/`FeedHandler` (websocket), `WeeklyOptionResolver` (expiry/strike), `OrderManager` (order lifecycle), `RiskManager`, `SessionManager` (TOTP auth), SQLModel persistence (SQLite/Postgres), and the paper-mode-first + kill-switch safety model.

Constraints: shorting options requires **margin** and carries **open-ended risk**; the Kotak quote feed must carry **OI**; 22 contracts streaming continuously is **high write throughput**; and entries are restricted to **Fri/Mon/Tue (IST)**.

## Goals / Non-Goals

**Goals:**
- Continuously subscribe NIFTX spot + ATM ±5 CE/PE and persist OI/LTP/volume to the DB as a time series.
- Track ATM live and re-window the chain as spot moves.
- Aggregate CE-vs-PE OI, short the higher-OI side 3 strikes OTM.
- Exit shorts on VWAP-cross SL + independent time square-off; gate to Fri/Mon/Tue.
- Support short positions/P&L and a margin pre-check; keep paper mode the default.

**Non-Goals:**
- Multi-underlying chains (SENSEX/BankNifty) — NIFTY only for v1.
- Greeks/IV analytics or multi-leg spread optimization.
- Historical backtesting of this strategy (paper mode is live-shadow).
- Replacing the existing VWAP-breakout strategy — this runs alongside it (one active strategy per process, selected by config).

## Decisions

### D1: Extend the tick/feed model to carry OI + volume
Add `oi` and `volume` to a market-data update (new `OptionQuote`/extended `Tick`). `normalize_tick` gains candidate keys for OI (`oi`, `openInterest`, `OI`) and volume. Rationale: the whole strategy is OI-driven; the current LTP-only `Tick` can't express it. Kept backward-compatible (fields optional/None). **Alternative:** a parallel quote path — rejected as duplicative of the existing feed plumbing.

### D2: Option-chain manager owns the dynamic subscription set
A new `feed/option_chain.py` `OptionChainManager`: given live NIFTY spot, resolves ATM, resolves the ATM ±5 CE/PE contracts (via `WeeklyOptionResolver` extended with a `chain(underlying, atm, width)` helper), and drives `LiveFeedCoordinator.subscribe_option` for each. On ATM shift it diffs the desired vs current token set and subscribes/unsubscribes the delta. It maintains an in-memory **latest chain state** (OI/LTP per strike/side) and a per-option **VWAP** (`SessionVWAP` reused per token). **Alternative:** re-subscribe the whole window each shift — rejected (churn, rate limits).

### D3: Persist chain snapshots with batched writes
New table `option_chain_snapshots` (trading_day, timestamp, underlying, strike, option_type, instrument_token, oi, ltp, volume). Writes are **batched** (buffer ticks, flush every N ticks or T seconds) to sustain 22 continuous streams. A retention job prunes old days. Rationale: per-tick single-row inserts would overwhelm the DB. **Alternative:** write only on change / on a sampling interval — offered as a config (min snapshot interval per token) to bound volume.

### D4: OI aggregation on the latest chain state, evaluated on a timer
The strategy evaluates on a fixed cadence (e.g. every candle close or every N seconds), reading the manager's latest chain state: `sum(CE OI over ±5)` vs `sum(PE OI over ±5)`. Higher side → short; equal → no signal. Rationale: OI changes slowly; a timer-based read is simpler and less noisy than tick-by-tick. **Alternative:** event-driven on every OI update — rejected (noisy, redundant).

### D5: Strike = 3 strikes OTM by side
Short strike = ATM + 3·step for CE, ATM − 3·step for PE (step=50 for NIFTY). Resolved to a concrete contract via the resolver; if that exact strike is unavailable, snap to nearest. Encapsulated so the offset/step are config.

### D6: VWAP-cross stop-loss for shorts + independent time square-off
`exit_manager` gains a short-aware, VWAP-based rule: for a short, exit (buy-to-close) when `LTP > option.vwap` (price crossing above VWAP = losing). To avoid whipsaw at entry, arm the rule only after the first full candle/after entry, and require the cross (not merely being above at entry). The **time square-off** runs on the existing independent `apscheduler` timer and flattens regardless of feed health, verified against `positions()`. **Alternative:** fixed-point SL — the operator explicitly chose VWAP-cross; fixed SL kept as a fallback config.

### D7: Short-position support in tracker/execution
`position_tracker` and `exit_manager` currently assume long (buy-to-open). Add short handling: sell opens a negative position at the sell price; buy-to-close realizes `+(sell−buy)·qty`. `signal_translator` already supports `Side.SELL`; the order flow (sell-to-open → buy-to-close) reuses `OrderManager`. `RiskManager` gains a **margin pre-check** using `limits()`/`margin_required()` before shorting. Rationale: shorting is the core action; the long-only assumptions must be relaxed.

### D8: Day-of-week gating in the strategy + scheduler
Add `allowed_weekdays` config (default Fri/Mon/Tue). The strategy's entry check consults the current IST weekday; the scheduler can also skip arming entries on disallowed days. The feed runs every day (data capture continues); only entries are gated.

### D9: Pluggable strategy selection
Introduce a `STRATEGY` config (`vwap_breakout` | `oi_selling`). The orchestrator builds the selected strategy. The OI strategy consumes the chain manager's state rather than the candle stream, so the orchestrator wires the chain manager when `STRATEGY=oi_selling`. Rationale: keep both strategies without entangling them.

### D10: Config-driven parameters
New settings: `strike_window` (5), `otm_strikes` (3), `strike_step` (50), `chain_eval_seconds`, `snapshot_min_interval_seconds`, `allowed_weekdays`, `squareoff_time` (reused), `lots`, plus margin buffer. All flagged must-confirm-before-live.

## Risks / Trade-offs

- **[Open-ended short risk]** → mandatory VWAP-cross SL + independent time square-off + margin pre-check + daily-loss kill-switch; paper-mode default.
- **[Kotak feed may not include OI in the default quote type]** → confirm the OI-bearing quote subscription/`quotes()` shape against the live API; if OI arrives only via a periodic `quotes()` REST call rather than the socket, fall back to polling OI on the eval cadence. Isolated in the chain manager.
- **[High DB write volume]** (22 streams) → batched inserts + min-snapshot-interval + retention pruning; Postgres in Docker.
- **[VWAP-cross whipsaw]** → arm SL only post-entry/after first candle; require an actual cross; make the buffer/confirmation configurable.
- **[ATM churn near a strike boundary]** → hysteresis on ATM (only re-window when spot moves past the midpoint by a margin) to avoid subscribe/unsubscribe flapping.
- **[Weekend/holiday & expiry-day effects]** → Fri/Mon/Tue gating is weekday-based; add an exchange-holiday guard so a holiday Friday/Monday is skipped.

## Migration Plan

Additive — no change to existing behavior; the new strategy is opt-in via `STRATEGY=oi_selling`.
1. Extend feed/tick for OI+volume; add chain persistence tables (idempotent `create_all`).
2. Build the chain manager + persistence; validate OI/LTP capture in **paper** mode against the live feed (read-only data capture, no orders).
3. Implement the strategy + short-position support; validate signal/short/exit in paper mode across Fri/Mon/Tue.
4. Confirm parameters; enable `STRATEGY=oi_selling` with `MODE=live` + small lots + conservative kill-switch. Rollback = `STRATEGY=vwap_breakout` or `MODE=paper`.

## Open Questions

- **OI source**: does the Kotak socket deliver OI on the quote feed, or only via the `quotes()` REST endpoint? Determines socket-vs-poll for OI (confirm against live API during implementation).
- **Exact VWAP-cross semantics**: immediate exit on first cross, or require the LTP to hold above VWAP for N seconds/one candle to reduce whipsaw? (Default: confirm on candle close.)
- **Lots / margin buffer / square-off time / eval cadence** — operator to confirm before live.
- **Holiday calendar source** for the Fri/Mon/Tue guard (NSE holiday list).
- **Does the feed keep running on non-trading days** for data capture, or only Fri/Mon/Tue? (Assumed: feed always on; entries gated.)
