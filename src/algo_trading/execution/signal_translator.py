"""Translate strategy signals into concrete, broker-ready option order requests.

Resolves the option contract, applies fixed lot sizing, chooses order type/limit price, and
attaches a unique client tag (idempotency key). If an option LTP is available a marketable
limit is used; otherwise the entry falls back to a market order.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import OrderType, Side, Validity
from algo_trading.domain.models import Instrument, OrderRequest, Signal
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.observability.logging import get_logger

log = get_logger("execution.translator")


def new_client_tag(prefix: str = "alg") -> str:
    """Short unique idempotency tag (fits Kotak's tag length limits)."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class SignalTranslator:
    def __init__(self, settings: Settings, resolver: WeeklyOptionResolver) -> None:
        self._settings = settings
        self._resolver = resolver

    def translate(self, signal: Signal, option_ltp: Decimal | None = None) -> OrderRequest:
        if signal.target_strike is not None:
            # explicit strike (e.g. OI strategy's 3-OTM); snap to nearest if absent
            instrument = self._resolver.find_at_strike(
                signal.underlying, signal.target_strike, signal.option_type
            )
            if instrument is None:
                raise ValueError(
                    f"No contract at strike {signal.target_strike} for "
                    f"{signal.underlying.value} {signal.option_type.value}"
                )
        else:
            instrument = self._resolver.resolve(
                underlying=signal.underlying,
                spot=signal.reference_price,
                option_type=signal.option_type,
                selection=self._settings.strike_selection,
            )
        quantity = self._settings.lots * instrument.lot_size
        return self._build(instrument, signal.side, quantity, option_ltp, is_exit=False)

    def build_exit(
        self, instrument: Instrument, quantity: int, ltp: Decimal | None,
        position_side: Side = Side.BUY,
    ) -> OrderRequest:
        """Build an order to flatten a position: SELL to close a long, BUY to close a short."""
        close_side = Side.SELL if position_side is Side.BUY else Side.BUY
        return self._build(instrument, close_side, quantity, ltp, is_exit=True)

    def _build(
        self,
        instrument: Instrument,
        side: Side,
        quantity: int,
        ltp: Decimal | None,
        *,
        is_exit: bool,
    ) -> OrderRequest:
        if ltp is not None and ltp > 0:
            # marketable limit: pay up a touch to cross the spread on entry/exit
            slippage = (ltp * Decimal("0.01")).quantize(Decimal("0.05"))
            price = ltp + slippage if side is Side.BUY else max(ltp - slippage, Decimal("0.05"))
            order_type = OrderType.LIMIT
        else:
            price = Decimal(0)
            order_type = OrderType.MARKET
        return OrderRequest(
            client_tag=new_client_tag(),
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            validity=Validity.DAY,
            is_exit=is_exit,
        )
