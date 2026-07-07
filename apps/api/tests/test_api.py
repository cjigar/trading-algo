"""FastAPI endpoint tests: auth, read models, controls, config, SSE."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from algo_trading.domain.enums import AlgoState, ExchangeSegment, OptionType, Side, Underlying
from algo_trading.domain.models import Instrument, Trade


def _inst(symbol="NIFTY23000CE"):
    return Instrument(underlying=Underlying.NIFTY, exchange_segment=ExchangeSegment.NSE_FO,
                      trading_symbol=symbol, instrument_token="1", expiry=date(2099, 1, 30),
                      strike=Decimal("23000"), option_type=OptionType.CE, lot_size=65)


def _trade(side, qty, price, symbol="NIFTY23000CE"):
    return Trade(client_tag=f"{side.value}{price}", broker_order_id="B", instrument=_inst(symbol),
                 side=side, quantity=qty, price=Decimal(price), timestamp=datetime(2025, 1, 15, 10, 0))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_auth_required(client):
    assert client.get("/api/state").status_code == 401


def test_login_wrong_password(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_state(client, auth, repo):
    repo.set_algo_state(AlgoState.RUNNING, "started")
    r = client.get("/api/state", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["algo_state"] == "RUNNING"
    assert body["mode"] == "paper"
    assert body["live_armed"] is False


def test_pnl_and_trades(client, auth, repo):
    repo.record_trade(_trade(Side.BUY, 65, "100"))
    repo.record_trade(_trade(Side.SELL, 65, "130"))
    pnl = client.get("/api/pnl", headers=auth).json()
    assert pnl["total_realized"] == 1950.0  # (130-100)*65
    trades = client.get("/api/trades", headers=auth).json()
    assert len(trades) == 2 and {t["side"] for t in trades} == {"B", "S"}


def test_chain(client, auth, repo):
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 5000, "ltp": "100", "volume": 10},
        {"underlying": "NIFTY", "strike": "23000", "option_type": "PE", "instrument_token": "p1",
         "oi": 1000, "ltp": "90", "volume": 10},
    ])
    chain = client.get("/api/chain", headers=auth).json()
    assert chain["ce_oi_total"] == 5000 and chain["pe_oi_total"] == 1000
    assert chain["selected_side"] == "CE"
    assert len(chain["per_strike"]) == 1


def test_control_enqueues_command(client, auth, repo):
    assert client.post("/api/control/stop", headers=auth).json()["ok"] is True
    cmds = [c.command for c in repo.pop_pending_commands()]
    assert "stop" in cmds
    assert client.post("/api/control/bogus", headers=auth).status_code == 400


def test_config_get_edit_and_validation(client, auth):
    cfg = client.get("/api/config", headers=auth).json()
    assert "lots" in cfg and "allowed_weekdays" in cfg
    # valid edit
    r = client.put("/api/config", headers=auth, json={"updates": {"lots": 4}})
    assert r.status_code == 200 and r.json()["lots"] == 4
    # non-editable field rejected
    assert client.put("/api/config", headers=auth, json={"updates": {"mode": "live"}}).status_code == 400
    # invalid value rejected
    assert client.put("/api/config", headers=auth, json={"updates": {"lots": "NaN"}}).status_code == 422


def test_stream_payload_builder(repo):
    # Test the SSE payload builder directly (the live endpoint streams this same dict forever).
    from app.routes import build_stream_payload

    from algo_trading.domain.enums import AlgoState
    repo.set_algo_state(AlgoState.HALTED, "x")
    payload = build_stream_payload()
    assert set(payload) == {"state", "pnl", "chain"}
    assert payload["state"]["algo_state"] == "HALTED"
