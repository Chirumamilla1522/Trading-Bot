"""
Tier-3 LangGraph Pipeline — Full Research & Execution Graph
===========================================================

Tier-1 (SentimentMonitor: LLM on structured Tier-2 news + MovementTracker) and
Tier-2 (FundamentalsRefresher, NewsProcessor) run as background loops managed by tiers.py.

Tier-3 is the triggered LLM pipeline (multi-horizon, not HFT). Invoked by:
  • Manual UI → POST /run_cycle
  • Auto watchdog (sentiment+movement, technical anomaly, fundamentals change,
    or market structure — see tiers.py)
  • Scanner / timer hooks

Pipeline flow:

  [ingest_data]
       │  IV surface + regime + desk timing/bias + Tier-2 digests
       │  (gate: circuit breaker / kill switch → early_abort)
       ▼
  [options_specialist]    ← IV surface + skew + chain + desk context
       │
  [sentiment_analyst]     ← Headline LLM, reconciled w/ SentimentMonitor prior
       │
  [bull_researcher]       ← builds the bullish case from market data
       │
  [bear_researcher]       ← challenges the bull case with specific risks
       │
  [strategist]            ← synthesises all inputs → concrete TradeProposal
       │
  [risk_manager]          ← hard risk gate; ABORT is non-negotiable
       │
  [adversarial_debate]    ← optional extra judge round on the proposal
       │
  [desk_head]             ← final supervisor, go/no-go decision
       │
       ├─ autopilot → [trader]     ← executes via EMS
       ├─ advisory  → [recommend]  ← parks Recommendation for user approval
       └─ no trade  → [xai_log]
       │
  [xai_log]               ← persists full reasoning log
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from typing import Any, Literal
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.state import AgentDecision, FirmState, Recommendation, ReasoningEntry
from agents.agents.desk_head         import desk_head_node
from agents.agents.options_specialist import options_specialist_node
from agents.agents.stock_specialist   import stock_specialist_node
from agents.agents.sentiment_analyst  import sentiment_analyst_node
from agents.agents.risk_manager       import risk_manager_node
from agents.agents.strategist         import strategist_node
from agents.agents.adversarial_debate import adversarial_debate_node
from agents.agents.trader             import trader_node
from agents.agents.paired_researcher  import paired_researcher_node
from agents.config                    import (
    COMBINED_BULL_BEAR_RESEARCH,
    ENABLE_ADVERSARIAL_DEBATE,
    ENABLE_BULL_BEAR_RESEARCH,
    ENABLE_SENTIMENT_ANALYST,
    MODELS,
)
from agents.llm_providers             import chat_llm
from agents.llm_retry                 import invoke_llm

log = logging.getLogger(__name__)


# ── Parallel analysis (speed) ──────────────────────────────────────────────────

def _parallel_analysis_node(state: FirmState) -> FirmState:
    """
    Stage 1 parallelization:
    - StockSpecialist
    - OptionsSpecialist
    - SentimentAnalyst

    Each runs on a deep-copied state snapshot. We merge back only agent-owned fields
    plus append their reasoning entries.
    """
    _t0 = time.time()
    base = state
    snap = base.model_copy(deep=True)

    jobs = {
        "stock_specialist": stock_specialist_node,
        "options_specialist": options_specialist_node,
    }
    if ENABLE_SENTIMENT_ANALYST:
        jobs["sentiment_analyst"] = sentiment_analyst_node

    results: dict[str, FirmState] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            ex.submit(fn, snap.model_copy(deep=True)): name
            for name, fn in jobs.items()
        }
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {str(e)[:220]}"

    def _append_new_reasoning(from_state: FirmState) -> None:
        try:
            seen = {(e.agent, e.action, getattr(e, "timestamp", None)) for e in (base.reasoning_log or [])}
            for e in (from_state.reasoning_log or []):
                key = (e.agent, e.action, getattr(e, "timestamp", None))
                if key in seen:
                    continue
                base.reasoning_log.append(e)
                seen.add(key)
        except Exception:
            pass

    if "stock_specialist" in results:
        r = results["stock_specialist"]
        base.stock_decision = r.stock_decision
        base.stock_confidence = r.stock_confidence
        base.pending_stock_proposal = r.pending_stock_proposal
        _append_new_reasoning(r)

    if "options_specialist" in results:
        r = results["options_specialist"]
        base.analyst_decision = r.analyst_decision
        base.analyst_confidence = r.analyst_confidence
        base.iv_atm = r.iv_atm
        base.iv_skew_ratio = r.iv_skew_ratio
        base.iv_regime = r.iv_regime
        base.iv_term_structure = r.iv_term_structure
        _append_new_reasoning(r)

    if "sentiment_analyst" in results:
        r = results["sentiment_analyst"]
        base.sentiment_decision = r.sentiment_decision
        base.sentiment_confidence = r.sentiment_confidence
        base.aggregate_sentiment = r.aggregate_sentiment
        base.sentiment_themes = r.sentiment_themes
        base.sentiment_tail_risks = r.sentiment_tail_risks
        _append_new_reasoning(r)

    if errors:
        base.reasoning_log.append(ReasoningEntry(
            agent="System",
            action="INFO",
            reasoning="Parallel analysis: one or more agents failed; continuing with partial results.",
            inputs={"errors": errors},
            outputs={},
        ))

    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "parallel_analysis",
            inputs={"ticker": base.ticker},
            outputs={"ok": sorted(list(results.keys())), "errors": errors},
            duration_s=max(0.0, time.time() - _t0),
        )
    except Exception:
        pass

    return base


def _parallel_five_node(state: FirmState) -> FirmState:
    """
    Run 5 independent agents in parallel to reduce Tier‑3 latency:
    - StockSpecialist
    - OptionsSpecialist
    - SentimentAnalyst
    - BullResearcher
    - BearResearcher

    Each runs on a deep-copied state snapshot. We merge back only agent-owned fields
    plus append their reasoning entries.
    """
    _t0 = time.time()
    base = state
    snap = base.model_copy(deep=True)

    def _run_stock(s: FirmState) -> FirmState:
        return stock_specialist_node(s)

    def _run_options(s: FirmState) -> FirmState:
        return options_specialist_node(s)

    def _run_sentiment(s: FirmState) -> FirmState:
        return sentiment_analyst_node(s)

    jobs = {
        "stock_specialist": _run_stock,
        "options_specialist": _run_options,
    }
    if ENABLE_SENTIMENT_ANALYST:
        jobs["sentiment_analyst"] = _run_sentiment
    if ENABLE_BULL_BEAR_RESEARCH:
        if COMBINED_BULL_BEAR_RESEARCH:
            jobs["paired_researcher"] = paired_researcher_node
        else:
            jobs["bull_researcher"] = bull_researcher_node
            jobs["bear_researcher"] = bear_researcher_node

    results: dict[str, FirmState] = {}
    errors: dict[str, str] = {}

    # Use threads: each agent is I/O-bound (LLM calls) and already blocks.
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {
            ex.submit(fn, snap.model_copy(deep=True)): name
            for name, fn in jobs.items()
        }
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {str(e)[:220]}"

    # Merge results back into base state (only agent-owned fields).
    # We also append only the "new" reasoning entries produced by each agent.
    def _append_new_reasoning(from_state: FirmState) -> None:
        try:
            # base.reasoning_log is a list; since snap was deep-copied, compare lengths.
            # safest: append entries whose timestamp/agent/action tuple isn't already present.
            seen = {(e.agent, e.action, getattr(e, "timestamp", None)) for e in (base.reasoning_log or [])}
            for e in (from_state.reasoning_log or []):
                key = (e.agent, e.action, getattr(e, "timestamp", None))
                if key in seen:
                    continue
                base.reasoning_log.append(e)
                seen.add(key)
        except Exception:
            pass

    if "stock_specialist" in results:
        r = results["stock_specialist"]
        base.stock_decision = r.stock_decision
        base.stock_confidence = r.stock_confidence
        base.pending_stock_proposal = r.pending_stock_proposal
        _append_new_reasoning(r)

    if "options_specialist" in results:
        r = results["options_specialist"]
        base.analyst_decision = r.analyst_decision
        base.analyst_confidence = r.analyst_confidence
        # IV metrics are owned by options ingest/specialist and used downstream
        base.iv_atm = r.iv_atm
        base.iv_skew_ratio = r.iv_skew_ratio
        base.iv_regime = r.iv_regime
        base.iv_term_structure = r.iv_term_structure
        _append_new_reasoning(r)

    if "sentiment_analyst" in results:
        r = results["sentiment_analyst"]
        base.sentiment_decision = r.sentiment_decision
        base.sentiment_confidence = r.sentiment_confidence
        base.aggregate_sentiment = r.aggregate_sentiment
        base.sentiment_themes = r.sentiment_themes
        base.sentiment_tail_risks = r.sentiment_tail_risks
        _append_new_reasoning(r)

    if "bull_researcher" in results:
        r = results["bull_researcher"]
        base.bull_argument = r.bull_argument
        base.bull_conviction = r.bull_conviction
        _append_new_reasoning(r)

    if "bear_researcher" in results:
        r = results["bear_researcher"]
        base.bear_argument = r.bear_argument
        base.bear_conviction = r.bear_conviction
        _append_new_reasoning(r)

    if "paired_researcher" in results:
        r = results["paired_researcher"]
        base.bull_argument = r.bull_argument
        base.bull_conviction = r.bull_conviction
        base.bear_argument = r.bear_argument
        base.bear_conviction = r.bear_conviction
        _append_new_reasoning(r)

    if errors:
        base.reasoning_log.append(ReasoningEntry(
            agent="System",
            action="INFO",
            reasoning="Parallel agents: one or more agents failed; continuing with partial results.",
            inputs={"errors": errors},
            outputs={},
        ))

    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "parallel_five",
            inputs={"ticker": base.ticker},
            outputs={
                "ok": sorted(list(results.keys())),
                "errors": errors,
            },
        )
    except Exception:
        pass

    return base


# ── Bull Researcher prompt ─────────────────────────────────────────────────────

BULL_RESEARCH_PROMPT = """You are the Bull Researcher.

