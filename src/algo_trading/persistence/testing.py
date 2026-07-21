"""Helpers for running tests against a real PostgreSQL/TimescaleDB server.

There is no in-process backend any more, so the suite provisions a throwaway database on the
server named by ``ALGO_TEST_DATABASE_URL`` (default: the compose ``db`` service published by
``make db-up``), bootstraps the full schema into it, and drops it at the end of the session.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://algo:algo@localhost:55432/algo"

_NO_SERVER_HINT = (
    "Could not connect to PostgreSQL at {url}.\n"
    "The test suite requires a running TimescaleDB instance — start one with `make db-up` "
    "(or point ALGO_TEST_DATABASE_URL at your own server)."
)


class SchemaTuning:
    """Bootstrap knobs for tests, standing in for ``Settings``.

    Retention and compression default to effectively "never": test fixtures write snapshots with
    historical timestamps (2025-01-15 and similar), and TimescaleDB's background retention job
    would otherwise drop those chunks mid-test. Tests that assert on the policies themselves pass
    their own values.
    """

    def __init__(
        self,
        *,
        retention_days: int = 36_500,
        compress_after_days: int = 36_500,
        chunk_days: int = 1,
        bucket_seconds: int = 60,
    ) -> None:
        self.chain_retention_days = retention_days
        self.chain_compress_after_days = compress_after_days
        self.chain_chunk_interval_days = chunk_days
        self.chain_agg_bucket_seconds = bucket_seconds

    def as_env(self) -> dict[str, str]:
        """The same tuning as ``ALGO_``-prefixed environment variables, for tests that build
        settings from the environment (the FastAPI app reads them at import time)."""
        return {
            "ALGO_CHAIN_RETENTION_DAYS": str(self.chain_retention_days),
            "ALGO_CHAIN_COMPRESS_AFTER_DAYS": str(self.chain_compress_after_days),
            "ALGO_CHAIN_CHUNK_INTERVAL_DAYS": str(self.chain_chunk_interval_days),
            "ALGO_CHAIN_AGG_BUCKET_SECONDS": str(self.chain_agg_bucket_seconds),
        }


def admin_url() -> str:
    """Server URL the tests connect to in order to create/drop their own database."""
    return os.environ.get("ALGO_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def create_test_database(name: str) -> str:
    """Create (recreate, if left over from a crashed run) database ``name`` and return its URL."""
    admin = admin_url()
    engine = create_engine(admin, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:  # server not running / wrong credentials
        raise RuntimeError(_NO_SERVER_HINT.format(url=admin)) from exc
    finally:
        engine.dispose()
    # render_as_string(hide_password=False): str(URL) masks the password as "***".
    return make_url(admin).set(database=name).render_as_string(hide_password=False)


def drop_test_database(name: str) -> None:
    """Drop the throwaway database, ignoring a server that has already gone away."""
    engine = create_engine(admin_url(), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    except OperationalError:
        pass
    finally:
        engine.dispose()


def unique_database_name(suffix: str = "") -> str:
    """A per-process database name, so parallel suites (api + core) never collide."""
    return f"algo_test_{os.getpid()}{'_' + suffix if suffix else ''}"


def truncate_all(engine: Engine) -> None:
    """Empty every application table between tests (keeps hypertables and policies in place)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT LIKE '\\_timescaledb%'"
            )
        ).all()
        names = [f'"{r[0]}"' for r in rows]
        if names:
            conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
        conn.commit()
