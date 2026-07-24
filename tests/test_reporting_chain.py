"""summarize_chain pivots persisted greeks onto the per-strike view."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from algo_trading.reporting import summarize_chain


def _row(strike, ot, token, **greeks):
    base = dict(strike=strike, option_type=ot, instrument_token=token, oi=100, ltp="120",
                vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)
    base.update(greeks)
    return SimpleNamespace(**base)


def test_summarize_chain_attaches_greeks():
    rows = [
        _row("23000", "CE", "C1", iv="0.19", delta="0.52", gamma="0.003", theta="-6.1", vega="8.0"),
        _row("23000", "PE", "P1", iv="0.21", delta="-0.48", gamma="0.003", theta="-5.9", vega="7.8"),
    ]
    summary = summarize_chain(rows)
    strike = next(s for s in summary.per_strike if s.strike == Decimal("23000"))
    assert strike.ce_greeks is not None and abs(strike.ce_greeks.iv - 0.19) < 1e-9
    assert strike.ce_greeks.delta == 0.52
    assert strike.pe_greeks is not None and strike.pe_greeks.delta == -0.48


def test_summarize_chain_greeks_none_when_absent():
    rows = [_row("23000", "CE", "C1"), _row("23000", "PE", "P1")]
    summary = summarize_chain(rows)
    strike = summary.per_strike[0]
    assert strike.ce_greeks is None and strike.pe_greeks is None


def _oi_row(strike, ot, token, oi, ltp="120"):
    return SimpleNamespace(strike=strike, option_type=ot, instrument_token=token, oi=oi,
                           ltp=ltp, vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)


def _chain_rows(atm=23000, half=20, step=50, ce_oi=100, pe_oi=100):
    """CE+PE rows for atm ± half strikes. LTPs vary with distance from ATM so _resolve_atm
    identifies the middle strike (atm parameter) as the actual ATM (smallest CE-PE difference at ATM)."""
    rows = []
    for k in range(atm - half * step, atm + half * step + step, step):
        dist = abs(k - atm) / step
        # At ATM, CE ≈ PE. Far from ATM, they diverge (realistic options behavior).
        ce_ltp = str(120.0 + dist * 2)  # CE LTP increases as strike increases
        pe_ltp = str(120.0 - dist * 2)  # PE LTP decreases as strike increases
        rows.append(_oi_row(str(k), "CE", f"C{k}", ce_oi, ce_ltp))
        rows.append(_oi_row(str(k), "PE", f"P{k}", pe_oi, pe_ltp))
    return rows


def test_display_window_slices_to_atm_plus_minus_n():
    rows = _chain_rows(atm=23000, half=20)  # 41 strikes captured
    summary = summarize_chain(rows, display_window=7)
    strikes = [int(s.strike) for s in summary.per_strike]
    assert len(strikes) == 15  # 7 below + ATM + 7 above
    assert strikes[0] == 23000 - 7 * 50 and strikes[-1] == 23000 + 7 * 50
    assert summary.atm == Decimal("23000")
    assert any(s.is_atm and s.strike == Decimal("23000") for s in summary.per_strike)
    assert summary.display_window == 7


def test_display_window_totals_are_windowed():
    # 41 strikes, CE oi=100, PE oi=100 each -> windowed (15 strikes) totals = 15*100 each.
    rows = _chain_rows(atm=23000, half=20, ce_oi=100, pe_oi=100)
    summary = summarize_chain(rows, display_window=7)
    assert summary.ce_oi_total == 15 * 100
    assert summary.pe_oi_total == 15 * 100
    assert summary.selected_side == "—"


def test_windowed_selected_side_can_differ_from_full_chain():
    # Full chain: PE-heavy on the far wings; within ATM ±2, CE dominates. Windowed totals must
    # follow the window, not the whole chain.
    rows = []
    for k in range(23000 - 5 * 50, 23000 + 5 * 50 + 50, 50):
        near = abs(k - 23000) <= 2 * 50
        # LTPs vary so _resolve_atm identifies 23000 as ATM (smallest CE-PE difference there)
        dist = abs(k - 23000) / 50.0
        ce_ltp = str(120.0 + dist * 2)
        pe_ltp = str(120.0 - dist * 2)
        rows.append(_oi_row(str(k), "CE", f"C{k}", 500 if near else 10, ce_ltp))
        rows.append(_oi_row(str(k), "PE", f"P{k}", 10 if near else 500, pe_ltp))
    full = summarize_chain(rows)  # no window
    win = summarize_chain(rows, display_window=2)
    assert full.selected_side == "PE"   # wings dominate the full chain
    assert win.selected_side == "CE"    # ATM ±2 is CE-heavy
    assert win.ce_oi_total == 5 * 500 and win.pe_oi_total == 5 * 10


def test_display_window_clamps_at_edges():
    # ATM near the low edge: only 3 strikes below exist, window still returns what's available.
    rows = _chain_rows(atm=23000, half=3)  # strikes 22850..23150 (7 total)
    summary = summarize_chain(rows, display_window=7)
    assert len(summary.per_strike) == 7  # clamped: can't exceed the 7 captured strikes
    assert summary.atm == Decimal("23000")


def test_display_window_none_or_zero_returns_full_chain():
    rows = _chain_rows(atm=23000, half=10)  # 21 strikes
    assert len(summarize_chain(rows).per_strike) == 21              # default None -> full
    assert len(summarize_chain(rows, display_window=0).per_strike) == 21  # 0 -> full
    assert summarize_chain(rows).display_window == 0
