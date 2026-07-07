"""Secret loading for Kotak Neo credentials (TOTP flow — required by the API).

Per the Kotak Neo docs, the Trade API authenticates via TOTP:
    NeoAPI(environment, access_token=<NEO-app token>, neo_fin_key="neotradeapi")
    -> totp_login(mobile_number, ucc, totp)   # totp = 6-digit code
    -> totp_validate(mpin)

Required credentials: an access token (from the NEO app: Invest → Trade API → Your
Applications → copy token), mobile number, UCC (client code), MPIN, and a TOTP source —
either a base32 ``KOTAK_TOTP_SECRET`` (for unattended runs via pyotp) or a current 6-digit
``KOTAK_TOTP`` (for a one-shot manual run; expires in ~30s).

Secrets are loaded from environment / ``.env`` and are NEVER logged; ``repr``/``str`` redact.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REQUIRED = ("access_token", "mobile", "ucc", "mpin")


class KotakSecrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOTAK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    access_token: SecretStr = SecretStr("")  # NEO-app token (Authorization header)
    mobile: SecretStr = SecretStr("")  # registered mobile, e.g. +91XXXXXXXXXX
    ucc: SecretStr = SecretStr("")  # Unique Client Code
    mpin: SecretStr = SecretStr("")
    totp_secret: SecretStr = SecretStr("")  # base32 seed -> pyotp generates the 6-digit code
    totp: SecretStr = SecretStr("")  # OR a current 6-digit code for a one-shot manual run
    # Accepted for compatibility / older SDK builds but not required by the TOTP API:
    consumer_key: SecretStr = SecretStr("")
    consumer_secret: SecretStr = SecretStr("")

    def _val(self, name: str) -> str:
        return getattr(self, name).get_secret_value().strip()

    def has_totp_source(self) -> bool:
        return bool(self._val("totp_secret") or self._val("totp"))

    def is_complete(self) -> bool:
        return all(self._val(f) for f in _REQUIRED) and self.has_totp_source()

    def missing_fields(self) -> list[str]:
        missing = [name for name in _REQUIRED if not self._val(name)]
        if not self.has_totp_source():
            missing.append("totp_secret_or_totp")
        return missing

    def __repr__(self) -> str:  # never leak values
        return f"KotakSecrets(complete={self.is_complete()}, missing={self.missing_fields()})"

    __str__ = __repr__


def load_secrets() -> KotakSecrets:
    """Load secrets from the environment / .env file."""
    load_dotenv(os.getenv("ALGO_ENV_FILE", ".env"), override=False)
    return KotakSecrets()
