# Option-chain display window (config-driven, default ±7) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit the dashboard's option-chain view to a config-driven ATM ±N window (default 7) and compute Total CE OI, Total PE OI, and the Higher-OI side over exactly that window.

**Architecture:** All chain rendering flows through `reporting.summarize_chain` (via `apps/api` `chain_out` → `_chain_out_with_trends`, shared by `/api/chain` and the SSE stream). Add the window there: resolve ATM on the full captured set, slice `per_strike` to ATM ±N by position, and sum the totals + selected side over the slice. A new `chain_display_window` setting drives it; the web already renders whatever the API returns.

**Tech Stack:** Python (SQLModel/FastAPI/pydantic, pytest against a real TimescaleDB via `make db-up`), Next.js/TypeScript web dashboard.

## Global Constraints

- **Window semantics:** ATM ±N strikes **by position** in the sorted-strike list (step-agnostic: NIFTY-50 and SENSEX-100). N = `chain_display_window`, default **7** (7 below + ATM + 7 above = 15 strikes). `0` or `None` = keep the full captured chain.
- **Totals are windowed:** `ce_oi_total`, `pe_oi_total`, and `selected_side` are summed over the **kept slice**, not the full chain.
- **ATM first, then slice:** resolve ATM from the full `per_strike` before slicing, so ATM is never distorted by the window near a captured edge.
- **Backward compatible:** existing callers of `summarize_chain` that pass no `display_window` get the current full-chain behavior unchanged.
- **Do not touch** the OI-selling strategy, its `strike_window` aggregation, or capture (`chain_feed_window`).
- Python tests: `.venv/bin/pytest` against the local TimescaleDB (`localhost:55432`). Web: `apps/web` tooling.

## File Structure

- `src/algo_trading/reporting.py` — `ChainSummary.display_window` field; `summarize_chain(display_window=...)` slice + windowed totals.
- `src/algo_trading/config/settings.py` — `chain_display_window: int = 7`.
- `apps/api/app/schemas.py` — `ChainOut.display_window`; `chain_out(display_window=...)`.
- `apps/api/app/routes.py` — `_chain_out_with_trends` passes `settings.chain_display_window`.
- `apps/web/lib/api.ts`, `apps/web/lib/api-types.ts`, `apps/web/app/dashboard/page.tsx` — `display_window` field + windowed metric labels.
- `.env.example` — document `ALGO_CHAIN_DISPLAY_WINDOW=7`.
- Tests: `tests/test_reporting_chain.py`, an API test file for the chain route/stream.

---

### Task 1: Window the chain in `summarize_chain`

**Files:**
- Modify: `src/algo_trading/reporting.py` (`ChainSummary` ~line 126-132; `summarize_chain` ~line 147-202)
- Test: `tests/test_reporting_chain.py`

**Interfaces:**
- Produces: `ChainSummary.display_window: int`; `summarize_chain(..., display_window: int | None = None)` — resolves ATM on the full set, slices `per_strike` to ATM ±`display_window` by position, and sums `ce_oi_total`/`pe_oi_total`/`selected_side` over the slice. `display_window` None/0 → full chain (unchanged).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reporting_chain.py` (the file already imports `Decimal`, `SimpleNamespace`, `summarize_chain`, and defines `_row(strike, ot, token, **greeks)` with default `oi=100`):

```python
def _oi_row(strike, ot, token, oi):
    return SimpleNamespace(strike=strike, option_type=ot, instrument_token=token, oi=oi,
                           ltp="120", vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)


def _chain_rows(atm=23000, half=20, step=50, ce_oi=100, pe_oi=100):
    """CE+PE rows for atm ± half strikes. Equal LTP everywhere so _resolve_atm falls back to the
    middle strike == atm."""
    rows = []
    for k in range(atm - half * step, atm + half * step + step, step):
        rows.append(_oi_row(str(k), "CE", f"C{k}", ce_oi))
        rows.append(_oi_row(str(k), "PE", f"P{k}", pe_oi))
    return rows


