"""
Bull / Bear Adversarial Debate – Structured Argumentation with Scoring

Before the Desk Head makes a final call, a Bull researcher advocates for the
trade while a Bear researcher challenges it. Each turn includes a numerical
confidence score. The Judge receives full market context for a calibrated verdict.

Rounds default to 2 (configurable via DEBATE_ROUNDS) to minimise token cost.
"""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.state import AgentDecision, DebateRecord, DebateTurn, FirmState, ReasoningEntry
from agents.config import MODELS, DEBATE_ROUNDS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import DebateJudgeOutput, parse_and_validate

BULL_SYSTEM = """ROLE: Bull (Advocate / best-case)
You are the Bull side of a structured desk debate. Your job is to argue FOR the proposed trade
using ONLY the provided market context and the proposal details.

GROUNDING REQUIREMENTS:
- Cite at least 6 concrete fields from the market context: underlying_price, market_regime,
  iv_regime and iv_atm, skew_ratio, term_structure, aggregate_sentiment, plus movement_signal or price_change_pct.
- Point out why the proposal’s structure matches IV/skew/term (not vibes).

Output: 4-6 sentences, no lists. End with exactly: CONVICTION: X/10 (X=1..10).
Do NOT discuss risk controls (Bear handles that)."""

BEAR_SYSTEM = """ROLE: Bear (Skeptic / failure modes)
You are the Bear side of a structured desk debate. Your job is to argue AGAINST the proposed trade
by identifying concrete failure modes, execution risk, and regime mismatch using ONLY the provided context.

GROUNDING REQUIREMENTS:
- Cite at least 6 concrete fields from market context and proposal (e.g., max_risk vs cap, regime,
  iv_regime/iv_atm, skew_ratio, term_structure, drawdown_pct, portfolio delta/vega, sentiment/tail_risks).
- Attack the Bull’s weakest claim directly and explain what would invalidate it.

Output: 4-6 sentences, no lists. End with exactly: CONVICTION: X/10 (X=1..10)."""

JUDGE_SYSTEM = """You are the Desk Head judging a structured Bull/Bear debate.
Review all turns AND the market context. Produce a final verdict.

Weigh arguments by specificity (data-backed > opinion) and conviction score.
Capital preservation takes priority: a Bear conviction ≥ 8 overrides Bull unless
Bull also scores ≥ 8 with clear data backing.

Output STRICT JSON:
{{
  "verdict":        "PROCEED" | "HOLD" | "ABORT",
  "bull_score":     1-10,
  "bear_score":     1-10,
  "winning_side":   "BULL" | "BEAR" | "TIE",
  "summary":        "<4-6 sentences: MUST cite concrete fields from market context (price, iv_regime/iv_atm/skew_ratio, aggregate_sentiment, portfolio delta/vega) and 1-2 key debate claims.>",
  "confidence":     0.0-1.0
}}"""


def _extract_conviction(text: str) -> int:
    """Extract CONVICTION: X/10 from argument text."""
    import re
    m = re.search(r"CONVICTION:\s*(\d+)\s*/\s*10", text, re.IGNORECASE)
    return int(m.group(1)) if m else 5


def _build_llm(role: Literal["bull", "bear", "judge"]):
    model_cfg = (
        MODELS.bull_researcher if role == "bull" else
        MODELS.bear_researcher if role == "bear" else
        MODELS.desk_head
    )
    agent_role = (
        "bull_researcher" if role == "bull" else
        "bear_researcher" if role == "bear" else
        "adversarial_judge"
    )
    return chat_llm(
        model_cfg.active,
        agent_role=agent_role,
        temperature=0.25 if role != "judge" else 0.05,
    )


