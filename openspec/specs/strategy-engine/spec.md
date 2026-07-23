# strategy-engine Specification

## Purpose
TBD - created by archiving change add-kotak-fno-trading-algo. Update Purpose after archive.
## Requirements
### Requirement: Clock-aligned candle aggregation
The system SHALL aggregate incoming ticks for each underlying into fixed-interval candles whose boundaries are aligned to the wall clock in the exchange timezone (IST), and SHALL evaluate strategy signals only on candle close to avoid look-ahead and duplicate signals.

#### Scenario: Candle close evaluation
- **WHEN** a candle interval boundary is reached
- **THEN** the system finalizes the candle and evaluates the strategy against the closed candle, not against intra-candle ticks

#### Scenario: Missing or late ticks
- **WHEN** ticks are missing or arrive late around a boundary
- **THEN** the system handles the gap explicitly (carrying forward or marking the candle) without producing a look-ahead signal

### Requirement: VWAP and breakout indicators
The system SHALL compute a session-anchored VWAP and the rolling high/low levels needed to detect a price-action breakout for each underlying, updated as candles close.

#### Scenario: Session-anchored VWAP reset
- **WHEN** a new trading session begins
- **THEN** VWAP accumulation resets and is computed from that session's ticks only

### Requirement: Pluggable strategy signal generation
The system SHALL define a strategy interface so that concrete strategies can be substituted, and SHALL provide a VWAP breakout strategy that emits typed entry signals (underlying, side, and strike-selection intent) when its configured breakout condition relative to VWAP is met. Strategy parameters SHALL be sourced from configuration.

#### Scenario: Breakout entry signal
- **WHEN** a closed candle satisfies the configured VWAP breakout condition on an underlying
- **THEN** the strategy emits an entry signal specifying the underlying, side, and strike-selection intent

#### Scenario: No signal when condition unmet
- **WHEN** a closed candle does not satisfy the breakout condition
- **THEN** the strategy emits no entry signal

