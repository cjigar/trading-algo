# Expiry-aligned weekly option capture (NIFTY + SENSEX, ±20 strikes) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the full-week option chain for both NIFTY and SENSEX over a ±20-strike CE/PE band, and delete each week's snapshots once its contract expiry has passed.

**Architecture:** Stamp every option-chain snapshot with its contract `expiry`. A per-day row-level `DELETE ... WHERE expiry < today` (run from the `run_algo` loop) purges each expiry the morning after it passes — NIFTY on Wednesday, SENSEX on Friday, automatically. Capture is decoupled from trading: a new `chain_capture_underlyings` setting governs which chains are *captured* (both indices), while `oi_underlyings` still governs which are *traded* (NIFTY only) — so widening capture never arms SENSEX live trading. Compression stays on; the Timescale version supports DELETE on compressed chunks.

**Tech Stack:** Python 3, SQLModel + SQLAlchemy over PostgreSQL/TimescaleDB, pydantic-settings, pytest (against a real TimescaleDB, provisioned by `make db-up`).

## Global Constraints

- **PostgreSQL/TimescaleDB only** — no SQLite/file fallback. Tests require a live TimescaleDB (`make db-up`; `ALGO_TEST_DATABASE_URL` default `postgresql+psycopg://algo:algo@localhost:55432/algo`).
- **TDD** — write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- **Trading safety** — `oi_underlyings` MUST stay `[NIFTY]`. Do NOT add SENSEX to it (that would arm SENSEX live trading). SENSEX capture is enabled only via `chain_capture_underlyings`.
- **Compression stays ON, untouched** — do not add/remove compression policies. The expiry purge works on compressed chunks in this Timescale version.
- **Decimals stored as strings** — existing convention on snapshot rows (`ltp`, `vwap`); `expiry` is a real `date` column.
- **Settings env prefix is `ALGO_`** — e.g. `ALGO_CHAIN_CAPTURE_UNDERLYINGS`.
- **Kotak websocket cap: 200 scrips/subscribe** — ±20 both indices = 166 concurrent; within limit.

## File Structure

- `src/algo_trading/persistence/db.py` — add `expiry` column to `OptionChainSnapshotRow`.
- `src/algo_trading/persistence/bootstrap.py` — idempotent `ADD COLUMN expiry`; lower `DEFAULT_RETENTION_DAYS` 30→14.
- `src/algo_trading/persistence/repositories.py` — stamp `expiry` on write; new `purge_expired_chain_snapshots`.
- `src/algo_trading/feed/option_chain.py` — stamp `inst.expiry` into the snapshot dict.
- `src/algo_trading/core/orchestrator.py` — build chains for the capture set (⊇ traded set); `purge_expired_snapshots` hook.
- `src/algo_trading/config/settings.py` — `chain_capture_underlyings`, `chain_retention_mode`, retention default 14, `chain_feed_window` default 20.
- `src/algo_trading/entrypoints/run_algo.py` — daily purge call in the loop.
- `.env.example` — document the new knobs + `ALGO_SENSEX_INDEX_TOKEN`.
- Tests: `tests/test_option_chain_persistence.py`, `tests/test_option_chain.py`, `tests/test_sensex_oi.py`, `tests/test_oi_orchestrator.py`, `tests/conftest.py` (comment fix).

---

### Task 1: Persist a per-snapshot `expiry` (schema + write path)

**Files:**
- Modify: `src/algo_trading/persistence/db.py` (`OptionChainSnapshotRow`, ~line 207-220)
- Modify: `src/algo_trading/persistence/bootstrap.py` (`_ensure_chain_columns`, ~line 131-136)
- Modify: `src/algo_trading/persistence/repositories.py` (`write_chain_snapshots`, ~line 288-311)
- Test: `tests/test_option_chain_persistence.py`

