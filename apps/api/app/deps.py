"""Shared dependencies: engine settings + a StateBridge (read/control over the shared DB)."""

from __future__ import annotations

from algo_trading.config.settings import Settings, get_settings
from algo_trading.dashboard.state_bridge import StateBridge


def get_engine_settings() -> Settings:
    return get_settings(reload=True)  # pick up any persisted config overrides


# One bridge per database URL, reused across requests. Each StateBridge owns a SQLAlchemy engine
# (and therefore a connection pool); building one per request — or, on the SSE stream, per tick per
# connected client — churns pools continuously for no benefit. Keyed by URL so a config change
# that repoints the database still takes effect.
_bridges: dict[str, StateBridge] = {}


def get_bridge() -> StateBridge:
    # The bridge reads state and writes control commands only — it holds no broker session.
    settings = get_settings(reload=True)
    key = str(settings.database_url)
    bridge = _bridges.get(key)
    if bridge is None:
        bridge = _bridges[key] = StateBridge(settings)
    return bridge
