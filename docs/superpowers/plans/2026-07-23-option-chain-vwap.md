# Per-option VWAP in the Option Chain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each option's session VWAP for both CALL and PUT in the dashboard option chain, with LTP tinted/arrowed relative to its VWAP.

**Architecture:** The engine already computes a per-option session VWAP (`OptionChainManager._vwap`, a `TickVWAP` per token) but never persists it. Thread that value through the existing snapshot pipeline: persist it on `option_chain_snapshots`, pivot it in `summarize_chain`, expose it on `ChainStrikeOut`, and render two VWAP columns in the web chain table with LTP coloring. The dashboard is a separate process reading the DB, so VWAP must be persisted (not just held in memory).

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy on PostgreSQL/TimescaleDB, FastAPI, pytest; Next.js/React + TypeScript (Tailwind) web app.

## Global Constraints

- PostgreSQL/TimescaleDB is the only backend; tests run against it via `make db-up`. Run the DB before pytest.
- `option_chain_snapshots` is a **hypertable**; `SQLModel.metadata.create_all` does NOT add columns to an existing table — new columns need an idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `bootstrap.py`.
- Monetary values are stored as strings (serialized `Decimal`) and converted at the edges; VWAP follows this (`str | None`).
- VWAP renders as a ₹ premium price (2 decimals), NOT Lk/Cr (that formatting is for OI counts only).
- Python: `ruff check src tests apps` and `mypy` must stay clean. Web: `cd apps/web && npx tsc --noEmit` must stay clean.
- Test commands run from repo root with the venv: `.venv/bin/python -m pytest ...`.

---

### Task 1: Persist a `vwap` column on option-chain snapshots

**Files:**
- Modify: `src/algo_trading/persistence/db.py` (class `OptionChainSnapshotRow`)
- Modify: `src/algo_trading/persistence/bootstrap.py` (`bootstrap_schema`, new helper)
- Modify: `src/algo_trading/persistence/repositories.py` (`write_chain_snapshots`)
- Test: `tests/test_option_chain_persistence.py`

**Interfaces:**
- Consumes: existing `Repository.write_chain_snapshots(rows: list[dict])` where each dict may now include key `"vwap": str | None`; `Repository.latest_chain_state(...) -> list[OptionChainSnapshotRow]`.
- Produces: `OptionChainSnapshotRow.vwap: str | None`; `write_chain_snapshots` persists `r.get("vwap")`; the `vwap` column exists on the hypertable after `bootstrap_schema`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_option_chain_persistence.py`:

```python
def test_write_chain_snapshot_persists_vwap(repo: Repository):
    repo.write_chain_snapshots([
        _snap("T1", "23000", oi=1000, ltp="100", ts=datetime(2025, 1, 15, 10, 0)) | {"vwap": "98.5"},
    ])
    row = repo.latest_chain_state()[0]
    assert row.vwap == "98.5"


def test_write_chain_snapshot_vwap_defaults_none(repo: Repository):
    # A snapshot dict without a vwap key stores NULL, not a crash.
    repo.write_chain_snapshots([_snap("T1", "23000", oi=1000, ts=datetime(2025, 1, 15, 10, 0))])
    assert repo.latest_chain_state()[0].vwap is None
```

Note: `_snap(...)` already exists in this file and returns a dict; `| {"vwap": ...}` merges the key.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_option_chain_persistence.py::test_write_chain_snapshot_persists_vwap -v`
Expected: FAIL — `TypeError`/`AttributeError` (`OptionChainSnapshotRow` has no `vwap`) or the column doesn't exist.

- [ ] **Step 3: Add the model column**

In `src/algo_trading/persistence/db.py`, in `class OptionChainSnapshotRow`, add after the `ltp` / `volume` fields:

```python
    vwap: str | None = None  # session VWAP for this option (serialized Decimal); None until first tick
```

- [ ] **Step 4: Add an idempotent ADD COLUMN in bootstrap**

In `src/algo_trading/persistence/bootstrap.py`, add a helper near the other `_ensure_*` functions:

