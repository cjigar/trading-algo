## ADDED Requirements

### Requirement: Scrip-master ingestion
The system SHALL download the Kotak Neo scrip-master files for the `nse_fo` and `bse_fo` segments each trading day, parse them into an indexed instrument table, and cache the result for the day. The table MUST expose, per instrument, the trading symbol, instrument token, expiry, strike, option type (CE/PE), and lot size.

#### Scenario: Daily scrip-master refresh
- **WHEN** the service starts on a new trading day
- **THEN** the system downloads and parses the current `nse_fo` and `bse_fo` scrip-master files and builds a searchable instrument table

#### Scenario: Download or parse failure
- **WHEN** the scrip-master download or parse fails
- **THEN** the system SHALL NOT begin trading, SHALL log the error, and SHALL surface an error state rather than trading against stale or missing instrument data

### Requirement: Weekly option contract resolution
The system SHALL resolve the correct current-week option contract for a given underlying (NIFTY or SENSEX), spot price, side, and strike-selection rule (ATM or a configured OTM/ITM offset), returning the trading symbol, instrument token, and lot size. It SHALL apply the correct weekly-expiry calendar for each underlying.

#### Scenario: Resolve ATM weekly option
- **WHEN** the strategy requests an ATM current-week CE for NIFTY at a given spot
- **THEN** the resolver returns the nearest-strike current-week CE contract's trading symbol, token, and lot size from the `nse_fo` table

#### Scenario: No matching contract
- **WHEN** no contract matches the requested underlying, expiry, strike, and option type
- **THEN** the resolver returns no contract and the signal is rejected without an order being placed
