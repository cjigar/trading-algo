"""Typed application configuration.

All non-secret runtime configuration lives here, loaded from environment variables
(prefix ``ALGO_``) or a ``.env`` file. Strategy parameters carry PLACEHOLDER defaults
that MUST be confirmed with the operator before live trading (see README go-live checklist).
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from algo_trading.domain.enums import ProductType, StrikeSelection, TradingMode, Underlying


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALGO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Mode & environment ---
    mode: TradingMode = TradingMode.PAPER
    # Must equal "YES" (exactly) for live orders to be armed. Guards against accidental live runs.
    confirm_live: str = ""
    kotak_environment: str = "prod"

    # --- Persistence ---
    db_path: str = "data/algo.db"

    # --- Instruments ---
    # NoDecode: skip pydantic-settings' JSON parsing so the comma-split validator below handles
    # values like "NIFTY,SENSEX".
    underlyings: Annotated[list[Underlying], NoDecode] = Field(
        default_factory=lambda: [Underlying.NIFTY, Underlying.SENSEX]
    )

    # --- Strategy parameters (PLACEHOLDERS — confirm before live) ---
    candle_timeframe_minutes: int = 5
    strike_selection: StrikeSelection = StrikeSelection.ATM
    vwap_breakout_buffer: Decimal = Decimal("0")
    target_points: Decimal = Decimal("30")
    trail_points: Decimal = Decimal("10")
    stoploss_points: Decimal = Decimal("15")

    # --- Risk ---
    lots: int = 1
    daily_loss_cap: Decimal = Decimal("5000")  # absolute rupees (positive number)
    max_positions: int = 1
    max_trades_per_day: int = 5
    product_type: ProductType = ProductType.MIS
    flatten_on_kill_switch: bool = True

    # --- Timing (IST) ---
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    squareoff_time: time = time(15, 15)
    premarket_login_time: time = time(8, 45)
    stale_feed_seconds: int = 15

    # --- Order rate limiting & exchange limits ---
    max_orders_per_second_per_exchange: int = 10
    # Exchange freeze quantity: orders larger than this are split into multiple legs.
    # PLACEHOLDER — confirm the current per-underlying freeze qty with the exchange/operator.
    freeze_quantity: int = 1800

    @field_validator("underlyings", mode="before")
    @classmethod
    def _split_underlyings(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip().upper() for item in v.split(",") if item.strip()]
        return v

    @field_validator("market_open", "market_close", "squareoff_time", "premarket_login_time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str) and ":" in v:
            hh, mm = v.split(":")[:2]
            return time(int(hh), int(mm))
        return v

    @property
    def is_live(self) -> bool:
        return self.mode is TradingMode.LIVE

    @property
    def live_armed(self) -> bool:
        """Live orders are only armed when mode is live AND the explicit confirmation is set."""
        return self.is_live and self.confirm_live.strip().upper() == "YES"

    def must_set_before_live(self) -> list[str]:
        """Return a checklist of parameters the operator must consciously confirm before going live."""
        return [
            "candle_timeframe_minutes",
            "strike_selection",
            "vwap_breakout_buffer",
            "target_points",
            "trail_points",
            "stoploss_points",
            "lots",
            "daily_loss_cap",
            "max_positions",
            "max_trades_per_day",
            "product_type",
            "squareoff_time",
        ]


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
