"""Shared dependencies: engine settings + a StateBridge (read/control over the shared DB)."""

from __future__ import annotations

from algo_trading.config.settings import Settings, get_settings
from algo_trading.dashboard.state_bridge import StateBridge


def get_engine_settings() -> Settings:
    return get_settings(reload=True)  # pick up any persisted config overrides


def get_bridge() -> StateBridge:
    # The bridge reads state and writes control commands only — it holds no broker session.
    return StateBridge(get_settings(reload=True))
