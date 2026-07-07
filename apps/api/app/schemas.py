"""API response models + a builder that maps the engine's DashboardState/summaries to them."""

from __future__ import annotations

from pydantic import BaseModel

from algo_trading.config.settings import Settings
from algo_trading.dashboard.state_bridge import DashboardState
from algo_trading.reporting import summarize_chain, summarize_fills


class StateOut(BaseModel):
    mode: str
    live_armed: bool
    algo_state: str
    active_underlying: str | None  # the OI underlying that trades today (SENSEX Wed/Thu, NIFTY else)
    oi_underlyings: list[str]


class SymbolPnLOut(BaseModel):
    symbol: str
    buy_qty: int
    sell_qty: int
    avg_buy: float
    avg_sell: float
    net_qty: int
    realized_pnl: float


class PnLOut(BaseModel):
    total_realized: float
    total_buy_value: float
    total_sell_value: float
    trade_count: int
    matched_symbols: int
    open_symbols: int
    per_symbol: list[SymbolPnLOut]


class PositionOut(BaseModel):
    symbol: str
    side: str
    quantity: int
    average_price: float
    last_price: float
    unrealized_pnl: float


class TradeOut(BaseModel):
    time: str
    symbol: str
    side: str
    quantity: int
    price: float


class OrderOut(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    price: str
    order_type: str
    product: str
    status: str
    order_time: str


class ChainStrikeOut(BaseModel):
    strike: float
    ce_oi: int
    ce_ltp: float
    pe_oi: int
    pe_ltp: float


class ChainOut(BaseModel):
    underlying: str | None
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str
    per_strike: list[ChainStrikeOut]


def state_out(settings: Settings, s: DashboardState) -> StateOut:
    active = settings.active_underlying_for_today()
    return StateOut(
        mode=settings.mode.value, live_armed=settings.live_armed, algo_state=s.algo_state.value,
        active_underlying=active.value if active else None,
        oi_underlyings=[u.value for u in settings.oi_underlyings],
    )


def pnl_out(s: DashboardState) -> PnLOut:
    fs = summarize_fills(s.trades)
    return PnLOut(
        total_realized=float(fs.total_realized), total_buy_value=float(fs.total_buy_value),
        total_sell_value=float(fs.total_sell_value), trade_count=fs.trade_count,
        matched_symbols=fs.matched_symbols, open_symbols=fs.open_symbols,
        per_symbol=[SymbolPnLOut(symbol=r.symbol, buy_qty=r.buy_qty, sell_qty=r.sell_qty,
                                 avg_buy=float(r.avg_buy), avg_sell=float(r.avg_sell),
                                 net_qty=r.net_qty, realized_pnl=float(r.realized_pnl))
                    for r in fs.per_symbol],
    )


def positions_out(s: DashboardState) -> list[PositionOut]:
    return [PositionOut(symbol=p.instrument.trading_symbol, side=p.side.value, quantity=p.quantity,
                        average_price=float(p.average_price), last_price=float(p.last_price),
                        unrealized_pnl=float(p.unrealized_pnl)) for p in s.positions]


def trades_out(s: DashboardState) -> list[TradeOut]:
    return [TradeOut(time=str(t.timestamp), symbol=t.instrument.trading_symbol, side=t.side.value,
                     quantity=t.quantity, price=float(t.price)) for t in s.trades]


def orders_out(s: DashboardState) -> list[OrderOut]:
    return [OrderOut(order_id=o.order_id, symbol=o.trading_symbol, side=o.side, quantity=o.quantity,
                     filled_quantity=o.filled_quantity, price=o.price, order_type=o.order_type,
                     product=o.product, status=o.status, order_time=o.order_time) for o in s.orders]


def chain_out(rows: list, underlying: str | None = None) -> ChainOut:
    cs = summarize_chain(rows)
    return ChainOut(underlying=underlying, ce_oi_total=cs.ce_oi_total, pe_oi_total=cs.pe_oi_total,
                    selected_side=cs.selected_side,
                    per_strike=[ChainStrikeOut(strike=float(x.strike), ce_oi=x.ce_oi, ce_ltp=float(x.ce_ltp),
                                               pe_oi=x.pe_oi, pe_ltp=float(x.pe_ltp)) for x in cs.per_strike])
