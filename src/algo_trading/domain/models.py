"""Immutable domain models shared across the pipeline.

These are plain Pydantic models (not persistence rows). Persistence rows live in
``algo_trading.persistence`` and are mapped to/from these where needed.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.domain.enums import (
    ExchangeSegment,
    OptionType,
    OrderState,
    OrderType,
    Side,
    Underlying,
    Validity,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Instrument(_Frozen):
    """A resolved tradeable option contract from the scrip master."""

    underlying: Underlying
    exchange_segment: ExchangeSegment
    trading_symbol: str
    instrument_token: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    lot_size: int


class Tick(_Frozen):
    """A normalized market-data update."""

    instrument_token: str
    exchange_segment: ExchangeSegment
    ltp: Decimal
    timestamp: datetime
    is_index: bool = False


class Candle(_Frozen):
    """A closed OHLC candle for an underlying over a fixed interval."""

    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)


class Signal(_Frozen):
    """A strategy entry intent on an underlying (not yet an order)."""

    underlying: Underlying
    side: Side
    option_type: OptionType
    reference_price: Decimal  # underlying price at signal time (for strike selection)
    timestamp: datetime
    reason: str = ""


class OrderRequest(_Frozen):
    """A concrete, broker-ready order request carrying an idempotency tag."""

    client_tag: str  # unique idempotency key, persisted before submission
    instrument: Instrument
    side: Side
    quantity: int  # already a lot multiple
    order_type: OrderType
    price: Decimal  # limit price; 0 for market
    validity: Validity = Validity.DAY
    trigger_price: Decimal = Decimal(0)
    is_exit: bool = False


class OrderEvent(_Frozen):
    """A normalized order/trade update from the order feed or a paper fill."""

    client_tag: str
    broker_order_id: str | None
    state: OrderState
    filled_quantity: int = 0
    average_price: Decimal = Decimal(0)
    timestamp: datetime
    message: str = ""


class Position(_Frozen):
    """An open position with computed P&L."""

    instrument: Instrument
    side: Side
    quantity: int
    average_price: Decimal
    last_price: Decimal
    realized_pnl: Decimal = Decimal(0)

    @property
    def unrealized_pnl(self) -> Decimal:
        direction = Decimal(1) if self.side is Side.BUY else Decimal(-1)
        return (self.last_price - self.average_price) * direction * Decimal(self.quantity)


class Trade(_Frozen):
    """A completed fill."""

    client_tag: str
    broker_order_id: str | None
    instrument: Instrument
    side: Side
    quantity: int
    price: Decimal
    timestamp: datetime = Field(default_factory=datetime.now)
