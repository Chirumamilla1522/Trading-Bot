"""
Sentiment Analyst – News & Social Feed Processor

Ingests recent headlines, applies recency-weighted scoring, uses per-headline
Redis caching (not aggregate), and produces a calibrated market mood signal.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentDecision, FirmState, ReasoningEntry
from agents.config import MODELS, REDIS_URL, ENABLE_SEMANTIC_CACHE
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import SentimentAnalystOutput, parse_and_validate

CACHE_TTL_SECONDS = 600   # 10 min per headline

SYSTEM_PROMPT = """You are a buy-side financial sentiment analyst with a strict
risk-first mandate. Analyse the provided headlines for {ticker} (current price ${price:.2f}).

Each headline has a recency_weight (0-1, higher = more recent).

You MUST ground your output in the provided headlines:
- Mention (in your reasoning) the 2-3 most important headlines verbatim (shortened if needed).
- Use concrete numbers: reference the current price and the weighted_sentiment you compute.
- If headlines are mixed/low-signal, say so and keep confidence low.

Output STRICT JSON:
{{
  "decision":            "PROCEED" | "HOLD" | "ABORT",
  "aggregate_sentiment": -1.0 to +1.0,
  "weighted_sentiment":  -1.0 to +1.0,
  "headline_scores": [{{"text": "...", "score": -1.0...1.0, "weight": 0.0-1.0}}],
  "key_themes":          ["<theme>"],
  "tail_risks":          ["<specific risk>"],
  "catalyst_detected":   true | false,
  "confidence":          0.0-1.0,
  "reasoning":           "<3-4 sentences. Be specific about what you read.>"
}}

Decision rules (apply in order):
1. ABORT if any headline signals: exchange outage, systemic crisis, SEC halt, war escalation affecting markets.
2. ABORT if weighted_sentiment < -0.6 AND tail_risks has ≥2 items.
3. PROCEED if weighted_sentiment > 0.35 AND confidence > 0.65 AND tail_risks is empty.
4. Otherwise HOLD.

