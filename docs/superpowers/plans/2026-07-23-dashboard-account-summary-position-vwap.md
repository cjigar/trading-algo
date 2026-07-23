# Persistent account summary + position VWAP in the P&L table — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a VWAP column (from the persisted per-option VWAP) to the broker M2M positions table, and pin a compact account-summary strip (M2M/Realized/Unrealized/Algo state/Open/Total) below the ticker so it's visible on every tab.

**Architecture:** Part A threads a per-position VWAP through the existing broker-P&L pipeline (`repo.latest_vwap_for` → `bridge.chain_vwaps_for` → `summarize_broker_positions(vwaps)` → `BrokerPositionPnLOut.vwap` → `/broker-pnl` + SSE → web column), mirroring how LTP already flows via `live_quotes`. Part B is frontend-only: a new `AccountSummary` component fed by the SSE `broker_pnl` + `state`, pinned in a shared sticky header with the existing `SpotTicker`.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy on PostgreSQL/TimescaleDB, FastAPI, pytest; Next.js + React + TypeScript (Tailwind).

## Global Constraints

- PostgreSQL/TimescaleDB is the only backend; tests run against it (`make db-up` first). No SQLite/mocks.
- Prices are serialized `Decimal` strings; VWAP is `Decimal | None` (reporting) / `float | None` (API) / `number | null` (web). **`None`/`null` (unknown) must stay distinct from `0` at every layer** — a position with no VWAP renders `—`, never `0.00`.
- VWAP is a ₹ price → `.toFixed(2)`, never the `fmtOi` Lk/Cr formatter.
- Position VWAP source: the newest non-NULL `option_chain_snapshots.vwap` for the position's instrument token (positions outside the captured chain window get `None`).
- ruff + mypy clean (`.venv/bin/ruff check src tests apps`, `.venv/bin/mypy`); web `cd apps/web && npx tsc --noEmit` clean.
- Run Python via the venv: `.venv/bin/python -m pytest ...`.

---

### Task 1: `latest_vwap_for` repository read

**Files:**
- Modify: `src/algo_trading/persistence/repositories.py` (new method near `live_quotes` / `latest_chain_state`)
- Test: `tests/test_persistence.py`

**Interfaces:**
- Produces: `Repository.latest_vwap_for(tokens: list[str]) -> dict[str, Decimal]` — newest non-NULL `vwap` per requested token for today; `{}` for empty input; tokens with no VWAP row omitted.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_persistence.py` (this file's `_snap` helper lives in `tests/test_option_chain_persistence.py`, so build the dicts inline here). The test writes two snapshots for one token (older then newer vwap) plus a token with only a NULL vwap, and asserts the newest non-null wins and the null-only token is absent:

```python
def test_latest_vwap_for_returns_newest_nonnull_per_token(repo: Repository):
    base = {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "volume": 10}
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T1", "oi": 1000, "ltp": "100", "vwap": "98.0",
         "timestamp": datetime(2025, 1, 15, 10, 0)},
    ])
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T1", "oi": 1000, "ltp": "101", "vwap": "99.5",
         "timestamp": datetime(2025, 1, 15, 10, 5)},  # newer
    ])
    repo.write_chain_snapshots([
        {**base, "instrument_token": "T2", "oi": 1000, "ltp": "50",  # no vwap key -> NULL
         "timestamp": datetime(2025, 1, 15, 10, 0)},
    ])
    got = repo.latest_vwap_for(["T1", "T2", "T3"])
    assert got == {"T1": Decimal("99.5")}  # T1 newest non-null; T2 null-only omitted; T3 absent


def test_latest_vwap_for_empty_tokens_is_empty(repo: Repository):
    assert repo.latest_vwap_for([]) == {}
