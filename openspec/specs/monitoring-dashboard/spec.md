# monitoring-dashboard Specification

## Purpose
TBD - created by archiving change add-kotak-fno-trading-algo. Update Purpose after archive.
## Requirements
### Requirement: Live monitoring surface
The system SHALL provide a Streamlit dashboard that displays current positions, live realized/unrealized P&L, the order and trade log, the current algo state (RUNNING/HALTED), and a clear paper-vs-live mode indicator. The dashboard SHALL read this state from the shared datastore and MUST NOT hold the broker session itself.

#### Scenario: Display live state
- **WHEN** the dashboard is open while the orchestrator is running
- **THEN** it shows current positions, day P&L, recent orders/trades, algo state, and the paper/live indicator sourced from the shared datastore

#### Scenario: Mode indicator visibility
- **WHEN** the orchestrator is running in live mode
- **THEN** the dashboard prominently indicates live mode so it is not mistaken for paper mode

### Requirement: Control commands
The system SHALL let the operator start and stop the algo and trigger a manual flatten/halt from the dashboard by writing commands to a control channel in the shared datastore that the orchestrator consumes. The dashboard SHALL NOT place broker orders directly.

#### Scenario: Stop from dashboard
- **WHEN** the operator clicks Stop
- **THEN** the dashboard writes a stop command to the control channel and the orchestrator halts new entries and stops on its next control check

#### Scenario: Manual flatten from dashboard
- **WHEN** the operator triggers a manual flatten/halt
- **THEN** the dashboard records the command and the orchestrator enters HALTED and squares off open positions

