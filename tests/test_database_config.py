"""Database URL resolution: PostgreSQL only, no fallback backend."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from algo_trading.config.settings import Settings, get_settings
from algo_trading.persistence.db import create_engine_from_settings, create_engine_from_url


def _settings(**overrides):
    s = get_settings(reload=True)
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def test_postgres_url_accepted():
    url = "postgresql+psycopg://algo:algo@db:5432/algo"
    assert _settings(database_url=url).resolved_database_url() == url


def test_blank_url_rejected():
    s = _settings(database_url="")
    with pytest.raises(ValueError, match="ALGO_DATABASE_URL is required"):
        s.resolved_database_url()


def test_sqlite_url_rejected_by_settings_validator():
    with pytest.raises(ValidationError, match="must be a PostgreSQL URL"):
        Settings(database_url="sqlite:///data/x.db")


def test_sqlite_url_rejected_by_engine_factory():
    with pytest.raises(ValueError, match="Only PostgreSQL URLs are supported"):
        create_engine_from_url("sqlite:///data/x.db")


def test_create_engine_from_settings_bootstraps_schema(fresh_db):
    """Uses its own database: bootstrapping with production settings would re-apply the default
    retention policy to the shared session database."""
    s = _settings(database_url=fresh_db)
    engine = create_engine_from_settings(s)
    assert engine.dialect.name == "postgresql"
    assert "orders" in inspect(engine).get_table_names()
    engine.dispose()


def test_connect_retries_are_honored(monkeypatch):
    """A server that never accepts connections is retried `db_connect_retries` times, then fails."""
    from algo_trading.persistence import bootstrap

    attempts: list[int] = []
    monkeypatch.setattr(bootstrap._time, "sleep", lambda _s: None)
    real_autocommit = bootstrap._autocommit

    def failing(engine):
        attempts.append(1)
        return real_autocommit(engine)

    monkeypatch.setattr(bootstrap, "_autocommit", failing)
    s = _settings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/none", db_connect_retries=2
    )
    with pytest.raises(OperationalError):
        create_engine_from_settings(s)
    assert len(attempts) == 3  # initial attempt + 2 retries
