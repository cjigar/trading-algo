## Why

The existing algo trades a VWAP breakout by *buying* weekly options. The operator wants a distinct, data-driven **option-selling** strategy: use the NIFTY option chain's Open Interest (OI) to decide which side to short, and continuously capture the chain into the database for analysis. This needs (a) a live option-chain feed (NIFTY spot + strikes around ATM, with OI) persisted to the DB, and (b) a new strategy that shorts the higher-OI side a few strikes OTM. Neither exists today: the current feed streams only LTP for a single underlying/option and captures no OI or chain data.

## What Changes

- **Option-chain websocket feed → database**: continuously subscribe to the NIFTY index spot and the option contracts from **ATM −5 to ATM +5 strikes on both CE and PE** (22 contracts). Stream each tick's **OI, LTP, and volume** into the database as time-series snapshots, and re-subscribe dynamically as ATM shifts.
- **ATM resolution from the NIFTY feed**: derive ATM continuously from the live NIFTY spot (nearest 50-point strike) so the chain window and strike selection track the market.
- **Per-option VWAP**: compute a session-anchored VWAP for each streamed option from its own ticks (needed for the exit rule).
- **OI-based selling strategy** (`oi-selling-strategy`): on each evaluation, aggregate **total CE-side OI vs total PE-side OI** across the ATM ±5 band; **SELL (short) the higher-OI side**, at the contract **3 strikes OTM** (CE → ATM+150, PE → ATM−150 for 50-pt strikes). Enter one short position per signal.
- **Exit rule**: **stop-loss when the short option's LTP crosses above its own session VWAP**, plus a hard **time-based square-off**. (Selling premium: rising price = losing; VWAP cross = SL.)
- **Day-of-week gating**: the strategy trades **only on Friday, Monday, and Tuesday**; on other days the feed may still run but no entries are taken.
- **Short-position support**: sell-to-open / buy-to-close lifecycle, short P&L, and margin-aware risk checks (short options require margin and have open-ended risk).

## Capabilities

### New Capabilities
- `option-chain-feed`: resolve the ATM ±5 CE/PE contracts from live NIFTX spot, subscribe them (plus the index) over the websocket, compute per-option VWAP, and persist OI/LTP/volume snapshots to the database with dynamic re-subscription as ATM moves.
- `oi-selling-strategy`: aggregate CE-vs-PE OI over the ±5 band, short the higher-OI side 3 strikes OTM, gate entries to Fri/Mon/Tue, and exit on VWAP-cross stop-loss plus time square-off, including short-position tracking and margin-aware risk.

### Modified Capabilities
<!-- The prior change (add-kotak-fno-trading-algo) is not yet archived, so there are no published
     specs to delta. Extensions to its feed/execution/risk modules are described under Impact. -->

## Impact

- **New code**: `feed/option_chain.py` (chain resolver + subscription manager), option-chain persistence tables + repository methods, per-option VWAP tracking, a new `strategy/oi_selling.py`, day-of-week gating, and short-position/margin handling.
- **Extends existing modules**: the market-data `Tick`/feed to carry **OI and volume** (currently LTP-only); the `LiveFeedCoordinator` to manage a large, dynamic option-chain subscription set; `position_tracker`/`exit_manager` to support **short** positions and a VWAP-cross exit; `risk_manager` to add a margin/short-exposure pre-trade check; `scheduler`/settings for the Fri/Mon/Tue calendar.
- **Data volume**: streaming 22 option contracts' OI/LTP into the DB continuously produces high write throughput — needs efficient batched inserts and retention/pruning (PostgreSQL in the Docker deployment).
- **Dependencies**: no new libraries expected (reuses the Kotak SDK websocket, pandas, SQLModel). Requires the NIFTY index token and the SDK's OI-bearing quote feed.
- **Risk**: this shorts options (open-ended risk, margin required). Safety leans on the mandatory VWAP-cross SL, the time square-off, margin pre-checks, the existing paper-mode default, and the daily-loss kill-switch.
