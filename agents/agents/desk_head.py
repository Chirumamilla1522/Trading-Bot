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
from agents.llm_retry import invoke_llm, invoke_llm_with_metrics
from agents.schemas import DeskHeadOutput, parse_and_validate

SYSTEM_PROMPT = """ROLE: DeskHead (Supervisor / final decision)
You are the supervisor. You review reports from specialist agents and make the final go/no-go decision.

Weighting guide (higher weight = more authoritative):
- RiskManager ABORT → always override to ABORT (non-negotiable)
- Debate verdict (weight 0.35): most balanced view, includes full debate
- OptionsSpecialist (weight 0.25): structural IV opportunity
- StockSpecialist (weight 0.20): underlying cash equity opportunity (when clean)
- Sentiment (weight 0.20): headline-driven bias when news_timing is fresh; down-weight
  stale headline narratives — then lean on market_bias, IV, and movement in the report.
- Strategy confidence (weight 0.20): how good the specific proposal is

Decision rules:
1. If risk_decision == ABORT → output ABORT, no exceptions.
2. If debate_verdict == ABORT → output ABORT.
3. If all three (analyst, sentiment, debate) == PROCEED with confidence > 0.6 → PROCEED.
4. Any ABORT among analyst, sentiment, or strategy_confidence < 0.45 → HOLD.
5. If news_timing_regime is stale/moderate, do not PROCEED solely on bullish headline
   sentiment if price has already moved — require structure (IV/momentum/bias) alignment.
6. Otherwise: weigh signals and use judgment. Capital preservation > returns.

STRICTNESS (must follow):
- Use ONLY the provided report JSON. Do NOT invent prices, regimes, earnings dates, or liquidity.
- If any critical fields are missing/unknown (e.g. underlying_price <= 0, iv_regime missing, pending_proposal missing),
  you MUST output HOLD and explicitly list what is missing.
- If you PROCEED, you must mention the proposal's max_risk (USD) and why it is acceptable vs drawdown/position cap in the report.

Output STRICT JSON:
{
  "decision":       "PROCEED" | "HOLD" | "ABORT",
  "confidence":     0.0-1.0,
  "signal_weights": {"risk": 1.0, "debate": 0.35, "options": 0.25, "stock": 0.20, "sentiment": 0.20, "strategy": 0.20},
  "reasoning":      "<4-5 sentences: MUST cite concrete fields from the report: underlying_price, iv_regime/iv_atm/skew_ratio, aggregate_sentiment, and any ABORT/HOLD reasons.>"
}

Do not output anything except the JSON object."""


def desk_head_node(state: FirmState) -> FirmState:
    _t0 = __import__("time").time()
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
        try:
            from agents.tracking.mlflow_tracing import log_agent_step
            log_agent_step(
                "desk_head",
                inputs={"ticker": state.ticker, "risk_decision": "ABORT"},
                outputs={"decision": decision.value, "confidence": float(confidence)},
                duration_s=max(0.0, __import__("time").time() - _t0),
            )
        except Exception:
            pass
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
        "technical_context":    (state.technical_context.model_dump() if state.technical_context else None),
        "aggregate_sentiment":  state.aggregate_sentiment,
        "key_themes":           state.sentiment_themes,
        "tail_risks":           state.sentiment_tail_risks,
        "sub_agent_verdicts": {
            "stock_specialist": {
                "decision":   state.stock_decision.value,
                "confidence": state.stock_confidence,
                "proposal": (
                    {
                        "side": state.pending_stock_proposal.side.value,
                        "qty": state.pending_stock_proposal.qty,
                        "order_type": state.pending_stock_proposal.order_type,
                        "limit_price": state.pending_stock_proposal.limit_price,
                    }
                    if state.pending_stock_proposal else None
                ),
            },
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
        "pending_stock_proposal": (
            state.pending_stock_proposal.model_dump()
            if state.pending_stock_proposal else None
        ),
        "portfolio": {
            "delta":        state.risk.portfolio_delta,
            "vega":         state.risk.portfolio_vega,
            "daily_pnl":    state.risk.daily_pnl,
            "drawdown_pct": f"{state.risk.drawdown_pct:.2%}",
        },
        "strategy_confidence": state.strategy_confidence,
        "stock_confidence":    state.stock_confidence,
        "desk_context": {
            "news_timing_regime":      state.news_timing_regime,
            "news_newest_age_minutes": state.news_newest_age_minutes,
            "market_bias_score":       state.market_bias_score,
            "movement_signal":         state.movement_signal,
            "movement_anomaly":        state.movement_anomaly,
            "sentiment_monitor": {
                "score": state.sentiment_monitor_score,
                "source": state.sentiment_monitor_source,
                "confidence": state.sentiment_monitor_confidence,
            },
            "structured_digest_count": len(state.tier3_structured_digests),
        },
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"Sub-agent reports:\n{json.dumps(context, indent=2)}"),
    ]

    response, llm_meta = invoke_llm_with_metrics(llm, messages)
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
            default_headers={"X-Title": "Agentic Trading Terminal"},
        )
        resp2, llm_meta_repair = invoke_llm_with_metrics(llm_repair, [
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
        outputs={
            "confidence": confidence,
            "llm_call": llm_meta,
            "llm_repair_call": (llm_meta_repair if "llm_meta_repair" in locals() else None),
        },
    ))
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "desk_head",
            inputs={
                "ticker": state.ticker,
                "sub_agent_verdicts": context.get("sub_agent_verdicts"),
                "debate_verdict": (context.get("debate") or {}).get("verdict"),
                "strategy_confidence": float(state.strategy_confidence or 0.0),
            },
            outputs={"decision": decision.value, "confidence": float(confidence or 0.0)},
            duration_s=max(0.0, __import__("time").time() - _t0),
        )
    except Exception:
        pass
    return state
