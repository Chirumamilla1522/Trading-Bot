"""Phase 2 — Deterministic sentiment aggregation from NewsItem feed."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Sequence

from agents.state import NewsItem

from agents.perception.schemas import SentimentPerceptionReport


def _recency_weight(published_at: datetime, now: datetime) -> float:
    try:
        age_min = (now - published_at).total_seconds() / 60.0
    except TypeError:
        age_min = 30.0
    return max(0.1, 1.0 - (age_min / 60.0) * 0.9)


def _relevant(item: NewsItem, ticker: str) -> bool:
    u = ticker.upper()
    if u in [x.upper() for x in (item.tickers or [])]:
        return True
    if u in (item.headline or "").upper():
        return True
    return False


def build_sentiment_report(
    news_feed: Sequence[NewsItem],
    ticker: str,
    *,
    lookback_hours: float = 24.0,
) -> SentimentPerceptionReport:
    t = ticker.upper().strip()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    rows = [
        n
        for n in news_feed
        if n.published_at >= cutoff and _relevant(n, t)
    ]
    rows.sort(key=lambda x: x.published_at, reverse=True)
    rows = rows[:80]

    if not rows:
        return SentimentPerceptionReport(confidence=0.15, headline_sample=[])

    scores: list[float] = []
    weights: list[float] = []
    hype = 0.0
    fear = 0.0
    sample: list[str] = []

    for n in rows[:40]:
        w = _recency_weight(n.published_at, now)
        if n.ticker_tier in ("portfolio", "active"):
            w *= 1.2
        s = float(n.sentiment or 0.0)
        scores.append(s)
        weights.append(w)
        hype = max(hype, w * max(0.0, s))
        fear = max(fear, w * max(0.0, -s))
        if len(sample) < 5:
            sample.append((n.headline or "")[:120])

    num = sum(w * s for w, s in zip(weights, scores))
    den = sum(weights) or 1.0
    weighted = max(-1.0, min(1.0, num / den))
    plain = sum(scores) / len(scores) if scores else 0.0

    # Dispersion → anomaly if opinions wildly disagree
    anomaly = False
    if len(scores) >= 5:
        try:
            anomaly = statistics.stdev(scores) > 0.55
        except statistics.StatisticsError:
            pass

    conf = min(0.95, 0.25 + 0.6 * min(1.0, len(rows) / 15.0))
    if anomaly:
        conf *= 0.85

    return SentimentPerceptionReport(
        aggregate_score=round(plain, 4),
        weighted_score=round(weighted, 4),
        hype_score=round(min(1.0, hype), 4),
        fear_score=round(min(1.0, fear), 4),
        confidence=round(conf, 3),
        anomaly=anomaly,
        headline_sample=sample,
        n_headlines_used=len(rows),
    )
