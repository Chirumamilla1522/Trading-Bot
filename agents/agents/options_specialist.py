"""
Options Specialist – Volatility Surface & Arbitrage Analysis

Analyses the IV surface using pre-computed deterministic metrics (skew, term
structure, regime) so the LLM focuses on interpretation and opportunity
identification rather than arithmetic.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentDecision, FirmState, ReasoningEntry
from agents.config import MODELS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.features import build_chain_analytics, compute_iv_metrics
from agents.schemas import OptionsSpecialistOutput, parse_and_validate

SYSTEM_PROMPT = """ROLE: OptionsSpecialist (Volatility Surface / Structure)
You are the options surface specialist at a proprietary desk. Your job is NOT to be generic.
You must interpret the CURRENT ticker’s option surface and turn it into a clear go/no-go
signal plus one concrete structural opportunity (if any).

You will be given a JSON context containing:
- ticker, underlying_price, market_regime
- portfolio_delta/vega/theta, open_positions count
- chain_analytics (precomputed): iv_metrics (atm_iv, skew_ratio, iv_regime), term_structure,
  near_atm_contracts and highest_iv_contracts (OCC symbols), plus any derived metrics

Think step-by-step:
1. REGIME: Is IV elevated (sell premium) or compressed (buy gamma)?
2. SKEW: Does put_skew > 1.20 signal institutional hedging demand (bullish for credit spreads)?
   Does skew_ratio > 1.30 indicate extreme fear (opportunity for put credit spreads)?
3. TERM STRUCTURE: Is the curve in contango (normal) or backwardation (stressed)?
   Steep contango favors calendar spreads. Backwardation → near-term event risk.
4. OPPORTUNITY: Given regime + skew + term structure, what is the best structural trade?
5. CONFIDENCE: How clean is the signal? Conflicting signals → lower confidence.

STRICTNESS (must follow):
- Use ONLY the provided JSON context. Do NOT use outside market knowledge, do NOT assume earnings dates.
- Do NOT invent liquidity/spread facts; if not provided, say “not provided”.
- If key fields are missing/zero/unknown (e.g., underlying_price <= 0, no near_atm_contracts, iv_regime missing),
  you MUST choose HOLD with low confidence and state exactly what is missing.

GROUNDING REQUIREMENTS (must follow):
- Your reasoning MUST cite at least 4 concrete fields from the context, including:
  underlying_price, atm_iv, skew_ratio, iv_regime, and one term-structure detail.
- If you mention liquidity/spreads, do so ONLY if the context provides it; otherwise say “not provided”.
- If you output PROCEED, opportunity must be specific (e.g., “put credit spread 21–45d”, “calendar spread”, “iron condor wide wings”).

Output STRICT JSON:
{
  "decision":      "PROCEED" | "HOLD" | "ABORT",
  "iv_regime":     "LOW" | "NORMAL" | "ELEVATED" | "EXTREME",
  "skew_signal":   "PUT_PREMIUM" | "CALL_PREMIUM" | "NEUTRAL",
  "term_signal":   "CONTANGO" | "BACKWARDATION" | "FLAT",
  "opportunity":   "<specific structure or null>",
  "preferred_dte_bucket": "<e.g. '21-45d'>",
  "confidence":    0.0-1.0,
  "reasoning":     "<3-5 sentences: cite numbers and explain the structure you’d prefer>"
}

