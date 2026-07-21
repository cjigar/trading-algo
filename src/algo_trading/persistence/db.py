"""Persistence schema (SQLModel) on PostgreSQL/TimescaleDB.

PostgreSQL is the only supported backend — the engine is built from ``ALGO_DATABASE_URL`` and
there is no file/SQLite fallback. Monetary values are stored as strings (serialized ``Decimal``)
to preserve exactness for the audit trail; the repository layer converts to/from ``Decimal``.
Event, trade, P&L, and audit tables are treated as append-only by the repository layer.

The two snapshot series (``option_chain_snapshots``, ``pnl_snapshots``) are TimescaleDB
hypertables partitioned on ``timestamp``; see :mod:`algo_trading.persistence.bootstrap` for the
extension, hypertable, policy, and continuous-aggregate setup applied at startup.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Column, Engine, Identity, Index
from sqlmodel import Field, SQLModel, create_engine

from algo_trading.config.settings import is_postgres_url
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
    """Append-only periodic P&L snapshot. Hypertable partitioned on ``timestamp``, so the primary
    key is composite ``(timestamp, id)`` — Timescale requires the partitioning column in every
    unique index."""

    __tablename__ = "pnl_snapshots"

    timestamp: datetime = Field(default_factory=datetime.utcnow, primary_key=True, index=True)
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(), primary_key=True),
    )
    trading_day: str = Field(index=True)
    realized: str
    unrealized: str
    total: str


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
    """Append-only time series of option-chain quotes (OI/LTP/volume) for the ATM window.

    Hypertable partitioned on ``timestamp`` (one chunk per trading day by default), so the primary
    key is composite ``(timestamp, id)`` — Timescale requires the partitioning column in every
    unique index. ``id`` is retained purely as a tiebreaker for rows sharing a timestamp.
    """

    __tablename__ = "option_chain_snapshots"
    # Composite index for the point-in-time-per-token anchor lookup used by rolling-window OI
    # trends: filter by token within a trading_day, order by timestamp to find the latest row
    # at-or-before a target time. Kept alongside the single-column indexes below.
    __table_args__ = (
        Index(
            "ix_chain_snap_token_day_ts",
            "instrument_token",
            "trading_day",
            "timestamp",
        ),
    )

    timestamp: datetime = Field(default_factory=datetime.utcnow, primary_key=True, index=True)
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(), primary_key=True),
    )
    trading_day: str = Field(index=True)
    underlying: str = Field(index=True)
    strike: str
    option_type: str
    instrument_token: str = Field(index=True)
    oi: int | None = None
    ltp: str = "0"
    volume: int | None = None


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


def create_engine_from_url(
    url: str,
    *,
    create: bool = True,
    retries: int = 1,
    settings=None,
) -> Engine:
    """Create an engine from a PostgreSQL URL and (unless ``create`` is false) bootstrap the schema.

    ``retries`` allows waiting for the database container to accept connections on first boot
    (docker-compose start ordering). ``settings`` supplies the time-series tuning knobs; when
    omitted, the bootstrap defaults are used.
    """
    if not is_postgres_url(url):
        raise ValueError(
            f"Only PostgreSQL URLs are supported; got {url!r}. "
            "Set ALGO_DATABASE_URL to a postgresql+psycopg:// URL."
        )
    engine = create_engine(url, pool_pre_ping=True)
    if create:
        # Imported here (not at module scope) because bootstrap imports the models above.
        from algo_trading.persistence.bootstrap import bootstrap_schema

        bootstrap_schema(engine, settings=settings, retries=retries)
    return engine


def create_engine_from_settings(settings, *, create: bool = True) -> Engine:
    """Build the PostgreSQL engine from application settings."""
    url = settings.resolved_database_url()
    log.info("db_engine", backend="postgres")
    return create_engine_from_url(
        url, create=create, retries=settings.db_connect_retries, settings=settings
    )
