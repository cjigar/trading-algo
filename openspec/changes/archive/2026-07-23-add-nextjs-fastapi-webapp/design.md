## Context

The trading algo (Python `src/algo_trading`) is monitored via a Streamlit dashboard that reads the shared DB through `StateBridge` and issues control commands (it holds no broker session). We are replacing that UI with a **Next.js** app backed by a **FastAPI** service, organized as a **Turborepo** monorepo, keeping the engine untouched. The user's priority is **concise, easy-to-understand code**.

Reuses (no logic rewrite): `config.settings.Settings`, `dashboard.state_bridge.StateBridge` + `DashboardState`, `persistence.repositories.Repository`, `reporting.summarize_fills`/`summarize_chain`, the control-command table, and the paper-first + kill-switch safety model. The trading loop stays its own process.

Constraints: mixed-language monorepo (Node + Python) under Turbo; the API must remain read/control-only (no broker/order path); single-user auth; live updates; keep it small.

## Goals / Non-Goals

**Goals:**
- One repo: `apps/web` (Next.js) + `apps/api` (FastAPI) + existing `src/algo_trading`, orchestrated by Turbo.
- Thin, typed API over the existing bridge/reporting code; a generated TS client so front/back stay in sync.
- Web UI at parity with Streamlit (mode banner, P&L, positions, orders, trades, chain) + controls + config editor + live updates, behind single-user login.
- Docker: `api` + `web` services alongside `db` and the loop.

**Non-Goals:**
- Rewriting or moving the trading engine (`src/algo_trading` stays as-is).
- Multi-user accounts/roles, RBAC (single-user only).
- The API placing/modifying orders or holding a broker session (control commands only).
- Replacing the trading loop or the capture/import tools (unchanged).

## Decisions

### D1: Turborepo with pnpm workspaces; Python app as a shim workspace
Root `pnpm-workspace.yaml` includes `apps/*`. `apps/web` is a normal Node workspace. `apps/api` gets a minimal `package.json` whose `dev`/`build`/`lint`/`typecheck` scripts invoke the Python toolchain (`uvicorn`, `ruff`, `mypy`), so Turbo orchestrates both languages uniformly. `src/algo_trading` stays at repo root and is installed editable into the API's environment. **Alternative:** move the engine into `packages/engine` — rejected for v1 (unnecessary churn; the user chose "wrap current code").

### D2: FastAPI as a thin adapter over StateBridge/reporting
`apps/api/app` exposes routers that call the existing code: a `StateBridge(settings)` for reads and control writes, `summarize_fills`/`summarize_chain` for P&L/chain, `Repository` for lists. Responses are Pydantic models mirroring `DashboardState`/summaries. No business logic is added in the API. **Alternative:** duplicate queries in the API — rejected (drift, more code).

### D3: Read/control-only — never the order path
The API imports only read/bridge modules; it does **not** import the broker/order/session code, and control endpoints write to the control-command table via `StateBridge.send_*`. This preserves the dashboard's safety guarantee (no orders from the UI process). Enforced by keeping broker imports out of `apps/api`.

### D4: SSE for live updates (over websockets)
A single `GET /api/stream` returns an SSE `StreamingResponse` that re-reads state/P&L (and chain in OI mode) every N seconds and emits JSON events. SSE is one-way, trivially proxied, and less code than websockets — matching the "concise" goal. The Next.js client uses `EventSource`. **Alternative:** websockets — deferred; SSE covers server→client push, and controls use normal POSTs.

### D5: Single-user auth via a configured credential + signed token
`POST /api/login` checks a single configured operator password (`WEB_AUTH_PASSWORD`) and returns a short signed JWT (HS256, `WEB_AUTH_SECRET`). A dependency validates the `Authorization: Bearer` token on all other routes. The Next.js middleware guards routes and stores the token (httpOnly cookie). **Alternative:** NextAuth/full session store — over-engineered for one user.

### D6: Config edit via a persisted overrides file the loop reads
Editable parameters (lots, per-underlying weekdays, targets, strike window, etc.) are written by the API to a small overrides store (a JSON row/file layered onto `Settings`), validated with Pydantic. The loop reads settings (which merge overrides) on its next cycle/session. Read returns the effective values. **Alternative:** editing `.env` directly — rejected (requires restart, unsafe parsing); the overrides layer is safer and hot-readable. Secrets are never exposed or editable via the API.

### D7: Type-safe client from OpenAPI
FastAPI auto-generates the OpenAPI schema; a `pnpm` script runs `openapi-typescript` to emit `apps/web/lib/api-types.ts`. The web app's fetch wrapper is typed against it, so endpoints and models stay in sync with minimal hand-written types. **Alternative:** hand-maintain TS types — rejected (drift, more code).

### D8: Next.js App Router, TypeScript, Tailwind; small components
`apps/web` uses the App Router with a route group behind auth. One page with tabbed views (P&L / Orders / Positions / Trades / Option Chain / Config) mirroring the Streamlit layout, a live `EventSource` hook feeding a small client store, and a typed `api()` fetch helper. Tailwind for styling; minimal component library to keep it concise. Mode/kill-switch banner is always visible.

### D9: Docker + Turbo dev
Compose adds `api` (uvicorn, imports `algo_trading`, reads the same Postgres) and `web` (Next.js). Both read-only w.r.t. the broker. `turbo run dev` runs both locally. The Streamlit `dashboard` service is kept until parity, then removed.

## Risks / Trade-offs

- **[Mixed-language monorepo friction]** → keep the Python shim minimal; Turbo just shells into `uvicorn`/`ruff`/`mypy`; document the Python env setup (uv/pip) so `pnpm`+`turbo` and Python coexist.
- **[API drifting from engine reads]** → the API only calls existing bridge/reporting functions; generated TS types keep the client aligned.
- **[Config edit racing the loop]** → overrides are written atomically and read at cycle/session boundaries; validation prevents bad values; only whitelisted tunables are editable (never secrets/mode-arming).
- **[Live-money exposure via UI]** → API is control-only over the DB; arming live still requires the loop's own `ALGO_MODE=live` + confirmation (not settable to "armed" from the UI); auth gates access.
- **[SSE behind proxies]** → disable buffering for the stream route; fall back to short-interval polling if SSE is blocked.
- **[Auth is single-user]** → acceptable for a personal tool; documented as not multi-tenant; token is short-lived and secret-configured.

## Migration Plan

Additive; the Streamlit dashboard keeps working during the build.
1. Scaffold the monorepo (pnpm workspaces, `turbo.json`, `apps/web`, `apps/api` shim); keep `src/algo_trading` in place.
2. Build the FastAPI read models + control + auth + SSE over `StateBridge`/reporting; generate the OpenAPI/TS types.
3. Build the Next.js app (login, tabbed monitoring, controls, config editor, live updates).
4. Add `api` + `web` compose services; validate parity with Streamlit against the same DB.
5. Retire the Streamlit `dashboard` service once parity is confirmed. Rollback = keep/redeploy Streamlit.

## Open Questions

- **Package manager**: pnpm assumed (best Turbo fit) — confirm vs npm/yarn.
- **Python env in `apps/api`**: uv vs pip/venv for the API's Python toolchain (both work; uv is faster).
- **UI kit**: plain Tailwind vs shadcn/ui — default to a minimal set for conciseness; confirm if a component library is wanted.
- **Config overrides scope**: exact whitelist of editable parameters (must exclude secrets and live-arming).
- **Auth transport**: httpOnly cookie vs Authorization header for the Next.js↔API calls (cookie is simpler for SSR; confirm).