```

`datetime` and `Decimal` are already imported at the top of `tests/test_persistence.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_persistence.py::test_latest_vwap_for_returns_newest_nonnull_per_token -v`
Expected: FAIL — `AttributeError: 'Repository' object has no attribute 'latest_vwap_for'`.

- [ ] **Step 3: Implement the method**

In `src/algo_trading/persistence/repositories.py`, add this method right after `latest_chain_state` (it reuses `_today_str`, `col`, `select`, `Session`, `OptionChainSnapshotRow`, `_safe_decimal` — all already imported/defined in the file):

```python
    def latest_vwap_for(
        self, tokens: list[str], trading_day: date | None = None
    ) -> dict[str, Decimal]:
        """Newest non-NULL session VWAP per requested instrument token for the day.

        The dashboard marks each open broker position's VWAP the same way it marks LTP from
        ``live_quotes`` — but VWAP lives on ``option_chain_snapshots`` (persisted per option tick).
        A token with no VWAP row (outside the captured chain window) is omitted, so the caller
        renders it as unknown rather than zero.
        """
        toks = [str(t) for t in tokens if t]
        if not toks:
            return {}
        day = _today_str(trading_day)
        stmt = (
            select(OptionChainSnapshotRow)
            .where(OptionChainSnapshotRow.trading_day == day)
            .where(col(OptionChainSnapshotRow.instrument_token).in_(toks))
            .where(col(OptionChainSnapshotRow.vwap).is_not(None))
            .distinct(col(OptionChainSnapshotRow.instrument_token))
            .order_by(
                col(OptionChainSnapshotRow.instrument_token),
                col(OptionChainSnapshotRow.timestamp).desc(),
                col(OptionChainSnapshotRow.id).desc(),
            )
        )
        with Session(self._engine) as session:
            return {
                r.instrument_token: _safe_decimal(r.vwap) for r in session.exec(stmt)
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_persistence.py -k latest_vwap -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/persistence/repositories.py tests/test_persistence.py
git commit -m "feat(persistence): latest_vwap_for(tokens) read for position VWAP"
```

---

### Task 2: Thread VWAP through the broker-P&L summary and API

**Files:**
- Modify: `src/algo_trading/dashboard/state_bridge.py` (new `chain_vwaps_for`)
- Modify: `src/algo_trading/reporting.py` (`BrokerPositionPnL`, `summarize_broker_positions`)
- Modify: `apps/api/app/schemas.py` (`BrokerPositionPnLOut`, `broker_pnl_out`)
- Modify: `apps/api/app/routes.py` (`/broker-pnl` handler + `build_stream_payload`)
- Test: `tests/test_reporting.py`, `apps/api/tests/test_api.py`

**Interfaces:**
- Consumes: `Repository.latest_vwap_for(tokens)` (Task 1).
- Produces: `StateBridge.chain_vwaps_for(tokens) -> dict[str, Decimal]`; `BrokerPositionPnL.vwap: Decimal | None`; `summarize_broker_positions(rows, quotes=None, vwaps=None)`; `BrokerPositionPnLOut.vwap: float | None`; `broker_pnl_out(rows, quotes=None, vwaps=None)`; `/broker-pnl` + SSE `broker_pnl.per_position[].vwap`.

- [ ] **Step 1: Write the failing test (reporting)**

Add to `tests/test_reporting.py`. `summarize_broker_positions` reads raw Kotak dict fields (`tok`, `trdSym`, `flBuyQty`, `flSellQty`, `buyAmt`, `sellAmt`):

```python
def test_summarize_broker_positions_attaches_vwap_by_token():
    from decimal import Decimal
    from algo_trading.reporting import summarize_broker_positions

    rows = [
        {"tok": "T1", "trdSym": "NIFTY23000CE", "flBuyQty": "0", "flSellQty": "75",
         "buyAmt": "0", "sellAmt": "7500"},   # open short, has vwap
        {"tok": "T2", "trdSym": "NIFTY23100PE", "flBuyQty": "75", "flSellQty": "75",
         "buyAmt": "7000", "sellAmt": "7500"},  # squared, no vwap in map
    ]
    s = summarize_broker_positions(rows, quotes={"T1": Decimal("90")}, vwaps={"T1": Decimal("88.5")})
    by_sym = {p.symbol: p for p in s.per_position}
    assert by_sym["NIFTY23000CE"].vwap == Decimal("88.5")
    assert by_sym["NIFTY23100PE"].vwap is None  # absent from vwaps -> None, not 0
```

- [ ] **Step 2: Run it — verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reporting.py::test_summarize_broker_positions_attaches_vwap_by_token -v`
Expected: FAIL — `summarize_broker_positions() got an unexpected keyword argument 'vwaps'`.

- [ ] **Step 3: Add `vwap` to the dataclass and `vwaps` to the summary**

In `src/algo_trading/reporting.py`:

(a) In the `BrokerPositionPnL` dataclass, add after `mtm_pending`:

```python
    vwap: Decimal | None = None  # session VWAP for the position's option (None if outside chain window)
```

(b) Change the `summarize_broker_positions` signature and attach vwap. Signature:

```python
def summarize_broker_positions(
    rows: list[dict], quotes: dict[str, Decimal] | None = None,
    vwaps: dict[str, Decimal] | None = None,
) -> BrokerPnLSummary:
```

At the top of the body, next to `quotes = quotes or {}`, add:

```python
    vwaps = vwaps or {}
```

In the `BrokerPositionPnL(...)` construction inside the loop, add the `vwap` arg (the token is `r.get("tok", "")`):

```python
                ltp=used_ltp, total_pnl=total, mtm_pending=mtm_pending,
                vwap=vwaps.get(str(r.get("tok", ""))),
```

- [ ] **Step 4: Run it — verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reporting.py -k broker_positions_attaches_vwap -v`
Expected: PASS.

- [ ] **Step 5: Add the bridge read**

In `src/algo_trading/dashboard/state_bridge.py`, add right after `live_quotes_for`:

```python
    def chain_vwaps_for(self, tokens: list[str]) -> dict[str, Decimal]:
        """Latest per-option session VWAP for the given instrument tokens (for the P&L table)."""
        tokens = [t for t in tokens if t]
        if not tokens:
            return {}
        return self._repo.latest_vwap_for(tokens)
```

- [ ] **Step 6: Thread through the API schema**

In `apps/api/app/schemas.py`:

(a) In `class BrokerPositionPnLOut`, add after `mtm_pending`:

```python
    vwap: float | None = None  # session VWAP for the position's option (null if unavailable)
```

(b) Change `broker_pnl_out` to accept and pass `vwaps`, and map the field:

```python
def broker_pnl_out(rows: list[dict], quotes: dict[str, Decimal] | None = None,
                   vwaps: dict[str, Decimal] | None = None) -> BrokerPnLOut:
    s = summarize_broker_positions(rows, quotes, vwaps)
```

and in the `BrokerPositionPnLOut(...)` construction add:

```python
            mtm_pending=p.mtm_pending,
            vwap=float(p.vwap) if p.vwap is not None else None,
```

- [ ] **Step 7: Pass vwaps from both routes**

In `apps/api/app/routes.py`:

(a) `/broker-pnl` handler — after the `quotes = ...` line:

```python
    quotes = bridge.live_quotes_for([str(r.get("tok", "")) for r in rows])
    vwaps = bridge.chain_vwaps_for([str(r.get("tok", "")) for r in rows])
    return broker_pnl_out(rows, quotes, vwaps)
```

(b) `build_stream_payload` — after `broker_quotes = ...`:

```python
    broker_quotes = bridge.live_quotes_for([str(r.get("tok", "")) for r in broker_positions])
    broker_vwaps = bridge.chain_vwaps_for([str(r.get("tok", "")) for r in broker_positions])
```

and change the payload line:

```python
        "broker_pnl": broker_pnl_out(broker_positions, broker_quotes, broker_vwaps).model_dump(),
```

- [ ] **Step 8: Write the failing API test**

Add to `apps/api/tests/test_api.py`. Positions are seeded with `repo.replace_broker_positions([...])` (stored as raw dicts and returned by `bridge.broker_positions()`), LTP with `repo.upsert_live_quotes`, and VWAP with `repo.write_chain_snapshots` — all on the shared test `repo`. A position row uses raw Kotak fields `tok`, `trdSym`, `flBuyQty`, `flSellQty`, `buyAmt`, `sellAmt`.

```python
def test_broker_pnl_exposes_position_vwap(client, auth, repo):
    from decimal import Decimal
    tok = "OPT1"
    repo.replace_broker_positions([
        {"tok": tok, "trdSym": "NIFTY23000CE", "flBuyQty": "0", "flSellQty": "75",
         "buyAmt": "0", "sellAmt": "7500"},
    ])
    repo.upsert_live_quotes({tok: Decimal("90")})
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": tok,
         "oi": 1000, "ltp": "90", "volume": 10, "vwap": "88.5"},
    ])
    pos = client.get("/api/broker-pnl", headers=auth).json()["per_position"][0]
    assert pos["vwap"] == 88.5
