"""Live position and P&L tracking.

Strategy positions are long options (buy-to-open, sell-to-close). The tracker maintains net
quantity and average price per contract, computes realized P&L on exits and unrealized P&L from
the latest LTP, and exposes aggregate day P&L.
"""

from __future__ import annotations

from decimal import Decimal

from algo_trading.domain.enums import Side
from algo_trading.domain.models import Instrument, Position, Trade
from algo_trading.observability.logging import get_logger

log = get_logger("execution.positions")


class _Book:
    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument
        self.quantity = 0  # net (positive = long)
        self.avg_price = Decimal(0)
        self.realized = Decimal(0)
        self.last_price = Decimal(0)


class PositionTracker:
    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}

    def on_fill(self, trade: Trade) -> None:
        book = self._books.setdefault(trade.instrument.trading_symbol, _Book(trade.instrument))
        book.last_price = trade.price
        if trade.side is Side.BUY:
            self._add(book, trade.quantity, trade.price)
        else:
            self._reduce(book, trade.quantity, trade.price)
        log.info(
            "position_updated",
            symbol=trade.instrument.trading_symbol,
            net_qty=book.quantity,
            avg=str(book.avg_price),
            realized=str(book.realized),
        )

    def _add(self, book: _Book, qty: int, price: Decimal) -> None:
        new_qty = book.quantity + qty
        if new_qty == 0:
            book.avg_price = Decimal(0)
        else:
            book.avg_price = (
                (book.avg_price * book.quantity) + (price * qty)
            ) / Decimal(new_qty)
        book.quantity = new_qty

    def _reduce(self, book: _Book, qty: int, price: Decimal) -> None:
        closing = min(qty, book.quantity)
        if closing > 0:
            book.realized += (price - book.avg_price) * Decimal(closing)
        book.quantity -= qty
        if book.quantity <= 0:
            book.quantity = max(book.quantity, 0)
            book.avg_price = Decimal(0)

    def on_price(self, instrument_token: str, ltp: Decimal) -> None:
        for book in self._books.values():
            if book.instrument.instrument_token == instrument_token:
                book.last_price = ltp

    def open_positions(self) -> list[Position]:
        return [
            Position(
                instrument=b.instrument,
                side=Side.BUY,
                quantity=b.quantity,
                average_price=b.avg_price,
                last_price=b.last_price or b.avg_price,
                realized_pnl=b.realized,
            )
            for b in self._books.values()
            if b.quantity > 0
        ]

    def open_position_count(self) -> int:
        return sum(1 for b in self._books.values() if b.quantity > 0)

    def realized_pnl(self) -> Decimal:
        return sum((b.realized for b in self._books.values()), Decimal(0))

    def unrealized_pnl(self) -> Decimal:
        total = Decimal(0)
        for b in self._books.values():
            if b.quantity > 0:
                total += (b.last_price - b.avg_price) * Decimal(b.quantity)
        return total

    def day_pnl(self) -> Decimal:
        return self.realized_pnl() + self.unrealized_pnl()
