"""Trading orchestrator: wires the full pipeline and owns the daily lifecycle.

Pipeline: tick -> candle builder -> strategy -> risk -> execution -> position/P&L tracker,
with exits driven off option ticks and an independent square-off. Runs in its own process
(never inside Streamlit). Mode gating selects the paper fill engine or the live Kotak broker;
live requires explicit confirmation before real orders are armed.
"""

from __future__ import annotations

import threading
from decimal import Decimal

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
from algo_trading.persistence.db import create_db_engine
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
    ) -> None:
        self._settings = settings or get_settings()
        self._secrets = secrets
        self._bus = EventBus()
        self._ltp: dict[str, Decimal] = {}  # instrument_token -> last ltp
        self._underlying_token: dict[str, Underlying] = {}  # index token -> underlying
        self._lock = threading.RLock()

        self._repo: Repository = repo or Repository(create_db_engine(self._settings.db_path))

        self._positions = PositionTracker()
        self._exits = ExitManager(self._settings)
        self._risk = RiskManager(self._settings, self._repo, self._positions)
        self._resolver = WeeklyOptionResolver(scrip_master)
        self._translator = SignalTranslator(self._settings, self._resolver)
        self._strategy = strategy or VwapBreakoutStrategy(self._settings)

        self._broker = broker or self._build_broker()
        self._orders = OrderManager(self._broker, self._repo, self._settings, on_fill=self._on_fill)

        self._candles: dict[str, CandleBuilder] = {
            _underlying_symbol(u): CandleBuilder(_underlying_symbol(u), self._settings.candle_timeframe_minutes)
            for u in self._settings.underlyings
        }
        self._exit_symbol_token: dict[str, str] = {}  # option token -> trading symbol

        self._wire_bus()

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
        from algo_trading.broker.auth import SessionManager
        from algo_trading.broker.kotak_client import KotakClient

        secrets = self._secrets or load_secrets()
        session = SessionManager(self._settings, secrets)
        neo = session.login()
        client = KotakClient(self._settings, neo_client=neo)
        self._session = session
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
        self._strategy.on_session_start()
        self._orders.reconcile()
        log.info("session_started", state=state.value, mode=self._settings.mode.value)
        return state

    def stop_session(self) -> None:
        self._risk.stop_session()
        log.info("session_stopped")

    # -- Tick handling -----------------------------------------------------------------

    def _handle_tick(self, tick: Tick) -> None:
        self._ltp[tick.instrument_token] = tick.ltp
        self._positions.on_price(tick.instrument_token, tick.ltp)

        # If this is an index tick, feed the underlying's candle builder.
        underlying = self._underlying_token.get(tick.instrument_token)
        if underlying is not None:
            symbol = _underlying_symbol(underlying)
            builder = self._candles.get(symbol)
            if builder is not None:
                closed = builder.add_tick(tick)
                if closed is not None:
                    self._bus.publish(Topic.CANDLE, closed)

        # Exit evaluation for any option position quoted by this tick.
        self._evaluate_exit(tick.instrument_token, tick.ltp)

        # Continuously evaluate the kill-switch on P&L moves.
        self._risk.evaluate_kill_switch()

    def _handle_candle(self, candle) -> None:
        signals = self._strategy.on_candle(candle)
        for signal in signals:
            self._handle_signal(signal)

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
        if trade.side is Side.BUY:
            # entry fill -> arm exits and remember the option token->symbol mapping
            self._exits.register(trade.instrument, trade.quantity, trade.price)
            self._exit_symbol_token[trade.instrument.instrument_token] = symbol
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

    # -- Square-off & control ----------------------------------------------------------

    def square_off_all(self, reason: str = "square_off") -> None:
        """Flatten every open position. Independent of strategy/feed health."""
        for position in self._positions.open_positions():
            token = position.instrument.instrument_token
            ltp = self._ltp.get(token)
            exit_req = self._translator.build_exit(position.instrument, position.quantity, ltp)
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