def test_display_window_slices_to_atm_plus_minus_n():
    rows = _chain_rows(atm=23000, half=20)  # 41 strikes captured
    summary = summarize_chain(rows, display_window=7)
    strikes = [int(s.strike) for s in summary.per_strike]
    assert len(strikes) == 15  # 7 below + ATM + 7 above
    assert strikes[0] == 23000 - 7 * 50 and strikes[-1] == 23000 + 7 * 50
    assert summary.atm == Decimal("23000")
    assert any(s.is_atm and s.strike == Decimal("23000") for s in summary.per_strike)
    assert summary.display_window == 7


def test_display_window_totals_are_windowed():
    # 41 strikes, CE oi=100, PE oi=100 each -> windowed (15 strikes) totals = 15*100 each.
    rows = _chain_rows(atm=23000, half=20, ce_oi=100, pe_oi=100)
    summary = summarize_chain(rows, display_window=7)
    assert summary.ce_oi_total == 15 * 100
    assert summary.pe_oi_total == 15 * 100
    assert summary.selected_side == "—"


def test_windowed_selected_side_can_differ_from_full_chain():
    # Full chain: PE-heavy on the far wings; within ATM ±2, CE dominates. Windowed totals must
    # follow the window, not the whole chain.
    rows = []
    for k in range(23000 - 5 * 50, 23000 + 5 * 50 + 50, 50):
        near = abs(k - 23000) <= 2 * 50
        rows.append(_oi_row(str(k), "CE", f"C{k}", 500 if near else 10))
        rows.append(_oi_row(str(k), "PE", f"P{k}", 10 if near else 500))
    full = summarize_chain(rows)  # no window
    win = summarize_chain(rows, display_window=2)
    assert full.selected_side == "PE"   # wings dominate the full chain
    assert win.selected_side == "CE"    # ATM ±2 is CE-heavy
    assert win.ce_oi_total == 5 * 500 and win.pe_oi_total == 5 * 10


def test_display_window_clamps_at_edges():
    # ATM near the low edge: only 3 strikes below exist, window still returns what's available.
    rows = _chain_rows(atm=23000, half=3)  # strikes 22850..23150 (7 total)
    summary = summarize_chain(rows, display_window=7)
    assert len(summary.per_strike) == 7  # clamped: can't exceed the 7 captured strikes
    assert summary.atm == Decimal("23000")


def test_display_window_none_or_zero_returns_full_chain():
    rows = _chain_rows(atm=23000, half=10)  # 21 strikes
    assert len(summarize_chain(rows).per_strike) == 21              # default None -> full
    assert len(summarize_chain(rows, display_window=0).per_strike) == 21  # 0 -> full
    assert summarize_chain(rows).display_window == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_reporting_chain.py::test_display_window_slices_to_atm_plus_minus_n -v`
Expected: FAIL — `summarize_chain()` got an unexpected keyword argument `display_window` (and `ChainSummary` has no `display_window`).

- [ ] **Step 3a: Add the `display_window` field to `ChainSummary`**

In `src/algo_trading/reporting.py`, `ChainSummary` (~line 126-132), add the field:

```python
@dataclass(frozen=True)
class ChainSummary:
    per_strike: list[ChainStrike]
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str  # "CE" | "PE" | "—"
    atm: Decimal | None = None
    display_window: int = 0  # strikes each side of ATM the view/totals were narrowed to (0 = full)
