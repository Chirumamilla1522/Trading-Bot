"""
Desk-level context shared by Tier-1 loops and the Tier-3 graph.

This is NOT millisecond/HFT logic: horizons are tens of seconds to hours.
We distinguish:
- News timing (fresh vs stale headlines) for headline-driven moves
- Non-news signals (price/vol structure) so agents can justify trades without fresh news
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.state import FirmState


# Align with product: "fresh" headline for faster reaction; stale → lean on risk + structure
FRESH_NEWS_MAX_MIN = 15.0
MODERATE_NEWS_MAX_MIN = 60.0


def update_news_timing_from_feed(state: "FirmState") -> None:
    """
    Set news_newest_age_minutes and news_timing_regime from firm_state.news_feed
    (last 1h window, same window as SentimentMonitor).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    ages_min: list[float] = []

    for n in state.news_feed:
        if not hasattr(n, "published_at") or n.published_at is None:
            continue
        pub = n.published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if pub >= cutoff:
            ages_min.append(max(0.0, (now - pub).total_seconds() / 60.0))

    if not ages_min:
        state.news_newest_age_minutes = None
        state.news_timing_regime = "none"
        return

    newest = min(ages_min)
    state.news_newest_age_minutes = round(newest, 2)
    if newest < FRESH_NEWS_MAX_MIN:
        state.news_timing_regime = "fresh"
    elif newest < MODERATE_NEWS_MAX_MIN:
        state.news_timing_regime = "moderate"
    else:
        state.news_timing_regime = "stale"


def update_market_bias_score(state: "FirmState") -> None:
    """
    Non-news directional bias from price/vol structure already on FirmState
    (Tier-1 movement + momentum + volume). Bounded [-1, 1].

    Used when headlines are absent or stale: regime/momentum can still warrant a thesis.
    """
    mv = state.movement_signal
    mom = state.momentum
    vr = state.vol_ratio
    # Emphasize movement_signal; momentum scaled; volume expansion nudges conviction
    vol_adj = max(-0.25, min(0.25, (vr - 1.0) * 0.2))
    raw = 0.5 * mv + 0.35 * max(-1.0, min(1.0, mom * 8.0)) + vol_adj
    state.market_bias_score = round(max(-1.0, min(1.0, raw)), 4)


def fundamentals_fingerprint(info: dict[str, Any]) -> str:
    """Stable hash of fields that rarely jump except on real refreshes / revisions."""
    keys = (
        "pe_ratio", "fwd_pe", "peg", "eps_ttm", "analyst_target",
        "analyst_rating", "beta", "div_yield", "market_cap",
    )
    payload = {k: info.get(k) for k in keys if k in info}
    try:
        s = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        s = str(payload)

    return hashlib.sha256(s.encode()).hexdigest()[:20]
