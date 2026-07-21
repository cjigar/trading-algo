# algo-trading

A Python service that trades a **VWAP / price-action breakout** strategy on **NIFTY weekly options** (NSE F&O) and **SENSEX weekly options** (BSE F&O) through the **Kotak Neo API**, with a **Next.js + FastAPI** web app for monitoring and control (see [Web app](#web-app-nextjs--fastapi-monorepo)).

> ⚠️ **Live-money system.** Paper mode is the default. Live trading places real orders and must be armed explicitly. Read the go-live checklist below before switching to `live`.

## Architecture

The trading loop runs as its **own process**; the web app (`apps/api` + `apps/web`) runs **separately** and communicates with the loop through a shared **PostgreSQL/TimescaleDB** database — the same backend locally and in Docker (the web app never holds the broker session or places orders directly).

```
market-data feed ─┐
                  ├─▶ candle builder ─▶ strategy ─▶ risk ─▶ execution ─▶ position/P&L tracker
order feed ───────┘                                                          │
                                                                            ▼
                                                        PostgreSQL/TimescaleDB (audit + control)
                                                                            ▲
                                                              FastAPI (apps/api) ← Next.js (apps/web)
```

Package layout (`src/algo_trading/`):

| Package | Responsibility |
|---|---|
| `config/` | Typed settings + secret loading |
| `domain/` | Enums and immutable data models |
| `persistence/` | DB schema + repositories (append-only audit; PostgreSQL/TimescaleDB hypertables) |
| `broker/` | Kotak client wrapper, auth/session, market-data & order websockets, live-feed coordinator |
| `instruments/` | Scrip-master ingestion + weekly-option resolver |
| `strategy/` | Candle builder, indicators (VWAP/ATR), pluggable strategies |
| `execution/` | Signal→order translation, order lifecycle, position tracking, exits, paper fills |
| `risk/` | Lot sizing, limits, persistent daily-loss kill-switch |
| `core/` | Event bus, orchestrator, scheduler |
| `dashboard/` | `StateBridge` — read state + write control commands (shared by the web API) |
| `entrypoints/` | `run_algo`, `run_capture` |
| `reporting.py` | fills P&L + option-chain summaries (used by the web API) |

The web UI lives in the Turborepo (`apps/api` FastAPI + `apps/web` Next.js) — see [Web app](#web-app-nextjs--fastapi-monorepo).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
make install            # core + dev deps (paper mode + tests, no broker SDK)
make install-broker     # additionally installs the Kotak Neo SDK from its pinned tag
cp .env.example .env    # fill in credentials + parameters
make db-up              # TimescaleDB on 127.0.0.1:55432 — required for running AND for tests
```

The Kotak Neo SDK is **not on PyPI**; `make install-broker` pulls it from the pinned GitHub tag. Paper mode and the full test suite run without it.

The test suite talks to a real TimescaleDB: it creates a throwaway `algo_test_<pid>` database on the server from `ALGO_TEST_DATABASE_URL` (default `postgresql+psycopg://algo:algo@localhost:55432/algo`) and drops it afterwards. Without a reachable server the suite fails with a pointer to `make db-up` — it never falls back to another backend.

## Running

```bash
make run         # trading loop (defaults to paper mode)
```

For the monitoring/control UI, see [Web app](#web-app-nextjs--fastapi-monorepo).

## Docker (Postgres + loop + web app)

The containerized stack runs via Docker Compose — **`db`** (TimescaleDB), **`algo`** (trading loop), **`api`** (FastAPI), and **`web`** (Next.js) — all sharing the same PostgreSQL database.

```bash
cp .env.example .env       # fill Kotak creds + params; POSTGRES_* have working defaults
mkdir -p scrip_cache       # drop nse_fo.csv / bse_fo.csv here for paper mode
make docker-up             # build + start db, algo  (add: docker compose up -d api web  →  http://localhost:3001)
make docker-logs           # tail algo
make docker-down           # stop
```

- **PostgreSQL is required** — `ALGO_DATABASE_URL` must be set to a `postgresql+psycopg://…` URL and there is no file/SQLite fallback. Compose sets it to the `db` service automatically; for local runs start the same container with `make db-up` (published on `127.0.0.1:55432`) and use the URL in `.env.example`.
- The `db` service runs **TimescaleDB** (`timescale/timescaledb:latest-pg16` — *not* the `-ha` variant, which relocates `PGDATA` and would ignore the existing `pgdata` volume). At startup the app creates the extension, converts `option_chain_snapshots` and `pnl_snapshots` to hypertables, and applies the compression, retention, and continuous-aggregate policies — all idempotent, so restarts are no-ops. Tuning lives in `ALGO_CHAIN_*` settings.
- To bake the Kotak SDK into the image (for live mode), build with `INSTALL_BROKER=1` (env var or `--build-arg`).
- Postgres data persists in the `pgdata` volume; `make docker-down` keeps it (`docker compose down -v` wipes it).

**Upgrading an existing (stock PostgreSQL) deployment to TimescaleDB** — do this **outside market hours**:

1. Snapshot the volume first: `docker compose stop algo api && docker run --rm -v trading-algo_pgdata:/v -v "$PWD:/b" alpine tar czf /b/pgdata-backup.tgz /v`.
2. `docker compose pull db && docker compose up -d db` (same PG16 major version, same volume), wait for `healthy`.
3. `docker compose up -d algo api` — first startup creates the extension, fixes the snapshot tables' primary keys, converts them to hypertables (`migrate_data`, so existing rows are kept), and installs the policies.
4. Verify: `docker compose exec db psql -U algo -d algo -c "select hypertable_name from timescaledb_information.hypertables"` and `... -c "select proc_name, config from timescaledb_information.jobs"`.
5. Rollback: hypertable-converted tables are **not** readable by stock PostgreSQL — restore the snapshot from step 1 and revert the image tag.

## Option-chain capture (read-only, no orders)

To validate live OI/LTP capture **without any risk**, run capture-only mode. It authenticates,
streams the NIFTY ATM±5 option chain into the DB (and the dashboard's ⛓️ Option Chain tab), and
**never evaluates the strategy or places an order** (a PaperBroker is wired as a hard guard):

```bash
# requires the SDK + credentials + ALGO_NIFTY_INDEX_TOKEN; run during market hours (09:15–15:30 IST)
docker compose run --rm algo python -m algo_trading.entrypoints.run_capture   # or: make capture
```

Watch the dashboard's Option Chain tab fill with real per-strike OI/LTP and the CE-vs-PE aggregate.
This is the safe way to validate the OI feed before ever arming shorts.

## Live market-data feed

In live mode the loop connects the Kotak websockets. `LiveFeedCoordinator` (`broker/live_feed.py`)
subscribes the **underlying index feeds** (so candles build) and the **order feed**, routes each
incoming message to the quote or order handler, and subscribes an option's quotes when a position
opens (so exits get its LTP). Wiring is `run_algo → Orchestrator.attach_live_feeds()`; it's a no-op
in paper mode (no authenticated client), so the loop stays idle until a feed is attached.

To get real ticks flowing (candles → signals → fills):

1. Set the index-spot tokens in `.env` — `ALGO_NIFTY_INDEX_TOKEN`, `ALGO_SENSEX_INDEX_TOKEN`
   (from Kotak's index scrip list).
2. Build the image with the SDK and run in live mode:
   ```bash
   INSTALL_BROKER=1 docker compose build
   # .env: ALGO_MODE=live + ALGO_CONFIRM_LIVE=YES to arm the feed + real orders
   make docker-up
   ```

The message-type routing heuristic (`is_order_message`) and the exact index tokens may need small
adjustments against your live SDK/account — both are isolated in `broker/live_feed.py` and
`broker/order_feed.py`.

## Web app (Next.js + FastAPI monorepo)

A Turborepo monorepo adds a modern web UI (replacing Streamlit). Both are **read/control-only** over
the shared DB — no broker session, no orders from the web tier.

- `apps/api` — FastAPI over the existing `algo_trading` engine (state, P&L, positions, orders,
  trades, chain, config edit, controls, SSE live stream, single-user auth). Tests: `pytest apps/api/tests`.
- `apps/web` — Next.js (App Router + Tailwind): login, tabbed monitoring, Start/Stop/Flatten,
  config editor, live updates via SSE.

```bash
npm install                       # installs web deps + turbo (workspaces)
# local dev (two servers):
uvicorn app.main:app --reload --app-dir apps/api   # API on :8000  (needs WEB_AUTH_PASSWORD)
npm run dev -w @trading-algo/web                     # web on :3000
# or the whole stack in Docker:
docker compose up -d db api web                      # web → http://localhost:3000
```

Set `WEB_AUTH_PASSWORD` / `WEB_AUTH_SECRET` (and `NEXT_PUBLIC_API_BASE` for the browser). The API's
config editor only edits whitelisted tunables (never secrets or live-arming), persisted to an
overrides file the loop reads on reload.

## Quality

```bash
make db-up       # once: the suite needs PostgreSQL/TimescaleDB
make check       # ruff + mypy + pytest
```

## Go-live checklist

1. `make check` is green; unit + paper-mode integration behavior validated.
2. Exact strategy parameters confirmed and set in `.env` (see the `ALGO_*` placeholders).
3. Credentials filled (consumer key/secret, PAN or mobile, password, MPIN); pre-market login, scrip-master download, and startup reconciliation verified in **paper** mode.
4. Kill-switch (daily-loss cap) and the independent square-off timer validated in paper mode across multiple sessions.
5. Set `ALGO_MODE=live` **and** `ALGO_CONFIRM_LIVE=YES`, start with a **small fixed lot size** and a **conservative daily-loss cap**, and monitor via the dashboard.
6. Rollback = set `ALGO_MODE=paper` and/or trigger the kill-switch / manual flatten from the dashboard.