```

- [ ] **Step 3b: Window the summary in `summarize_chain`**

In `src/algo_trading/reporting.py`, change the `summarize_chain` signature (~line 147) to add the parameter:

```python
def summarize_chain(
    rows: list,
    oi_baseline: dict[str, int] | None = None,
    oi_anchors: dict[int, dict[str, int]] | None = None,
    trend_windows: list[int] | None = None,
    flat_threshold: int = 0,
    display_window: int | None = None,
) -> ChainSummary:
```

Then replace the build/total/return block (currently ~line 181-202, from `per_strike: list[ChainStrike] = []` through the `return`) with this — the per-strike build no longer totals inline; totals are computed after windowing:

```python
    per_strike: list[ChainStrike] = []
    for strike in sorted(by_strike):
        ce = by_strike[strike].get("CE", (0, Decimal(0), 0, "", None, None))
        pe = by_strike[strike].get("PE", (0, Decimal(0), 0, "", None, None))
        per_strike.append(ChainStrike(
            strike=strike, ce_oi=ce[0], ce_ltp=ce[1], ce_chg_oi=ce[2],
            pe_oi=pe[0], pe_ltp=pe[1], pe_chg_oi=pe[2],
            ce_oi_trends=_trends_for_token(ce[0], ce[3], anchors, windows, flat_threshold),
            pe_oi_trends=_trends_for_token(pe[0], pe[3], anchors, windows, flat_threshold),
            ce_vwap=ce[4], pe_vwap=pe[4],
            ce_greeks=ce[5], pe_greeks=pe[5],
        ))

    atm = _resolve_atm(per_strike)
    if atm is not None:
        per_strike = [replace(s, is_atm=(s.strike == atm)) for s in per_strike]

    # Narrow to the display window (ATM ±N by position) so the view — and the totals below —
    # focus on the strikes around ATM. A None/0 window keeps the full captured chain.
    window = display_window or 0
    if window > 0 and atm is not None:
        idx = next((i for i, s in enumerate(per_strike) if s.strike == atm), None)
        if idx is not None:
            per_strike = per_strike[max(0, idx - window): idx + window + 1]

    ce_total = sum(s.ce_oi for s in per_strike)
    pe_total = sum(s.pe_oi for s in per_strike)
    selected = "CE" if ce_total > pe_total else "PE" if pe_total > ce_total else "—"
    return ChainSummary(per_strike, ce_total, pe_total, selected, atm, display_window=window)
