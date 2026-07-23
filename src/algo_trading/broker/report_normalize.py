"""Pure normalizers mapping raw Kotak order/trade-report rows into DB fields.

The broker's report field names vary by SDK version, so each of our fields is matched against a
list of candidate keys (case-insensitive, first match wins). These functions are pure — raw dict
in, fields dict (or ``None``) out — with no broker or DB dependency, so they are shared by both the
one-shot import CLIs (``tools/import_orders``, ``tools/import_trades``) and the live broker-account
poller (``Orchestrator.refresh_broker_account``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# our-field -> candidate order-report column names (case-insensitive, first match wins)
ORDER_FIELD_CANDIDATES: dict[str, list[str]] = {
    "order_id": ["nOrdNo", "order_id", "orderId", "ordNo", "nsOrdNo"],
    "trading_symbol": ["trdSym", "pTrdSymbol", "tradingsymbol", "sym", "symbol"],
    "side": ["trnsTp", "buySell", "transaction_type", "side", "bs"],
    "quantity": ["qty", "quantity", "ordQty", "totQty"],
    "filled_quantity": ["fldQty", "filledQty", "fillQty", "cumQty"],
    "price": ["prc", "avgPrc", "price", "ordPrc"],
    "order_type": ["prcTp", "order_type", "ordTyp", "prctype"],
    "product": ["prod", "product", "prodType"],
    "status": ["ordSt", "status", "orderStatus"],
    "order_time": ["ordDtTm", "flDtTm", "orderTime", "ordEntTm", "hsUpTm"],
}

# our-field -> candidate trade-report column names (case-insensitive, first match wins)
TRADE_FIELD_CANDIDATES: dict[str, list[str]] = {
    "trading_symbol": ["pTrdSymbol", "trdSym", "tradingsymbol", "trdSymbol", "sym", "symbol"],
    "instrument_token": ["tok", "instrument_token", "token", "pSymbol"],
    "exchange_segment": ["exSeg", "exchange_segment", "exch", "seg"],
    "side": ["trnsTp", "buySell", "transaction_type", "side", "bs"],
    "quantity": ["fldQty", "trdQty", "qty", "filledQty", "fillQty", "quantity"],
    "price": ["avgPrc", "fldPrc", "trdPrc", "fillPrice", "price"],
    "order_id": ["nOrdNo", "order_id", "orderId", "ordNo"],
    "fill_id": ["flTrdId", "trdNo", "fillId", "tradeId", "flId", "exchOrdId"],
    "timestamp": ["flDtTm", "exchTime", "fillTime", "trdTime", "exch_time", "time"],
}


def _pick(raw: dict, candidates: list[str]) -> Any:
    lower = {k.lower(): k for k in raw}
    for cand in candidates:
        if cand.lower() in lower:
            return raw[lower[cand.lower()]]
    return None


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _to_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _norm_side(v: Any) -> str:
    s = str(v).strip().upper()
    if s in ("B", "BUY", "1", "+1"):
        return "B"
    if s in ("S", "SELL", "-1"):
        return "S"
    return s[:1] or "B"


def _parse_symbol(symbol: str) -> tuple[str, str, str]:
    """Best-effort (underlying, option_type, strike) from a trading symbol (strike always '0')."""
    up = (symbol or "").upper()
    underlying = "SENSEX" if "SENSEX" in up else ("NIFTY" if "NIFTY" in up else "NA")
    option_type = "CE" if up.endswith("CE") else ("PE" if up.endswith("PE") else "NA")
    return underlying, option_type, "0"


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    text = str(v).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_order_row(raw: dict) -> dict | None:
    """Normalize a broker order-report row into fields for record_broker_order, or None."""
    order_id = _pick(raw, ORDER_FIELD_CANDIDATES["order_id"])
    symbol = _pick(raw, ORDER_FIELD_CANDIDATES["trading_symbol"])
    if not order_id or not symbol:
        return None
    return {
        "order_id": str(order_id),
        "trading_symbol": str(symbol),
        "side": _norm_side(_pick(raw, ORDER_FIELD_CANDIDATES["side"])),
        "quantity": _to_int(_pick(raw, ORDER_FIELD_CANDIDATES["quantity"])),
        "filled_quantity": _to_int(_pick(raw, ORDER_FIELD_CANDIDATES["filled_quantity"])),
        "price": str(_pick(raw, ORDER_FIELD_CANDIDATES["price"]) or "0"),
        "order_type": str(_pick(raw, ORDER_FIELD_CANDIDATES["order_type"]) or ""),
        "product": str(_pick(raw, ORDER_FIELD_CANDIDATES["product"]) or ""),
        "status": str(_pick(raw, ORDER_FIELD_CANDIDATES["status"]) or ""),
        "order_time": str(_pick(raw, ORDER_FIELD_CANDIDATES["order_time"]) or ""),
    }


def normalize_trade_row(raw: dict) -> dict | None:
    """Normalize a broker trade-report row into fields for record_broker_trade, or None."""
    symbol = _pick(raw, TRADE_FIELD_CANDIDATES["trading_symbol"])
    qty = _pick(raw, TRADE_FIELD_CANDIDATES["quantity"])
    price = _pick(raw, TRADE_FIELD_CANDIDATES["price"])
    if not symbol or qty is None or price is None:
        return None
    underlying, option_type, strike = _parse_symbol(str(symbol))
    fill_id = _pick(raw, TRADE_FIELD_CANDIDATES["fill_id"])
    order_id = _pick(raw, TRADE_FIELD_CANDIDATES["order_id"])
    tag_seed = fill_id or f"{order_id}-{symbol}-{qty}-{price}"
    return {
        "client_tag": f"trd-{tag_seed}",
        "broker_order_id": str(order_id) if order_id else None,
        "trading_symbol": str(symbol),
        "instrument_token": str(_pick(raw, TRADE_FIELD_CANDIDATES["instrument_token"]) or ""),
        "exchange_segment": str(_pick(raw, TRADE_FIELD_CANDIDATES["exchange_segment"]) or "nse_fo"),
        "underlying": underlying,
        "option_type": option_type,
        "strike": strike,
        "side": _norm_side(_pick(raw, TRADE_FIELD_CANDIDATES["side"])),
        "quantity": _to_int(qty),
        "price": _to_decimal(price),
        "timestamp": _parse_ts(_pick(raw, TRADE_FIELD_CANDIDATES["timestamp"])),
    }
