## ADDED Requirements

### Requirement: Long-running orchestration pipeline
The system SHALL run as a long-running service that wires the pipeline — market-data feed → candle builder → strategy → risk checks → execution → position tracking — and SHALL support graceful start and stop. The trading loop MUST run as its own process, separate from the dashboard.

#### Scenario: Pipeline wiring on start
- **WHEN** the orchestrator is started
- **THEN** it authenticates, subscribes to feeds, and connects feed, strategy, risk, and execution components into a running pipeline

#### Scenario: Graceful stop
- **WHEN** a stop command is issued
- **THEN** the orchestrator stops accepting new signals, completes in-flight order handling, and shuts down cleanly

### Requirement: Market-hours and daily lifecycle scheduling
The system SHALL gate trading to market hours, trigger pre-market login, and drive the end-of-day square-off and logout on a schedule. Strategy evaluation MUST NOT place entries outside configured trading hours.

#### Scenario: Outside market hours
- **WHEN** a candle closes outside configured trading hours
- **THEN** the system does not place new entry orders

#### Scenario: End-of-day sequence
- **WHEN** the end-of-day time is reached
- **THEN** the system ensures positions are squared off and the broker session is logged out

### Requirement: Paper and live mode gating
The system SHALL support a `MODE` of `paper` or `live`. In `paper` mode, order requests SHALL be routed to a simulated fill engine that mirrors the live tracking path without contacting the broker's order API. `paper` SHALL be the default, and `live` mode SHALL require explicit configuration plus a startup confirmation before real orders are armed.

#### Scenario: Default paper mode
- **WHEN** the service starts without an explicit live-mode configuration
- **THEN** it runs in paper mode and no real orders are sent to the broker

#### Scenario: Arming live mode
- **WHEN** the service is configured for live mode and the startup confirmation is satisfied
- **THEN** real orders are armed and routed to the broker's order API

### Requirement: Startup reconciliation
On every start, before placing any new orders, the system SHALL pull the broker's current positions and order reports and reconcile them with local persisted state.

#### Scenario: Reconcile before trading
- **WHEN** the orchestrator starts
- **THEN** it fetches live positions and orders, reconciles them against the local datastore, and only then allows new order placement
