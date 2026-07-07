"""Risk management: pre-trade checks and the persistent daily-loss-cap kill-switch.

The authoritative :class:`AlgoState` lives in the database (per trading day). Once HALTED, it is
never auto-reset within the same day and survives a process restart, so the kill-switch cannot be
defeated by restarting. All entry decisions go through :meth:`check_entry`.
"""

from __future__ import annotations

from dataclasses import dataclass

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import AlgoState
from algo_trading.domain.models import Signal
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.observability.logging import get_logger
from algo_trading.persistence.repositories import Repository

log = get_logger("risk")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, settings: Settings, repo: Repository, positions: PositionTracker) -> None:
        self._settings = settings
        self._repo = repo
        self._positions = positions
        self._entries_today = 0

    # -- State ---------------------------------------------------------------------------

    @property
    def state(self) -> AlgoState:
        return self._repo.get_algo_state()

    def is_halted(self) -> bool:
        return self.state is AlgoState.HALTED

    def start_session(self) -> AlgoState:
        """Set RUNNING, unless the day is already HALTED (persisted kill-switch wins)."""
        current = self._repo.get_algo_state()
        if current is AlgoState.HALTED:
            log.warning("session_start_blocked_halted")
            return current
        self._repo.set_algo_state(AlgoState.RUNNING, reason="session start")
        return AlgoState.RUNNING

    def stop_session(self, reason: str = "session stop") -> None:
        if not self.is_halted():  # don't overwrite a HALTED audit state
            self._repo.set_algo_state(AlgoState.IDLE, reason=reason)

    def manual_halt(self, reason: str = "manual halt") -> None:
        self._repo.set_algo_state(AlgoState.HALTED, reason=reason)
        self._repo.record_audit("manual_halt", reason)
        log.warning("manual_halt", reason=reason)

    # -- Kill-switch ---------------------------------------------------------------------

    def evaluate_kill_switch(self) -> bool:
        """Halt trading if realized+unrealized day P&L has breached the daily-loss cap.

        Returns True if the kill-switch is (now or already) active.
        """
        if self.is_halted():
            return True
        day_pnl = self._positions.day_pnl()
        cap = self._settings.daily_loss_cap
        if day_pnl <= -abs(cap):
            self._repo.set_algo_state(AlgoState.HALTED, reason="daily loss cap breached")
            self._repo.record_audit(
                "kill_switch",
                "daily loss cap breached",
                {"day_pnl": str(day_pnl), "cap": str(cap)},
            )
            log.error("kill_switch_triggered", day_pnl=str(day_pnl), cap=str(cap))
            return True
        return False

    # -- Pre-trade checks ----------------------------------------------------------------

    def check_entry(self, signal: Signal) -> RiskDecision:
        if not self.state.entries_allowed:
            return RiskDecision(False, f"algo state is {self.state.value}")
        if self.is_halted():
            return RiskDecision(False, "kill-switch active")
        if self._positions.open_position_count() >= self._settings.max_positions:
            return RiskDecision(False, "max concurrent positions reached")
        if self._entries_today >= self._settings.max_trades_per_day:
            return RiskDecision(False, "max trades per day reached")
        return RiskDecision(True)

    def register_entry(self) -> None:
        """Record that an entry order was placed (for the max-trades-per-day limit)."""
        self._entries_today += 1

    @property
    def entries_today(self) -> int:
        return self._entries_today

    def lot_quantity(self, lot_size: int) -> int:
        return self._settings.lots * lot_size
