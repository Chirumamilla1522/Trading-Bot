"""
Deterministic PoP / EV helpers for LONG (debit) call/put candidates.

- No scipy dependency: uses math.erf for Normal CDF.
- All results are *undiscounted* and intended for ranking/gating candidates, not precise pricing.
"""

from __future__ import annotations

import math
from datetime import date


def _norm_cdf(x: float) -> float:
    """Standard Normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _safe_float(x) -> float | None:
    try:
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def dte_from_yyMMdd(expiry_yyMMdd: str, *, today: date | None = None) -> int | None:
    """
    Compute DTE (calendar days) from OCC YYMMDD expiry string.
    Returns None if parsing fails.
    """
    s = str(expiry_yyMMdd or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    yy = int(s[0:2])
    mm = int(s[2:4])
    dd = int(s[4:6])
    yyyy = 2000 + yy
    try:
        exp = date(yyyy, mm, dd)
    except Exception:
        return None
    t0 = today or date.today()
    return max(0, (exp - t0).days)


def breakeven_at_expiry(*, right: str, strike: float, premium: float) -> float | None:
    """
    Breakeven at expiry for long options:
    - CALL: K + premium
    - PUT:  K - premium
    """
    r = str(right or "").upper()
    K = _safe_float(strike)
    p = _safe_float(premium)
    if K is None or p is None:
        return None
    if r.startswith("C"):
        return K + p
    if r.startswith("P"):
        return K - p
    return None


def pop_long_option(
    *,
    right: str,
    s0: float,
    strike: float,
    premium: float,
    iv: float,
    dte: int,
    mu: float = 0.0,
) -> float | None:
    """
    Probability of profit at expiry (PoP) for a LONG call/put under lognormal:

    ln S_T ~ Normal( ln S0 + mu*T, (iv*sqrt(T))^2 )

    Profit at expiry:
    - CALL: S_T > K + premium
    - PUT:  S_T < K - premium
    """
    r = str(right or "").upper()
    S0 = _safe_float(s0)
    K = _safe_float(strike)
    p = _safe_float(premium)
    sig = _safe_float(iv)
    if S0 is None or K is None or p is None or sig is None:
        return None
    if S0 <= 0 or K <= 0 or sig <= 0 or dte <= 0:
        return None

    T = float(dte) / 365.0
    v = sig * math.sqrt(T)
    m = math.log(S0) + float(mu) * T

    if r.startswith("C"):
        be = K + p
        if be <= 0:
            return None
        z = (math.log(be) - m) / v
        return max(0.0, min(1.0, 1.0 - _norm_cdf(z)))

    if r.startswith("P"):
        be = K - p
        if be <= 0:
            # Need S_T < negative number -> impossible for lognormal
            return 0.0
        z = (math.log(be) - m) / v
        return max(0.0, min(1.0, _norm_cdf(z)))

    return None


def expected_value_long_option(
    *,
    right: str,
    s0: float,
    strike: float,
    premium: float,
    iv: float,
    dte: int,
    mu: float = 0.0,
) -> float | None:
    """
    Approx EV (USD) at expiry for 1 contract:

    EV = 100 * E[(S_T - K)+] - 100 * premium   for calls
    EV = 100 * E[(K - S_T)+] - 100 * premium   for puts

    Where S_T is lognormal with drift mu and vol iv.

    Closed-form for lognormal partial moments (undiscounted):
      Let m = ln S0 + mu*T
          v = iv*sqrt(T)
      E[(S_T - K)+] = exp(m + 0.5*v^2) * Phi(d1) - K * Phi(d2)
      E[(K - S_T)+] = K * Phi(-d2) - exp(m + 0.5*v^2) * Phi(-d1)
      with d2 = (m - ln K)/v, d1 = d2 + v
    """
    r = str(right or "").upper()
    S0 = _safe_float(s0)
    K = _safe_float(strike)
    p = _safe_float(premium)
    sig = _safe_float(iv)
    if S0 is None or K is None or p is None or sig is None:
        return None
    if S0 <= 0 or K <= 0 or sig <= 0 or dte <= 0:
        return None

    T = float(dte) / 365.0
    v = sig * math.sqrt(T)
    m = math.log(S0) + float(mu) * T
    lnK = math.log(K)

    d2 = (m - lnK) / v
    d1 = d2 + v
    ES_ind = math.exp(m + 0.5 * (v * v))

    if r.startswith("C"):
        expected_payoff = ES_ind * _norm_cdf(d1) - K * _norm_cdf(d2)
        return float(100.0 * expected_payoff - 100.0 * p)

    if r.startswith("P"):
        expected_payoff = K * _norm_cdf(-d2) - ES_ind * _norm_cdf(-d1)
        return float(100.0 * expected_payoff - 100.0 * p)

    return None