def adversarial_debate_node(state: FirmState) -> FirmState:
    if not state.pending_proposal:
        return state

    proposal_str = json.dumps(state.pending_proposal.model_dump(), indent=2)
    market_ctx = {
        "ticker":              state.ticker,
        "underlying_price":    state.underlying_price,
        "market_regime":       state.market_regime.value,
        "iv_regime":           state.iv_regime,
        "iv_atm":              f"{state.iv_atm:.1%}",
        "skew_ratio":          state.iv_skew_ratio,
        "term_structure":      state.iv_term_structure,
        "aggregate_sentiment": state.aggregate_sentiment,
        "key_themes":          state.sentiment_themes,
        "tail_risks":          state.sentiment_tail_risks,
        "portfolio_delta":     state.risk.portfolio_delta,
        "portfolio_vega":      state.risk.portfolio_vega,
        "daily_pnl":           state.risk.daily_pnl,
        "drawdown_pct":        f"{state.risk.drawdown_pct:.2%}",
        "analyst_confidence":  state.analyst_confidence,
        "strategy_confidence": state.strategy_confidence,
    }
    context_str = json.dumps(market_ctx, indent=2)
    opening = (
        f"Proposed trade:\n{proposal_str}\n\n"
        f"Market context:\n{context_str}"
    )

    turns: list[DebateTurn] = []
    bull_llm  = _build_llm("bull")
    bear_llm  = _build_llm("bear")
    judge_llm = _build_llm("judge")

    bull_history  = [SystemMessage(content=BULL_SYSTEM), HumanMessage(content=opening)]
    bear_history  = [SystemMessage(content=BEAR_SYSTEM), HumanMessage(content=opening)]

    bull_conviction_total = 0
    bear_conviction_total = 0
    rounds = max(1, DEBATE_ROUNDS)

    for turn_idx in range(rounds):
        # Bull speaks
        bull_response = invoke_llm(bull_llm, bull_history)
        bull_arg      = bull_response.content.strip()
        bull_conv     = _extract_conviction(bull_arg)
        bull_conviction_total += bull_conv
        turns.append(DebateTurn(agent="Bull", argument=f"[conv:{bull_conv}/10] {bull_arg}", turn=turn_idx + 1))
        bull_history.append(AIMessage(content=bull_arg))
        bear_history.append(HumanMessage(content=f"Bull argues (conviction {bull_conv}/10):\n{bull_arg}\n\nYour rebuttal:"))

        # Bear speaks
        bear_response = invoke_llm(bear_llm, bear_history)
        bear_arg      = bear_response.content.strip()
        bear_conv     = _extract_conviction(bear_arg)
        bear_conviction_total += bear_conv
        turns.append(DebateTurn(agent="Bear", argument=f"[conv:{bear_conv}/10] {bear_arg}", turn=turn_idx + 1))
        bear_history.append(AIMessage(content=bear_arg))
        bull_history.append(HumanMessage(content=f"Bear argues (conviction {bear_conv}/10):\n{bear_arg}\n\nYour counter:"))

    # Judge evaluates with full transcript AND market context
    debate_transcript = "\n\n".join(
        f"[Turn {t.turn} – {t.agent}]: {t.argument}" for t in turns
    )
    judge_messages = [
        SystemMessage(content=JUDGE_SYSTEM),
        HumanMessage(content=(
            f"Proposal:\n{proposal_str}\n\n"
            f"Market context:\n{context_str}\n\n"
            f"Debate transcript:\n{debate_transcript}\n\n"
            f"Cumulative Bull conviction: {bull_conviction_total}/{rounds * 10}\n"
            f"Cumulative Bear conviction: {bear_conviction_total}/{rounds * 10}"
        )),
    ]

    judge_response = invoke_llm(judge_llm, judge_messages)
    jout = parse_and_validate(judge_response.content, DebateJudgeOutput, "DebateJudge")
    if not jout:
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON matching this schema (no markdown, no prose):\n"
            "{\n"
            '  "verdict":"PROCEED|HOLD|ABORT",\n'
            '  "bull_score":5,\n'
            '  "bear_score":5,\n'
            '  "winning_side":"BULL|BEAR|TIE",\n'
            '  "summary":"...",\n'
            '  "confidence":0.0\n'
            "}\n"
            "If unsure, verdict=HOLD with low confidence."
        )
        judge_llm_repair = chat_llm(
            MODELS.desk_head.active,
            agent_role="adversarial_judge",
            temperature=0.0,
            max_tokens=650,
        )
        resp2 = invoke_llm(judge_llm_repair, [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(judge_response.content or "")[:2600]),
        ])
        jout = parse_and_validate(resp2.content, DebateJudgeOutput, "DebateJudge")

    if jout:
        verdict    = AgentDecision(jout.verdict)
        summary    = jout.summary
        confidence = jout.confidence
    else:
        verdict    = AgentDecision.HOLD
        summary    = judge_response.content[:500]
        confidence = 0.5

    turns.append(DebateTurn(agent="Judge", argument=summary, turn=rounds + 1))

    state.debate_record = DebateRecord(
        proposal=state.pending_proposal.strategy_name,
        turns=turns,
        verdict=verdict,
        summary=summary,
    )
    state.reasoning_log.append(ReasoningEntry(
        agent="AdversarialDebate", action=verdict.value,
        reasoning=summary,
        inputs={
            "rounds":        rounds,
            "proposal":      state.pending_proposal.strategy_name,
            "bull_total":    bull_conviction_total,
            "bear_total":    bear_conviction_total,
        },
        outputs={"confidence": confidence},
    ))
    return state
