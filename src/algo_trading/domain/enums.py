"""Enumerations for the trading domain.

String values are chosen to match Kotak Neo API conventions where relevant, so the
broker wrapper can map them with minimal translation.
"""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    """Order transaction side."""

    BUY = "B"
    SELL = "S"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Underlying(str, Enum):
    NIFTY = "NIFTY"
    SENSEX = "SENSEX"


class ExchangeSegment(str, Enum):
    """Kotak Neo exchange segments. NIFTY options trade on NSE F&O, SENSEX on BSE F&O."""

    NSE_FO = "nse_fo"
    BSE_FO = "bse_fo"

    @classmethod
    def for_underlying(cls, underlying: Underlying) -> ExchangeSegment:
        return cls.NSE_FO if underlying is Underlying.NIFTY else cls.BSE_FO


class ProductType(str, Enum):
    MIS = "MIS"  # intraday
    NRML = "NRML"  # carry-forward
    CNC = "CNC"


class OrderType(str, Enum):
    MARKET = "MKT"
    LIMIT = "L"
    SL = "SL"
    SL_M = "SL-M"


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderState(str, Enum):
    """Lifecycle states for an order tracked by the order manager."""

    PENDING = "PENDING"  # persisted, about to be / just submitted
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED}


class AlgoState(str, Enum):
    """Authoritative run state consulted by all entry decisions."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    HALTED = "HALTED"  # kill-switch / manual halt; blocks new entries

    @property
    def entries_allowed(self) -> bool:
        return self is AlgoState.RUNNING


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class StrikeSelection(str, Enum):
    """How to pick the strike relative to ATM (in strike steps)."""

    ITM2 = "ITM2"
    ITM1 = "ITM1"
    ATM = "ATM"
    OTM1 = "OTM1"
    OTM2 = "OTM2"

    @property
    def offset_steps(self) -> int:
        """Signed number of strike steps from ATM (positive = OTM for a call-side view)."""
        return {
            StrikeSelection.ITM2: -2,
            StrikeSelection.ITM1: -1,
            StrikeSelection.ATM: 0,
            StrikeSelection.OTM1: 1,
            StrikeSelection.OTM2: 2,
        }[self]
