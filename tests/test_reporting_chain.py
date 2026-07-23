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
