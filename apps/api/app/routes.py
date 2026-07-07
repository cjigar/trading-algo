"""API routes: auth, read models, controls, config, and the SSE live stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from algo_trading.config.settings import EDITABLE_FIELDS, Settings, get_settings, save_overrides
from algo_trading.dashboard.state_bridge import StateBridge
from app.config import WebSettings, get_web_settings
from app.deps import get_bridge, get_engine_settings
from app.schemas import (
    ChainOut,
    OrderOut,
    PnLOut,
    PositionOut,
    StateOut,
    TradeOut,
    chain_out,
    orders_out,
    pnl_out,
    positions_out,
    state_out,
    trades_out,
)
from app.security import create_token, require_auth, verify_password

# --- Auth (public) -------------------------------------------------------------------

auth_router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    password: str


class TokenOut(BaseModel):
    token: str


@auth_router.post("/login", response_model=TokenOut)
def login(body: LoginIn, web: WebSettings = Depends(get_web_settings)) -> TokenOut:
    if not verify_password(body.password, web):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenOut(token=create_token(web))


# --- Protected API -------------------------------------------------------------------

api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["api"])


@api.get("/state", response_model=StateOut)
def get_state(settings: Settings = Depends(get_engine_settings), bridge: StateBridge = Depends(get_bridge)):
    return state_out(settings, bridge.read_state())


@api.get("/pnl", response_model=PnLOut)
def get_pnl(bridge: StateBridge = Depends(get_bridge)):
    return pnl_out(bridge.read_state())


@api.get("/positions", response_model=list[PositionOut])
def get_positions(bridge: StateBridge = Depends(get_bridge)):
    return positions_out(bridge.read_state())


@api.get("/orders", response_model=list[OrderOut])
def get_orders(bridge: StateBridge = Depends(get_bridge)):
    return orders_out(bridge.read_state())


@api.get("/trades", response_model=list[TradeOut])
def get_trades(bridge: StateBridge = Depends(get_bridge)):
    return trades_out(bridge.read_state())


@api.get("/chain", response_model=ChainOut)
def get_chain(
    underlying: str | None = None,
    settings: Settings = Depends(get_engine_settings),
    bridge: StateBridge = Depends(get_bridge),
):
    # Default to the underlying that trades today (SENSEX Wed/Thu, NIFTY else).
    if not underlying:
        active = settings.active_underlying_for_today()
        underlying = active.value if active else None
    return chain_out(bridge.chain(underlying), underlying)


# --- Controls (enqueue commands; never the broker order path) -------------------------


class ControlOut(BaseModel):
    ok: bool
    command: str


@api.post("/control/{command}", response_model=ControlOut)
def control(command: str, bridge: StateBridge = Depends(get_bridge)):
    actions = {"start": bridge.send_start, "stop": bridge.send_stop, "flatten": bridge.send_flatten}
    if command not in actions:
        raise HTTPException(status_code=400, detail=f"Unknown command '{command}'")
    actions[command]()
    return ControlOut(ok=True, command=command)


# --- Config (read effective tunables; edit whitelisted ones) --------------------------


@api.get("/config")
def get_config(settings: Settings = Depends(get_engine_settings)) -> dict[str, Any]:
    return {k: _jsonable(getattr(settings, k)) for k in sorted(EDITABLE_FIELDS)}


class ConfigIn(BaseModel):
    updates: dict[str, Any]


@api.put("/config")
def put_config(body: ConfigIn) -> dict[str, Any]:
    bad = [k for k in body.updates if k not in EDITABLE_FIELDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Not editable: {bad}")
    try:
        settings = save_overrides(body.updates)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return {k: _jsonable(getattr(settings, k)) for k in sorted(EDITABLE_FIELDS)}


# --- SSE live stream ------------------------------------------------------------------


def build_stream_payload() -> dict[str, Any]:
    """Assemble one live-stream snapshot (state + P&L + today's-underlying chain). Pure/testable."""
    settings = get_settings(reload=True)
    bridge = StateBridge(settings)
    state = bridge.read_state()
    active = settings.active_underlying_for_today()
    u = active.value if active else None
    return {
        "state": state_out(settings, state).model_dump(),
        "pnl": pnl_out(state).model_dump(),
        "chain": chain_out(bridge.chain(u), u).model_dump(),
    }


@api.get("/stream")
async def stream(web: WebSettings = Depends(get_web_settings)):
    async def gen():
        while True:
            yield f"data: {json.dumps(build_stream_payload())}\n\n"
            await asyncio.sleep(web.stream_interval_seconds)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _jsonable(v: Any) -> Any:
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value"):  # enum
        return v.value
    if isinstance(v, (int, float, bool, str)) or v is None:
        return v
    return str(v)  # Decimal, time, etc.
