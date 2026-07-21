"""P&L reporting from trade fills.

Computes realized P&L from a day's fills using per-symbol average-price matching, which is
order-independent (robust to how fills are stored/returned). For each symbol:
    matched_qty  = min(buy_qty, sell_qty)
    realized_pnl = matched_qty * (avg_sell_price - avg_buy_price)
Any unmatched quantity is an open position (net_qty), whose P&L is unrealized and not counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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


# OI-trend direction codes for a single look-back window.
TREND_UP = "up"
TREND_DOWN = "down"
TREND_FLAT = "flat"
TREND_NA = "na"  # no anchor snapshot precedes now-window (unavailable, NOT flat)


@dataclass(frozen=True)
class OiTrend:
    """OI trend for one look-back window: direction plus signed OI delta over the window.
    ``delta`` is None exactly when ``direction`` is ``na`` (no anchor available)."""

    direction: str
    delta: int | None = None


def oi_trend(current_oi: int, anchor_oi: int | None, flat_threshold: int = 0) -> OiTrend:
    """Classify a single window's OI trend by comparing current OI to the window's anchor OI.

    - anchor_oi is None  -> na (no snapshot precedes now-window; unavailable, not flat)
    - |delta| <= threshold -> flat
    - delta > 0 -> up ; delta < 0 -> down
    """
    if anchor_oi is None:
        return OiTrend(TREND_NA, None)
    delta = current_oi - anchor_oi
    if abs(delta) <= flat_threshold:
        return OiTrend(TREND_FLAT, delta)
    return OiTrend(TREND_UP if delta > 0 else TREND_DOWN, delta)


def _trends_for_token(
    current_oi: int,
    token: str,
    anchors: dict[int, dict[str, int]],
    windows: list[int],
    flat_threshold: int,
) -> dict[int, OiTrend]:
    """Per-window OiTrend for one instrument token. ``anchors`` maps window-minutes ->
    {token: anchor_oi}; a token absent from a window's map means no anchor (na)."""
    out: dict[int, OiTrend] = {}
    for w in windows:
        anchor = anchors.get(w, {}).get(token) if token else None
        out[w] = oi_trend(current_oi, anchor, flat_threshold)
    return out


@dataclass(frozen=True)
class ChainStrike:
    strike: Decimal
    ce_oi: int
    ce_ltp: Decimal
    pe_oi: int
    pe_ltp: Decimal
    ce_chg_oi: int = 0  # intraday change vs the day's first snapshot
    pe_chg_oi: int = 0
    is_atm: bool = False
    # Per-window OI trends keyed by window-minutes (e.g. {1: OiTrend, 3: ...}). Empty when no
    # anchors were supplied (callers that don't compute trends keep the prior behavior).
    ce_oi_trends: dict[int, OiTrend] = field(default_factory=dict)
    pe_oi_trends: dict[int, OiTrend] = field(default_factory=dict)


@dataclass(frozen=True)
class ChainSummary:
    per_strike: list[ChainStrike]
    ce_oi_total: int
    pe_oi_total: int
    selected_side: str  # "CE" | "PE" | "—"
    atm: Decimal | None = None


