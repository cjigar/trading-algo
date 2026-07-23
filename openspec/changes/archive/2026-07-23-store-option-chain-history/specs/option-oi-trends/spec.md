## ADDED Requirements

### Requirement: Rolling-window OI trend per strike and side

The system SHALL compute, for each strike and each side (CE and PE), an open-interest trend over a configurable set of look-back windows defaulting to 1, 3, 5 and 15 minutes. For each window the trend SHALL be derived by comparing the strike's current OI against its OI at approximately `now − window`, using the point-in-time snapshot at or just before that target time as the anchor. Each window's result SHALL include a direction of **Up**, **Down**, or **Flat** and the signed OI delta over the window.

#### Scenario: OI rising over a window
- **WHEN** a strike's current OI exceeds its anchor OI for the window by more than the flat threshold
- **THEN** that window's trend direction SHALL be Up and the signed delta SHALL be positive

#### Scenario: OI falling over a window
- **WHEN** a strike's current OI is below its anchor OI for the window by more than the flat threshold
- **THEN** that window's trend direction SHALL be Down and the signed delta SHALL be negative

#### Scenario: OI change within threshold is flat
- **WHEN** the absolute difference between current OI and anchor OI is within the configured flat threshold
- **THEN** that window's trend direction SHALL be Flat

#### Scenario: All four default windows computed
- **WHEN** a strike is summarized
- **THEN** trend results SHALL be produced for each configured window (1, 3, 5, 15 minutes by default) for both CE and PE

### Requirement: Graceful handling of missing history

When insufficient snapshot history exists to anchor a window (for example early in the session, or a strike newly entered the chain window), the system SHALL report that window's trend as unavailable rather than fabricating a direction or diffing against an incorrect anchor.

#### Scenario: Window longer than available history
- **WHEN** the earliest snapshot for a token is more recent than `now − window`
- **THEN** that window's trend SHALL be reported as unavailable (neither Up nor Down) and SHALL NOT be treated as Flat-by-default

#### Scenario: No snapshots for a strike yet
- **WHEN** a strike has no persisted snapshots
- **THEN** all its window trends SHALL be reported as unavailable

### Requirement: Trends exposed through the chain API and SSE stream

The per-strike, per-window trends SHALL be included in the option-chain API response and in the server-sent-events stream payload, alongside the existing OI, change-in-OI and LTP fields, without requiring a separate endpoint. Both the polled chain response and the streamed chain payload SHALL carry identical trend fields so the dashboard sees consistent data from either source.

#### Scenario: Chain response includes trends
- **WHEN** the option-chain endpoint is queried for an underlying
- **THEN** each returned strike SHALL include CE and PE trend direction and delta for every configured window

#### Scenario: Stream payload includes trends
- **WHEN** the SSE stream emits a chain payload
- **THEN** each strike in that payload SHALL include the same CE and PE per-window trend fields as the polled response

### Requirement: Dashboard displays the full live chain with OI trends

The dashboard option-chain view SHALL display the full chain window — every windowed strike with CE and PE OI, change-in-OI, and LTP — with the latest values, and SHALL additionally show compact per-window OI trend indicators (Up/Down/Flat) for 1, 3, 5 and 15 minutes on both sides. The view SHALL update live as new data arrives over the SSE stream, and unavailable windows SHALL render as a neutral placeholder rather than a false direction.

#### Scenario: Full chain rendered with latest values
- **WHEN** the option-chain view loads for an underlying
- **THEN** it SHALL show each strike in the window with current CE/PE OI, change-in-OI and LTP, and the ATM strike marked

#### Scenario: Trend arrows update live
- **WHEN** new chain data arrives over the SSE stream
- **THEN** each strike's 1/3/5/15-minute OI trend indicators SHALL update to reflect the latest computed directions

#### Scenario: Unavailable window shows neutral placeholder
- **WHEN** a window's trend is unavailable for a strike
- **THEN** the view SHALL render a neutral placeholder for that cell rather than an Up or Down arrow

### Requirement: Configurable windows and flat threshold

The look-back window set and the flat-vs-directional threshold SHALL be configurable via application settings, so operators can tune sensitivity and horizons without code changes.

#### Scenario: Custom window set honored
- **WHEN** the configured window list differs from the default
- **THEN** trend computation and the API/stream/display SHALL use exactly the configured windows

#### Scenario: Flat threshold honored
- **WHEN** the flat threshold is changed
- **THEN** the Up/Down/Flat classification SHALL use the new threshold
