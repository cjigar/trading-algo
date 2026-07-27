# Nifty Indicator Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a display-only dashboard panel showing 8 technical indicators (EMA 9/21/50, VWAP, RSI 14, MACD 12/26/9, Bollinger 20/2σ, ATR 14, SuperTrend 10/3×ATR, and first-30-min ORB) for Nifty across 5-minute and 15-minute timeframes, each with a value and a bullish/bearish/neutral trend read plus a composite.

**Architecture:** The trading loop's only new job is to build 5m and 15m Nifty candles (independent of the trading candle) and persist them to a bounded `nifty_candles` table. All indicator math is **pure and computed read-side** in a new `analytics/indicators.py`, assembled by `StateBridge`, serialized into the `/api/stream` SSE payload, and rendered by a new dashboard panel. No new math touches live order logic.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy over TimescaleDB/PostgreSQL, Pydantic v2, FastAPI (SSE), Next.js/React/TypeScript, pytest, `uv`/`.venv`, `ruff`.

## Global Constraints

- Python is run inside the project venv: prefix every command with `source .venv/bin/activate &&`.
- Money/price math uses `Decimal`, never `float`, in the Python core. `float` appears only at the Pydantic serialization boundary (`*_Out` models), matching existing code.
- Display-only: nothing in this plan may alter entries, exits, sizing, or the order path. The only orchestrator change is building/persisting candles, wrapped so any failure logs and never propagates.
- Reuse the existing incremental `ATR` and `SessionVWAP` classes from `src/algo_trading/strategy/indicators.py`; do not reimplement them.
- IST timezone is `ZoneInfo("Asia/Kolkata")`; candle buckets are IST wall-clock aligned (see `strategy/candle_builder.py`). ORB window = `settings.market_open` .. `market_open + orb_minutes`.
- Indicators are Nifty-only and timeframes are `[5, 15]` for this plan.
- Lint must pass: `source .venv/bin/activate && ruff check <files>`.
- Trend label vocabulary is exactly: `"bullish"`, `"bearish"`, `"neutral"`, `"na"` (warming up / insufficient data).

---

### Task 1: Config settings

**Files:**
- Modify: `src/algo_trading/config/settings.py`
- Test: `tests/test_settings_indicators.py`

**Interfaces:**
- Produces: `Settings.indicators_enabled: bool`, `Settings.indicator_timeframes: list[int]` (default `[5, 15]`), `Settings.orb_minutes: int` (default `30`), `Settings.nifty_candle_retention_days: int` (default `7`). `indicator_timeframes` parses a CSV env string like `"5,15"` into `list[int]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_indicators.py`:

```python
from algo_trading.config.settings import Settings, get_settings


def test_indicator_defaults():
    s = get_settings(reload=True)
    assert s.indicators_enabled is True
    assert s.indicator_timeframes == [5, 15]
    assert s.orb_minutes == 30
    assert s.nifty_candle_retention_days == 7


def test_indicator_timeframes_parse_csv():
    s = Settings(indicator_timeframes="5,15,60")
    assert s.indicator_timeframes == [5, 15, 60]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_settings_indicators.py -v`
Expected: FAIL (`AttributeError`/`ValidationError` — fields do not exist yet).

- [ ] **Step 3: Add the fields and validator**

In `src/algo_trading/config/settings.py`, add these fields near the other display/candle settings (e.g. just after `candle_timeframe_minutes`). The `Annotated[..., NoDecode]` + `field_validator` mirror the existing `oi_trend_windows` pattern:

```python
    indicators_enabled: bool = True
    indicator_timeframes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [5, 15])
    orb_minutes: int = 30
    nifty_candle_retention_days: int = 7
```

Add the CSV validator next to `_parse_trend_windows`:

```python
    @field_validator("indicator_timeframes", mode="before")
    @classmethod
    def _parse_indicator_timeframes(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return [int(item.strip()) for item in v.split(",") if item.strip()]
```

`Annotated`, `NoDecode`, `Field`, and `field_validator` are already imported in this file (used by `oi_trend_windows`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_settings_indicators.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/config/settings.py tests/test_settings_indicators.py
git add src/algo_trading/config/settings.py tests/test_settings_indicators.py
git commit -m "feat(config): add Nifty indicator-panel settings"
```

---

### Task 2: Indicator numeric primitives + label helpers (pure)

**Files:**
- Create: `src/algo_trading/analytics/__init__.py`
- Create: `src/algo_trading/analytics/indicators.py`
- Test: `tests/test_indicators_primitives.py`

**Interfaces:**
- Consumes: `Candle` from `algo_trading.domain.models`; `ATR`, `SessionVWAP` from `algo_trading.strategy.indicators`.
- Produces (pure functions, all `Decimal` math):
  - `ema_last(values: list[Decimal], period: int) -> Decimal | None`
  - `rsi_last(closes: list[Decimal], period: int = 14) -> Decimal | None`
  - `macd_last(closes: list[Decimal], fast=12, slow=26, signal=9) -> tuple[Decimal, Decimal, Decimal] | None` returning `(macd, signal, hist)`
  - `bollinger_last(closes: list[Decimal], period=20, mult=Decimal(2)) -> tuple[Decimal, Decimal, Decimal] | None` returning `(upper, mid, lower)`
  - `supertrend_last(candles: list[Candle], period=10, mult=Decimal(3)) -> tuple[Decimal, str] | None` returning `(line, direction)` where direction is `"up"`/`"down"`
  - `atr_last(candles: list[Candle], period=14) -> Decimal | None`
  - `vwap_last(candles: list[Candle]) -> Decimal | None`
  - Label constants `BULLISH="bullish"`, `BEARISH="bearish"`, `NEUTRAL="neutral"`, `NA="na"`
  - Label helpers: `rsi_label(rsi: Decimal | None) -> str`, `ema_label(e9, e21, e50, price) -> str`, `macd_label(macd, signal, hist) -> str`, `supertrend_label(price, line, direction) -> str`, `vwap_label(price, vwap) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_indicators_primitives.py`:

```python
from decimal import Decimal

from algo_trading.analytics import indicators as ind
from algo_trading.domain.models import Candle
from datetime import datetime, timedelta


def _c(o, h, l, c, i=0):
    start = datetime(2025, 1, 15, 9, 15) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(c),
                  volume=Decimal(1))


def _closes(vals):
    return [Decimal(v) for v in vals]


def test_ema_of_constant_series_is_the_constant():
    assert ind.ema_last(_closes([10, 10, 10, 10]), 3) == Decimal(10)


def test_ema_insufficient_data_returns_none():
    assert ind.ema_last(_closes([10, 11]), 3) is None


