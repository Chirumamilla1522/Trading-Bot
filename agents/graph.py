"""
Tier-3 LangGraph Pipeline — Full Research & Execution Graph
===========================================================

Tier-1 (SentimentMonitor + MovementTracker) and Tier-2 (FundamentalsRefresher)
run as always-on background loops managed by tiers.py.

This file defines Tier-3: the triggered, LLM-heavy research and execution
pipeline.  It is invoked by:
  • Manual UI button  → POST /run_cycle
  • T3 watchdog auto-trigger (tiers.py) when T1 signals align
  • Scanner anomaly hook

Pipeline flow:

  [ingest_data]
       │  (gate: circuit breaker / kill switch → early_abort)
       ▼
  [options_specialist]    ← IV surface + skew + chain analytics
       │
  [sentiment_analyst]     ← LLM news analysis → sentiment score + themes
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
import json
import logging
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents.state import AgentDecision, FirmState, Recommendation, ReasoningEntry
from agents.agents.desk_head         import desk_head_node
from agents.agents.options_specialist import options_specialist_node
from agents.agents.sentiment_analyst  import sentiment_analyst_node
from agents.agents.risk_manager       import risk_manager_node
from agents.agents.strategist         import strategist_node
from agents.agents.adversarial_debate import adversarial_debate_node
from agents.agents.trader             import trader_node
from agents.config                    import ENABLE_ADVERSARIAL_DEBATE, MODELS
from agents.llm_providers             import chat_llm
from agents.llm_retry                 import invoke_llm

log = logging.getLogger(__name__)


# ── Bull Researcher prompt ─────────────────────────────────────────────────────

BULL_RESEARCH_PROMPT = """You are the Bull Researcher at an autonomous options trading desk.

Your job: build the strongest possible BULLISH case for {ticker} using ALL available data.
Be specific — reference actual numbers from the market context.

Market context you MUST use:
- Price change today: {price_change_pct:+.2f}%
- Market regime:      {regime}
- IV regime:          {iv_regime}  (ATM IV: {iv_atm:.1%})
- IV skew ratio:      {skew_ratio:.2f}  (>1 = puts expensive = downside fear priced in)
- Sentiment score:    {sentiment:+.3f}  (-1 bearish … +1 bullish)
- Key themes:         {themes}
- Momentum signal:    {momentum:+.4f}
- Volume ratio:       {vol_ratio:.2f}x avg

Instructions:
1. Name 3-4 specific bullish catalysts grounded in the numbers above.
2. State which options strategy benefits most and why (reference IV regime).
3. Identify the single strongest argument against your case — acknowledge it briefly.
4. End with: "CONVICTION: X/10" where X is how strongly you believe in the bullish thesis.

Be concise (5-7 sentences). Do NOT output JSON."""

BEAR_RESEARCH_PROMPT = """You are the Bear Researcher at an autonomous options trading desk.

The Bull Researcher made the following argument:
---
{bull_argument}
---

Your job: challenge this with the strongest possible BEARISH case for {ticker}.
Directly rebut the Bull's weakest points. Use specific numbers.

Market context:
- Price change today: {price_change_pct:+.2f}%
- Market regime:      {regime}
- IV regime:          {iv_regime}  (ATM IV: {iv_atm:.1%})
- IV skew ratio:      {skew_ratio:.2f}
- Sentiment score:    {sentiment:+.3f}
- Tail risks:         {tail_risks}
- Drawdown so far:    {drawdown:.2%}
- Portfolio delta:    {port_delta:+.3f}

