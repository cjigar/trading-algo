# Nifty indicator panel (display-only)

**Date:** 2026-07-27
**Status:** Approved — ready for implementation plan

## Goal

Show a live technical-indicator panel for **Nifty** on the dashboard: eight indicators computed on
**5-minute and 15-minute** candles side by side, each rendered with its value(s) and a trend read
(bullish / bearish / neutral), plus a session-level Opening Range Breakout. **Display-only** — it must
not influence any entry, exit, or sizing decision, and no new math runs in the live trading loop.

### Indicators

| # | Indicator | Params | Purpose |
|---|-----------|--------|---------|
| 1 | EMA 9 / 21 / 50 | — | Trend direction |
| 2 | VWAP | intraday reset | Fair value |
| 3 | RSI | 14 | Momentum |
| 4 | MACD | 12 / 26 / 9 | Trend + momentum |
| 5 | Bollinger Bands | 20, 2σ | Volatility |
| 6 | ATR | 14 | Volatility (value only) |
| 7 | SuperTrend | 10, 3× ATR | Trend filter |
| 8 | ORB | first 30 min (09:15–09:45 IST) | Opening-range breakout |

VWAP and Wilder's ATR already exist as incremental classes in `strategy/indicators.py` and are reused.

## Approach (chosen: A — read-side compute from persisted candles)

The trading loop's only new responsibility is to build **5m and 15m Nifty candles** (separate from the
trading candle) and **persist** them. All indicator and ORB math is **pure and computed on the read
side** in `StateBridge`, from the persisted candles plus the live index spot. This keeps every bit of
new math off the live trading hot path and makes warm-up automatic: as candles accumulate and persist
across restarts/days, long-period indicators (e.g. 15m EMA50 ≈ 2 trading days) become ready and stay
ready.

Rejected: **B** (incremental compute in the loop) — puts indicator math on the hot path and needs
engine-state seeding to survive restarts. **C** (seed from Kotak historical candles) — best warm-up but
adds broker-history integration; may be layered on later if a day-one gap matters.

## Data flow

```
Nifty index ticks
  -> loop: CandleBuilder(5m) + CandleBuilder(15m)   (independent of the trading candle)
  -> on close: repo.upsert_nifty_candle(...)         (fail-safe; trailing ~7 trading days)

Dashboard read (StateBridge.read_state / indicator_panel)
  -> analytics/indicators.py (pure, batch): replay persisted candles + latest index spot
  -> per timeframe: {8 indicators: value(s) + trend label} + composite trend
  -> session-level ORB (shared across timeframes)
  -> IndicatorPanelOut -> /api/pnl + /api/stream (SSE) -> useStream -> dashboard panel
```

## Components

### 1. Persistence — `nifty_candles`

`NiftyCandleRow(SQLModel, table=True)`:
- `id` PK, `trading_day` (index), `underlying`, `timeframe_minutes` (index),
  `start`, `end`, `open`, `high`, `low`, `close`, `volume`.
- Unique `(underlying, timeframe_minutes, start)` → idempotent upsert (a candle re-persisted after a
  restart does not duplicate).
- Self-adding columns on bootstrap, following the existing greeks-columns pattern.

Repository methods:
- `upsert_nifty_candle(underlying, timeframe_minutes, candle, trading_day=None)`
- `nifty_candles(underlying, timeframe_minutes, lookback_days)` → candles ordered by `start`
- `trim_nifty_candles(keep_days=7)` — invoked on write so the table stays bounded.

### 2. Loop change — build & persist candles (`core/orchestrator.py`)

- Add per-underlying indicator `CandleBuilder`s for the configured indicator timeframes (`[5, 15]`),
  keyed by `(underlying, timeframe)`, populated only for the indicator underlying (Nifty).
- Feed Nifty index ticks (in the existing index-tick path) into these builders; on a returned closed
  candle, call `upsert_nifty_candle`.
- Entirely wrapped so any failure logs and never disrupts the trading pipeline. Gated by
  `indicators_enabled`.

### 3. Read-side engine — `src/algo_trading/analytics/indicators.py` (new, pure)

Batch functions over a `list[Candle]` (oldest→newest), each returning value(s) + a trend label
(`bullish` / `bearish` / `neutral`, or `na` while warming up):

