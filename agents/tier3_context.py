"""
Shared Tier-3 ingest enrichment: align the LangGraph with Tier-1 monitor + Tier-2 news DB.

Called from ``ingest_data_node`` so downstream agents see the same structured story.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.state import FirmState

log = logging.getLogger(__name__)


def attach_structured_news_digests(state: "FirmState", *, limit: int = 10) -> None:
    """
    Load compact ``llm_digest`` lines for ``state.ticker`` from SQLite (and in-memory fallback).
    Idempotent for empty DB — sets ``tier3_structured_digests`` on ``state``.
    """
    lim = max(1, min(24, int(limit)))
    try:
        from agents.data.news_processor import get_llm_news_digests

        rows = get_llm_news_digests(state.ticker, limit=lim)
        state.tier3_structured_digests = [str(x).strip() for x in rows if x and str(x).strip()]
    except Exception as exc:
        log.debug("attach_structured_news_digests: %s", exc)
        state.tier3_structured_digests = []
