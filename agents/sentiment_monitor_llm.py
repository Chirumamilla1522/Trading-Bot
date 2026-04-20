"""
Tier-1 SentimentMonitor — LLM synthesis over structured news (not keyword scores).

Inputs come from Tier-2 NewsProcessor + SQLite (`ProcessedArticle` fields):
per-article sentiment, confidence, impact_magnitude, category, digest, themes.

Falls back to a deterministic blend of the same structured fields if the LLM fails.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

log = logging.getLogger(__name__)

MONITOR_SYSTEM = """You are SentimentMonitor on an equity options desk (intraday horizons: minutes to hours, not HFT).

You receive JSON: AI-structured news items for a single ticker. Each item was already analyzed
for category, impact_magnitude (1–5), per-article sentiment (-1..1), and confidence (0..1).

Your job: output ONE desk-level score `desk_sentiment` in [-1.0, 1.0] for THIS ticker right now.

Rules:
- Weight higher `impact_magnitude` and `confidence` more. Discount older `published_at`.
- If items conflict, lean conservative (toward 0) and say so in `reasoning`.
- Do NOT re-interpret headline text from scratch; synthesize the structured fields only.
- If the list is empty, return desk_sentiment 0.0 and confidence 0.0.

STRICTNESS:
- Use ONLY the structured fields in the payload. Do NOT infer “what happened” beyond the provided digest/themes/category.
- Output MUST be valid JSON on a single line (no markdown fences, no trailing text).

Output STRICT JSON only:
{"desk_sentiment": <float>, "confidence": <float 0-1>, "reasoning": "<=280 chars>"}
"""


def _parse_iso(dt: str) -> datetime | None:
    try:
        s = str(dt).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def _recency_weight(published_at: str, now: datetime) -> float:
    pub = _parse_iso(published_at)
    if pub is None:
        return 0.35
    age_min = max(0.0, (now - pub).total_seconds() / 60.0)
    return max(0.08, 1.0 - (age_min / 90.0) * 0.92)


def _deterministic_desk_score(rows: list[dict[str, Any]], now: datetime) -> tuple[float, float]:
    """Structured fallback: weighted average using confidence × impact × recency."""
    if not rows:
        return 0.0, 0.0
    wsum = 0.0
    num = 0.0
    for r in rows:
        s = float(r.get("sentiment") or 0.0)
        c = max(0.05, min(1.0, float(r.get("confidence") or 0.0)))
        imp = max(1, min(5, int(r.get("impact_magnitude") or 1)))
        w = c * (imp / 5.0) * _recency_weight(str(r.get("published_at") or ""), now)
        wsum += s * w
        num += w
    if num <= 1e-9:
        return 0.0, 0.0
    v = max(-1.0, min(1.0, wsum / num))
    conf = max(0.15, min(0.95, num / max(1.0, len(rows))))
    return round(v, 4), round(conf, 4)


def _merge_with_memory(ticker: str, hours: float, db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer SQLite; add in-memory processed articles not yet flushed (same window)."""
    by_id = {r["id"]: r for r in db_rows}
    t = ticker.upper().strip()
    now = datetime.now(timezone.utc)
    cut = now - timedelta(hours=hours)

    try:
        from agents.data.news_processor import get_articles_affecting_ticker

        mem = get_articles_affecting_ticker(ticker, limit=40)
    except Exception:
        mem = []

    for d in mem:
        aid = str(d.get("id") or "").strip()
        if not aid or aid in by_id:
            continue
        pub_s = d.get("published_at")
        if isinstance(pub_s, datetime):
            piso = pub_s.astimezone(timezone.utc).isoformat()
        else:
            piso = str(pub_s or "")
        pub_dt = _parse_iso(piso) if piso else None
        if pub_dt is not None and pub_dt < cut:
            continue
        themes = d.get("themes") or []
        if not isinstance(themes, list):
            themes = []
        by_id[aid] = {
            "id": aid,
            "published_at": piso,
            "headline": str(d.get("headline") or "")[:240],
            "category": str(d.get("category") or "general"),
            "sentiment": float(d.get("sentiment") or 0.0),
            "confidence": float(d.get("confidence") or 0.0),
            "impact_magnitude": int(d.get("impact_magnitude") or 1),
            "llm_digest": str(d.get("llm_digest") or "")[:450],
            "themes": themes,
            "ticker_role": "memory",
        }

    return sorted(by_id.values(), key=lambda x: str(x.get("published_at") or ""), reverse=True)


def collect_structured_window(ticker: str, hours: float = 1.0) -> list[dict[str, Any]]:
    """Structured articles for SentimentMonitor (DB + in-memory merge)."""
    try:
        from agents.data.news_processed_db import get_structured_articles_for_monitor

        db_rows = get_structured_articles_for_monitor(ticker, hours=hours, limit=30)
    except Exception as exc:
        log.debug("collect_structured_window DB: %s", exc)
        db_rows = []
    return _merge_with_memory(ticker, hours, db_rows)


def run_sentiment_monitor_cycle(ticker: str, *, hours: float = 1.0) -> dict[str, Any]:
    """
    One SentimentMonitor step: LLM over structured rows, else deterministic structured blend.

    Returns:
      desk_sentiment, confidence, reasoning, source in {llm_structured, fallback_structured, none}
    """
    now = datetime.now(timezone.utc)
    rows = collect_structured_window(ticker, hours=hours)

    if not rows:
        return {
            "desk_sentiment": 0.0,
            "confidence": 0.0,
            "reasoning": "No structured news in window (await Tier-2 processing or feeds).",
            "source": "none",
        }

    compact = [
        {
            "published_at": r.get("published_at"),
            "sentiment": r.get("sentiment"),
            "confidence": r.get("confidence"),
            "impact_magnitude": r.get("impact_magnitude"),
            "category": r.get("category"),
            "themes": (r.get("themes") or [])[:4],
            "digest": (r.get("llm_digest") or "")[:360],
        }
        for r in rows[:22]
    ]

    payload = json.dumps(
        {"ticker": ticker.upper(), "article_count": len(rows), "items": compact},
        indent=2,
        ensure_ascii=False,
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from agents.config import MODELS
        from agents.llm_providers import chat_llm
        from agents.llm_retry import invoke_llm

        llm = chat_llm(
            MODELS.sentiment_analyst.active,
            agent_role="sentiment_monitor",
            temperature=0.05,
        )
        msg = invoke_llm(
            llm,
            [
                SystemMessage(content=MONITOR_SYSTEM),
                HumanMessage(content=payload),
            ],
        )
        raw = (msg.content or "").strip()
        if "```" in raw:
            i, j = raw.find("{"), raw.rfind("}") + 1
            if i >= 0 and j > i:
                raw = raw[i:j]
        data = json.loads(raw)
        ds = float(data.get("desk_sentiment", 0.0))
        cf = float(data.get("confidence", 0.0))
        reason = str(data.get("reasoning", ""))[:400]
        ds = max(-1.0, min(1.0, ds))
        cf = max(0.0, min(1.0, cf))
        return {
            "desk_sentiment": round(ds, 4),
            "confidence": round(cf, 4),
            "reasoning": reason,
            "source": "llm_structured",
        }
    except Exception as exc:
        log.debug("SentimentMonitor LLM fallback: %s", exc)
        ds, cf = _deterministic_desk_score(rows, now)
        return {
            "desk_sentiment": ds,
            "confidence": cf,
            "reasoning": f"Structured blend (LLM unavailable): {type(exc).__name__}",
            "source": "fallback_structured",
        }