Instructions:
1. Pick the Bull's 2 weakest points and dismantle them with data.
2. Name 2-3 concrete downside risks or regime threats.
3. Suggest what an overly-bearish case would get WRONG (stay calibrated).
4. End with: "CONVICTION: X/10" where X is how strongly you believe in the bearish thesis.

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

    prompt = BULL_RESEARCH_PROMPT.format(
        ticker           = state.ticker,
        price_change_pct = state.price_change_pct * 100,
        regime           = state.market_regime.value,
        iv_regime        = state.iv_regime,
        iv_atm           = state.iv_atm,
        skew_ratio       = state.iv_skew_ratio,
        sentiment        = state.aggregate_sentiment,
        themes           = ", ".join(state.sentiment_themes) or "none",
        momentum         = state.momentum,
        vol_ratio        = state.vol_ratio,
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
            "sentiment":        state.aggregate_sentiment,
            "movement_signal":  state.movement_signal,
            "regime":           state.market_regime.value,
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

    prompt = BEAR_RESEARCH_PROMPT.format(
        ticker           = state.ticker,
        bull_argument    = state.bull_argument or "(no bull argument provided)",
        price_change_pct = state.price_change_pct * 100,
        regime           = state.market_regime.value,
        iv_regime        = state.iv_regime,
        iv_atm           = state.iv_atm,
        skew_ratio       = state.iv_skew_ratio,
        sentiment        = state.aggregate_sentiment,
        tail_risks       = ", ".join(state.sentiment_tail_risks) or "none identified",
        drawdown         = state.risk.drawdown_pct,
        port_delta       = state.risk.portfolio_delta,
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


# ── Helper nodes ───────────────────────────────────────────────────────────────

def ingest_data_node(state: FirmState) -> FirmState:
    """
    Enrich state with deterministic features:
    - Vol surface reconstruction from option chain
    - Market regime classification (LOW_VOL / MEAN_REVERTING / TRENDING_* / HIGH_VOL)
    - Full IV metrics: ATM IV, skew ratio (25d put/call), term structure by DTE bucket
    """
    try:
        from agents.features import build_vol_surface, classify_regime, compute_iv_metrics

        if state.latest_greeks:
            state.vol_surface   = build_vol_surface(state.ticker, state.latest_greeks)
            state.market_regime = classify_regime(state.latest_greeks)
            iv = compute_iv_metrics(state.latest_greeks, state.underlying_price)
            state.iv_atm            = iv.atm_iv
            state.iv_skew_ratio     = iv.skew_ratio
            state.iv_regime         = iv.iv_regime
            state.iv_term_structure = iv.term_structure
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
    if not state.pending_proposal:
        return state

    # Build a recommendation from the current pipeline outputs
    last_desk = next(
        (e for e in reversed(state.reasoning_log) if e.agent == "DeskHead"),
        None,
    )
    rec = Recommendation(
        ticker              = state.ticker,
        strategy_name       = state.pending_proposal.strategy_name,
        proposal            = state.pending_proposal,
        bull_conviction     = state.bull_conviction,
        bear_conviction     = state.bear_conviction,
        desk_head_reasoning = last_desk.reasoning if last_desk else "",
        confidence          = state.strategy_confidence,
    )
    state.pending_recommendations.append(rec)

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
) -> Literal["options_specialist", "early_abort"]:
    if state.circuit_breaker_tripped or state.kill_switch_active:
        return "early_abort"
    return "options_specialist"


def should_debate(
    state: FirmState,
) -> Literal["adversarial_debate", "desk_head"]:
    """Optional extra debate round on the proposal — only if enabled and proposal exists."""
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
    g.add_node("options_specialist", options_specialist_node)
    g.add_node("bull_researcher",    bull_researcher_node)
    g.add_node("bear_researcher",    bear_researcher_node)
    g.add_node("sentiment_analyst",  sentiment_analyst_node)
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
        "options_specialist": "options_specialist",
        "early_abort":        "early_abort",
    })
    g.add_edge("early_abort", "xai_log")

    # Analysis: IV → sentiment → bull research → bear rebuttal → strategist
    g.add_edge("options_specialist", "sentiment_analyst")
    g.add_edge("sentiment_analyst",  "bull_researcher")
    g.add_edge("bull_researcher",    "bear_researcher")
    g.add_edge("bear_researcher",    "strategist")

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
