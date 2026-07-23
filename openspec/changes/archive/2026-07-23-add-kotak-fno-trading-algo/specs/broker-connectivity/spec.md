## ADDED Requirements

### Requirement: Kotak Neo authentication
The system SHALL authenticate to the Kotak Neo API using the password flow, obtaining a view token via `login` (with a PAN or mobile identifier plus password) and a trade token via `session_2fa` (using the MPIN). Orders MUST NOT be placed before a valid trade token is held. The SDK login calls SHALL be isolated so the flow can be adapted to SDK-version differences or switched to a TOTP variant.

#### Scenario: Successful login
- **WHEN** the service starts with valid consumer key/secret, a login identifier (PAN or mobile), password, and MPIN
- **THEN** the system performs `login` then `session_2fa` and holds a valid trade token enabling order placement

#### Scenario: Missing or invalid credentials
- **WHEN** any required credential (consumer key/secret, login identifier, password, or MPIN) is missing or authentication fails
- **THEN** the system SHALL NOT arm order placement, SHALL log a redacted error, and SHALL surface an unauthenticated state to the orchestrator

### Requirement: Daily session lifecycle and re-authentication
The system SHALL treat Kotak Neo sessions as daily and SHALL re-authenticate before market open each trading day. It SHALL detect mid-session token expiry (authentication errors) and re-authenticate without losing in-memory trading state.

#### Scenario: Pre-market re-login
- **WHEN** a new trading day begins and the pre-market login window is reached
- **THEN** the system obtains a fresh trade token before the strategy loop is allowed to place orders

#### Scenario: Mid-session token expiry
- **WHEN** an API call fails due to an expired or invalid session token during the trading day
- **THEN** the system re-authenticates and retries the operation while preserving current positions and order state

### Requirement: Broker client wrapper
The system SHALL expose a single client wrapper that is the only component importing the Kotak SDK, and SHALL provide typed methods for placing, modifying, and cancelling orders and for reading positions, limits, and order/trade reports. The wrapper SHALL convert between typed domain values and the SDK's string parameters and SHALL enforce an order-submission throttle of at most 10 orders per second per exchange.

#### Scenario: Order submission throttling
- **WHEN** more than 10 order operations are requested within one second for a single exchange
- **THEN** the wrapper queues and paces submissions so the per-exchange rate never exceeds the regulatory limit

#### Scenario: Typed to SDK conversion
- **WHEN** a domain order request with numeric quantity and price is submitted
- **THEN** the wrapper converts them to the SDK's required string form and selects the correct exchange segment (`nse_fo` for NIFTY, `bse_fo` for SENSEX)

### Requirement: Market-data and order websocket feeds
The system SHALL consume the Kotak Neo quote websocket for live LTP/quotes and the order/trade websocket for order and trade updates, normalizing both into internal event models. It SHALL maintain a heartbeat, automatically reconnect with backoff on disconnect, and resubscribe to all active instrument tokens and the order feed on reconnect.

#### Scenario: Reconnect and resubscribe
- **WHEN** either websocket disconnects
- **THEN** the system reconnects with exponential backoff and resubscribes to all previously subscribed instrument tokens and the order feed

#### Scenario: Stale feed detection
- **WHEN** no ticks are received for a configured number of seconds during market hours
- **THEN** the system flags the feed as stale and signals a halt condition to the orchestrator
