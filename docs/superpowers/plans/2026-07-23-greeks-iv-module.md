# Greeks/IV Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute per-strike option IV and Greeks (iv/delta/gamma/theta/vega) client-side from the data already flowing, and surface them on the dashboard, in persisted history, and via a strategy-readable accessor.

**Architecture:** A pure numeric core (`analytics/greeks.py`) over `py_vollib` solves IV against a put-call-parity forward (Black-76) and derives analytical Greeks. `OptionChainManager` computes greeks per option tick, stores them on each `ChainQuote` (strategy accessor `greeks_for`), and includes them in the snapshot it persists. The dashboard chain — rebuilt from persisted rows via `summarize_chain` → `chain_out` — reads the persisted greeks and renders them; the SSE stream reuses the same assembly. No strategy behavior changes this round (expose only).

**Tech Stack:** Python 3.11, `py_vollib` (Black-76 IV + analytical greeks; pulls scipy/numpy), SQLModel/TimescaleDB, FastAPI, Next.js/React + TypeScript.

## Global Constraints

- Python `>=3.11`.
- Monetary/numeric values persist as **Decimal-as-string** (`varchar`), matching the existing `ltp`/`vwap` columns.
- Greeks computation is **null-safe end to end**: any failure (price below intrinsic, `T<=0`, missing CE/PE leg, non-convergence) resolves to `None` / null column / `—` in UI and **must never raise into the trading loop**.
- **No strategy behavior change** this round — `oi_selling`/`vwap_breakout` entry/exit logic is untouched (`greeks_for` is exposed but unused by them).
- Values computed/surfaced/persisted: `iv, delta, gamma, theta, vega` (no rho).
- Coverage is the ATM ± `strike_window` band already subscribed (~20–22 strikes); strikes with no LTP have no greeks.
- Pricing: synthetic forward `F = K_atm + (CE_ltp − PE_ltp)·e^{rT}` at the ATM strike, applied per underlying/expiry; Black-76 IV then analytical greeks at that IV.
- Conventions: `r = risk_free_rate` (default `0.065`, config `ALGO_RISK_FREE_RATE`); dividend yield 0; time-to-expiry = calendar time to **15:30 IST on expiry day**, annualized `/365`.
- Python tests run against a real Postgres (`make db-up` provides it on `127.0.0.1:55432`); `ruff` and `mypy` must stay clean.
- `py_vollib` emits a `DeprecationWarning` (successor is `vollib`). We pin `py_vollib` as chosen; the warning is benign. Filter it in the module if it appears in test output.

## File Structure

- Create `src/algo_trading/analytics/__init__.py` — new analytics package.
- Create `src/algo_trading/analytics/greeks.py` — pure core: `Greeks` dataclass + `year_fraction`, `implied_forward`, `solve_iv`, `compute_greeks`.
- Create `tests/test_greeks.py` — pure-core unit tests.
- Modify `pyproject.toml` — add `py_vollib` to `[project.dependencies]`.
- Modify `src/algo_trading/config/settings.py` — add `risk_free_rate` setting + reload-copy list.
- Modify `src/algo_trading/feed/option_chain.py` — compute/store greeks on `ChainQuote`, `greeks_for` accessor, greeks in writer dict.
- Create `tests/test_option_chain_greeks.py` — chain-manager integration test.
- Modify `src/algo_trading/persistence/db.py` — 5 nullable columns on `OptionChainSnapshotRow`.
- Modify `src/algo_trading/persistence/bootstrap.py` — extend `_ensure_chain_columns` to add the 5 columns.
- Modify `src/algo_trading/persistence/repositories.py` — `write_chain_snapshots` passes greeks through.
- Modify `tests/test_timescale_schema.py` — assert columns self-add.
- Modify `tests/test_option_chain_persistence.py` — round-trip greeks columns.
- Modify `src/algo_trading/reporting.py` — `ChainStrike` gains `ce_greeks`/`pe_greeks`; `summarize_chain` reads them.
- Modify `tests/test_reporting_chain.py` (or nearest reporting-chain test) — greeks pivot.
- Modify `apps/api/app/schemas.py` — `GreeksOut` + `ChainStrikeOut.ce_greeks/pe_greeks` + `chain_out` mapping.
- Modify `apps/api/tests/test_chain_api.py` (or nearest chain API test) — API shape.
- Modify `apps/web/lib/api.ts` — `Greeks` type + `ChainStrike` fields.
- Modify `apps/web/app/dashboard/page.tsx` — render an IV/greeks cell per side.

---

### Task 1: Pure greeks core + dependency

**Files:**
- Modify: `pyproject.toml` (add `py_vollib` to `[project.dependencies]`)
- Create: `src/algo_trading/analytics/__init__.py`
- Create: `src/algo_trading/analytics/greeks.py`
- Test: `tests/test_greeks.py`

