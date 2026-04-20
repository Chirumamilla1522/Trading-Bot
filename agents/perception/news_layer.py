"""Phase 2 — News perception: impact tiers, bias, macro shock heuristics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Sequence

from agents.state import NewsItem

from agents.perception.schemas import MacroShockRisk, NewsImpactRow, NewsPerceptionReport


def _bias(sentiment: float) -> Literal["bullish", "bearish", "neutral"]:
    if sentiment > 0.15:
        return "bullish"
    if sentiment < -0.15:
        return "bearish"
    return "neutral"


def build_news_report(
    news_feed: Sequence[NewsItem],
    ticker: str,
    *,
    lookback_hours: float = 48.0,
) -> NewsPerceptionReport:
    t = ticker.upper().strip()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    rows = [n for n in news_feed if n.published_at >= cutoff]
    rows.sort(key=lambda x: x.published_at, reverse=True)

    items: list[NewsImpactRow] = []
    themes: dict[str, int] = {}
    high_impact = 0
    macro_hits = 0

    for n in rows[:100]:
        tick_match = t in [x.upper() for x in (n.tickers or [])] or t in (n.headline or "").upper()
        if not tick_match and n.category not in ("macro", "earnings"):
            continue

        imp = float(getattr(n, "impact_score", 0.0) or 0.0)
        if imp >= 0.55 or n.priority == "HIGH" or n.urgency_tier in ("T0", "T1"):
            high_impact += 1
        if n.category == "macro" and n.priority == "HIGH":
            macro_hits += 1
        cat = n.category or "general"
        themes[cat] = themes.get(cat, 0) + 1

        items.append(
            NewsImpactRow(
                headline=(n.headline or "")[:200],
                impact=round(imp, 4),
                bias=_bias(float(n.sentiment or 0.0)),
                urgency_tier=n.urgency_tier,
                category=cat,
            )
        )

    items.sort(key=lambda x: x.impact, reverse=True)
    top_themes = sorted(themes.keys(), key=lambda k: themes[k], reverse=True)[:6]

    macro_risk = MacroShockRisk.LOW
    if macro_hits >= 3 or high_impact >= 8:
        macro_risk = MacroShockRisk.HIGH
    elif macro_hits >= 1 or high_impact >= 3:
        macro_risk = MacroShockRisk.MEDIUM

    return NewsPerceptionReport(
        high_impact_count=high_impact,
        items=items[:25],
        macro_shock_risk=macro_risk,
        dominant_themes=top_themes,
    )
