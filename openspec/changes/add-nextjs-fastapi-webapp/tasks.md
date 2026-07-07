## 1. Monorepo scaffolding

- [x] 1.1 Add root `package.json` (private, packageManager pnpm), `pnpm-workspace.yaml` (`apps/*`), and `.nvmrc` (Node 20)
- [x] 1.2 Add `turbo.json` with `dev`/`build`/`lint`/`typecheck` pipelines; shared `tsconfig.base.json`
- [x] 1.3 Update `.gitignore`/`.dockerignore` for `node_modules`, `.next`, `.turbo`; keep `src/algo_trading` at root as the shared engine
- [x] 1.4 Verify `pnpm install` + `turbo run build` resolve both workspaces (stub scripts first)

## 2. FastAPI backend (apps/api)

- [x] 2.1 Scaffold `apps/api` (FastAPI app, uvicorn) with a `package.json` shim whose `dev`/`lint`/`typecheck` invoke uvicorn/ruff/mypy; install the root `algo_trading` package editable
- [x] 2.2 Settings/deps: `WEB_AUTH_PASSWORD`, `WEB_AUTH_SECRET`, `WEB_CORS_ORIGINS`; CORS middleware for the web origin
- [x] 2.3 Auth: `POST /api/login` (single configured credential -> signed JWT) + a bearer-token dependency guarding all other routes; `POST /api/logout`
- [x] 2.4 Read routers over `StateBridge`/`reporting`/`Repository`: `GET /api/state`, `/api/pnl`, `/api/positions`, `/api/orders`, `/api/trades`, `/api/chain` (Pydantic response models mirroring DashboardState/summaries)
- [x] 2.5 Control router: `POST /api/control/{start|stop|flatten}` writing to the control-command table via `StateBridge` (no broker/order imports in apps/api)
- [x] 2.6 Config router: `GET /api/config` (effective tunables) + `PUT /api/config` (validated whitelist -> overrides store the loop reads; never secrets or live-arming)
- [x] 2.7 Live stream: `GET /api/stream` SSE emitting state/P&L (and chain in OI mode) every N seconds; disable proxy buffering
- [x] 2.8 Backend tests (pytest): auth (401/token), read endpoints shape, control writes a command, config validation accept/reject, SSE emits an event

## 3. Type-safe contract

- [x] 3.1 Ensure FastAPI OpenAPI schema is complete/typed; add a `gen:types` script running `openapi-typescript` -> `apps/web/lib/api-types.ts`
- [x] 3.2 Typed `api()` fetch helper in `apps/web` using the generated types (base URL from env, attaches auth)

## 4. Next.js web app (apps/web)

- [x] 4.1 Scaffold `apps/web` (Next.js App Router, TypeScript, Tailwind); base layout with the always-visible paper/live + kill-switch banner
- [x] 4.2 Auth: login page, middleware guarding dashboard routes, token stored in an httpOnly cookie; unauthenticated -> redirect to login
- [x] 4.3 Monitoring views (tabs): P&L (realized + per-symbol), Positions, Trades, Orders, Option Chain (per-strike OI/LTP + CE/PE aggregate + selected side)
- [x] 4.4 Controls: Start / Stop / Flatten buttons calling the control API, with confirmation for destructive actions; reflect resulting state
- [x] 4.5 Config editor: view/edit tunable parameters (lots, per-underlying weekdays, targets, strike window); save via `PUT /api/config`; show validation errors
- [x] 4.6 Live updates: `EventSource` hook subscribing to `/api/stream`; update views in place without refresh
- [x] 4.7 Frontend checks: `pnpm lint` + `typecheck` pass; a smoke test of the api() helper / a component render test

## 5. Docker & orchestration

- [x] 5.1 Dockerfiles for `apps/api` (uvicorn + algo_trading) and `apps/web` (Next.js build/serve)
- [x] 5.2 Compose: add `api` (reads the same Postgres, no broker) and `web` (proxied to api); keep `db` + trading loop; wire env
- [x] 5.3 `turbo run dev` runs web + api locally against the DB; document setup in README

## 6. Parity, validation & cutover

- [x] 6.1 Verify web app parity — ✅ verified in browser: login, LIVE banner, P&L (real fills), option chain, config editor, controls all render against the shared Postgres
- [x] 6.2 Validate single-user auth — ✅ login required (redirect), 401 without token (tested), config editor exposes only whitelisted tunables (no secrets/mode/order path)
- [~] 6.3 Confirm open questions with the operator (pnpm, uv vs pip, UI kit, editable-config whitelist, cookie vs header)
- [x] 6.4 Run `openspec validate add-nextjs-fastapi-webapp`, all tests, lint/type across the monorepo
- [x] 6.5 Retired the Streamlit dashboard — removed the compose service, app.py/run_dashboard.py, the streamlit dep + algo-dashboard script + make target; kept StateBridge (the web API reuses it)
