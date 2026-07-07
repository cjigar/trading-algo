"""Secret loading for Kotak Neo credentials (password + MPIN 2FA flow).

Secrets are loaded from environment / ``.env`` and are NEVER logged. ``repr``/``str`` are
overridden to redact values so accidental logging cannot leak them.

Auth model: ``login(pan|mobilenumber, password)`` then ``session_2fa(OTP=mpin)``.
A login identifier is required — provide PAN (``KOTAK_PAN``) and/or mobile (``KOTAK_MOBILE``).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KotakSecrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOTAK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    consumer_key: SecretStr = SecretStr("")
    consumer_secret: SecretStr = SecretStr("")
    # Login identifier — PAN and/or mobile (at least one required).
    pan: SecretStr = SecretStr("")
    mobile: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")
    mpin: SecretStr = SecretStr("")  # used as the session_2fa OTP value
    # Optional: only needed if you later switch to the TOTP login flow.
    totp_secret: SecretStr = SecretStr("")

    def _val(self, name: str) -> str:
        return getattr(self, name).get_secret_value().strip()

    def has_login_identifier(self) -> bool:
        return bool(self._val("pan") or self._val("mobile"))

    def is_complete(self) -> bool:
        """True when every credential required for the password + MPIN flow is present."""
        return (
            all(self._val(f) for f in ("consumer_key", "consumer_secret", "password", "mpin"))
            and self.has_login_identifier()
        )

    def missing_fields(self) -> list[str]:
        missing = [
            name
            for name in ("consumer_key", "consumer_secret", "password", "mpin")
            if not self._val(name)
        ]
        if not self.has_login_identifier():
            missing.append("pan_or_mobile")
        return missing

    def login_identifier(self) -> tuple[str, str]:
        """Return (kind, value) for login — prefers PAN, falls back to mobile."""
        if self._val("pan"):
            return ("pan", self._val("pan"))
        return ("mobilenumber", self._val("mobile"))

    def __repr__(self) -> str:  # never leak values
        return f"KotakSecrets(complete={self.is_complete()}, missing={self.missing_fields()})"

    __str__ = __repr__


def load_secrets() -> KotakSecrets:
    """Load secrets from the environment / .env file."""
    load_dotenv(os.getenv("ALGO_ENV_FILE", ".env"), override=False)
    return KotakSecrets()
