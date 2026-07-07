## 1. Project scaffolding & tooling

- [x] 1.1 Create `src/`-layout package `algo_trading` with `pyproject.toml`, pinned dependencies (`neo_api_client` from the Kotak GitHub tag, `pyotp`, `pydantic`+`pydantic-settings`, `pandas`, `SQLModel`, `streamlit`, `structlog`, `apscheduler`, `tenacity`, `python-dotenv`) and dev tools (`pytest`, `ruff`, `mypy`, `freezegun`); target Python 3.11+
- [x] 1.2 Set up virtualenv and verify `neo_api_client` installs and imports from the pinned tag — venv + core/dev deps installed & verified; the Kotak SDK is an external git package left as `make install-broker` for the operator (blocked by the code-from-external policy in this sandbox; wrapper imports it lazily so paper mode + tests run without it)
- [x] 1.3 Add `.env.example`, `.gitignore` (exclude `.env`, DB files, logs), and a README with setup + go-live checklist
- [x] 1.4 Configure `ruff`/`mypy`/`pytest` and a `Makefile`/task runner for lint/type/test

## 2. Config, domain models & observability

- [x] 2.1 Implement `config/settings.py` (`pydantic-settings`): symbols, candle timeframe, lots, strike selection, target/trail/SL points, square-off time, daily-loss cap, max positions, max trades/day, `MODE=paper|live`, stale-feed seconds — with placeholder defaults flagged must-set-before-live
- [x] 2.2 Implement `config/secrets.py`: load Kotak consumer key/secret, mobile, UCC, MPIN, and TOTP secret from `.env`/keyring; never log values
- [x] 2.3 Implement `domain/enums.py` (Side, ExchangeSegment, ProductType, OrderType, Validity, AlgoState) and `domain/models.py` (`Tick, Candle, Signal, OrderRequest, OrderEvent, Position, Trade`)
- [x] 2.4 Implement `observability/logging.py`: structured JSON logging with secret redaction and per-day log files

## 3. Persistence

- [x] 3.1 Implement `persistence/db.py`: SQLite engine + SQLModel schema (orders, trades, P&L snapshots, kill-switch/audit events, algo-state, control-command table)
- [x] 3.2 Implement `persistence/repositories.py`: append-only writes and read APIs for orders/trades/P&L/audit; persisted `AlgoState` get/set
- [x] 3.3 Unit-test persistence round-trips and the append-only audit guarantee

## 4. Broker connectivity (Kotak Neo)

- [x] 4.1 Implement `broker/kotak_client.py` wrapper (only importer of `neo_api_client`): place/modify/cancel order, positions, limits, order/trade reports; typed↔string conversion; `nse_fo`/`bse_fo` selection; `tenacity` retries; ≤10 orders/sec/exchange throttle
- [x] 4.2 Implement `broker/auth.py` `SessionManager`: `totp_login`→`totp_validate` using `pyotp`; pre-market re-login schedule; mid-session expiry re-auth preserving state
- [x] 4.3 Implement `broker/market_data.py` `FeedHandler`: quote websocket subscribe, heartbeat, backoff reconnect + resubscribe, `Tick` normalization, stale-feed detection
- [x] 4.4 Implement `broker/order_feed.py` `OrderFeedHandler`: subscribe to order feed, normalize order/trade updates into `OrderEvent`s
- [~] 4.5 Integration test against Kotak (paper/limits calls only): login flow, feed subscribe, reconnect/resubscribe behavior — **BLOCKED (needs real Kotak credentials + SDK)**. Logic is unit-tested with a fake NeoAPI (`tests/test_broker.py`: param conversion, rejection classification, throttle, tick/order-event normalization, stale-feed). Live login/feed/reconnect verification is deferred to the operator (see task 11.3).

## 5. Instrument resolution

- [x] 5.1 Implement `instruments/scrip_master.py`: download/cache/parse daily `nse_fo` + `bse_fo` CSVs into an indexed table; confirm real column names against a live file; fail closed on error — parser uses candidate column-name lists; **operator must confirm real columns against a live file (task 11.3)**
- [x] 5.2 Implement `instruments/option_resolver.py` `WeeklyOptionResolver`: underlying+spot+side+strike-rule → current-week CE/PE trading symbol, token, lot size; NIFTY vs SENSEX expiry calendars
- [x] 5.3 Unit-test ATM/OTM selection, expiry-week selection, and the no-matching-contract path

## 6. Strategy engine

- [x] 6.1 Implement `strategy/candle_builder.py`: IST wall-clock-aligned candle aggregation from ticks; handle missing/late ticks; emit on candle close
- [x] 6.2 Implement `strategy/indicators.py`: session-anchored VWAP (resets each session), rolling highs/lows, ATR
- [x] 6.3 Implement `strategy/base.py` `Strategy` interface and `strategy/vwap_breakout.py` concrete strategy emitting entry `Signal`s from config-driven breakout rules
- [x] 6.4 Unit-test VWAP reset, breakout trigger/no-trigger, and candle-close-only evaluation

