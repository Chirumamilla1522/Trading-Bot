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
from agents.config import (
    MODELS,
    REDIS_URL,
    ENABLE_SEMANTIC_CACHE,
    SENTIMENT_ANALYST_TOPK_HEADLINES,
    SENTIMENT_HEADLINE_LOOKBACK_HOURS,
)
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import SentimentAnalystOutput, parse_and_validate

CACHE_TTL_SECONDS = 600   # 10 min per headline

SYSTEM_PROMPT = """You are a buy-side financial sentiment analyst with a strict
risk-first mandate. Analyse the provided headlines for {ticker} (current price ${price:.2f}).

PRIOR CONTEXT (Tier-1 SentimentMonitor — already ran on Tier-2 structured news):
The JSON includes `desk_sentiment_monitor`: score/confidence/source from structured articles.
Reconcile your headline-level analysis with that prior. If you disagree materially, explain why
(headline-specific info, timing, or stale structured data). Do not ignore it silently.

Each headline has a recency_weight (0-1, higher = more recent).

You MUST ground your output in the provided headlines:
- Mention (in your reasoning) the 2-3 most important headlines verbatim (shortened if needed).
- Use concrete numbers: reference the current price and the weighted_sentiment you compute.
- If headlines are mixed/low-signal, say so and keep confidence low.
- Consider headline age: very recent vs stale. Stale headlines may already be priced;
  reduce confidence for directional PROCEED unless themes are structural (earnings/guidance).

STRICTNESS (must follow):
- Output must be VALID JSON only (no markdown).
- All numeric fields MUST be JSON numbers (e.g. 0.12), never formulas like "0.2 * 0.5 = 0.1".
- Use ONLY the provided headlines + the provided `desk_sentiment_monitor` prior. Do NOT use outside knowledge.
- If there are <3 relevant headlines in-window or they are low-confidence/noisy, choose HOLD and keep confidence ≤ 0.55.

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
    _t0 = __import__("time").time()
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
    lookback_h = state.sentiment_headline_lookback_hours
    if lookback_h is None:
        lookback_h = SENTIMENT_HEADLINE_LOOKBACK_HOURS
    lookback_h = max(0.05, float(lookback_h))
    cutoff = now - timedelta(hours=lookback_h)

    # Coerce every published_at in the feed to UTC-aware in-place so no naive
    # datetimes survive into any comparison (handles items loaded before the
    # NewsItem._ensure_utc validator was deployed, or any other naive source).
    for _n in state.news_feed:
        try:
            if _n.published_at.tzinfo is None:
                _n.published_at = _n.published_at.replace(tzinfo=timezone.utc)
        except Exception:
            _n.published_at = now   # fallback: treat as "just now"

    # ── FinBERT-ranked PriorityQueue: highest-score unseen items first ─────
    # Each call drains the top-K of what this agent hasn't seen yet, so over
    # successive cycles ALL ingested news is eventually analyzed — highest
    # priority first.
    top_k = max(5, int(SENTIMENT_ANALYST_TOPK_HEADLINES))
    queue_picked_ids: list[str] = []
    recent_news: list = []
    try:
        from agents.data.news_priority_queue import AGENT_SENTIMENT_ANALYST, get_queue

        q = get_queue()
        for qn in q.take_unseen(AGENT_SENTIMENT_ANALYST, top_k):
            if qn.item.published_at >= cutoff:
                recent_news.append(qn.item)
                queue_picked_ids.append(qn.id)
    except Exception:
        pass

    # Fallback / top-up from state.news_feed (legacy + dry-run + backfill).
    if len(recent_news) < top_k:
        pool = [n for n in state.news_feed if n.published_at >= cutoff]

        def _urg_rank(x: str) -> int:
            u = (x or "").upper()
            return 0 if u == "T0" else 1 if u == "T1" else 2 if u == "T2" else 3

        pool.sort(
            key=lambda n: (
                _urg_rank(getattr(n, "urgency_tier", "T2")),
                -float(getattr(n, "impact_score", 0.0) or 0.0),
                -float(getattr(n, "vol_prob", 0.0) or 0.0),
                -float(getattr(n, "confidence", 0.0) or 0.0),
                -abs(float(getattr(n, "sentiment", 0.0) or 0.0)),
                n.published_at,
            ),
            reverse=False,
        )
        already = {n.headline for n in recent_news}
        for n in pool:
            if n.headline in already:
                continue
            recent_news.append(n)
            if len(recent_news) >= top_k:
                break

    # Build headline list with weights, use per-headline cache
    headlines_with_weight: list[dict] = []
    need_llm: list[dict] = []

    for item in recent_news[:60]:   # token budget cap
        weight = round(_recency_weight(item.published_at, now), 3)
        cache_key = _cache_key(item.headline)
        cached_score = _get_cached(r, cache_key) if r else None

        entry = {
            "text": item.headline,
            "weight": weight,
            "ticker_specific": state.ticker in item.headline.upper(),
        }
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
        # Send the full top window to the LLM so reasoning can cite multiple key headlines,
        # even if most are cached. (Cache is still used to avoid repeated scoring work.)
        headlines_for_llm = headlines_with_weight[:40]
        context = {
            "ticker":   state.ticker,
            "price":    state.underlying_price,
            "headlines": headlines_for_llm,
            "desk_sentiment_monitor": {
                "score":      state.sentiment_monitor_score,
                "confidence": state.sentiment_monitor_confidence,
                "source":     state.sentiment_monitor_source,
                "reasoning":  (state.sentiment_monitor_reasoning or "")[:420],
            },
            "tier3_structured_digests": state.tier3_structured_digests[:10],
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
                "Return ONLY valid JSON matching this schema (no markdown, no prose).\n"
                "All numeric fields MUST be JSON numbers (no arithmetic, no formulas):\n"
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
    elif headlines_with_weight:
        # All cached — compute weighted average
        weighted_total = sum(e.get("score", 0) * e["weight"] for e in headlines_with_weight)
        weight_sum     = sum(e["weight"] for e in headlines_with_weight) or 1.0
        weighted_sent  = round(weighted_total / weight_sum, 3)
        agg_sent       = weighted_sent
        decision       = (AgentDecision.PROCEED if weighted_sent > 0.35
                         else AgentDecision.HOLD)
        reasoning      = (
            f"All {len(headlines_with_weight)} headlines served from cache. "
            f"Weighted sentiment: {weighted_sent:.3f}"
        )
        confidence     = 0.75
    else:
        # No headlines in the lookback window (or empty feed) — not "all cached"
        decision = AgentDecision.HOLD
        agg_sent = 0.0
        weighted_sent = 0.0
        n_feed = len(state.news_feed)
        if n_feed == 0:
            reasoning = "No headlines in feed for this cycle."
        else:
            reasoning = (
                f"No headlines within the last {lookback_h:.2f}h lookback "
                f"({n_feed} item(s) in feed are outside that window). Weighted sentiment: 0.000"
            )
        confidence = 0.35

    state.aggregate_sentiment  = agg_sent
    state.sentiment_decision   = decision
    state.sentiment_confidence = confidence
    state.sentiment_themes     = key_themes
    state.sentiment_tail_risks = tail_risks

    # Mark queue items as seen by this agent so next cycle can drain lower-priority tail
    if queue_picked_ids:
        try:
            from agents.data.news_priority_queue import AGENT_SENTIMENT_ANALYST, get_queue

            get_queue().mark_seen(AGENT_SENTIMENT_ANALYST, queue_picked_ids)
        except Exception:
            pass

    state.reasoning_log.append(ReasoningEntry(
        agent     = "SentimentAnalyst",
        action    = decision.value,
        reasoning = reasoning,
        inputs    = {
            "headline_count": len(recent_news),
            "uncached_count": len(need_llm),
            "lookback_hours": lookback_h,
            "feed_total": len(state.news_feed),
            "tail_risks":     tail_risks,
            "key_themes":     key_themes,
            "desk_monitor_score": state.sentiment_monitor_score,
            "desk_monitor_source": state.sentiment_monitor_source,
        },
        outputs   = {
            "aggregate_sentiment": agg_sent,
            "weighted_sentiment":  weighted_sent,
            "confidence":          confidence,
        },
    ))
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "sentiment_analyst",
            inputs={
                "ticker": state.ticker,
                "headline_count": len(recent_news),
                "uncached_count": len(need_llm),
                "lookback_hours": float(lookback_h),
                "desk_monitor_score": float(state.sentiment_monitor_score or 0.0),
                "desk_monitor_source": str(state.sentiment_monitor_source or ""),
            },
            outputs={
                "decision": decision.value,
                "aggregate_sentiment": float(agg_sent or 0.0),
                "weighted_sentiment": float(weighted_sent or 0.0),
                "confidence": float(confidence or 0.0),
                "themes_n": len(key_themes or []),
                "tail_risks_n": len(tail_risks or []),
            },
            duration_s=max(0.0, __import__("time").time() - _t0),
        )
    except Exception:
        pass
    return state