Your job: build the strongest possible BULLISH case for {ticker} using ALL available data.
Be specific — reference actual numbers from the market context.

Market context you MUST use:
- Price change today: {price_change_pct:+.2f}%
- Market regime:      {regime}
- IV regime:          {iv_regime}  (ATM IV: {iv_atm:.1%})
- IV skew ratio:      {skew_ratio:.2f}  (>1 = puts expensive = downside fear priced in)
- Sentiment score:    {sentiment:+.3f}  (-1 bearish … +1 bullish)
- Non-news bias:      {market_bias:+.3f}  (structure/momentum; relevant when headlines are stale/absent)
- News timing:        {news_timing}  (newest headline ~{news_age} min; fresh = can react to narrative, stale = do not chase — use risk + structure)
- Key themes:         {themes}
- Momentum signal:    {momentum:+.4f}
- Volume ratio:       {vol_ratio:.2f}x avg
- Technical context (deterministic): {technical_context}
- Tier-2 structured digests (AI-enriched news lines; may overlap themes): {structured_digests}

Instructions:
1. Name 3-4 specific bullish catalysts grounded in the numbers above.
2. State the market thesis in market terms: key level(s), what confirms, and what invalidates.
3. State which allowed naked option expression fits that thesis best (short put vs short call) and why (reference IV regime).
3. Identify the single strongest argument against your case — acknowledge it briefly.
4. End with: "CONVICTION: X/10" where X is how strongly you believe in the bullish thesis.

