# Greeks / IV module

**Date:** 2026-07-23
**Status:** Approved (design)

## Problem

The Kotak Neo Trade API does not provide option Greeks or implied volatility. Its market-data
feed (websocket `stock_feed` and REST `quotes`) returns only `ltp`, `oi`, `volume`, OHLC, and
depth — no IV, no delta/gamma/theta/vega/rho. This codebase consequently carries none: the `Tick`
model and `OptionChainManager` state track only OI/LTP/volume, and both strategies trade off price
and OI alone.

We want IV and Greeks computed client-side from the data already flowing, made available in three
places: the dashboard chain view, the persisted snapshot history, and a strategy-readable
accessor (for rules added in a later change).

## Goals / Non-Goals

**Goals:**
- Compute per-strike-side IV and Greeks (`iv, delta, gamma, theta, vega`) for the option band the
  algo already tracks, updated on each chain refresh.
- Match how NSE / the Kotak app quote option IV, by pricing against a put-call-parity forward
  rather than raw spot.
- Surface the values on the dashboard, persist them alongside `option_chain_snapshots`, and expose
  a clean in-process accessor for strategies.
- Keep the numeric core a pure, isolated, independently testable unit.

**Non-Goals (YAGNI):**
- Rho (negligible for weekly index options).
- Greeks for strikes outside the tracked ATM ± window band (we have no quotes for them).
- Any change to `oi_selling` / `vwap_breakout` entry/exit behavior this round — *expose only*.
- A futures instrument subscription.
- Greeks-based dashboard aggregates/analytics beyond per-strike display.

## Decisions

### Decision 1: Pure numeric core over `py_vollib`

A new module `src/algo_trading/analytics/greeks.py` with no I/O and no application dependencies
beyond `py_vollib`:

```
implied_forward(ce_ltp, pe_ltp, atm_strike, r, T) -> Decimal | None   # put-call parity
solve_iv(price, F, K, r, T, option_type) -> float | None              # py_vollib Black-76 IV
compute_greeks(price, F, K, r, T, option_type) -> Greeks | None       # IV + analytical greeks
```

`Greeks` is a frozen dataclass `(iv, delta, gamma, theta, vega)`. Every function is **total and
null-safe**: bad inputs (price below intrinsic, `T <= 0`, missing CE/PE leg, solver
non-convergence) return `None` and never raise. This unit is tested in isolation against known
Black-76 reference values.

*Why `py_vollib`:* robust IV inversion (via `py_lets_be_rational`) and analytical Greeks, well
tested. We price the ~20-22-strike band once per refresh, so the scalar API is sufficient — the
vectorized/`numba` variant (`py_vollib_vectorized`) is not needed and would only add image weight.

### Decision 2: Synthetic-forward Black-76

Index options here are European weeklies, cash-settled. Compute one forward per underlying per
refresh from put-call parity at the ATM strike (where both CE and PE quote):

```
F = K_atm + (CE_ltp - PE_ltp) * e^{r*T}
```

Apply `F` to every strike of that underlying/expiry and solve each option's IV with Black-76 on its
own market price. Greeks are the analytical Black-76 Greeks at the solved IV.

*Why not spot + Black-Scholes:* raw spot ignores the forward basis, so IV drifts from the
broker-displayed value, especially away from ATM. The parity forward needs no new data feed — both
legs are already quoted.

*Why not Black-76 on a real future:* would require subscribing the index future, a new data
dependency that is out of scope.

### Decision 3: Time-to-expiry and rate conventions

- **Time to expiry `T`:** calendar time from now to **15:30 IST on the expiry day**, annualized by
  `/365`. Floored just above zero. On expiry day after close, `T <= 0`, so greeks return `None`.
- **Risk-free rate `r`:** a new setting `risk_free_rate: Decimal = Decimal("0.065")` (~India
  T-bill), configurable via `ALGO_RISK_FREE_RATE`.
- **Dividend yield:** assumed 0 (index).

