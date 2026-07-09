"""Repository layer over the SQLite schema.

Provides append-only writes for events/trades/P&L/audit, an idempotent-upsert for order
state (each transition also appends an immutable event), and persisted algo-state get/set.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import Engine, delete, func
from sqlmodel import Session, col, select

from algo_trading.domain.enums import AlgoState, ExchangeSegment, OptionType, Side, Underlying
from algo_trading.domain.models import Instrument, OrderEvent, OrderRequest, Trade
from algo_trading.persistence.db import (
    AlgoStateRow,
    AuditEventRow,
    BrokerOrderRow,
    BrokerPositionRow,
    ControlCommandRow,
    OptionChainSnapshotRow,
    OrderEventRow,
    OrderRow,
    PnlSnapshotRow,
    TradeRow,
)


def _today_str(trading_day: date | None = None) -> str:
    return (trading_day or date.today()).isoformat()


def _instrument_to_row_fields(inst: Instrument) -> dict[str, object]:
    return {
        "trading_symbol": inst.trading_symbol,
        "instrument_token": inst.instrument_token,
        "underlying": inst.underlying.value,
        "exchange_segment": inst.exchange_segment.value,
        "strike": str(inst.strike),
        "option_type": inst.option_type.value,
        "lot_size": inst.lot_size,
    }


def _safe_enum(enum_cls, value, default):
    """Coerce a stored string to an enum, falling back for imported/odd rows."""
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        return default


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _instrument_from_row(row: OrderRow | TradeRow) -> Instrument:
    # Tolerant of imported broker rows whose underlying/option_type may not map to our enums.
    return Instrument(
        underlying=_safe_enum(Underlying, row.underlying, Underlying.NIFTY),
        exchange_segment=_safe_enum(ExchangeSegment, row.exchange_segment, ExchangeSegment.NSE_FO),
        trading_symbol=row.trading_symbol,
        instrument_token=row.instrument_token,
        expiry=date.today(),  # expiry not persisted on order/trade rows; not needed post-hoc
        strike=_safe_decimal(row.strike),
        option_type=_safe_enum(OptionType, row.option_type, OptionType.CE),
        lot_size=row.lot_size,
    )


class Repository:
    """Thin data-access object. One instance per process; safe for the single-writer loop."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- Orders (idempotent state + append-only events) --------------------------------

    def record_new_order(self, req: OrderRequest, trading_day: date | None = None) -> None:
        """Persist a PENDING order *before* it is submitted (idempotency guarantee).

        No-op if the client_tag already exists, so a retry never creates a duplicate.
        """
        with Session(self._engine) as session:
            if session.get(OrderRow, req.client_tag) is not None:
                return
            row = OrderRow(
                client_tag=req.client_tag,
                broker_order_id=None,
                side=req.side.value,
                quantity=req.quantity,
                order_type=req.order_type.value,
                price=str(req.price),
                state="PENDING",
                is_exit=req.is_exit,
                trading_day=_today_str(trading_day),
                **_instrument_to_row_fields(req.instrument),
            )
            session.add(row)
            session.add(
                OrderEventRow(
                    client_tag=req.client_tag, state="PENDING", message="order created"
                )
            )
            session.commit()

    def apply_order_event(self, event: OrderEvent) -> None:
        """Update order state and append an immutable event row."""
        with Session(self._engine) as session:
            row = session.get(OrderRow, event.client_tag)
            if row is not None:
                row.state = event.state.value
                if event.broker_order_id:
                    row.broker_order_id = event.broker_order_id
                row.filled_quantity = event.filled_quantity or row.filled_quantity
                if event.average_price and event.average_price != Decimal(0):
                    row.average_price = str(event.average_price)
                row.updated_at = datetime.utcnow()
                session.add(row)
            session.add(
                OrderEventRow(
                    client_tag=event.client_tag,
                    broker_order_id=event.broker_order_id,
                    state=event.state.value,
                    filled_quantity=event.filled_quantity,
                    average_price=str(event.average_price),
                    message=event.message,
                )
            )
            session.commit()

    def get_order_state(self, client_tag: str) -> str | None:
        with Session(self._engine) as session:
            row = session.get(OrderRow, client_tag)
            return row.state if row else None

    def open_orders(self, trading_day: date | None = None) -> list[OrderRow]:
        with Session(self._engine) as session:
            stmt = select(OrderRow).where(
                OrderRow.trading_day == _today_str(trading_day),
                OrderRow.state.not_in(["FILLED", "REJECTED", "CANCELLED"]),  # type: ignore[attr-defined]
            )
            return list(session.exec(stmt))

    # -- Trades (append-only) ----------------------------------------------------------

    def record_trade(self, trade: Trade, trading_day: date | None = None) -> None:
        with Session(self._engine) as session:
            session.add(
                TradeRow(
                    client_tag=trade.client_tag,
                    broker_order_id=trade.broker_order_id,
                    side=trade.side.value,
                    quantity=trade.quantity,
                    price=str(trade.price),
                    trading_day=_today_str(trading_day),
                    timestamp=trade.timestamp,
                    **_instrument_to_row_fields(trade.instrument),
                )
            )
            session.commit()

    # -- Option-chain snapshots (append-only time series) ------------------------------

    def chain_day_open_oi(
        self, trading_day: date | None = None, underlying: str | None = None
    ) -> dict[str, int]:
        """Per instrument_token, the OI from that token's FIRST snapshot of the day (the intraday
        change-in-OI baseline). Optionally filtered to one underlying."""
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            earliest_q = (
                select(func.min(OptionChainSnapshotRow.id))
                .where(OptionChainSnapshotRow.trading_day == day)
                .group_by(col(OptionChainSnapshotRow.instrument_token))
            )
            if underlying is not None:
                earliest_q = earliest_q.where(OptionChainSnapshotRow.underlying == underlying)
            rows = session.exec(
                select(OptionChainSnapshotRow).where(col(OptionChainSnapshotRow.id).in_(earliest_q))
            )
            return {r.instrument_token: (r.oi or 0) for r in rows}

    def replace_broker_positions(self, positions: list[dict], trading_day: date | None = None) -> int:
        """Replace the stored broker-position snapshot with the current set (raw broker dicts).
        Positions are point-in-time, so we clear and re-insert rather than append."""
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            session.exec(delete(BrokerPositionRow))
            for p in positions:
                session.add(BrokerPositionRow(trading_day=day, raw=json.dumps(p, default=str)))
            session.commit()
        return len(positions)

    def latest_broker_positions(self) -> list[dict]:
        """The most recently captured broker positions as raw dicts (empty if none captured)."""
        with Session(self._engine) as session:
            rows = list(session.exec(select(BrokerPositionRow).order_by(col(BrokerPositionRow.id))))
        out: list[dict] = []
        for row in rows:
            try:
                out.append(json.loads(row.raw))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def write_chain_snapshots(self, rows: list[dict], trading_day: date | None = None) -> int:
        """Bulk-insert option-chain snapshot rows. Each dict: underlying, strike, option_type,
        instrument_token, oi, ltp, volume, timestamp."""
        if not rows:
            return 0
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            for r in rows:
                session.add(
                    OptionChainSnapshotRow(
                        trading_day=day,
                        underlying=str(r["underlying"]),
                        strike=str(r["strike"]),
                        option_type=str(r["option_type"]),
                        instrument_token=str(r["instrument_token"]),
                        oi=r.get("oi"),
                        ltp=str(r.get("ltp", "0")),
                        volume=r.get("volume"),
                        timestamp=r.get("timestamp") or datetime.utcnow(),
                    )
                )
            session.commit()
        return len(rows)

    def latest_chain_state(
        self, trading_day: date | None = None, underlying: str | None = None
    ) -> list[OptionChainSnapshotRow]:
        """Latest snapshot per instrument token for the day (the current chain state), optionally
        filtered to one underlying (e.g. only SENSEX)."""
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            latest_q = (
                select(func.max(OptionChainSnapshotRow.id))
                .where(OptionChainSnapshotRow.trading_day == day)
                .group_by(col(OptionChainSnapshotRow.instrument_token))
            )
            if underlying is not None:
                latest_q = latest_q.where(OptionChainSnapshotRow.underlying == underlying)
            return list(
                session.exec(
                    select(OptionChainSnapshotRow).where(col(OptionChainSnapshotRow.id).in_(latest_q))
                )
            )

    def prune_snapshots(self, older_than_days: int, today: date | None = None) -> int:
        """Delete option-chain snapshots older than ``older_than_days``. Returns rows deleted."""
        cutoff = ((today or date.today()) - timedelta(days=older_than_days)).isoformat()
        with Session(self._engine) as session:
            result = session.exec(
                delete(OptionChainSnapshotRow).where(col(OptionChainSnapshotRow.trading_day) < cutoff)
            )
            session.commit()
            return result.rowcount or 0

    def record_broker_order(self, fields: dict, trading_day: date | None = None) -> bool:
        """Upsert an order from the broker's order report, keyed by order_id. Returns True if a
        new row was inserted, False if an existing row was updated."""
        order_id = str(fields["order_id"])
        with Session(self._engine) as session:
            row = session.get(BrokerOrderRow, order_id)
            inserted = row is None
            if row is None:
                row = BrokerOrderRow(order_id=order_id, trading_symbol=fields["trading_symbol"],
                                     side=fields["side"], trading_day=_today_str(trading_day))
            row.trading_symbol = fields["trading_symbol"]
            row.side = fields["side"]
            row.quantity = int(fields.get("quantity", 0))
            row.filled_quantity = int(fields.get("filled_quantity", 0))
            row.price = str(fields.get("price", "0"))
            row.order_type = str(fields.get("order_type", ""))
            row.product = str(fields.get("product", ""))
            row.status = str(fields.get("status", ""))
            row.order_time = str(fields.get("order_time", ""))
            row.updated_at = datetime.utcnow()
            session.add(row)
            session.commit()
        return inserted

    def broker_orders_for_day(self, trading_day: date | None = None) -> list[BrokerOrderRow]:
        with Session(self._engine) as session:
            return list(
                session.exec(
                    select(BrokerOrderRow).where(
                        BrokerOrderRow.trading_day == _today_str(trading_day)
                    )
                )
            )

    def trade_exists(self, client_tag: str) -> bool:
        with Session(self._engine) as session:
            row = session.exec(
                select(TradeRow).where(TradeRow.client_tag == client_tag)
            ).first()
            return row is not None

    def record_broker_trade(self, fields: dict, trading_day: date | None = None) -> bool:
        """Insert a trade imported from the broker's trade report. Deduplicated by client_tag
        (typically 'trd-<fill_id>'). Returns True if inserted, False if it already existed."""
        client_tag = fields["client_tag"]
        if self.trade_exists(client_tag):
            return False
        with Session(self._engine) as session:
            session.add(
                TradeRow(
                    client_tag=client_tag,
                    broker_order_id=fields.get("broker_order_id"),
                    trading_symbol=fields["trading_symbol"],
                    instrument_token=fields.get("instrument_token", ""),
                    underlying=fields.get("underlying", "NA"),
                    exchange_segment=fields.get("exchange_segment", "nse_fo"),
                    strike=str(fields.get("strike", "0")),
                    option_type=fields.get("option_type", "NA"),
                    lot_size=int(fields.get("lot_size", 0)),
                    side=fields["side"],
                    quantity=int(fields["quantity"]),
                    price=str(fields["price"]),
                    trading_day=_today_str(trading_day),
                    timestamp=fields.get("timestamp") or datetime.utcnow(),
                )
            )
            session.commit()
        return True

    def trades_for_day(self, trading_day: date | None = None) -> list[Trade]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(TradeRow).where(TradeRow.trading_day == _today_str(trading_day))
            ).all()
            return [
                Trade(
                    client_tag=r.client_tag,
                    broker_order_id=r.broker_order_id,
                    instrument=_instrument_from_row(r),
                    side=Side(r.side),
                    quantity=r.quantity,
                    price=Decimal(r.price),
                    timestamp=r.timestamp,
                )
                for r in rows
            ]

    # -- P&L snapshots (append-only) ---------------------------------------------------

    def record_pnl(
        self, realized: Decimal, unrealized: Decimal, trading_day: date | None = None
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                PnlSnapshotRow(
                    trading_day=_today_str(trading_day),
                    realized=str(realized),
                    unrealized=str(unrealized),
                    total=str(realized + unrealized),
                )
            )
            session.commit()

    def latest_pnl(self, trading_day: date | None = None) -> Decimal | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(PnlSnapshotRow)
                .where(PnlSnapshotRow.trading_day == _today_str(trading_day))
                .order_by(PnlSnapshotRow.id.desc())  # type: ignore[union-attr]
            ).first()
            return Decimal(row.total) if row else None

    # -- Audit (append-only) -----------------------------------------------------------

    def record_audit(
        self,
        event_type: str,
        message: str = "",
        payload: dict | None = None,
        trading_day: date | None = None,
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                AuditEventRow(
                    event_type=event_type,
                    message=message,
                    payload=json.dumps(payload or {}, default=str),
                    trading_day=_today_str(trading_day),
                )
            )
            session.commit()

    def audit_events(self, trading_day: date | None = None) -> list[AuditEventRow]:
        with Session(self._engine) as session:
            return list(
                session.exec(
                    select(AuditEventRow).where(
                        AuditEventRow.trading_day == _today_str(trading_day)
                    )
                )
            )

    # -- Algo state (persisted, per trading day) ---------------------------------------

    def get_algo_state(self, trading_day: date | None = None) -> AlgoState:
        with Session(self._engine) as session:
            row = session.get(AlgoStateRow, _today_str(trading_day))
            return AlgoState(row.state) if row else AlgoState.IDLE

    def set_algo_state(
        self, state: AlgoState, reason: str = "", trading_day: date | None = None
    ) -> None:
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            row = session.get(AlgoStateRow, day)
            if row is None:
                row = AlgoStateRow(trading_day=day, state=state.value, reason=reason)
            else:
                row.state = state.value
                row.reason = reason
                row.updated_at = datetime.utcnow()
            session.add(row)
            session.commit()

    # -- Control commands (dashboard -> orchestrator) ----------------------------------

    def enqueue_command(self, command: str, payload: dict | None = None) -> None:
        with Session(self._engine) as session:
            session.add(
                ControlCommandRow(command=command, payload=json.dumps(payload or {}, default=str))
            )
            session.commit()

    def pop_pending_commands(self) -> list[ControlCommandRow]:
        """Return unconsumed commands and mark them consumed (FIFO)."""
        with Session(self._engine) as session:
            rows = list(
                session.exec(
                    select(ControlCommandRow)
                    .where(ControlCommandRow.consumed_at.is_(None))  # type: ignore[union-attr]
                    .order_by(ControlCommandRow.id)  # type: ignore[arg-type]
                )
            )
            for row in rows:
                row.consumed_at = datetime.utcnow()
                session.add(row)
            session.commit()
            # expunge so callers can read fields after the session closes
            for row in rows:
                session.refresh(row)
            return rows
