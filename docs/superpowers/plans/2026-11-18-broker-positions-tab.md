# Live Kotak Account Positions Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's Positions tab with a live Kotak account M2M view, priced from REST quotes because the quote websocket is decommissioned.

**Architecture:** The trading loop already polls broker positions every `broker_refresh_seconds` and writes `live_quotes`. We add a batched REST `quotes()` call to that same pass, preferring any websocket LTP so the future SDK-v3 migration needs no change here. The API and SSE stream already carry `broker_pnl`; only two derived summary fields and the frontend tab are new.

**Tech Stack:** Python 3.11, SQLModel/TimescaleDB, FastAPI, Next.js/React/Tailwind, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-broker-positions-tab-design.md`

## Global Constraints

- `apps/api` must never hold a broker session; quotes are fetched by the loop only.
- One batched `quotes()` call per refresh, not one per token.
- A missing/unparseable price must surface as `mtm_pending`, never as a priced position.
- Deploy only after **15:30 IST** — a rebuild restarts the live loop.
- Bare `pytest` excludes `apps/api/tests` (`testpaths = ["tests"]`); run it explicitly.
- Keep `sqlmodel==0.0.39` / `SQLAlchemy<2.1` (naive-UTC columns).

## Review Focus

1. **LTP of `"0"`, `""` or non-numeric** — marking a short at 0 shows a huge fake profit. Must be treated as no quote (`mtm_pending`), never `Decimal(0)`. (Task 1)
2. **`quotes()` returns fewer tokens than requested** — partial responses are normal; unmatched tokens stay pending, no crash. (Task 1)
3. **`quotes()` raises or times out** — refresh completes, other stores still update, positions read `mtm_pending`. (Task 2)
4. **Empty/all-squared position list** — no REST call issued at all. (Task 2)
5. **Paper mode / no authenticated client** — no REST attempt, no error. (Task 2)

---

### Task 1: REST quotes on the broker client

**Files:**
- Modify: `src/algo_trading/broker/base.py` (add to `BrokerClient` Protocol)
- Modify: `src/algo_trading/broker/kotak_client.py` (implement)
- Modify: `src/algo_trading/execution/paper_broker.py` (no-op override)
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: existing `KotakClient._call(name, **kwargs)` and `_as_list`.
- Produces: `quotes(self, tokens: list[tuple[str, str]]) -> dict[str, Decimal]`, mapping
  instrument token -> LTP. Input is `(instrument_token, exchange_segment)` pairs. Tokens whose
  price is absent or unparseable are **omitted** from the result. Empty input returns `{}` without
  calling the SDK. `PaperBroker.quotes` always returns `{}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_quotes_maps_token_to_ltp():
    broker = _kotak_with_quotes_response([
        {"exchange_token": "835470", "ltp": "150.2500"},
        {"exchange_token": "999", "ltp": "12.0000"},
    ])
    assert broker.quotes([("835470", "bse_fo"), ("999", "nse_cm")]) == {
        "835470": Decimal("150.25"), "999": Decimal("12"),
    }

def test_quotes_omits_unusable_prices():
    """A zero/blank/garbage LTP must not mark a position - it reads as 'no quote'."""
    broker = _kotak_with_quotes_response([
        {"exchange_token": "A", "ltp": "0"},
        {"exchange_token": "B", "ltp": ""},
        {"exchange_token": "C", "ltp": "n/a"},
        {"exchange_token": "D", "ltp": "7.5"},
    ])
    assert broker.quotes([(t, "bse_fo") for t in "ABCD"]) == {"D": Decimal("7.5")}

def test_quotes_tolerates_missing_tokens_in_response():
    broker = _kotak_with_quotes_response([{"exchange_token": "A", "ltp": "5"}])
    assert broker.quotes([("A", "bse_fo"), ("B", "bse_fo")]) == {"A": Decimal("5")}

def test_quotes_empty_input_makes_no_sdk_call():
    broker, calls = _kotak_recording_calls()
    assert broker.quotes([]) == {}
    assert calls == []