```python
def _ensure_chain_columns(conn: Connection) -> None:
    """Add columns introduced after the hypertable already existed. create_all() only creates
    tables, never alters them, so new columns on option_chain_snapshots are added here."""
    conn.execute(
        text(f"ALTER TABLE {CHAIN_TABLE} ADD COLUMN IF NOT EXISTS vwap varchar")
    )
```

Then call it inside `bootstrap_schema`, in the existing `with _autocommit(engine) as conn:` block, immediately after the `for table, time_column in HYPERTABLES.items(): ...` loop and before `_ensure_compression`:

```python
        _ensure_chain_columns(conn)
```

(`Connection` and `text` are already imported in this module; `CHAIN_TABLE` is already defined.)

- [ ] **Step 5: Persist vwap in write_chain_snapshots**

In `src/algo_trading/persistence/repositories.py`, in `write_chain_snapshots`, add `vwap=r.get("vwap")` to the `OptionChainSnapshotRow(...)` constructor (alongside `volume=r.get("volume")`):

```python
                        volume=r.get("volume"),
                        vwap=r.get("vwap"),
                        timestamp=r.get("timestamp") or datetime.utcnow(),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_option_chain_persistence.py -v`
Expected: PASS (new tests + existing ones).

- [ ] **Step 7: Verify bootstrap ADD COLUMN is idempotent**

Add to `tests/test_timescale_schema.py`:

```python
def test_vwap_column_added_idempotently(fresh_db):
    engine = create_engine_from_url(fresh_db, settings=SchemaTuning())
    try:
        with engine.connect() as conn:
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'option_chain_snapshots' AND column_name = 'vwap'"
            )).first()
            assert has is not None
        bootstrap_schema(engine, settings=SchemaTuning())  # second run must be a no-op
    finally:
        engine.dispose()
```

Run: `.venv/bin/python -m pytest tests/test_timescale_schema.py::test_vwap_column_added_idempotently -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/algo_trading/persistence/db.py src/algo_trading/persistence/bootstrap.py src/algo_trading/persistence/repositories.py tests/test_option_chain_persistence.py tests/test_timescale_schema.py
git commit -m "feat(persistence): persist per-option vwap on chain snapshots"
```

---

### Task 2: Write the computed VWAP from the chain manager

**Files:**
- Modify: `src/algo_trading/feed/option_chain.py` (`on_option_tick`)
- Test: `tests/test_option_chain.py`

