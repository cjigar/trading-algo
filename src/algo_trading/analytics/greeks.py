"""Black-76 implied volatility and analytical greeks over a put-call-parity forward.

Pure and null-safe: every function returns ``None`` on bad input (price below intrinsic,
non-positive time, missing leg, solver non-convergence) and never raises, so a greeks failure
cannot propagate into the trading loop. Index options are European, cash-settled, so Black-76 on
the forward is the right model; the forward comes from put-call parity, not raw spot.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from py_vollib.black.greeks.analytical import delta, gamma, theta, vega
    from py_vollib.black.implied_volatility import implied_volatility

from algo_trading.domain.enums import OptionType

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_CLOSE = time(15, 30)
_SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def _flag(option_type: OptionType) -> str:
    return "c" if option_type is OptionType.CE else "p"


def year_fraction(now: datetime, expiry: date) -> float:
    """Annualized time from ``now`` to 15:30 IST on ``expiry`` (0.0 once expired)."""
    now_ist = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(IST)
    expiry_dt = datetime.combine(expiry, EXPIRY_CLOSE, tzinfo=IST)
    seconds = (expiry_dt - now_ist).total_seconds()
    return seconds / _SECONDS_PER_YEAR if seconds > 0 else 0.0


def implied_forward(
    ce_ltp: float, pe_ltp: float, atm_strike: float, r: float, T: float
) -> float | None:
    """Forward from put-call parity: C - P = (F - K) e^{-rT}  =>  F = K + (C-P) e^{rT}."""
    if T <= 0:
        return None
    return atm_strike + (ce_ltp - pe_ltp) * math.exp(r * T)


def solve_iv(
    price: float, F: float, K: float, r: float, T: float, option_type: OptionType
) -> float | None:
    if T <= 0 or price <= 0 or F <= 0:
        return None
    try:
        return float(implied_volatility(price, F, K, r, T, _flag(option_type)))
    except Exception:  # noqa: BLE001 - below-intrinsic / non-convergence must degrade to None
        return None


def compute_greeks(
    price: float, F: float, K: float, r: float, T: float, option_type: OptionType
) -> Greeks | None:
    iv = solve_iv(price, F, K, r, T, option_type)
    if iv is None:
        return None
    flag = _flag(option_type)
    try:
        return Greeks(
            iv=iv,
            delta=float(delta(flag, F, K, T, r, iv)),
            gamma=float(gamma(flag, F, K, T, r, iv)),
            theta=float(theta(flag, F, K, T, r, iv)),
            vega=float(vega(flag, F, K, T, r, iv)),
        )
    except Exception:  # noqa: BLE001
        return None
