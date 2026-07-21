"""Trading orchestrator: wires the full pipeline and owns the daily lifecycle.

Pipeline: tick -> candle builder -> strategy -> risk -> execution -> position/P&L tracker,
with exits driven off option ticks and an independent square-off. Runs in its own process
(never inside Streamlit). Mode gating selects the paper fill engine or the live Kotak broker;
live requires explicit confirmation before real orders are armed.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC
from decimal import Decimal
from typing import Any

from algo_trading.broker.base import BrokerClient
from algo_trading.config.secrets import KotakSecrets, load_secrets
from algo_trading.config.settings import Settings, get_settings
from algo_trading.core.events import EventBus, Topic
from algo_trading.domain.enums import AlgoState, Side, TradingMode, Underlying
from algo_trading.domain.models import OrderEvent, Signal, Tick, Trade
from algo_trading.execution.exit_manager import ExitManager
from algo_trading.execution.order_manager import OrderManager
from algo_trading.execution.paper_broker import PaperBroker
from algo_trading.execution.position_tracker import PositionTracker
from algo_trading.execution.signal_translator import SignalTranslator
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.observability.logging import get_logger
from algo_trading.persistence.db import create_engine_from_settings
from algo_trading.persistence.repositories import Repository
from algo_trading.risk.risk_manager import RiskManager
from algo_trading.strategy.base import Strategy
from algo_trading.strategy.candle_builder import CandleBuilder
from algo_trading.strategy.vwap_breakout import VwapBreakoutStrategy

log = get_logger("core.orchestrator")


class LiveModeNotArmedError(RuntimeError):
    """Raised when live mode is requested without the explicit confirmation."""


def _underlying_symbol(underlying: Underlying) -> str:
    """Symbol used for the underlying's candle stream (index feed)."""
    return f"{underlying.value}-IDX"


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        scrip_master: ScripMaster,
        broker: BrokerClient | None = None,
        strategy: Strategy | None = None,
        secrets: KotakSecrets | None = None,
        repo: Repository | None = None,
        neo_client: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._secrets = secrets
        self._neo_client = neo_client  # authenticated Kotak client (live mode)
        self._coordinator: Any = None  # LiveFeedCoordinator, set by attach_live_feeds()
        self._last_feed_recovery: float | None = None  # monotonic ts of the last stale-feed reconnect
        self._bus = EventBus()
        self._ltp: dict[str, Decimal] = {}  # instrument_token -> last ltp
        self._underlying_token: dict[str, Underlying] = {}  # index token -> underlying
        self._lock = threading.RLock()

        self._repo: Repository = repo or Repository(create_engine_from_settings(self._settings))

        self._positions = PositionTracker()
        self._exits = ExitManager(self._settings)
        self._risk = RiskManager(self._settings, self._repo, self._positions)
        self._resolver = WeeklyOptionResolver(scrip_master)
        self._translator = SignalTranslator(self._settings, self._resolver)

        self._oi_mode = self._settings.strategy == "oi_selling"
        self._short_tokens: dict[str, Any] = {}  # option token -> Instrument (open shorts)
        self._oi_chains: dict[Underlying, Any] = {}  # underlying -> OptionChainManager
        self._oi_strategies: dict[Underlying, Any] = {}  # underlying -> OiSellingStrategy
        if self._oi_mode:
            from algo_trading.feed.option_chain import OptionChainManager
            from algo_trading.persistence.snapshot_writer import SnapshotWriter
            from algo_trading.strategy.oi_selling import OiSellingStrategy

            self._writer = SnapshotWriter(
                self._repo,
                flush_seconds=float(self._settings.snapshot_min_interval_seconds) or 2.0,
                min_interval_seconds=float(self._settings.snapshot_min_interval_seconds),
            )
            # One chain manager + strategy per configured underlying (each gated to its own days).
            for u in self._settings.oi_underlyings:
                chain = OptionChainManager(
                    self._settings, self._resolver, subscribe=self._subscribe_option,
                    snapshot_writer=self._writer, underlying=u,
                )
                self._oi_chains[u] = chain
                self._oi_strategies[u] = OiSellingStrategy(self._settings, chain, underlying=u)
            self._strategy: Any = None  # not used in OI mode
        else:
            self._strategy = strategy or VwapBreakoutStrategy(self._settings)

        self._broker = broker or self._build_broker()
        self._orders = OrderManager(self._broker, self._repo, self._settings, on_fill=self._on_fill)

        self._candles: dict[str, CandleBuilder] = {
            _underlying_symbol(u): CandleBuilder(_underlying_symbol(u), self._settings.candle_timeframe_minutes)
            for u in self._settings.underlyings
        }
        self._exit_symbol_token: dict[str, str] = {}  # option token -> trading symbol

        self._wire_bus()

    def _subscribe_option(self, token: str, segment) -> None:
        """Subscribe an option's quotes if a live coordinator is attached (chain manager callback)."""
        if self._coordinator is not None:
            self._coordinator.subscribe_option(token, segment)

    # -- Mode gating -------------------------------------------------------------------

    def _build_broker(self) -> BrokerClient:
        if self._settings.mode is TradingMode.LIVE:
            if not self._settings.live_armed:
                raise LiveModeNotArmedError(
                    "ALGO_MODE=live requires ALGO_CONFIRM_LIVE=YES to arm real orders."
                )
            return self._build_live_broker()
        log.info("broker_paper_mode")
        return PaperBroker(ltp_provider=lambda token: self._ltp.get(token))

    def _build_live_broker(self) -> BrokerClient:  # pragma: no cover - needs the SDK + creds
        from algo_trading.broker.kotak_client import KotakClient

        if self._neo_client is None:
            # No pre-authenticated client supplied: log in here.
            from algo_trading.broker.auth import SessionManager

            session = SessionManager(self._settings, self._secrets or load_secrets())
            self._neo_client = session.login()
            self._session = session
        client = KotakClient(self._settings, neo_client=self._neo_client)
        log.warning("broker_live_mode_armed")
        return client

    # -- Bus wiring --------------------------------------------------------------------

    def _wire_bus(self) -> None:
        self._bus.subscribe(Topic.TICK, self._handle_tick)
        self._bus.subscribe(Topic.CANDLE, self._handle_candle)
        self._bus.subscribe(Topic.ORDER_EVENT, self._orders.handle_event)

    # -- Public entry points -----------------------------------------------------------

    def publish_tick(self, tick: Tick) -> None:
        self._bus.publish(Topic.TICK, tick)

    def publish_order_event(self, event: OrderEvent) -> None:
        self._bus.publish(Topic.ORDER_EVENT, event)

    def register_index_token(self, token: str, underlying: Underlying) -> None:
        self._underlying_token[token] = underlying

    def start_session(self) -> AlgoState:
        state = self._risk.start_session()
        if self._oi_mode:
            for strat in self._oi_strategies.values():
                strat.on_session_start()
        else:
            self._strategy.on_session_start()
        self._orders.reconcile()
        log.info("session_started", state=state.value, mode=self._settings.mode.value)
        return state

    def stop_session(self) -> None:
        self._risk.stop_session()
        log.info("session_stopped")

    def attach_live_feeds(self) -> bool:
        """Wire the live Kotak websockets into the pipeline. No-op without an authenticated
        client (paper mode). Returns True if the live feed was started."""
        if self._neo_client is None:
            log.info("live_feeds_skipped", reason="no authenticated client (paper mode)")
            return False
        from algo_trading.broker.live_feed import LiveFeedCoordinator

        coordinator = LiveFeedCoordinator(
            self._settings,
            self._neo_client,
            on_tick=self.publish_tick,
            on_order_event=self.publish_order_event,
        )
        # Map each configured index token to its underlying so index ticks build candles.
        subscribed = []
        for u in self._settings.underlyings:
            token = self._settings.index_token_for(u)
            if token:
                self.register_index_token(token, u)
                subscribed.append(u.value)
        coordinator.start()
        self._coordinator = coordinator
        if not subscribed:
            log.warning("no_index_tokens_configured",
                        hint="set ALGO_NIFTY_INDEX_TOKEN / ALGO_SENSEX_INDEX_TOKEN")
        log.info("live_feeds_attached", underlyings=subscribed)
        return True

    def feed_is_stale(self) -> bool:
        """True if the live quote feed has gone stale (used as a halt condition)."""
        return self._coordinator is not None and self._coordinator.is_stale()

    def recover_stale_feed(self, now: float | None = None) -> bool:
        """Reconnect the quote feed when it has gone quiet. Returns True if a reconnect was made.

        Call this from the process's main loop. It exists because a subscription issued before
        the websocket finishes connecting is silently dropped by the SDK — the feed then sits
        open and empty until the socket is torn down, which cost ~3 minutes of a live session in
        production. ``stale_feed_seconds`` decides "quiet for too long"; attempts are spaced by
        ``feed_recover_cooldown_seconds`` so a genuinely idle market (or a broker-side outage)
        cannot turn this into a reconnect loop.
        """
        if self._coordinator is None or not self._coordinator.is_stale():
            return False
        now = now if now is not None else time.monotonic()
        cooldown = self._settings.feed_recover_cooldown_seconds
        if self._last_feed_recovery is not None and (now - self._last_feed_recovery) < cooldown:
            return False
        self._last_feed_recovery = now
        log.warning("feed_stale_reconnecting", stale_seconds=self._settings.stale_feed_seconds)
        ok = self._coordinator.reconnect()
        log.info("feed_reconnect_result", ok=ok)
        return ok

    def flush_snapshots(self) -> int:
        """Flush any buffered option-chain snapshots to the DB (OI mode). Returns rows written."""
        if self._oi_mode and self._writer is not None:
            return self._writer.flush()
        return 0

    @property
    def is_oi_mode(self) -> bool:
        return self._oi_mode

    # -- Tick handling -----------------------------------------------------------------

    def _handle_tick(self, tick: Tick) -> None:
        self._ltp[tick.instrument_token] = tick.ltp
        self._positions.on_price(tick.instrument_token, tick.ltp)

        underlying = self._underlying_token.get(tick.instrument_token)

        if self._oi_mode:
            # Route to the per-underlying chain manager (index -> its ATM window; option -> all
            # chains, each ignores tokens outside its window).
            if underlying is not None:
                chain = self._oi_chains.get(underlying)
                if chain is not None:
                    chain.on_index_tick(tick)
            else:
                for chain in self._oi_chains.values():
                    chain.on_option_tick(tick)
                self._evaluate_short_vwap_exit(tick.instrument_token, tick.ltp)
        else:
            # Candle path for the VWAP-breakout strategy.
            if underlying is not None:
                builder = self._candles.get(_underlying_symbol(underlying))
                if builder is not None:
                    closed = builder.add_tick(tick)
                    if closed is not None:
                        self._bus.publish(Topic.CANDLE, closed)
            self._evaluate_exit(tick.instrument_token, tick.ltp)

        # Continuously evaluate the kill-switch on P&L moves.
        self._risk.evaluate_kill_switch()

    def _evaluate_short_vwap_exit(self, token: str, ltp: Decimal) -> None:
        inst = self._short_tokens.get(token)
        if inst is None:
            return
        chain = self._oi_chains.get(inst.underlying)
        vwap = chain.vwap_for(token) if chain is not None else None
        reason = self._exits.evaluate_short_vwap(inst.trading_symbol, ltp, vwap)
        if reason is not None:
            self._flatten_short(inst, ltp, reason.value)

    def _handle_candle(self, candle) -> None:
        signals = self._strategy.on_candle(candle)
        for signal in signals:
            self._handle_signal(signal)

    def evaluate_oi(self, now: Any = None) -> None:
        """Run each underlying's OI strategy once (called on a timer). No-op outside OI mode /
        when halted. Each strategy self-gates to its own weekdays (NIFTY Fri/Mon/Tue, SENSEX
        Wed/Thu), so only the day's underlying produces signals."""
        if not self._oi_mode or self._risk.is_halted():
            return
        from datetime import datetime

        when = now or datetime.now(UTC)
        for strat in self._oi_strategies.values():
            for signal in strat.evaluate(when):
                self._handle_short_signal(signal)

    def _handle_short_signal(self, signal: Signal) -> None:
        decision = self._risk.check_entry(signal)
        if not decision.allowed:
            log.info("entry_blocked", reason=decision.reason)
            return
        try:
            request = self._translator.translate(signal)
            ltp = self._ltp.get(request.instrument.instrument_token)
            if ltp is not None:
                request = self._translator.translate(signal, option_ltp=ltp)
        except Exception:  # noqa: BLE001
            log.exception("short_signal_translation_failed")
            return
        if not self._short_margin_ok(request):
            log.info("entry_blocked", reason="insufficient margin")
            return
        try:
            self._orders.submit(request)  # sell-to-open
        except Exception:  # noqa: BLE001 - a broker/submit failure must not kill the trading loop
            log.exception("short_order_submit_failed",
                          symbol=request.instrument.trading_symbol, quantity=request.quantity)
            return  # do not count a failed submit as an entry
        self._risk.register_entry()

    def _short_margin_ok(self, request) -> bool:
        """Margin pre-check for a short. Paper/unknown -> pass; live parsing is confirmed against
        the API in task 7.4 (margin response shape). Never blocks on a fetch error."""
        if not self._settings.is_live:
            return True
        try:
            required = self._broker.margin_required(request) if hasattr(self._broker, "margin_required") else Decimal(0)
            limits = self._broker.limits()
            available = Decimal(str(limits.get("Net", limits.get("net", 0)))) if isinstance(limits, dict) else Decimal(0)
            if required in (None, 0) or available == 0:
                return True  # can't determine -> don't block (flagged for live confirmation)
            return self._risk.margin_ok(Decimal(str(required)), available)
        except Exception:  # noqa: BLE001
            log.warning("margin_check_failed_passing")
            return True

    def _handle_signal(self, signal: Signal) -> None:
        decision = self._risk.check_entry(signal)
        if not decision.allowed:
            log.info("entry_blocked", reason=decision.reason, underlying=signal.underlying.value)
            return
        try:
            # resolve first to learn the option token, then look up its LTP for a marketable limit
            request = self._translator.translate(signal, option_ltp=None)
            ltp = self._ltp.get(request.instrument.instrument_token)
            if ltp is not None:
                request = self._translator.translate(signal, option_ltp=ltp)
        except Exception:  # noqa: BLE001 - resolution failure must not crash the loop
            log.exception("signal_translation_failed")
            return
        self._orders.submit(request)
        self._risk.register_entry()

    # -- Fills & exits -----------------------------------------------------------------

    def _on_fill(self, trade: Trade) -> None:
        self._positions.on_fill(trade)
        symbol = trade.instrument.trading_symbol

        if self._oi_mode:
            pos = self._positions.position_for(symbol)
            if pos is not None and pos.side is Side.SELL:
                # short is open -> arm the VWAP-cross exit and track its token
                self._exits.register_short_vwap(trade.instrument, pos.quantity, pos.average_price)
                self._short_tokens[trade.instrument.instrument_token] = trade.instrument
                if self._coordinator is not None:
                    self._coordinator.subscribe_option(
                        trade.instrument.instrument_token, trade.instrument.exchange_segment
                    )
            else:
                # position flat (bought back) -> stop tracking
                self._exits.unregister(symbol)
                self._short_tokens.pop(trade.instrument.instrument_token, None)
            return

        if trade.side is Side.BUY:
            # entry fill -> arm exits and remember the option token->symbol mapping
            self._exits.register(trade.instrument, trade.quantity, trade.price)
            self._exit_symbol_token[trade.instrument.instrument_token] = symbol
            # subscribe to the option's live quotes so exits get its LTP (live mode)
            if self._coordinator is not None:
                self._coordinator.subscribe_option(
                    trade.instrument.instrument_token, trade.instrument.exchange_segment
                )
        else:
            # exit fill -> stop tracking exits for this symbol
            self._exits.unregister(symbol)
            self._exit_symbol_token.pop(trade.instrument.instrument_token, None)

    def _evaluate_exit(self, token: str, ltp: Decimal) -> None:
        symbol = self._exit_symbol_token.get(token)
        if symbol is None:
            return
        reason = self._exits.evaluate(symbol, ltp)
        if reason is None:
            return
        self._flatten_symbol(symbol, ltp, reason.value)

    def _flatten_symbol(self, symbol: str, ltp: Decimal, reason: str) -> None:
        state = self._exits.state_for(symbol)
        if state is None:
            return
        log.info("exit_triggered", symbol=symbol, reason=reason, ltp=str(ltp))
        exit_req = self._translator.build_exit(state.instrument, state.quantity, ltp)
        self._exits.unregister(symbol)  # prevent duplicate exits while the order works
        self._orders.submit(exit_req)

    def _flatten_short(self, inst, ltp: Decimal, reason: str) -> None:
        state = self._exits.short_state_for(inst.trading_symbol)
        if state is None:
            return
        log.info("short_exit_triggered", symbol=inst.trading_symbol, reason=reason, ltp=str(ltp))
        exit_req = self._translator.build_exit(inst, state.quantity, ltp, position_side=Side.SELL)
        self._exits.unregister(inst.trading_symbol)  # prevent duplicate exits
        self._short_tokens.pop(inst.instrument_token, None)
        self._orders.submit(exit_req)  # buy-to-close

    # -- Square-off & control ----------------------------------------------------------

    def square_off_all(self, reason: str = "square_off") -> None:
        """Flatten every open position. Independent of strategy/feed health."""
        for position in self._positions.open_positions():
            token = position.instrument.instrument_token
            ltp = self._ltp.get(token)
            # close in the correct direction: SELL to close a long, BUY to close a short
            exit_req = self._translator.build_exit(
                position.instrument, position.quantity, ltp, position_side=position.side
            )
            self._exits.unregister(position.instrument.trading_symbol)
            self._short_tokens.pop(token, None)
            self._orders.submit(exit_req)
        self._repo.record_audit("square_off", reason)
        log.info("square_off_all", reason=reason)

    def process_control_commands(self) -> None:
        for cmd in self._repo.pop_pending_commands():
            log.info("control_command", command=cmd.command)
            if cmd.command == "stop":
                self._risk.manual_halt("dashboard stop")
                self.square_off_all("dashboard stop")
            elif cmd.command == "flatten":
                self.square_off_all("dashboard flatten")
            elif cmd.command == "start":
                self.start_session()

    # -- Introspection (used by tests & dashboard bridge) ------------------------------

    @property
    def positions(self) -> PositionTracker:
        return self._positions

    @property
    def risk(self) -> RiskManager:
        return self._risk

    @property
    def repo(self) -> Repository:
        return self._repo
