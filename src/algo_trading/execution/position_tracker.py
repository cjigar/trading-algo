"""Live position and P&L tracking (long and short).

Uses signed net quantity per contract (positive = long, negative = short) so both buy-to-open/
sell-to-close (long, VWAP-breakout strategy) and sell-to-open/buy-to-close (short, OI-selling
strategy) are handled by one accounting model. Realized P&L is booked when a fill reduces or
closes an existing position; unrealized P&L is marked from the latest LTP.
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
        self.quantity = 0  # signed net: positive = long, negative = short
        self.avg_price = Decimal(0)
        self.realized = Decimal(0)
        self.last_price = Decimal(0)


def _same_sign(a: int, b: int) -> bool:
    return (a >= 0) == (b >= 0)


class PositionTracker:
    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}

    def on_fill(self, trade: Trade) -> None:
        book = self._books.setdefault(trade.instrument.trading_symbol, _Book(trade.instrument))
        book.last_price = trade.price
        signed = trade.quantity if trade.side is Side.BUY else -trade.quantity
        self._apply(book, signed, trade.price)
        log.info("position_updated", symbol=trade.instrument.trading_symbol,
                 net_qty=book.quantity, avg=str(book.avg_price), realized=str(book.realized))

    def _apply(self, book: _Book, signed_qty: int, price: Decimal) -> None:
        if book.quantity == 0 or _same_sign(book.quantity, signed_qty):
            # opening or adding in the same direction -> weighted average by absolute size
            total_abs = abs(book.quantity) + abs(signed_qty)
            book.avg_price = (
                (book.avg_price * abs(book.quantity)) + (price * abs(signed_qty))
            ) / Decimal(total_abs)
            book.quantity += signed_qty
            return
        # opposite direction -> reduce/close (and possibly flip)
        closing = min(abs(signed_qty), abs(book.quantity))
        direction = 1 if book.quantity > 0 else -1  # long realizes (price-avg), short (avg-price)
        book.realized += (price - book.avg_price) * Decimal(direction) * Decimal(closing)
        book.quantity += signed_qty
        if book.quantity == 0:
            book.avg_price = Decimal(0)
        elif not _same_sign(book.quantity - signed_qty, book.quantity):
            # flipped through zero -> the remainder opens a new position at this price
            book.avg_price = price

    def on_price(self, instrument_token: str, ltp: Decimal) -> None:
        for book in self._books.values():
            if book.instrument.instrument_token == instrument_token:
                book.last_price = ltp

    @staticmethod
    def _unrealized(book: _Book) -> Decimal:
        direction = Decimal(1) if book.quantity > 0 else Decimal(-1)
        return (book.last_price - book.avg_price) * direction * Decimal(abs(book.quantity))

    def open_positions(self) -> list[Position]:
        return [
            Position(
                instrument=b.instrument,
                side=Side.BUY if b.quantity > 0 else Side.SELL,
                quantity=abs(b.quantity),
                average_price=b.avg_price,
                last_price=b.last_price or b.avg_price,
                realized_pnl=b.realized,
            )
            for b in self._books.values()
            if b.quantity != 0
        ]

    def open_position_count(self) -> int:
        return sum(1 for b in self._books.values() if b.quantity != 0)

    def position_for(self, trading_symbol: str) -> Position | None:
        b = self._books.get(trading_symbol)
        if b is None or b.quantity == 0:
            return None
        return Position(
            instrument=b.instrument,
            side=Side.BUY if b.quantity > 0 else Side.SELL,
            quantity=abs(b.quantity),
            average_price=b.avg_price,
            last_price=b.last_price or b.avg_price,
            realized_pnl=b.realized,
        )

    def realized_pnl(self) -> Decimal:
        return sum((b.realized for b in self._books.values()), Decimal(0))

    def unrealized_pnl(self) -> Decimal:
        return sum(
            (self._unrealized(b) for b in self._books.values() if b.quantity != 0), Decimal(0)
        )

    def day_pnl(self) -> Decimal:
        return self.realized_pnl() + self.unrealized_pnl()