**Interfaces:**
- Produces:
  - `Greeks` — frozen dataclass with float fields `iv, delta, gamma, theta, vega`.
  - `year_fraction(now: datetime, expiry: date) -> float` — annualized time to 15:30 IST on `expiry` (0.0 once expired).
  - `implied_forward(ce_ltp: float, pe_ltp: float, atm_strike: float, r: float, T: float) -> float | None` — put-call-parity forward; `None` when `T<=0`.
  - `solve_iv(price: float, F: float, K: float, r: float, T: float, option_type: OptionType) -> float | None`.
  - `compute_greeks(price: float, F: float, K: float, r: float, T: float, option_type: OptionType) -> Greeks | None`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, inside `[project]` `dependencies = [ ... ]`, add a line after `"python-dotenv>=1.0",`:

```toml
    "py_vollib>=1.0.1",
```

- [ ] **Step 2: Install it into the dev venv**

Run: `source .venv/bin/activate && pip install "py_vollib>=1.0.1"`
Expected: `Successfully installed ... py_vollib ...` (pulls `py_lets_be_rational`, `scipy`, `numpy`, `simplejson`).

- [ ] **Step 3: Write the failing tests**

Create `tests/test_greeks.py`:

```python
"""Pure Black-76 greeks core: IV round-trips, greek sanity, and null-safety."""

from __future__ import annotations

from datetime import UTC, date, datetime

from algo_trading.analytics.greeks import (
    Greeks,
    compute_greeks,
    implied_forward,
    solve_iv,
    year_fraction,
)
from algo_trading.domain.enums import OptionType

# Reference point verified against py_vollib: F=100,K=100,r=0.065,T=7/365,sigma=0.20
# -> black price 1.1035; greeks delta 0.5049, gamma 0.14384, theta -0.07862, vega 0.05517
_F, _K, _R, _T = 100.0, 100.0, 0.065, 7 / 365


def test_solve_iv_round_trips():
    price = 1.1035  # black('c', F, K, T, r, 0.20)
    iv = solve_iv(price, _F, _K, _R, _T, OptionType.CE)
    assert iv is not None
    assert abs(iv - 0.20) < 1e-3


def test_compute_greeks_atm_call_signs_and_magnitudes():
    g = compute_greeks(1.1035, _F, _K, _R, _T, OptionType.CE)
    assert g is not None
    assert abs(g.iv - 0.20) < 1e-3
    assert 0.45 < g.delta < 0.55          # ATM call ~0.5
    assert g.gamma > 0
    assert g.theta < 0                    # long option decays
    assert g.vega > 0


def test_put_delta_is_negative():
    g = compute_greeks(1.1035, _F, _K, _R, _T, OptionType.PE)
    assert g is not None
    assert -0.55 < g.delta < -0.45


def test_implied_forward_from_parity():
    # CE-PE = 5 at K=100 with ~0 rate/time -> F ~ 105
    f = implied_forward(ce_ltp=7.0, pe_ltp=2.0, atm_strike=100.0, r=0.065, T=_T)
    assert f is not None
    assert abs(f - 105.0) < 0.1


def test_null_paths():
    assert implied_forward(7.0, 2.0, 100.0, 0.065, 0.0) is None          # T<=0
    assert solve_iv(1.0, _F, _K, _R, 0.0, OptionType.CE) is None          # T<=0
    assert solve_iv(0.0, _F, _K, _R, _T, OptionType.CE) is None           # non-positive price
    assert solve_iv(0.0001, 100.0, 50.0, _R, _T, OptionType.CE) is None   # below intrinsic -> caught
    assert compute_greeks(0.0, _F, _K, _R, _T, OptionType.CE) is None


def test_year_fraction_expiry_day_after_close_is_zero():
    # 2025-01-30 16:00 IST is after the 15:30 close on expiry day 2025-01-30
    now = datetime(2025, 1, 30, 10, 30, tzinfo=UTC)  # 16:00 IST
    assert year_fraction(now, date(2025, 1, 30)) == 0.0


def test_year_fraction_positive_before_expiry():
    now = datetime(2025, 1, 23, 4, 0, tzinfo=UTC)  # ~09:30 IST, 7 days out
    t = year_fraction(now, date(2025, 1, 30))
    assert 0.017 < t < 0.021  # ~7/365
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_greeks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'algo_trading.analytics'`.

- [ ] **Step 5: Create the package marker**

Create `src/algo_trading/analytics/__init__.py`:

```python
"""Numeric analytics that derive from market data (option greeks, IV)."""
```

- [ ] **Step 6: Implement the pure core**

Create `src/algo_trading/analytics/greeks.py`:

