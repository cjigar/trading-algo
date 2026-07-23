# option-chain-feed Specification

## Purpose
TBD - created by archiving change add-oi-option-selling-strategy. Update Purpose after archive.
## Requirements
### Requirement: ATM resolution from live NIFTY spot
The system SHALL derive the at-the-money (ATM) strike continuously from the live NIFTY index spot price by rounding to the nearest strike step (50 points for NIFTY), and SHALL update ATM as the spot moves.

#### Scenario: ATM tracks spot
- **WHEN** the NIFTY spot LTP is 23,062
- **THEN** the resolved ATM strike is 23,050 (nearest 50-point strike)

#### Scenario: No spot yet
- **WHEN** no NIFTY spot tick has been received
- **THEN** the chain window is not resolved and no option-chain subscription is attempted

### Requirement: Option-chain window subscription
The system SHALL resolve the CE and PE contracts for strikes from **ATM −5 to ATM +5** (inclusive) for the current weekly expiry, and SHALL subscribe to all of them plus the NIFTY index over the websocket. When ATM shifts such that the window changes, the system SHALL subscribe to the newly-included contracts and unsubscribe (or stop persisting) those that left the window.

#### Scenario: Full window subscribed
- **WHEN** ATM is resolved as 23,050
- **THEN** the system subscribes to CE and PE contracts for strikes 22,800 … 23,300 (ATM ±5 × 50) for the current-week expiry

#### Scenario: Window shifts with ATM
- **WHEN** ATM moves from 23,050 to 23,100
- **THEN** the system subscribes the newly-included strikes (e.g. 23,350 CE/PE) and stops tracking those that fell out of the ±5 window

### Requirement: Persist option-chain snapshots
The system SHALL persist each received option-chain update — instrument, strike, option type, **open interest**, LTP, and volume with a timestamp — into the database as an append-only time series, using batched writes to sustain the throughput of 22 contracts streaming continuously.

#### Scenario: OI/LTP persisted
- **WHEN** an option-chain tick with OI and LTP is received for a subscribed strike
- **THEN** a snapshot row (strike, CE/PE, OI, LTP, volume, timestamp) is written to the database

#### Scenario: Missing OI in a message
- **WHEN** a quote message lacks an OI field
- **THEN** the LTP/volume are still recorded and OI is stored as null/unchanged rather than dropping the update

### Requirement: Per-option session VWAP
The system SHALL compute a session-anchored VWAP for each subscribed option from its own streamed ticks, reset at the start of each session, and expose the current VWAP per option for the strategy's exit rule.

#### Scenario: VWAP available for exit checks
- **WHEN** an option has received ticks during the session
- **THEN** its current VWAP is available and updates as new ticks arrive

### Requirement: Latest chain snapshot for evaluation
The system SHALL expose the latest OI and LTP per strike/side (the current chain state) so the strategy can aggregate OI across the window without re-reading the full time series.

#### Scenario: Current chain state
- **WHEN** the strategy requests the current chain
- **THEN** it receives the most recent OI and LTP for each CE and PE strike in the ATM ±5 window

