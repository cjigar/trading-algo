"""Web API settings (separate from the engine's ALGO_ settings)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEB_", env_file=".env", extra="ignore")

    # Single-user auth: the operator's password and a secret to sign tokens.
    auth_password: str = "changeme"
    auth_secret: str = "dev-insecure-secret-change-me"
    token_ttl_minutes: int = 720
    # Allowed browser origins for CORS (comma-separated).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    # How often the SSE stream pushes an update.
    stream_interval_seconds: float = 3.0

    def _split(self, v):  # noqa: ANN001
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v


_web: WebSettings | None = None


def get_web_settings() -> WebSettings:
    global _web
    if _web is None:
        s = WebSettings()
        if isinstance(s.cors_origins, str):
            object.__setattr__(s, "cors_origins", s._split(s.cors_origins))
        _web = s
    return _web