| Indicator | Value(s) | Trend rule |
|---|---|---|
| EMA 9/21/50 | three EMAs | 9>21>50 & price>ema9 → bull; 9<21<50 & price<ema9 → bear; else neutral |
| VWAP | vwap, dist% | close>vwap → bull; close<vwap → bear; within a small dead-band → neutral |
| RSI 14 | rsi | >60 bull / <40 bear / else neutral; annotate >70 overbought, <30 oversold |
| MACD 12/26/9 | macd, signal, hist | macd>signal & hist>0 → bull; macd<signal & hist<0 → bear; else neutral |
| Bollinger 20/2σ | upper, mid, lower, bandwidth | position label: above upper / inside / below lower; bandwidth = volatility read |
| ATR 14 | atr, atr% | volatility only — direction `neutral` (no trend) |
| SuperTrend 10/3×ATR | line, direction | price above line → bull; below → bear (flip = signal) |
| ORB (session) | OR High, OR Low, price | price>OR High → bull breakout; <OR Low → bear breakdown; inside → neutral; "forming" before 09:45 |

- Reuses existing `ATR` and `SessionVWAP` by replaying candles; EMA / RSI / MACD / Bollinger /
  SuperTrend added as pure functions here.
- **Composite trend** per timeframe: aggregate the directional indicators (EMA, VWAP, RSI, MACD,
  SuperTrend, ORB — ATR and Bollinger are volatility, excluded) into `Bullish` / `Bearish` / `Neutral`
  with a tally (e.g. "5 / 6 bullish").
- Current price for level-break checks (VWAP dist, ORB break, SuperTrend/Bollinger position) comes from
  the existing live `index_spots`; indicator series themselves are computed from closed candles.
- ORB window = `market_open` .. `market_open + orb_minutes` (IST), OR high/low over that window's
  candles. Computed once per session and shared across both timeframe columns.

### 4. API / transport

- Pydantic: `IndicatorCellOut` (label + value(s)), `TimeframeIndicatorsOut` (the 8 cells + composite),
  `IndicatorPanelOut` (`{"5": …, "15": …, "orb": …, "as_of": …}`).
- `StateBridge.indicator_panel()` assembles it from persisted candles + spot.
- Add `indicators` to the `/api/pnl` response and the `/api/stream` payload, wired the same way as the
  existing pnl/greeks fields.

### 5. Dashboard (`apps/web`)

- New **"Nifty Indicators"** panel: two columns headed **5-min | 15-min**, one row per indicator with
  its value(s) and a colored trend chip (green / red / grey). A composite trend badge per column.
- ORB rendered once (above/below the columns): OR High, OR Low, current price, break state.
- Warming-up cells show "—". A freshness badge reuses the existing age-based pattern.
- Types added to `apps/web/lib/api.ts` and the stream payload in `apps/web/lib/useStream.ts`.

### 6. Config (`config/settings.py`)

- `indicators_enabled: bool = True`
- `indicator_timeframes: list[int] = [5, 15]`
- `indicator_underlying = NIFTY`
- `orb_minutes: int = 30`
- `nifty_candle_retention_days: int = 7`

## Error handling & edge cases

- Candle build/persist failures log and never propagate into the trading pipeline.
- Insufficient candles for an indicator → `na` label / "—" value (warm-up).
- ORB before 09:45 → "forming"; on a holiday / no candles → panel renders empty cells, no crash.
- Index feed stale → panel shows last computed values with the freshness badge flagging age.
- Zero-volume index candles → VWAP degrades to typical-price average (existing `SessionVWAP` behavior).

## Testing

- Pure indicator functions vs hand-computed fixtures on a small candle series (EMA, RSI, MACD,
  Bollinger, SuperTrend, ATR, VWAP).
- ORB: high/low over the 09:15–09:45 window; break / inside / forming states.
- Trend-label boundaries: RSI 40/60, EMA stack up/down, SuperTrend flip, MACD cross, composite tally.
- Warm-up: insufficient candles → `na`.
- Repository: upsert idempotency (same start not duplicated) + retention trim to `keep_days`.
- Loop: candle-builder feed → `upsert_nifty_candle` persistence path (fail-safe on error).

## Out of scope

- Feeding indicators into trading decisions (entries / exits / sizing) — a future spec.
- Timeframes other than 5m and 15m; underlyings other than Nifty.
- Historical (Kotak) candle seeding for instant day-one readiness (Approach C).

## Files touched

- `src/algo_trading/persistence/db.py`, `src/algo_trading/persistence/repositories.py`
- `src/algo_trading/persistence/bootstrap.py` (self-adding columns)
- `src/algo_trading/core/orchestrator.py`
- `src/algo_trading/analytics/indicators.py` (new)
- `src/algo_trading/dashboard/state_bridge.py`
- `apps/api/app/schemas.py`, `apps/api/app/routes.py`
- `apps/web/lib/api.ts`, `apps/web/lib/useStream.ts`, `apps/web/app/dashboard/*`
- `src/algo_trading/config/settings.py`
- `tests/`