```python
"""Black-76 implied volatility and analytical greeks over a put-call-parity forward.

Pure and null-safe: every function returns ``None`` on bad input (price below intrinsic,
non-positive time, missing leg, solver non-convergence) and never raises, so a greeks failure
cannot propagate into the trading loop. Index options are European, cash-settled, so Black-76 on
the forward is the right model; the forward comes from put-call parity, not raw spot.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from py_vollib.black.greeks.analytical import delta, gamma, theta, vega
    from py_vollib.black.implied_volatility import implied_volatility

from algo_trading.domain.enums import OptionType

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_CLOSE = time(15, 30)
_SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def _flag(option_type: OptionType) -> str:
    return "c" if option_type is OptionType.CE else "p"


def year_fraction(now: datetime, expiry: date) -> float:
    """Annualized time from ``now`` to 15:30 IST on ``expiry`` (0.0 once expired)."""
    now_ist = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(IST)
    expiry_dt = datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=IST)
    seconds = (expiry_dt - now_ist).total_seconds()
    return seconds / _SECONDS_PER_YEAR if seconds > 0 else 0.0


def implied_forward(
    ce_ltp: float, pe_ltp: float, atm_strike: float, r: float, T: float
) -> float | None:
    """Forward from put-call parity: C - P = (F - K) e^{-rT}  =>  F = K + (C-P) e^{rT}."""
    if T <= 0:
        return None
    return atm_strike + (ce_ltp - pe_ltp) * math.exp(r * T)


def solve_iv(
    price: float, F: float, K: float, r: float, T: float, option_type: OptionType
) -> float | None:
    if T <= 0 or price <= 0 or F <= 0:
        return None
    try:
        return float(implied_volatility(price, F, K, r, T, _flag(option_type)))
    except Exception:  # noqa: BLE001 - below-intrinsic / non-convergence must degrade to None
        return None


def compute_greeks(
    price: float, F: float, K: float, r: float, T: float, option_type: OptionType
) -> Greeks | None:
    iv = solve_iv(price, F, K, r, T, option_type)
    if iv is None:
        return None
    flag = _flag(option_type)
    try:
        return Greeks(
            iv=iv,
            delta=float(delta(flag, F, K, T, r, iv)),
            gamma=float(gamma(flag, F, K, T, r, iv)),
            theta=float(theta(flag, F, K, T, r, iv)),
            vega=float(vega(flag, F, K, T, r, iv)),
        )
    except Exception:  # noqa: BLE001
        return None
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_greeks.py -q`
Expected: PASS (7 passed).

- [ ] **Step 8: Lint & type-check**

Run: `source .venv/bin/activate && ruff check src/algo_trading/analytics tests/test_greeks.py && mypy src/algo_trading/analytics/greeks.py`
Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml src/algo_trading/analytics tests/test_greeks.py
git commit -m "feat(analytics): pure Black-76 IV + greeks core over py_vollib"
```

---

### Task 2: Config — risk-free rate

**Files:**
- Modify: `src/algo_trading/config/settings.py`
- Test: `tests/test_greeks_config.py` (create)

**Interfaces:**
- Produces: `Settings.risk_free_rate: Decimal` (default `0.065`, env `ALGO_RISK_FREE_RATE`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_greeks_config.py`:

```python
from decimal import Decimal

from algo_trading.config.settings import get_settings


def test_risk_free_rate_default():
    s = get_settings(reload=True)
    assert s.risk_free_rate == Decimal("0.065")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_greeks_config.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'risk_free_rate'`.

- [ ] **Step 3: Add the setting**

In `src/algo_trading/config/settings.py`, add this field next to the other strategy-parameter fields (near `oi_trend_flat_threshold`, around line 100):

```python
    # Annualized risk-free rate used to price the put-call-parity forward and solve option IV.
    # ~India T-bill. Dividend yield is assumed 0 for the index.
    risk_free_rate: Decimal = Decimal("0.065")
```

- [ ] **Step 4: Add it to the reload-copy list**

In `settings.py`, find the tuple of field names copied on reload (the block listing `"target_points", "trail_points", "stoploss_points", ...` around line 297) and add `"risk_free_rate"` to it so `get_settings(reload=True)` preserves it:

```python
    "target_points", "trail_points", "stoploss_points", "vwap_breakout_buffer",
    "strike_window", "chain_feed_window", "otm_strikes", "chain_eval_seconds",
    "daily_loss_cap", "max_positions", "max_trades_per_day", "flatten_on_kill_switch",
    "risk_free_rate",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_greeks_config.py -q`
Expected: PASS.

- [ ] **Step 6: Lint & type-check + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/config/settings.py tests/test_greeks_config.py && mypy src/algo_trading/config/settings.py
git add src/algo_trading/config/settings.py tests/test_greeks_config.py
git commit -m "feat(config): add risk_free_rate for greeks/IV pricing"
```

---

### Task 3: Chain-manager integration + strategy accessor

**Files:**
- Modify: `src/algo_trading/feed/option_chain.py`
- Test: `tests/test_option_chain_greeks.py` (create)

**Interfaces:**
- Consumes: `Greeks`, `implied_forward`, `compute_greeks`, `year_fraction` (Task 1); `Settings.risk_free_rate` (Task 2).
- Produces:
  - `ChainQuote.greeks: Greeks | None` (new field).
  - `OptionChainManager.greeks_for(instrument_token: str) -> Greeks | None`.
  - Writer dict gains keys `iv, delta, gamma, theta, vega` (Decimal-as-string or `None`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_option_chain_greeks.py`:

