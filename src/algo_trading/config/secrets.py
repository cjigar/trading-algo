"""Secret loading for Kotak Neo credentials.

Secrets are loaded from environment / ``.env`` and are NEVER logged. ``repr``/``str``
are overridden to redact values so accidental logging cannot leak them.
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
    mobile: SecretStr = SecretStr("")
    ucc: SecretStr = SecretStr("")
    mpin: SecretStr = SecretStr("")
    totp_secret: SecretStr = SecretStr("")  # base32 seed for pyotp

    def is_complete(self) -> bool:
        """True when every credential required to authenticate is present."""
        return all(
            field.get_secret_value().strip()
            for field in (
                self.consumer_key,
                self.consumer_secret,
                self.mobile,
                self.ucc,
                self.mpin,
                self.totp_secret,
            )
        )

    def missing_fields(self) -> list[str]:
        return [
            name
            for name in ("consumer_key", "consumer_secret", "mobile", "ucc", "mpin", "totp_secret")
            if not getattr(self, name).get_secret_value().strip()
        ]

    def __repr__(self) -> str:  # never leak values
        return f"KotakSecrets(complete={self.is_complete()}, missing={self.missing_fields()})"

    __str__ = __repr__


def load_secrets() -> KotakSecrets:
    """Load secrets from the environment / .env file."""
    # Ensure .env is applied even if settings were constructed before it was present.
    load_dotenv(os.getenv("ALGO_ENV_FILE", ".env"), override=False)
    return KotakSecrets()
