# web-monorepo Specification

## Purpose
TBD - created by archiving change add-nextjs-fastapi-webapp. Update Purpose after archive.
## Requirements
### Requirement: Turborepo workspace layout
The repository SHALL be organized as a Turborepo monorepo with pnpm workspaces containing `apps/web` (Next.js) and `apps/api` (FastAPI), while the existing `src/algo_trading` package remains in place as the shared trading engine that the API imports.

#### Scenario: Workspaces resolve
- **WHEN** `pnpm install` is run at the repo root
- **THEN** `apps/web` and `apps/api` are recognized as workspaces and their dependencies install

#### Scenario: Engine unchanged
- **WHEN** the API is built
- **THEN** it imports `algo_trading` from the existing `src/` package without modifying the engine's logic

### Requirement: Turbo task pipeline
The monorepo SHALL define a `turbo.json` pipeline exposing `dev`, `build`, `lint`, and `typecheck` tasks that run across the workspaces, including the Python `apps/api` (via package.json script shims that invoke the Python toolchain).

#### Scenario: One command runs everything
- **WHEN** `turbo run lint` (or `dev`/`build`/`typecheck`) is invoked at the root
- **THEN** the corresponding task runs for both `apps/web` and `apps/api`

#### Scenario: Python app orchestrated
- **WHEN** `turbo run dev` starts
- **THEN** `apps/api` runs its FastAPI dev server (uvicorn) and `apps/web` runs the Next.js dev server

