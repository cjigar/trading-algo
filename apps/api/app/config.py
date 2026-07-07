"""Web API settings (separate from the engine's ALGO_ settings)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEB_", env_file=".env", extra="ignore")

    # Single-user auth: the operator's password and a secret to sign tokens.
    auth_password: str = "changeme"
    auth_secret: str = "dev-insecure-secret-change-me"
    token_ttl_minutes: int = 720
    # Allowed browser origins for CORS (comma-separated). NoDecode + the validator below handle a
    # plain string value from the env (pydantic-settings would otherwise try to JSON-parse it).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    # How often the SSE stream pushes an update.
    stream_interval_seconds: float = 3.0

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


_web: WebSettings | None = None


def get_web_settings() -> WebSettings:
    global _web
    if _web is None:
        _web = WebSettings()
    return _web
