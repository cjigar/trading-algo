"""Test setup for the FastAPI app: isolated temp DB + web auth, before importing the app."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# Configure the environment BEFORE importing the app (settings are read at import time).
os.environ["WEB_AUTH_PASSWORD"] = "testpass"
os.environ["WEB_AUTH_SECRET"] = "test-secret"
os.environ["WEB_STREAM_INTERVAL_SECONDS"] = "0.1"
os.environ["ALGO_MODE"] = "paper"
os.environ["ALGO_DATABASE_URL"] = ""
os.environ["ALGO_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["ALGO_OVERRIDES_PATH"] = tempfile.mktemp(suffix=".json")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # apps/api on the path

import pytest  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from algo_trading.config.settings import get_settings  # noqa: E402
from algo_trading.persistence.db import create_engine_from_settings  # noqa: E402
from algo_trading.persistence.repositories import Repository  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth(client: TestClient) -> dict:
    token = client.post("/api/login", json={"password": "testpass"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def repo() -> Repository:
    return Repository(create_engine_from_settings(get_settings(reload=True)))