STRICTNESS:
- Use ONLY the fields provided above. Do NOT invent earnings dates, guidance, or fundamentals not present.
- If regime/IV/news fields are "unknown" or missing, say so and lower conviction.

Be concise (5-7 sentences). Do NOT output JSON."""

BEAR_RESEARCH_PROMPT = """You are the Bear Researcher.

Your job: build the strongest possible BEARISH case for {ticker} using the market context.
Focus on concrete failure modes, tail risks, and what would invalidate a bullish thesis.

Market context:
- Price change today: {price_change_pct:+.2f}%
- Market regime:      {regime}
- IV regime:          {iv_regime}  (ATM IV: {iv_atm:.1%})
- IV skew ratio:      {skew_ratio:.2f}
- Sentiment score:    {sentiment:+.3f}
- Non-news bias:      {market_bias:+.3f}
- News timing:        {news_timing}  (newest ~{news_age} min; if stale, prioritise tail risks / mean reversion / fade rather than chasing headline direction)
- Tail risks:         {tail_risks}
- Drawdown so far:    {drawdown:.2%}
- Portfolio delta:    {port_delta:+.3f}
- Technical context (deterministic): {technical_context}
- Tier-2 structured digests: {structured_digests}

Instructions:
1. Name 2-3 concrete downside risks or regime threats, including level-based invalidation risk if technical_context provides levels.
2. State what would CONFIRM the bearish case and what would INVALIDATE it (price/level based when possible).
3. Suggest what an overly-bearish case would get WRONG (stay calibrated).
4. End with: "CONVICTION: X/10" where X is how strongly you believe in the bearish thesis.

STRICTNESS:
- Use ONLY the fields provided above. Do NOT invent catalysts, dates, or “known” events.
- If fields are unknown/missing, explicitly say “not provided” and reduce conviction.