Score each headline independently — macro headlines affect all stocks, company-specific
headlines affect only the ticker. Weight company-specific news higher (×1.5).
Do not output anything except the JSON object."""


def _cache_key(headline: str) -> str:
    return f"sa_score:{hashlib.sha256(headline.encode()).hexdigest()[:20]}"


def _get_cached(r, key: str) -> Optional[float]:
    try:
        val = r.get(key)
        return float(val) if val else None
    except Exception:
        return None


def _set_cached(r, key: str, score: float) -> None:
    try:
        r.setex(key, CACHE_TTL_SECONDS, str(score))
    except Exception:
        pass


def _recency_weight(published_at: datetime, now: datetime) -> float:
    """Linear decay: 1.0 at t=0, 0.1 at t=60min. Safe for both naive and aware."""
    try:
        age_min = (now - published_at).total_seconds() / 60.0
    except TypeError:
        # Timezone mismatch fallback — treat as 30 min old
        age_min = 30.0
    return max(0.1, 1.0 - (age_min / 60.0) * 0.9)


def sentiment_analyst_node(state: FirmState) -> FirmState:
    llm = chat_llm(
        MODELS.sentiment_analyst.active,
        agent_role="sentiment_analyst",
        temperature=0.0,
    )

    r = None
    if ENABLE_SEMANTIC_CACHE:
        try:
            import redis
            r = redis.from_url(REDIS_URL, socket_connect_timeout=1)
            r.ping()
        except Exception:
            r = None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)

    # Coerce every published_at in the feed to UTC-aware in-place so no naive
    # datetimes survive into any comparison (handles items loaded before the
    # NewsItem._ensure_utc validator was deployed, or any other naive source).
    for _n in state.news_feed:
        try:
            if _n.published_at.tzinfo is None:
                _n.published_at = _n.published_at.replace(tzinfo=timezone.utc)
        except Exception:
            _n.published_at = now   # fallback: treat as "just now"

    recent_news = [n for n in state.news_feed if n.published_at >= cutoff]

    # Sort by recency descending
    recent_news.sort(key=lambda n: n.published_at, reverse=True)

    # Build headline list with weights, use per-headline cache
    headlines_with_weight: list[dict] = []
    need_llm: list[dict] = []

    for item in recent_news[:60]:   # token budget cap
        weight = round(_recency_weight(item.published_at, now), 3)
        cache_key = _cache_key(item.headline)
        cached_score = _get_cached(r, cache_key) if r else None

        entry = {"text": item.headline, "weight": weight, "ticker_specific": state.ticker in item.headline.upper()}
        if cached_score is not None:
            item.sentiment = cached_score
            item.cached = True
            entry["score"] = cached_score
            headlines_with_weight.append(entry)
        else:
            need_llm.append(entry)
            headlines_with_weight.append(entry)

    decision   = AgentDecision.HOLD
    agg_sent   = 0.0
    weighted_sent = 0.0
    reasoning  = ""
    confidence = 0.5
    tail_risks : list[str] = []
    key_themes : list[str] = []

    if need_llm:
        # Only send uncached headlines to LLM
        context = {
            "ticker":   state.ticker,
            "price":    state.underlying_price,
            "headlines": need_llm[:40],
        }
        messages = [
            SystemMessage(content=SYSTEM_PROMPT.format(
                ticker=state.ticker, price=state.underlying_price
            )),
            HumanMessage(content=json.dumps(context, indent=2)),
        ]
        response = invoke_llm(llm, messages)
        out = parse_and_validate(response.content, SentimentAnalystOutput, "SentimentAnalyst")
        if not out:
            # One-shot repair pass: coerce STRICT JSON only.
            repair_sys = (
                "You are a strict JSON repair tool.\n"
                "Return ONLY valid JSON matching this schema (no markdown, no prose):\n"
                "{\n"
                '  "decision":"PROCEED|HOLD|ABORT",\n'
                '  "aggregate_sentiment":0.0,\n'
                '  "weighted_sentiment":0.0,\n'
                '  "headline_scores":[{"text":"...","score":0.0,"weight":0.0}],\n'
                '  "key_themes":["..."],\n'
                '  "tail_risks":["..."],\n'
                '  "catalyst_detected":false,\n'
                '  "confidence":0.0,\n'
                '  "reasoning":"..."\n'
                "}\n"
                "If you cannot comply, output HOLD with confidence 0.0 and brief reasoning."
            )
            llm_repair = chat_llm(
                MODELS.sentiment_analyst.active,
                agent_role="sentiment_analyst",
                temperature=0.0,
                max_tokens=650,
            )
            resp2 = invoke_llm(llm_repair, [
                SystemMessage(content=repair_sys),
                HumanMessage(content=(response.content or "")[:2600]),
            ])
            out = parse_and_validate(resp2.content, SentimentAnalystOutput, "SentimentAnalyst")
        if out:
            decision      = AgentDecision(out.decision)
            agg_sent      = out.aggregate_sentiment
            weighted_sent = out.weighted_sentiment
            tail_risks    = out.tail_risks
            key_themes    = out.key_themes
            reasoning     = out.reasoning
            confidence    = out.confidence
            # Cache individual scores
            if r:
                for hs in out.headline_scores:
                    if hs.text:
                        _set_cached(r, _cache_key(hs.text), hs.score)
        else:
            decision   = AgentDecision.HOLD
            agg_sent   = 0.0
            reasoning  = response.content[:400]
            confidence = 0.0
    else:
        # All cached — compute weighted average
        weighted_total = sum(e.get("score", 0) * e["weight"] for e in headlines_with_weight)
        weight_sum     = sum(e["weight"] for e in headlines_with_weight) or 1.0
        weighted_sent  = round(weighted_total / weight_sum, 3)
        agg_sent       = weighted_sent
        decision       = (AgentDecision.PROCEED if weighted_sent > 0.35
                         else AgentDecision.HOLD)
        reasoning      = f"All {len(headlines_with_weight)} headlines served from cache. Weighted sentiment: {weighted_sent:.3f}"
        confidence     = 0.75

    state.aggregate_sentiment  = agg_sent
    state.sentiment_decision   = decision
    state.sentiment_confidence = confidence
    state.sentiment_themes     = key_themes
    state.sentiment_tail_risks = tail_risks

    state.reasoning_log.append(ReasoningEntry(
        agent     = "SentimentAnalyst",
        action    = decision.value,
        reasoning = reasoning,
        inputs    = {
            "headline_count": len(recent_news),
            "uncached_count": len(need_llm),
            "tail_risks":     tail_risks,
            "key_themes":     key_themes,
        },
        outputs   = {
            "aggregate_sentiment": agg_sent,
            "weighted_sentiment":  weighted_sent,
            "confidence":          confidence,
        },
    ))
    return state
