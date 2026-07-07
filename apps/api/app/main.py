"""FastAPI app: a thin, typed, read/control-only layer over the algo_trading engine.

Reuses StateBridge/reporting/Repository; holds NO broker session and places NO orders.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_web_settings
from app.routes import api, auth_router


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Algo API", version="0.1.0")
    web = get_web_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=web.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router)
    app.include_router(api)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
