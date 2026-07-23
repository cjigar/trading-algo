"""Option-chain manager: track ATM, subscribe the ATM ±N window, and capture OI/LTP.

Given the live NIFTY spot it resolves ATM (with hysteresis to avoid flapping at a strike
boundary), resolves the ATM ±window CE/PE contracts, and drives dynamic subscribe/unsubscribe as
the window shifts. It maintains the latest chain state (OI/LTP/volume per strike/side) and a
per-option session VWAP, and streams snapshots to the batched writer.

The subscribe callback and snapshot writer are injected so the manager is testable without a live
broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from algo_trading.analytics.greeks import Greeks, compute_greeks, implied_forward, year_fraction
from algo_trading.domain.enums import ExchangeSegment, OptionType, Underlying
from algo_trading.domain.models import Instrument, Tick
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.observability.logging import get_logger
from algo_trading.strategy.indicators import TickVWAP

log = get_logger("feed.option_chain")

SubscribeFn = Callable[[str, ExchangeSegment], None]


@dataclass
class ChainQuote:
    instrument: Instrument
    oi: int | None = None
    ltp: Decimal = Decimal(0)
    volume: int | None = None
    greeks: Greeks | None = None


class OptionChainManager:
    def __init__(
        self,
        settings,
        resolver: WeeklyOptionResolver,
        *,
        subscribe: SubscribeFn | None = None,
        snapshot_writer=None,
        underlying: Underlying = Underlying.NIFTY,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._subscribe = subscribe or (lambda _t, _s: None)
        self._writer = snapshot_writer
        self._underlying = underlying
        self._step = settings.strike_step_for(underlying)
        self._window = settings.feed_window()  # capture/subscribe window (>= OI band)
        self._oi_band = settings.strike_window  # strikes each side of ATM the strategy aggregates
        self._hysteresis = self._step * Decimal("0.2")

        self._spot: Decimal | None = None
        self._atm: Decimal | None = None
        self._chain: dict[str, Instrument] = {}  # token -> instrument (current window)
        self._quotes: dict[str, ChainQuote] = {}  # token -> latest quote
        self._vwap: dict[str, TickVWAP] = {}
        self._last_volume: dict[str, int] = {}

    # -- Index spot -> ATM window ------------------------------------------------------

    def on_index_tick(self, tick: Tick) -> None:
        self._spot = tick.ltp
        new_atm = self._resolver.atm_strike(self._underlying, tick.ltp, self._step)
        if self._atm is None or new_atm != self._atm and abs(tick.ltp - self._atm) > (self._step / 2 + self._hysteresis):
            self._set_atm(new_atm)

    def _set_atm(self, atm: Decimal) -> None:
        self._atm = atm
        self._rewindow()

    def _rewindow(self) -> None:
        if self._atm is None:
            return
        desired = self._resolver.chain(self._underlying, self._atm, self._window, self._step)
        desired_by_token = {i.instrument_token: i for i in desired}
        new_tokens = [t for t in desired_by_token if t not in self._chain]
        for token in new_tokens:
            inst = desired_by_token[token]
            self._chain[token] = inst
            self._subscribe(token, inst.exchange_segment)
        # drop tokens that left the window (stop tracking; leave any position feeds alone)
        for token in list(self._chain):
            if token not in desired_by_token:
                self._chain.pop(token, None)
        if new_tokens:
            log.info("chain_rewindowed", atm=str(self._atm), subscribed=len(new_tokens),
                     window_size=len(self._chain))

    def _atm_forward(self, r: float, T: float) -> float | None:
        """Parity forward from the ATM CE/PE quotes currently held (None until both have ticked)."""
        if self._atm is None or T <= 0:
            return None
        ce = pe = None
        for q in self._quotes.values():
            if q.instrument.strike == self._atm and q.ltp > 0:
                if q.instrument.option_type is OptionType.CE:
                    ce = float(q.ltp)
                else:
                    pe = float(q.ltp)
        if ce is None or pe is None:
            return None
        return implied_forward(ce, pe, float(self._atm), r, T)

    def _greeks_for_tick(self, inst: Instrument, tick: Tick) -> Greeks | None:
        """Greeks for the just-ticked option; None whenever any input is unavailable."""
        try:
            if tick.ltp is None or tick.ltp <= 0:
                return None
            r = float(self._settings.risk_free_rate)
            T = year_fraction(tick.timestamp, inst.expiry)
            F = self._atm_forward(r, T)
            if F is None:
                return None
            return compute_greeks(float(tick.ltp), F, float(inst.strike), r, T, inst.option_type)
        except Exception:  # noqa: BLE001 - greeks must never break the feed loop
            log.warning("greeks_compute_failed", token=tick.instrument_token)
            return None

    # -- Option quotes -----------------------------------------------------------------

    def on_option_tick(self, tick: Tick) -> None:
        inst = self._chain.get(tick.instrument_token)
        if inst is None:
            return  # not a chain contract we're tracking
        greeks = self._greeks_for_tick(inst, tick)
        self._quotes[tick.instrument_token] = ChainQuote(
            instrument=inst, oi=tick.oi, ltp=tick.ltp, volume=tick.volume, greeks=greeks
        )
        # volume-weighted VWAP by per-tick volume delta (fallback: equal weight)
        weight: Decimal | None = None
        if tick.volume is not None:
            prev = self._last_volume.get(tick.instrument_token)
            self._last_volume[tick.instrument_token] = tick.volume
            if prev is not None:
                weight = Decimal(max(0, tick.volume - prev))
        vwap = self._vwap.setdefault(tick.instrument_token, TickVWAP())
        vwap.update(tick.ltp, weight)

        if self._writer is not None:
            self._writer.add(
                {
                    "underlying": inst.underlying.value, "strike": str(inst.strike),
                    "option_type": inst.option_type.value, "instrument_token": tick.instrument_token,
                    "oi": tick.oi, "ltp": str(tick.ltp), "volume": tick.volume,
                    "vwap": str(vwap.value) if vwap.value is not None else None,
                    "expiry": inst.expiry,
                    "iv": str(greeks.iv) if greeks else None,
                    "delta": str(greeks.delta) if greeks else None,
                    "gamma": str(greeks.gamma) if greeks else None,
                    "theta": str(greeks.theta) if greeks else None,
                    "vega": str(greeks.vega) if greeks else None,
                    "timestamp": tick.timestamp,
                }
            )

    # -- Accessors ---------------------------------------------------------------------

    @property
    def atm(self) -> Decimal | None:
        return self._atm

    def chain_state(self) -> list[ChainQuote]:
        return list(self._quotes.values())

    def aggregate_oi(self) -> tuple[int, int]:
        """(total CE OI, total PE OI) across the strategy's ATM ±strike_window band. The capture
        window may be wider (for the chain view); OI aggregation stays on the central band."""
        band = self._oi_band * self._step
        in_band = [
            q for q in self._quotes.values()
            if self._atm is not None and abs(q.instrument.strike - self._atm) <= band
        ]
        ce = sum(q.oi or 0 for q in in_band if q.instrument.option_type is OptionType.CE)
        pe = sum(q.oi or 0 for q in in_band if q.instrument.option_type is OptionType.PE)
        return ce, pe

    def vwap_for(self, instrument_token: str) -> Decimal | None:
        v = self._vwap.get(instrument_token)
        return v.value if v else None

    def greeks_for(self, instrument_token: str) -> Greeks | None:
        q = self._quotes.get(instrument_token)
        return q.greeks if q else None

    def reset_session(self) -> None:
        for v in self._vwap.values():
            v.reset()
        self._last_volume.clear()
