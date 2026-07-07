"""Secret loading for Kotak Neo credentials (TOTP flow — required by the SDK).

The Kotak Neo SDK authenticates via TOTP only: ``totp_login(mobile_number, ucc, totp)`` then
``totp_validate(mpin)``. Required credentials: consumer key, mobile number, UCC (client code),
a base32 TOTP secret, and MPIN. (``consumer_secret``/``pan``/``password`` are accepted for
compatibility but are not used by this API version.)

Secrets are loaded from environment / ``.env`` and are NEVER logged; ``repr``/``str`` redact.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Credentials required for the TOTP login flow.
_REQUIRED = ("consumer_key", "mobile", "ucc", "mpin", "totp_secret")


class KotakSecrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOTAK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    consumer_key: SecretStr = SecretStr("")
    mobile: SecretStr = SecretStr("")  # registered mobile, e.g. +91XXXXXXXXXX
    ucc: SecretStr = SecretStr("")  # Unique Client Code
    mpin: SecretStr = SecretStr("")
    totp_secret: SecretStr = SecretStr("")  # base32 seed used to generate the 6-digit TOTP
    # Accepted for compatibility / other SDK versions but unused by the current TOTP API:
    consumer_secret: SecretStr = SecretStr("")
    pan: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")

    def _val(self, name: str) -> str:
        return getattr(self, name).get_secret_value().strip()

    def is_complete(self) -> bool:
        return all(self._val(f) for f in _REQUIRED)

    def missing_fields(self) -> list[str]:
        return [name for name in _REQUIRED if not self._val(name)]

    def __repr__(self) -> str:  # never leak values
        return f"KotakSecrets(complete={self.is_complete()}, missing={self.missing_fields()})"

    __str__ = __repr__


def load_secrets() -> KotakSecrets:
    """Load secrets from the environment / .env file."""
    load_dotenv(os.getenv("ALGO_ENV_FILE", ".env"), override=False)
    return KotakSecrets()
