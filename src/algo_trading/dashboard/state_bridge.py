"""Bridge between the dashboard and the trading loop.

The dashboard runs as a SEPARATE process and must never hold a broker session or place orders.
This bridge only reads state from the shared SQLite database and writes control commands
(start/stop/flatten) that the orchestrator consumes. Open positions and P&L are reconstructed by
replaying the day's trades through the same PositionTracker the loop uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import AlgoState
from algo_trading.domain.models import Position
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository


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


class StateBridge:
    def __init__(self, settings: Settings) -> None:
        # A fresh engine over the SAME database the loop writes to (separate process).
        # With Postgres both processes share the server; with SQLite they share the file.
        self._repo = Repository(create_engine_from_settings(settings))

    def read_state(self) -> DashboardState:
        trades = self._repo.trades_for_day()
        tracker = PositionTracker()
        for trade in trades:
            tracker.on_fill(trade)
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
            latest_pnl_snapshot=self._repo.latest_pnl(),
        )

    # -- Control commands (consumed by the orchestrator) -------------------------------

    def send_start(self) -> None:
        self._repo.enqueue_command("start")

    def send_stop(self) -> None:
        self._repo.enqueue_command("stop")

    def send_flatten(self) -> None:
        self._repo.enqueue_command("flatten")
