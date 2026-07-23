# trading-api Specification

## Purpose
TBD - created by archiving change add-nextjs-fastapi-webapp. Update Purpose after archive.
## Requirements
### Requirement: Read models over the shared database
The API SHALL expose read-only endpoints returning the algo state (mode, live-armed, kill-switch/algo state), today's P&L summary, positions, orders, trades, and the option chain (per-strike OI/LTP + CE-vs-PE aggregate), sourced from the shared database via the existing `StateBridge`/`Repository`/`reporting` code. It SHALL hold no broker session.

#### Scenario: State endpoint
- **WHEN** a client GETs the state endpoint
- **THEN** the response includes the trading mode, live-armed flag, and algo state (RUNNING/HALTED/IDLE)

#### Scenario: P&L endpoint
- **WHEN** a client GETs the P&L endpoint
- **THEN** the response includes today's realized P&L and the per-symbol breakdown from the fills summary

#### Scenario: Option-chain endpoint
- **WHEN** a client GETs the chain endpoint
- **THEN** the response includes the latest per-strike CE/PE OI and LTP and the aggregate CE-vs-PE OI with the selected side

### Requirement: Control endpoints (no order path)
The API SHALL provide start, stop, and flatten control endpoints that enqueue commands into the existing control-command table for the trading loop to consume. The API SHALL NOT place, modify, or cancel broker orders directly.

#### Scenario: Stop control
- **WHEN** a client POSTs the stop control
- **THEN** a stop command is written to the control-command table and the trading loop halts on its next control check

#### Scenario: No direct order placement
- **WHEN** any control endpoint is called
- **THEN** the API writes only a control command and never calls the broker order API

### Requirement: Config read and edit
The API SHALL expose the strategy/runtime configuration for reading and SHALL allow editing the tunable parameters (e.g. lots, per-underlying weekdays, targets, strike window). Edits SHALL be validated and persisted so the running loop can pick them up.

#### Scenario: Read config
- **WHEN** a client GETs the config endpoint
- **THEN** the current tunable parameters are returned

#### Scenario: Edit config with validation
- **WHEN** a client submits a valid config change
- **THEN** it is validated, persisted, and reflected on the next read; an invalid change is rejected with an error

### Requirement: Live update stream
The API SHALL provide a live stream (Server-Sent Events) that pushes updated state/P&L/chain to connected clients on a short interval, so the UI updates without manual refresh.

#### Scenario: Stream pushes updates
- **WHEN** a client subscribes to the live stream
- **THEN** it receives periodic messages containing the latest state/P&L (and chain when in OI mode)

### Requirement: Single-user authentication
The API SHALL require authentication for all non-login endpoints using a single configured operator credential, issuing a token/session on successful login and rejecting unauthenticated requests.

#### Scenario: Login issues a token
- **WHEN** the operator submits the correct configured credential to the login endpoint
- **THEN** a token/session is returned for use on subsequent requests

#### Scenario: Unauthenticated request rejected
- **WHEN** a protected endpoint is called without a valid token
- **THEN** the API responds 401 Unauthorized