def _resolve_atm(per_strike: list[ChainStrike]) -> Decimal | None:
    """ATM strike from chain data alone: the strike where CE and PE premiums are closest
    (put-call parity puts ATM there). Falls back to the middle strike of the window when LTPs
    aren't available (e.g. market closed)."""
    priced = [s for s in per_strike if s.ce_ltp > 0 and s.pe_ltp > 0]
    if priced:
        return min(priced, key=lambda s: abs(s.ce_ltp - s.pe_ltp)).strike
    if per_strike:
        return per_strike[len(per_strike) // 2].strike
    return None


def summarize_chain(
    rows: list,
    oi_baseline: dict[str, int] | None = None,
    oi_anchors: dict[int, dict[str, int]] | None = None,
    trend_windows: list[int] | None = None,
    flat_threshold: int = 0,
) -> ChainSummary:
    """Pivot latest option-chain snapshot rows (with .strike/.option_type/.oi/.ltp/.instrument_token)
    into a per-strike CE/PE view: OI, intraday change-in-OI (vs the day-open baseline), the
    aggregate CE-vs-PE OI, the side the OI strategy would sell, and the ATM strike.

    ``oi_baseline`` maps instrument_token -> day-open OI; change is 0 when no baseline is given.
    ``oi_anchors`` maps window-minutes -> {token: anchor_oi} for rolling OI trends; when omitted,
    trends are not computed and the prior behavior is preserved."""
    baseline = oi_baseline or {}
    anchors = oi_anchors or {}
    windows = trend_windows or []
    # value tuple: (oi, ltp, chg_oi, token)
    by_strike: dict[Decimal, dict[str, tuple[int, Decimal, int, str]]] = {}
    for r in rows:
        try:
            strike = Decimal(str(r.strike))
        except (ValueError, ArithmeticError):
            continue
        oi = int(r.oi) if r.oi is not None else 0
        ltp = _to_decimal(r.ltp)
        token = str(getattr(r, "instrument_token", ""))
        chg = oi - baseline.get(token, oi)  # 0 when this token has no day-open baseline
        by_strike.setdefault(strike, {})[str(r.option_type).upper()] = (oi, ltp, chg, token)

    per_strike: list[ChainStrike] = []
    ce_total = pe_total = 0
    for strike in sorted(by_strike):
        ce = by_strike[strike].get("CE", (0, Decimal(0), 0, ""))
        pe = by_strike[strike].get("PE", (0, Decimal(0), 0, ""))
        ce_total += ce[0]
        pe_total += pe[0]
        per_strike.append(ChainStrike(
            strike=strike, ce_oi=ce[0], ce_ltp=ce[1], ce_chg_oi=ce[2],
            pe_oi=pe[0], pe_ltp=pe[1], pe_chg_oi=pe[2],
            ce_oi_trends=_trends_for_token(ce[0], ce[3], anchors, windows, flat_threshold),
            pe_oi_trends=_trends_for_token(pe[0], pe[3], anchors, windows, flat_threshold),
        ))

    atm = _resolve_atm(per_strike)
    if atm is not None:
        per_strike = [replace(s, is_atm=(s.strike == atm)) for s in per_strike]

    selected = "CE" if ce_total > pe_total else "PE" if pe_total > ce_total else "—"
    return ChainSummary(per_strike, ce_total, pe_total, selected, atm)


def _to_decimal(v: object) -> Decimal:
    try:
        return Decimal(str(v))
    except (ValueError, ArithmeticError):
        return Decimal(0)


@dataclass(frozen=True)
class BrokerPositionPnL:
    symbol: str
    net_qty: int  # >0 open long, <0 open short, 0 fully squared
    buy_qty: int
    sell_qty: int
    avg_buy: Decimal
    avg_sell: Decimal
    realized_pnl: Decimal  # matched-qty realized (avg_sell - avg_buy) * matched
    is_open: bool


@dataclass(frozen=True)
class BrokerPnLSummary:
    per_position: list[BrokerPositionPnL]
    total_realized: Decimal
    open_count: int


def _to_int(v: object) -> int:
    try:
        return int(Decimal(str(v)))
    except (ValueError, ArithmeticError):
        return 0


def summarize_broker_positions(rows: list[dict]) -> BrokerPnLSummary:
    """Realized day P&L per broker position via matched-qty (avg-price) matching, from the raw
    Kotak position fields (flBuyQty/flSellQty filled quantities, buyAmt/sellAmt rupee values).

    Realized on the matched (squared) quantity is exact. Any net_qty is still open — its
    unrealized MTM needs a live LTP and is NOT included here."""
    out: list[BrokerPositionPnL] = []
    total = Decimal(0)
    for r in rows:
        bq = _to_int(r.get("flBuyQty"))
        sq = _to_int(r.get("flSellQty"))
        buy_val = _to_decimal(r.get("buyAmt"))
        sell_val = _to_decimal(r.get("sellAmt"))
        avg_buy, avg_sell = _avg(buy_val, bq), _avg(sell_val, sq)
        matched = min(bq, sq)
        realized = Decimal(matched) * (avg_sell - avg_buy)
        total += realized
        net = bq - sq
        out.append(
            BrokerPositionPnL(
                symbol=str(r.get("trdSym", "")), net_qty=net, buy_qty=bq, sell_qty=sq,
                avg_buy=avg_buy, avg_sell=avg_sell, realized_pnl=realized, is_open=net != 0,
            )
        )
    out.sort(key=lambda p: p.realized_pnl)
    return BrokerPnLSummary(
        per_position=out,
        total_realized=total,
        open_count=sum(1 for p in out if p.is_open),
    )


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
