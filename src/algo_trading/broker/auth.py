"""Kotak Neo session/auth management (password + MPIN 2FA flow).

Performs the login flow: ``login(pan|mobilenumber, password)`` -> view token, then
``session_2fa(OTP=mpin)`` -> trade token (required for orders). Sessions are daily; the
manager supports pre-market re-login and on-demand re-authentication after token expiry.

Note: the exact SDK kwarg names can vary slightly between Kotak Neo SDK versions. The two SDK
calls are isolated in ``_do_login``/``_do_2fa`` so they are the only places to adjust if your
installed SDK differs. If your account requires a dynamic OTP delivered to your phone (rather
than accepting the MPIN as the 2FA value), unattended login is not possible — switch to the
TOTP flow instead.
"""

from __future__ import annotations

import threading
from typing import Any

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

    def login(self) -> Any:
        """Perform the full login -> session_2fa flow and return the authenticated client."""
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

            login_resp = self._do_login(neo)
            self._register_tokens(login_resp)

            validate_resp = self._do_2fa(neo)
            self._register_tokens(validate_resp)

            self._neo = neo
            self._authenticated = True
            log.info("kotak_login_ok", environment=self._settings.kotak_environment)
            return neo

    def _do_login(self, neo: Any) -> Any:
        """Call the SDK login with PAN (preferred) or mobile + password."""
        kind, value = self._secrets.login_identifier()
        password = self._secrets.password.get_secret_value()
        log.info("kotak_login_attempt", identifier_kind=kind)
        return neo.login(**{kind: value, "password": password})

    def _do_2fa(self, neo: Any) -> Any:
        """Complete 2FA using the MPIN as the OTP value."""
        return neo.session_2fa(OTP=self._secrets.mpin.get_secret_value())

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
