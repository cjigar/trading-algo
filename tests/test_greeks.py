"""Pure Black-76 greeks core: IV round-trips, greek sanity, and null-safety."""

from __future__ import annotations

from datetime import UTC, date, datetime

from algo_trading.analytics.greeks import (
    Greeks,
    compute_greeks,
    implied_forward,
    solve_iv,
    year_fraction,
)
from algo_trading.domain.enums import OptionType

# Reference point verified against py_vollib: F=100,K=100,r=0.065,T=7/365,sigma=0.20
# -> black price 1.1035; greeks delta 0.5049, gamma 0.14384, theta -0.07862, vega 0.05517
_F, _K, _R, _T = 100.0, 100.0, 0.065, 7 / 365


def test_solve_iv_round_trips():
    price = 1.1035  # black('c', F, K, T, r, 0.20)
    iv = solve_iv(price, _F, _K, _R, _T, OptionType.CE)
    assert iv is not None
    assert abs(iv - 0.20) < 1e-3


def test_compute_greeks_atm_call_signs_and_magnitudes():
    g = compute_greeks(1.1035, _F, _K, _R, _T, OptionType.CE)
    assert g is not None
    assert isinstance(g, Greeks)
    assert abs(g.iv - 0.20) < 1e-3
    assert 0.45 < g.delta < 0.55          # ATM call ~0.5
    assert g.gamma > 0
    assert g.theta < 0                    # long option decays
    assert g.vega > 0


def test_put_delta_is_negative():
    g = compute_greeks(1.1035, _F, _K, _R, _T, OptionType.PE)
    assert g is not None
    assert -0.55 < g.delta < -0.45


def test_implied_forward_from_parity():
    # CE-PE = 5 at K=100 with ~0 rate/time -> F ~ 105
    f = implied_forward(ce_ltp=7.0, pe_ltp=2.0, atm_strike=100.0, r=0.065, T=_T)
    assert f is not None
    assert abs(f - 105.0) < 0.1


def test_null_paths():
    assert implied_forward(7.0, 2.0, 100.0, 0.065, 0.0) is None          # T<=0
    assert solve_iv(1.0, _F, _K, _R, 0.0, OptionType.CE) is None          # T<=0
    assert solve_iv(0.0, _F, _K, _R, _T, OptionType.CE) is None           # non-positive price
    assert solve_iv(0.0001, 100.0, 50.0, _R, _T, OptionType.CE) is None   # below intrinsic -> caught
    assert compute_greeks(0.0, _F, _K, _R, _T, OptionType.CE) is None


def test_year_fraction_expiry_day_after_close_is_zero():
    # 2025-01-30 16:00 IST is after the 15:30 close on expiry day 2025-01-30
    now = datetime(2025, 1, 30, 10, 30, tzinfo=UTC)  # 16:00 IST
    assert year_fraction(now, date(2025, 1, 30)) == 0.0


def test_year_fraction_positive_before_expiry():
    now = datetime(2025, 1, 23, 4, 0, tzinfo=UTC)  # ~09:30 IST, 7 days out
    t = year_fraction(now, date(2025, 1, 30))
    assert 0.017 < t < 0.021  # ~7/365
