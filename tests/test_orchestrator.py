"""End-to-end paper-mode pipeline test: ticks -> candle -> signal -> order -> fill -> exit.

This exercises the whole orchestrator in-process with the paper broker (no network/SDK). The
live-feed integration against Kotak is a separate, credential-gated step (task 11.3).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from algo_trading.config.settings import get_settings
from algo_trading.core.orchestrator import LiveModeNotArmedError, Orchestrator
from algo_trading.domain.enums import (
    ExchangeSegment,
    OptionType,
    TradingMode,
    Underlying,
)
from algo_trading.domain.models import Instrument, Tick
from algo_trading.instruments.option_resolver import WeeklyOptionResolver
from algo_trading.instruments.scrip_master import ScripMaster
from algo_trading.persistence.repositories import Repository
from algo_trading.strategy.vwap_breakout import VwapBreakoutStrategy

IST = ZoneInfo("Asia/Kolkata")
INDEX_TOKEN = "NIFTY-IDX-TOKEN"


def _nifty_chain(expiry, strikes) -> ScripMaster:
    instruments = []
    for strike in strikes:
        for ot in (OptionType.CE, OptionType.PE):
            instruments.append(
                Instrument(
                    underlying=Underlying.NIFTY,
                    exchange_segment=ExchangeSegment.NSE_FO,
                    trading_symbol=f"NIFTY{strike}{ot.value}",
                    instrument_token=f"{strike}-{ot.value}",
                    expiry=expiry,
                    strike=Decimal(strike),
                    option_type=ot,
                    lot_size=75,
                )
            )
    return ScripMaster(instruments)


def _settings():
    s = get_settings(reload=True)
    object.__setattr__(s, "strategy", "vwap_breakout")  # these tests exercise the candle-driven pipeline
    object.__setattr__(s, "mode", TradingMode.PAPER)
    object.__setattr__(s, "target_points", Decimal("30"))
    object.__setattr__(s, "trail_points", Decimal("10"))
    object.__setattr__(s, "stoploss_points", Decimal("15"))
    object.__setattr__(s, "max_positions", 1)
    object.__setattr__(s, "max_trades_per_day", 5)
    return s


def _index_tick(price, ts) -> Tick:
    return Tick(
        instrument_token=INDEX_TOKEN,
        exchange_segment=ExchangeSegment.NSE_FO,
        ltp=Decimal(price),
        timestamp=ts,
        is_index=True,
    )


def _option_tick(token, price, ts) -> Tick:
    return Tick(
        instrument_token=token,
        exchange_segment=ExchangeSegment.NSE_FO,
        ltp=Decimal(price),
        timestamp=ts,
    )


def _build(engine):
    sm = _nifty_chain(
        datetime(2025, 1, 30).date(), [str(s) for s in range(22800, 23400, 50)]
    )
    settings = _settings()
    orch = Orchestrator(
        settings,
        scrip_master=sm,
        strategy=VwapBreakoutStrategy(settings, breakout_window=2),
        repo=Repository(engine),
    )
    orch.register_index_token(INDEX_TOKEN, Underlying.NIFTY)
    return orch, sm


def test_live_mode_without_confirmation_refuses(engine):
    sm = _nifty_chain(datetime(2025, 1, 30).date(), ["23000"])
    settings = get_settings(reload=True)
    object.__setattr__(settings, "mode", TradingMode.LIVE)
    object.__setattr__(settings, "confirm_live", "")  # not armed
    with pytest.raises(LiveModeNotArmedError):
        Orchestrator(settings, scrip_master=sm, repo=Repository(engine))


@freeze_time("2025-01-27")
def test_paper_pipeline_entry_and_exit(engine):
    orch, sm = _build(engine)
    orch.start_session()
    assert orch.risk.state.value == "RUNNING"

    # resolve which option the strategy will buy, and pre-seed its LTP so the entry fills there
    resolver = WeeklyOptionResolver(sm)
    ce = resolver.resolve(Underlying.NIFTY, Decimal("23200"), OptionType.CE, today=datetime(2025, 1, 27).date())

    base = datetime(2025, 1, 15, 9, 30, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    # seed option LTP at 100 before the breakout so the marketable-limit entry is ~100
    orch.publish_tick(_option_tick(ce.instrument_token, "100", base))

    # index closes: flat then a decisive bullish breakout -> BUY CE on the 4th closed candle
    prices = ["23000", "23000", "23000", "23200", "23200"]
    for i, p in enumerate(prices):
        orch.publish_tick(_index_tick(p, base + timedelta(minutes=5 * i)))

    # entry happened: one open position, one entry trade recorded
    assert orch.positions.open_position_count() == 1
    trades = orch.repo.trades_for_day()
    assert len(trades) == 1
    assert trades[0].instrument.option_type is OptionType.CE

    # now drive the option price: up through target (arms trail), then back down to trip the trail
    t2 = base + timedelta(minutes=40)
    orch.publish_tick(_option_tick(ce.instrument_token, "140", t2))  # target(130) hit -> trail at 130
    orch.publish_tick(_option_tick(ce.instrument_token, "128", t2 + timedelta(seconds=5)))  # <=130 -> exit

    # exit filled: position flat, a second (SELL) trade recorded, realized P&L positive
    assert orch.positions.open_position_count() == 0
    assert len(orch.repo.trades_for_day()) == 2
    assert orch.positions.realized_pnl() > 0


@freeze_time("2025-01-27")
def test_square_off_all_flattens(engine):
    orch, sm = _build(engine)
    orch.start_session()
    resolver = WeeklyOptionResolver(sm)
    ce = resolver.resolve(Underlying.NIFTY, Decimal("23200"), OptionType.CE, today=datetime(2025, 1, 27).date())

    base = datetime(2025, 1, 15, 9, 30, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    orch.publish_tick(_option_tick(ce.instrument_token, "100", base))
    for i, p in enumerate(["23000", "23000", "23000", "23200", "23200"]):
        orch.publish_tick(_index_tick(p, base + timedelta(minutes=5 * i)))
    assert orch.positions.open_position_count() == 1

    orch.square_off_all("test")
    assert orch.positions.open_position_count() == 0


@freeze_time("2025-01-27")
def test_control_command_stop_halts_and_flattens(engine):
    orch, sm = _build(engine)
    orch.start_session()
    resolver = WeeklyOptionResolver(sm)
    ce = resolver.resolve(Underlying.NIFTY, Decimal("23200"), OptionType.CE, today=datetime(2025, 1, 27).date())
    base = datetime(2025, 1, 15, 9, 30, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    orch.publish_tick(_option_tick(ce.instrument_token, "100", base))
    for i, p in enumerate(["23000", "23000", "23000", "23200", "23200"]):
        orch.publish_tick(_index_tick(p, base + timedelta(minutes=5 * i)))

    orch.repo.enqueue_command("stop")
    orch.process_control_commands()
    assert orch.risk.is_halted() is True
    assert orch.positions.open_position_count() == 0
