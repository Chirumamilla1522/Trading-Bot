"""
Strategist – Regime-Aware Trade Proposal Generator

Creates a concrete TradeProposal by matching the current market regime, IV regime,
and sentiment signal to an appropriate options strategy. Uses pre-computed chain
analytics so the LLM focuses on strategy selection, not contract arithmetic.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.config import MODELS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.features import build_chain_analytics
from agents.schemas import StrategistOutput, parse_and_validate
from agents.state import (
    AgentDecision, FirmState, OptionRight, OrderSide,
    ReasoningEntry, TradeLeg, TradeProposal,
)

# Regime → strategy recommendation table (injected into prompt)
_REGIME_STRATEGY_GUIDE = """
REGIME-TO-STRATEGY MATRIX (follow this unless contradicted by skew/sentiment):
┌─────────────────┬────────────────┬────────────────────────────────────────────────────────┐
│ Market Regime   │ IV Regime      │ Preferred Strategies                                   │
├─────────────────┼────────────────┼────────────────────────────────────────────────────────┤
│ TRENDING_UP     │ LOW/NORMAL     │ Bull Call Spread, Long Call, Bull Put Spread (credit)  │
│ TRENDING_UP     │ ELEVATED       │ Bull Put Spread (collect premium), Cash-secured Put    │
│ TRENDING_DOWN   │ ELEVATED       │ Bear Put Spread, Long Put, Protective Collar           │
│ TRENDING_DOWN   │ EXTREME        │ HOLD or Bear Put Spread (small size)                   │
│ MEAN_REVERTING  │ ELEVATED       │ Iron Condor, Iron Butterfly, Short Strangle            │
│ MEAN_REVERTING  │ NORMAL         │ Iron Condor, Butterfly Spread                          │
│ HIGH_VOL        │ ELEVATED/EXTREME│ Iron Condor (wide wings), HOLD                        │
│ LOW_VOL         │ LOW            │ Long Straddle, Long Strangle, Calendar Spread          │
│ UNKNOWN         │ ANY            │ HOLD (insufficient data)                               │
└─────────────────┴────────────────┴────────────────────────────────────────────────────────┘

Skew modifiers:
- skew_ratio > 1.25 (fear bid): prefer put credit spreads, iron condors weighted to put side
- skew_ratio < 0.85 (call bid): prefer call credit spreads, bearish positioning
- sentiment > 0.4: lean bullish; sentiment < -0.3: lean bearish
"""

SYSTEM_PROMPT = f"""ROLE: Strategist (Strategy selection + proposal assembly)
You are the strategy constructor for an autonomous options desk. Your job is to choose ONE
options strategy that fits THIS ticker RIGHT NOW, using the provided context (price/IV/skew/regime,
sentiment, researcher conviction, portfolio risk limits), and then assemble a valid proposal.

{_REGIME_STRATEGY_GUIDE}

SIZING RULES:
- max_risk (max dollar loss) must be ≤ position_cap_pct × current_nav
- target_return = max_risk × reward_risk_ratio (aim for ≥ 1.5:1 on debit spreads, ≥ 0.33:1 on credit)
- Keep it simple: 2-4 legs, qty 1 each (scale up only if max_risk allows 2+)
- For credit spreads: max_risk = width_of_spread × 100 − premium_collected × 100
- For debit spreads: max_risk = net_debit × 100

ONLY use OCC symbols that appear in the provided `near_atm_contracts` list.
If near_atm_contracts is empty or confidence < 0.5, output HOLD.

RESEARCHER DEBATE GUIDANCE:
- The context includes `bull_researcher` and `bear_researcher` outputs from dedicated
  researcher agents. Use their conviction scores (1-10) to calibrate your confidence.
- If bull_conviction > bear_conviction by ≥3 → lean bullish strategy.
- If bear_conviction > bull_conviction by ≥3 → lean bearish or HOLD.
- If convictions are equal → favour market-neutral (iron condor, butterfly).

