"""Telegram alerting: sender throttle/rate-limit/fail-safe, and event classification."""

from __future__ import annotations

from algo_trading.observability import alerts
from algo_trading.observability.alerts import TelegramAlerter


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, data: dict) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((url, data))


def _alerter(fail: bool = False, **kw) -> tuple[TelegramAlerter, FakeTransport]:
    t = FakeTransport(fail)
    a = TelegramAlerter("TOK", "123", transport=t, clock=lambda: 1000.0, **kw)
    return a, t


# -- Sender behaviour ------------------------------------------------------------------

def test_deliver_sends_message():
    a, t = _alerter()
    assert a._deliver("hello", "k", now=1000) is True
    assert len(t.calls) == 1
    url, data = t.calls[0]
    assert data == {"chat_id": "123", "text": "hello"}
    assert url.endswith("/sendMessage")


def test_deliver_dedups_same_key_within_window():
    a, t = _alerter(throttle_seconds=300)
    assert a._deliver("m", "k", now=1000) is True
    assert a._deliver("m", "k", now=1100) is False  # within 300s -> suppressed
    assert len(t.calls) == 1
    # after the window it sends again, carrying a "+N more" summary
    assert a._deliver("m", "k", now=1400) is True
    assert "+1 more suppressed" in t.calls[-1][1]["text"]


def test_deliver_rate_limit_caps_per_minute():
    a, t = _alerter(throttle_seconds=0, rate_limit_per_min=2)
    assert a._deliver("a", "k1", now=1000) is True
    assert a._deliver("b", "k2", now=1000) is True
    assert a._deliver("c", "k3", now=1000) is False  # 3rd in the minute -> capped
    assert len(t.calls) == 2
    # next window resets the cap
    assert a._deliver("d", "k4", now=1061) is True


def test_deliver_is_fail_safe_on_transport_error():
    a, t = _alerter(fail=True)
    # a broken transport must not raise, and must not record the send
    assert a._deliver("x", "k", now=1000) is False
    assert a._last_sent == {}


# -- Classification --------------------------------------------------------------------

class Capture:
    def __init__(self, trade: bool = True) -> None:
        self.trade_fills_enabled = trade
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, key: str | None = None) -> None:
        self.sent.append((text, key))


def test_alert_event_critical(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(alerts, "_alerter", cap)
    alerts.alert_event({"event": "kill_switch", "level": "error", "day_pnl": "-5000", "cap": "5000"})
    text, key = cap.sent[0]
    assert text.startswith("🔴 kill_switch") and "day_pnl=-5000" in text and "cap=5000" in text
    assert key == "critical:kill_switch"


def test_alert_event_health_and_error(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(alerts, "_alerter", cap)
    alerts.alert_event({"event": "starting", "level": "info", "mode": "live", "live_armed": True})
    alerts.alert_event({"event": "pnl_snapshot_failed", "level": "error"})
    assert cap.sent[0][0].startswith("🟡 starting") and "mode=live" in cap.sent[0][0]
    assert cap.sent[1][0].startswith("⚠️ pnl_snapshot_failed") and cap.sent[1][1] == "error:pnl_snapshot_failed"


def test_alert_event_trade_gated(monkeypatch):
    on = Capture(trade=True)
    monkeypatch.setattr(alerts, "_alerter", on)
    fill = {"event": "fill_recorded", "level": "info", "symbol": "X", "side": "B", "quantity": 75, "price": "100"}
    alerts.alert_event(fill)
    assert on.sent[0][0].startswith("💹 fill_recorded")
    assert on.sent[0][1] == "trade:fill_recorded:X:B:75:100"  # distinct fills don't dedup

    off = Capture(trade=False)
    monkeypatch.setattr(alerts, "_alerter", off)
    alerts.alert_event(fill)
    assert off.sent == []  # trade tape disabled


def test_alert_event_ignores_uninteresting_and_noop_when_disabled(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(alerts, "_alerter", cap)
    alerts.alert_event({"event": "boot_idle", "level": "info"})  # not a tracked event
    assert cap.sent == []
    monkeypatch.setattr(alerts, "_alerter", None)
    alerts.alert_event({"event": "kill_switch", "level": "error"})  # no alerter -> no-op, no raise


def test_format_only_surfaces_whitelisted_fields():
    text = alerts._format("🔴", "x", {"reason": "y", "some_token_field": "SEKRIT"})
    assert "reason=y" in text and "SEKRIT" not in text


# -- init_alerts gating ----------------------------------------------------------------

class _Settings:
    alerts_enabled = True
    telegram_chat_id = "123"
    alert_trade_fills = True
    alert_throttle_seconds = 300
    alert_rate_limit_per_min = 20


def test_init_alerts_noop_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert alerts.init_alerts(_Settings()) is None
    assert alerts._alerter is None


def test_init_alerts_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    s = _Settings()
    s.alerts_enabled = False
    assert alerts.init_alerts(s) is None


def test_init_alerts_enabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    a = alerts.init_alerts(_Settings())
    try:
        assert a is not None and alerts._alerter is a
    finally:
        alerts._alerter = None  # don't leak into other tests
