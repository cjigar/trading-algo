"""Repository layer over the PostgreSQL/TimescaleDB schema.

Provides append-only writes for events/trades/P&L/audit, an idempotent-upsert for order
state (each transition also appends an immutable event), and persisted algo-state get/set.
Reads over the snapshot hypertable use PostgreSQL ``DISTINCT ON`` for per-token latest/earliest
rows; rolling OI anchors additionally read the ``chain_oi_1m`` continuous aggregate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import Engine, delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from algo_trading.domain.enums import AlgoState, ExchangeSegment, OptionType, Side, Underlying
from algo_trading.domain.models import Instrument, OrderEvent, OrderRequest, Trade
from algo_trading.persistence.bootstrap import CHAIN_AGG_VIEW, CHAIN_TABLE, agg_bucket_seconds
from algo_trading.persistence.db import (
    AlgoStateRow,
    AuditEventRow,
    BrokerOrderRow,
    BrokerPositionRow,
    ControlCommandRow,
    LiveQuoteRow,
    OptionChainSnapshotRow,
    OrderEventRow,
    OrderRow,
    PnlSnapshotRow,
    TradeRow,
)

# Point-in-time OI anchor per instrument token: the continuous aggregate answers everything up to
# the last bucket that closed at or before :target, the raw hypertable covers the remaining tail,
# and the outer DISTINCT ON picks whichever is newer per token. A token with no snapshot before
# :target appears in neither branch and is therefore absent from the result.
#
# Both branches consider only rows that actually carry an OI: the broker sends OI in a token's
# first full packet and NULL in the LTP-only ticks that follow, so "the newest row" is almost
# never "the newest OI reading".
_ANCHOR_SQL = f"""
WITH boundary AS (
    SELECT time_bucket(CAST(:bucket AS interval), CAST(:target AS timestamp)) AS b
),
raw_tail AS (
    SELECT DISTINCT ON (s.instrument_token) s.instrument_token, s.oi, s.timestamp AS at
    FROM {CHAIN_TABLE} s, boundary
    WHERE s.trading_day = :day
      AND s.oi IS NOT NULL
      AND s.timestamp >= boundary.b
      AND s.timestamp <= CAST(:target AS timestamp)
      AND (CAST(:underlying AS text) IS NULL OR s.underlying = :underlying)
    ORDER BY s.instrument_token, s.timestamp DESC
),
agg AS (
    SELECT DISTINCT ON (a.instrument_token) a.instrument_token, a.last_oi AS oi, a.oi_at AS at
    FROM {CHAIN_AGG_VIEW} a, boundary
    WHERE a.trading_day = :day
      AND a.last_oi IS NOT NULL
      AND a.bucket < boundary.b
      AND (CAST(:underlying AS text) IS NULL OR a.underlying = :underlying)
    ORDER BY a.instrument_token, a.bucket DESC
)
SELECT DISTINCT ON (instrument_token) instrument_token, oi
FROM (SELECT * FROM raw_tail UNION ALL SELECT * FROM agg) u
ORDER BY instrument_token, at DESC
"""


@dataclass(frozen=True)
class PnlSnapshot:
    """One periodic P&L reading taken by the trading loop, with the time it was taken."""

    realized: Decimal
    unrealized: Decimal
    total: Decimal
    at: datetime


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
        self._bucket_seconds: int | None = None

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
        """Per instrument_token, the OI from that token's first snapshot of the day that carries
        one (the intraday change-in-OI baseline). Optionally filtered to one underlying.

        LTP-only ticks have a NULL OI and are skipped — otherwise the baseline would be zero for
        any token whose first packet of the day happened to omit OI."""
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            rows = session.exec(
                self._distinct_per_token(day, underlying, newest=False, with_oi_only=True)
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
        filtered to one underlying (e.g. only SENSEX).

        LTP comes from the genuinely newest row, but OI is carried over from that token's newest
        row that *has* an OI: the broker sends OI once per token in its full packet and NULL in
        every LTP-only tick after it, so the newest row's ``oi`` is almost always NULL.
        """
        day = _today_str(trading_day)
        with Session(self._engine) as session:
            rows = list(session.exec(self._distinct_per_token(day, underlying, newest=True)))
            known_oi = {
                r.instrument_token: r.oi
                for r in session.exec(
                    self._distinct_per_token(day, underlying, newest=True, with_oi_only=True)
                )
            }
        for row in rows:
            if row.oi is None:
                row.oi = known_oi.get(row.instrument_token)
        return rows

    @staticmethod
    def _distinct_per_token(
        day: str, underlying: str | None, *, newest: bool, with_oi_only: bool = False
    ):
        """``DISTINCT ON (instrument_token)`` over the day's snapshots, picking each token's
        newest (or, when ``newest`` is false, oldest) row. ``id`` breaks ties between rows
        written with the same timestamp. ``with_oi_only`` restricts the scan to rows that carry
        an OI reading."""
        ts = col(OptionChainSnapshotRow.timestamp)
        rid = col(OptionChainSnapshotRow.id)
        order = (ts.desc(), rid.desc()) if newest else (ts.asc(), rid.asc())
        stmt = (
            select(OptionChainSnapshotRow)
            .where(OptionChainSnapshotRow.trading_day == day)
            .distinct(col(OptionChainSnapshotRow.instrument_token))
            .order_by(col(OptionChainSnapshotRow.instrument_token), *order)
        )
        if underlying is not None:
            stmt = stmt.where(OptionChainSnapshotRow.underlying == underlying)
        if with_oi_only:
            stmt = stmt.where(col(OptionChainSnapshotRow.oi).is_not(None))
        return stmt

    def oi_at_or_before(
        self,
        target: datetime,
        trading_day: date | None = None,
        underlying: str | None = None,
    ) -> dict[str, int]:
        """Per instrument_token, the OI from that token's latest snapshot at or before ``target``
        (the point-in-time anchor for rolling-window OI trends). A token is present in the result
        only if at least one snapshot precedes ``target``; tokens with no prior snapshot are
        OMITTED (callers treat absence as "no anchor" / unavailable, distinct from a zero OI).

        Served by the ``chain_oi_1m`` continuous aggregate for every bucket that closed at or
        before ``target``, plus a raw-hypertable read of the tail since that last closed bucket
        (at most one bucket wide). Splitting on the bucket boundary is what keeps the aggregate
        from looking *ahead* of ``target`` — a bucket's ``last_oi`` is its value at bucket end,
        so only fully-elapsed buckets may be used.
        """
        day = _today_str(trading_day)
        bucket = f"{self._agg_bucket_seconds()} seconds"
        params = {"day": day, "target": target, "underlying": underlying, "bucket": bucket}
        with self._engine.connect() as conn:
            rows = conn.execute(text(_ANCHOR_SQL), params).all()
        return {token: (oi or 0) for token, oi in rows}

    def _agg_bucket_seconds(self) -> int:
        """Bucket width of the continuous aggregate, read once per repository."""
        if self._bucket_seconds is None:
            self._bucket_seconds = agg_bucket_seconds(self._engine)
        return self._bucket_seconds

    def oi_anchors_for_windows(
        self,
        now: datetime,
        window_minutes: list[int],
        trading_day: date | None = None,
        underlying: str | None = None,
    ) -> dict[int, dict[str, int]]:
        """Resolve anchor OI per token for each look-back window. Returns a mapping
        ``{window_minutes: {instrument_token: anchor_oi}}``. One grouped query per window
        (batched over all tokens), per the design's Decision 4. A token missing from a
        window's inner dict means no snapshot precedes ``now - window`` (anchor unavailable)."""
        out: dict[int, dict[str, int]] = {}
        for minutes in window_minutes:
            target = now - timedelta(minutes=minutes)
            out[minutes] = self.oi_at_or_before(target, trading_day=trading_day, underlying=underlying)
        return out

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
        snap = self.latest_pnl_snapshot(trading_day)
        return snap.total if snap else None

    def latest_pnl_snapshot(self, trading_day: date | None = None) -> PnlSnapshot | None:
        """The loop's most recent P&L reading, with the time it was taken.

        The dashboard computes its own P&L from trades and quotes; this is the loop's independent
        claim, carried alongside so a stalled loop shows up as an ageing snapshot instead of a
        confidently frozen number.
        """
        with Session(self._engine) as session:
            row = session.exec(
                select(PnlSnapshotRow)
                .where(PnlSnapshotRow.trading_day == _today_str(trading_day))
                .order_by(
                    col(PnlSnapshotRow.timestamp).desc(),
                    col(PnlSnapshotRow.id).desc(),
                )
            ).first()
        if row is None:
            return None
        return PnlSnapshot(
            realized=_safe_decimal(row.realized),
            unrealized=_safe_decimal(row.unrealized),
            total=_safe_decimal(row.total),
            at=row.timestamp,
        )

    # -- Live quotes (latest price per token; mutable, one row per token) ----------------

    def upsert_live_quotes(
        self, quotes: dict[str, Decimal], trading_day: date | None = None
    ) -> int:
        """Publish the loop's current price for each token, replacing any previous row for it.

        Upsert rather than append: readers only ever want "the price now", and an append-only
        feed of every tick is already persisted as ``option_chain_snapshots``.
        """
        if not quotes:
            return 0
        day = _today_str(trading_day)
        now = datetime.utcnow()
        rows = [
            {"instrument_token": str(token), "trading_day": day, "ltp": str(ltp), "timestamp": now}
            for token, ltp in quotes.items()
        ]
        stmt = pg_insert(LiveQuoteRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_token"],
            set_={
                "ltp": stmt.excluded.ltp,
                "timestamp": stmt.excluded.timestamp,
                "trading_day": stmt.excluded.trading_day,
            },
        )
        with Session(self._engine) as session:
            session.exec(stmt)
            session.commit()
        return len(rows)

    def live_quotes(
        self, tokens: list[str] | None = None, *, max_age_seconds: float | None = None
    ) -> dict[str, Decimal]:
        """Latest published price per token.

        ``tokens`` restricts the read to the ones the caller cares about (an empty list reads
        nothing). ``max_age_seconds`` drops readings older than that: a feed that died an hour ago
        must not keep marking positions at the last price it happened to see.
        """
        if tokens is not None and not tokens:
            return {}
        stmt = select(LiveQuoteRow)
        if tokens is not None:
            stmt = stmt.where(col(LiveQuoteRow.instrument_token).in_([str(t) for t in tokens]))
        if max_age_seconds is not None:
            cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
            stmt = stmt.where(col(LiveQuoteRow.timestamp) >= cutoff)
        with Session(self._engine) as session:
            return {r.instrument_token: _safe_decimal(r.ltp) for r in session.exec(stmt)}

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
