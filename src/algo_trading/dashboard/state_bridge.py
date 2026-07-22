"""Bridge between the dashboard and the trading loop.

The dashboard runs as a SEPARATE process and must never hold a broker session or place orders.
This bridge only reads state from the shared PostgreSQL database and writes control commands
(start/stop/flatten) that the orchestrator consumes. Open positions and P&L are reconstructed by
replaying the day's trades through the same PositionTracker the loop uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import AlgoState
from algo_trading.domain.models import Position
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository


@dataclass(frozen=True)
class EnginePnL:
    """The trading loop's own last P&L reading, and how old it is.

    Carried next to the numbers the dashboard computes itself so the two can be compared: if the
    loop stops publishing, ``age_seconds`` grows and the UI can say so instead of showing a stale
    figure that still looks live.
    """

    realized: Decimal
    unrealized: Decimal
    total: Decimal
    age_seconds: float


@dataclass
class DashboardState:
    algo_state: AlgoState
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    day_pnl: Decimal
    positions: list[Position] = field(default_factory=list)
    trades: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    chain: list = field(default_factory=list)
    latest_pnl_snapshot: Decimal | None = None
    engine_pnl: EnginePnL | None = None


class StateBridge:
    def __init__(self, settings: Settings) -> None:
        # A fresh engine over the SAME PostgreSQL database the loop writes to (separate process).
        self._repo = Repository(create_engine_from_settings(settings))
        self._quote_max_age = float(getattr(settings, "live_quote_max_age_seconds", 60))

    def read_state(self) -> DashboardState:
        trades = self._repo.trades_for_day()
        tracker = PositionTracker()
        for trade in trades:
            tracker.on_fill(trade)
        self._mark_to_market(tracker)
        snapshot = self._repo.latest_pnl_snapshot()
        return DashboardState(
            algo_state=self._repo.get_algo_state(),
            realized_pnl=tracker.realized_pnl(),
            unrealized_pnl=tracker.unrealized_pnl(),
            day_pnl=tracker.day_pnl(),
            positions=tracker.open_positions(),
            trades=trades,
            orders=self._repo.broker_orders_for_day(),
            audit=self._repo.audit_events(),
            chain=self._repo.latest_chain_state(),
            latest_pnl_snapshot=snapshot.total if snapshot else None,
            engine_pnl=_engine_pnl(snapshot),
        )

    def _mark_to_market(self, tracker: PositionTracker) -> None:
        """Mark the replayed positions at the loop's published prices.

        Replaying fills alone leaves every position marked at its own fill price, so unrealized
        P&L would always be zero. Positions with no fresh quote keep the fill price — the same
        behaviour as before, and visible as a stale ``engine_pnl`` rather than a silent zero.
        """
        tokens = [p.instrument.instrument_token for p in tracker.open_positions()]
        if not tokens:
            return
        for token, ltp in self._repo.live_quotes(
            tokens, max_age_seconds=self._quote_max_age
        ).items():
            tracker.on_price(token, ltp)

    def chain(self, underlying: str | None = None) -> list:
        """Latest option-chain snapshots, optionally filtered to one underlying."""
        return self._repo.latest_chain_state(underlying=underlying)

    def chain_oi_baseline(self, underlying: str | None = None) -> dict[str, int]:
        """Day-open OI per token (intraday change-in-OI baseline) for the chain view."""
        return self._repo.chain_day_open_oi(underlying=underlying)

    def chain_oi_anchors(
        self, window_minutes: list[int], underlying: str | None = None
    ) -> dict[int, dict[str, int]]:
        """Anchor OI per token for each look-back window (for rolling OI-trend arrows).
        Returns {window_minutes: {token: anchor_oi}} using now (UTC) as the reference."""
        if not window_minutes:
            return {}
        return self._repo.oi_anchors_for_windows(
            datetime.utcnow(), window_minutes, underlying=underlying
        )

    def broker_positions(self) -> list[dict]:
        """The live broker positions captured at the last reconcile (raw broker dicts)."""
        return self._repo.latest_broker_positions()

    # -- Control commands (consumed by the orchestrator) -------------------------------

    def send_start(self) -> None:
        self._repo.enqueue_command("start")

    def send_stop(self) -> None:
        self._repo.enqueue_command("stop")

    def send_flatten(self) -> None:
        self._repo.enqueue_command("flatten")


def _engine_pnl(snapshot) -> EnginePnL | None:
    """Adapt a stored P&L snapshot to the dashboard view, resolving its age against now.

    Snapshot timestamps are written as naive UTC (``datetime.utcnow``), so the age is computed
    against the same clock. A clock skew between the two processes can only make the age look
    slightly off, never negative-and-alarming — it is floored at zero.
    """
    if snapshot is None:
        return None
    age = (datetime.utcnow() - snapshot.at).total_seconds()
    return EnginePnL(
        realized=snapshot.realized,
        unrealized=snapshot.unrealized,
        total=snapshot.total,
        age_seconds=max(0.0, age),
    )
