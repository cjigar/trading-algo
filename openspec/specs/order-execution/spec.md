# order-execution Specification

## Purpose
TBD - created by archiving change add-kotak-fno-trading-algo. Update Purpose after archive.
## Requirements
### Requirement: Signal-to-order translation
The system SHALL translate an entry signal into a concrete option order request by resolving the target contract, applying fixed lot sizing, and setting a limit-price policy. The resulting order request MUST carry a unique client tag for idempotency.

#### Scenario: Signal produces an order request
- **WHEN** a validated entry signal is received and risk checks pass
- **THEN** the system produces an order request for the resolved option contract with quantity equal to the configured lot multiple and a unique client tag

### Requirement: Idempotent order lifecycle
The system SHALL manage each order as a state machine (pending → acknowledged → partially filled → filled / rejected / cancelled) driven by the order feed. It SHALL persist the client tag before submission, and on restart or reconnect SHALL reconcile against the broker's order and position reports rather than resubmitting. Orders exceeding the exchange freeze quantity SHALL be split into multiple legs, and submissions SHALL respect the order-rate throttle.

#### Scenario: Rejection handling
- **WHEN** the broker rejects an order (e.g. margin, price band, or freeze-quantity)
- **THEN** the system records the rejection reason, does not leave an unintended naked leg, and decides retry-or-abort per the rejection class without blindly resubmitting

#### Scenario: Restart reconciliation
- **WHEN** the service restarts while orders or positions may be open
- **THEN** the system reconciles local state against the broker's order and position reports before placing any new orders

#### Scenario: Freeze-quantity splitting
- **WHEN** an order quantity exceeds the exchange freeze-quantity limit
- **THEN** the system splits it into multiple compliant legs submitted within the rate throttle

### Requirement: Position and P&L tracking
The system SHALL track live positions and compute realized and unrealized P&L from fills and current LTP, updating continuously as fills and ticks arrive.

#### Scenario: Unrealized P&L update
- **WHEN** a new LTP tick arrives for a held option position
- **THEN** the system recomputes that position's unrealized P&L and the aggregate day P&L

### Requirement: Exit management
The system SHALL manage exits for each open position with a fixed profit target that transitions to a trailing stop once reached, a hard stop-loss, and a time-based square-off. The time-based square-off SHALL run on an independent timer and execute even if the strategy or data feed is degraded, verifying flat status against the broker's positions.

#### Scenario: Target then trail
- **WHEN** an open position reaches its configured profit target
- **THEN** the system activates a trailing stop that follows favorable price movement and exits when the trail is hit

#### Scenario: Stop-loss exit
- **WHEN** an open position's loss reaches the configured stop-loss level
- **THEN** the system submits an exit order for that position

#### Scenario: Time-based square-off
- **WHEN** the configured square-off time is reached
- **THEN** the independent timer squares off all open positions and verifies flat status against the broker's positions, regardless of strategy or feed health

