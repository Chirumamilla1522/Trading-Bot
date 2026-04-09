"""
Feature builders for FirmState.

Deterministic computations (surface summaries, regime classification, skew metrics)
extracted from LLM nodes so prompts stay lean and we avoid paying tokens for math.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import mean, stdev
from typing import Optional

from agents.state import GreeksSnapshot, MarketRegime, VolSurface, VolSurfacePoint


# ── Vol surface builder ────────────────────────────────────────────────────────

def build_vol_surface(ticker: str, greeks: list[GreeksSnapshot], max_points: int = 200) -> VolSurface:
    """
    Create a coarse vol surface from the current option chain.
    Groups by expiry and selects strikes closest to ATM by |delta - 0.5|.
    """
    by_expiry: dict[str, list[GreeksSnapshot]] = defaultdict(list)
    for g in greeks:
        if not g.expiry or g.iv <= 0:
            continue
        by_expiry[g.expiry].append(g)

    points: list[VolSurfacePoint] = []
    for exp, xs in sorted(by_expiry.items())[:6]:
        xs.sort(key=lambda g: abs(abs(g.delta) - 0.5) if g.delta is not None else 1e9)
        for g in xs[: max(10, max_points // 6)]:
            points.append(VolSurfacePoint(strike=g.strike, expiry=exp, iv=g.iv, delta=g.delta))
            if len(points) >= max_points:
                break
        if len(points) >= max_points:
            break

    return VolSurface(underlying=ticker, points=points)


# ── IV metrics dataclass ──────────────────────────────────────────────────────

@dataclass
class IVMetrics:
    """
    Pre-computed IV analytics passed to agents as structured context.
    Avoids burning LLM tokens on arithmetic the model can get wrong.
    """
    atm_iv: float = 0.0                       # front-month ATM IV
    put_skew: float = 1.0                     # 25-delta put IV / ATM IV (>1 = put premium)
    call_skew: float = 1.0                    # 25-delta call IV / ATM IV
    skew_ratio: float = 1.0                   # put_skew / call_skew (>1 = fear bid for puts)
    term_structure: dict[str, float] = field(default_factory=dict)  # DTE bucket → avg ATM IV
    iv_stdev: float = 0.0                     # cross-chain IV dispersion
    iv_regime: str = "UNKNOWN"                # LOW / NORMAL / ELEVATED / EXTREME
    near_expiry_gamma_risk: bool = False      # any contract with DTE ≤ 7 and |delta|>0.3
    cheapest_atm_spread: float = 0.0          # min (ask-bid) across near-ATM options
    call_put_oi_ratio: float = 1.0            # aggregate call OI / put OI (needs OI data)


def _dte(expiry: str) -> Optional[int]:
    """Parse YYMMDD → DTE from today. Returns None on parse error."""
    if len(expiry) != 6:
        return None
    try:
        exp = date(int("20" + expiry[:2]), int(expiry[2:4]), int(expiry[4:6]))
        return (exp - date.today()).days
    except Exception:
        return None


def _dte_bucket(dte: int) -> str:
    if dte <= 7:   return "0-7d"
    if dte <= 21:  return "7-21d"
    if dte <= 45:  return "21-45d"
    if dte <= 90:  return "45-90d"
    return "90d+"


def compute_iv_metrics(greeks: list[GreeksSnapshot], underlying_price: float = 0.0) -> IVMetrics:
    """
    Compute structured IV analytics from the current option chain.
    All math is done here — agents receive the results, not raw greeks.
    """
    m = IVMetrics()
    if not greeks:
        return m

    all_ivs = [g.iv for g in greeks if g.iv > 0]
    if not all_ivs:
        return m

    m.iv_stdev = stdev(all_ivs) if len(all_ivs) >= 2 else 0.0

    # ── Term structure: average ATM IV per DTE bucket ─────────────────────────
    bucket_ivs: dict[str, list[float]] = defaultdict(list)
    for g in greeks:
        dte = _dte(g.expiry)
        if dte is None or dte < 0 or g.iv <= 0:
            continue
        if g.delta is not None and abs(abs(g.delta) - 0.5) < 0.15:
            bucket_ivs[_dte_bucket(dte)].append(g.iv)

    m.term_structure = {
        bucket: round(mean(ivs), 4)
        for bucket, ivs in bucket_ivs.items()
        if ivs
    }

    # Front-month ATM IV (prefer 21-45d bucket)
    for preferred in ("21-45d", "7-21d", "0-7d", "45-90d", "90d+"):
        if preferred in m.term_structure:
            m.atm_iv = m.term_structure[preferred]
            break
    if not m.atm_iv and all_ivs:
        m.atm_iv = mean(all_ivs)

    # ── Skew: 25-delta put vs call ─────────────────────────────────────────────
    # Front-month (21-45d preferred)
    front_greeks = []
    for g in greeks:
        dte = _dte(g.expiry)
        if dte is not None and 7 <= dte <= 60 and g.iv > 0 and g.delta is not None:
            front_greeks.append(g)

    put_25d_ivs:  list[float] = []
    call_25d_ivs: list[float] = []
    for g in front_greeks:
        d = abs(g.delta)
        if 0.20 <= d <= 0.30:
            if g.right.value == "PUT":
                put_25d_ivs.append(g.iv)
            else:
                call_25d_ivs.append(g.iv)

    if put_25d_ivs and m.atm_iv > 0:
        m.put_skew = round(mean(put_25d_ivs) / m.atm_iv, 3)
    if call_25d_ivs and m.atm_iv > 0:
        m.call_skew = round(mean(call_25d_ivs) / m.atm_iv, 3)
    if m.call_skew > 0:
        m.skew_ratio = round(m.put_skew / m.call_skew, 3)

    # ── Cheapest near-ATM spread (liquidity proxy) ─────────────────────────────
    near_atm = [g for g in front_greeks if g.delta is not None and abs(abs(g.delta) - 0.5) < 0.12]
    spreads = [g.ask - g.bid for g in near_atm if g.ask > g.bid]
    m.cheapest_atm_spread = round(min(spreads), 3) if spreads else 0.0

    # ── Near-expiry gamma risk ─────────────────────────────────────────────────
    m.near_expiry_gamma_risk = any(
        (_dte(g.expiry) or 99) <= 7 and g.delta is not None and abs(g.delta) > 0.30
        for g in greeks
    )

    # ── IV regime ─────────────────────────────────────────────────────────────
    if m.atm_iv >= 0.60:
        m.iv_regime = "EXTREME"
    elif m.atm_iv >= 0.35:
        m.iv_regime = "ELEVATED"
    elif m.atm_iv >= 0.18:
        m.iv_regime = "NORMAL"
    elif m.atm_iv > 0:
        m.iv_regime = "LOW"
    else:
        m.iv_regime = "UNKNOWN"

    return m


# ── Regime classifier ─────────────────────────────────────────────────────────

def classify_regime(greeks: list[GreeksSnapshot]) -> MarketRegime:
    """
    Regime heuristic using ATM IV level, put/call skew, and term structure shape.
    """
    m = compute_iv_metrics(greeks)
    iv = m.atm_iv

    if iv <= 0:
        return MarketRegime.UNKNOWN
    if iv >= 0.60:
        return MarketRegime.HIGH_VOL

    # Steep contango (front IV << back IV) → trending / low vol
    front = m.term_structure.get("7-21d") or m.term_structure.get("0-7d")
    back  = m.term_structure.get("45-90d") or m.term_structure.get("90d+")
    if front and back:
        spread = back - front
        if spread > 0.05 and iv < 0.25:
            return MarketRegime.TRENDING_UP
        if spread > 0.05 and m.skew_ratio > 1.25:
            return MarketRegime.TRENDING_DOWN
        if spread < -0.03:
            # Inverted term structure (backwardation) → fear / high vol
            return MarketRegime.HIGH_VOL

    if iv <= 0.18:
        return MarketRegime.LOW_VOL
    # Default: mean-reverting in the middle
    return MarketRegime.MEAN_REVERTING


# ── Portfolio Greeks aggregation ─────────────────────────────────────────────

def compute_portfolio_greeks(
    positions: list,   # list[Position]
    greeks_map: dict[str, "GreeksSnapshot"],  # symbol → GreeksSnapshot
    underlying_price: float = 0.0,
) -> dict:
    """
    Aggregate dollar Greeks and P&L across all open option positions.

    Returns a dict with keys matching RiskMetrics field names so the caller
    can write them back with a simple loop.

    Dollar greeks:
      - portfolio_delta  = sum(delta × qty × 100)  — $ move per $1 underlying
      - portfolio_gamma  = sum(gamma × qty × 100)  — delta change per $1 move
      - portfolio_vega   = sum(vega  × qty × 100)  — $ per 1% IV move
      - portfolio_theta  = sum(theta × qty × 100)  — $ per calendar day
      - daily_pnl        = sum(current_pnl)         — unrealized P&L
    """
    delta = gamma = vega = theta = daily_pnl = 0.0

    for pos in positions:
        g = greeks_map.get(pos.symbol)
        multiplier = 100  # standard option multiplier

        # Update live P&L if we have a live quote
        if g:
            mid = (g.bid + g.ask) / 2 if g.bid > 0 and g.ask > 0 else 0.0
            if mid > 0:
                pos.current_pnl = (mid - pos.avg_cost) * pos.quantity * multiplier

            # Sign convention: long (qty > 0) adds greeks; short (qty < 0) subtracts
            qty = pos.quantity
            delta  += (g.delta  or 0.0) * qty * multiplier
            gamma  += (g.gamma  or 0.0) * qty * multiplier
            vega   += (g.vega   or 0.0) * qty * multiplier
            theta  += (g.theta  or 0.0) * qty * multiplier

        daily_pnl += pos.current_pnl

    return {
        "portfolio_delta": round(delta,  4),
        "portfolio_gamma": round(gamma,  2),
        "portfolio_vega":  round(vega,   2),
        "portfolio_theta": round(theta,  2),
        "daily_pnl":       round(daily_pnl, 2),
    }


# ── Chain analysis summary for agents ────────────────────────────────────────

def build_chain_analytics(greeks: list[GreeksSnapshot], underlying_price: float) -> dict:
    """
    Build a compact, pre-computed analytics dict to inject into LLM prompts.
    Avoids sending raw greeks lists to the model.
    """
    m = compute_iv_metrics(greeks, underlying_price)
    regime = classify_regime(greeks)

    # Best candidates by strategy type
    near_atm = sorted(
        [g for g in greeks if g.delta is not None and abs(abs(g.delta) - 0.5) < 0.12
         and g.iv > 0 and (_dte(g.expiry) or 0) > 5],
        key=lambda g: abs(abs(g.delta) - 0.5)
    )[:10]

    high_iv = sorted(
        [g for g in greeks if g.iv > 0 and (_dte(g.expiry) or 0) > 5],
        key=lambda g: -g.iv
    )[:5]

    return {
        "iv_metrics": {
            "atm_iv": f"{m.atm_iv:.1%}",
            "atm_iv_raw": m.atm_iv,
            "iv_regime": m.iv_regime,
            "put_skew": m.put_skew,
            "call_skew": m.call_skew,
            "skew_ratio": m.skew_ratio,
            "iv_stdev": round(m.iv_stdev, 4),
            "near_expiry_gamma_risk": m.near_expiry_gamma_risk,
            "cheapest_atm_spread": m.cheapest_atm_spread,
        },
        "term_structure": m.term_structure,
        "regime": regime.value,
        "near_atm_contracts": [
            {
                "symbol": g.symbol, "expiry": g.expiry, "strike": g.strike,
                "right": g.right.value, "dte": _dte(g.expiry),
                "iv": f"{g.iv:.1%}", "delta": round(g.delta or 0, 3),
                "gamma": round(g.gamma or 0, 4), "theta": round(g.theta or 0, 2),
                "bid": g.bid, "ask": g.ask,
                "mid": round((g.bid + g.ask) / 2, 2) if g.bid and g.ask else None,
            }
            for g in near_atm
        ],
        "highest_iv_contracts": [
            {
                "symbol": g.symbol, "right": g.right.value,
                "strike": g.strike, "expiry": g.expiry, "dte": _dte(g.expiry),
                "iv": f"{g.iv:.1%}", "bid": g.bid, "ask": g.ask,
            }
            for g in high_iv
        ],
    }
