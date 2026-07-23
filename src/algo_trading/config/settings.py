"""Typed application configuration.

All non-secret runtime configuration lives here, loaded from environment variables
(prefix ``ALGO_``) or a ``.env`` file. Strategy parameters carry PLACEHOLDER defaults
that MUST be confirmed with the operator before live trading (see README go-live checklist).
"""

from __future__ import annotations

import json
import os
from datetime import time
from decimal import Decimal
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from algo_trading.domain.enums import ProductType, StrikeSelection, TradingMode, Underlying


def is_postgres_url(url: str) -> bool:
    """True for SQLAlchemy URLs pointing at PostgreSQL (``postgresql://``, ``postgresql+psycopg://``,
    and the legacy ``postgres://`` alias)."""
    scheme = url.split("://", 1)[0].split("+", 1)[0].strip().lower()
    return scheme in {"postgresql", "postgres"}


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
    # Static value required by the Kotak Neo login APIs (neo-fin-key header).
    kotak_neo_fin_key: str = "neotradeapi"

    # --- Persistence ---
    # Full SQLAlchemy URL — PostgreSQL only, e.g.
    # postgresql+psycopg://user:pass@db:5432/algo. Required: there is no local/file fallback.
    database_url: str = ""
    # Retries when connecting to the DB on startup (lets Postgres finish booting in compose).
    db_connect_retries: int = 10
    # Time-series (TimescaleDB) tuning for the option-chain snapshot hypertable.
    chain_chunk_interval_days: int = 1  # one chunk per trading day
    chain_compress_after_days: int = 2  # compress chunks older than this
    chain_agg_bucket_seconds: int = 60  # continuous-aggregate bucket for OI-trend anchors

    # --- Instruments ---
    # NoDecode: skip pydantic-settings' JSON parsing so the comma-split validator below handles
    # values like "NIFTY,SENSEX".
    underlyings: Annotated[list[Underlying], NoDecode] = Field(
        default_factory=lambda: [Underlying.NIFTY, Underlying.BANKNIFTY, Underlying.SENSEX]
    )
    # Instrument tokens for the underlying INDEX spot LTP (from Kotak's index scrip list).
    # Required for the live quote feed to build candles — set before live/paper-with-feed runs.
    # BANKNIFTY is feed/display-only (not in oi_underlyings), so its token only drives the ticker.
    nifty_index_token: str = ""
    banknifty_index_token: str = ""
    sensex_index_token: str = ""

    # --- Strategy selection ---
    strategy: str = "oi_selling"  # oi_selling | vwap_breakout

    # --- Strategy parameters (PLACEHOLDERS — confirm before live) ---
    candle_timeframe_minutes: int = 5
    strike_selection: StrikeSelection = StrikeSelection.ATM
    vwap_breakout_buffer: Decimal = Decimal("0")
    target_points: Decimal = Decimal("30")
    trail_points: Decimal = Decimal("10")
    stoploss_points: Decimal = Decimal("15")

    # --- OI selling strategy (PLACEHOLDERS — confirm before live) ---
    # Underlyings the OI strategy trades (each gated to its own weekdays below).
    oi_underlyings: Annotated[list[Underlying], NoDecode] = Field(default_factory=lambda: [Underlying.NIFTY])
    # Underlyings whose option chain is CAPTURED into the snapshot store (data-only). A superset
    # of oi_underlyings: capturing an underlying does NOT arm trading it. Each captured underlying
    # must ALSO be in `underlyings` (that is the set whose index feed is subscribed) or its chain
    # captures nothing. Default: both indices.
    chain_capture_underlyings: Annotated[list[Underlying], NoDecode] = Field(
        default_factory=lambda: [Underlying.NIFTY, Underlying.SENSEX]
    )
    strike_window: int = 5  # strikes each side of ATM the strategy AGGREGATES OI over
    # strikes each side of ATM to subscribe/capture for the chain VIEW (±20 both indices = 166
    # scrips, within Kotak's 200/subscribe cap; strategy still aggregates OI over strike_window)
    chain_feed_window: int = 20
    otm_strikes: int = 3  # strikes OTM to sell (CE=ATM+3, PE=ATM-3)
    strike_step: Decimal = Decimal("50")  # NIFTY strike interval
    sensex_strike_step: Decimal = Decimal("100")  # SENSEX strike interval
    chain_eval_seconds: int = 30  # cadence for OI evaluation
    snapshot_min_interval_seconds: int = 2  # min gap between persisted snapshots per token
    # Cadence at which the loop publishes P&L + open-position prices for the dashboard process.
    pnl_snapshot_seconds: int = 5
    # Cadence at which the loop polls the broker account (positions/orders/trades) so the dashboard
    # shows the live Kotak account, not a boot-time snapshot. Broker reads are not rate-limited.
    broker_refresh_seconds: int = 5

    # --- Telegram alerting (bot token is a secret in env: TELEGRAM_BOT_TOKEN, never here) ---
    alerts_enabled: bool = False
    telegram_chat_id: str = ""  # numeric chat id to send to (not a secret)
    # Two-way control (/clear, /stop) via Telegram — a SEPARATE opt-in from alerting so enabling
    # notifications never silently enables remote control of a live account.
    telegram_commands_enabled: bool = False
    alert_trade_fills: bool = True  # include the per-fill trade tape (noisier)
    alert_throttle_seconds: int = 300  # collapse repeats of the same alert within this window
    alert_rate_limit_per_min: int = 20  # global cap; excess is coalesced into a "+N more" summary
    # How old a published quote may be before the dashboard refuses to mark positions against it
    # (a dead feed must read as stale, not as a frozen-but-plausible price).
    live_quote_max_age_seconds: int = 60
    # Look-back windows (minutes) for rolling OI-trend arrows on the chain. Each window compares
    # current OI to the OI at now-window. Keep the shortest window >= a few * the snapshot interval
    # above so it has prior anchor rows (at 2s spacing a 1-min window has ~30 anchors).
    oi_trend_windows: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [1, 3, 5, 15])
    # Absolute OI change (contracts) below which a window is classified Flat rather than Up/Down.
    oi_trend_flat_threshold: int = 0
    # Annualized risk-free rate used to price the put-call-parity forward and solve option IV.
    # ~India T-bill. Dividend yield is assumed 0 for the index.
    risk_free_rate: Decimal = Decimal("0.065")
    chain_retention_days: int = 14  # backstop retention: drop chain-snapshot chunks older than this
    # Retention model: "expiry" runs the app-level per-expiry purge (delete a week once its expiry
    # passes); "days" disables it and relies solely on the time-based chain_retention_days policy.
    chain_retention_mode: str = "expiry"
    margin_buffer: Decimal = Decimal("0")  # fraction of extra margin headroom required
    # Weekdays each underlying may take entries (Mon=0 … Sun=6).
    allowed_weekdays: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [4, 0, 1])  # NIFTY: Fri,Mon,Tue
    sensex_weekdays: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [2, 3])  # SENSEX: Wed,Thu
    # NSE trading holidays (ISO dates) on which the strategy takes no entries. Operator-supplied.
    market_holidays: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Risk / sizing ---
    lots: int = 1
    # Per-underlying lot size override (0 = use the scrip master's lot size). Set to force a known
    # contract size instead of trusting the scrip file.
    nifty_lot_size: int = 0
    sensex_lot_size: int = 0
    daily_loss_cap: Decimal = Decimal("5000")  # absolute rupees (positive number)
    max_positions: int = 1
    max_trades_per_day: int = 5
    product_type: ProductType = ProductType.MIS
    flatten_on_kill_switch: bool = True

    # --- Timing (IST) ---
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    squareoff_time: time = time(15, 15)
    # Daily fresh broker login runs here (Mon-Fri, non-holiday), before the 09:15 open, so the
    # feed is warm and authenticated at the bell. Kotak access tokens are day-scoped, so a
    # process that logged in yesterday must re-authenticate for today.
    premarket_login_time: time = time(9, 0)
    stale_feed_seconds: int = 15
    # Minimum gap between stale-feed reconnect attempts, so a quiet market or a broker-side
    # outage can never turn the watchdog into a reconnect loop.
    feed_recover_cooldown_seconds: int = 30
    # If the feed stays stale this long *during the trading window* despite the watchdog's
    # resubscribes, the broker session itself is dead (e.g. the day-scoped token expired) and a
    # resubscribe cannot fix it. The loop then re-execs for a fresh login. Well above
    # feed_recover_cooldown_seconds so the cheap reconnect always gets several tries first.
    feed_hard_recover_seconds: int = 120

    # --- Dashboard ---
    dashboard_refresh_seconds: int = 30  # auto-refresh interval for the Streamlit dashboard

    # --- Order rate limiting & exchange limits ---
    max_orders_per_second_per_exchange: int = 10
    # Exchange freeze quantity: orders larger than this are split into multiple legs.
    # PLACEHOLDER — confirm the current per-underlying freeze qty with the exchange/operator.
    freeze_quantity: int = 1800

    @field_validator("database_url")
    @classmethod
    def _require_postgres_url(cls, v: str) -> str:
        """Reject non-PostgreSQL URLs at construction. An empty value is allowed here (some
        entrypoints never touch the DB); :meth:`resolved_database_url` requires it to be set."""
        url = v.strip()
        if url and not is_postgres_url(url):
            raise ValueError(
                f"ALGO_DATABASE_URL must be a PostgreSQL URL; got {url!r}. "
                "SQLite and other backends are not supported."
            )
        return v

    @field_validator("underlyings", "oi_underlyings", "chain_capture_underlyings", mode="before")
    @classmethod
    def _split_underlyings(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip().upper() for item in v.split(",") if item.strip()]
        return v

    @field_validator("allowed_weekdays", "sensex_weekdays", mode="before")
    @classmethod
    def _parse_weekdays(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        names = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
        out: list[int] = []
        for item in v.split(","):
            token = item.strip().upper()
            if not token:
                continue
            out.append(names[token[:3]] if token[:3] in names else int(token))
        return out

    @field_validator("oi_trend_windows", mode="before")
    @classmethod
    def _parse_trend_windows(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return [int(item.strip()) for item in v.split(",") if item.strip()]

    @field_validator("market_holidays", mode="before")
    @classmethod
    def _split_holidays(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("market_open", "market_close", "squareoff_time", "premarket_login_time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str) and ":" in v:
            hh, mm = v.split(":")[:2]
            return time(int(hh), int(mm))
        return v

    def index_token_for(self, underlying: Underlying) -> str:
        """Configured index-spot instrument token for an underlying ('' if unset)."""
        tokens = {
            Underlying.NIFTY: self.nifty_index_token,
            Underlying.BANKNIFTY: self.banknifty_index_token,
            Underlying.SENSEX: self.sensex_index_token,
        }
        return tokens.get(underlying, "").strip()

    def strike_step_for(self, underlying: Underlying) -> Decimal:
        """Strike interval per underlying (NIFTY 50, SENSEX 100)."""
        return self.strike_step if underlying is Underlying.NIFTY else self.sensex_strike_step

    def weekdays_for(self, underlying: Underlying) -> list[int]:
        """Weekdays this underlying may take entries (NIFTY Fri/Mon/Tue, SENSEX Wed/Thu)."""
        return self.allowed_weekdays if underlying is Underlying.NIFTY else self.sensex_weekdays

    def feed_window(self) -> int:
        """Strikes each side of ATM to subscribe/capture for the chain (view window >= OI band)."""
        return self.chain_feed_window if self.chain_feed_window > 0 else self.strike_window

    def active_underlying_for_today(self, weekday: int | None = None) -> Underlying | None:
        """The OI underlying whose trading weekdays include today (SENSEX Wed/Thu, NIFTY else).
        Falls back to the first configured OI underlying if none matches."""
        import datetime as _dt
        from zoneinfo import ZoneInfo

        wd = weekday if weekday is not None else _dt.datetime.now(ZoneInfo("Asia/Kolkata")).weekday()
        for u in self.oi_underlyings:
            if wd in set(self.weekdays_for(u)):
                return u
        return self.oi_underlyings[0] if self.oi_underlyings else None

    def lot_size_for(self, underlying: Underlying) -> int:
        """Configured lot-size override for an underlying (0 = use the scrip master's lot size)."""
        return self.nifty_lot_size if underlying is Underlying.NIFTY else self.sensex_lot_size

    def effective_lot_size(self, underlying: Underlying, scrip_lot_size: int) -> int:
        """Lot size to use for sizing: the configured override if set, else the scrip's value."""
        override = self.lot_size_for(underlying)
        return override if override > 0 else scrip_lot_size

    def resolved_database_url(self) -> str:
        """Return the configured PostgreSQL URL.

        PostgreSQL is the only supported backend — there is no file/SQLite fallback — so a
        missing or non-Postgres URL is a configuration error raised here rather than a
        silently-degraded engine later.
        """
        url = self.database_url.strip()
        if not url:
            raise ValueError(
                "ALGO_DATABASE_URL is required (PostgreSQL only), e.g. "
                "postgresql+psycopg://algo:algo@localhost:5432/algo"
            )
        if not is_postgres_url(url):
            raise ValueError(
                f"ALGO_DATABASE_URL must be a PostgreSQL URL; got {url!r}. "
                "SQLite and other backends are not supported."
            )
        return url

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


# Tunable parameters the web config editor may override (NEVER secrets, mode, or live-arming).
EDITABLE_FIELDS: frozenset[str] = frozenset({
    "lots", "nifty_lot_size", "sensex_lot_size",
    "allowed_weekdays", "sensex_weekdays", "market_holidays",
    "target_points", "trail_points", "stoploss_points", "vwap_breakout_buffer",
    "strike_window", "chain_feed_window", "otm_strikes", "chain_eval_seconds",
    "daily_loss_cap", "max_positions", "max_trades_per_day", "flatten_on_kill_switch",
    "candle_timeframe_minutes", "strike_selection",
    "oi_trend_windows", "oi_trend_flat_threshold",
    "broker_refresh_seconds",
    "risk_free_rate",
})


def overrides_path() -> str:
    return os.getenv("ALGO_OVERRIDES_PATH", "data/overrides.json")


def load_overrides() -> dict:
    """Read the persisted config overrides (whitelisted tunables), if any."""
    path = overrides_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE_FIELDS}


def save_overrides(updates: dict) -> Settings:
    """Persist whitelisted override updates and return freshly-reloaded settings.

    Validates by constructing Settings with the merged overrides (raises on invalid values).
    """
    filtered = {k: v for k, v in updates.items() if k in EDITABLE_FIELDS}
    merged = {**load_overrides(), **filtered}
    Settings(**merged)  # validate before writing (raises pydantic ValidationError on bad input)
    path = overrides_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(merged, f, default=str, indent=2)
    return get_settings(reload=True)


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Return the process-wide settings singleton (env + persisted config overrides)."""
    global _settings
    if _settings is None or reload:
        _settings = Settings(**load_overrides())
    return _settings