ABORT only for: (a) system-level trading halt flags in context, or (b) truly extreme stress clearly evidenced in the provided fields
(e.g., iv_regime=EXTREME with atm_iv >= 0.80 AND term_signal=BACKWARDATION). Otherwise HOLD/PROCEED."""


def options_specialist_node(state: FirmState) -> FirmState:
    _t0 = __import__("time").time()
    llm = chat_llm(
        MODELS.options_specialist.active,
        agent_role="options_specialist",
        temperature=0.05,
    )

    # Build rich pre-computed analytics — no arithmetic for the LLM
    analytics = build_chain_analytics(state.latest_greeks, state.underlying_price)
    iv_metrics = compute_iv_metrics(state.latest_greeks, state.underlying_price)

    context = {
        "ticker":            state.ticker,
        "underlying_price":  state.underlying_price,
        "market_regime":     state.market_regime.value,
        "portfolio_delta":   state.risk.portfolio_delta,
        "portfolio_vega":    state.risk.portfolio_vega,
        "portfolio_theta":   state.risk.portfolio_theta,
        "open_positions":    len(state.open_positions),
        "chain_analytics":   analytics,
        "desk": {
            "sentiment_monitor_score":    round(state.sentiment_monitor_score, 4),
            "sentiment_monitor_source":   state.sentiment_monitor_source,
            "news_timing_regime":        state.news_timing_regime,
            "market_bias_score":         round(state.market_bias_score, 4),
        },
        "tier3_structured_digests": state.tier3_structured_digests[:8],
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, indent=2)),
    ]

    response = invoke_llm(llm, messages)
    out = parse_and_validate(response.content, OptionsSpecialistOutput, "OptionsSpecialist")
    if not out:
        # One-shot repair pass: ask the model to output STRICT JSON only.
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON matching exactly this schema (no markdown, no prose):\n"
            "{\n"
            '  "decision": "PROCEED|HOLD|ABORT",\n'
            '  "iv_regime": "LOW|NORMAL|ELEVATED|EXTREME",\n'
            '  "skew_signal": "PUT_PREMIUM|CALL_PREMIUM|NEUTRAL",\n'
            '  "term_signal": "CONTANGO|BACKWARDATION|FLAT",\n'
            '  "opportunity": "string or null",\n'
            '  "preferred_dte_bucket": "string or null",\n'
            '  "confidence": 0.0,\n'
            '  "reasoning": "string"\n'
            "}\n"
            "If you cannot comply, output HOLD with confidence 0.0 and a short reason."
        )
        repair_msgs = [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(response.content or "")[:2400]),
        ]
        llm_repair = chat_llm(
            MODELS.options_specialist.active,
            agent_role="options_specialist",
            temperature=0.0,
            max_tokens=450,
        )
        resp2 = invoke_llm(llm_repair, repair_msgs)
        out = parse_and_validate(resp2.content, OptionsSpecialistOutput, "OptionsSpecialist")

    if out:
        decision    = AgentDecision(out.decision)
        reasoning   = out.reasoning
        confidence  = out.confidence
        opportunity = out.opportunity
    else:
        decision    = AgentDecision.HOLD
        reasoning   = response.content[:400]
        confidence  = 0.0
        opportunity = None

    # Propagate IV metrics into state so other agents can read them
    state.iv_atm        = iv_metrics.atm_iv
    state.iv_skew_ratio = iv_metrics.skew_ratio
    state.iv_regime     = iv_metrics.iv_regime
    state.iv_term_structure = iv_metrics.term_structure
    state.analyst_decision  = decision
    state.analyst_confidence = confidence

    state.reasoning_log.append(ReasoningEntry(
        agent    = "OptionsSpecialist",
        action   = decision.value,
        reasoning = reasoning,
        inputs   = {
            "atm_iv":       analytics["iv_metrics"]["atm_iv"],
            "iv_regime":    analytics["iv_metrics"]["iv_regime"],
            "skew_ratio":   analytics["iv_metrics"]["skew_ratio"],
            "term_structure": analytics["term_structure"],
        },
        outputs  = {
            "confidence": confidence,
            "opportunity": opportunity,
        },
    ))
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "options_specialist",
            inputs={
                "ticker": state.ticker,
                "underlying_price": float(state.underlying_price or 0.0),
                "iv_regime": str(state.iv_regime),
                "atm_iv": float(state.iv_atm or 0.0),
                "skew_ratio": float(state.iv_skew_ratio or 0.0),
                "near_atm_contracts_n": len((analytics or {}).get("near_atm_contracts") or []),
            },
            outputs={
                "decision": decision.value,
                "confidence": float(confidence or 0.0),
                "opportunity": opportunity,
            },
            duration_s=max(0.0, __import__("time").time() - _t0),
        )
    except Exception:
        pass
    return state
