## ADDED Requirements

### Requirement: Pre-trade risk checks and lot sizing
The system SHALL apply fixed lot sizing and enforce configured limits (maximum concurrent positions and maximum trades per day) as pre-trade checks. An entry signal that would breach a limit MUST be rejected before any order is placed.

#### Scenario: Position limit reached
- **WHEN** an entry signal arrives while the maximum concurrent positions are already open
- **THEN** the system rejects the signal and does not place an order

#### Scenario: Fixed lot sizing applied
- **WHEN** an entry signal passes risk checks
- **THEN** the order quantity is set to the configured number of lots times the contract lot size

### Requirement: Persistent daily-loss-cap kill-switch
The system SHALL continuously evaluate realized plus unrealized day P&L against a configured daily-loss cap. When the cap is breached, it SHALL enter a HALTED state that blocks all new entries and MAY flatten open positions. The HALTED state MUST be persisted so it survives a process restart and MUST NOT auto-reset during the same trading day.

#### Scenario: Daily loss cap breached
- **WHEN** realized plus unrealized day P&L reaches or exceeds the configured daily-loss cap
- **THEN** the system enters HALTED, blocks new entries, records a kill-switch audit event, and (per configuration) flattens open positions

#### Scenario: Halt persists across restart
- **WHEN** the service restarts on the same trading day after the kill-switch has fired
- **THEN** the system reads the persisted HALTED state and refuses to place new entries for the remainder of that day

### Requirement: Authoritative algo state
The system SHALL maintain an authoritative algo state (e.g. RUNNING, HALTED) that all entry decisions consult, and SHALL allow a manual halt to be triggered from the dashboard.

#### Scenario: Manual halt
- **WHEN** a manual halt command is issued from the dashboard
- **THEN** the system enters HALTED, blocks new entries, and records the manual halt as an audit event
