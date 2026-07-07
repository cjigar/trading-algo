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
| `broker/` | Kotak Neo client wrapper, auth/session, market-data & order websockets |
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
