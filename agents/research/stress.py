"""
Lightweight stress / sanity checks for proposals (not full Monte Carlo).

Extend later with greeks-based bounds when proposal legs are present.
"""
from __future__ import annotations

from typing import Any

from agents.research.schema import StressCheck, TickerBrief


def attach_default_stress(brief: TickerBrief, max_risk_usd: float | None = None) -> TickerBrief:
    """Populate stress block if empty."""
    if brief.stress.notes:
        return brief
    s = brief.stress.model_copy()
    if max_risk_usd is not None:
        s.max_loss_usd_estimate = max_risk_usd
    s.notes = (
        "Spot ±1σ path not modeled here; use full pipeline + broker risk for sizing. "
        "This field is a placeholder for future greeks-based bounds."
    )
    brief.stress = s
    return brief


def quick_spread_stress(
    max_risk: float,
    credit_received: float = 0.0,
) -> dict[str, Any]:
    """Toy metrics for vertical/credit structures."""
    return {
        "max_loss_usd": max_risk,
        "credit_usd": credit_received,
        "breakeven_note": "See full greeks in chain drilldown",
    }