### Decision 4: Computed inside `OptionChainManager`

`OptionChainManager` (`feed/option_chain.py`) already holds the live spot, the resolved ATM, and
per-strike CE/PE LTP. On each chain refresh it computes the forward once, then greeks per tracked
strike-side, storing them on the existing per-strike `_Quote`. New accessor:

```
greeks_for(instrument_token) -> Greeks | None
```

is the strategy-facing interface. The compute pass is wrapped and logged so a greeks failure can
never break the trading loop (same discipline as the existing snapshot/pnl passes).

**Coverage:** greeks exist only for the ATM ± `strike_window` band the algo subscribes (~20-22
strikes) — the same band the strategy and dashboard already use. Strikes outside it have no LTP and
so no greeks.

### Decision 5: Persist as five nullable columns on the snapshot row

Extend `OptionChainSnapshotRow` with `iv, delta, gamma, theta, vega`. Each snapshot row is already
a single strike-side (one `instrument_token`), so this is 5 columns, not 10. Stored as `varchar`
(Decimal-as-string), matching the repo's existing money/number convention.

For the already-live hypertable, the columns are added via the existing `_ensure_chain_columns`
idiom in `bootstrap.py` (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) — the same path just used for
the `vwap` column. `create_all` covers fresh databases. `ADD COLUMN` is instant, so no
market-closed deploy window is required.

### Decision 6: API and dashboard surface

- `apps/api/app/schemas.py`: new `GreeksOut(iv, delta, gamma, theta, vega)`; `ChainStrikeOut` gains
  `ce_greeks: GreeksOut | None` and `pe_greeks: GreeksOut | None` (nested, nullable).
- Wired through both the polled `/api/chain` and the SSE `/api/stream` paths (mirroring the
  existing OI-trend fields in `dashboard/state_bridge.py`).
- Web (`apps/web`): the chain table renders IV + greeks per side, compact, `—` when null; the ATM
  row is already marked.

## Data flow

```
option tick (CE/PE ltp, per token)
   -> OptionChainManager.on_option_tick  (existing)
   -> refresh pass: implied_forward(ATM CE/PE) once per underlying
   -> per strike-side: compute_greeks(price, F, K, r, T, type) -> Greeks | None
   -> stored on _Quote
        |-> greeks_for(token)                      (strategy accessor)
        |-> chain_out / build_stream_payload       (API + SSE -> dashboard)
        |-> snapshot writer                         (iv/delta/gamma/theta/vega columns)
```

## Failure handling

Any solve failure resolves to `None` end to end: null DB column, null API field, `—` in the UI. The
greeks compute pass is exception-wrapped and logged; it cannot propagate into the trading loop.
Missing risk-free rate / expiry / a quoted leg all degrade to `None`, never an error.

## Testing

- **Pure core (unit):** known Black-76 reference values — IV round-trips (price -> IV -> price),
  greek signs and magnitudes at/around ATM; null paths (price < intrinsic, `T <= 0`, missing leg,
  non-convergence).
- **Forward:** parity forward is sane relative to spot for a constructed CE/PE pair.
- **Chain integration:** feed CE/PE ticks, assert `greeks_for` populates and the forward is sane.
- **Persistence:** a written snapshot round-trips the five columns (null when greeks unavailable).
- **API / stream:** `/api/chain` and `/api/stream` payloads carry `ce_greeks`/`pe_greeks` with the
  correct shape.

## Dependency & deployment

- Add `py_vollib` to `pyproject.toml` (pulls `scipy` / `numpy`) and bake it into the algo Docker
  image.
- Deploy is a normal image rebuild. The five snapshot columns self-add on first boot via
  `_ensure_chain_columns`; `ADD COLUMN` is instant, so no market-closed window is needed.
- No config is required to enable the feature; `risk_free_rate` has a sensible default.

## Open questions

None outstanding. Rate default (6.5%) and the 365-day annualization are operator-tunable via config
and the convention is documented here.
