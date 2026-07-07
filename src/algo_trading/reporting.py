"""P&L reporting from trade fills.

Computes realized P&L from a day's fills using per-symbol average-price matching, which is
order-independent (robust to how fills are stored/returned). For each symbol:
    matched_qty  = min(buy_qty, sell_qty)
    realized_pnl = matched_qty * (avg_sell_price - avg_buy_price)
Any unmatched quantity is an open position (net_qty), whose P&L is unrealized and not counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from algo_trading.domain.enums import Side
from algo_trading.domain.models import Trade


@dataclass(frozen=True)
class SymbolPnL:
    symbol: str
    buy_qty: int
    sell_qty: int
    avg_buy: Decimal
    avg_sell: Decimal
    matched_qty: int
    realized_pnl: Decimal
    net_qty: int  # buy_qty - sell_qty (>0 = open long, <0 = open short)


@dataclass(frozen=True)
class FillSummary:
    per_symbol: list[SymbolPnL]
    total_realized: Decimal
    total_buy_value: Decimal
    total_sell_value: Decimal
    trade_count: int
    matched_symbols: int
    open_symbols: int


def _avg(value: Decimal, qty: int) -> Decimal:
    return (value / Decimal(qty)) if qty else Decimal(0)


def summarize_fills(trades: list[Trade]) -> FillSummary:
    """Aggregate fills into a per-symbol realized-P&L summary."""
    buy_qty: dict[str, int] = {}
    sell_qty: dict[str, int] = {}
    buy_val: dict[str, Decimal] = {}
    sell_val: dict[str, Decimal] = {}
    symbols: list[str] = []

    for t in trades:
        sym = t.instrument.trading_symbol
        if sym not in buy_qty:
            buy_qty[sym] = sell_qty[sym] = 0
            buy_val[sym] = sell_val[sym] = Decimal(0)
            symbols.append(sym)
        value = t.price * Decimal(t.quantity)
        if t.side is Side.BUY:
            buy_qty[sym] += t.quantity
            buy_val[sym] += value
        else:
            sell_qty[sym] += t.quantity
            sell_val[sym] += value

    rows: list[SymbolPnL] = []
    total_realized = Decimal(0)
    for sym in symbols:
        bq, sq = buy_qty[sym], sell_qty[sym]
        avg_buy, avg_sell = _avg(buy_val[sym], bq), _avg(sell_val[sym], sq)
        matched = min(bq, sq)
        realized = Decimal(matched) * (avg_sell - avg_buy)
        total_realized += realized
        rows.append(
            SymbolPnL(
                symbol=sym, buy_qty=bq, sell_qty=sq, avg_buy=avg_buy, avg_sell=avg_sell,
                matched_qty=matched, realized_pnl=realized, net_qty=bq - sq,
            )
        )

    # most impactful symbols first
    rows.sort(key=lambda r: r.realized_pnl)
    return FillSummary(
        per_symbol=rows,
        total_realized=total_realized,
        total_buy_value=sum(buy_val.values(), Decimal(0)),
        total_sell_value=sum(sell_val.values(), Decimal(0)),
        trade_count=len(trades),
        matched_symbols=sum(1 for r in rows if r.matched_qty > 0),
        open_symbols=sum(1 for r in rows if r.net_qty != 0),
    )
