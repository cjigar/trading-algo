"""Exit management for open long option positions.

Per position (keyed by trading symbol), enforces on the option premium:
  - a fixed profit target that, once reached, switches to a trailing stop,
  - a hard stop-loss,
  - (time-based square-off is driven separately by the scheduler via ``square_off_all``).

``evaluate`` is pure w.r.t. the current LTP and returns an exit reason (or None), so it is
straightforward to unit-test each path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from algo_trading.config.settings import Settings
from algo_trading.domain.models import Instrument
from algo_trading.observability.logging import get_logger

log = get_logger("execution.exits")


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    SQUARE_OFF = "square_off"
    VWAP_CROSS = "vwap_cross"


@dataclass
class _ExitState:
    instrument: Instrument
    quantity: int
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    trail_active: bool = False
    trail_stop: Decimal = Decimal(0)


@dataclass
class _ShortVwapState:
    """Short position exited on a VWAP cross: exit (buy-to-close) when LTP crosses ABOVE the
    option's session VWAP. Armed only after LTP has been at/below VWAP, to avoid an immediate
    exit if the premium is already above VWAP at entry."""

    instrument: Instrument
    quantity: int
    entry_price: Decimal
    armed: bool = False


class ExitManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._states: dict[str, _ExitState] = {}
        self._short_states: dict[str, _ShortVwapState] = {}

    def register(self, instrument: Instrument, quantity: int, entry_price: Decimal) -> None:
        s = self._settings
        self._states[instrument.trading_symbol] = _ExitState(
            instrument=instrument,
            quantity=quantity,
            entry_price=entry_price,
            target_price=entry_price + s.target_points,
            stop_price=entry_price - s.stoploss_points,
        )
        log.info(
            "exit_registered",
            symbol=instrument.trading_symbol,
            entry=str(entry_price),
            target=str(entry_price + s.target_points),
            stop=str(entry_price - s.stoploss_points),
        )

    def register_short_vwap(
        self, instrument: Instrument, quantity: int, entry_price: Decimal
    ) -> None:
        """Register a short position exited on a VWAP cross (buy-to-close when LTP > VWAP)."""
        self._short_states[instrument.trading_symbol] = _ShortVwapState(
            instrument=instrument, quantity=quantity, entry_price=entry_price
        )
        log.info("short_vwap_registered", symbol=instrument.trading_symbol, entry=str(entry_price))

    def evaluate_short_vwap(
        self, trading_symbol: str, ltp: Decimal, vwap: Decimal | None
    ) -> ExitReason | None:
        """Exit a short when LTP crosses above the option's session VWAP (armed after LTP<=VWAP)."""
        st = self._short_states.get(trading_symbol)
        if st is None or vwap is None:
            return None
        if ltp <= vwap:
            st.armed = True  # price at/below VWAP -> a subsequent cross above is a real SL
            return None
        if st.armed and ltp > vwap:
            return ExitReason.VWAP_CROSS
        return None

    def unregister(self, trading_symbol: str) -> None:
        self._states.pop(trading_symbol, None)
        self._short_states.pop(trading_symbol, None)

    @property
    def tracked_symbols(self) -> list[str]:
        return list(self._states) + list(self._short_states)

    def short_state_for(self, trading_symbol: str) -> _ShortVwapState | None:
        return self._short_states.get(trading_symbol)

    def evaluate(self, trading_symbol: str, ltp: Decimal) -> ExitReason | None:
        """Return an exit reason if the position should be exited at this LTP, else None."""
        st = self._states.get(trading_symbol)
        if st is None:
            return None

        # activate trailing once the target is reached
        if not st.trail_active and ltp >= st.target_price:
            st.trail_active = True
            st.trail_stop = ltp - self._settings.trail_points

        if st.trail_active:
            st.trail_stop = max(st.trail_stop, ltp - self._settings.trail_points)
            if ltp <= st.trail_stop:
                return ExitReason.TRAILING_STOP
            return None

        # before target: hard stop-loss
        if ltp <= st.stop_price:
            return ExitReason.STOP_LOSS
        return None

    def state_for(self, trading_symbol: str) -> _ExitState | None:
        return self._states.get(trading_symbol)