Be concise (5-7 sentences). Do NOT output JSON."""


# ── Research nodes ─────────────────────────────────────────────────────────────

def bull_researcher_node(state: FirmState) -> FirmState:
    """T3 Node: Bull Researcher — builds the bullish case before the Strategist."""
    import re

    llm = chat_llm(
        MODELS.bull_researcher.active,
        agent_role="bull_researcher",
        temperature=0.3,
    )

    _na = state.news_newest_age_minutes
    _news_age = f"{_na:.1f}" if _na is not None else "n/a"
    _dig = (
        " | ".join(state.tier3_structured_digests[:4])
        if state.tier3_structured_digests
        else "none"
    )

    # If SentimentAnalyst hasn't populated aggregate_sentiment yet (parallel run),
    # fall back to Tier-1 SentimentMonitor score.
    _sent = (
        float(state.aggregate_sentiment or 0.0)
        if abs(float(state.aggregate_sentiment or 0.0)) > 1e-9
        else float(state.sentiment_monitor_score or 0.0)
    )

    prompt = BULL_RESEARCH_PROMPT.format(
        ticker           = state.ticker,
        price_change_pct = state.price_change_pct * 100,
        regime           = state.market_regime.value,
        iv_regime        = state.iv_regime,
        iv_atm           = state.iv_atm,
        skew_ratio       = state.iv_skew_ratio,
        sentiment        = _sent,
        market_bias      = state.market_bias_score,
        news_timing      = state.news_timing_regime,
        news_age         = _news_age,
        themes           = ", ".join(state.sentiment_themes) or "none",
        momentum         = state.momentum,
        vol_ratio        = state.vol_ratio,
        technical_context = (
            json.dumps(state.technical_context.model_dump(), separators=(",", ":"), ensure_ascii=False)
            if state.technical_context else "none"
        ),
        structured_digests = _dig[:900],
    )

    messages = [HumanMessage(content=prompt)]
    response = invoke_llm(llm, messages)
    argument = response.content.strip()

    # Extract conviction score
    m = re.search(r"CONVICTION:\s*(\d+)\s*/\s*10", argument, re.IGNORECASE)
    conviction = int(m.group(1)) if m else 5

    state.bull_argument  = argument
    state.bull_conviction = conviction

    state.reasoning_log.append(ReasoningEntry(
        agent     = "BullResearcher",
        action    = "HOLD",  # research phase, no trade decision yet
        reasoning = argument,
        inputs    = {
            "sentiment":         state.aggregate_sentiment,
            "movement_signal":   state.movement_signal,
            "regime":            state.market_regime.value,
            "news_timing_regime": state.news_timing_regime,
            "market_bias_score":  state.market_bias_score,
        },
        outputs   = {"conviction": conviction},
    ))

    log.info("BullResearcher: conviction=%d/10", conviction)
    return state


def bear_researcher_node(state: FirmState) -> FirmState:
    """T3 Node: Bear Researcher — challenges the bull case before the Strategist."""
    import re

    llm = chat_llm(
        MODELS.bear_researcher.active,
        agent_role="bear_researcher",
        temperature=0.3,
    )

    _na = state.news_newest_age_minutes
    _news_age = f"{_na:.1f}" if _na is not None else "n/a"
    _dig = (
        " | ".join(state.tier3_structured_digests[:4])
        if state.tier3_structured_digests
        else "none"
    )

    _sent = (
        float(state.aggregate_sentiment or 0.0)
        if abs(float(state.aggregate_sentiment or 0.0)) > 1e-9
        else float(state.sentiment_monitor_score or 0.0)
    )

    prompt = BEAR_RESEARCH_PROMPT.format(
        ticker           = state.ticker,
        price_change_pct = state.price_change_pct * 100,
        regime           = state.market_regime.value,
        iv_regime        = state.iv_regime,
        iv_atm           = state.iv_atm,
        skew_ratio       = state.iv_skew_ratio,
        sentiment        = _sent,
        market_bias      = state.market_bias_score,
        news_timing      = state.news_timing_regime,
        news_age         = _news_age,
        tail_risks       = ", ".join(state.sentiment_tail_risks) or "none identified",
        drawdown         = state.risk.drawdown_pct,
        port_delta       = state.risk.portfolio_delta,
        technical_context = (
            json.dumps(state.technical_context.model_dump(), separators=(",", ":"), ensure_ascii=False)
            if state.technical_context else "none"
        ),
        structured_digests = _dig[:900],
    )

    messages = [HumanMessage(content=prompt)]
    response = invoke_llm(llm, messages)
    argument = response.content.strip()

    m = re.search(r"CONVICTION:\s*(\d+)\s*/\s*10", argument, re.IGNORECASE)
    conviction = int(m.group(1)) if m else 5

    state.bear_argument  = argument
    state.bear_conviction = conviction

    state.reasoning_log.append(ReasoningEntry(
        agent     = "BearResearcher",
        action    = "HOLD",
        reasoning = argument,
        inputs    = {
            "bull_conviction": state.bull_conviction,
            "tail_risks":      state.sentiment_tail_risks,
            "drawdown":        state.risk.drawdown_pct,
        },
        outputs   = {"conviction": conviction},
    ))

    log.info("BearResearcher: conviction=%d/10", conviction)
    return state


# ── Parallel research (speed) ──────────────────────────────────────────────────

def _parallel_research_node(state: FirmState) -> FirmState:
    """
    Run BullResearcher and BearResearcher in parallel.

    After removing Bear's dependency on Bull's argument, both can be computed concurrently.
    """
    base = state
    snap = base.model_copy(deep=True)

    jobs = {
        "bull_researcher": bull_researcher_node,
        "bear_researcher": bear_researcher_node,
    }
    results: dict[str, FirmState] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {
            ex.submit(fn, snap.model_copy(deep=True)): name
            for name, fn in jobs.items()
        }
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {str(e)[:220]}"

    def _append_new_reasoning(from_state: FirmState) -> None:
        try:
            seen = {(e.agent, e.action, getattr(e, "timestamp", None)) for e in (base.reasoning_log or [])}
            for e in (from_state.reasoning_log or []):
                key = (e.agent, e.action, getattr(e, "timestamp", None))
                if key in seen:
                    continue
                base.reasoning_log.append(e)
                seen.add(key)
        except Exception:
            pass

    if "bull_researcher" in results:
        r = results["bull_researcher"]
        base.bull_argument = r.bull_argument
        base.bull_conviction = r.bull_conviction
        _append_new_reasoning(r)

    if "bear_researcher" in results:
        r = results["bear_researcher"]
        base.bear_argument = r.bear_argument
        base.bear_conviction = r.bear_conviction
        _append_new_reasoning(r)

    if errors:
        base.reasoning_log.append(ReasoningEntry(
            agent="System",
            action="INFO",
            reasoning="Parallel research: one or more agents failed; continuing with partial results.",
            inputs={"errors": errors},
            outputs={},
        ))

    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "parallel_research",
            inputs={"ticker": base.ticker},
            outputs={"ok": sorted(list(results.keys())), "errors": errors},
            duration_s=max(0.0, time.time() - _t0),
        )
    except Exception:
        pass

    return base


# ── Helper nodes ───────────────────────────────────────────────────────────────

def ingest_data_node(state: FirmState) -> FirmState:
    """
    Enrich state with deterministic features:
    - Vol surface reconstruction from option chain
    - Market regime classification (LOW_VOL / MEAN_REVERTING / TRENDING_* / HIGH_VOL)
    - Full IV metrics: ATM IV, skew ratio (25d put/call), term structure by DTE bucket
    - News age / timing regime + non-news market bias (aligned with Tier-1 desk_context)
    - Tier-2 structured ``llm_digest`` lines for ``tier3_structured_digests`` (parallel to headline LLM)
    """
    _t0 = time.time()
    try:
        from agents.desk_context import update_market_bias_score, update_news_timing_from_feed
        from agents.features import build_vol_surface, classify_regime, compute_iv_metrics
        from agents.technicals import build_technical_context_from_bars

        # Refresh spot price before any options analytics.
        # This prevents stale/incorrect `underlying_price` from poisoning near-ATM selection and regime metrics.
        try:
            from agents.data.equity_snapshot import fetch_stock_quote

            q = fetch_stock_quote(str(state.ticker or "").upper())
            last = q.get("last") if isinstance(q, dict) else None
            if last is not None:
                last_f = float(last)
                cur = float(state.underlying_price or 0.0)
                # Update if missing or materially different (quote is source of truth vs option-strike fallback).
                if cur <= 0.0 or abs(last_f - cur) / max(last_f, 1.0) >= 0.02:
                    state.underlying_price = last_f
        except Exception:
            pass

        update_news_timing_from_feed(state)
        update_market_bias_score(state)

        from agents.tier3_context import attach_structured_news_digests

        attach_structured_news_digests(state, limit=10)

        # Deterministic technical context from daily bars (EMA200, volume, S/R, outside-week flags).
        # Uses the same bar fetcher as the UI and benefits from the daily bars cache DB.
        try:
            from agents.data.chart_data import fetch_bars

            t = str(state.ticker or "").upper().strip()
            # Pull enough daily history for EMA200 + some level structure; capped by chart_data logic.
            bars, src = fetch_bars(t, timeframe="1Day", limit=260)
            state.technical_context = build_technical_context_from_bars(
                ticker=t,
                bars=bars,
                bars_source=src,
                timeframe="1Day",
            )
        except Exception:
            state.technical_context = None

        if state.latest_greeks:
            from agents.data.options_chain_filter import filter_greeks_for_agents

            greeks_for_agents = filter_greeks_for_agents(
                list(state.latest_greeks), state.underlying_price
            )
            state.latest_greeks = greeks_for_agents
            state.vol_surface   = build_vol_surface(state.ticker, state.latest_greeks)
            state.market_regime = classify_regime(state.latest_greeks)
            iv = compute_iv_metrics(state.latest_greeks, state.underlying_price)
            state.iv_atm            = iv.atm_iv
            state.iv_skew_ratio     = iv.skew_ratio
            state.iv_regime         = iv.iv_regime
            state.iv_term_structure = iv.term_structure

            # Persist ATM IV to build a rolling historical IV rank, then attach it to technical_context.
            try:
                from agents.data.iv_history_db import append_atm_iv, iv_rank

                append_atm_iv(str(state.ticker or ""), float(state.iv_atm or 0.0))
                if state.technical_context is not None:
                    r = iv_rank(str(state.ticker or ""), float(state.iv_atm or 0.0), lookback_days=30)
                    state.technical_context.iv_rank_30d = r
            except Exception:
                pass

        # A+ setup scorecard (deterministic confluence gate for naked calls/puts)
        try:
            from agents.aplus_setup import compute_aplus_setup

            state.aplus_setup = compute_aplus_setup(state)
        except Exception:
            state.aplus_setup = None
    except Exception as exc:
        log.warning("ingest_data feature build error: %s", exc)

    log.debug(
        "ingest_data: ticker=%s px=%.2f greeks=%d regime=%s atm_iv=%.1f%% skew=%.2f",
        state.ticker, state.underlying_price,
        len(state.latest_greeks or []),
        state.market_regime.value,
        state.iv_atm * 100,
        state.iv_skew_ratio,
    )
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "ingest_data",
            inputs={
                "ticker": state.ticker,
                "underlying_price": float(state.underlying_price or 0.0),
                "news_feed_count": len(state.news_feed or []),
                "latest_greeks_in": None,  # not tracked precisely here
            },
            outputs={
                "latest_greeks_count": len(state.latest_greeks or []),
                "market_regime": getattr(state.market_regime, "value", str(state.market_regime)),
                "iv_regime": state.iv_regime,
                "iv_atm": float(state.iv_atm or 0.0),
                "iv_skew_ratio": float(state.iv_skew_ratio or 0.0),
                "has_technical_context": bool(state.technical_context is not None),
            },
            duration_s=max(0.0, time.time() - _t0),
        )
    except Exception:
        pass
    return state


def early_abort_node(state: FirmState) -> FirmState:
    """Short-circuit when circuit breaker / kill switch is active."""
    state.analyst_decision   = AgentDecision.ABORT
    state.sentiment_decision = AgentDecision.ABORT
    state.risk_decision      = AgentDecision.ABORT
    state.trader_decision    = AgentDecision.ABORT
    reason = "Circuit breaker" if state.circuit_breaker_tripped else "Kill switch"
    state.reasoning_log.append(ReasoningEntry(
        agent="System", action="ABORT",
        reasoning=f"{reason} active — entire agent pipeline skipped.",
        inputs={}, outputs={},
    ))
    return state


def xai_log_node(state: FirmState) -> FirmState:
    """Persist the full reasoning log to disk after every graph run."""
    try:
        from agents.xai.reasoning_log import persist_reasoning_log
        persist_reasoning_log(state)
    except Exception as exc:
        log.warning("xai_log persist failed: %s", exc)
    return state


def recommend_node(state: FirmState) -> FirmState:
    """
    Advisory mode: park the approved proposal as a Recommendation
    instead of executing it. The user approves/dismisses from the UI.
    """
    if not state.pending_proposal and not state.pending_stock_proposal:
        return state

    # Cap pending approvals per ticker: if the desk already has too many pending
    # recommendations for this symbol, do not add more.
    try:
        cap = int(float(os.getenv("MAX_PENDING_RECS_PER_TICKER", "5")))
    except Exception:
        cap = 5
    cap = max(1, min(50, cap))
    try:
        cur_t = (state.ticker or "").upper().strip()
        pending_n = sum(
            1
            for r in (state.pending_recommendations or [])
            if (getattr(r, "ticker", "") or "").upper().strip() == cur_t
            and getattr(r, "status", "") == "pending"
        )
        if pending_n >= cap:
            state.reasoning_log.append(ReasoningEntry(
                agent="System",
                action="INFO",
                reasoning=(
                    f"Recommendation skipped: {cur_t} already has {pending_n} pending approvals "
                    f"(cap={cap})."
                ),
                inputs={"ticker": cur_t, "pending": pending_n, "cap": cap},
                outputs={"skipped_recommendation": True},
            ))
            return state
    except Exception:
        pass

    # Safety: never park expired legs as recommendations (can happen via stale persistence)
    try:
        from datetime import date as _date
        from agents.data.opra_client import occ_expiry_as_date
        from agents.data.options_chain_filter import parse_greeks_expiry_str

        today = _date.today()
        expired: list[dict] = []
        if state.pending_proposal:
            for leg in state.pending_proposal.legs:
                sym = (leg.symbol or "").strip()
                exp_d = occ_expiry_as_date(sym) or parse_greeks_expiry_str(str(leg.expiry or ""))
                if exp_d is not None and exp_d < today:
                    expired.append({"symbol": sym, "expired_on": exp_d.isoformat()})
        if expired:
            state.pending_proposal = None
            state.reasoning_log.append(ReasoningEntry(
                agent="System",
                action="INFO",
                reasoning="Proposal dropped: contains expired legs; not adding to recommendations.",
                inputs={"expired_legs": expired},
                outputs={"skipped_recommendation": True},
            ))
            return state
    except Exception:
        pass

    # Pick which recommendation to park if both exist.
    pick = "option"
    if state.pending_stock_proposal and not state.pending_proposal:
        pick = "stock"
    elif state.pending_stock_proposal and state.pending_proposal:
        # Prefer option recommendations when available so the UI actually sees them.
        # (We already cap total pending recs per ticker.)
        pick = "option"

    # Build a recommendation from the current pipeline outputs
    last_desk = next(
        (e for e in reversed(state.reasoning_log) if e.agent == "DeskHead"),
        None,
    )
    if pick == "stock" and state.pending_stock_proposal:
        rec = Recommendation(
            ticker              = state.ticker,
            asset_type          = "stock",
            strategy_name       = f"Stock ({state.pending_stock_proposal.side.value})",
            stock_proposal      = state.pending_stock_proposal,
            bull_conviction     = state.bull_conviction,
            bear_conviction     = state.bear_conviction,
            desk_head_reasoning = last_desk.reasoning if last_desk else "",
            confidence          = float(state.stock_confidence or 0.0),
        )
    else:
        if not state.pending_proposal:
            return state
        rec = Recommendation(
            ticker              = state.ticker,
            asset_type          = "option",
            strategy_name       = state.pending_proposal.strategy_name,
            proposal            = state.pending_proposal,
            bull_conviction     = state.bull_conviction,
            bear_conviction     = state.bear_conviction,
            desk_head_reasoning = last_desk.reasoning if last_desk else "",
            confidence          = float(state.strategy_confidence or 0.0),
        )
    state.pending_recommendations.append(rec)
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "recommend",
            inputs={
                "ticker": state.ticker,
                "strategy_name": state.pending_proposal.strategy_name if state.pending_proposal else "",
                "legs_count": len(state.pending_proposal.legs) if state.pending_proposal else 0,
                "trading_mode": state.trading_mode,
            },
            outputs={
                "recommendation_id": rec.id,
                "status": rec.status,
                "pending_recommendations_count": len(state.pending_recommendations or []),
            },
        )
    except Exception:
        pass

    state.reasoning_log.append(ReasoningEntry(
        agent     = "System",
        action    = "INFO",
        reasoning = (
            f"Advisory mode: recommendation '{rec.strategy_name}' for {rec.ticker} "
            f"(confidence {rec.confidence:.0%}) parked for user approval. ID: {rec.id}"
        ),
        inputs  = {"recommendation_id": rec.id},
        outputs = {"status": "pending"},
    ))

    log.info(
        "Advisory recommend: %s on %s (conf=%.2f) → pending approval [%s]",
        rec.strategy_name, rec.ticker, rec.confidence, rec.id,
    )
    return state


# ── Routing functions ──────────────────────────────────────────────────────────

def should_run_pipeline(
    state: FirmState,
) -> Literal["parallel_analysis", "early_abort"]:
    if state.circuit_breaker_tripped or state.kill_switch_active:
        return "early_abort"
    return "parallel_analysis"


def should_debate(
    state: FirmState,
) -> Literal["adversarial_debate", "desk_head"]:
    """
    Optional extra debate round on the proposal.

    Policy:
    - AUTOPILOT: run AdversarialDebate (extra safety gate) when enabled and a proposal exists.
    - ADVISORY: skip AdversarialDebate (faster; user approves anyway).
    """
    try:
        if str(getattr(state, "trading_mode", "advisory") or "advisory").lower() != "autopilot":
            return "desk_head"
    except Exception:
        return "desk_head"
    if (
        ENABLE_ADVERSARIAL_DEBATE
        and state.pending_proposal is not None
        and state.risk_decision != AgentDecision.ABORT
    ):
        return "adversarial_debate"
    return "desk_head"


def should_trade(state: FirmState) -> Literal["trader", "recommend", "xai_log"]:
    if state.trader_decision != AgentDecision.PROCEED:
        return "xai_log"
    if state.circuit_breaker_tripped or state.kill_switch_active:
        return "xai_log"
    if state.trading_mode == "autopilot":
        return "trader"
    return "recommend"


# ── Graph assembly ─────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(FirmState)

    # Register nodes
    g.add_node("ingest_data",        ingest_data_node)
    g.add_node("early_abort",        early_abort_node)
    g.add_node("parallel_analysis",  _parallel_analysis_node)
    g.add_node("parallel_five",      _parallel_five_node)
    g.add_node("stock_specialist",   stock_specialist_node)   # kept for compatibility / debugging
    g.add_node("options_specialist", options_specialist_node)  # kept for compatibility / debugging
    g.add_node("bull_researcher",    bull_researcher_node)   # kept for compatibility / debugging
    g.add_node("bear_researcher",    bear_researcher_node)   # kept for compatibility / debugging
    g.add_node("parallel_research",  _parallel_research_node)  # kept for compatibility / debugging
    g.add_node("sentiment_analyst",  sentiment_analyst_node)  # kept for compatibility / debugging
    g.add_node("strategist",         strategist_node)
    g.add_node("risk_manager",       risk_manager_node)
    g.add_node("adversarial_debate", adversarial_debate_node)
    g.add_node("desk_head",          desk_head_node)
    g.add_node("trader",             trader_node)
    g.add_node("recommend",          recommend_node)
    g.add_node("xai_log",            xai_log_node)

    # Entry → gate on circuit breaker
    g.add_edge(START, "ingest_data")
    g.add_conditional_edges("ingest_data", should_run_pipeline, {
        "parallel_analysis":  "parallel_analysis",
        "early_abort":        "early_abort",
    })
    g.add_edge("early_abort", "xai_log")

    # Two-stage parallelization:
    # Stage 1: stock/options/sentiment (parallel_analysis)
    # Stage 2: bull/bear (parallel_research)
    g.add_edge("parallel_analysis", "parallel_research")
    g.add_edge("parallel_research", "strategist")

    # Risk gate → optional extra debate → desk head
    g.add_edge("strategist", "risk_manager")
    g.add_conditional_edges("risk_manager", should_debate, {
        "adversarial_debate": "adversarial_debate",
        "desk_head":          "desk_head",
    })
    g.add_edge("adversarial_debate", "desk_head")

    # Execution gate (branches on trading_mode)
    g.add_conditional_edges("desk_head", should_trade, {
        "trader":    "trader",
        "recommend": "recommend",
        "xai_log":   "xai_log",
    })
    g.add_edge("trader",    "xai_log")
    g.add_edge("recommend", "xai_log")
    g.add_edge("xai_log", END)

    return g


# Compiled graph — importable singleton
compiled_graph = build_graph().compile()


def _coerce_firm_state(obj: Any) -> FirmState:
    if isinstance(obj, FirmState):
        return obj
    if isinstance(obj, dict):
        return FirmState.model_validate(obj)
    raise TypeError(f"Unexpected graph state type: {type(obj)!r}")


def run_cycle(state: FirmState) -> tuple[FirmState, Exception | None]:
    """
    Execute one T3 agent cycle synchronously.
    Streams the graph so partial state is always available if a node raises.
    """
    from agents.xai.reasoning_log import persist_reasoning_log

    last: Any = state
    try:
        for chunk in compiled_graph.stream(state, stream_mode="values"):
            last = chunk
        return _coerce_firm_state(last), None
    except Exception as exc:
        try:
            partial = _coerce_firm_state(last)
        except Exception as conv_exc:
            log.error(
                "Could not coerce partial graph state after %s: %s",
                type(exc).__name__, conv_exc,
            )
            raise exc from conv_exc
        try:
            persist_reasoning_log(partial)
        except Exception as persist_exc:
            log.warning("partial reasoning persist failed: %s", persist_exc)
        return partial, exc


async def run_cycle_async(state: FirmState) -> tuple[FirmState, Exception | None]:
    """Async wrapper — runs run_cycle in a thread pool."""
    return await asyncio.to_thread(run_cycle, state)
