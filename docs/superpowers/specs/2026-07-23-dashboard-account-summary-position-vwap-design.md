# Persistent account summary + position VWAP in the P&L table

## Context

The operator watches the dashboard while it trades. Two at-a-glance needs are unmet:

1. The **Broker account P&L (live M2M)** panel — the numbers that matter most (live M2M, realized,
   open positions) — lives only on the **P&L tab**. When the operator is on the Option Chain,
   Trades, or Config tab, those numbers are gone. They want the summary (plus unrealized and the
   algo state) pinned at the top, visible on every tab.
2. The broker M2M **positions table** shows Avg buy / Avg sell / LTP per position but no VWAP.
   The operator wants each position's session VWAP shown **between Avg sell and LTP**, to judge
   the current premium against its volume-weighted average (the same read the chain now offers,
   applied to open positions).

All the data already rides the SSE `/stream` payload (`broker_pnl`, `pnl`, `state`), so the
summary strip is frontend-only. The position VWAP needs a small backend read: reuse the
per-option VWAP already persisted on `option_chain_snapshots.vwap`, looked up by the position's
instrument token.

Outcome: a compact account-summary strip pinned below the NIFTY/SENSEX ticker on all tabs, and a
VWAP column in the broker positions table.

## Decisions (confirmed with operator)

- **Position VWAP source:** reuse `option_chain_snapshots.vwap` looked up by the position's
  instrument token. No new computation. Populated when the position is inside the captured ATM
  chain window (normal for ATM±3 OI shorts); a position outside the window shows `—`. `None`
  (unknown) stays distinct from `0`.
- **Top panel scope:** summary **cards only** — Live M2M, Realized, Unrealized, Algo state, Open
  positions, Positions total. The full per-position table stays on the P&L tab.
- **Unrealized value:** broker-account unrealized = `Live M2M − Realized` (`total_pnl −
  total_realized`), derived on the frontend. No backend field needed.
- **Pinning:** the summary strip is pinned while scrolling, stacked with the existing ticker.

## Part A — Position VWAP column (backend + frontend)

Data flow mirrors how LTP already reaches the positions table
(`bridge.live_quotes_for(tokens)` → `repo.live_quotes(tokens)` → `summarize_broker_positions`).

- **`src/algo_trading/persistence/repositories.py`** — add
  `latest_vwap_for(tokens: list[str]) -> dict[str, Decimal]`: newest non-null `vwap` per token
  from `option_chain_snapshots`, using the existing DISTINCT-ON-latest-per-token pattern
  (mirror `live_quotes`, but select `vwap` and skip NULL vwap rows). Empty/no-token input → `{}`.
- **`src/algo_trading/dashboard/state_bridge.py`** — add
  `chain_vwaps_for(tokens: list[str]) -> dict[str, Decimal]` mirroring `live_quotes_for`
  (filter empties, delegate to `repo.latest_vwap_for`).
- **`src/algo_trading/reporting.py`** — `summarize_broker_positions(rows, quotes, vwaps=None)`
  gains a `vwaps` param; each `BrokerPositionPnL` gets `vwap = vwaps.get(str(r.get("tok", "")))`
  (Decimal | None). Add `vwap: Decimal | None = None` to the `BrokerPositionPnL` dataclass.
- **`apps/api/app/schemas.py`** — `BrokerPositionPnLOut` gains `vwap: float | None`;
  `broker_pnl_out(rows, quotes, vwaps=None)` passes `vwaps` through and maps
  `float(p.vwap) if p.vwap is not None else None`.
- **`apps/api/app/routes.py`** — both the `/broker-pnl` handler and `build_stream_payload`
  compute the position tokens (already done for quotes) and pass
  `bridge.chain_vwaps_for(tokens)` into `broker_pnl_out(rows, quotes, vwaps)`.
- **`apps/web/lib/api.ts`** — `BrokerPositionPnL` type gains `vwap?: number | null`.
- **`apps/web/app/dashboard/page.tsx`** — `BrokerPnLTable`: add a `VWAP` `<th>` **between "Avg
  sell" and "LTP"** and a matching `<td>` rendering `vwap.toFixed(2)` or `—` when null/undefined.
  Uses `== null` to catch null+undefined. VWAP is a ₹ price — never `fmtOi`.

## Part B — Persistent account summary strip (frontend-only)

- **`apps/web/components/ui.tsx`** — new `AccountSummary({ brokerPnl, algoState })` component:
  a compact card/stat strip showing Live M2M (`total_pnl`), Realized (`total_realized`),
  Unrealized (`total_pnl - total_realized`), Algo state (`algoState`), Open positions
  (`open_count`), Positions total (`per_position.length`). Money values signed/colored
  (green ≥ 0, red < 0), consistent with the existing `Signed`/broker-card styling. Renders
  nothing when `brokerPnl` is null.
- **`apps/web/app/dashboard/page.tsx`** —
  - Wrap the existing `SpotTicker` and the new `AccountSummary` in a single
    `sticky top-0 z-10` header container so they pin together (remove the internal
    `sticky top-0` from `SpotTicker`'s own wrapper so the two don't fight; the outer wrapper
    owns stickiness). Rendered above `<Tabs>`, so both show on every tab.
  - Pass `brokerPnl` (from the SSE `data.broker_pnl`) and `state?.algo_state` to `AccountSummary`.
  - On the **P&L tab**: remove the four broker summary `<Metric>` cards (Live M2M / Realized /
    Open / Total) — now shown up top — but keep the "Broker account P&L (live M2M)" heading, the
    explanatory note, and `BrokerPnLTable`. Remove the redundant "Algo state" `<Metric>` from the
    Algo-session P&L block (it is now pinned up top); keep Day P&L / Realized / Unrealized there.

## Edge cases
- Position VWAP `None` when the token isn't in `option_chain_snapshots` (outside chain window, or
  before any tick) → `—`, no coloring, never `0`.
- `AccountSummary` renders nothing until `broker_pnl` arrives on the stream.
- Two stacked sticky bars: solved by one shared sticky wrapper, not per-bar `top` offsets.

## Testing
- **Repo:** `latest_vwap_for` returns newest non-null vwap per requested token; `{}` for empty
  input; a token with only NULL-vwap rows is absent from the result.
- **summarize_broker_positions:** with a `vwaps` map, the matching position gets `vwap` set; a
  position whose token isn't in the map gets `vwap = None` (not 0).
- **API:** `/broker-pnl` and the stream payload include `vwap` per position (value when known,
  `null` otherwise).
- Full pytest + apps/api tests + ruff + mypy green (DB up via `make db-up`); `tsc --noEmit` clean.
- **Manual:** VWAP column appears between Avg sell and LTP; the account summary strip is pinned
  below the ticker and visible on every tab (P&L, Positions, Orders, Trades, Option Chain,
  Config); scrolling a long tab keeps it visible.

## Out of scope
- Publishing VWAP for positions outside the captured chain window (accepted `—` gap).
- Any change to how VWAP itself is computed (reuses the existing persisted per-option VWAP).
- Moving the per-position table or algo-session detail out of the P&L tab.