## 7. Execution & tracking

- [x] 7.1 Implement `execution/signal_translator.py`: `Signal` → option `OrderRequest` via resolver + fixed lot sizing + limit-price policy + unique client tag
- [x] 7.2 Implement `execution/order_manager.py`: idempotent state machine (PENDING→ACK→PARTIAL→FILLED/REJECTED/CANCELLED), persist tag before submit, freeze-quantity splitting, throttle, rejection classification (retry vs abort), restart reconciliation against `order_report()`/`positions()`
- [x] 7.3 Implement `execution/position_tracker.py`: live positions and realized/unrealized P&L from fills + LTP
- [x] 7.4 Implement `execution/exit_manager.py`: fixed target → trailing stop, hard stop-loss, and time-based square-off enforcement
- [x] 7.5 Implement the paper-mode simulated fill engine sharing the same tracking path as live
- [x] 7.6 Unit-test order state transitions, freeze-qty split, reconciliation, and each exit path

## 8. Risk management

- [x] 8.1 Implement `risk/risk_manager.py`: pre-trade checks (fixed lots, max positions, max trades/day) rejecting breaching signals before order placement
- [x] 8.2 Implement the persistent daily-loss-cap kill-switch: continuous realized+unrealized P&L eval, set `HALTED` in DB (survives restart, no intraday auto-reset), block entries, optional flatten, audit event
- [x] 8.3 Wire authoritative `AlgoState` consulted by all entry decisions, plus manual halt entry point
- [x] 8.4 Unit-test cap breach, halt-persists-across-restart, and manual halt

## 9. Orchestration & lifecycle

- [x] 9.1 Implement `core/events.py` pub/sub bus decoupling feed → candle → strategy → risk → execution → tracker (thread-safe synchronous bus; chosen over asyncio to safely interoperate with the SDK's thread-callback websockets)
- [x] 9.2 Implement `core/orchestrator.py`: wire the pipeline, graceful start/stop, consume dashboard control commands, startup reconciliation before any new order
- [x] 9.3 Implement `core/scheduler.py` (`apscheduler`): market-hours gating, pre-market login trigger, independent end-of-day square-off timer (verifies flat via `positions()`), logout
- [x] 9.4 Implement `MODE=paper|live` gating: default paper; live requires explicit config + startup confirmation before arming real orders
- [x] 9.5 Implement `run_algo.py` entrypoint
- [x] 9.6 Integration test the full paper-mode pipeline (signal→order→exit→tracking) — in-process end-to-end test in `tests/test_orchestrator.py` (entry, trailing-stop exit, square-off, control-command halt). **Live-feed integration against Kotak deferred to task 11.3 (needs credentials).**

## 10. Monitoring dashboard

- [x] 10.1 Implement `dashboard/state_bridge.py`: read state from SQLite and write start/stop/flatten control commands (no broker session, no direct orders)
- [x] 10.2 Implement `dashboard/app.py` (Streamlit): positions, live P&L, order/trade log, algo state, prominent paper/live indicator, Start/Stop/flatten controls
- [x] 10.3 Implement `run_dashboard.py` entrypoint and verify the loop and dashboard run as separate processes sharing SQLite (verified in `tests/test_dashboard_bridge.py` via separate engines over one DB file)

## 11. Confirm parameters, validate & go-live

- [~] 11.1 Confirm exact strategy parameters with the operator (VWAP breakout definition/buffer, timeframe, strike offset, target/trail/SL points, square-off time, lots, daily-loss cap, max trades/day, product type MIS vs NRML, flatten-on-kill-switch) and load into config — **NEEDS OPERATOR**. All parameters are wired as config in `config/settings.py` / `.env.example` with placeholder defaults; set real values in `.env` before live.
- [x] 11.2 Run `openspec validate add-kotak-fno-trading-algo` and the full test suite; ensure lint/type checks pass — ✅ ruff clean, mypy clean, 60 tests pass, openspec valid
- [~] 11.3 Register the TOTP secret; verify pre-market login, scrip-master download, and startup reconciliation in paper mode — **NEEDS OPERATOR + Kotak credentials/SDK** (`make install-broker`, fill `.env`, place scrip-master CSVs in `scrip_cache/` or run live)
- [~] 11.4 Validate kill-switch and independent square-off behavior in paper mode across multiple sessions — logic is unit/integration-tested; **multi-session live validation needs the operator**
- [~] 11.5 Go live with small fixed lot size and conservative daily-loss cap; monitor via dashboard; document the go-live checklist and paper↔live rollback — **OPERATOR go-live step** (checklist documented in `README.md`)
