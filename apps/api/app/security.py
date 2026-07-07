"""Single-user auth: password login -> signed JWT, and a bearer-token dependency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import WebSettings, get_web_settings

_bearer = HTTPBearer(auto_error=False)


def create_token(settings: WebSettings) -> str:
    exp = datetime.now(UTC) + timedelta(minutes=settings.token_ttl_minutes)
    return jwt.encode({"sub": "operator", "exp": exp}, settings.auth_secret, algorithm="HS256")


def verify_password(password: str, settings: WebSettings) -> bool:
    return bool(password) and password == settings.auth_password


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: WebSettings = Depends(get_web_settings),
) -> str:
    # Prefer the Authorization header; fall back to a ?token= query param (EventSource/SSE can't
    # send headers).
    raw = creds.credentials if creds is not None else request.query_params.get("token")
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(raw, settings.auth_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    return str(payload.get("sub", "operator"))
