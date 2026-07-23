## Context

This is the first change in a greenfield repository — OpenSpec 1.5.0 is initialized but there is no application code. The goal is a Python service that trades a VWAP / price-action breakout strategy on **NIFTY weekly options** (NSE F&O, `nse_fo`) and **SENSEX weekly options** (BSE F&O, `bse_fo`) **live** through the **Kotak Neo API**, with a **Streamlit** dashboard for monitoring and control.

The design is grounded in verified Kotak Neo SDK behavior (see Decisions). Two facts dominate the architecture:

1. The official SDK is **not on PyPI** and uses **TOTP-first, daily-expiring** sessions.
2. **Streamlit re-executes its script on every interaction**, so the trading loop cannot live inside it.

Constraints: real money; regulatory order-rate cap (≤10 orders/sec/exchange); exchange freeze-quantity limits; daily session expiry; known-buggy websocket reconnect. The operator is a single user running the service on their own machine/VM during market hours.

Sources (verified via web research): Kotak-neo-api-v2 GitHub docs (`Totp_login.md`, `Place_Order.md`, `webSocket.md`, `Scrip_Master.md`), kotakneo.com trade-API guide and support pages.

## Goals / Non-Goals

**Goals:**
- Execute the VWAP-breakout strategy on NIFTY & SENSEX weekly options end-to-end: signal → option resolution → order → position/P&L tracking → exit.
- Enforce live-money safety: paper-mode default, persistent daily-loss kill-switch, idempotent orders, independent square-off, startup reconciliation.
- Provide a Streamlit dashboard for live monitoring and start/stop/flatten control.
- Keep the Kotak SDK isolated behind one wrapper so the rest of the system is broker-agnostic and testable.
- Make strategy parameters configuration-driven so they can be tuned without code changes.

**Non-Goals:**
- Backtesting / historical simulation framework (paper mode is a live-shadow, not a backtester).
- Multi-user, multi-account, or cloud-hosted multi-tenant operation.
- Strategies beyond the VWAP breakout (the interface is pluggable, but only this one strategy ships).
- Broker abstraction for brokers other than Kotak Neo.
- Advanced options analytics (greeks, IV surfaces, multi-leg spread optimization).

## Decisions

### D1: Kotak Neo v2 SDK, installed from a pinned GitHub tag
Use `neo_api_client` (class `NeoAPI`) from `Kotak-Neo/Kotak-neo-api-v2`, pinned to an exact tag/commit (e.g. `@v2.0.2`). **Alternatives considered:** the older v1 repo (OTP-based `session_2fa` flow — worse for unattended running) and unofficial PyPI mirrors like `pk-neo-api-client` (rejected: not official, unacceptable for live money). Rationale: v2 is the current REST stack and is TOTP-first, which suits an unattended service; pinning gives reproducibility.

### D2: Password + MPIN auth with daily re-login
`SessionManager` performs `login(pan|mobilenumber, password)` → `session_2fa(OTP=mpin)`, matching the operator's account (password-based login, PAN identifier). It schedules a pre-market re-login (~08:30–09:00 IST) and re-authenticates on mid-session token-expiry errors. The two SDK calls are isolated in `_do_login`/`_do_2fa` so they can be adapted to SDK-version differences. **Alternatives:** the TOTP-first flow (`totp_login`/`totp_validate`, kept available via an optional `KOTAK_TOTP_SECRET` and `pyotp`) — preferable for fully unattended running but requires a registered authenticator secret the operator does not currently use; and manual OTP-to-phone entry — rejected because it defeats unattended operation (only viable if the account cannot accept the MPIN as the `session_2fa` value).

### D3: Single broker wrapper is the only SDK importer
`broker/kotak_client.py` is the sole module importing `neo_api_client`. It converts typed domain objects ↔ the SDK's string params, selects `nse_fo`/`bse_fo` per underlying, centralizes retries (`tenacity`) and the ≤10 orders/sec/exchange throttle. Rationale: isolates a fragile third-party dependency, enables a mock/paper implementation, and confines the "quantity/price are strings" bug-surface to one place.

### D4: Two self-managed websockets with reconnect + stale-feed halt
`FeedHandler` (quotes/LTP) and `OrderFeedHandler` (order/trade updates) each own heartbeat, exponential-backoff reconnect, and resubscribe-on-reconnect, because the SDK's reconnect is known-buggy. A feed with no ticks for N seconds during market hours is treated as a halt condition. **Alternative:** rely on SDK auto-reconnect — rejected as unreliable.

### D5: Loop and dashboard are separate processes, coupled via the shared database
The orchestrator runs as its own process. The Streamlit dashboard reads state and writes control commands (start/stop/flatten) through a control table in the shared database; it never holds the broker session or places orders. The database is **SQLite** for local dev/tests and **PostgreSQL** for the containerized deployment — selected by `ALGO_DATABASE_URL`, with identical SQLModel schema and code on both (Postgres is a cleaner cross-process/cross-container channel than a shared SQLite file). **Alternatives considered:** running the loop inside Streamlit (rejected — Streamlit reruns the script per interaction and is session-scoped); a REST API / message queue between them (rejected for v1 as over-engineering — the DB is already the persistence layer and is sufficient for a single-operator tool). This can be revisited if the dashboard needs sub-second control latency.