GROUNDING REQUIREMENTS (must follow):
- In the PROCEED rationale, cite at least 6 concrete fields from context, including:
  underlying_price, market_regime, iv_regime, skew_ratio, aggregate_sentiment,
  price_change_pct or movement_signal, and position_cap_dollars/max_risk.
- You MUST only use OCC symbols from `near_atm_contracts`. Do NOT invent symbols.
- If `near_atm_contracts` is empty OR you are unsure about strikes/expiry, output HOLD.

Output STRICT JSON (one of these two forms only):

HOLD:
{{"decision": "HOLD", "reason": "<1-2 sentences>"}}

PROCEED:
{{
  "decision": "PROCEED",
  "proposal": {{
    "strategy_name": "<e.g. Iron Condor>",
    "legs": [
      {{"symbol":"<OCC>", "right":"CALL|PUT", "strike":<float>, "expiry":"<YYMMDD>", "side":"BUY|SELL", "qty":<int>}}
    ],
    "max_risk":      <float>,
    "target_return": <float>,
    "stop_loss_pct": <0.0-1.0>,
    "take_profit_pct": <0.0-1.0>,
    "rationale":     "<3-4 sentences: why this strategy, why these strikes, what is the thesis>",
    "confidence":    <0.0-1.0>
  }}
}}

