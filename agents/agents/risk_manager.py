"""
Risk Manager – Greek Neutrality & Capital Guardrails

Deterministic hard-limit checks run first (no LLM spend on obvious violations).
LLM soft assessment only runs when hard limits pass.
"""
from __future__ import annotations

import json
import os

from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import AgentDecision, FirmState, ReasoningEntry
from agents.config import MODELS, MAX_DAILY_DRAWDOWN, MAX_POSITION_PCT
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import RiskManagerOutput, parse_and_validate

# Hard limits (deterministic, not adjustable via LLM)
# NOTE: RiskMetrics.portfolio_delta is stored as "share-equivalent delta" (≈ $ PnL per $1 move),
# computed as sum(delta × qty × 100). It is NOT normalized to [-1,1].
# We therefore limit delta exposure as % of NAV (delta_notional / NAV), not by an absolute 0.1 band.
_DELTA_LIMIT_PCT_NAV  = float(os.getenv("PORTFOLIO_DELTA_LIMIT_PCT_NAV", "0.5"))  # 25% NAV notional delta
_GAMMA_LIMIT         =  500.0  # $ per 1-point move
_VEGA_LIMIT          = 1_000.0  # $ per 1% IV move
_MAX_OPEN_POSITIONS  =  10      # max simultaneous option spreads
_MIN_STRATEGY_CONF   =  0.40    # reject proposals with confidence < this

SYSTEM_PROMPT = """ROLE: RiskManager (Capital preservation / execution risk)
You are the risk manager.

All hard limits have already been checked deterministically. Your role is a softer
second-opinion on market-risk and execution risk.

Evaluate:
1. MARKET RISK: Does the proposal's max_risk / target_return ratio make sense for the regime?
   For naked short options: runaway trend + confirming volume + outside-week confirmed is high tail risk.
2. CONCENTRATION: Is the trade correlated with existing positions? Too many longs/shorts in same direction?
3. EXECUTION RISK: Are the bid-ask spreads acceptable? Wide spread → uncertain fill price.
4. TIMING: Use term-structure stress (backwardation) as a risk flag. Do NOT assume specific events.

UNITS (must follow):
- `pending_proposal.max_risk` and `target_return` are USD. Options are quoted per-share; all dollar risk math is assumed to already include the 100x contract multiplier.
- Do NOT “correct” units or convert per-share to dollars here. If the proposal looks internally inconsistent, HOLD and describe why.

GROUNDING REQUIREMENTS (must follow):
- Your reasoning MUST cite at least 6 concrete fields from context, including:
  drawdown_pct, portfolio_delta, portfolio_vega, market_regime, iv_regime, skew_ratio,
  and proposal max_risk/target_return (if present).
- If pending_proposal is null, output HOLD with a single clear reason.

Hard limits already enforced (do NOT revisit them):
- Max daily drawdown: {max_dd}%
- Max position size: {max_pos}%
- Delta exposure (notional): ≤ {delta_limit_pct:.0%} of NAV
- Gamma limit: ${gamma_limit}/pt
- Vega limit: ${vega_limit}/1% IV

Output STRICT JSON:
{{
  "decision":   "PROCEED" | "HOLD" | "ABORT",
  "violations": ["<specific risk identified>"],
  "risk_reward_ok": true | false,
  "execution_risk": "LOW" | "MEDIUM" | "HIGH",
  "reasoning":  "<3-4 sentences. Be specific and cite the numbers you used: IV regime, skew_ratio, drawdown_pct, position size/max_risk.>"
}}

ABORT only if you identify a material risk the hard checks missed. Otherwise PROCEED or HOLD."""