**Interfaces:**
- Produces: `OptionChainSnapshotRow.expiry: date | None`; `write_chain_snapshots` reads `dict["expiry"]` (a `datetime.date` or absent/None) and persists it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_option_chain_persistence.py` (top-level `from datetime import date` is not present — add `date` to the existing `from datetime import datetime` import at line 8, making it `from datetime import date, datetime`):

```python
def test_write_chain_snapshot_persists_expiry(repo: Repository):
    repo.write_chain_snapshots([
        _snap("T1", "23000", ts=datetime(2025, 1, 15, 10, 0)) | {"expiry": date(2025, 1, 21)},
    ])
    assert repo.latest_chain_state()[0].expiry == date(2025, 1, 21)


def test_write_chain_snapshot_expiry_defaults_none(repo: Repository):
    repo.write_chain_snapshots([_snap("T1", "23000", ts=datetime(2025, 1, 15, 10, 0))])
    assert repo.latest_chain_state()[0].expiry is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_option_chain_persistence.py::test_write_chain_snapshot_persists_expiry -v`
Expected: FAIL — `OptionChainSnapshotRow` has no attribute/column `expiry` (or `TypeError` on the unexpected keyword once Step 4 is partially done). Before any impl it fails at the assert / column-missing.

- [ ] **Step 3a: Add the column to the model**

In `src/algo_trading/persistence/db.py`, change the import at line 15 from:

```python
from datetime import datetime
```

to:

```python
from datetime import date, datetime
```

Then in `OptionChainSnapshotRow` (after the `vwap` field, ~line 220) add:

```python
    expiry: date | None = Field(default=None, index=True)  # contract weekly expiry; drives expiry-aligned purge
```

- [ ] **Step 3b: Add the idempotent ALTER for existing databases**

In `src/algo_trading/persistence/bootstrap.py`, extend `_ensure_chain_columns` (~line 131):

```python
def _ensure_chain_columns(conn: Connection) -> None:
    """Add columns introduced after the hypertable already existed. create_all() only creates
    tables, never alters them, so new columns on option_chain_snapshots are added here."""
    conn.execute(
        text(f"ALTER TABLE {CHAIN_TABLE} ADD COLUMN IF NOT EXISTS vwap varchar")
    )
    conn.execute(
        text(f"ALTER TABLE {CHAIN_TABLE} ADD COLUMN IF NOT EXISTS expiry date")
    )
```

(The `ix_..._expiry` index the model declares is created on restart by `_ensure_declared_indexes`; no manual index DDL needed.)

- [ ] **Step 3c: Stamp expiry on write**

In `src/algo_trading/persistence/repositories.py`, `write_chain_snapshots` (~line 296-309), add `expiry` to the row construction:

```python
                session.add(
                    OptionChainSnapshotRow(
                        trading_day=day,
                        underlying=str(r["underlying"]),
                        strike=str(r["strike"]),
                        option_type=str(r["option_type"]),
                        instrument_token=str(r["instrument_token"]),
                        oi=r.get("oi"),
                        ltp=str(r.get("ltp", "0")),
                        volume=r.get("volume"),
                        vwap=r.get("vwap"),
                        expiry=r.get("expiry"),
                        timestamp=r.get("timestamp") or datetime.utcnow(),
                    )
                )
