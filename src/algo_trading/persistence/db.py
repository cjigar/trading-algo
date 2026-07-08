"""Persistence schema (SQLModel), portable across SQLite and PostgreSQL.

The engine is built from a SQLAlchemy URL: SQLite by default (local dev + tests), PostgreSQL
in containers. Monetary values are stored as strings (serialized ``Decimal``) to preserve
exactness for the audit trail; the repository layer converts to/from ``Decimal``. Event,
trade, P&L, and audit tables are treated as append-only by the repository layer.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Field, SQLModel, create_engine

from algo_trading.observability.logging import get_logger

log = get_logger("persistence.db")


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


class OptionChainSnapshotRow(SQLModel, table=True):
    """Append-only time series of option-chain quotes (OI/LTP/volume) for the ATM window."""

    __tablename__ = "option_chain_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    trading_day: str = Field(index=True)
    underlying: str = Field(index=True)
    strike: str
    option_type: str
    instrument_token: str = Field(index=True)
    oi: int | None = None
    ltp: str = "0"
    volume: int | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class BrokerOrderRow(SQLModel, table=True):
    """An order imported from the broker's order report (order book). Keyed by broker order id;
    re-importing upserts the latest status/fill."""

    __tablename__ = "broker_orders"

    order_id: str = Field(primary_key=True)
    trading_symbol: str
    side: str
    quantity: int = 0
    filled_quantity: int = 0
    price: str = "0"
    order_type: str = ""
    product: str = ""
    status: str = Field(default="", index=True)
    order_time: str = ""
    trading_day: str = Field(index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BrokerPositionRow(SQLModel, table=True):
    """A snapshot of one open broker position captured at reconcile. The raw broker dict is kept
    as JSON so the view is resilient to the broker's field naming; the whole set is replaced on
    each capture (positions are a point-in-time snapshot, not an event log)."""

    __tablename__ = "broker_positions"

    id: int | None = Field(default=None, primary_key=True)
    trading_day: str = Field(index=True)
    raw: str = "{}"  # JSON-encoded raw broker position dict
    captured_at: datetime = Field(default_factory=datetime.utcnow, index=True)


def create_db_engine(db_path: str | Path) -> Engine:
    """Create (and initialize) a SQLite engine at ``db_path`` (local dev / tests)."""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine_from_url(f"sqlite:///{path}")


def create_engine_from_url(url: str, *, create: bool = True, retries: int = 1) -> Engine:
    """Create an engine from a SQLAlchemy URL (SQLite or PostgreSQL) and init the schema.

    For PostgreSQL, ``retries`` allows waiting for the database container to accept
    connections on first boot (docker-compose start ordering).
    """
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if create:
        _create_all_with_retry(engine, retries)
    return engine


def create_engine_from_settings(settings, *, create: bool = True) -> Engine:
    """Build the engine from application settings (SQLite by default, Postgres when configured)."""
    url = settings.resolved_database_url()
    if url.startswith("sqlite"):
        path = Path(url.replace("sqlite:///", "", 1))
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
    log.info("db_engine", backend="postgres" if "postgres" in url else "sqlite")
    return create_engine_from_url(url, create=create, retries=settings.db_connect_retries)


def _create_all_with_retry(engine: Engine, retries: int) -> None:
    attempt = 0
    while True:
        try:
            SQLModel.metadata.create_all(engine)
            return
        except OperationalError:
            attempt += 1
            if attempt > retries:
                raise
            wait = min(2 ** attempt, 10)
            log.warning("db_not_ready_retrying", attempt=attempt, wait_s=wait)
            time.sleep(wait)
