"""API response models + a builder that maps the engine's DashboardState/summaries to them."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from algo_trading.broker.report_normalize import normalize_trade_row
from algo_trading.config.settings import Settings
from algo_trading.dashboard.state_bridge import DashboardState
from algo_trading.reporting import summarize_broker_positions, summarize_chain, summarize_fills


class IndexSpotOut(BaseModel):
    underlying: str
    ltp: float
    day_open: float
    prev_close: float  # previous trading day's close — the baseline for the day change
    change: float  # ltp - prev_close (points); falls back to ltp - day_open on day one
    change_pct: float  # change / baseline * 100
    age_seconds: float  # how old the reading is
    stale: bool  # older than the live-quote freshness window


class StateOut(BaseModel):
    mode: str
    live_armed: bool
    algo_state: str
    strategy: str  # active strategy: oi_selling | vwap_breakout
    active_underlying: str | None  # the OI underlying that trades today (SENSEX Wed/Thu, NIFTY else)
    oi_underlyings: list[str]
    spots: list[IndexSpotOut] = []  # live NIFTY/SENSEX spot for the rate ticker


class SymbolPnLOut(BaseModel):
    symbol: str
    buy_qty: int
    sell_qty: int
    avg_buy: float
    avg_sell: float
    net_qty: int
    realized_pnl: float


class EnginePnLOut(BaseModel):
    """The trading loop's own last P&L reading. ``age_seconds`` is how long ago it published;
    a growing age means the loop is not reporting, whatever the other numbers say."""

    realized: float
    unrealized: float
    total: float
    age_seconds: float


class PnLOut(BaseModel):
    total_realized: float
    total_unrealized: float  # open positions marked at the loop's published prices
    day_pnl: float  # realized + unrealized
    total_buy_value: float
    total_sell_value: float
    trade_count: int
    matched_symbols: int
    open_symbols: int
    per_symbol: list[SymbolPnLOut]
    engine: EnginePnLOut | None = None


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
    ce_vwap: float | None = None
    pe_vwap: float | None = None


class ChainOut(BaseModel):
    underlying: str | None
    atm: float | None
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str
    per_strike: list[ChainStrikeOut]


def _spot_out(row, max_age_seconds: float, prev_close: Decimal | None = None) -> IndexSpotOut:
    def _dec(v) -> Decimal:
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(0)

    ltp, day_open = _dec(row.ltp), _dec(row.day_open)
    # Day change is measured against the previous trading day's close (what every ticker shows).
    # Fall back to the day's first spot only when there is no prior-day close yet (day one).
    baseline = prev_close if (prev_close is not None and prev_close != 0) else day_open
    change = ltp - baseline
    pct = (change / baseline * 100) if baseline != 0 else Decimal(0)
    age = max(0.0, (datetime.utcnow() - row.updated_at).total_seconds())
    return IndexSpotOut(
        underlying=row.underlying, ltp=float(ltp), day_open=float(day_open),
        prev_close=float(baseline), change=float(change), change_pct=float(pct),
        age_seconds=age, stale=age > max_age_seconds,
    )


def state_out(settings: Settings, s: DashboardState) -> StateOut:
    active = settings.active_underlying_for_today()
    max_age = float(getattr(settings, "live_quote_max_age_seconds", 60))
    return StateOut(
        mode=settings.mode.value, live_armed=settings.live_armed, algo_state=s.algo_state.value,
        strategy=settings.strategy,
        active_underlying=active.value if active else None,
        oi_underlyings=[u.value for u in settings.oi_underlyings],
        spots=[_spot_out(row, max_age, s.prev_index_closes.get(row.underlying)) for row in s.spots],
    )


def pnl_out(s: DashboardState) -> PnLOut:
    fs = summarize_fills(s.trades)
    e = s.engine_pnl
    return PnLOut(
        total_realized=float(fs.total_realized),
        total_unrealized=float(s.unrealized_pnl),
        day_pnl=float(fs.total_realized) + float(s.unrealized_pnl),
        engine=EnginePnLOut(realized=float(e.realized), unrealized=float(e.unrealized),
                            total=float(e.total), age_seconds=e.age_seconds) if e else None,
        total_buy_value=float(fs.total_buy_value),
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


def broker_trades_out(rows: list[dict]) -> list[TradeOut]:
    """The live broker trade report (raw Kotak dicts) mapped to clean TradeOut rows, reusing the
    same tolerant normalizer as the import CLI. Unparseable rows are skipped."""
    out: list[TradeOut] = []
    for raw in rows:
        f = normalize_trade_row(raw)
        if f is None:
            continue
        out.append(TradeOut(time=str(f["timestamp"] or ""), symbol=f["trading_symbol"],
                            side=f["side"], quantity=f["quantity"], price=float(f["price"])))
    return out


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
    total_pnl: float  # live M2M for this position (realized + unrealized at ltp)
    ltp: float | None = None  # price the open net qty is marked at (null if unpriced)
    mtm_pending: bool = False  # open position awaiting a live quote


class BrokerPnLOut(BaseModel):
    total_realized: float  # realized on matched (squared) qty
    total_pnl: float  # account live M2M (realized + unrealized) — matches the Kotak app
    open_count: int
    mtm_pending_count: int  # open positions still awaiting a live quote
    per_position: list[BrokerPositionPnLOut]


def broker_pnl_out(rows: list[dict], quotes: dict[str, Decimal] | None = None) -> BrokerPnLOut:
    s = summarize_broker_positions(rows, quotes)
    return BrokerPnLOut(
        total_realized=float(s.total_realized), total_pnl=float(s.total_pnl),
        open_count=s.open_count, mtm_pending_count=s.mtm_pending_count,
        per_position=[BrokerPositionPnLOut(
            symbol=p.symbol, net_qty=p.net_qty, buy_qty=p.buy_qty, sell_qty=p.sell_qty,
            avg_buy=float(p.avg_buy), avg_sell=float(p.avg_sell),
            realized_pnl=float(p.realized_pnl), is_open=p.is_open,
            total_pnl=float(p.total_pnl), ltp=float(p.ltp) if p.ltp is not None else None,
            mtm_pending=p.mtm_pending,
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
            ce_vwap=float(x.ce_vwap) if x.ce_vwap is not None else None,
            pe_vwap=float(x.pe_vwap) if x.pe_vwap is not None else None,
        ) for x in cs.per_strike])