def test_rsi_all_gains_is_100():
    assert ind.rsi_last(_closes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]), 14) == Decimal(100)


def test_rsi_all_losses_is_0():
    assert ind.rsi_last(_closes(list(range(20, 5, -1))), 14) == Decimal(0)


def test_macd_of_constant_series_is_zero():
    macd, signal, hist = ind.macd_last(_closes([50] * 40))
    assert macd == Decimal(0) and signal == Decimal(0) and hist == Decimal(0)


def test_bollinger_of_constant_series_collapses_to_the_mean():
    upper, mid, lower = ind.bollinger_last(_closes([100] * 20), 20, Decimal(2))
    assert upper == Decimal(100) and mid == Decimal(100) and lower == Decimal(100)


def test_supertrend_uptrend_is_up_and_below_price():
    candles = [_c(100 + i, 101 + i, 99 + i, 100.5 + i, i) for i in range(30)]
    line, direction = ind.supertrend_last(candles, 10, Decimal(3))
    assert direction == "up"
    assert line < candles[-1].close


def test_rsi_label_boundaries():
    assert ind.rsi_label(Decimal(65)) == ind.BULLISH
    assert ind.rsi_label(Decimal(35)) == ind.BEARISH
    assert ind.rsi_label(Decimal(50)) == ind.NEUTRAL
    assert ind.rsi_label(None) == ind.NA


def test_ema_label_stacked_up_is_bullish():
    assert ind.ema_label(Decimal(21), Decimal(20), Decimal(19), Decimal(22)) == ind.BULLISH
    assert ind.ema_label(Decimal(19), Decimal(20), Decimal(21), Decimal(18)) == ind.BEARISH
    assert ind.ema_label(Decimal(20), Decimal(20), Decimal(20), Decimal(20)) == ind.NEUTRAL
    assert ind.ema_label(None, Decimal(20), Decimal(21), Decimal(20)) == ind.NA
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_indicators_primitives.py -v`
Expected: FAIL (`ModuleNotFoundError: algo_trading.analytics`).

- [ ] **Step 3: Create the package and primitives**

Create `src/algo_trading/analytics/__init__.py` (empty file):

```python
```

Create `src/algo_trading/analytics/indicators.py`:

```python
"""Pure, batch technical indicators for the display-only Nifty panel.

Every function takes a plain list of closed candles / closes (oldest -> newest) and returns the
LATEST value(s), or None when there is not enough data to be meaningful (warm-up). No DB, no
network, no mutation of the live trading loop. Wilder's ATR and session VWAP are reused from the
existing incremental classes in strategy/indicators.py.
"""

from __future__ import annotations

from decimal import Decimal

