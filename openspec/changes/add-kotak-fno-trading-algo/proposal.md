## Why

There is no automated way to trade the user's intended F&O strategy — a VWAP / price-action breakout on NIFTY and SENSEX weekly options — through the Kotak Neo broker. Doing it manually is slow, error-prone, and cannot enforce disciplined exits or a daily-loss limit in real time. This change bootstraps a greenfield Python application that executes the strategy automatically, live, with the risk controls and monitoring a live-money system requires.

## What Changes

- Introduce a new Python application (`src/algo_trading/`, src-layout, pip-installable) — the repository currently has no application code.
- **Kotak Neo integration**: TOTP-first session/auth (daily re-login), an order/positions client wrapper, and two websocket feeds (quotes + order/trade updates) with self-managed reconnect/resubscribe.
- **Weekly-option resolution**: download and parse the daily scrip master to resolve NIFTY (`nse_fo`) and SENSEX (`bse_fo`) weekly-option trading symbols, tokens, and lot sizes.
- **Strategy engine**: build clock-aligned candles, compute session-anchored VWAP + breakout indicators, and emit entry signals through a pluggable strategy interface.
- **Order execution & tracking**: translate signals into option orders, run an idempotent order lifecycle (with freeze-quantity splitting and an OPS throttle), and track live positions and realized/unrealized P&L.
- **Exit management**: fixed target → trailing stop, hard stop-loss, and an independent time-based square-off.
- **Risk controls**: fixed lot sizing plus a persistent daily-loss-cap kill-switch that halts new entries (and can flatten) when breached.
- **Orchestration & modes**: a long-running service that wires the pipeline and owns the daily lifecycle, with a `MODE=paper|live` toggle — **paper is the default**; `live` requires explicit arming.
- **Monitoring dashboard**: a Streamlit UI (separate process) showing positions, live P&L, order/trade logs, and kill-switch status, with Start/Stop/flatten controls and a paper/live indicator.
- **Persistence & observability**: SQLite append-only audit log (orders, trades, P&L snapshots, kill-switch events) and structured, secret-redacting logging.

## Capabilities

### New Capabilities
- `broker-connectivity`: Kotak Neo TOTP session/auth lifecycle, order/positions client wrapper, and the quote + order/trade websocket feeds with reconnect handling.
- `instrument-resolution`: scrip-master ingestion and selection of the correct NIFTY/SENSEX current-week option contract (symbol, token, lot size, strike, CE/PE).
- `strategy-engine`: candle aggregation, VWAP + breakout indicator computation, and pluggable signal generation.
- `order-execution`: signal-to-order translation, idempotent order lifecycle, position & P&L tracking, and exit management (target/trail/SL/time square-off).
- `risk-management`: fixed lot sizing, position limits, and the persistent daily-loss-cap kill-switch with authoritative algo state.
- `trading-orchestration`: the long-running event loop, market-hours/daily lifecycle scheduling, paper/live mode gating, and startup reconciliation.
- `monitoring-dashboard`: Streamlit monitoring and control surface running as a separate process, communicating via the shared datastore.

### Modified Capabilities
<!-- None — this is the first change in a greenfield repo; no existing specs to modify. -->

## Impact

- **New code**: the entire `src/algo_trading/` package and two entrypoints (`run_algo.py`, `run_dashboard.py`). No existing code is modified (greenfield).
- **Dependencies**: `neo_api_client` (installed from a pinned Kotak GitHub tag — not on PyPI), `pyotp`, `pydantic`/`pydantic-settings`, `pandas`, `SQLModel`, `streamlit`, `structlog`, `apscheduler`, `tenacity`, `python-dotenv`. Python 3.11+.
- **External systems**: live Kotak Neo trading API (real orders, real money) and its websocket feeds; NSE/BSE F&O segments.
- **Secrets/ops**: requires Kotak consumer key/secret, mobile, UCC, MPIN, and a registered TOTP secret, loaded from `.env`/keyring and never logged. Daily pre-market re-login is required.
- **Risk**: this is a live-money system — safety is gated by paper-mode default, the daily-loss kill-switch, idempotent order handling, and an independent square-off timer.
