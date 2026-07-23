# oi-selling-strategy Specification

## Purpose
TBD - created by archiving change add-oi-option-selling-strategy. Update Purpose after archive.
## Requirements
### Requirement: Per-underlying trading-day gating
The strategy SHALL run **per underlying**, each with its own trading days, strike step, and index token. **NIFTY** takes entries only on **Friday, Monday, and Tuesday**; **SENSEX** only on **Wednesday and Thursday** (IST). On a disallowed day for an underlying it SHALL NOT open positions for it; the option-chain feed MAY continue to run and persist data for all underlyings.

#### Scenario: NIFTY allowed day
- **WHEN** it is a Friday, Monday, or Tuesday during market hours
- **THEN** the NIFTY strategy may evaluate and open a position, and the SENSEX strategy does not

#### Scenario: SENSEX allowed day
- **WHEN** it is a Wednesday or Thursday during market hours
- **THEN** the SENSEX strategy may evaluate and open a position, and the NIFTY strategy does not

#### Scenario: Per-underlying strike step
- **WHEN** resolving strikes for SENSEX vs NIFTY
- **THEN** the SENSEX strike step is 100 and the NIFTY strike step is 50

### Requirement: OI aggregation and sell-side selection
The strategy SHALL aggregate the total CE-side Open Interest and the total PE-side Open Interest across the ATM ±5 strike window, and SHALL select the side with the **higher aggregate OI** as the side to SELL (short). If the two aggregates are equal, no signal is produced.

#### Scenario: CE OI dominates
- **WHEN** aggregate CE OI across ATM ±5 exceeds aggregate PE OI
- **THEN** the strategy selects the CALL (CE) side to short

#### Scenario: PE OI dominates
- **WHEN** aggregate PE OI across ATM ±5 exceeds aggregate CE OI
- **THEN** the strategy selects the PUT (PE) side to short

### Requirement: Strike selection 3 strikes OTM
The strategy SHALL sell the selected side's contract **3 strikes out-of-the-money** from ATM: for a CALL that is ATM + 3 strikes (higher), for a PUT that is ATM − 3 strikes (lower). With NIFTY's 50-point step this is a 150-point offset.

#### Scenario: Short CE strike
- **WHEN** the CE side is selected and ATM is 23,050
- **THEN** the strategy shorts the 23,200 CE (ATM + 3 × 50)

#### Scenario: Short PE strike
- **WHEN** the PE side is selected and ATM is 23,050
- **THEN** the strategy shorts the 22,900 PE (ATM − 3 × 50)

### Requirement: Short entry with margin and risk checks
The strategy SHALL open the position by SELLING (shorting) the resolved contract for the configured lots, subject to pre-trade risk checks including available margin, maximum concurrent positions, and the authoritative algo state / kill-switch. An entry that fails a check SHALL be rejected before any order is placed.

#### Scenario: Entry placed when checks pass
- **WHEN** a signal is produced on an allowed day and margin and limits permit
- **THEN** a sell-to-open order is placed for the resolved OTM contract

#### Scenario: Entry blocked by risk
- **WHEN** margin is insufficient, the position limit is reached, or the algo is HALTED
- **THEN** no order is placed and the reason is recorded

### Requirement: VWAP-cross stop-loss and time square-off
The strategy SHALL exit (buy-to-close) a short option position when its **LTP crosses above the option's session VWAP** (the short is moving against the position), and SHALL additionally square off all positions at a configured cutoff time regardless of P&L. The time square-off SHALL run independently of the strategy/feed path.

#### Scenario: VWAP-cross stop-loss
- **WHEN** the shorted option's LTP rises above its session VWAP
- **THEN** the strategy buys back the position to close it

#### Scenario: Time square-off
- **WHEN** the configured square-off time is reached with an open short position
- **THEN** the position is bought back to close, verified flat against the broker

### Requirement: Short position and P&L tracking
The system SHALL track short option positions (sell-to-open, buy-to-close), computing realized and unrealized P&L for shorts (profit when the buy-back price is below the sell price).

#### Scenario: Short realized P&L
- **WHEN** a contract is sold at 100 and bought back at 70
- **THEN** the realized P&L is +30 per unit (times quantity)

