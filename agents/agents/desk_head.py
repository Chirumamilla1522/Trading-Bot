"""
Desk Head – Supervisor Agent

Synthesises all sub-agent verdicts, confidence scores, and the adversarial
debate outcome into a final go/no-go decision with reasoning.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentDecision, FirmState, ReasoningEntry
from agents.config import MODELS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import DeskHeadOutput, parse_and_validate

SYSTEM_PROMPT = """ROLE: DeskHead (Supervisor / final decision)
You are the Desk Head of an autonomous options trading desk.
You review reports from specialist agents and make the final go/no-go decision.

Weighting guide (higher weight = more authoritative):
- RiskManager ABORT → always override to ABORT (non-negotiable)
- Debate verdict (weight 0.35): most balanced view, includes full debate
- OptionsSpecialist (weight 0.25): structural IV opportunity
- Sentiment (weight 0.20): news-driven directional bias
- Strategy confidence (weight 0.20): how good the specific proposal is

Decision rules:
1. If risk_decision == ABORT → output ABORT, no exceptions.
2. If debate_verdict == ABORT → output ABORT.
3. If all three (analyst, sentiment, debate) == PROCEED with confidence > 0.6 → PROCEED.
4. Any ABORT among analyst, sentiment, or strategy_confidence < 0.45 → HOLD.
5. Otherwise: weigh signals and use judgment. Capital preservation > returns.

Output STRICT JSON:
{
  "decision":       "PROCEED" | "HOLD" | "ABORT",
  "confidence":     0.0-1.0,
  "signal_weights": {"risk": 1.0, "debate": 0.35, "specialist": 0.25, "sentiment": 0.20, "strategy": 0.20},
  "reasoning":      "<4-5 sentences: MUST cite concrete fields from the report: underlying_price, iv_regime/iv_atm/skew_ratio, aggregate_sentiment, and any ABORT/HOLD reasons.>"
}

Do not output anything except the JSON object."""


def desk_head_node(state: FirmState) -> FirmState:
    # Hard gate: RiskManager ABORT is unconditional
    if state.risk_decision == AgentDecision.ABORT:
        decision   = AgentDecision.ABORT
        confidence = 1.0
        reasoning  = (
            f"Risk Manager issued ABORT. "
            f"Regime: {state.market_regime.value}, IV: {state.iv_regime}. "
            "No further analysis needed — hard risk limit violated."
        )
        state.trader_decision = decision
        state.reasoning_log.append(ReasoningEntry(
            agent="DeskHead", action=decision.value,
            reasoning=reasoning,
            inputs={"risk_decision": "ABORT"},
            outputs={"confidence": confidence},
        ))
        return state

    # Also gate on circuit breaker
    if state.circuit_breaker_tripped:
        state.trader_decision = AgentDecision.ABORT
        state.reasoning_log.append(ReasoningEntry(
            agent="DeskHead", action="ABORT",
            reasoning="Circuit breaker active — all trading halted.",
            inputs={}, outputs={"confidence": 1.0},
        ))
        return state

    llm = chat_llm(
        MODELS.desk_head.active,
        agent_role="desk_head",
        temperature=0.05,
        default_headers={"X-Title": "Agentic Trading Terminal"},
    )

    context = {
        "ticker":               state.ticker,
        "underlying_price":     state.underlying_price,
        "market_regime":        state.market_regime.value,
        "iv_regime":            state.iv_regime,
        "iv_atm":               f"{state.iv_atm:.1%}",
        "skew_ratio":           state.iv_skew_ratio,
        "aggregate_sentiment":  state.aggregate_sentiment,
        "key_themes":           state.sentiment_themes,
        "tail_risks":           state.sentiment_tail_risks,
        "sub_agent_verdicts": {
            "options_specialist": {
                "decision":   state.analyst_decision.value,
                "confidence": state.analyst_confidence,
            },
            "sentiment_analyst": {
                "decision":   state.sentiment_decision.value,
                "confidence": state.sentiment_confidence,
            },
            "risk_manager": {
                "decision":   state.risk_decision.value,
                "confidence": state.risk_confidence,
            },
        },
        "debate": {
            "verdict":   state.debate_record.verdict.value if state.debate_record else "N/A",
            "summary":   state.debate_record.summary[:400] if state.debate_record else "",
        },
        "pending_proposal": (
            {
                "strategy_name":  state.pending_proposal.strategy_name,
                "max_risk":       state.pending_proposal.max_risk,
                "target_return":  state.pending_proposal.target_return,
                "confidence":     state.pending_proposal.confidence,
                "legs_count":     len(state.pending_proposal.legs),
            }
            if state.pending_proposal else None
        ),
        "portfolio": {
            "delta":        state.risk.portfolio_delta,
            "vega":         state.risk.portfolio_vega,
            "daily_pnl":    state.risk.daily_pnl,
            "drawdown_pct": f"{state.risk.drawdown_pct:.2%}",
        },
        "strategy_confidence": state.strategy_confidence,
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"Sub-agent reports:\n{json.dumps(context, indent=2)}"),
    ]

    response = invoke_llm(llm, messages)
    out = parse_and_validate(response.content, DeskHeadOutput, "DeskHead")
    if not out:
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON matching this schema (no markdown, no prose):\n"
            "{\n"
            '  "decision":"PROCEED|HOLD|ABORT",\n'
            '  "confidence":0.0,\n'
            '  "signal_weights":{"risk":1.0,"debate":0.35,"specialist":0.25,"sentiment":0.2,"strategy":0.2},\n'
            '  "reasoning":"..."\n'
            "}\n"
            "If you cannot comply, output HOLD with confidence 0.0 and short reasoning."
        )
        llm_repair = chat_llm(
            MODELS.desk_head.active,
            agent_role="desk_head",
            temperature=0.0,
            max_tokens=450,
            default_headers={"X-Title": "Agentic Trading Terminal"},
        )
        resp2 = invoke_llm(llm_repair, [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(response.content or "")[:2400]),
        ])
        out = parse_and_validate(resp2.content, DeskHeadOutput, "DeskHead")

    if out:
        decision   = AgentDecision(out.decision)
        confidence = out.confidence
        reasoning  = out.reasoning
    else:
        decision   = AgentDecision.HOLD
        confidence = 0.0
        reasoning  = f"Schema validation failed: {response.content[:300]}"

    state.trader_decision = decision
    state.reasoning_log.append(ReasoningEntry(
        agent="DeskHead", action=decision.value,
        reasoning=reasoning,
        inputs=context,
        outputs={"confidence": confidence},
    ))
    return state
