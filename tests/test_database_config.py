"""Database URL resolution across SQLite (default) and Postgres (configured)."""

from __future__ import annotations

from algo_trading.config.settings import get_settings
from algo_trading.persistence.db import create_engine_from_settings


def _settings(**overrides):
    s = get_settings(reload=True)
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def test_default_resolves_to_sqlite():
    s = _settings(database_url="", db_path="data/x.db")
    assert s.resolved_database_url() == "sqlite:///data/x.db"
    assert s.uses_postgres is False


def test_explicit_postgres_url_wins():
    url = "postgresql+psycopg://algo:algo@db:5432/algo"
    s = _settings(database_url=url)
    assert s.resolved_database_url() == url
    assert s.uses_postgres is True


def test_create_engine_from_settings_sqlite(tmp_path):
    s = _settings(database_url="", db_path=str(tmp_path / "e.db"))
    engine = create_engine_from_settings(s)
    assert engine.dialect.name == "sqlite"
    # schema created -> tables present
    from sqlalchemy import inspect

    assert "orders" in inspect(engine).get_table_names()
