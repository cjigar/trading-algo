## Why

The trading algo is monitored and controlled through a Streamlit dashboard. Streamlit is limited: it re-runs its whole script per interaction, is awkward for real-time push and multi-view layouts, and doesn't scale to a polished control panel. We want a proper web app — a **Next.js** frontend backed by a **FastAPI** service — that gives live monitoring, controls, and config editing with a clean, maintainable codebase. Organizing it as a **Turborepo** monorepo (`apps/web` + `apps/api` alongside the existing `algo_trading` engine) keeps one repo, shared tooling, and concise, easy-to-follow code.

## What Changes

- **Turborepo monorepo**: introduce `turbo.json` + pnpm workspaces with `apps/web` (Next.js) and `apps/api` (FastAPI). The existing `src/algo_trading` package stays in place and is imported by the API as the trading engine (no logic rewrite).
- **FastAPI backend** (`apps/api`): a thin, typed HTTP + streaming layer over the existing `StateBridge`/`Repository`/`reporting` code. It reads state from the shared DB and issues control commands — it holds **no broker session and places no orders** (same safety model as the Streamlit dashboard). Endpoints for state, P&L, positions, orders, trades, option chain, config (read/edit), and start/stop/flatten controls, plus a **live stream** (SSE) and **single-user auth**.
- **Next.js web app** (`apps/web`): App Router + TypeScript + Tailwind. Views for the mode/kill-switch banner, P&L, positions, orders, trades, and option chain; **start/stop/flatten controls**; a **config editor** for strategy parameters; and **live updates** pushed from the API (no manual refresh). Behind a **single-user login**.
- **Type-safe contract**: the frontend consumes types generated from FastAPI's OpenAPI schema, so the client and server stay in sync with minimal boilerplate.
- **Docker**: add `api` and `web` services to the compose stack (alongside `db` and the trading loop); the Streamlit `dashboard` service can be retired once parity is reached.

## Capabilities

### New Capabilities
- `web-monorepo`: the Turborepo structure — pnpm workspaces, `turbo.json` task pipeline (dev/build/lint/typecheck), shared TS config, and the `apps/api` Python shim so Turbo orchestrates both languages; keeps `src/algo_trading` as the shared engine.
- `trading-api`: the FastAPI service exposing read models (state, P&L, positions, orders, trades, chain, config), config edits, start/stop/flatten controls, an SSE live stream, and single-user token auth — reusing the existing engine and never touching the broker/order path.
- `web-dashboard-ui`: the Next.js application — login, monitoring views, controls, config editor, and live updates — consuming the typed API.

### Modified Capabilities
<!-- The Streamlit `monitoring-dashboard` capability (from add-kotak-fno-trading-algo) is superseded
     but that change is unarchived, so there is no published spec to delta. Retirement noted in Impact. -->

## Impact

- **New code**: `turbo.json`, root `package.json`/`pnpm-workspace.yaml`, `apps/web/*` (Next.js), `apps/api/*` (FastAPI app importing `algo_trading`). No changes to the trading engine's logic.
- **Reuses**: `config.settings`, `dashboard.state_bridge.StateBridge`, `persistence.repositories.Repository`, `reporting.summarize_fills`/`summarize_chain` — the API is a thin adapter over these.
- **Supersedes**: the Streamlit `dashboard` service/app once the web app reaches parity (kept during transition).
- **Deps/tooling**: Node 20 + pnpm + Next.js/React/Tailwind for `apps/web`; FastAPI + uvicorn (+ the existing Python deps) for `apps/api`; Turbo for orchestration. New env: a single-user credential/token and the API base URL for the web app.
- **Safety**: unchanged — the API is read/control-only over the shared DB, holds no broker session, and places no orders; controls go through the existing control-command table. Single-user auth gates access.
