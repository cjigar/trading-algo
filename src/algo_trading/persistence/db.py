"""SQLite persistence schema (SQLModel).

Monetary values are stored as strings (serialized ``Decimal``) to preserve exactness for
the audit trail; the repository layer converts to/from ``Decimal``. Event, trade, P&L, and
audit tables are treated as append-only by the repository layer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Field, SQLModel, create_engine


class OrderRow(SQLModel, table=True):
    """Current state of an order, keyed by its idempotency tag. Mutated on state transitions;
    every transition also appends an immutable :class:`OrderEventRow`."""

    __tablename__ = "orders"

    client_tag: str = Field(primary_key=True)
    broker_order_id: str | None = Field(default=None, index=True)
    trading_symbol: str
    instrument_token: str
    underlying: str
    exchange_segment: str
    strike: str
    option_type: str
    lot_size: int
    side: str
    quantity: int
    order_type: str
    price: str
    state: str = Field(index=True)
    filled_quantity: int = 0
    average_price: str = "0"
    is_exit: bool = False
    trading_day: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OrderEventRow(SQLModel, table=True):
    """Append-only log of every order/trade update (never mutated)."""

    __tablename__ = "order_events"

    id: int | None = Field(default=None, primary_key=True)
    client_tag: str = Field(index=True)
    broker_order_id: str | None = None
    state: str
    filled_quantity: int = 0
    average_price: str = "0"
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class TradeRow(SQLModel, table=True):
    """Append-only record of a completed fill."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    client_tag: str = Field(index=True)
    broker_order_id: str | None = None
    trading_symbol: str
    instrument_token: str
    underlying: str
    exchange_segment: str
    strike: str
    option_type: str
    lot_size: int
    side: str
    quantity: int
    price: str
    trading_day: str = Field(index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class PnlSnapshotRow(SQLModel, table=True):
    """Append-only periodic P&L snapshot."""

    __tablename__ = "pnl_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    trading_day: str = Field(index=True)
    realized: str
    unrealized: str
    total: str
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class AuditEventRow(SQLModel, table=True):
    """Append-only audit trail (kill-switch, manual halt, login, reconciliation, etc.)."""

    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(index=True)
    message: str = ""
    payload: str = ""  # JSON string
    trading_day: str = Field(index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class AlgoStateRow(SQLModel, table=True):
    """Authoritative algo state, keyed by trading day. HALTED persists across restarts and
    is never auto-reset within the same trading day."""

    __tablename__ = "algo_state"

    trading_day: str = Field(primary_key=True)
    state: str
    reason: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ControlCommandRow(SQLModel, table=True):
    """Commands issued by the dashboard for the orchestrator to consume (start/stop/flatten)."""

    __tablename__ = "control_commands"

    id: int | None = Field(default=None, primary_key=True)
    command: str
    payload: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    consumed_at: datetime | None = Field(default=None, index=True)


def create_db_engine(db_path: str | Path) -> Engine:
    """Create (and initialize) the SQLite engine at ``db_path``."""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine
