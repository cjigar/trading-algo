"""API response models + a builder that maps the engine's DashboardState/summaries to them."""

from __future__ import annotations

from pydantic import BaseModel

from algo_trading.config.settings import Settings
from algo_trading.dashboard.state_bridge import DashboardState
from algo_trading.reporting import summarize_broker_positions, summarize_chain, summarize_fills


class StateOut(BaseModel):
    mode: str
    live_armed: bool
    algo_state: str
    strategy: str  # active strategy: oi_selling | vwap_breakout
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


class OiTrendOut(BaseModel):
    """One look-back window's OI trend: dir in up|down|flat|na; delta null when dir==na."""

    dir: str
    delta: int | None = None


class ChainStrikeOut(BaseModel):
    strike: float
    ce_oi: int
    ce_ltp: float
    ce_chg_oi: int
    pe_oi: int
    pe_ltp: float
    pe_chg_oi: int
    is_atm: bool
    # Per-window OI trends keyed by window label (e.g. "1m", "3m"). Empty when trends not computed.
    ce_oi_trends: dict[str, OiTrendOut] = {}
    pe_oi_trends: dict[str, OiTrendOut] = {}


class ChainOut(BaseModel):
    underlying: str | None
    atm: float | None
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str
    per_strike: list[ChainStrikeOut]


def state_out(settings: Settings, s: DashboardState) -> StateOut:
    active = settings.active_underlying_for_today()
    return StateOut(
        mode=settings.mode.value, live_armed=settings.live_armed, algo_state=s.algo_state.value,
        strategy=settings.strategy,
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


class BrokerPositionPnLOut(BaseModel):
    symbol: str
    net_qty: int
    buy_qty: int
    sell_qty: int
    avg_buy: float
    avg_sell: float
    realized_pnl: float
    is_open: bool


class BrokerPnLOut(BaseModel):
    total_realized: float  # realized on matched (squared) qty; excludes open-position MTM
    open_count: int
    per_position: list[BrokerPositionPnLOut]


def broker_pnl_out(rows: list[dict]) -> BrokerPnLOut:
    s = summarize_broker_positions(rows)
    return BrokerPnLOut(
        total_realized=float(s.total_realized), open_count=s.open_count,
        per_position=[BrokerPositionPnLOut(
            symbol=p.symbol, net_qty=p.net_qty, buy_qty=p.buy_qty, sell_qty=p.sell_qty,
            avg_buy=float(p.avg_buy), avg_sell=float(p.avg_sell),
            realized_pnl=float(p.realized_pnl), is_open=p.is_open,
        ) for p in s.per_position],
    )


def _trends_out(trends: dict) -> dict[str, OiTrendOut]:
    """Map reporting OiTrend (keyed by window-minutes) to API OiTrendOut (keyed 'Nm')."""
    return {f"{w}m": OiTrendOut(dir=t.direction, delta=t.delta) for w, t in trends.items()}


def chain_out(rows: list, underlying: str | None = None,
              oi_baseline: dict[str, int] | None = None,
              oi_anchors: dict[int, dict[str, int]] | None = None,
              trend_windows: list[int] | None = None,
              flat_threshold: int = 0) -> ChainOut:
    cs = summarize_chain(
        rows, oi_baseline, oi_anchors=oi_anchors,
        trend_windows=trend_windows, flat_threshold=flat_threshold,
    )
    return ChainOut(
        underlying=underlying, atm=float(cs.atm) if cs.atm is not None else None,
        ce_oi_total=cs.ce_oi_total, pe_oi_total=cs.pe_oi_total, selected_side=cs.selected_side,
        per_strike=[ChainStrikeOut(
            strike=float(x.strike), ce_oi=x.ce_oi, ce_ltp=float(x.ce_ltp), ce_chg_oi=x.ce_chg_oi,
            pe_oi=x.pe_oi, pe_ltp=float(x.pe_ltp), pe_chg_oi=x.pe_chg_oi, is_atm=x.is_atm,
            ce_oi_trends=_trends_out(x.ce_oi_trends), pe_oi_trends=_trends_out(x.pe_oi_trends),
        ) for x in cs.per_strike])
