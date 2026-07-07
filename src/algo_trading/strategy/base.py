"""Pluggable strategy interface.

A strategy consumes closed candles and emits entry :class:`Signal`s. Concrete strategies own
their indicators. The orchestrator feeds candles via :meth:`on_candle` and resets per session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from algo_trading.domain.models import Candle, Signal


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def on_candle(self, candle: Candle) -> list[Signal]:
        """Evaluate a closed candle and return zero or more entry signals."""

    def on_session_start(self) -> None:
        """Reset session-anchored state (e.g. VWAP). Called at the start of each session."""
