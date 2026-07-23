# option-chain-history Specification

## Purpose
TBD - created by archiving change store-option-chain-history. Update Purpose after archive.
## Requirements
### Requirement: Per-strike option chain snapshots persisted from the websocket feed

The system SHALL persist a per-strike option-chain time series to Postgres, sourced from the live websocket quote feed. Each persisted snapshot SHALL record, for a single instrument token (a specific strike and side), at minimum: the underlying, strike, option type (CE/PE), instrument token, open interest (OI), last traded price (LTP), traded volume, the trading day, and a UTC timestamp. Snapshots SHALL be append-only (no in-place updates), forming a time series that can be queried for both the latest state and historical points.

#### Scenario: Websocket quote produces a snapshot row
- **WHEN** an option quote arrives over the websocket for a strike currently in the chain window
- **THEN** a snapshot capturing that strike's OI, LTP, volume, timestamp, underlying, strike, option type and instrument token SHALL be written to the `option_chain_snapshots` store

#### Scenario: Snapshots are append-only history
- **WHEN** a second quote for the same strike arrives later with a different OI
- **THEN** a new snapshot row SHALL be appended (the earlier row is retained), so the token accumulates a time-ordered history rather than being overwritten

### Requirement: Periodic snapshot cadence

The system SHALL write snapshots on a periodic cadence rather than on every individual tick. The latest quote per strike SHALL be retained in memory between writes and flushed on a fixed interval and/or buffer threshold, and a per-token minimum write interval SHALL suppress redundant sub-interval writes. This SHALL bound database write volume and SHALL produce regularly spaced anchor points suitable for time-windowed trend computation.

#### Scenario: Bursty ticks collapse to one periodic write
- **WHEN** a strike ticks many times within the configured minimum write interval
- **THEN** at most one snapshot for that strike SHALL be persisted for that interval, carrying its latest OI/LTP/volume

#### Scenario: Regular anchor spacing
- **WHEN** snapshots for a strike are queried over a trading session
- **THEN** persisted rows SHALL be spaced no finer than the configured minimum interval, giving trend windows well-spaced anchor points

### Requirement: Change-in-OI against the day-open baseline

The system SHALL expose, per strike, a change-in-OI value computed as the current OI minus the OI of that strike's first snapshot of the trading day (the day-open baseline). This preserves the existing full-day change-in-OI reading alongside the new rolling-window trends.

#### Scenario: Day-open change-in-OI
- **WHEN** the current chain is summarized for a strike
- **THEN** its change-in-OI SHALL equal current OI minus the OI of that token's earliest snapshot for the trading day
- **AND** WHEN no earlier snapshot exists for that token today, the change-in-OI SHALL be zero

### Requirement: Queryable snapshot store with efficient windowed lookup

The snapshot store SHALL support efficient retrieval of (a) the latest snapshot per token for a trading day and underlying, and (b) the snapshot at or just before an arbitrary target timestamp for a token. A composite index over instrument token, trading day and timestamp SHALL exist so these lookups remain fast as the day's row count grows.

#### Scenario: Latest chain state read
- **WHEN** the current chain is requested for an underlying and trading day
- **THEN** the store SHALL return the most recent snapshot for each token in the window

#### Scenario: Point-in-time OI read
- **WHEN** the OI of a token as of a past timestamp is requested
- **THEN** the store SHALL return the OI from that token's latest snapshot at or before the given timestamp, or indicate no anchor exists if none precedes it

### Requirement: Snapshot retention

The system SHALL provide a retention mechanism that prunes snapshots older than a configured number of trading days, so the append-only history does not grow without bound.

#### Scenario: Old snapshots pruned
- **WHEN** retention runs with a configured age limit
- **THEN** snapshots whose trading day is older than the limit SHALL be removed and current-day data SHALL be retained

