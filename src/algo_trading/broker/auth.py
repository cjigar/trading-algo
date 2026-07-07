"""Kotak Neo session/auth management (TOTP flow — the only auth the SDK supports).

The installed Kotak Neo SDK (neo_api_client) exposes only ``totp_login`` / ``totp_validate``
(there is no password ``login``/``session_2fa``). Flow:
    NeoAPI(environment, consumer_key)
    -> totp_login(mobile_number, ucc, totp)   # totp is a 6-digit code from the base32 secret
    -> totp_validate(mpin)                     # yields the trade token required for orders

The TOTP is generated with ``pyotp`` from ``KOTAK_TOTP_SECRET`` so login is unattended. Sessions
are daily; the manager supports pre-market re-login and re-authentication after token expiry.
The two SDK calls are isolated in ``_do_login`` / ``_do_2fa`` for SDK-version differences.
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
        if secret:
            try:
                return pyotp.TOTP(secret).now()
            except Exception as exc:  # noqa: BLE001
                raise AuthError(f"Invalid KOTAK_TOTP_SECRET (must be base32): {exc}") from exc
        manual = self._secrets.totp.get_secret_value().strip()
        if manual:
            return manual  # one-shot: a code the operator pasted (expires in ~30s)
        raise AuthError("No TOTP source: set KOTAK_TOTP_SECRET (base32) or KOTAK_TOTP (6-digit).")

    def login(self) -> Any:
        """Perform totp_login -> totp_validate and return the authenticated client."""
        with self._lock:
            if not self._secrets.is_complete():
                raise AuthError(f"Missing Kotak credentials: {self._secrets.missing_fields()}")

            neo_cls = _load_neo_api()
            neo = self._build_client(neo_cls)

            login_resp = self._do_login(neo)
            self._register_tokens(login_resp)
            self._check_auth_response(login_resp, "totp_login")

            validate_resp = self._do_2fa(neo)
            self._register_tokens(validate_resp)
            self._check_auth_response(validate_resp, "totp_validate")

            self._neo = neo
            self._authenticated = True
            log.info("kotak_login_ok", environment=self._settings.kotak_environment)
            return neo

    def _build_client(self, neo_cls: Any) -> Any:
        """Construct NeoAPI. The SDK sends ``consumer_key`` as the login Authorization header,
        so the NEO-app access token must be passed there (its naming is misleading)."""
        return neo_cls(
            environment=self._settings.kotak_environment,
            access_token=None,  # not used by the TOTP login; session token is set post-validate
            neo_fin_key=self._settings.kotak_neo_fin_key,
            consumer_key=self._secrets.access_token.get_secret_value(),
        )

    @staticmethod
    def _check_auth_response(resp: Any, step: str) -> None:
        """Raise AuthError if a login/validate response is an error rather than a token."""
        if isinstance(resp, dict):
            if resp.get("error"):
                raise AuthError(f"{step} failed: {resp['error']}")
            data = resp.get("data")
            if isinstance(data, dict) and data.get("token"):
                return
            if resp.get("status") == "error" or resp.get("Error"):
                raise AuthError(f"{step} failed: {resp}")
        raise AuthError(f"{step} did not return a token: {resp!r}")

    def _do_login(self, neo: Any) -> Any:
        totp = self._current_totp()
        register_secret(totp)
        return neo.totp_login(
            mobile_number=self._secrets.mobile.get_secret_value(),
            ucc=self._secrets.ucc.get_secret_value(),
            totp=totp,
        )

    def _do_2fa(self, neo: Any) -> Any:
        return neo.totp_validate(mpin=self._secrets.mpin.get_secret_value())

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