def risk_manager_node(state: FirmState) -> FirmState:
    _t0 = __import__("time").time()
    violations: list[str] = []

    # ── Gate 0: Control flags ─────────────────────────────────────────────────
    if state.circuit_breaker_tripped or state.kill_switch_active:
        reason = "Circuit breaker" if state.circuit_breaker_tripped else "Kill switch"
        state.risk_decision = AgentDecision.ABORT
        state.risk_confidence = 1.0
        state.reasoning_log.append(ReasoningEntry(
            agent="RiskManager", action="ABORT",
            reasoning=f"{reason} is active.",
            inputs={}, outputs={"violations": [reason.upper()]},
        ))
        return state

    r = state.risk

    # ── Gate 1: Daily drawdown ────────────────────────────────────────────────
    if r.drawdown_pct >= r.max_drawdown_pct:
        violations.append(
            f"DAILY_DRAWDOWN {r.drawdown_pct:.2%} >= limit {r.max_drawdown_pct:.2%}"
        )

    # ── Gate 2: Portfolio delta exposure (notional % of NAV) ──────────────────
    # portfolio_delta is share-equivalent; convert to notional exposure using spot.
    try:
        nav = max(float(r.current_nav or 0.0), float(state.account_equity or 0.0), 1.0)
        spot = max(float(state.underlying_price or 0.0), 0.0)
        delta_shares = float(r.portfolio_delta or 0.0)
        delta_notional = abs(delta_shares) * spot if spot > 0 else abs(delta_shares)
        delta_pct_nav = delta_notional / nav if nav > 0 else 0.0
        if delta_pct_nav > _DELTA_LIMIT_PCT_NAV:
            violations.append(
                f"PORTFOLIO_DELTA_EXPOSURE {delta_pct_nav:.1%} NAV "
                f"(delta_shares={delta_shares:+.1f}, spot={spot:.2f}, nav=${nav:,.0f}) "
                f"> limit {(_DELTA_LIMIT_PCT_NAV):.0%}"
            )
    except Exception:
        pass

    # ── Gate 3: Portfolio gamma ────────────────────────────────────────────────
    if abs(r.portfolio_gamma) > _GAMMA_LIMIT:
        violations.append(
            f"PORTFOLIO_GAMMA ${r.portfolio_gamma:.0f}/pt exceeds limit ${_GAMMA_LIMIT:.0f}/pt"
        )

    # ── Gate 4: Portfolio vega ────────────────────────────────────────────────
    if abs(r.portfolio_vega) > _VEGA_LIMIT:
        violations.append(
            f"PORTFOLIO_VEGA ${r.portfolio_vega:.0f}/1%IV exceeds limit ${_VEGA_LIMIT:.0f}/1%IV"
        )

    # ── Gate 5: Too many open positions ──────────────────────────────────────
    if len(state.open_positions) >= _MAX_OPEN_POSITIONS:
        violations.append(
            f"POSITION_COUNT {len(state.open_positions)} >= max {_MAX_OPEN_POSITIONS}"
        )

    # ── Gate 6: Proposal checks (if present) ─────────────────────────────────
    if state.pending_proposal:
        nav = max(r.current_nav, state.account_equity, 1.0)
        prop_risk_pct = state.pending_proposal.max_risk / nav
        if prop_risk_pct > r.position_cap_pct:
            violations.append(
                f"POSITION_SIZE {prop_risk_pct:.2%} > cap {r.position_cap_pct:.2%} "
                f"(max_risk=${state.pending_proposal.max_risk:.0f}, NAV=${nav:.0f})"
            )
        if state.strategy_confidence < _MIN_STRATEGY_CONF:
            violations.append(
                f"LOW_STRATEGY_CONFIDENCE {state.strategy_confidence:.2f} < {_MIN_STRATEGY_CONF}"
            )

    # Hard violations → ABORT immediately, no LLM call
    if violations:
        state.risk_decision   = AgentDecision.ABORT
        state.risk_confidence = 1.0
        state.reasoning_log.append(ReasoningEntry(
            agent="RiskManager", action="ABORT",
            reasoning=f"Hard limit violations: {'; '.join(violations)}",
            inputs=r.model_dump(),
            outputs={"violations": violations},
        ))
        return state

    # ── LLM soft assessment ───────────────────────────────────────────────────
    llm = chat_llm(
        MODELS.risk_manager.active,
        agent_role="risk_manager",
        temperature=0.0,
    )

    system = SYSTEM_PROMPT.format(
        max_dd=f"{MAX_DAILY_DRAWDOWN * 100:.0f}",
        max_pos=f"{MAX_POSITION_PCT * 100:.0f}",
        delta_limit_pct=_DELTA_LIMIT_PCT_NAV,
        gamma_limit=_GAMMA_LIMIT,
        vega_limit=_VEGA_LIMIT,
    )

    context = {
        "risk_metrics": r.model_dump(),
        "pending_proposal": (
            state.pending_proposal.model_dump() if state.pending_proposal else None
        ),
        "open_positions_count": len(state.open_positions),
        "market_regime":        state.market_regime.value,
        "iv_regime":            state.iv_regime,
        "iv_skew_ratio":        state.iv_skew_ratio,
        "iv_term_structure":    state.iv_term_structure,
        "technical_context":    (state.technical_context.model_dump() if state.technical_context else None),
        "aggregate_sentiment":  state.aggregate_sentiment,
        "tail_risks":           state.sentiment_tail_risks,
        "strategy_confidence":  state.strategy_confidence,
        "analyst_confidence":   state.analyst_confidence,
        "news_timing_regime":   state.news_timing_regime,
        "news_newest_age_minutes": state.news_newest_age_minutes,
        "market_bias_score":    state.market_bias_score,
        "movement_anomaly":     state.movement_anomaly,
    }

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=json.dumps(context, indent=2)),
    ]

    response = invoke_llm(llm, messages)
    out = parse_and_validate(response.content, RiskManagerOutput, "RiskManager")
    if not out:
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON matching this schema (no markdown, no prose):\n"
            "{\n"
            '  "decision":"PROCEED|HOLD|ABORT",\n'
            '  "violations":["..."],\n'
            '  "risk_reward_ok":true,\n'
            '  "execution_risk":"LOW|MEDIUM|HIGH",\n'
            '  "reasoning":"..."\n'
            "}\n"
            "If you cannot comply, output HOLD, execution_risk HIGH, and explain briefly."
        )
        llm_repair = chat_llm(
            MODELS.risk_manager.active,
            agent_role="risk_manager",
            temperature=0.0,
        )
        resp2 = invoke_llm(llm_repair, [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(response.content or "")[:2400]),
        ])
        out = parse_and_validate(resp2.content, RiskManagerOutput, "RiskManager")

    if out:
        decision       = AgentDecision(out.decision)
        reasoning      = out.reasoning
        llm_violations = out.violations
        confidence     = 1.0 if decision == AgentDecision.PROCEED else 0.5
    else:
        decision       = AgentDecision.HOLD
        reasoning      = response.content[:400]
        llm_violations = []
        confidence     = 0.5

    state.risk_decision   = decision
    state.risk_confidence = confidence
    state.reasoning_log.append(ReasoningEntry(
        agent="RiskManager", action=decision.value,
        reasoning=reasoning,
        inputs=context,
        outputs={"violations": llm_violations, "confidence": confidence},
    ))
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "risk_manager",
            inputs={
                "ticker": state.ticker,
                "drawdown_pct": float(getattr(state.risk, "drawdown_pct", 0.0) or 0.0),
                "portfolio_delta": float(getattr(state.risk, "portfolio_delta", 0.0) or 0.0),
                "portfolio_gamma": float(getattr(state.risk, "portfolio_gamma", 0.0) or 0.0),
                "portfolio_vega": float(getattr(state.risk, "portfolio_vega", 0.0) or 0.0),
                "proposal_present": bool(state.pending_proposal),
            },
            outputs={
                "decision": decision.value,
                "violations_n": len(llm_violations or []),
                "confidence": float(confidence or 0.0),
            },
            duration_s=max(0.0, __import__("time").time() - _t0),
        )
    except Exception:
        pass
    return state