```

Also update the docstring one-liner at ~line 289 to mention `expiry`:

```python
        """Bulk-insert option-chain snapshot rows. Each dict: underlying, strike, option_type,
        instrument_token, oi, ltp, volume, expiry, timestamp."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_option_chain_persistence.py -v`
Expected: PASS (all, including the two new tests). The `repo`/`engine` session fixture re-bootstraps, so the new column exists.

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/persistence/db.py src/algo_trading/persistence/bootstrap.py src/algo_trading/persistence/repositories.py tests/test_option_chain_persistence.py
git commit -m "feat(persistence): persist per-snapshot option expiry"
```

---

### Task 2: Stamp `inst.expiry` in the chain manager write path

**Files:**
- Modify: `src/algo_trading/feed/option_chain.py` (`on_option_tick`, ~line 113-122)
- Test: `tests/test_option_chain.py`

**Interfaces:**
- Consumes: `Instrument.expiry: date` (already on every resolved chain instrument).
- Produces: every snapshot dict passed to `snapshot_writer.add(...)` now carries `"expiry"` (a `date`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_option_chain.py` (self-contained: a tiny scrip master + a capturing writer). Match the fixture style already used in `tests/test_sensex_oi.py`:

```python
from datetime import date

import pandas as pd

from algo_trading.config.settings import get_settings
from algo_trading.domain.enums import ExchangeSegment, Underlying
from algo_trading.domain.models import Tick
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster


class _CapturingWriter:
    def __init__(self):
        self.rows = []

    def add(self, snapshot: dict) -> None:
        self.rows.append(snapshot)


def _nifty_scrip():
    rows = []
    for k in range(22000, 24000, 50):
        for ot in ("CE", "PE"):
            rows.append({"pTrdSymbol": f"NIFTY{k}{ot}", "pSymbol": f"NIFTY{k}{ot}",
                         "pSymbolName": "NIFTY", "pExpiryDate": "2099-01-30", "dStrikePrice": k,
                         "pOptionType": ot, "lLotSize": 75})
    return ScripMaster.from_dataframe(pd.DataFrame(rows), ExchangeSegment.NSE_FO)


def test_on_option_tick_stamps_expiry(monkeypatch):
    from decimal import Decimal
    s = get_settings(reload=True)
    object.__setattr__(s, "strike_window", 5)
    object.__setattr__(s, "chain_feed_window", 5)
    object.__setattr__(s, "strike_step", Decimal("50"))
    writer = _CapturingWriter()
    chain = OptionChainManager(s, WeeklyOptionResolver(_nifty_scrip()),
                               snapshot_writer=writer, underlying=Underlying.NIFTY)
    chain.on_index_tick(Tick(instrument_token="NIFTY-IDX", exchange_segment=ExchangeSegment.NSE_CM,
                             ltp=Decimal("23000"), timestamp=None, is_index=True))
    chain.on_option_tick(Tick(instrument_token="NIFTY23000CE", exchange_segment=ExchangeSegment.NSE_FO,
                              ltp=Decimal("100"), timestamp=None, oi=1000, volume=50))
    assert writer.rows, "an option tick in the window should produce a snapshot"
    assert writer.rows[-1]["expiry"] == date(2099, 1, 30)
```

Note: if `Tick` requires a non-None `timestamp`, use `datetime(2025, 1, 15, 10, 0)` from `datetime` instead of `None` — check `domain/models.py` `Tick` and adjust. Keep the assertion on `["expiry"]`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_option_chain.py::test_on_option_tick_stamps_expiry -v`
Expected: FAIL with `KeyError: 'expiry'` on the final assert.

- [ ] **Step 3: Add the expiry to the snapshot dict**

In `src/algo_trading/feed/option_chain.py`, `on_option_tick` (~line 114-122), add the `expiry` key:

```python
        if self._writer is not None:
            self._writer.add(
                {
                    "underlying": inst.underlying.value, "strike": str(inst.strike),
                    "option_type": inst.option_type.value, "instrument_token": tick.instrument_token,
                    "oi": tick.oi, "ltp": str(tick.ltp), "volume": tick.volume,
                    "vwap": str(vwap.value) if vwap.value is not None else None,
                    "expiry": inst.expiry,
                    "timestamp": tick.timestamp,
                }
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_option_chain.py::test_on_option_tick_stamps_expiry -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/feed/option_chain.py tests/test_option_chain.py
git commit -m "feat(feed): stamp contract expiry onto chain snapshots"
```

---

### Task 3: Repository purge by expiry

**Files:**
- Modify: `src/algo_trading/persistence/repositories.py` (add method near the other chain methods, ~after line 311)
- Test: `tests/test_option_chain_persistence.py`

**Interfaces:**
- Produces: `Repository.purge_expired_chain_snapshots(today: date | None = None) -> int` — deletes rows with `expiry IS NOT NULL AND expiry < today`; returns rows deleted. NULL-expiry rows are left untouched.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_option_chain_persistence.py`:

```python
def test_purge_removes_expired_keeps_live_and_null(repo: Repository):
    # Expired (NIFTY Tue 2025-01-21), live (2025-01-28), and a legacy NULL-expiry row.
    repo.write_chain_snapshots([_snap("EXP", "23000", ts=datetime(2025, 1, 20, 10, 0)) | {"expiry": date(2025, 1, 21)}])
    repo.write_chain_snapshots([_snap("LIVE", "23050", ts=datetime(2025, 1, 20, 10, 0)) | {"expiry": date(2025, 1, 28)}])
    repo.write_chain_snapshots([_snap("NULLX", "23100", ts=datetime(2025, 1, 20, 10, 0))])  # NULL expiry
    deleted = repo.purge_expired_chain_snapshots(today=date(2025, 1, 22))
    assert deleted == 1
    tokens = {r.instrument_token for r in repo.latest_chain_state()}
    assert tokens == {"LIVE", "NULLX"}  # expired gone; live + legacy NULL kept


def test_purge_is_noop_when_nothing_expired(repo: Repository):
    repo.write_chain_snapshots([_snap("LIVE", "23050", ts=datetime(2025, 1, 20, 10, 0)) | {"expiry": date(2025, 1, 28)}])
    assert repo.purge_expired_chain_snapshots(today=date(2025, 1, 22)) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_option_chain_persistence.py::test_purge_removes_expired_keeps_live_and_null -v`
Expected: FAIL — `Repository` has no attribute `purge_expired_chain_snapshots`.

- [ ] **Step 3: Implement the purge**

In `src/algo_trading/persistence/repositories.py`, add after `write_chain_snapshots` (`delete`, `col`, `date`, and `Session` are already imported at the top of the file):

```python
    def purge_expired_chain_snapshots(self, today: date | None = None) -> int:
        """Delete option-chain snapshots whose contract expiry is strictly before ``today``
        (expiry-aligned retention). Each underlying self-purges the morning after its own expiry
        passes — NIFTY on Wednesday, SENSEX on Friday. Rows with a NULL expiry (legacy, written
        before the expiry column existed) are left for the time-based backstop retention to reap.
        Returns the number of rows deleted."""
        cutoff = today or date.today()
        with Session(self._engine) as session:
            result = session.exec(
                delete(OptionChainSnapshotRow).where(
                    col(OptionChainSnapshotRow.expiry).is_not(None),
                    OptionChainSnapshotRow.expiry < cutoff,
                )
            )
            session.commit()
            return result.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_option_chain_persistence.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/persistence/repositories.py tests/test_option_chain_persistence.py
git commit -m "feat(persistence): purge_expired_chain_snapshots (expiry-aligned retention)"
```

---

### Task 4: Retention backstop + `chain_retention_mode` + orchestrator purge hook

**Files:**
- Modify: `src/algo_trading/config/settings.py` (~line 116)
- Modify: `src/algo_trading/persistence/bootstrap.py` (`DEFAULT_RETENTION_DAYS`, line 44)
- Modify: `src/algo_trading/core/orchestrator.py` (add method; ensure `date` import)
- Modify: `tests/conftest.py` (comment fix, line 18)
- Test: `tests/test_oi_orchestrator.py`

**Interfaces:**
- Consumes: `Repository.purge_expired_chain_snapshots` (Task 3).
- Produces: `Settings.chain_retention_mode: str` (`"expiry"` default | `"days"`); `Settings.chain_retention_days` default `14`; `Orchestrator.purge_expired_snapshots(today: date | None = None) -> int` (no-op unless mode is `"expiry"`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_oi_orchestrator.py`. Reuse whatever settings/scrip helpers that file already has for building an orchestrator; the essential shape (adapt names to the file's existing fixtures):

```python
from datetime import date, datetime  # ensure both imported

def test_orchestrator_purges_expired_snapshots(oi_settings, oi_scrip, engine):
    from algo_trading.core.orchestrator import Orchestrator
    from algo_trading.execution.paper_broker import PaperBroker
    from algo_trading.persistence.repositories import Repository
    object.__setattr__(oi_settings, "chain_retention_mode", "expiry")
    repo = Repository(engine)
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23000", "option_type": "CE",
                                 "instrument_token": "EXP", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 21), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23050", "option_type": "CE",
                                 "instrument_token": "LIVE", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 28), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    orch = Orchestrator(oi_settings, scrip_master=oi_scrip, broker=PaperBroker(), repo=repo)
    assert orch.purge_expired_snapshots(today=date(2025, 1, 22)) == 1
    assert {r.instrument_token for r in repo.latest_chain_state()} == {"LIVE"}


def test_orchestrator_purge_noop_in_days_mode(oi_settings, oi_scrip, engine):
    from algo_trading.core.orchestrator import Orchestrator
    from algo_trading.execution.paper_broker import PaperBroker
    from algo_trading.persistence.repositories import Repository
    object.__setattr__(oi_settings, "chain_retention_mode", "days")
    repo = Repository(engine)
    repo.write_chain_snapshots([{"underlying": "NIFTY", "strike": "23000", "option_type": "CE",
                                 "instrument_token": "EXP", "oi": 1, "ltp": "1", "volume": 1,
                                 "expiry": date(2025, 1, 21), "timestamp": datetime(2025, 1, 20, 10, 0)}])
    orch = Orchestrator(oi_settings, scrip_master=oi_scrip, broker=PaperBroker(), repo=repo)
    assert orch.purge_expired_snapshots(today=date(2025, 1, 22)) == 0  # days mode: app-purge disabled
    assert {r.instrument_token for r in repo.latest_chain_state()} == {"EXP"}
```

If `tests/test_oi_orchestrator.py` has no reusable `oi_settings`/`oi_scrip` fixtures, copy the `_settings()` and `combined_scrip` helpers from `tests/test_sensex_oi.py` into these tests (they build a paper-mode OI orchestrator with a NIFTY+SENSEX scrip).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_oi_orchestrator.py::test_orchestrator_purges_expired_snapshots -v`
Expected: FAIL — `Orchestrator` has no attribute `purge_expired_snapshots`.

- [ ] **Step 3a: Settings — retention default + mode**

In `src/algo_trading/config/settings.py`, change line 116 and add the mode line:

```python
    chain_retention_days: int = 14  # backstop retention: drop chain-snapshot chunks older than this
    # Retention model: "expiry" runs the app-level per-expiry purge (delete a week once its expiry
    # passes); "days" disables it and relies solely on the time-based chain_retention_days policy.
    chain_retention_mode: str = "expiry"
```

- [ ] **Step 3b: Bootstrap default**

In `src/algo_trading/persistence/bootstrap.py`, change line 44:

```python
DEFAULT_RETENTION_DAYS = 14
```

- [ ] **Step 3c: Orchestrator hook**

In `src/algo_trading/core/orchestrator.py`, change the top-level import at line 13 from:

```python
from datetime import UTC
```

to:

```python
from datetime import UTC, date
```

Then add this method to `Orchestrator` (near `flush_snapshots`, ~line 249):

```python
    def purge_expired_snapshots(self, today: date | None = None) -> int:
        """Expiry-aligned retention: delete chain snapshots whose contract expiry has passed.
        No-op unless chain_retention_mode == 'expiry'. Returns the number of rows deleted."""
        if getattr(self._settings, "chain_retention_mode", "expiry") != "expiry":
            return 0
        return self._repo.purge_expired_chain_snapshots(today)
```

- [ ] **Step 3d: conftest comment fix**

In `tests/conftest.py`, line 18, change `production 30-day retention` to `production 14-day retention` (the comment describes why `SchemaTuning` overrides it; keep it accurate).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_oi_orchestrator.py tests/test_timescale_schema.py -v`
Expected: PASS. `test_timescale_schema.py` passes explicit `retention_days` values, so the default change doesn't affect it.

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/config/settings.py src/algo_trading/persistence/bootstrap.py src/algo_trading/core/orchestrator.py tests/test_oi_orchestrator.py tests/conftest.py
git commit -m "feat(persistence): expiry retention mode + orchestrator purge hook; backstop 14d"
```

---

### Task 5: Decouple capture from trading (`chain_capture_underlyings`)

**Files:**
- Modify: `src/algo_trading/config/settings.py` (new field + validator)
- Modify: `src/algo_trading/core/orchestrator.py` (chain/strategy build loop, ~line 97-104)
- Test: `tests/test_sensex_oi.py`

**Interfaces:**
- Produces: `Settings.chain_capture_underlyings: list[Underlying]` (default `[NIFTY, SENSEX]`). Orchestrator builds an `OptionChainManager` for `chain_capture_underlyings ∪ oi_underlyings`, and an `OiSellingStrategy` only for `oi_underlyings`.
- Consumes: existing per-underlying option-tick routing (`self._oi_chains.get(inst.underlying)`), unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sensex_oi.py` (reuses `combined_scrip`, `_settings`, `_feed_both`, `NIFTY_IDX`, `SENSEX_IDX`, `WED` already in that file):

```python
def test_captures_both_but_trades_only_nifty(combined_scrip, engine):
    s = _settings()
    object.__setattr__(s, "oi_underlyings", [Underlying.NIFTY])                              # trade NIFTY only
    object.__setattr__(s, "chain_capture_underlyings", [Underlying.NIFTY, Underlying.SENSEX])  # capture both
    orch = Orchestrator(s, scrip_master=combined_scrip, broker=PaperBroker(), repo=Repository(engine))
    orch.register_index_token(NIFTY_IDX, Underlying.NIFTY)
    orch.register_index_token(SENSEX_IDX, Underlying.SENSEX)
    orch.start_session()
    _feed_both(orch, nifty_ce_oi=5000, sensex_ce_oi=5000)
    orch.flush_snapshots()
    assert {r.underlying for r in orch.repo.latest_chain_state()} == {"NIFTY", "SENSEX"}  # both captured
    orch.evaluate_oi(now=WED)  # Wednesday is a SENSEX trading day — but SENSEX isn't armed
    assert orch.positions.open_position_count() == 0  # SENSEX not in oi_underlyings -> no trade
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sensex_oi.py::test_captures_both_but_trades_only_nifty -v`
Expected: FAIL — SENSEX chain isn't captured (its underlying isn't built), so the captured set is `{"NIFTY"}`, not `{"NIFTY", "SENSEX"}`. (Before the field exists, `getattr` default in the orchestrator handles it; see Step 3.)

- [ ] **Step 3a: Settings — new field + validator**

In `src/algo_trading/config/settings.py`, add after `oi_underlyings` (~line 83):

```python
    # Underlyings whose option chain is CAPTURED into the snapshot store (data-only). A superset
    # of oi_underlyings: capturing an underlying does NOT arm trading it. Default: both indices.
    chain_capture_underlyings: Annotated[list[Underlying], NoDecode] = Field(
        default_factory=lambda: [Underlying.NIFTY, Underlying.SENSEX]
    )
```

And add `chain_capture_underlyings` to the comma-split validator at line 176:

```python
    @field_validator("underlyings", "oi_underlyings", "chain_capture_underlyings", mode="before")
    @classmethod
    def _split_underlyings(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip().upper() for item in v.split(",") if item.strip()]
        return v
```

- [ ] **Step 3b: Orchestrator — build chains for the capture set, strategies for the trade set**

In `src/algo_trading/core/orchestrator.py`, replace the build loop (~line 97-104):

```python
            # One chain manager + strategy per configured underlying (each gated to its own days).
            for u in self._settings.oi_underlyings:
                chain = OptionChainManager(
                    self._settings, self._resolver, subscribe=self._subscribe_option,
                    snapshot_writer=self._writer, underlying=u,
                )
                self._oi_chains[u] = chain
                self._oi_strategies[u] = OiSellingStrategy(self._settings, chain, underlying=u)
```

with:

```python
            # Chains are captured for the capture set (data-only); strategies (and thus orders) are
            # built ONLY for oi_underlyings. The capture set always includes every traded underlying
            # so each strategy has a chain, but capturing SENSEX never arms trading it.
            capture_underlyings = getattr(
                self._settings, "chain_capture_underlyings", self._settings.oi_underlyings
            )
            capture_set = list(dict.fromkeys([*capture_underlyings, *self._settings.oi_underlyings]))
            for u in capture_set:
                self._oi_chains[u] = OptionChainManager(
                    self._settings, self._resolver, subscribe=self._subscribe_option,
                    snapshot_writer=self._writer, underlying=u,
                )
            for u in self._settings.oi_underlyings:
                self._oi_strategies[u] = OiSellingStrategy(
                    self._settings, self._oi_chains[u], underlying=u
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sensex_oi.py -v`
Expected: PASS (all — existing multi-underlying tests set `oi_underlyings=[NIFTY,SENSEX]`, so both are captured and traded there; the new test captures both but arms only NIFTY).

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/config/settings.py src/algo_trading/core/orchestrator.py tests/test_sensex_oi.py
git commit -m "feat(capture): chain_capture_underlyings decouples capture from trading"
```

---

### Task 6: Wire the daily purge into `run_algo`; widen capture window; document env

**Files:**
- Modify: `src/algo_trading/entrypoints/run_algo.py` (imports + loop)
- Modify: `src/algo_trading/config/settings.py` (`chain_feed_window` default, line 86)
- Modify: `.env.example`
- Test: `tests/test_run_algo_purge.py` (new, unit-level — no live loop)

**Interfaces:**
- Consumes: `Orchestrator.purge_expired_snapshots` (Task 4).
- Produces: `run_algo.compute_purge_date(now_utc)` helper (pure, testable) returning the IST date; the loop calls `orch.purge_expired_snapshots(today)` once per IST day.

- [ ] **Step 1: Write the failing test**

The `main()` loop is `# pragma: no cover` (long-running), so extract the date logic into a pure helper and test that. Create `tests/test_run_algo_purge.py`:

```python
from datetime import UTC, date, datetime

from algo_trading.entrypoints.run_algo import compute_purge_date


def test_compute_purge_date_is_ist_calendar_date():
    # 2026-07-22 20:00 UTC == 2026-07-23 01:30 IST -> IST date is the 23rd.
    assert compute_purge_date(datetime(2026, 7, 22, 20, 0, tzinfo=UTC)) == date(2026, 7, 23)


def test_compute_purge_date_before_ist_midnight():
    # 2026-07-22 17:00 UTC == 2026-07-22 22:30 IST -> still the 22nd.
    assert compute_purge_date(datetime(2026, 7, 22, 17, 0, tzinfo=UTC)) == date(2026, 7, 22)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_run_algo_purge.py -v`
Expected: FAIL — `cannot import name 'compute_purge_date'`.

- [ ] **Step 3a: Add the helper + loop wiring**

In `src/algo_trading/entrypoints/run_algo.py`, change the import at line 16 from:

```python
from datetime import UTC, datetime
```

to:

```python
from datetime import UTC, date, datetime
```

Add the pure helper (after `reexec_process`, ~line 46):

```python
def compute_purge_date(now_utc: datetime) -> date:
    """The current IST calendar date — the boundary the expiry purge keys off (NIFTY rolls
    Wednesday, SENSEX Friday, both in IST)."""
    return now_utc.astimezone(IST).date()
```

In `main()`, add a purge-tracker alongside the other loop counters (~after line 153, `stale_since: float | None = None`):

```python
    # Expiry-aligned retention: purge each week's snapshots once its expiry passes. Runs once at
    # startup and then once per IST day (a cheap idempotent DELETE). No-op in "days" retention mode.
    last_purge: date | None = None
```

Inside the `while` loop, as the first guarded block (right after the `try/except` for `process_control_commands`, ~before the feed-recovery block at line 163):

```python
            try:
                today_ist = compute_purge_date(datetime.now(UTC))
                if today_ist != last_purge:
                    deleted = orch.purge_expired_snapshots(today_ist)
                    log.info("chain_purge", deleted=deleted, as_of=str(today_ist))
                    last_purge = today_ist
            except Exception:  # noqa: BLE001
                log.exception("chain_purge_failed")
```

- [ ] **Step 3b: Widen the capture window default**

In `src/algo_trading/config/settings.py`, change line 86:

```python
    chain_feed_window: int = 20  # strikes each side of ATM to subscribe/capture for the chain VIEW
```

- [ ] **Step 3c: Document the new env knobs**

Append to `.env.example` (under the OI/chain section — match the file's existing comment style):

```bash
# --- Option-chain capture & retention ---
# Underlyings whose chain is captured into the snapshot store (data-only; does NOT arm trading).
ALGO_CHAIN_CAPTURE_UNDERLYINGS=NIFTY,SENSEX
# Strikes each side of ATM to subscribe/capture for the chain view (strategy still aggregates
# OI over ALGO_STRIKE_WINDOW). ±20 both indices = 166 scrips, within Kotak's 200/subscribe cap.
ALGO_CHAIN_FEED_WINDOW=20
# Retention model: "expiry" purges each week once its expiry passes; "days" uses only the
# time-based policy below.
ALGO_CHAIN_RETENTION_MODE=expiry
# Backstop time retention (days) — reaps any NULL-expiry/legacy rows the expiry purge skips.
ALGO_CHAIN_RETENTION_DAYS=14
# Required for SENSEX capture to resolve its ATM/chain (BSE index spot token; verified = 1).
ALGO_SENSEX_INDEX_TOKEN=1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_run_algo_purge.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: all pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/algo_trading/entrypoints/run_algo.py src/algo_trading/config/settings.py .env.example tests/test_run_algo_purge.py
git commit -m "feat(capture): daily expiry purge in run_algo; ±20 capture window; env docs"
```

---

## Post-implementation: live-server verification (manual, after deploy)

These are operator steps, not code — run against `kotakserver` after the change is deployed. Not part of the TDD tasks.

1. **SENSEX index token set** — confirm `ALGO_SENSEX_INDEX_TOKEN` is in the live `.env` (verified value `1`, bse_cm). Without it, the SENSEX chain never resolves an ATM and captures nothing.
2. **Subscription acceptance** — during market hours, confirm all 166 scrips subscribe and quotes stream for both indices at ±20 (check the `chain_rewindowed` logs and that `latest_chain_state()` returns both `NIFTY` and `SENSEX` tokens across the ±20 band). If Kotak rejects the volume, set `ALGO_CHAIN_FEED_WINDOW=10` (84 option scrips) and redeploy.
3. **Purge fires on the roll** — after a NIFTY Tuesday expiry, confirm the Wednesday `chain_purge` log shows `deleted > 0` and that `SELECT count(*) ... WHERE underlying='SENSEX'` is untouched (SENSEX's Thursday expiry still live). Repeat the check for SENSEX after its Friday roll.
4. **Store size holds steady** — after a full cycle, `hypertable_size('option_chain_snapshots')` should plateau at roughly one week of both indices rather than growing unbounded.

## Self-Review

- **Spec coverage:** schema `expiry` (T1) · write-path stamp (T2) · row-level purge (T3) · retention backstop 14d + `chain_retention_mode` + orchestrator hook (T4) · both-indices capture decoupled from trading (T5) · daily purge trigger + ±20 window + env docs (T6) · compression left ON (constraint, no task needed) · continuous aggregate left as-is (spec §6, no task) · feed-capacity + purge + retention verification (post-impl manual). All spec sections map to a task.
- **Placeholder scan:** none — every code step shows the exact code; the one conditional (Task 2 `Tick.timestamp`) names the concrete fallback value and where to check.
- **Type consistency:** `purge_expired_chain_snapshots(today: date | None) -> int` used identically in T3 (def), T4 (orchestrator delegate + tests), T6 (loop). `chain_capture_underlyings: list[Underlying]` consistent across T5 def/validator/orchestrator. `compute_purge_date(datetime) -> date` consistent T6 def/test/call. Snapshot dict `"expiry"` key consistent T1 (read) / T2 (write).
