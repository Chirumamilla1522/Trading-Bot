"""
Phases 0–2 — Build a full PerceptionBundle for one ticker.

Usage:
  from agents.perception.pipeline import run_perception_bundle
  b = run_perception_bundle("AAPL", news_feed=firm_state.news_feed)
"""
from __future__ import annotations

import logging
from typing import Sequence

from agents.state import NewsItem

from agents.perception.events import build_event_report
from agents.perception.fundamental import build_fundamental_report
from agents.perception.news_layer import build_news_report
from agents.perception.schemas import PerceptionBundle
from agents.perception.sentiment_layer import build_sentiment_report
from agents.perception.snapshot import build_snapshot
from agents.perception.store import append_bundle
from agents.perception.technical import build_technical_report

log = logging.getLogger(__name__)


def run_perception_bundle(
    ticker: str,
    news_feed: Sequence[NewsItem] | None = None,
    *,
    persist: bool = True,
    timeframe: str = "1Day",
    bar_limit: int = 260,
) -> PerceptionBundle:
    """
    Run technical, fundamental, event, sentiment, and news perception.

    ``news_feed`` should be the caller's recent ``FirmState.news_feed`` (or filtered).
    If None, sentiment/news layers see empty feeds.
    """
    news_feed = list(news_feed or [])

    snap, bars = build_snapshot(ticker, timeframe=timeframe, limit=bar_limit)
    tech = build_technical_report(bars, snap.ticker)
    fund = build_fundamental_report(snap.ticker)
    ev = build_event_report(bars)
    sent = build_sentiment_report(news_feed, snap.ticker)
    nws = build_news_report(news_feed, snap.ticker)

    bundle = PerceptionBundle(
        snapshot=snap,
        technical=tech,
        fundamental=fund,
        sentiment=sent,
        news=nws,
        events=ev,
    )
    if persist:
        try:
            append_bundle(bundle)
        except Exception as e:
            log.debug("perception persist skipped: %s", e)
    return bundle


def run_perception_bundle_json(
    ticker: str,
    news_feed: Sequence[NewsItem] | None = None,
    **kwargs,
) -> dict:
    return run_perception_bundle(ticker, news_feed, **kwargs).model_dump(mode="json")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    t = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    b = run_perception_bundle(t, [], persist=False)
    print(b.model_dump_json(indent=2)[:4000])