```python
"""OptionChainManager computes per-strike greeks from the parity forward."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment
from algo_trading.domain.models import Tick
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster


@pytest.fixture()
def resolver():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"{k}{ot}",
                         "pSymbolName": "NIFTY", "pExpiryDate": "2025-01-30",
                         "dStrikePrice": k, "pOptionType": ot, "lLotSize": 75})
    sm = ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)
    return WeeklyOptionResolver(sm)


def _settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    object.__setattr__(s, "risk_free_rate", Decimal("0.065"))
    return s


def _captured(writer_rows, token):
    return next(r for r in reversed(writer_rows) if r["instrument_token"] == token)


def test_greeks_for_populated_and_written(resolver):
    written: list[dict] = []
    m = OptionChainManager(_settings(), resolver,
                           snapshot_writer=type("W", (), {"add": lambda self, r: written.append(r)})())
    # 7 days before the 2025-01-30 expiry
    ts = datetime(2025, 1, 23, 4, 0, tzinfo=UTC)
    m.on_index_tick(Tick(instrument_token="IDX", exchange_segment=ExchangeSegment.NSE_CM,
                         ltp=Decimal("23000"), timestamp=ts, is_index=True))
    # ATM = 23000: feed CE and PE legs so the forward resolves, plus a wing strike.
    for token, ltp in [("23000CE", "120"), ("23000PE", "115"), ("23100CE", "70")]:
        m.on_option_tick(Tick(instrument_token=token, exchange_segment=ExchangeSegment.NSE_FO,
                              ltp=Decimal(ltp), timestamp=ts, oi=1000))

    g = m.greeks_for("23100CE")
    assert g is not None
    assert 0.0 < g.iv < 3.0            # a sane implied vol
    assert 0.0 < g.delta < 1.0         # OTM call delta
    assert g.gamma > 0 and g.vega > 0 and g.theta < 0

    row = _captured(written, "23100CE")
    assert row["iv"] is not None and float(row["iv"]) == pytest.approx(g.iv, rel=1e-9)
    assert row["delta"] is not None


def test_greeks_none_before_forward_available(resolver):
    m = OptionChainManager(_settings(), resolver)
    ts = datetime(2025, 1, 23, 4, 0, tzinfo=UTC)
    m.on_index_tick(Tick(instrument_token="IDX", exchange_segment=ExchangeSegment.NSE_CM,
                         ltp=Decimal("23000"), timestamp=ts, is_index=True))
    # Only a CE leg — no PE at ATM, so no parity forward, so no greeks.
    m.on_option_tick(Tick(instrument_token="23100CE", exchange_segment=ExchangeSegment.NSE_FO,
                          ltp=Decimal("70"), timestamp=ts, oi=1000))
    assert m.greeks_for("23100CE") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_option_chain_greeks.py -q`
Expected: FAIL — `AttributeError: 'OptionChainManager' object has no attribute 'greeks_for'`.

- [ ] **Step 3: Add the greeks field to ChainQuote**

In `src/algo_trading/feed/option_chain.py`, extend the `ChainQuote` dataclass and add the import:

```python
from algo_trading.analytics.greeks import Greeks, compute_greeks, implied_forward, year_fraction
```

```python
@dataclass
class ChainQuote:
    instrument: Instrument
    oi: int | None = None
    ltp: Decimal = Decimal(0)
    volume: int | None = None
    greeks: Greeks | None = None
```

- [ ] **Step 4: Compute greeks in on_option_tick and store them**

In `option_chain.py`, replace the body of `on_option_tick` up to and including the `self._quotes[...] = ChainQuote(...)` assignment, and extend the writer dict. The full method becomes:

```python
    def on_option_tick(self, tick: Tick) -> None:
        inst = self._chain.get(tick.instrument_token)
        if inst is None:
            return  # not a chain contract we're tracking
        greeks = self._greeks_for_tick(inst, tick)
        self._quotes[tick.instrument_token] = ChainQuote(
            instrument=inst, oi=tick.oi, ltp=tick.ltp, volume=tick.volume, greeks=greeks
        )
        # volume-weighted VWAP by per-tick volume delta (fallback: equal weight)
        weight: Decimal | None = None
        if tick.volume is not None:
            prev = self._last_volume.get(tick.instrument_token)
            self._last_volume[tick.instrument_token] = tick.volume
            if prev is not None:
                weight = Decimal(max(0, tick.volume - prev))
        vwap = self._vwap.setdefault(tick.instrument_token, TickVWAP())
        vwap.update(tick.ltp, weight)

        if self._writer is not None:
            self._writer.add(
                {
                    "underlying": inst.underlying.value, "strike": str(inst.strike),
                    "option_type": inst.option_type.value, "instrument_token": tick.instrument_token,
                    "oi": tick.oi, "ltp": str(tick.ltp), "volume": tick.volume,
                    "vwap": str(vwap.value) if vwap.value is not None else None,
                    "iv": str(greeks.iv) if greeks else None,
                    "delta": str(greeks.delta) if greeks else None,
                    "gamma": str(greeks.gamma) if greeks else None,
                    "theta": str(greeks.theta) if greeks else None,
                    "vega": str(greeks.vega) if greeks else None,
                    "timestamp": tick.timestamp,
                }
            )
```

- [ ] **Step 5: Add the forward + greeks helpers and the accessor**

In `option_chain.py`, add these methods to `OptionChainManager` (place `_greeks_for_tick` and `_atm_forward` near the other private helpers, and `greeks_for` in the Accessors block next to `vwap_for`):