def test_paper_broker_quotes_is_empty():
    assert PaperBroker().quotes([("A", "bse_fo")]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_broker.py -k quotes -v`
Expected: FAIL — `AttributeError: 'KotakClient' object has no attribute 'quotes'`

- [ ] **Step 3: Implement `quotes(tokens: list[tuple[str, str]]) -> dict[str, Decimal]`**

In `kotak_client.py`, call `self._call("quotes", instrument_tokens=[{"instrument_token": t, "exchange_segment": seg} for t, seg in tokens], quote_type="ltp")`. The SDK returns a list of dicts keyed `exchange_token` / `ltp`. Parse each `ltp` with the module's existing Decimal-coercion style; skip any value that fails to parse or is `<= 0`. Add the signature to the `BrokerClient` Protocol and a `return {}` override on `PaperBroker`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_broker.py -k quotes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/broker/base.py src/algo_trading/broker/kotak_client.py src/algo_trading/execution/paper_broker.py tests/test_broker.py
git commit -m "feat(broker): add batched REST quotes() for LTP"
```

---

### Task 2: Price positions from REST in the broker refresh

**Files:**
- Modify: `src/algo_trading/core/orchestrator.py:436` (`_mark_open_positions`)
- Test: `tests/test_broker_refresh.py`

**Interfaces:**
- Consumes: `BrokerClient.quotes(...)` from Task 1; existing `self._ltp`, `self._coordinator`,
  `Repository.upsert_live_quotes`.
- Produces: no signature change — `_mark_open_positions(positions: list[dict]) -> int` still
  returns the number of tokens priced.

Behaviour: build the candidate token set from **all** positions (not only open ones). Keep the
existing subscribe-once websocket call for open tokens. For every candidate without a value in
`self._ltp`, issue **one** batched `self._broker.quotes(...)`; merge those into the websocket
values (websocket wins) and `upsert_live_quotes` the union.

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_prices_tokens_from_rest_when_feed_is_silent(scrip, engine):
    orch = _orch(scrip, engine, _open_broker_with_quotes({"835470": Decimal("150")}))
    orch._coordinator = FakeCoord()
    orch.refresh_broker_account()
    assert Repository(engine).live_quotes(["835470"]) == {"835470": Decimal("150")}

def test_websocket_ltp_takes_precedence_over_rest(scrip, engine):
    broker, asked = _open_broker_recording_quote_tokens({"835470": Decimal("150")})
    orch = _orch(scrip, engine, broker)
    orch._coordinator = FakeCoord()
    orch._ltp["835470"] = Decimal("151")
    orch.refresh_broker_account()
    assert Repository(engine).live_quotes(["835470"])["835470"] == Decimal("151")
    assert "835470" not in asked      # not requested over REST at all

def test_squared_positions_are_priced_too(scrip, engine):
    """The Kotak screen shows an LTP for closed rows, so they are quoted as well."""
    broker, asked = _open_broker_recording_quote_tokens({"111": Decimal("9")})
    orch = _orch(scrip, engine, broker)
    orch._coordinator = FakeCoord()
    orch.refresh_broker_account()
    assert "111" in asked

def test_rest_quotes_failure_is_fail_safe(scrip, engine):
    orch = _orch(scrip, engine, _open_broker_raising_quotes())
    orch._coordinator = FakeCoord()
    summary = orch.refresh_broker_account()      # must not raise
    assert summary["positions"] == len(OPEN_POSITIONS)
    assert Repository(engine).live_quotes(["835470"]) == {}

def test_no_positions_makes_no_quote_call(scrip, engine):
    broker, asked = _empty_broker_recording_quote_tokens()
    orch = _orch(scrip, engine, broker)
    orch.refresh_broker_account()
    assert asked == []

def test_paper_mode_makes_no_quote_call(scrip, engine):
    orch = _orch(scrip, engine, PaperBroker())
    orch.refresh_broker_account()                # no coordinator, no creds - must be harmless
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_broker_refresh.py -v`
Expected: FAIL — REST-sourced tokens absent from `live_quotes`; `asked` empty.

- [ ] **Step 3: Implement the REST fallback in `_mark_open_positions`**

Wrap the `quotes(...)` call in its own `try/except` logging `broker_quotes_failed`, matching the
per-step fail-safe style already used in `refresh_broker_account`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_broker_refresh.py -v`
Expected: PASS (existing tests in the file included)

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/core/orchestrator.py tests/test_broker_refresh.py
git commit -m "feat(orchestrator): price broker positions from REST when the feed is silent"
```

---

### Task 3: Unrealised and net-traded-value summary fields

**Files:**
- Modify: `src/algo_trading/reporting.py` (`BrokerPnLSummary`, `summarize_broker_positions`)
- Modify: `apps/api/app/schemas.py` (`BrokerPnLOut`, `broker_pnl_out`)
- Test: `tests/test_reporting.py`, `apps/api/tests/test_api.py`

**Interfaces:**
- Produces: `BrokerPnLSummary.total_unrealized: Decimal` and `.net_traded_value: Decimal`, mirrored
  as floats on `BrokerPnLOut`. `total_unrealized = total_pnl - total_realized`.
  `net_traded_value = Σ(sellAmt) - Σ(buyAmt)` over all positions.

**Note:** before fixing `net_traded_value`, verify the formula reproduces **-38,351** against the
live account (the reference screenshot's figure, alongside Realised **+7,212** and Unrealised
**+13,094**). If it does not, derive the formula that does and record it in the spec.

- [ ] **Step 1: Write the failing tests**

```python
def test_unrealized_is_total_minus_realized():
    s = summarize_broker_positions(
        [{"trdSym": "X", "tok": "1", "flBuyQty": "0", "flSellQty": "100",
          "buyAmt": "0", "sellAmt": "25358"}],
        quotes={"1": Decimal("104.25")},
    )
    assert s.total_realized == Decimal(0)
    assert s.total_unrealized == s.total_pnl

def test_net_traded_value_is_sell_minus_buy_across_all_positions():
    s = summarize_broker_positions([
        {"trdSym": "A", "tok": "1", "flBuyQty": "100", "flSellQty": "0",
         "buyAmt": "1000", "sellAmt": "0"},
        {"trdSym": "B", "tok": "2", "flBuyQty": "0", "flSellQty": "50",
         "buyAmt": "0", "sellAmt": "400"},
    ], quotes={})
    assert s.net_traded_value == Decimal("-600")

def test_broker_pnl_out_exposes_both_fields():
    out = broker_pnl_out([...], quotes={...})
    assert out.total_unrealized == pytest.approx(...)
    assert out.net_traded_value == pytest.approx(...)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reporting.py -k "unrealized or net_traded" -v`
Expected: FAIL — `AttributeError: 'BrokerPnLSummary' object has no attribute 'total_unrealized'`

- [ ] **Step 3: Add both fields**

Accumulate `net_traded_value` in the existing loop over rows; derive `total_unrealized` at
construction. Mirror onto `BrokerPnLOut` in `broker_pnl_out`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reporting.py -v && pytest apps/api/tests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/algo_trading/reporting.py apps/api/app/schemas.py tests/test_reporting.py apps/api/tests/test_api.py
git commit -m "feat(reporting): expose unrealised P&L and net traded value"
```

---

### Task 4: Rewrite the Positions tab as the account view

**Files:**
- Modify: `apps/web/app/dashboard/page.tsx:128` (the `tab === "Positions"` block)
- Modify: `apps/web/lib/api-types.ts` (add the two new `BrokerPnL` fields)

**Interfaces:**
- Consumes: `data.broker_pnl` (already on the SSE stream, so no new fetch) with
  `total_pnl`, `total_unrealized`, `total_realized`, `net_traded_value`, `open_count`,
  `mtm_pending_count`, and `per_position[]` carrying `symbol`, `net_qty`, `avg_buy`, `avg_sell`,
  `ltp`, `total_pnl`, `is_open`, `mtm_pending`.

Layout: a four-tile summary strip (Profit/Loss, Unrealised P/L, Realised P/L, Net traded value)
above a table with columns Name, Type, Quantity, Avg. cost, LTP, Profit/loss. Open rows at full
contrast, squared rows de-emphasised but still showing LTP. Signed values coloured green/red.
Rows with `mtm_pending` show a marker in place of a P&L figure rather than a number.

- [ ] **Step 1: Update the stream type**

Add `total_unrealized: number` and `net_traded_value: number` to the `BrokerPnL` type.

- [ ] **Step 2: Replace the Positions tab body**

Render the strip and table from `brokerPnl`. Reuse the existing table/card classes from the Orders
and Trades tabs so the styling matches. Keep the tab label `"Positions"` in `TABS`.

- [ ] **Step 3: Typecheck**

Run: `cd apps/web && npx tsc --noEmit`
Expected: no errors

- [ ] **Step 4: Verify against the running stack**

With the loop running, open the dashboard and confirm: Realised reads **+7,212**, the four open
SENSEX rows show non-null LTP, and Unrealised is non-zero. Confirm `mtm_pending_count` is 0 via
`/api/broker-pnl`.

- [ ] **Step 5: Commit**

```bash
git add apps/web/app/dashboard/page.tsx apps/web/lib/api-types.ts
git commit -m "feat(web): replace Positions tab with live Kotak account M2M view"
```
