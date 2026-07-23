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
    assert body["strategy"] == "oi_selling"  # committed default


def test_state_spots_carry_day_change(client, auth, repo):
    repo.upsert_index_spots({"NIFTY": Decimal("23800")})
    repo.upsert_index_spots({"NIFTY": Decimal("23912")})  # +112 on the day
    body = client.get("/api/state", headers=auth).json()
    spots = {s["underlying"]: s for s in body["spots"]}
    assert spots["NIFTY"]["ltp"] == 23912.0
    assert spots["NIFTY"]["day_open"] == 23800.0
    assert spots["NIFTY"]["change"] == 112.0
    assert round(spots["NIFTY"]["change_pct"], 4) == round(112 / 23800 * 100, 4)
    assert spots["NIFTY"]["stale"] is False


def test_pnl_and_trades(client, auth, repo):
    repo.record_trade(_trade(Side.BUY, 65, "100"))
    repo.record_trade(_trade(Side.SELL, 65, "130"))
    pnl = client.get("/api/pnl", headers=auth).json()
    assert pnl["total_realized"] == 1950.0  # (130-100)*65
    trades = client.get("/api/trades", headers=auth).json()
    assert len(trades) == 2 and {t["side"] for t in trades} == {"B", "S"}


def test_chain(client, auth, repo):
    # day-open baseline snapshot (first of the day per token)
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 4000, "ltp": "100", "volume": 10},
        {"underlying": "NIFTY", "strike": "23000", "option_type": "PE", "instrument_token": "p1",
         "oi": 800, "ltp": "90", "volume": 10},
    ])
    # later snapshot: OI built up during the day
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 5000, "ltp": "100", "volume": 10},
        {"underlying": "NIFTY", "strike": "23000", "option_type": "PE", "instrument_token": "p1",
         "oi": 1000, "ltp": "90", "volume": 10},
    ])
    # Explicit underlying (the endpoint otherwise defaults to today's active underlying).
    chain = client.get("/api/chain", params={"underlying": "NIFTY"}, headers=auth).json()
    assert chain["underlying"] == "NIFTY"
    assert chain["ce_oi_total"] == 5000 and chain["pe_oi_total"] == 1000
    assert chain["selected_side"] == "CE"
    assert chain["atm"] == 23000.0
    assert len(chain["per_strike"]) == 1
    row = chain["per_strike"][0]
    assert row["is_atm"] is True
    assert row["ce_chg_oi"] == 1000 and row["pe_chg_oi"] == 200  # 5000-4000, 1000-800
    # trend fields present for every configured window on both sides
    assert set(row["ce_oi_trends"]) == {"1m", "3m", "5m", "15m"}
    assert set(row["pe_oi_trends"]) == {"1m", "3m", "5m", "15m"}
    # both snapshots were written ~now, so now-Nmin has no prior anchor -> na
    assert row["ce_oi_trends"]["1m"]["dir"] == "na"
    assert row["ce_oi_trends"]["1m"]["delta"] is None


def test_chain_oi_trends_with_history(client, auth, repo):
    from datetime import timedelta
    now = datetime.utcnow()
    # anchor snapshot 10 minutes ago (oi=4000) ...
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 4000, "ltp": "100", "volume": 10, "timestamp": now - timedelta(minutes=10)},
    ])
    # ... and the latest snapshot now (oi=5000)
    repo.write_chain_snapshots([
        {"underlying": "NIFTY", "strike": "23000", "option_type": "CE", "instrument_token": "c1",
         "oi": 5000, "ltp": "100", "volume": 10, "timestamp": now},
    ])
    row = client.get("/api/chain", params={"underlying": "NIFTY"}, headers=auth).json()["per_strike"][0]
    # now-1/3/5m all land after the 10-min-old anchor -> Up 1000; now-15m precedes it -> na
    assert row["ce_oi_trends"]["1m"] == {"dir": "up", "delta": 1000}
    assert row["ce_oi_trends"]["5m"] == {"dir": "up", "delta": 1000}
    assert row["ce_oi_trends"]["15m"]["dir"] == "na"


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
    # Everything that changes intraday rides the stream, so the dashboard never falls back on a
    # one-shot fetch that then sits stale.
    assert set(payload) == {
        "state", "pnl", "positions", "orders", "broker_pnl",
        "broker_positions", "broker_trades", "chain",
    }
    assert payload["state"]["algo_state"] == "HALTED"
    # chain payload carries per-strike trend fields identical in shape to /api/chain
    for row in payload["chain"]["per_strike"]:
        assert "ce_oi_trends" in row and "pe_oi_trends" in row


def test_pnl_reports_unrealized_from_published_quotes(client, auth, repo):
    # These tests share one database, so assert on the change this test causes, not on absolutes.
    before = client.get("/api/pnl", headers=auth).json()

    repo.record_trade(_trade(Side.BUY, 65, "100"))
    opened = client.get("/api/pnl", headers=auth).json()
    # Position is open but the loop has published no price for it, so it stays marked at its fill.
    assert opened["total_unrealized"] == before["total_unrealized"]

    repo.upsert_live_quotes({_inst().instrument_token: Decimal("130")})
    repo.record_pnl(Decimal("0"), Decimal("1950"))

    pnl = client.get("/api/pnl", headers=auth).json()
    assert pnl["total_unrealized"] == before["total_unrealized"] + 1950.0  # (130-100)*65
    assert pnl["day_pnl"] == pnl["total_realized"] + pnl["total_unrealized"]
    # The loop's own reading is carried alongside, with its age.
    assert pnl["engine"]["unrealized"] == 1950.0
    assert pnl["engine"]["age_seconds"] < 60


# -- Live broker account: broker-trades endpoint + normalization -----------------------

RAW_BROKER_TRADE = {
    "flTrdId": "T1", "nOrdNo": "B1", "pTrdSymbol": "SENSEX24500CE", "trnsTp": "S",
    "fldQty": "20", "avgPrc": "80", "flDtTm": "23-Jul-2026 11:30:00",
}


def test_broker_trades_out_normalizes_raw_dicts():
    from app.schemas import broker_trades_out

    out = broker_trades_out([RAW_BROKER_TRADE])
    assert len(out) == 1
    assert out[0].symbol == "SENSEX24500CE"
    assert out[0].side == "S"
    assert out[0].quantity == 20
    assert out[0].price == 80.0
    assert "2026" in out[0].time


def test_broker_trades_out_skips_unparseable():
    from app.schemas import broker_trades_out

    assert broker_trades_out([{"garbage": "no symbol"}]) == []


def test_broker_trades_endpoint(client, auth, repo):
    repo.replace_broker_trades([RAW_BROKER_TRADE])
    r = client.get("/api/broker-trades", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert any(row["symbol"] == "SENSEX24500CE" and row["quantity"] == 20 for row in body)