```

If `repo.replace_broker_positions` is not the method used elsewhere in this file to seed positions, use whatever the existing broker-P&L tests use (grep the file for `broker` to find the seeding helper).

- [ ] **Step 9: Run it — verify it fails then passes**

Run: `.venv/bin/python -m pytest apps/api/tests/test_api.py::test_broker_pnl_exposes_position_vwap -p no:cacheprovider -v`
Expected: PASS (Steps 3–7 implement the whole path; this test exercises it end-to-end through the API). If you run it before Steps 6–7, it FAILs with `vwap` absent — that's the red state.

- [ ] **Step 10: Run the full touched suites + lint/types**

Run:
```bash
.venv/bin/python -m pytest tests/test_reporting.py apps/api/tests/test_api.py -p no:cacheprovider -q
.venv/bin/ruff check src tests apps
.venv/bin/mypy
```
Expected: all pass; ruff clean; mypy clean.

- [ ] **Step 11: Commit**

```bash
git add src/algo_trading/reporting.py src/algo_trading/dashboard/state_bridge.py apps/api/app/schemas.py apps/api/app/routes.py tests/test_reporting.py apps/api/tests/test_api.py
git commit -m "feat(api): per-position VWAP on broker P&L (chain-snapshot source)"
```

---

### Task 3: VWAP column in the broker positions table (web)

**Files:**
- Modify: `apps/web/lib/api.ts` (`BrokerPositionPnL` type)
- Modify: `apps/web/app/dashboard/page.tsx` (`BrokerPnLTable`)

**Interfaces:**
- Consumes: `/broker-pnl` + SSE `broker_pnl.per_position[].vwap` (Task 2).
- Produces: a `VWAP` column between "Avg sell" and "LTP" in `BrokerPnLTable`.

- [ ] **Step 1: Extend the type**

In `apps/web/lib/api.ts`, in `export type BrokerPositionPnL = { ... }`, add:

```typescript
  vwap?: number | null;