**Interfaces:**
- Consumes: existing `TickVWAP` `vwap` local in `on_option_tick` (`vwap.value: Decimal | None`); `OptionChainManager.vwap_for(token) -> Decimal | None`; the injected `snapshot_writer.add(dict)`.
- Produces: the snapshot dict emitted by `on_option_tick` now includes `"vwap"` (str of the current session VWAP, or `None`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_option_chain.py`. This file already defines the `resolver` fixture and the
`_settings()`, `_idx(price)`, `_opt(token, price, oi=1000, vol=None)` helpers — reuse them. A
capturing writer only needs an `add(row)` method (that is the whole `snapshot_writer` interface
`on_option_tick` uses):

```python
def test_option_tick_snapshot_includes_vwap(resolver):
    captured = []

    class _Writer:
        def add(self, row):
            captured.append(row)

    m = OptionChainManager(_settings(), resolver, subscribe=lambda t, s: None, snapshot_writer=_Writer())
    m.on_index_tick(_idx("23062"))          # ATM -> 23050, subscribes the ±5 window
    m.on_option_tick(_opt("23050CE", "100", vol=500))  # a tracked ATM contract
    assert captured, "expected a snapshot to be written"
    assert captured[-1]["instrument_token"] == "23050CE"
    assert "vwap" in captured[-1]
    assert captured[-1]["vwap"] is not None   # a tick arrived, so VWAP is set
    assert Decimal(captured[-1]["vwap"]) == Decimal("100")  # single tick -> VWAP == LTP
```

(`OptionChainManager` accepts `snapshot_writer=`; `on_option_tick` writes only when a writer is
set. Token `"23050CE"` is a real contract in the `resolver` fixture's scrip list and falls inside
the ATM ±5 window, so it is tracked.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_option_chain.py::test_option_tick_snapshot_includes_vwap -v`
Expected: FAIL — `KeyError`/assertion: snapshot dict has no `"vwap"`.

- [ ] **Step 3: Add vwap to the emitted snapshot dict**

In `src/algo_trading/feed/option_chain.py`, in `on_option_tick`, the `vwap` object is computed just above the writer call. Add the `vwap` key to the dict passed to `self._writer.add(...)`:

```python
        if self._writer is not None:
            self._writer.add(
                {
                    "underlying": inst.underlying.value, "strike": str(inst.strike),
                    "option_type": inst.option_type.value, "instrument_token": tick.instrument_token,
                    "oi": tick.oi, "ltp": str(tick.ltp), "volume": tick.volume,
                    "vwap": str(vwap.value) if vwap.value is not None else None,
                    "timestamp": tick.timestamp,
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_option_chain.py -v`
Expected: PASS (new test + existing chain tests).

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/feed/option_chain.py tests/test_option_chain.py
git commit -m "feat(feed): emit per-option vwap in chain snapshots"
```

---

### Task 3: Surface VWAP through the chain summary and API

**Files:**
- Modify: `src/algo_trading/reporting.py` (`ChainStrike`, `summarize_chain`)
- Modify: `apps/api/app/schemas.py` (`ChainStrikeOut`, `chain_out`)
- Test: `tests/test_reporting.py`, `apps/api/tests/test_api.py`

**Interfaces:**
- Consumes: snapshot rows with `.vwap: str | None` (from Task 1); `summarize_chain(rows, ...) -> ChainSummary`.
- Produces: `ChainStrike.ce_vwap: Decimal | None`, `ChainStrike.pe_vwap: Decimal | None`; `ChainStrikeOut.ce_vwap: float | None`, `ChainStrikeOut.pe_vwap: float | None`; `/api/chain` and the SSE `chain` payload carry `ce_vwap`/`pe_vwap`.

- [ ] **Step 1: Write the failing test (reporting)**

Add to `tests/test_reporting.py`. Use a tiny row stub matching what `summarize_chain` reads (`.strike/.option_type/.oi/.ltp/.instrument_token/.vwap`):

```python
def test_summarize_chain_maps_vwap_per_side():
    from types import SimpleNamespace
    from algo_trading.reporting import summarize_chain

    rows = [
        SimpleNamespace(strike="100", option_type="CE", oi=10, ltp="5.5", instrument_token="c", vwap="5.0"),
        SimpleNamespace(strike="100", option_type="PE", oi=20, ltp="4.5", instrument_token="p", vwap=None),
    ]
    cs = summarize_chain(rows)
    row = cs.per_strike[0]
    assert row.ce_vwap == Decimal("5.0")
    assert row.pe_vwap is None
```

Ensure `from decimal import Decimal` is imported in the test file (add if missing).

- [ ] **Step 2: Run it — verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reporting.py::test_summarize_chain_maps_vwap_per_side -v`
Expected: FAIL — `ChainStrike` has no `ce_vwap`.

- [ ] **Step 3: Add fields to ChainStrike and populate in summarize_chain**

In `src/algo_trading/reporting.py`:

(a) Add to the `ChainStrike` dataclass (after `pe_chg_oi`, keeping defaults so field order stays valid):

```python
    ce_vwap: Decimal | None = None
    pe_vwap: Decimal | None = None
```

(b) In `summarize_chain`, widen the per-strike value tuple to carry vwap. Change the tuple comment and the two build/read sites:

- The build loop currently does:
  ```python
      by_strike.setdefault(strike, {})[str(r.option_type).upper()] = (oi, ltp, chg, token)
  ```
  Change to capture vwap (parse with the existing `_to_decimal`, but keep `None` distinct):
  ```python
      raw_vwap = getattr(r, "vwap", None)
      vwap = _to_decimal(raw_vwap) if raw_vwap is not None else None
      by_strike.setdefault(strike, {})[str(r.option_type).upper()] = (oi, ltp, chg, token, vwap)
  ```

- The defaults and unpacking in the strike loop currently do:
  ```python
      ce = by_strike[strike].get("CE", (0, Decimal(0), 0, ""))
      pe = by_strike[strike].get("PE", (0, Decimal(0), 0, ""))
  ```
  Change the defaults to 5-tuples with `None` vwap:
  ```python
      ce = by_strike[strike].get("CE", (0, Decimal(0), 0, "", None))
      pe = by_strike[strike].get("PE", (0, Decimal(0), 0, "", None))
  ```

- In the `per_strike.append(ChainStrike(...))` call add the vwap args:
  ```python
          ce_vwap=ce[4], pe_vwap=pe[4],
  ```

- [ ] **Step 4: Run it — verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reporting.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test (API)**

Add to `apps/api/tests/test_api.py` (uses the existing `client`, `auth`, `repo` fixtures; `_snap`-style writes go through `repo.write_chain_snapshots`):

```python
def test_chain_exposes_vwap(client, auth, repo):
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 4000, "ltp": "100", "volume": 10, "vwap": "97.5"},
        {"underlying": "NIFTY", "strike": "23000", "option_type": "PE", "instrument_token": "p1",
         "oi": 800, "ltp": "90", "volume": 10},  # no vwap -> None
    ])
    row = client.get("/api/chain", params={"underlying": "NIFTY"}, headers=auth).json()["per_strike"][0]
    assert row["ce_vwap"] == 97.5
    assert row["pe_vwap"] is None
```

- [ ] **Step 6: Run it — verify it fails**

Run: `.venv/bin/python -m pytest apps/api/tests/test_api.py::test_chain_exposes_vwap -p no:cacheprovider -v`
Expected: FAIL — response has no `ce_vwap`.

- [ ] **Step 7: Add fields to ChainStrikeOut and map in chain_out**

In `apps/api/app/schemas.py`:

(a) In `class ChainStrikeOut`, add after `pe_chg_oi`:

```python
    ce_vwap: float | None = None
    pe_vwap: float | None = None
```

(b) In `chain_out`, in the `ChainStrikeOut(...)` construction, add:

```python
            ce_vwap=float(x.ce_vwap) if x.ce_vwap is not None else None,
            pe_vwap=float(x.pe_vwap) if x.pe_vwap is not None else None,
```

- [ ] **Step 8: Run it — verify it passes**

Run: `.venv/bin/python -m pytest apps/api/tests/test_api.py -p no:cacheprovider -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/algo_trading/reporting.py apps/api/app/schemas.py tests/test_reporting.py apps/api/tests/test_api.py
git commit -m "feat(api): expose ce_vwap/pe_vwap on the chain"
```

---

### Task 4: Render VWAP columns and color LTP in the web chain

**Files:**
- Modify: `apps/web/lib/api.ts` (`ChainStrike` type)
- Modify: `apps/web/app/dashboard/page.tsx` (`OptionChainTable`)

**Interfaces:**
- Consumes: `/api/chain` and SSE `chain` payload rows carrying `ce_vwap`/`pe_vwap` (from Task 3).
- Produces: chain table with CE VWAP + PE VWAP columns and LTP cells tinted vs VWAP. No new exported symbols.

- [ ] **Step 1: Extend the ChainStrike type**

In `apps/web/lib/api.ts`, in `export type ChainStrike = { ... }`, add:

```typescript
  ce_vwap?: number | null; pe_vwap?: number | null;
```

- [ ] **Step 2: Add a VWAP-aware LTP helper and column headers**

In `apps/web/app/dashboard/page.tsx`, inside `OptionChainTable` (near the existing `chg` / `bar` helpers), add a helper that renders an LTP tinted vs its VWAP:

```tsx
  const ltpVsVwap = (ltp: number, vwap: number | null | undefined) => {
    if (vwap == null) return <span>{ltp.toFixed(2)}</span>;
    const up = ltp >= vwap;
    return (
      <span className={up ? "text-emerald-400" : "text-red-400"}>
        {ltp.toFixed(2)} {up ? "↑" : "↓"}
      </span>
    );
  };
  const vwapCell = (v: number | null | undefined) =>
    v == null ? <span className="text-neutral-500">—</span> : <span>{v.toFixed(2)}</span>;
```

Then add the two VWAP header cells to the sub-header row. Locate the CALLS/PUTS sub-header (the `<tr className="text-[10px]">` with `OI Trend / OI / Chg OI / LTP / ... / LTP / Chg OI / OI / OI Trend`) and insert a `VWAP` header after the CE `LTP` header and before the strike, and after the strike before the PE `LTP` header:

```tsx
            <th className="px-3 py-1">LTP</th>
            <th className="px-3 py-1">VWAP</th>
            <th className="px-3 py-1 text-center">{chain.atm ? `ATM ${chain.atm.toLocaleString()}` : ""}</th>
            <th className="px-3 py-1 text-left">VWAP</th>
            <th className="px-3 py-1 text-left">LTP</th>
```

(The top header row spans `colSpan={4}` for CALLS and PUTS — bump both to `colSpan={5}` so the group headers still line up.)

- [ ] **Step 3: Render the VWAP cells and colored LTP in each row**

In the `rows.map((r) => (...))` body, replace the CE LTP cell and add a CE VWAP cell after it, and mirror on the PE side. The CE side currently is:

```tsx
              <td className="px-3 py-1.5 text-emerald-400">{r.ce_ltp.toFixed(2)}</td>
```
Change to (LTP colored vs VWAP, then a VWAP cell before the strike):

```tsx
              <td className="px-3 py-1.5">{ltpVsVwap(r.ce_ltp, r.ce_vwap)}</td>
              <td className="px-3 py-1.5">{vwapCell(r.ce_vwap)}</td>
```

The PE side currently is:

```tsx
              <td className="px-3 py-1.5 text-left text-red-400">{r.pe_ltp.toFixed(2)}</td>
```
Change to (VWAP cell first after the strike, then LTP colored vs VWAP):

```tsx
              <td className="px-3 py-1.5 text-left">{vwapCell(r.pe_vwap)}</td>
              <td className="px-3 py-1.5 text-left">{ltpVsVwap(r.pe_ltp, r.pe_vwap)}</td>
```

Column order per the spec mockup: `CE: OI Trend | OI | Chg OI | LTP | VWAP || STRIKE || VWAP | LTP | Chg OI | OI | OI Trend`. Ensure the header cells from Step 2 match this order.

- [ ] **Step 4: Typecheck the web app**

Run: `cd apps/web && npx tsc --noEmit`
Expected: exit 0, no errors.

- [ ] **Step 5: Commit**

```bash
git add apps/web/lib/api.ts apps/web/app/dashboard/page.tsx
git commit -m "feat(web): show CE/PE VWAP columns and tint LTP vs VWAP"
```

---

### Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite + lint + types (DB must be up)**

Run:
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
Seed a couple of chain rows with `vwap` via `repo.write_chain_snapshots` (through the api container), open the dashboard Option Chain tab, and confirm: CE VWAP and PE VWAP columns show ₹ prices (or `—` when null), and each LTP is green ↑ when ≥ its VWAP, red ↓ when below.

- [ ] **Step 4: Commit any doc/verification tidy-ups if needed**

```bash
git commit --allow-empty -m "chore: verify per-option vwap end-to-end"
```

---

## Notes for the implementer

- **VWAP is a price, not a count** — always `toFixed(2)` / `₹`-style, never `fmtOi`.
- **None vs 0** — a token with no VWAP yet must read as `—`/`null`, never `0.00`. Keep the `None` distinct through every layer (DB `NULL` → `Decimal|None` → `float|None` → `null`).
- **Deployment** (out of plan scope, do when the user asks): the `vwap` column is added by `bootstrap_schema` on the next `algo`/`api` startup via `ADD COLUMN IF NOT EXISTS`; existing rows have `NULL` vwap until fresh ticks arrive. A live deploy means rebuilding `algo` + `api` + `web` on the server.
