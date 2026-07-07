"""Weekly option contract resolution.

Given an underlying, current spot, side, option type, and a strike-selection rule, picks the
current-week option contract (nearest non-expired weekly expiry) and returns its resolved
:class:`Instrument`. Strike-step inference and ATM/OTM selection are pure and unit-testable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from algo_trading.domain.enums import OptionType, StrikeSelection, Underlying
from algo_trading.domain.models import Instrument
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.observability.logging import get_logger

log = get_logger("instruments.resolver")

# Fallback strike steps if a single expiry doesn't expose enough strikes to infer one.
_DEFAULT_STRIKE_STEP = {Underlying.NIFTY: Decimal("50"), Underlying.SENSEX: Decimal("100")}


class OptionResolutionError(RuntimeError):
    pass


class WeeklyOptionResolver:
    def __init__(self, scrip_master: ScripMaster) -> None:
        self._sm = scrip_master

    def current_week_expiry(self, underlying: Underlying, today: date | None = None) -> date | None:
        """Nearest expiry that is not before ``today`` (the current trading week's expiry)."""
        today = today or date.today()
        future = [e for e in self._sm.expiries(underlying) if e >= today]
        return future[0] if future else None

    def _strike_step(self, underlying: Underlying, expiry: date, option_type: OptionType) -> Decimal:
        strikes = self._sm.strikes(underlying, expiry, option_type)
        diffs = sorted({strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)})
        positive = [d for d in diffs if d > 0]
        return positive[0] if positive else _DEFAULT_STRIKE_STEP.get(underlying, Decimal("50"))

    @staticmethod
    def _atm_strike(spot: Decimal, step: Decimal) -> Decimal:
        # round spot to the nearest strike step
        steps = (spot / step).to_integral_value(rounding="ROUND_HALF_UP")
        return steps * step

    def resolve(
        self,
        underlying: Underlying,
        spot: Decimal,
        option_type: OptionType,
        selection: StrikeSelection = StrikeSelection.ATM,
        today: date | None = None,
    ) -> Instrument:
        """Resolve the current-week contract for the given parameters. Raises on no match."""
        expiry = self.current_week_expiry(underlying, today)
        if expiry is None:
            raise OptionResolutionError(f"No current-week expiry for {underlying.value}")

        step = self._strike_step(underlying, expiry, option_type)
        atm = self._atm_strike(spot, step)
        # OTM for a CE is a higher strike; OTM for a PE is a lower strike.
        direction = Decimal(1) if option_type is OptionType.CE else Decimal(-1)
        target_strike = atm + (direction * Decimal(selection.offset_steps) * step)

        instrument = self._sm.find(underlying, expiry, target_strike, option_type)
        if instrument is None:
            # snap to the nearest available strike for this expiry/type
            available = self._sm.strikes(underlying, expiry, option_type)
            if not available:
                raise OptionResolutionError(
                    f"No {option_type.value} strikes for {underlying.value} {expiry}"
                )
            nearest = min(available, key=lambda s: abs(s - target_strike))
            instrument = self._sm.find(underlying, expiry, nearest, option_type)
        if instrument is None:
            raise OptionResolutionError(
                f"Could not resolve {underlying.value} {option_type.value} "
                f"near strike {target_strike} for {expiry}"
            )
        log.info(
            "option_resolved",
            underlying=underlying.value,
            symbol=instrument.trading_symbol,
            strike=str(instrument.strike),
            expiry=str(expiry),
            selection=selection.value,
        )
        return instrument
