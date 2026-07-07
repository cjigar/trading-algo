"""VWAP / price-action breakout strategy.

Per underlying, maintains a session VWAP and a rolling high/low window. On each closed candle:
  - Bullish breakout: close > VWAP + buffer AND close > prior rolling high  -> BUY a CALL (CE)
  - Bearish breakout: close < VWAP - buffer AND close < prior rolling low   -> BUY a PUT  (PE)

Parameters (buffer, rolling window, timeframe) come from configuration. Indicators are updated
*after* evaluation so the breakout is judged against the prior window (no look-ahead).
"""

from __future__ import annotations

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import OptionType, Side, Underlying
from algo_trading.domain.models import Candle, Signal
from algo_trading.strategy.base import Strategy
from algo_trading.strategy.indicators import RollingExtrema, SessionVWAP


def underlying_from_symbol(symbol: str) -> Underlying | None:
    up = symbol.upper()
    if "SENSEX" in up:
        return Underlying.SENSEX
    if "NIFTY" in up:
        return Underlying.NIFTY
    return None


class _State:
    def __init__(self, window: int) -> None:
        self.vwap = SessionVWAP()
        self.extrema = RollingExtrema(window)


class VwapBreakoutStrategy(Strategy):
    name = "vwap_breakout"

    def __init__(self, settings: Settings, breakout_window: int = 3) -> None:
        self._settings = settings
        self._window = breakout_window
        self._buffer = settings.vwap_breakout_buffer
        self._states: dict[Underlying, _State] = {}

    def _state(self, underlying: Underlying) -> _State:
        if underlying not in self._states:
            self._states[underlying] = _State(self._window)
        return self._states[underlying]

    def on_session_start(self) -> None:
        for st in self._states.values():
            st.vwap.reset()
            st.extrema.reset()

    def on_candle(self, candle: Candle) -> list[Signal]:
        underlying = underlying_from_symbol(candle.symbol)
        if underlying is None:
            return []
        st = self._state(underlying)

        vwap = st.vwap.update(candle)
        prior_high = st.extrema.high()
        prior_low = st.extrema.low()
        ready = st.extrema.ready
        # update the rolling window AFTER capturing the prior extrema
        st.extrema.update(candle)

        if not ready or vwap is None:
            return []

        signals: list[Signal] = []
        close = candle.close
        buf = self._buffer

        if close > vwap + buf and prior_high is not None and close > prior_high:
            signals.append(
                Signal(
                    underlying=underlying,
                    side=Side.BUY,
                    option_type=OptionType.CE,
                    reference_price=close,
                    timestamp=candle.end,
                    reason=f"bullish breakout: close {close} > vwap {vwap:.2f}+buf and > high {prior_high}",
                )
            )
        elif close < vwap - buf and prior_low is not None and close < prior_low:
            signals.append(
                Signal(
                    underlying=underlying,
                    side=Side.BUY,
                    option_type=OptionType.PE,
                    reference_price=close,
                    timestamp=candle.end,
                    reason=f"bearish breakout: close {close} < vwap {vwap:.2f}-buf and < low {prior_low}",
                )
            )
        return signals