```python
    def _atm_forward(self, r: float, T: float) -> float | None:
        """Parity forward from the ATM CE/PE quotes currently held (None until both have ticked)."""
        if self._atm is None or T <= 0:
            return None
        ce = pe = None
        for q in self._quotes.values():
            if q.instrument.strike == self._atm and q.ltp > 0:
                if q.instrument.option_type is OptionType.CE:
                    ce = float(q.ltp)
                else:
                    pe = float(q.ltp)
        if ce is None or pe is None:
            return None
        return implied_forward(ce, pe, float(self._atm), r, T)

    def _greeks_for_tick(self, inst: Instrument, tick: Tick) -> Greeks | None:
        """Greeks for the just-ticked option; None whenever any input is unavailable."""
        try:
            if tick.ltp is None or tick.ltp <= 0:
                return None
            r = float(self._settings.risk_free_rate)
            T = year_fraction(tick.timestamp, inst.expiry)
            F = self._atm_forward(r, T)
            if F is None:
                return None
            return compute_greeks(float(tick.ltp), F, float(inst.strike), r, T, inst.option_type)
        except Exception:  # noqa: BLE001 - greeks must never break the feed loop
            log.warning("greeks_compute_failed", token=tick.instrument_token)
            return None

    def greeks_for(self, instrument_token: str) -> Greeks | None:
        q = self._quotes.get(instrument_token)
        return q.greeks if q else None
```

- [ ] **Step 6: Run test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_option_chain_greeks.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Run the existing chain suite (no regressions)**

Run: `source .venv/bin/activate && python -m pytest tests/test_option_chain.py tests/test_oi_selling.py -q`
Expected: PASS (existing tests unaffected — the writer dict gained keys but old assertions don't check them).

- [ ] **Step 8: Lint & type-check + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/feed/option_chain.py tests/test_option_chain_greeks.py && mypy src/algo_trading/feed/option_chain.py
git add src/algo_trading/feed/option_chain.py tests/test_option_chain_greeks.py
git commit -m "feat(feed): compute per-strike greeks in OptionChainManager + greeks_for accessor"
```

---

### Task 4: Persistence — snapshot columns + write path

**Files:**
- Modify: `src/algo_trading/persistence/db.py:188+` (`OptionChainSnapshotRow` fields)
- Modify: `src/algo_trading/persistence/bootstrap.py` (`_ensure_chain_columns`)
- Modify: `src/algo_trading/persistence/repositories.py` (`write_chain_snapshots`)
- Test: `tests/test_timescale_schema.py` (column self-add), `tests/test_option_chain_persistence.py` (round-trip)

**Interfaces:**
- Consumes: writer dict keys `iv, delta, gamma, theta, vega` (Task 3).
- Produces: `OptionChainSnapshotRow.iv/delta/gamma/theta/vega: str | None`; these columns self-add on the live hypertable.

- [ ] **Step 1: Write the failing column-self-add test**

Add to `tests/test_timescale_schema.py` (mirror the existing `test_vwap_column_added_idempotently`):

```python
def test_greeks_columns_added_idempotently(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            present = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'option_chain_snapshots'"
                )).all()
            }
        assert {"iv", "delta", "gamma", "theta", "vega"} <= present
        bootstrap_schema(engine, settings=SchemaTuning())  # second run must be a no-op
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && make db-up && python -m pytest tests/test_timescale_schema.py::test_greeks_columns_added_idempotently -q`
Expected: FAIL — the greeks columns are not present.

- [ ] **Step 3: Add the columns to the model**

In `src/algo_trading/persistence/db.py`, in `OptionChainSnapshotRow`, add after the existing `vwap` field (the string columns near `ltp`/`volume`/`vwap`):

```python
    iv: str | None = None
    delta: str | None = None
    gamma: str | None = None
    theta: str | None = None
    vega: str | None = None
```

- [ ] **Step 4: Self-add the columns for the live hypertable**

In `src/algo_trading/persistence/bootstrap.py`, extend `_ensure_chain_columns` to add all five:

```python
def _ensure_chain_columns(conn: Connection) -> None:
    """Add columns introduced after the hypertable already existed. create_all() only creates
    tables, never alters them, so new columns on option_chain_snapshots are added here."""
    for column in ("vwap", "iv", "delta", "gamma", "theta", "vega"):
        conn.execute(
            text(f"ALTER TABLE {CHAIN_TABLE} ADD COLUMN IF NOT EXISTS {column} varchar")
        )
```

- [ ] **Step 5: Run the column test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_timescale_schema.py::test_greeks_columns_added_idempotently -q`
Expected: PASS.

- [ ] **Step 6: Write the failing round-trip test**