```

- [ ] **Step 2: Add the header cell**

In `apps/web/app/dashboard/page.tsx`, in `BrokerPnLTable`'s `<thead>`, insert a `VWAP` header between "Avg sell" and "LTP":

```tsx
            <th className="px-3 py-2">Avg sell</th>
            <th className="px-3 py-2">VWAP</th>
            <th className="px-3 py-2">LTP</th>
```

- [ ] **Step 3: Add the body cell**

In the same component's `<tbody>` row, insert a VWAP `<td>` between the Avg sell cell and the LTP cell:

```tsx
              <td className="px-3 py-1.5">{p.avg_sell.toFixed(2)}</td>
              <td className="px-3 py-1.5 text-neutral-400">{p.vwap != null ? p.vwap.toFixed(2) : "—"}</td>
              <td className="px-3 py-1.5 text-neutral-300">{p.ltp != null ? p.ltp.toFixed(2) : "—"}</td>
```

(`!= null` catches null+undefined; `.toFixed(2)` — never `fmtOi`.)

- [ ] **Step 4: Typecheck**

Run: `cd apps/web && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add apps/web/lib/api.ts apps/web/app/dashboard/page.tsx
git commit -m "feat(web): VWAP column in the broker positions table"
```

---

### Task 4: Persistent account-summary strip (web)

**Files:**
- Modify: `apps/web/components/ui.tsx` (new `AccountSummary`)
- Modify: `apps/web/app/dashboard/page.tsx` (shared sticky header; remove duplicated cards)

**Interfaces:**
- Consumes: SSE `data.broker_pnl` (`BrokerPnL`) and `data.state.algo_state`.
- Produces: `AccountSummary({ brokerPnl, algoState })` rendered in a shared sticky header with `SpotTicker`, above `<Tabs>`.

- [ ] **Step 1: Add the `AccountSummary` component**

In `apps/web/components/ui.tsx`, add (it needs the `BrokerPnL` type — extend the existing import from `@/lib/api` at the top of the file, which currently imports `IndexSpot`):

```tsx
import type { BrokerPnL, IndexSpot } from "@/lib/api";

