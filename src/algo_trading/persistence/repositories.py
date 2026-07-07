"""Repository layer over the SQLite schema.

Provides append-only writes for events/trades/P&L/audit, an idempotent-upsert for order
state (each transition also appends an immutable event), and persisted algo-state get/set.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Engine
from sqlmodel import Session, select

from algo_trading.domain.enums import AlgoState, ExchangeSegment, OptionType, Side, Underlying
from algo_trading.domain.models import Instrument, OrderEvent, OrderRequest, Trade
from algo_trading.persistence.db import (
    AlgoStateRow,
    AuditEventRow,
    ControlCommandRow,
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


def _instrument_from_row(row: OrderRow | TradeRow) -> Instrument:
    return Instrument(
        underlying=Underlying(row.underlying),
        exchange_segment=ExchangeSegment(row.exchange_segment),
        trading_symbol=row.trading_symbol,
        instrument_token=row.instrument_token,
        expiry=date.today(),  # expiry not persisted on order/trade rows; not needed post-hoc
        strike=Decimal(row.strike),
        option_type=OptionType(row.option_type),
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