Do not output anything except the JSON object."""


def strategist_node(state: FirmState) -> FirmState:
    # Gate on analyst + sentiment confidence
    if state.analyst_confidence < 0.3 and state.sentiment_confidence < 0.3:
        state.pending_proposal = None
        state.reasoning_log.append(ReasoningEntry(
            agent="Strategist", action="HOLD",
            reasoning="Both analyst and sentiment confidence too low (<0.3). Skipping proposal generation.",
            inputs={"analyst_confidence": state.analyst_confidence,
                    "sentiment_confidence": state.sentiment_confidence},
            outputs={"skipped": True},
        ))
        return state

    llm = chat_llm(
        MODELS.strategist.active,
        agent_role="strategist",
        temperature=0.1,
    )

    # Use pre-computed chain analytics
    analytics = build_chain_analytics(state.latest_greeks, state.underlying_price)

    # Existing position summary (avoid doubling up on same underlying)
    existing_summary = [
        {"symbol": p.symbol, "right": p.right.value, "strike": p.strike,
         "expiry": p.expiry, "qty": p.quantity}
        for p in state.open_positions[:10]
    ]

    # NAV-based sizing guidance
    nav = max(state.risk.current_nav, state.account_equity, 10_000.0)
    position_cap = nav * state.risk.position_cap_pct

    context = {
        "ticker":              state.ticker,
        "underlying_price":    state.underlying_price,
        "market_regime":       state.market_regime.value,
        "iv_regime":           state.iv_regime or analytics["iv_metrics"]["iv_regime"],
        "iv_atm":              analytics["iv_metrics"]["atm_iv"],
        "skew_ratio":          analytics["iv_metrics"]["skew_ratio"],
        "term_structure":      analytics["term_structure"],
        "aggregate_sentiment": state.aggregate_sentiment,
        "sentiment_themes":    state.sentiment_themes,
        "analyst_confidence":  round(state.analyst_confidence, 2),
        "risk_metrics": {
            "portfolio_delta":   state.risk.portfolio_delta,
            "portfolio_vega":    state.risk.portfolio_vega,
            "daily_pnl":         state.risk.daily_pnl,
            "drawdown_pct":      f"{state.risk.drawdown_pct:.2%}",
        },
        "nav":                  round(nav, 2),
        "position_cap_dollars": round(position_cap, 2),
        "existing_positions":   existing_summary,
        "near_atm_contracts":   analytics["near_atm_contracts"],
        "highest_iv_contracts": analytics["highest_iv_contracts"],
        "existing_proposal":    state.pending_proposal.model_dump() if state.pending_proposal else None,
        # T3 researcher debate outputs (may be empty on first cycle)
        "movement_signal":      round(state.movement_signal, 4),
        "price_change_pct":     round(state.price_change_pct * 100, 3),
        "bull_researcher": {
            "argument":    state.bull_argument[:800] if state.bull_argument else "",
            "conviction":  state.bull_conviction,
        },
        "bear_researcher": {
            "argument":    state.bear_argument[:800] if state.bear_argument else "",
            "conviction":  state.bear_conviction,
        },
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, indent=2)),
    ]

    response = invoke_llm(llm, messages)
    out = parse_and_validate(response.content, StrategistOutput, "Strategist")
    if not out:
        # One-shot repair pass: coerce the model into STRICT JSON only.
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON in ONE of these two forms (no markdown, no prose):\n\n"
            'HOLD: {"decision":"HOLD","reason":"..."}\n\n'
            'PROCEED: {"decision":"PROCEED","proposal":{"strategy_name":"...","legs":[{"symbol":"<OCC>","right":"CALL|PUT","strike":0.0,"expiry":"YYMMDD","side":"BUY|SELL","qty":1}],"max_risk":0.0,"target_return":0.0,"stop_loss_pct":0.5,"take_profit_pct":0.75,"rationale":"...","confidence":0.0}}\n\n'
            "If near_atm_contracts was empty OR you are unsure, choose HOLD."
        )
        repair_msgs = [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(response.content or "")[:2600]),
        ]
        llm_repair = chat_llm(
            MODELS.strategist.active,
            agent_role="strategist",
            temperature=0.0,
            max_tokens=650,
        )
        resp2 = invoke_llm(llm_repair, repair_msgs)
        out = parse_and_validate(resp2.content, StrategistOutput, "Strategist")

    proposal: TradeProposal | None = None
    decision   = AgentDecision.HOLD
    reasoning  = ""
    confidence = 0.0

    if out:
        decision = AgentDecision(out.decision)
        if decision == AgentDecision.PROCEED and out.proposal:
            p = out.proposal
            legs = []
            for leg in p.legs:
                try:
                    legs.append(TradeLeg(
                        symbol=leg.symbol,
                        right=OptionRight(leg.right),
                        strike=leg.strike,
                        expiry=leg.expiry,
                        side=OrderSide(leg.side),
                        qty=leg.qty,
                    ))
                except Exception:
                    continue

            if not legs:
                decision  = AgentDecision.HOLD
                reasoning = "Strategist PROCEED but no valid legs parsed."
            else:
                proposal = TradeProposal(
                    strategy_name   = p.strategy_name,
                    legs            = legs,
                    max_risk        = p.max_risk,
                    target_return   = p.target_return,
                    rationale       = p.rationale,
                    confidence      = p.confidence,
                    stop_loss_pct   = p.stop_loss_pct,
                    take_profit_pct = p.take_profit_pct,
                )
                reasoning  = proposal.rationale
                confidence = proposal.confidence
                # Validate max_risk against position cap
                if proposal.max_risk > position_cap * 1.1:
                    decision  = AgentDecision.HOLD
                    reasoning = (f"max_risk ${proposal.max_risk:.0f} exceeds cap "
                                 f"${position_cap:.0f}. Downgrading to HOLD.")
                    proposal  = None
        else:
            reasoning = out.reason or ""
    else:
        decision  = AgentDecision.HOLD
        reasoning = f"Schema validation failed: {response.content[:250]}"
        proposal  = None

    state.pending_proposal   = proposal if decision == AgentDecision.PROCEED else None
    state.strategy_confidence = confidence

    state.reasoning_log.append(ReasoningEntry(
        agent="Strategist",
        action=decision.value,
        reasoning=reasoning,
        inputs={
            "ticker":          state.ticker,
            "regime":          state.market_regime.value,
            "iv_regime":       state.iv_regime,
            "skew_ratio":      analytics["iv_metrics"]["skew_ratio"],
            "sentiment":       state.aggregate_sentiment,
            "position_cap":    round(position_cap, 2),
        },
        outputs={
            "has_proposal":    proposal is not None,
            "confidence":      confidence,
            "strategy_name":   proposal.strategy_name if proposal else None,
            "legs_count":      len(proposal.legs) if proposal else 0,
            "max_risk":        proposal.max_risk if proposal else None,
            "target_return":   proposal.target_return if proposal else None,
        },
    ))
    return state
