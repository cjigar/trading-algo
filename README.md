# algo-trading

A Python service that trades a **VWAP / price-action breakout** strategy on **NIFTY weekly options** (NSE F&O) and **SENSEX weekly options** (BSE F&O) through the **Kotak Neo API**, with a **Streamlit** dashboard for monitoring and control.

> ⚠️ **Live-money system.** Paper mode is the default. Live trading places real orders and must be armed explicitly. Read the go-live checklist below before switching to `live`.

## Architecture

The trading loop runs as its **own process**; the Streamlit dashboard runs **separately** and communicates with the loop through a shared database — **SQLite** locally, **PostgreSQL** in Docker (it never holds the broker session or places orders directly).

```
market-data feed ─┐
                  ├─▶ candle builder ─▶ strategy ─▶ risk ─▶ execution ─▶ position/P&L tracker
order feed ───────┘                                                          │
                                                                            ▼
                                                                     SQLite (audit + control)
                                                                            ▲
                                                                   Streamlit dashboard
```

Package layout (`src/algo_trading/`):

| Package | Responsibility |
|---|---|
| `config/` | Typed settings + secret loading |
| `domain/` | Enums and immutable data models |
| `persistence/` | DB schema + repositories (append-only audit; SQLite or Postgres) |
| `broker/` | Kotak client wrapper, auth/session, market-data & order websockets, live-feed coordinator |
| `instruments/` | Scrip-master ingestion + weekly-option resolver |
| `strategy/` | Candle builder, indicators (VWAP/ATR), pluggable strategies |
| `execution/` | Signal→order translation, order lifecycle, position tracking, exits, paper fills |
| `risk/` | Lot sizing, limits, persistent daily-loss kill-switch |
| `core/` | Event bus, orchestrator, scheduler |
| `dashboard/` | Streamlit UI + state/control bridge |
| `entrypoints/` | `run_algo`, `run_dashboard` |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
make install            # core + dev deps (paper mode + tests, no broker SDK)
make install-broker     # additionally installs the Kotak Neo SDK from its pinned tag
cp .env.example .env    # fill in credentials + parameters
```

The Kotak Neo SDK is **not on PyPI**; `make install-broker` pulls it from the pinned GitHub tag. Paper mode and the full test suite run without it.

## Running

```bash
make run         # trading loop (defaults to paper mode)
make dashboard   # Streamlit dashboard (separate process)
```

## Docker (Postgres + loop + dashboard)

The containerized stack runs three services via Docker Compose — **`db`** (PostgreSQL), **`algo`** (trading loop), and **`dashboard`** (Streamlit) — all sharing Postgres (a better cross-process channel than the SQLite file).

```bash
cp .env.example .env       # fill Kotak creds + params; POSTGRES_* have working defaults
mkdir -p scrip_cache       # drop nse_fo.csv / bse_fo.csv here for paper mode
make docker-up             # build + start db, algo, dashboard  (dashboard on http://localhost:8501)
make docker-logs           # tail algo + dashboard
make docker-down           # stop
```

- The database is selected by `ALGO_DATABASE_URL`: Compose sets it to the Postgres service automatically; locally it's unset, so the app falls back to SQLite (`ALGO_DB_PATH`). The same code and schema run on both.
- To bake the Kotak SDK into the image (for live mode), build with `INSTALL_BROKER=1` (env var or `--build-arg`).
- Postgres data persists in the `pgdata` volume; `make docker-down` keeps it (`docker compose down -v` wipes it).

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

## Quality

```bash
make check       # ruff + mypy + pytest
```

## Go-live checklist

1. `make check` is green; unit + paper-mode integration behavior validated.
2. Exact strategy parameters confirmed and set in `.env` (see the `ALGO_*` placeholders).
3. Credentials filled (consumer key/secret, PAN or mobile, password, MPIN); pre-market login, scrip-master download, and startup reconciliation verified in **paper** mode.
4. Kill-switch (daily-loss cap) and the independent square-off timer validated in paper mode across multiple sessions.
5. Set `ALGO_MODE=live` **and** `ALGO_CONFIRM_LIVE=YES`, start with a **small fixed lot size** and a **conservative daily-loss cap**, and monitor via the dashboard.
6. Rollback = set `ALGO_MODE=paper` and/or trigger the kill-switch / manual flatten from the dashboard.