```

(Remove the old `ce_total = pe_total = 0` initialization and the `ce_total += ce[0]` / `pe_total += pe[0]` lines inside the loop — totals now come from the post-window `sum(...)`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_reporting_chain.py -v`
Expected: PASS (the 5 new tests + the existing greeks tests — those call `summarize_chain(rows)` with no window, so full-chain behavior is preserved).

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/reporting.py tests/test_reporting_chain.py
git commit -m "feat(reporting): ATM ±N display window in summarize_chain (windowed totals)"
```

---

### Task 2: Config + API plumbing

**Files:**
- Modify: `src/algo_trading/config/settings.py` (~line 98, near `chain_feed_window`)
- Modify: `apps/api/app/schemas.py` (`ChainOut` ~line 137-143; `chain_out` ~line 290-308)
- Modify: `apps/api/app/routes.py` (`_chain_out_with_trends` ~line 110-118)
- Modify: `.env.example`
- Test: `tests/test_api_chain_window.py` (new)

**Interfaces:**
- Consumes: `summarize_chain(display_window=...)` and `ChainSummary.display_window` (Task 1).
- Produces: `Settings.chain_display_window: int = 7`; `ChainOut.display_window: int`; `chain_out(..., display_window: int | None = None)` forwards it and echoes `cs.display_window`; `/api/chain` and the stream both apply `settings.chain_display_window`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_chain_window.py`. Model the setup on an existing API test — check `tests/` for how the app/bridge is built (there is an existing API test module; reuse its `client`/bridge fixtures). The essential assertions (adapt fixture names to the existing API test's fixtures):

```python
from algo_trading.reporting import summarize_chain
from types import SimpleNamespace


def _row(strike, ot, oi):
    return SimpleNamespace(strike=strike, option_type=ot, instrument_token=f"{strike}{ot}", oi=oi,
                           ltp="120", vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)


def test_chain_out_applies_display_window():
    from apps_api_app_schemas import chain_out  # adjust import to the real module path below
    rows = []
    for k in range(23000 - 20 * 50, 23000 + 20 * 50 + 50, 50):
        rows += [_row(str(k), "CE", 100), _row(str(k), "PE", 100)]
    out = chain_out(rows, "NIFTY", display_window=7)
    assert len(out.per_strike) == 15
    assert out.display_window == 7
    assert out.ce_oi_total == 15 * 100 and out.pe_oi_total == 15 * 100
```

Note: the real import is `from app.schemas import chain_out` (the FastAPI app package is `app` under `apps/api`); confirm how the existing API tests import app modules and match that. If the existing API tests run through a TestClient, add an endpoint-level assertion too: set `chain_display_window` on the settings the app uses and assert `GET /api/chain` returns `display_window == 7` and a `per_strike` length ≤ 15.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_api_chain_window.py -v`
Expected: FAIL — `chain_out()` got an unexpected keyword argument `display_window` (and `ChainOut` has no `display_window`).

- [ ] **Step 3a: Add the setting**

In `src/algo_trading/config/settings.py`, right after `chain_feed_window` (~line 98), add:

```python
    # Strikes each side of ATM the DASHBOARD chain view shows (and totals/higher-OI side aggregate
    # over). View-only: independent of strike_window (strategy) and chain_feed_window (capture).
    # 0 = show the full captured chain.
    chain_display_window: int = 7
```

- [ ] **Step 3b: Add `display_window` to `ChainOut` and forward it in `chain_out`**

In `apps/api/app/schemas.py`, `ChainOut` (~line 137-143), add the field:

```python
class ChainOut(BaseModel):
    underlying: str | None
    atm: float | None
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str
    display_window: int = 0
    per_strike: list[ChainStrikeOut]
```

Then in `chain_out` (~line 290-308), add the parameter and forward it, and echo `cs.display_window`:

```python
def chain_out(rows: list, underlying: str | None = None,
              oi_baseline: dict[str, int] | None = None,
              oi_anchors: dict[int, dict[str, int]] | None = None,
              trend_windows: list[int] | None = None,
              flat_threshold: int = 0,
              display_window: int | None = None) -> ChainOut:
    cs = summarize_chain(
        rows, oi_baseline, oi_anchors=oi_anchors,
        trend_windows=trend_windows, flat_threshold=flat_threshold,
        display_window=display_window,
    )
    return ChainOut(
        underlying=underlying, atm=float(cs.atm) if cs.atm is not None else None,
        ce_oi_total=cs.ce_oi_total, pe_oi_total=cs.pe_oi_total, selected_side=cs.selected_side,
        display_window=cs.display_window,
        per_strike=[ChainStrikeOut(
            strike=float(x.strike), ce_oi=x.ce_oi, ce_ltp=float(x.ce_ltp), ce_chg_oi=x.ce_chg_oi,
            pe_oi=x.pe_oi, pe_ltp=float(x.pe_ltp), pe_chg_oi=x.pe_chg_oi, is_atm=x.is_atm,
            ce_oi_trends=_trends_out(x.ce_oi_trends), pe_oi_trends=_trends_out(x.pe_oi_trends),
            ce_vwap=float(x.ce_vwap) if x.ce_vwap is not None else None,
            pe_vwap=float(x.pe_vwap) if x.pe_vwap is not None else None,
            ce_greeks=_greeks_out(x.ce_greeks), pe_greeks=_greeks_out(x.pe_greeks),
        ) for x in cs.per_strike])
```

- [ ] **Step 3c: Pass the setting from the route/stream**

In `apps/api/app/routes.py`, `_chain_out_with_trends` (~line 110-118), forward the setting so both `/api/chain` and the stream apply it:

```python
def _chain_out_with_trends(bridge: StateBridge, settings: Settings, underlying: str | None) -> ChainOut:
    """Build the chain response including rolling OI-trend windows from settings. Shared by the
    /chain endpoint and the SSE stream so both carry identical trend fields."""
    windows = settings.oi_trend_windows
    return chain_out(
        bridge.chain(underlying), underlying, bridge.chain_oi_baseline(underlying),
        oi_anchors=bridge.chain_oi_anchors(windows, underlying),
        trend_windows=windows, flat_threshold=settings.oi_trend_flat_threshold,
        display_window=settings.chain_display_window,
    )
```

- [ ] **Step 3d: Document the env var**

In `.env.example`, next to `ALGO_CHAIN_FEED_WINDOW`, add:

```bash
# Strikes each side of ATM the DASHBOARD option-chain view shows (and Total CE/PE OI + higher-OI
# side aggregate over). View-only: does not affect capture or the strategy. 0 = full captured chain.
ALGO_CHAIN_DISPLAY_WINDOW=7
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_chain_window.py -v && .venv/bin/pytest tests/test_reporting_chain.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/config/settings.py apps/api/app/schemas.py apps/api/app/routes.py .env.example tests/test_api_chain_window.py
git commit -m "feat(api): chain_display_window setting drives /chain + stream view window"
```

---

### Task 3: Web — label the windowed metrics

**Files:**
- Modify: `apps/web/lib/api.ts` (`Chain` type ~line 136-138)
- Modify: `apps/web/lib/api-types.ts` (`ChainOut` ~line 200-208)
- Modify: `apps/web/app/dashboard/page.tsx` (chain metrics ~line 167-172)

**Interfaces:**
- Consumes: `ChainOut.display_window` (Task 2).
- Produces: metric labels that show the active window, e.g. `Total CE OI (ATM ±7)`.

- [ ] **Step 1: Add `display_window` to the web types**

In `apps/web/lib/api.ts`, `Chain` type (~line 136-138), add the field:

```typescript
export type Chain = {
  underlying: string | null; atm: number | null; ce_oi_total: number; pe_oi_total: number;
  selected_side: string; display_window: number; per_strike: ChainStrike[];
};
```

In `apps/web/lib/api-types.ts`, the `ChainOut` object (~line 200-208), add `display_window` alongside `selected_side`:

```typescript
        ChainOut: {
            // ... existing fields ...
            selected_side: string;
            /** Display Window */
            display_window: number;
            per_strike: components["schemas"]["ChainStrikeOut"][];
        };
```

(If `apps/web/lib/api-types.ts` is generated by openapi-typescript, this hand-edit is a stopgap that matches the new schema; a later regen against the running API will reproduce it. Keep the field name exact: `display_window`.)

- [ ] **Step 2: Label the metrics with the window**

In `apps/web/app/dashboard/page.tsx`, the chain metrics block (~line 167-172), derive a suffix from `display_window` and apply it to the three OI metrics (leave `ATM strike` as-is):

```tsx
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                {(() => {
                  const w = displayChain.display_window;
                  const s = w > 0 ? ` (ATM ±${w})` : "";
                  return (
                    <>
                      <Metric label="ATM strike" value={displayChain.atm ? displayChain.atm.toLocaleString() : "—"} />
                      <Metric label={`Total CE OI${s}`} value={fmtOi(displayChain.ce_oi_total)} />
                      <Metric label={`Total PE OI${s}`} value={fmtOi(displayChain.pe_oi_total)} />
                      <Metric label={`Higher-OI side${s}`} value={displayChain.selected_side} />
                    </>
                  );
                })()}
              </div>
```

- [ ] **Step 3: Typecheck / build the web app**

Run the web app's typecheck or build to confirm the types line up. Check `apps/web/package.json` for the script; use whichever exists (in order of preference):

```bash
cd apps/web && (npm run typecheck 2>/dev/null || npx tsc --noEmit || npm run build)
```

Expected: no type errors on the changed files (`display_window` is now on `Chain`/`ChainOut`).

- [ ] **Step 4: Commit**

```bash
git add apps/web/lib/api.ts apps/web/lib/api-types.ts apps/web/app/dashboard/page.tsx
git commit -m "feat(web): label chain OI metrics with the active display window"
```

---

## Post-implementation: deploy to the live server (manual, confirm first)

Not a code task — the live trading dashboard runs on `kotakserver` (`trading-algo-api-1`, `trading-algo-web-1`). After merge to `main`:

1. Set `ALGO_CHAIN_DISPLAY_WINDOW=7` in the live `.env` (optional — 7 is the default, so unset also works; set it only to override).
2. Rebuild/redeploy the `api` and `web` containers (the `web` image bundles the built dashboard, so it must be rebuilt for the label change). Confirm with the operator before restarting containers on the production trading host.
3. Verify: the Option Chain tab shows 15 strikes (ATM ±7) and the three metrics read "(ATM ±7)".

## Self-Review

- **Spec coverage:** config-driven window default 7 (Task 2) · window inside `summarize_chain` (Task 1) · totals + higher-OI side over the window (Task 1) · plumbed to `/chain` + stream (Task 2) · `display_window` echoed for labels (Tasks 2-3) · web labels (Task 3) · `.env.example` (Task 2) · reporting + API tests (Tasks 1-2). All spec sections map to a task.
- **Placeholder scan:** none — every code step carries the exact code. The one adapt-to-existing note (API test fixture/import path in Task 2 Step 1) names the concrete target (`from app.schemas import chain_out`, reuse the existing API test's fixtures).
- **Type consistency:** `display_window: int | None` param → `ChainSummary.display_window: int` → `ChainOut.display_window: int` → web `Chain.display_window: number`; the field name `display_window` is identical across Python, schema, and TS. `summarize_chain`/`chain_out` keep `display_window` as the last keyword arg so existing callers are unaffected.
