## ADDED Requirements

### Requirement: Authenticated single-user access
The web app SHALL present a login screen and require the operator to authenticate before any dashboard view is shown, storing the session/token for API calls and redirecting unauthenticated users to login.

#### Scenario: Login required
- **WHEN** an unauthenticated user opens any dashboard route
- **THEN** they are redirected to the login screen

#### Scenario: Successful login
- **WHEN** the operator logs in with the correct credential
- **THEN** they reach the dashboard and subsequent API calls carry the session/token

### Requirement: Monitoring views
The web app SHALL show a prominent paper/live mode + kill-switch banner and views for P&L, positions, orders, trades, and the option chain (per-strike OI/LTP + CE-vs-PE aggregate), sourced from the API.

#### Scenario: Mode banner
- **WHEN** the app loads while the loop is in live mode
- **THEN** a prominent LIVE indicator is shown so it is not mistaken for paper

#### Scenario: Option-chain view
- **WHEN** the operator opens the option-chain view
- **THEN** per-strike CE/PE OI and LTP and the CE-vs-PE aggregate with selected side are displayed

### Requirement: Controls
The web app SHALL provide Start, Stop, and Flatten controls that call the API's control endpoints, with confirmation for destructive actions.

#### Scenario: Stop from the UI
- **WHEN** the operator clicks Stop and confirms
- **THEN** the app calls the API stop control and reflects the resulting HALTED state

### Requirement: Config editor
The web app SHALL let the operator view and edit tunable strategy parameters (e.g. lots, per-underlying weekdays, targets, strike window) and save them via the API, surfacing validation errors.

#### Scenario: Edit and save config
- **WHEN** the operator changes a parameter and saves
- **THEN** the app submits it to the API and shows success, or shows the validation error on rejection

### Requirement: Live updates
The web app SHALL subscribe to the API's live stream and update the monitoring views automatically as new data arrives, without a manual page refresh.

#### Scenario: Auto-refreshing views
- **WHEN** the live stream pushes an update
- **THEN** the visible P&L/state (and chain) update in place without reloading the page