### D6: Paper mode is the default and the pre-live gate
A `MODE=paper|live` toggle routes `OrderRequest`s either to a simulated fill engine (fills at LTP/limit) or to the real `place_order`, with an identical downstream tracking path. Default is `paper`; `live` requires explicit config plus a startup confirmation. Rationale: validate the full pipeline against live data before risking capital; the shared tracking path means paper exercises the same code as live.

### D7: Idempotent order lifecycle + startup reconciliation
Every order carries a unique client `tag`, persisted **before** `place_order`. The order manager is a state machine (PENDING→ACK→PARTIAL→FILLED/REJECTED/CANCELLED) driven by the order feed, with freeze-quantity splitting and throttling. On start/reconnect it reconciles against `order_report()`/`positions()` instead of resubmitting. Rationale: prevents duplicate orders and naked legs across crashes and ambiguous network errors.

### D8: Persistent, authoritative daily-loss kill-switch
`RiskManager` continuously evaluates realized+unrealized day P&L; on breach it sets `HALTED` in SQLite (survives restart, no intraday auto-reset), blocks entries, and optionally flattens. All entry decisions consult the authoritative `AlgoState`. A manual halt is available from the dashboard. Rationale: the loss cap must not be defeated by a restart or a stale in-memory flag.

### D9: Independent square-off timer
The time-based square-off runs on its own scheduler (`apscheduler`) and executes even if the strategy/feed is degraded, verifying flat status against `positions()`. Rationale: end-of-day flattening is a safety guarantee that must not depend on the health of the signal path.

### D10: Config-driven strategy, IST time handling, candle-close evaluation
Strategy parameters live in `pydantic-settings` config. Candles are aggregated with wall-clock-aligned boundaries in IST (`zoneinfo`), and the strategy is evaluated only on candle close to avoid look-ahead/duplicate signals. **Alternative:** tick-driven evaluation — rejected due to look-ahead and double-signal risk.

### D11: Tech stack
Python 3.11+ (3.12 recommended). Dependencies: `neo_api_client` (pinned, optional `broker` extra), `pyotp`, `pydantic`+`pydantic-settings`, `pandas` (scrip-master parsing, indicators), `SQLModel` over SQLite or PostgreSQL (`psycopg`, optional `postgres` extra), `streamlit`, `structlog` (redacting logs), `apscheduler`, `tenacity`, `python-dotenv`. `src/`-layout, pip-installable package `algo_trading`. Deployment: Docker + Docker Compose (Postgres `db` + `algo` loop + `dashboard`).

## Risks / Trade-offs

- **[Live money, real orders]** → Paper-mode default + explicit live arming; startup reconciliation; idempotency keys; independent square-off; persistent kill-switch. Require a documented go-live checklist.
- **[Websocket reconnect is buggy]** → Own heartbeat/backoff/resubscribe; stale-feed halt; reconcile order state on reconnect.
- **[Daily session expiry / mid-day token loss]** → Scheduled pre-market re-login and on-error re-auth that preserves state.
- **[Ambiguous order submission (network error after send)]** → Never resubmit blindly; reconcile against `order_report()`/`positions()` using the persisted client tag.
- **[Scrip-master schema drift]** → Column names vary by segment; confirm against the live file at build time and fail closed (no trading) if parsing fails.
- **[Order-rate / freeze-quantity limits]** → Central throttle (≤10/s/exchange) and freeze-quantity splitting in the order manager.
- **[SQLite as the loop↔dashboard channel]** → Acceptable for a single-operator tool; poll interval bounds control latency. Revisit with an API/queue if multi-second latency becomes a problem.
- **[Streamlit rerun model]** → Dashboard is stateless/observational; all authoritative state and control live in SQLite and the orchestrator process.
- **[Clock skew affecting candle boundaries]** → Use NTP-synced system time and IST-aligned boundaries; gate on candle close.

## Migration Plan

Greenfield — no data migration or rollback of existing behavior. Rollout is a staged go-live:
1. Scaffold package, config, persistence, logging; wire the pipeline in **paper mode** against live feeds.
2. Validate signal→order→exit→tracking and the kill-switch/square-off in paper mode over multiple sessions.
3. Confirm exact strategy parameters with the operator (see Open Questions) and load them into config.
4. Register the TOTP secret; verify pre-market login and reconciliation.
5. Switch `MODE=live` with a small fixed lot size and a conservative daily-loss cap; monitor via the dashboard. "Rollback" = flip back to `MODE=paper` and/or trigger the kill-switch/flatten.

## Open Questions

- **Exact strategy parameters** (deferred to config, must be set before live): VWAP-breakout trigger definition and buffer (points/ATR), candle timeframe (1/3/5/15m), strike selection (ATM vs OTM offset), target points, trail step, stop-loss points, square-off time, lots per trade, daily-loss cap, and max trades/day. Captured in `tasks.md` as a confirm-with-operator task.
- **Product type** for orders (MIS intraday vs NRML) — assumed intraday (MIS) given the time-based square-off; confirm with the operator.
- **Flatten-on-kill-switch** — whether breaching the daily-loss cap should only block new entries or also immediately flatten open positions (configurable; default TBD with operator).
- **Scrip-master CSV columns** — exact per-segment column names to be confirmed against the live downloaded file during implementation.