// Compact account-summary strip: live M2M / realized / unrealized (M2M - realized) / algo state /
// open / total positions. Fed by the SSE stream, pinned so it shows on every tab. Renders nothing
// until broker P&L arrives.
export function AccountSummary({ brokerPnl, algoState }: { brokerPnl: BrokerPnL | null; algoState?: string }) {
  if (!brokerPnl) return null;
  const unreal = brokerPnl.total_pnl - brokerPnl.total_realized;
  const money = (n: number) => `₹${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  const signed = (n: number) => (
    <span className={n >= 0 ? "text-emerald-400" : "text-red-400"}>{money(n)}</span>
  );
  const Item = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className="text-sm font-semibold tabular-nums">{children}</span>
    </div>
  );
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-neutral-800 bg-neutral-950/95 px-4 py-2 backdrop-blur">
      <Item label="Live M2M">{signed(brokerPnl.total_pnl)}</Item>
      <Item label="Realized">{signed(brokerPnl.total_realized)}</Item>
      <Item label="Unrealized">{signed(unreal)}</Item>
      <Item label="Algo">{algoState ?? "—"}</Item>
      <Item label="Open">{brokerPnl.open_count}</Item>
      <Item label="Positions">{brokerPnl.per_position.length}</Item>
    </div>
  );
}
```

`React` is available in this file (it's a client component); if `React` is not already imported, use `import type { ReactNode } from "react";` and type `children: ReactNode` instead of `React.ReactNode`. Check the top of `ui.tsx` and match its existing React import style.

- [ ] **Step 2: Make `SpotTicker` non-sticky (stickiness moves to a shared wrapper)**

In `apps/web/components/ui.tsx`, in `SpotTicker`, remove `sticky top-0 z-10` from its outer `<div>` className (keep the rest — `flex flex-wrap items-center gap-3 border-b border-neutral-800 bg-neutral-950/95 px-4 py-2 backdrop-blur`):

```tsx
    <div className="flex flex-wrap items-center gap-3 border-b border-neutral-800 bg-neutral-950/95 px-4 py-2 backdrop-blur">
```

- [ ] **Step 3: Render the shared sticky header and remove duplicated cards**

In `apps/web/app/dashboard/page.tsx`:

(a) Update the ui import to include `AccountSummary`:

```tsx
import { AccountSummary, Banner, DataTable, Metric, SpotTicker, Tabs } from "@/components/ui";
```

(b) Replace the current `<SpotTicker .../>` line with a shared sticky wrapper containing both bars:

```tsx
      <div className="sticky top-0 z-10">
        <SpotTicker spots={state?.spots ?? []} />
        <AccountSummary brokerPnl={brokerPnl} algoState={state?.algo_state} />
      </div>
```

(`brokerPnl` is already defined in the component as `data?.broker_pnl ?? null`; `state` is `data?.state`.)

(c) On the **P&L tab**, remove the four broker summary `<Metric>` cards (Live M2M / Realized / Open positions / Positions total) — the `<div className="grid grid-cols-2 gap-3 md:grid-cols-4">` block directly under the "Broker account P&amp;L (live M2M)" heading. Keep that heading, the explanatory `<p>` note, and `<BrokerPnLTable pnl={brokerPnl} />`.

(d) In the Algo-session P&L block, remove the `<Metric label="Algo state" value={state?.algo_state ?? "—"} />` card (algo state is now pinned up top). Keep Day P&L / Realized / Unrealized (open) in that grid — it becomes a 3-card row.

- [ ] **Step 4: Typecheck**

Run: `cd apps/web && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add apps/web/components/ui.tsx apps/web/app/dashboard/page.tsx
git commit -m "feat(web): pinned account-summary strip on all tabs"
```

---

### Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite + lint + types (DB up)**

```bash
make db-up
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest apps/api/tests -p no:cacheprovider -q
.venv/bin/ruff check src tests apps
.venv/bin/mypy
```
Expected: all pass; ruff "All checks passed!"; mypy "Success".

- [ ] **Step 2: Web typecheck**

Run: `cd apps/web && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 3: Manual smoke (optional, local stack)**

```bash
docker compose up -d db api web
```
Seed (through the api container) a broker position + its live quote + a chain snapshot with `vwap` for the same token, open the dashboard: confirm the VWAP column shows between Avg sell and LTP in the P&L positions table, and the account-summary strip is pinned below the ticker and visible on every tab while scrolling.

- [ ] **Step 4: Commit any tidy-ups**

```bash
git commit --allow-empty -m "chore: verify account summary + position VWAP end-to-end"
```

---

## Notes for the implementer

- **None vs 0** everywhere: a missing VWAP is `—`/`null`, never `0.00`. Keep `Decimal|None` → `float|None` → `number|null` intact; use `!= null` (not falsy checks) in TSX so a real `0` would still render.
- **VWAP is a price** → `.toFixed(2)`; never `fmtOi`.
- **Sticky stacking:** one shared `sticky top-0` wrapper around both bars; `SpotTicker` must NOT keep its own `sticky` or the two will fight.
- **Deployment (out of plan scope):** frontend + a read-only backend addition; a live deploy rebuilds `api` + `web` (and `algo` only if you want the image in lockstep). No schema/migration change — `latest_vwap_for` reads the existing `vwap` column.
