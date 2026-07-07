"""Kotak Neo session/auth management (TOTP-first).

Performs the two-step v2 flow: ``totp_login(mobile, ucc, totp)`` -> view token, then
``totp_validate(mpin)`` -> trade token (required for orders). The TOTP code is generated
from a stored base32 secret via ``pyotp`` so login is unattended. Sessions are daily; the
manager supports pre-market re-login and on-demand re-authentication after token expiry.
"""

from __future__ import annotations

import threading
from typing import Any

import pyotp

from algo_trading.broker.base import AuthError
from algo_trading.broker.kotak_client import _load_neo_api
from algo_trading.config.secrets import KotakSecrets
from algo_trading.config.settings import Settings
from algo_trading.observability.logging import get_logger, register_secret

log = get_logger("broker.auth")


class SessionManager:
    """Owns the authenticated ``NeoAPI`` client and its lifecycle."""

    def __init__(self, settings: Settings, secrets: KotakSecrets) -> None:
        self._settings = settings
        self._secrets = secrets
        self._neo: Any | None = None
        self._authenticated = False
        self._lock = threading.RLock()

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def client(self) -> Any:
        if self._neo is None or not self._authenticated:
            raise AuthError("Not authenticated. Call login() first.")
        return self._neo

    def _current_totp(self) -> str:
        secret = self._secrets.totp_secret.get_secret_value().strip()
        if not secret:
            raise AuthError("KOTAK_TOTP_SECRET is not set.")
        return pyotp.TOTP(secret).now()

    def login(self) -> Any:
        """Perform the full TOTP login -> validate flow and return the authenticated client."""
        with self._lock:
            if not self._secrets.is_complete():
                raise AuthError(f"Missing Kotak credentials: {self._secrets.missing_fields()}")

            neo_cls = _load_neo_api()
            neo = neo_cls(
                environment=self._settings.kotak_environment,
                consumer_key=self._secrets.consumer_key.get_secret_value(),
                consumer_secret=self._secrets.consumer_secret.get_secret_value(),
                access_token=None,
                neo_fin_key=None,
            )

            totp = self._current_totp()
            register_secret(totp)
            login_resp = neo.totp_login(
                mobilenumber=self._secrets.mobile.get_secret_value(),
                ucc=self._secrets.ucc.get_secret_value(),
                totp=totp,
            )
            self._register_tokens(login_resp)

            validate_resp = neo.totp_validate(mpin=self._secrets.mpin.get_secret_value())
            self._register_tokens(validate_resp)

            self._neo = neo
            self._authenticated = True
            log.info("kotak_login_ok", environment=self._settings.kotak_environment)
            return neo

    def relogin(self) -> Any:
        """Force a fresh login (used pre-market and after mid-session token expiry)."""
        with self._lock:
            self._authenticated = False
            self._neo = None
            return self.login()

    def ensure_authenticated(self) -> Any:
        """Return a valid client, re-authenticating if the session was invalidated."""
        if self._authenticated and self._neo is not None:
            return self._neo
        return self.login()

    @staticmethod
    def _register_tokens(resp: Any) -> None:
        """Register any token-like values from a login response for log redaction."""
        if not isinstance(resp, dict):
            return
        data = resp.get("data", resp)
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and ("token" in key.lower() or "sid" in key.lower()):
                    register_secret(value)
