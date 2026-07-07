"""OI-based option-selling strategy.

On each evaluation it reads the option-chain manager's latest state, compares aggregate CE OI vs
aggregate PE OI over the ATM ±window band, and SHORTS the higher-OI side at a strike ``otm_strikes``
out-of-the-money (CE → ATM + N·step, PE → ATM − N·step). Entries are gated to configured weekdays
(default Fri/Mon/Tue) and skipped on configured market holidays. Not candle-driven — the
orchestrator calls :meth:`evaluate` on a timer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import OptionType, Side, Underlying
from algo_trading.domain.models import Signal
from algo_trading.feed.option_chain import OptionChainManager
from algo_trading.observability.logging import get_logger

log = get_logger("strategy.oi_selling")
IST = ZoneInfo("Asia/Kolkata")


class OiSellingStrategy:
    name = "oi_selling"

    def __init__(
        self,
        settings: Settings,
        chain: OptionChainManager,
        underlying: Underlying = Underlying.NIFTY,
    ) -> None:
        self._settings = settings
        self._chain = chain
        self._underlying = underlying
        self._holidays: set[date] = {
            date.fromisoformat(d) for d in settings.market_holidays if d
        }

    def is_trading_day(self, now: datetime) -> bool:
        local = now.astimezone(IST)
        if local.date() in self._holidays:
            return False
        return local.weekday() in set(self._settings.allowed_weekdays)

    def on_session_start(self) -> None:
        self._chain.reset_session()

    def evaluate(self, now: datetime) -> list[Signal]:
        """Produce at most one short signal from the current chain state, or none."""
        if not self.is_trading_day(now):
            return []
        atm = self._chain.atm
        if atm is None:
            return []
        ce_oi, pe_oi = self._chain.aggregate_oi()
        if ce_oi == pe_oi:
            return []  # no dominant side

        step = self._settings.strike_step
        offset = Decimal(self._settings.otm_strikes) * step
        if ce_oi > pe_oi:
            option_type = OptionType.CE
            target_strike = atm + offset  # OTM call = higher strike
        else:
            option_type = OptionType.PE
            target_strike = atm - offset  # OTM put = lower strike

        signal = Signal(
            underlying=self._underlying,
            side=Side.SELL,  # short the option
            option_type=option_type,
            reference_price=atm,
            timestamp=now,
            target_strike=target_strike,
            reason=(
                f"OI sell: CE_OI={ce_oi} PE_OI={pe_oi} -> short {option_type.value} "
                f"@{target_strike} (ATM {atm} {'+' if option_type is OptionType.CE else '-'}"
                f"{self._settings.otm_strikes} strikes)"
            ),
        )
        log.info("oi_signal", side=option_type.value, strike=str(target_strike),
                 ce_oi=ce_oi, pe_oi=pe_oi)
        return [signal]