Add to `tests/test_option_chain_persistence.py` (use the module's existing `repo` fixture and imports):

```python
def test_write_chain_snapshots_persists_greeks(repo):
    from datetime import datetime
    ts = datetime(2025, 1, 15, 10, 0)
    repo.write_chain_snapshots([{
        "underlying": "NIFTY", "strike": "23000", "option_type": "CE",
        "instrument_token": "GK1", "oi": 100, "ltp": "120", "volume": 5, "vwap": "119",
        "iv": "0.185", "delta": "0.52", "gamma": "0.0031", "theta": "-6.2", "vega": "8.1",
        "timestamp": ts,
    }])
    rows = repo.latest_chain_state()
    row = next(r for r in rows if r.instrument_token == "GK1")
    assert row.iv == "0.185"
    assert row.delta == "0.52"
    assert row.vega == "8.1"
```

- [ ] **Step 7: Run it to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_option_chain_persistence.py::test_write_chain_snapshots_persists_greeks -q`
Expected: FAIL — `TypeError`/`AttributeError` (write path ignores the greeks keys; row has no value).

- [ ] **Step 8: Pass the greeks through the write path**

In `src/algo_trading/persistence/repositories.py`, in `write_chain_snapshots`, add the five fields to the `OptionChainSnapshotRow(...)` construction after `vwap=r.get("vwap")`:

```python
                        vwap=r.get("vwap"),
                        iv=r.get("iv"),
                        delta=r.get("delta"),
                        gamma=r.get("gamma"),
                        theta=r.get("theta"),
                        vega=r.get("vega"),
```

- [ ] **Step 9: Run the round-trip test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_option_chain_persistence.py::test_write_chain_snapshots_persists_greeks -q`
Expected: PASS.

- [ ] **Step 10: Lint & type-check + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/persistence tests/test_timescale_schema.py tests/test_option_chain_persistence.py && mypy src/algo_trading/persistence/db.py src/algo_trading/persistence/bootstrap.py src/algo_trading/persistence/repositories.py
git add src/algo_trading/persistence tests/test_timescale_schema.py tests/test_option_chain_persistence.py
git commit -m "feat(persistence): persist per-strike greeks columns (self-adding)"
```

---

### Task 5: Reporting — pivot greeks into the chain view

**Files:**
- Modify: `src/algo_trading/reporting.py` (`ChainStrike`, `summarize_chain`)
- Test: `tests/test_reporting_chain.py` (create if absent, else add)

**Interfaces:**
- Consumes: `Greeks` (Task 1); snapshot rows with `.iv/.delta/.gamma/.theta/.vega` (Task 4).
- Produces: `ChainStrike.ce_greeks: Greeks | None`, `ChainStrike.pe_greeks: Greeks | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reporting_chain.py` (if the repo already has a reporting-chain test module, add these two tests there instead):

```python
"""summarize_chain pivots persisted greeks onto the per-strike view."""

from __future__ import annotations

from types import SimpleNamespace

from algo_trading.reporting import summarize_chain


def _row(strike, ot, token, **greeks):
    base = dict(strike=strike, option_type=ot, instrument_token=token, oi=100, ltp="120",
                vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)
    base.update(greeks)
    return SimpleNamespace(**base)


def test_summarize_chain_attaches_greeks():
    rows = [
        _row("23000", "CE", "C1", iv="0.19", delta="0.52", gamma="0.003", theta="-6.1", vega="8.0"),
        _row("23000", "PE", "P1", iv="0.21", delta="-0.48", gamma="0.003", theta="-5.9", vega="7.8"),
    ]
    summary = summarize_chain(rows)
    strike = next(s for s in summary.per_strike if s.strike == __import__("decimal").Decimal("23000"))
    assert strike.ce_greeks is not None and abs(strike.ce_greeks.iv - 0.19) < 1e-9
    assert strike.ce_greeks.delta == 0.52
    assert strike.pe_greeks is not None and strike.pe_greeks.delta == -0.48


def test_summarize_chain_greeks_none_when_absent():
    rows = [_row("23000", "CE", "C1"), _row("23000", "PE", "P1")]
    summary = summarize_chain(rows)
    strike = summary.per_strike[0]
    assert strike.ce_greeks is None and strike.pe_greeks is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_reporting_chain.py -q`
Expected: FAIL — `AttributeError: 'ChainStrike' object has no attribute 'ce_greeks'`.

- [ ] **Step 3: Add greeks fields to ChainStrike + a row parser**

In `src/algo_trading/reporting.py`, add the import:

```python
from algo_trading.analytics.greeks import Greeks
```

Add fields to the `ChainStrike` dataclass (after `ce_vwap`/`pe_vwap`):

```python
    ce_greeks: Greeks | None = None
    pe_greeks: Greeks | None = None
```

Add a module-level helper near the other chain helpers:

```python
def _greeks_from_row(r) -> Greeks | None:
    """Build Greeks from a snapshot row's string columns; None when IV is absent/unparseable."""
    iv = getattr(r, "iv", None)
    if iv is None:
        return None
    try:
        return Greeks(iv=float(iv), delta=float(r.delta), gamma=float(r.gamma),
                      theta=float(r.theta), vega=float(r.vega))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Carry greeks through the pivot**

In `summarize_chain`, extend the per-strike value tuple to carry greeks. Change the tuple type comment and the `by_strike` write:

```python
    # value tuple: (oi, ltp, chg_oi, token, vwap, greeks)
    by_strike: dict[Decimal, dict[str, tuple[int, Decimal, int, str, Decimal | None, Greeks | None]]] = {}
```

In the row loop, replace the `by_strike.setdefault(...)` line with:

```python
        by_strike.setdefault(strike, {})[str(r.option_type).upper()] = (
            oi, ltp, chg, token, vwap, _greeks_from_row(r)
        )
```

In the per-strike build loop, update the defaults and the `ChainStrike(...)` construction:

```python
        ce = by_strike[strike].get("CE", (0, Decimal(0), 0, "", None, None))
        pe = by_strike[strike].get("PE", (0, Decimal(0), 0, "", None, None))
        ce_total += ce[0]
        pe_total += pe[0]
        per_strike.append(ChainStrike(
            strike=strike, ce_oi=ce[0], ce_ltp=ce[1], ce_chg_oi=ce[2],
            pe_oi=pe[0], pe_ltp=pe[1], pe_chg_oi=pe[2],
            ce_oi_trends=_trends_for_token(ce[0], ce[3], anchors, windows, flat_threshold),
            pe_oi_trends=_trends_for_token(pe[0], pe[3], anchors, windows, flat_threshold),
            ce_vwap=ce[4], pe_vwap=pe[4],
            ce_greeks=ce[5], pe_greeks=pe[5],
        ))
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_reporting_chain.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the existing reporting suite (no regressions)**

Run: `source .venv/bin/activate && python -m pytest tests/ -k reporting -q`
Expected: PASS.

- [ ] **Step 7: Lint & type-check + commit**

```bash
source .venv/bin/activate && ruff check src/algo_trading/reporting.py tests/test_reporting_chain.py && mypy src/algo_trading/reporting.py
git add src/algo_trading/reporting.py tests/test_reporting_chain.py
git commit -m "feat(reporting): pivot persisted greeks onto the per-strike chain view"
```

---

### Task 6: API — GreeksOut on the chain endpoint + stream

**Files:**
- Modify: `apps/api/app/schemas.py` (`GreeksOut`, `ChainStrikeOut`, `chain_out`)
- Test: `apps/api/tests/test_chain_api.py` (create if absent, else add)

**Interfaces:**
- Consumes: `ChainStrike.ce_greeks/pe_greeks` (Task 5).
- Produces: `GreeksOut(iv, delta, gamma, theta, vega: float)`; `ChainStrikeOut.ce_greeks/pe_greeks: GreeksOut | None`. `/api/chain` and `/api/stream` payloads carry them (both go through `chain_out`).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_chain_api.py` (or add to the nearest chain-schema test):

```python
"""chain_out maps per-strike greeks into GreeksOut."""

from __future__ import annotations

from types import SimpleNamespace

from app.schemas import chain_out


def _row(strike, ot, token, **g):
    base = dict(strike=strike, option_type=ot, instrument_token=token, oi=100, ltp="120",
                vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)
    base.update(g)
    return SimpleNamespace(**base)


def test_chain_out_includes_greeks():
    rows = [
        _row("23000", "CE", "C1", iv="0.19", delta="0.52", gamma="0.003", theta="-6.1", vega="8.0"),
        _row("23000", "PE", "P1", iv="0.21", delta="-0.48", gamma="0.003", theta="-5.9", vega="7.8"),
    ]
    out = chain_out(rows)
    strike = out.per_strike[0]
    assert strike.ce_greeks is not None
    assert strike.ce_greeks.iv == 0.19
    assert strike.ce_greeks.delta == 0.52
    assert strike.pe_greeks.delta == -0.48


def test_chain_out_greeks_null_when_absent():
    rows = [_row("23000", "CE", "C1"), _row("23000", "PE", "P1")]
    out = chain_out(rows)
    assert out.per_strike[0].ce_greeks is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `source .venv/bin/activate && python -m pytest apps/api/tests/test_chain_api.py -q`
Expected: FAIL — `AttributeError: 'ChainStrikeOut' object has no attribute 'ce_greeks'`.

- [ ] **Step 3: Add the GreeksOut schema and fields**

In `apps/api/app/schemas.py`, add above `ChainStrikeOut`:

```python
class GreeksOut(BaseModel):
    """Per-strike-side option greeks. Null when IV could not be solved."""

    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
```

Add to `ChainStrikeOut` (after `ce_vwap`/`pe_vwap`):

```python
    ce_greeks: GreeksOut | None = None
    pe_greeks: GreeksOut | None = None
```

- [ ] **Step 4: Map greeks in chain_out**

In `apps/api/app/schemas.py`, add a mapper near `_trends_out`:

```python
def _greeks_out(g) -> GreeksOut | None:
    if g is None:
        return None
    return GreeksOut(iv=g.iv, delta=g.delta, gamma=g.gamma, theta=g.theta, vega=g.vega)
```

In `chain_out`, add to the `ChainStrikeOut(...)` construction (alongside `ce_vwap=...`):

```python
            ce_greeks=_greeks_out(x.ce_greeks), pe_greeks=_greeks_out(x.pe_greeks),
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest apps/api/tests/test_chain_api.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the API suite (stream path reuses chain_out)**

Run: `source .venv/bin/activate && python -m pytest apps/api/tests -q`
Expected: PASS — `/api/stream` (via `build_stream_payload` → `_chain_out_with_trends` → `chain_out`) now carries greeks with no separate change.

- [ ] **Step 7: Lint & type-check + commit**

```bash
source .venv/bin/activate && ruff check apps/api/app/schemas.py apps/api/tests/test_chain_api.py && mypy apps/api/app/schemas.py
git add apps/api/app/schemas.py apps/api/tests/test_chain_api.py
git commit -m "feat(api): expose per-strike greeks on /api/chain and /api/stream"
```

---

### Task 7: Web — render IV/greeks on the chain table

**Files:**
- Modify: `apps/web/lib/api.ts` (`Greeks` type, `ChainStrike` fields)
- Modify: `apps/web/app/dashboard/page.tsx` (`OptionChainTable` header + cells)

**Interfaces:**
- Consumes: API `ce_greeks`/`pe_greeks` shape (Task 6).

- [ ] **Step 1: Add the TypeScript types**

In `apps/web/lib/api.ts`, add a `Greeks` type above `ChainStrike` and extend `ChainStrike`:

```typescript
// Per-strike-side option greeks; null when IV could not be solved.
export type Greeks = { iv: number; delta: number; gamma: number; theta: number; vega: number };
export type ChainStrike = {
  strike: number; ce_oi: number; ce_ltp: number; ce_chg_oi: number;
  pe_oi: number; pe_ltp: number; pe_chg_oi: number; is_atm: boolean;
  ce_oi_trends?: OiTrends; pe_oi_trends?: OiTrends;
  ce_vwap?: number | null; pe_vwap?: number | null;
  ce_greeks?: Greeks | null; pe_greeks?: Greeks | null;
};
```

- [ ] **Step 2: Add an IV/greeks cell renderer**

In `apps/web/app/dashboard/page.tsx`, add this helper near the other cell helpers (e.g. next to `vwapCell`):

```tsx
function greeksCell(g?: Greeks | null) {
  if (!g) return <span className="text-neutral-600">—</span>;
  const title = `IV ${(g.iv * 100).toFixed(1)}%  Δ ${g.delta.toFixed(2)}  Γ ${g.gamma.toFixed(4)}  θ ${g.theta.toFixed(2)}  ν ${g.vega.toFixed(2)}`;
  return (
    <span title={title} className="text-neutral-300">
      {(g.iv * 100).toFixed(1)}% <span className="text-neutral-500">Δ{g.delta.toFixed(2)}</span>
    </span>
  );
}
```

Import the `Greeks` type at the top of `page.tsx` (add to the existing `import { ... } from "@/lib/api"` line): add `Greeks` to the imported names.

- [ ] **Step 3: Add the header columns and update colSpans**

In `OptionChainTable` header (around lines 355–370), bump each side's `colSpan` from `5` to `6`:

```tsx
            <th className="px-3 py-2" colSpan={6}>Calls (CE)</th>
            <th className="px-3 py-2 text-center">Strike</th>
            <th className="px-3 py-2 text-left" colSpan={6}>Puts (PE)</th>
```

Add an `IV/Δ` header on each side. In the CE sub-header row add it after the `VWAP` `<th>` (so CE reads OI Trend, OI, Chg OI, LTP, VWAP, IV/Δ), and in the PE sub-header add it before the PE `VWAP` `<th>` (mirroring the reversed PE column order):

```tsx
            <th className="px-3 py-1">IV/Δ</th>        {/* CE side: after VWAP */}
```
```tsx
            <th className="px-3 py-1 text-left">IV/Δ</th>   {/* PE side: before VWAP */}
```

- [ ] **Step 4: Add the body cells**

In the strike row body (around lines 379–384), add the CE greeks cell after the CE VWAP cell:

```tsx
              <td className="px-3 py-1.5">{greeksCell(r.ce_greeks)}</td>
```

and the PE greeks cell before the PE VWAP cell (matching the PE column order on the right side):

```tsx
              <td className="px-3 py-1.5 text-left">{greeksCell(r.pe_greeks)}</td>
```

- [ ] **Step 5: Type-check and build**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: `tsc` clean; Next.js build succeeds.

- [ ] **Step 6: Lint**

Run: `cd apps/web && npm run lint`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add apps/web/lib/api.ts apps/web/app/dashboard/page.tsx
git commit -m "feat(web): show per-strike IV/greeks on the option chain table"
```

---

## Final verification

- [ ] **Full Python suite + lint + types**

Run: `source .venv/bin/activate && make db-up && python -m pytest -q && ruff check . && mypy src`
Expected: all green.

- [ ] **Web build**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: clean.

- [ ] **End-to-end smoke (optional, live)**

With the stack running, load the dashboard chain and confirm the IV/Δ column populates for the ATM band and shows `—` where a leg hasn't priced. On the server, confirm the columns exist:
`ssh kotakserver "docker exec -i trading-algo-db-1 psql -U algo -d algo -c \"\\d option_chain_snapshots\"" | grep -E 'iv|delta|gamma|theta|vega'`

## Deployment note

Deploy is a normal image rebuild (`py_vollib` is in `[project.dependencies]`, installed by the existing `pip install ".[postgres]"` step — no Dockerfile change). The five snapshot columns self-add on first boot via `_ensure_chain_columns`; `ADD COLUMN` is instant, so **no market-closed window is required**.