from algo_trading.domain.models import Candle
from algo_trading.strategy.indicators import ATR, SessionVWAP

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"
NA = "na"


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    """Full EMA series (seeded with the SMA of the first ``period`` values). Empty if too short."""
    if len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], Decimal(0)) / Decimal(period)
    out = [seed]
    for v in values[period:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def ema_last(values: list[Decimal], period: int) -> Decimal | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi_last(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder's RSI. Needs period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, cur in zip(closes, closes[1:]):
        diff = cur - prev
        gains.append(diff if diff > 0 else Decimal(0))
        losses.append(-diff if diff < 0 else Decimal(0))
    avg_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    for g, ls in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + ls) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100)
    if avg_gain == 0:
        return Decimal(0)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def macd_last(
    closes: list[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(macd, signal, histogram) from the latest bar. Needs slow + signal closes."""
    if len(closes) < slow + signal:
        return None
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    # Align the two EMA series on their tails (fast is longer; trim its head).
    offset = len(fast_ema) - len(slow_ema)
    macd_line = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]
    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return None
    macd_v = macd_line[-1]
    signal_v = signal_series[-1]
    return macd_v, signal_v, macd_v - signal_v


def bollinger_last(
    closes: list[Decimal], period: int = 20, mult: Decimal = Decimal(2)
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(upper, mid, lower) from the latest ``period`` closes. Population std-dev."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window, Decimal(0)) / Decimal(period)
    var = sum(((v - mid) ** 2 for v in window), Decimal(0)) / Decimal(period)
    std = var.sqrt()
    return mid + mult * std, mid, mid - mult * std


def atr_last(candles: list[Candle], period: int = 14) -> Decimal | None:
    if len(candles) < period + 1:
        return None
    atr = ATR(period)
    value = None
    for c in candles:
        value = atr.update(c)
    return value


def supertrend_last(
    candles: list[Candle], period: int = 10, mult: Decimal = Decimal(3)
) -> tuple[Decimal, str] | None:
    """(supertrend line, direction) where direction is 'up' or 'down'. Needs period+1 candles."""
    if len(candles) < period + 1:
        return None
    atr = ATR(period)
    atr_val: Decimal | None = None
    final_upper: Decimal | None = None
    final_lower: Decimal | None = None
    st: Decimal | None = None
    direction = "up"
    for c in candles:
        atr_val = atr.update(c)
        if atr_val is None:
            continue
        hl2 = (c.high + c.low) / Decimal(2)
        basic_upper = hl2 + mult * atr_val
        basic_lower = hl2 - mult * atr_val
        if final_upper is None:
            final_upper, final_lower, st, direction = basic_upper, basic_lower, basic_lower, "up"
            continue
        final_upper = basic_upper if (basic_upper < final_upper or c.close > final_upper) else final_upper
        final_lower = basic_lower if (basic_lower > final_lower or c.close < final_lower) else final_lower
        if c.close > final_upper:
            direction = "up"
        elif c.close < final_lower:
            direction = "down"
        st = final_lower if direction == "up" else final_upper
    if st is None:
        return None
    return st, direction


def vwap_last(candles: list[Candle]) -> Decimal | None:
    if not candles:
        return None
    vwap = SessionVWAP()
    for c in candles:
        vwap.update(c)
    return vwap.value


# --- trend labels -------------------------------------------------------------------------

_RSI_BULL = Decimal(60)
_RSI_BEAR = Decimal(40)


def rsi_label(rsi: Decimal | None) -> str:
    if rsi is None:
        return NA
    if rsi >= _RSI_BULL:
        return BULLISH
    if rsi <= _RSI_BEAR:
        return BEARISH
    return NEUTRAL


def ema_label(e9, e21, e50, price) -> str:
    if e9 is None or e21 is None or e50 is None or price is None:
        return NA
    if e9 > e21 > e50 and price > e9:
        return BULLISH
    if e9 < e21 < e50 and price < e9:
        return BEARISH
    return NEUTRAL


def macd_label(macd, signal, hist) -> str:
    if macd is None:
        return NA
    if macd > signal and hist > 0:
        return BULLISH
    if macd < signal and hist < 0:
        return BEARISH
    return NEUTRAL


def supertrend_label(price, line, direction) -> str:
    if line is None or price is None:
        return NA
    return BULLISH if direction == "up" else BEARISH


def vwap_label(price, vwap) -> str:
    if price is None or vwap is None:
        return NA
    if price > vwap:
        return BULLISH
    if price < vwap:
        return BEARISH
    return NEUTRAL
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_indicators_primitives.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/analytics/ tests/test_indicators_primitives.py
git add src/algo_trading/analytics/ tests/test_indicators_primitives.py
git commit -m "feat(analytics): pure indicator primitives (EMA/RSI/MACD/Bollinger/SuperTrend/ATR/VWAP) + labels"
```

---

### Task 3: ORB, cell/timeframe assembly, composite trend (pure)

**Files:**
- Modify: `src/algo_trading/analytics/indicators.py`
- Test: `tests/test_indicators_panel.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `@dataclass(frozen=True) class Cell: label: str; values: dict[str, float]`
  - `orb_levels(candles: list[Candle], market_open: time, orb_minutes: int, tz: ZoneInfo) -> tuple[Decimal, Decimal] | None` → `(high, low)` over the opening window, or None if the window has no candles or is not yet complete.
  - `orb_cell(or_high, or_low, price) -> Cell`
  - `compute_timeframe(candles: list[Candle], price: Decimal | None) -> dict[str, Cell]` keyed `ema, vwap, rsi, macd, bollinger, atr, supertrend`
  - `composite_of(cells: list[Cell]) -> tuple[str, str]` → `(label, tally)` where tally is e.g. `"4 bull / 1 bear"`
  - `@dataclass(frozen=True) class IndicatorPanel: timeframes: dict[int, dict]; orb: Cell; as_of: datetime | None`
  - `compute_panel(candles_by_tf: dict[int, list[Candle]], price: Decimal | None, market_open: time, orb_minutes: int, tz: ZoneInfo, as_of: datetime | None = None) -> IndicatorPanel`. Each timeframe entry is `{"cells": {name: Cell}, "composite": str, "composite_tally": str}`. ORB is computed once from the smallest timeframe's candles and folded into every timeframe's composite.

- [ ] **Step 1: Write the failing test**

Create `tests/test_indicators_panel.py`:

```python
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from algo_trading.analytics import indicators as ind
from algo_trading.domain.models import Candle

IST = ZoneInfo("Asia/Kolkata")


def _c(i, o, h, l, c):
    start = datetime(2025, 1, 15, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(c),
                  volume=Decimal(1))


def test_orb_levels_over_first_30_min():
    # 09:15-09:45 = six 5-min candles (i=0..5); i>=6 is after the window.
    candles = [_c(i, 100, 100 + i, 90 - i, 100) for i in range(10)]
    high, low = ind.orb_levels(candles, time(9, 15), 30, IST)
    assert high == Decimal(105)   # max high in i=0..5 is 100+5
    assert low == Decimal(85)     # min low in i=0..5 is 90-5


def test_orb_cell_breakout_and_breakdown():
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(106)).label == ind.BULLISH
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(84)).label == ind.BEARISH
    assert ind.orb_cell(Decimal(105), Decimal(85), Decimal(95)).label == ind.NEUTRAL
    assert ind.orb_cell(None, None, Decimal(95)).label == ind.NA


def test_compute_timeframe_warmup_gives_na_cells():
    cells = ind.compute_timeframe([_c(0, 100, 101, 99, 100)], Decimal(100))
    assert cells["ema"].label == ind.NA
    assert cells["rsi"].label == ind.NA


def test_composite_majority_bullish():
    bull = ind.Cell(ind.BULLISH, {})
    bear = ind.Cell(ind.BEARISH, {})
    neut = ind.Cell(ind.NEUTRAL, {})
    label, tally = ind.composite_of([bull, bull, bull, bear, neut])
    assert label == ind.BULLISH
    assert tally == "3 bull / 1 bear"


def test_compute_panel_shape():
    candles = [_c(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(40)]
    panel = ind.compute_panel({5: candles, 15: candles}, Decimal(140), time(9, 15), 30, IST)
    assert set(panel.timeframes.keys()) == {5, 15}
    assert "cells" in panel.timeframes[5] and "composite" in panel.timeframes[5]
    assert panel.orb.label in (ind.BULLISH, ind.BEARISH, ind.NEUTRAL, ind.NA)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_indicators_panel.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute 'orb_levels'`).

- [ ] **Step 3: Add ORB, cells, composite, and panel assembly**

Append to `src/algo_trading/analytics/indicators.py`. Add these imports at the top of the file (extend the existing import block):

```python
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
```

Then append:

```python
@dataclass(frozen=True)
class Cell:
    """One indicator's rendered state: a trend label plus named numeric values (JSON-ready floats)."""

    label: str
    values: dict[str, float]


def _f(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def orb_levels(
    candles: list[Candle], market_open: time, orb_minutes: int, tz: ZoneInfo
) -> tuple[Decimal, Decimal] | None:
    """(high, low) of the opening range. None if no candle falls in the window yet."""
    open_dt_by_day: dict = {}
    window: list[Candle] = []
    for c in candles:
        local = c.start.astimezone(tz)
        day = local.date()
        if day not in open_dt_by_day:
            open_dt_by_day[day] = datetime.combine(day, market_open, tzinfo=tz)
        start_of_range = open_dt_by_day[day]
        end_of_range = start_of_range + timedelta(minutes=orb_minutes)
        if start_of_range <= local < end_of_range:
            window.append(c)
    if not window:
        return None
    return max(c.high for c in window), min(c.low for c in window)


def orb_cell(or_high: Decimal | None, or_low: Decimal | None, price: Decimal | None) -> Cell:
    values = {"or_high": _f(or_high), "or_low": _f(or_low), "price": _f(price)}
    if or_high is None or or_low is None or price is None:
        return Cell(NA, values)
    if price > or_high:
        return Cell(BULLISH, values)
    if price < or_low:
        return Cell(BEARISH, values)
    return Cell(NEUTRAL, values)


def compute_timeframe(candles: list[Candle], price: Decimal | None) -> dict[str, Cell]:
    closes = [c.close for c in candles]
    px = price if price is not None else (closes[-1] if closes else None)

    e9, e21, e50 = ema_last(closes, 9), ema_last(closes, 21), ema_last(closes, 50)
    ema = Cell(ema_label(e9, e21, e50, px), {"ema9": _f(e9), "ema21": _f(e21), "ema50": _f(e50)})

    vw = vwap_last(candles)
    vwap = Cell(vwap_label(px, vw), {"vwap": _f(vw)})

    r = rsi_last(closes, 14)
    rsi = Cell(rsi_label(r), {"rsi": _f(r)})

    m = macd_last(closes)
    if m is None:
        macd = Cell(NA, {"macd": None, "signal": None, "hist": None})
    else:
        macd = Cell(macd_label(*m), {"macd": _f(m[0]), "signal": _f(m[1]), "hist": _f(m[2])})

    b = bollinger_last(closes)
    if b is None or px is None:
        bollinger = Cell(NA, {"upper": None, "mid": None, "lower": None})
    else:
        upper, mid, lower = b
        label = BULLISH if px > upper else (BEARISH if px < lower else NEUTRAL)
        bollinger = Cell(label, {"upper": _f(upper), "mid": _f(mid), "lower": _f(lower),
                                 "bandwidth": _f((upper - lower))})

    a = atr_last(candles, 14)
    atr = Cell(NEUTRAL if a is not None else NA,
               {"atr": _f(a), "atr_pct": _f((a / px * Decimal(100)) if (a is not None and px) else None)})

    st = supertrend_last(candles)
    if st is None:
        supertrend = Cell(NA, {"line": None})
    else:
        line, direction = st
        supertrend = Cell(supertrend_label(px, line, direction), {"line": _f(line)})

    return {"ema": ema, "vwap": vwap, "rsi": rsi, "macd": macd,
            "bollinger": bollinger, "atr": atr, "supertrend": supertrend}


def composite_of(cells: list[Cell]) -> tuple[str, str]:
    bull = sum(1 for c in cells if c.label == BULLISH)
    bear = sum(1 for c in cells if c.label == BEARISH)
    tally = f"{bull} bull / {bear} bear"
    if bull > bear:
        return BULLISH, tally
    if bear > bull:
        return BEARISH, tally
    return NEUTRAL, tally


@dataclass(frozen=True)
class IndicatorPanel:
    timeframes: dict[int, dict]
    orb: Cell
    as_of: datetime | None


def compute_panel(
    candles_by_tf: dict[int, list[Candle]],
    price: Decimal | None,
    market_open: time,
    orb_minutes: int,
    tz: ZoneInfo,
    as_of: datetime | None = None,
) -> IndicatorPanel:
    # ORB is a session-level construct: compute it once from the finest timeframe available.
    orb = Cell(NA, {"or_high": None, "or_low": None, "price": _f(price)})
    if candles_by_tf:
        finest = candles_by_tf[min(candles_by_tf)]
        levels = orb_levels(finest, market_open, orb_minutes, tz)
        orb = orb_cell(levels[0], levels[1], price) if levels else orb_cell(None, None, price)

    timeframes: dict[int, dict] = {}
    for tf, candles in candles_by_tf.items():
        cells = compute_timeframe(candles, price)
        directional = [cells["ema"], cells["vwap"], cells["rsi"], cells["macd"],
                       cells["supertrend"], orb]
        label, tally = composite_of(directional)
        timeframes[tf] = {"cells": cells, "composite": label, "composite_tally": tally}
    return IndicatorPanel(timeframes=timeframes, orb=orb, as_of=as_of)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_indicators_panel.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/analytics/indicators.py tests/test_indicators_panel.py
git add src/algo_trading/analytics/indicators.py tests/test_indicators_panel.py
git commit -m "feat(analytics): ORB, per-timeframe cell assembly, and composite trend"
```

---

### Task 4: Persistence — `nifty_candles` table + repository methods

**Files:**
- Modify: `src/algo_trading/persistence/db.py`
- Modify: `src/algo_trading/persistence/repositories.py`
- Test: `tests/test_nifty_candles_repo.py`

**Interfaces:**
- Consumes: `Candle` from `algo_trading.domain.models`.
- Produces on `Repository`:
  - `upsert_nifty_candle(underlying: str, timeframe_minutes: int, candle: Candle, trading_day: date | None = None) -> None` — idempotent on `(underlying, timeframe_minutes, start)`; trims to `keep_days` after write.
  - `nifty_candles(underlying: str, timeframe_minutes: int, lookback_days: int) -> list[Candle]` — ordered oldest→newest.
  - `trim_nifty_candles(keep_days: int) -> int` — deletes rows older than `keep_days` trading days; returns deleted count.
- Produces `NiftyCandleRow` in `db.py`.

Note: `nifty_candles` is a small, retention-bounded **plain** table — NOT a Timescale hypertable. `SQLModel.metadata.create_all` (already called in `bootstrap_schema`) creates it on any fresh DB, so no bootstrap change is needed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_nifty_candles_repo.py`:

```python
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo_trading.domain.models import Candle
from algo_trading.persistence.repositories import Repository


def _candle(i, day=date(2025, 1, 15)):
    start = datetime(day.year, day.month, day.day, 9, 15) + timedelta(minutes=5 * i)
    return Candle(symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=5),
                  open=Decimal(100), high=Decimal(101), low=Decimal(99),
                  close=Decimal(100 + i), volume=Decimal(1))


def test_upsert_and_read_back(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 5, _candle(1), trading_day=date(2025, 1, 15))
    got = repo.nifty_candles("NIFTY", 5, lookback_days=5)
    assert [c.close for c in got] == [Decimal(100), Decimal(101)]


def test_upsert_is_idempotent_on_start(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))  # same start
    assert len(repo.nifty_candles("NIFTY", 5, lookback_days=5)) == 1


def test_timeframe_isolation(engine):
    repo = Repository(engine)
    repo.upsert_nifty_candle("NIFTY", 5, _candle(0), trading_day=date(2025, 1, 15))
    repo.upsert_nifty_candle("NIFTY", 15, _candle(0), trading_day=date(2025, 1, 15))
    assert len(repo.nifty_candles("NIFTY", 5, lookback_days=5)) == 1
    assert len(repo.nifty_candles("NIFTY", 15, lookback_days=5)) == 1
```

Note: the `engine` fixture is the shared test Postgres engine already used by `tests/test_orchestrator.py` (defined in `tests/conftest.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_nifty_candles_repo.py -v`
Expected: FAIL (`AttributeError: 'Repository' object has no attribute 'upsert_nifty_candle'`).

- [ ] **Step 3: Add the model**

In `src/algo_trading/persistence/db.py`, add after `IndexSpotRow` (near line 152):

```python
class NiftyCandleRow(SQLModel, table=True):
    """Closed index candles per (underlying, timeframe), for the display-only indicator panel.

    A bounded, retention-trimmed PLAIN table (not a hypertable): a few hundred rows per timeframe
    is all the indicator read path needs. Unique on (underlying, timeframe_minutes, start) so a
    candle re-persisted after a process restart is upserted in place rather than duplicated.
    """

    __tablename__ = "nifty_candles"

    id: int | None = Field(default=None, primary_key=True)
    trading_day: str = Field(index=True)
    underlying: str = Field(index=True)
    timeframe_minutes: int = Field(index=True)
    start: datetime = Field(index=True)
    end: datetime
    open: str = "0"
    high: str = "0"
    low: str = "0"
    close: str = "0"
    volume: str = "0"
```

- [ ] **Step 4: Add repository methods**

In `src/algo_trading/persistence/repositories.py`, add `NiftyCandleRow` to the `from algo_trading.persistence.db import (...)` block, then add these methods to the `Repository` class (near the other upsert methods). `pg_insert`, `Session`, `select`, `col`, `delete`, `date`, `timedelta`, `Decimal` are already imported at the top of this file:

```python
    def upsert_nifty_candle(
        self, underlying: str, timeframe_minutes: int, candle, trading_day: date | None = None
    ) -> None:
        """Persist one closed index candle idempotently, then trim old rows. Keyed on
        (underlying, timeframe_minutes, start): a candle re-seen after a restart is a no-op."""
        day = _today_str(trading_day)
        stmt = pg_insert(NiftyCandleRow).values(
            trading_day=day, underlying=str(underlying), timeframe_minutes=int(timeframe_minutes),
            start=candle.start, end=candle.end,
            open=str(candle.open), high=str(candle.high), low=str(candle.low),
            close=str(candle.close), volume=str(candle.volume),
        ).on_conflict_do_nothing(index_elements=["underlying", "timeframe_minutes", "start"])
        with Session(self._engine) as session:
            session.exec(stmt)
            session.commit()
        self.trim_nifty_candles(self._nifty_keep_days)

    def nifty_candles(self, underlying: str, timeframe_minutes: int, lookback_days: int) -> list:
        """Closed candles for (underlying, timeframe), oldest -> newest, within the lookback."""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days))
        with Session(self._engine) as session:
            rows = list(session.exec(
                select(NiftyCandleRow)
                .where(NiftyCandleRow.underlying == str(underlying))
                .where(NiftyCandleRow.timeframe_minutes == int(timeframe_minutes))
                .where(NiftyCandleRow.start >= cutoff)
                .order_by(col(NiftyCandleRow.start))
            ))
        return [_candle_from_row(r) for r in rows]

    def trim_nifty_candles(self, keep_days: int) -> int:
        """Delete candles older than ``keep_days`` days. Returns the number deleted."""
        cutoff = datetime.utcnow() - timedelta(days=keep_days)
        with Session(self._engine) as session:
            result = session.exec(delete(NiftyCandleRow).where(NiftyCandleRow.start < cutoff))
            session.commit()
            return result.rowcount or 0
```

The idempotent upsert needs a unique constraint on `(underlying, timeframe_minutes, start)` for `on_conflict_do_nothing` to target. Add it to `NiftyCandleRow` in `db.py` via `__table_args__`:

```python
    __table_args__ = (
        UniqueConstraint("underlying", "timeframe_minutes", "start",
                         name="uq_nifty_candle_key"),
    )
```

Add `from sqlalchemy import UniqueConstraint` to `db.py` imports if not present.

Add the `keep_days` default and a row→Candle helper near the top of `repositories.py` (module level, next to `_instrument_from_row`):

```python
def _candle_from_row(row) -> Candle:
    return Candle(
        symbol=f"{row.underlying}-IDX",
        start=row.start,
        end=row.end,
        open=Decimal(row.open),
        high=Decimal(row.high),
        low=Decimal(row.low),
        close=Decimal(row.close),
        volume=Decimal(row.volume),
    )
```

Add `Candle` to the `from algo_trading.domain.models import (...)` line in `repositories.py`. Initialize `self._nifty_keep_days` in `Repository.__init__` (default 7, overridable):

```python
        self._nifty_keep_days = 7
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_nifty_candles_repo.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/persistence/db.py src/algo_trading/persistence/repositories.py tests/test_nifty_candles_repo.py
git add src/algo_trading/persistence/db.py src/algo_trading/persistence/repositories.py tests/test_nifty_candles_repo.py
git commit -m "feat(persistence): nifty_candles table + idempotent upsert/read/trim"
```

---

### Task 5: Orchestrator — build & persist 5m/15m Nifty candles

**Files:**
- Modify: `src/algo_trading/core/orchestrator.py`
- Test: `tests/test_orchestrator_indicator_candles.py`

**Interfaces:**
- Consumes: `Repository.upsert_nifty_candle` (Task 4), `CandleBuilder` (existing), `Settings.indicators_enabled` / `indicator_timeframes` (Task 1).
- Produces: side effect — for each configured indicator timeframe, closed Nifty index candles are persisted via `upsert_nifty_candle`. Runs in both OI and VWAP modes, gated by `indicators_enabled`, and never raises into the pipeline.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_indicator_candles.py`:

```python
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from algo_trading.domain.enums import ExchangeSegment, Underlying
from algo_trading.domain.models import Tick

# Reuse the orchestrator test harness helpers.
from tests.test_orchestrator import _build, INDEX_TOKEN

IST = ZoneInfo("Asia/Kolkata")


def _tick(price, ts):
    return Tick(instrument_token=INDEX_TOKEN, exchange_segment=ExchangeSegment.NSE_FO,
               ltp=Decimal(price), timestamp=ts, is_index=True)


@freeze_time("2025-01-15")
def test_closed_5m_candle_is_persisted(engine):
    orch, _sm = _build(engine)
    base = datetime(2025, 1, 15, 9, 15, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    # Two ticks in the 09:15 bucket, then one in 09:20 -> closes the 09:15 5m candle.
    orch.publish_tick(_tick("100", base))
    orch.publish_tick(_tick("110", base + timedelta(minutes=1)))
    orch.publish_tick(_tick("120", base + timedelta(minutes=6)))
    got = orch.repo.nifty_candles("NIFTY", 5, lookback_days=5)
    assert len(got) == 1
    assert got[0].high == Decimal(110)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_orchestrator_indicator_candles.py -v`
Expected: FAIL (no candle persisted — `len(got) == 0`).

- [ ] **Step 3: Wire indicator candle builders into the orchestrator**

In `src/algo_trading/core/orchestrator.py` `__init__` (after `self._candles` is built, near line 140), add dedicated indicator builders for the indicator underlying (Nifty) per configured timeframe:

```python
        # Display-only indicator candles: independent of the trading candle, built for Nifty on
        # each configured timeframe and persisted for the read-side indicator panel.
        self._indicator_builders: dict[tuple[Underlying, int], CandleBuilder] = {}
        if getattr(self._settings, "indicators_enabled", False):
            for u in self._settings.underlyings:
                if u is not Underlying.NIFTY:
                    continue
                for tf in self._settings.indicator_timeframes:
                    self._indicator_builders[(u, tf)] = CandleBuilder(_underlying_symbol(u), tf)
```

In `_handle_tick`, after `underlying = self._underlying_token.get(tick.instrument_token)` (line ~470) and before the `if self._oi_mode:` branch, add the persist hook so it runs in **both** modes:

```python
        if underlying is not None and self._indicator_builders:
            self._build_indicator_candles(underlying, tick)
```

Add the method near `_handle_candle`:

```python
    def _build_indicator_candles(self, underlying, tick) -> None:
        """Feed an index tick into the per-timeframe indicator builders and persist any closed
        candle. Display-only and fully isolated: a failure here must never disturb trading."""
        for (u, tf), builder in self._indicator_builders.items():
            if u is not underlying:
                continue
            try:
                closed = builder.add_tick(tick)
                if closed is not None:
                    self._repo.upsert_nifty_candle(u.value, tf, closed)
            except Exception:  # noqa: BLE001 - indicator persistence must not stall the pipeline
                log.exception("indicator_candle_persist_failed", underlying=u.value, tf=tf)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_orchestrator_indicator_candles.py tests/test_orchestrator.py -v`
Expected: PASS (new test + all existing orchestrator tests still green).

- [ ] **Step 5: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/core/orchestrator.py tests/test_orchestrator_indicator_candles.py
git add src/algo_trading/core/orchestrator.py tests/test_orchestrator_indicator_candles.py
git commit -m "feat(orchestrator): build & persist 5m/15m Nifty candles for the indicator panel"
```

---

### Task 6: StateBridge — assemble the indicator panel

**Files:**
- Modify: `src/algo_trading/dashboard/state_bridge.py`
- Test: `tests/test_state_bridge_indicators.py`

**Interfaces:**
- Consumes: `Repository.nifty_candles` (Task 4), `compute_panel` + `IndicatorPanel` (Task 3), `Settings.indicator_timeframes/orb_minutes/market_open`.
- Produces: `StateBridge.indicator_panel() -> IndicatorPanel` — reads persisted candles per configured timeframe, resolves the current Nifty price from the latest `index_spots` row (fallback: latest candle close), and returns the computed panel with `as_of=datetime.utcnow()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_bridge_indicators.py`:

```python
from datetime import date, datetime, timedelta
from decimal import Decimal

from algo_trading.config.settings import get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.models import Candle
from algo_trading.persistence.repositories import Repository


def _seed(repo, tf, n):
    day = date(2025, 1, 15)
    for i in range(n):
        start = datetime(2025, 1, 15, 9, 15) + timedelta(minutes=tf * i)
        repo.upsert_nifty_candle("NIFTY", tf, Candle(
            symbol="NIFTY-IDX", start=start, end=start + timedelta(minutes=tf),
            open=Decimal(100 + i), high=Decimal(101 + i), low=Decimal(99 + i),
            close=Decimal(100 + i), volume=Decimal(1)), trading_day=day)


def test_indicator_panel_returns_both_timeframes(engine, monkeypatch):
    repo = Repository(engine)
    _seed(repo, 5, 60)
    _seed(repo, 15, 60)
    settings = get_settings(reload=True)
    bridge = StateBridge(settings)
    monkeypatch.setattr(bridge, "_repo", repo)
    panel = bridge.indicator_panel()
    assert set(panel.timeframes.keys()) == {5, 15}
    assert panel.timeframes[5]["composite"] in ("bullish", "bearish", "neutral")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_state_bridge_indicators.py -v`
Expected: FAIL (`AttributeError: 'StateBridge' object has no attribute 'indicator_panel'`).

- [ ] **Step 3: Implement `indicator_panel()`**

In `src/algo_trading/dashboard/state_bridge.py`, add imports:

```python
from zoneinfo import ZoneInfo

from algo_trading.analytics.indicators import IndicatorPanel, compute_panel
```

Store settings in `__init__` (the constructor currently only keeps `_repo` and `_quote_max_age`) — add:

```python
        self._settings = settings
```

Add the method to `StateBridge`:

```python
    def indicator_panel(self) -> IndicatorPanel:
        """Compute the display-only Nifty indicator panel from persisted 5m/15m candles.

        Current price for level-break reads (VWAP/ORB/SuperTrend/Bollinger) comes from the live
        index spot; the indicator series themselves are computed from closed candles. Read-only.
        """
        s = self._settings
        lookback = int(getattr(s, "nifty_candle_retention_days", 7))
        candles_by_tf = {
            tf: self._repo.nifty_candles("NIFTY", tf, lookback_days=lookback)
            for tf in s.indicator_timeframes
        }
        price = None
        for row in self._repo.index_spots():
            if row.underlying == "NIFTY":
                price = Decimal(row.ltp)
                break
        if price is None:
            newest = max((c[-1] for c in candles_by_tf.values() if c), default=None,
                         key=lambda c: c.start)
            price = newest.close if newest is not None else None
        return compute_panel(
            candles_by_tf, price, s.market_open, int(getattr(s, "orb_minutes", 30)),
            ZoneInfo("Asia/Kolkata"), as_of=datetime.utcnow(),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_state_bridge_indicators.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Lint + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/dashboard/state_bridge.py tests/test_state_bridge_indicators.py
git add src/algo_trading/dashboard/state_bridge.py tests/test_state_bridge_indicators.py
git commit -m "feat(dashboard): StateBridge.indicator_panel assembles the read-side panel"
```

---

### Task 7: API — serialize the panel into `/api/stream` and `/api/indicators`

**Files:**
- Modify: `apps/api/app/schemas.py`
- Modify: `apps/api/app/routes.py`
- Test: `tests/test_api_indicators.py`

**Interfaces:**
- Consumes: `StateBridge.indicator_panel()` (Task 6), `IndicatorPanel`/`Cell` (Task 3).
- Produces:
  - Pydantic `IndicatorCellOut(label: str, values: dict[str, float | None])`, `TimeframeIndicatorsOut(cells: dict[str, IndicatorCellOut], composite: str, composite_tally: str)`, `IndicatorPanelOut(timeframes: dict[str, TimeframeIndicatorsOut], orb: IndicatorCellOut, as_of: str | None)`.
  - `indicator_panel_out(panel: IndicatorPanel) -> IndicatorPanelOut` in `schemas.py`.
  - `build_stream_payload()` gains `"indicators": indicator_panel_out(bridge.indicator_panel()).model_dump()`.
  - `GET /api/indicators` route returning `IndicatorPanelOut`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_indicators.py`:

```python
from datetime import datetime
from decimal import Decimal

from algo_trading.analytics.indicators import Cell, IndicatorPanel
from apps.api.app.schemas import indicator_panel_out


def test_indicator_panel_out_serializes():
    panel = IndicatorPanel(
        timeframes={5: {"cells": {"rsi": Cell("bullish", {"rsi": 65.0})},
                        "composite": "bullish", "composite_tally": "1 bull / 0 bear"}},
        orb=Cell("neutral", {"or_high": 105.0, "or_low": 85.0, "price": 95.0}),
        as_of=datetime(2025, 1, 15, 4, 0, 0),
    )
    out = indicator_panel_out(panel)
    dumped = out.model_dump()
    assert dumped["timeframes"]["5"]["composite"] == "bullish"
    assert dumped["timeframes"]["5"]["cells"]["rsi"]["label"] == "bullish"
    assert dumped["orb"]["values"]["or_high"] == 105.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_api_indicators.py -v`
Expected: FAIL (`ImportError: cannot import name 'indicator_panel_out'`).

- [ ] **Step 3: Add schemas + adapter**

In `apps/api/app/schemas.py`, add near `EnginePnLOut`:

```python
class IndicatorCellOut(BaseModel):
    """One indicator's rendered state: trend label + named numeric values (null while warming up)."""

    label: str
    values: dict[str, float | None]


class TimeframeIndicatorsOut(BaseModel):
    cells: dict[str, IndicatorCellOut]
    composite: str
    composite_tally: str


class IndicatorPanelOut(BaseModel):
    timeframes: dict[str, TimeframeIndicatorsOut]  # keyed by timeframe minutes as a string
    orb: IndicatorCellOut
    as_of: str | None = None
```

Add the adapter function (near `pnl_out`):

```python
def _cell_out(cell) -> IndicatorCellOut:
    return IndicatorCellOut(label=cell.label, values=cell.values)


def indicator_panel_out(panel) -> IndicatorPanelOut:
    return IndicatorPanelOut(
        timeframes={
            str(tf): TimeframeIndicatorsOut(
                cells={name: _cell_out(c) for name, c in tf_data["cells"].items()},
                composite=tf_data["composite"],
                composite_tally=tf_data["composite_tally"],
            )
            for tf, tf_data in panel.timeframes.items()
        },
        orb=_cell_out(panel.orb),
        as_of=panel.as_of.isoformat() if panel.as_of else None,
    )
```

- [ ] **Step 4: Run the schema test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_api_indicators.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Wire into the stream payload and add the REST route**

In `apps/api/app/routes.py`, import the new adapter (extend the existing `from apps.api.app.schemas import (...)`): add `indicator_panel_out` and `IndicatorPanelOut`.

Add to the returned dict in `build_stream_payload()` (after the `"chain"` entry):

```python
        "indicators": indicator_panel_out(bridge.indicator_panel()).model_dump(),
```

Add a one-shot route near `get_pnl`:

```python
@api.get("/indicators", response_model=IndicatorPanelOut)
def get_indicators(bridge: StateBridge = Depends(get_bridge)):
    return indicator_panel_out(bridge.indicator_panel())
```

- [ ] **Step 6: Verify the payload builds**

Run: `source .venv/bin/activate && python -m pytest tests/test_api_indicators.py -v && ruff check apps/api/app/schemas.py apps/api/app/routes.py`
Expected: PASS + clean lint. (`build_stream_payload` is covered end-to-end by the existing stream tests; run `python -m pytest tests/ -k stream -v` if present.)

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/schemas.py apps/api/app/routes.py tests/test_api_indicators.py
git commit -m "feat(api): serialize indicator panel into /api/stream and /api/indicators"
```

---

### Task 8: Web types — `Indicators` in `api.ts` and the stream payload

**Files:**
- Modify: `apps/web/lib/api.ts`

**Interfaces:**
- Produces TypeScript types mirroring Task 7's schema: `IndicatorCell`, `TimeframeIndicators`, `IndicatorPanel`, and `StreamPayload.indicators`.

- [ ] **Step 1: Add the types**

In `apps/web/lib/api.ts`, add before `StreamPayload`:

```typescript
// One indicator's rendered state: a trend label plus named numeric values (null while warming up).
export type TrendLabel = "bullish" | "bearish" | "neutral" | "na";
export type IndicatorCell = { label: TrendLabel; values: Record<string, number | null> };
export type TimeframeIndicators = {
  cells: Record<string, IndicatorCell>;
  composite: TrendLabel;
  composite_tally: string;
};
export type IndicatorPanel = {
  timeframes: Record<string, TimeframeIndicators>; // keyed by timeframe minutes, e.g. "5", "15"
  orb: IndicatorCell;
  as_of: string | null;
};
```

Extend `StreamPayload` to include the field:

```typescript
export type StreamPayload = {
  state: AlgoState; pnl: PnL; positions: Position[]; orders: Order[];
  broker_pnl: BrokerPnL; broker_positions: Record<string, unknown>[]; broker_trades: Trade[];
  chain: Chain; indicators: IndicatorPanel;
};
```

- [ ] **Step 2: Type-check**

Run: `cd apps/web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add apps/web/lib/api.ts
git commit -m "feat(web): indicator-panel types + stream payload field"
```

---

### Task 9: Web dashboard — the Nifty Indicators panel

**Files:**
- Create: `apps/web/components/IndicatorPanel.tsx`
- Modify: `apps/web/app/dashboard/page.tsx`

**Interfaces:**
- Consumes: `IndicatorPanel` type + `useStream()` data (`data.indicators`).
- Produces: a React panel rendering ORB once, then a 5-min | 15-min two-column table of the 8 indicators with a colored trend chip and each indicator's value(s), plus a composite badge per column.

- [ ] **Step 1: Create the component**

Create `apps/web/components/IndicatorPanel.tsx`:

```tsx
"use client";

import type { IndicatorCell, IndicatorPanel, TrendLabel } from "../lib/api";

const CHIP: Record<TrendLabel, string> = {
  bullish: "bg-green-800 text-green-200",
  bearish: "bg-red-800 text-red-200",
  neutral: "bg-neutral-700 text-neutral-300",
  na: "bg-neutral-800 text-neutral-500",
};

// Indicator display order + how to render each cell's numbers.
const ROWS: { key: string; label: string; fmt: (v: Record<string, number | null>) => string }[] = [
  { key: "ema", label: "EMA 9/21/50", fmt: (v) => `${num(v.ema9)} / ${num(v.ema21)} / ${num(v.ema50)}` },
  { key: "vwap", label: "VWAP", fmt: (v) => num(v.vwap) },
  { key: "rsi", label: "RSI 14", fmt: (v) => num(v.rsi) },
  { key: "macd", label: "MACD", fmt: (v) => `${num(v.macd)} / ${num(v.signal)}` },
  { key: "bollinger", label: "Bollinger", fmt: (v) => `${num(v.upper)} / ${num(v.lower)}` },
  { key: "atr", label: "ATR 14", fmt: (v) => num(v.atr) },
  { key: "supertrend", label: "SuperTrend", fmt: (v) => num(v.line) },
];

function num(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

function Chip({ cell }: { cell?: IndicatorCell }) {
  const label = cell?.label ?? "na";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${CHIP[label]}`}>{label}</span>;
}

export function IndicatorPanelView({ panel }: { panel: IndicatorPanel }) {
  const tfs = Object.keys(panel.timeframes).sort((a, b) => Number(a) - Number(b));
  const orb = panel.orb.values;
  return (
    <div className="rounded-lg bg-neutral-900 p-4">
      <h2 className="mb-2 text-sm font-semibold text-neutral-200">Nifty Indicators</h2>
      <div className="mb-3 flex items-center gap-3 text-xs text-neutral-400">
        <span>ORB (first 30m):</span>
        <span>High {num(orb.or_high)}</span>
        <span>Low {num(orb.or_low)}</span>
        <span>LTP {num(orb.price)}</span>
        <Chip cell={panel.orb} />
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-neutral-400">
            <th className="text-left font-normal">Indicator</th>
            {tfs.map((tf) => (
              <th key={tf} className="text-right font-normal">{tf}-min</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ROWS.map((row) => (
            <tr key={row.key} className="border-t border-neutral-800">
              <td className="py-1 text-neutral-300">{row.label}</td>
              {tfs.map((tf) => {
                const cell = panel.timeframes[tf].cells[row.key];
                return (
                  <td key={tf} className="py-1 text-right">
                    <span className="mr-2 text-neutral-200">{cell ? row.fmt(cell.values) : "—"}</span>
                    <Chip cell={cell} />
                  </td>
                );
              })}
            </tr>
          ))}
          <tr className="border-t border-neutral-700">
            <td className="py-1 font-semibold text-neutral-200">Composite</td>
            {tfs.map((tf) => (
              <td key={tf} className="py-1 text-right">
                <span className="mr-2 text-xs text-neutral-500">{panel.timeframes[tf].composite_tally}</span>
                <Chip cell={{ label: panel.timeframes[tf].composite, values: {} }} />
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Mount it on the dashboard**

In `apps/web/app/dashboard/page.tsx`, import the component and render it where `data.indicators` is available (near the P&L/chain panels). Add the import:

```tsx
import { IndicatorPanelView } from "../../components/IndicatorPanel";
```

And in the JSX, guarded so it only renders once data arrives:

```tsx
{data?.indicators && <IndicatorPanelView panel={data.indicators} />}
```

- [ ] **Step 3: Type-check + build**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: type-check clean, build succeeds.

- [ ] **Step 4: Manual smoke check**

Start the stack (API + web) per the repo's dev instructions, open the dashboard, and confirm the "Nifty Indicators" panel renders two columns (5-min / 15-min), the ORB line, and colored chips. Warming-up cells show "—" until enough candles exist.

- [ ] **Step 5: Commit**

```bash
git add apps/web/components/IndicatorPanel.tsx apps/web/app/dashboard/page.tsx
git commit -m "feat(web): Nifty indicator panel (5m/15m + ORB + composite)"
```

---

### Task 10: Full-suite regression + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-27-nifty-indicator-panel-design.md` (mark status Implemented)

- [ ] **Step 1: Run the full Python suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: all pass (existing + new).

- [ ] **Step 2: Lint the whole change set**

Run: `source .venv/bin/activate && ruff check src/ tests/ apps/api`
Expected: clean.

- [ ] **Step 3: Update spec status + commit**

Edit the spec header `**Status:**` to `Implemented — 2026-07-27`, then:

```bash
git add docs/superpowers/specs/2026-07-27-nifty-indicator-panel-design.md
git commit -m "docs(spec): mark Nifty indicator panel implemented"
```

---

## Self-Review

**Spec coverage:**
- 8 indicators + trend reads → Tasks 2 (primitives + labels), 3 (cells/ORB/composite). ✓
- 5m/15m side by side → Tasks 5 (build both), 6 (assemble both), 9 (two-column render). ✓
- Read-side compute, loop only persists candles → Tasks 4/5 (persist) vs 3/6 (pure read-side compute). ✓
- ORB first 30 min, shared → Task 3 `orb_levels`/`compute_panel` (computed once), Task 9 (rendered once). ✓
- Persistence + retention → Task 4 (`upsert`/`trim`, idempotent). ✓
- Config → Task 1. ✓
- API + SSE + one-shot endpoint → Task 7. ✓
- Dashboard panel → Tasks 8/9. ✓
- Warm-up → `na`/"—" handled in Tasks 3 (na cells) and 9 (render). ✓
- Fail-safe loop isolation → Task 5 (`try/except`, gated by `indicators_enabled`). ✓
- Error/edge cases (holiday/no candles, ORB forming, zero-volume VWAP) → covered by `na`/`None` guards throughout; ORB before window complete → `orb_levels` returns None → `na` cell.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows real assertions. ✓

**Type consistency:** `Cell(label, values)`, `compute_panel(...) -> IndicatorPanel`, `IndicatorPanel.timeframes[tf] = {"cells", "composite", "composite_tally"}`, `indicator_panel_out` keys timeframes by `str(tf)` — consistent across Tasks 3, 6, 7, 8, 9. `upsert_nifty_candle`/`nifty_candles` signatures match between Tasks 4, 5, 6. ✓

**One note for the implementer:** Task 4 assumes a shared `engine` pytest fixture (used by `tests/test_orchestrator.py`). If `tests/conftest.py` does not expose it, reuse the exact fixture that `test_orchestrator.py` relies on (same import path).
