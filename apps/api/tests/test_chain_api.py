"""chain_out maps per-strike greeks into GreeksOut."""

from __future__ import annotations

from types import SimpleNamespace

from app.schemas import chain_out


def _row(strike, ot, token, **g):
    base = dict(strike=strike, option_type=ot, instrument_token=token, oi=100, ltp="120",
                vwap=None, iv=None, delta=None, gamma=None, theta=None, vega=None)
    base.update(g)
    return SimpleNamespace(**base)


def test_chain_out_includes_greeks():
    rows = [
        _row("23000", "CE", "C1", iv="0.19", delta="0.52", gamma="0.003", theta="-6.1", vega="8.0"),
        _row("23000", "PE", "P1", iv="0.21", delta="-0.48", gamma="0.003", theta="-5.9", vega="7.8"),
    ]
    out = chain_out(rows)
    strike = out.per_strike[0]
    assert strike.ce_greeks is not None
    assert strike.ce_greeks.iv == 0.19
    assert strike.ce_greeks.delta == 0.52
    assert strike.pe_greeks.delta == -0.48


def test_chain_out_greeks_null_when_absent():
    rows = [_row("23000", "CE", "C1"), _row("23000", "PE", "P1")]
    out = chain_out(rows)
    assert out.per_strike[0].ce_greeks is None


def test_chain_out_applies_display_window():
    # ltp="0" -> _resolve_atm's priced branch is skipped -> ATM falls back to the middle strike
    # (23000), so the ±7 window is centered there deterministically.
    rows = []
    for k in range(23000 - 20 * 50, 23000 + 20 * 50 + 50, 50):
        rows += [_row(str(k), "CE", f"C{k}", ltp="0"), _row(str(k), "PE", f"P{k}", ltp="0")]
    out = chain_out(rows, "NIFTY", display_window=7)
    assert out.display_window == 7
    assert len(out.per_strike) == 15
    assert out.ce_oi_total == 15 * 100 and out.pe_oi_total == 15 * 100


def test_chain_out_full_chain_when_window_zero():
    rows = []
    for k in range(23000 - 10 * 50, 23000 + 10 * 50 + 50, 50):
        rows += [_row(str(k), "CE", f"C{k}", ltp="0"), _row(str(k), "PE", f"P{k}", ltp="0")]
    out = chain_out(rows, "NIFTY", display_window=0)
    assert len(out.per_strike) == 21 and out.display_window == 0
