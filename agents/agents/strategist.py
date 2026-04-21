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
from agents.data.options_chain_filter import parse_greeks_expiry_str
from agents.data.opra_client import occ_expiry_as_date
from agents.schemas import StrategistOutput, parse_and_validate
from agents.state import (
    AgentDecision, FirmState, OptionRight, OrderSide,
    ReasoningEntry, TradeLeg, TradeProposal,
)


def _classify_option_structure(proposal: TradeProposal) -> str:
    """
    Classify proposal into a small set of leg-pattern structures.
    Returns: SINGLE | VERTICAL | IRON_CONDOR | CALENDAR | OTHER
    """
    try:
        legs = proposal.legs or []
        if not legs:
            return "OTHER"
        n = len(legs)
        rights = {l.right for l in legs}
        expiries = {str(l.expiry or "") for l in legs}
        strikes = [float(l.strike) for l in legs]

        if n == 1:
            return "SINGLE"

        if n == 2:
            # Vertical: same expiry + same right, different strike.
            if len(expiries) == 1 and len(rights) == 1:
                if abs(strikes[1] - strikes[0]) > 0:
                    return "VERTICAL"
            # Calendar: same right + same strike, different expiry.
            if len(rights) == 1 and len(expiries) == 2:
                if abs(strikes[1] - strikes[0]) < 1e-9:
                    return "CALENDAR"
            return "OTHER"

        if n == 4 and len(expiries) == 1:
            puts = [l for l in legs if l.right == OptionRight.PUT]
            calls = [l for l in legs if l.right == OptionRight.CALL]
            if len(puts) == 2 and len(calls) == 2:
                return "IRON_CONDOR"
        return "OTHER"
    except Exception:
        return "OTHER"

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
│-────────────────┴────────────────┴────────────────────────────────────────────────────────┘

Skew modifiers:
- skew_ratio > 1.25 (fear bid): prefer put credit spreads, 
- skew_ratio < 0.85 (call bid): prefer call credit spreads, bearish positioning
- sentiment > 0.4: lean bullish; sentiment < -0.3: lean bearish
"""

# MEAN_REVERTING  │ ELEVATED       │ Iron Condor, Iron Butterfly, Short Strangle            │
# │ HIGH_VOL        │ ELEVATED/EXTREME│ Iron Condor (wide wings), HOLD                        │
# │ LOW_VOL         │ LOW            │ Long Straddle, Long Strangle, Calendar Spread          │
# └─
# iron condors weighted to put side
SYSTEM_PROMPT = f"""ROLE: Strategist (Strategy selection + proposal assembly)
You are the strategy constructor for an autonomous options desk. Your job is to choose ONE
options strategy that fits THIS ticker RIGHT NOW, using the provided context (price/IV/skew/regime,
sentiment, researcher conviction, portfolio risk limits), and then assemble a valid proposal.

{_REGIME_STRATEGY_GUIDE}

OPTION-RIGHTS CONSTRAINT (must follow):
- The context includes `allowed_option_rights` which is one of: CALL | PUT | BOTH.
- If CALL: all legs MUST be CALL.
- If PUT:  all legs MUST be PUT.
- If BOTH: no restriction.
- If you cannot satisfy the constraint using OCC symbols in `near_atm_contracts`, output HOLD.

OPTION-STRUCTURE CONSTRAINT (must follow):
- The context includes `allowed_option_structures`: one of ["ALL"] or a subset of:
  SINGLE | VERTICAL | IRON_CONDOR | CALENDAR.
- If it is not ["ALL"], you MUST only propose a strategy whose leg pattern matches one of the allowed structures.
- If you cannot satisfy the constraint using OCC symbols in `near_atm_contracts`, output HOLD.

SIZING RULES:
- max_risk (max dollar loss) must be ≤ position_cap_pct × current_nav
- target_return = max_risk × reward_risk_ratio (aim for ≥ 1.5:1 on debit spreads, ≥ 0.33:1 on credit)
- Keep it simple: 2-4 legs, qty 1 each (scale up only if max_risk allows 2+)
- Options prices are per-share; contract notional uses a 100x multiplier: dollars = option_price × qty × 100
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

TIMING (multi-horizon, not HFT):
- If `news_timing_regime` is fresh, headline+price narratives may justify directional risk.
- If stale or none, prefer thesis from `market_bias_score`, movement_signal, iv_regime —
  do not assume edge from old headlines.

GROUNDING REQUIREMENTS (must follow):
- In the PROCEED rationale, cite at least 6 concrete fields from context, including:
  underlying_price, market_regime, iv_regime, skew_ratio, aggregate_sentiment,
  news_timing_regime or market_bias_score, price_change_pct or movement_signal,
  and position_cap_dollars/max_risk.
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
    # Gate: need specialist conviction OR non-news structure (bias / anomaly)
    if (
        state.analyst_confidence < 0.3
        and state.sentiment_confidence < 0.3
        and abs(state.market_bias_score) < 0.35
        and not state.movement_anomaly
    ):
        state.pending_proposal = None
        state.reasoning_log.append(ReasoningEntry(
            agent="Strategist", action="HOLD",
            reasoning=(
                "Analyst and sentiment confidence low (<0.3) and no strong "
                "non-news signal (market_bias/anomaly). Skipping proposal generation."
            ),
            inputs={
                "analyst_confidence": state.analyst_confidence,
                "sentiment_confidence": state.sentiment_confidence,
                "market_bias_score": state.market_bias_score,
                "movement_anomaly": state.movement_anomaly,
            },
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

    def _compute_mid(bid: float, ask: float) -> float | None:
        try:
            bid = float(bid)
            ask = float(ask)
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        return round((bid + ask) / 2.0, 2)

    def _estimate_max_loss_usd_from_quotes(proposal: TradeProposal) -> float | None:
        """
        Best-effort max loss estimate in USD using mids from latest_greeks.
        We only use this to detect the common "forgot ×100" unit mistake.
        """
        try:
            gmap = {g.symbol: g for g in (state.latest_greeks or [])}
            legs = proposal.legs or []
            if not legs:
                return None

            # Net premium in USD (+credit, -debit)
            missing = 0
            net_usd = 0.0
            for leg in legs:
                g = gmap.get(leg.symbol)
                if not g:
                    missing += 1
                    continue
                m = _compute_mid(getattr(g, "bid", 0.0), getattr(g, "ask", 0.0))
                if m is None:
                    missing += 1
                    continue
                sign = 1.0 if leg.side == OrderSide.SELL else -1.0
                net_usd += sign * float(m) * float(leg.qty) * 100.0

            if missing:
                return None

            # Debit-style: worst case loss ≈ debit paid.
            if net_usd < 0:
                return round(abs(net_usd), 2)

            # Credit-style heuristics.
            credit_usd = max(0.0, net_usd)
            rights = {l.right for l in legs}
            expiries = {str(l.expiry or "") for l in legs}

            # Vertical spread: 2 legs, same right+expiry, qty 1 (or same qty).
            if len(legs) == 2 and len(rights) == 1 and len(expiries) == 1:
                strikes = sorted([float(l.strike) for l in legs])
                width = abs(strikes[1] - strikes[0])
                return round(max(0.0, width * 100.0 - credit_usd), 2)

            # Iron condor: 2 puts + 2 calls, same expiry.
            puts = [l for l in legs if l.right == OptionRight.PUT]
            calls = [l for l in legs if l.right == OptionRight.CALL]
            if len(puts) == 2 and len(calls) == 2 and len(expiries) == 1:
                put_strikes = sorted([float(l.strike) for l in puts])
                call_strikes = sorted([float(l.strike) for l in calls])
                width_put = abs(put_strikes[1] - put_strikes[0])
                width_call = abs(call_strikes[1] - call_strikes[0])
                width = max(width_put, width_call)
                return round(max(0.0, width * 100.0 - credit_usd), 2)

            # Fallback: for unknown credit structures, use a conservative proxy.
            # This isn't used for trading decisions, only unit-normalization.
            return round(max(0.0, credit_usd), 2)
        except Exception:
            return None

    context = {
        "ticker":              state.ticker,
        "underlying_price":    state.underlying_price,
        "market_regime":       state.market_regime.value,
        "allowed_option_rights": (state.allowed_option_rights or "BOTH"),
        "allowed_option_structures": (state.allowed_option_structures or ["ALL"]),
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
        "news_timing_regime":    state.news_timing_regime,
        "news_newest_age_minutes": state.news_newest_age_minutes,
        "market_bias_score":     round(state.market_bias_score, 4),
        "tier3_structured_digests": state.tier3_structured_digests[:8],
        "fundamentals_snapshot": {
            "pe_ratio": state.fundamentals.get("pe_ratio"),
            "fwd_pe":   state.fundamentals.get("fwd_pe"),
            "beta":     state.fundamentals.get("beta"),
            "sector":   state.fundamentals.get("sector"),
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

                # Enforce allowed rights deterministically (user preference).
                allowed = (state.allowed_option_rights or "BOTH").strip().upper()
                if allowed in ("CALL", "PUT"):
                    bad = [l.symbol for l in (proposal.legs or []) if l.right.value != allowed]
                    if bad:
                        decision = AgentDecision.HOLD
                        reasoning = (
                            f"Rejected proposal: allowed_option_rights={allowed} "
                            f"but proposal contains other rights: {bad[:4]}"
                        )
                        confidence = 0.0
                        proposal = None
                        # Skip multiplier normalization / cap validation.
                        # Continue to final section which clears pending_proposal.
                # Enforce allowed structures deterministically (user preference).
                if proposal is not None:
                    allowed_structs = state.allowed_option_structures or ["ALL"]
                    allowed_structs = [str(x or "").strip().upper() for x in allowed_structs if str(x or "").strip()]
                    if not allowed_structs:
                        allowed_structs = ["ALL"]
                    if "ALL" not in allowed_structs:
                        kind = _classify_option_structure(proposal)
                        if kind not in allowed_structs:
                            decision = AgentDecision.HOLD
                            reasoning = (
                                f"Rejected proposal: structure={kind} not in allowed_option_structures={allowed_structs}."
                            )
                            confidence = 0.0
                            proposal = None

                # Normalize common unit mistake: model forgets options are quoted per-share (×100 per contract).
                # If the proposal looks off by ~100x relative to live mids, correct it deterministically.
                est_loss = _estimate_max_loss_usd_from_quotes(proposal)
                try:
                    mr = float(proposal.max_risk)
                    tr = float(proposal.target_return)
                except Exception:
                    mr = tr = 0.0
                if est_loss is not None and mr > 0:
                    # Detect if max_risk likely provided in "option price units" instead of dollars.
                    if mr < 25 and est_loss >= 100:
                        scaled = mr * 100.0
                        rel_err = abs(est_loss - scaled) / max(est_loss, 1.0)
                        if rel_err <= 0.25:
                            proposal.max_risk = round(scaled, 2)
                            # target_return should be in dollars too; scale if it appears similarly small.
                            if tr > 0 and tr < 250:
                                proposal.target_return = round(tr * 100.0, 2)
                            state.reasoning_log.append(ReasoningEntry(
                                agent="Strategist",
                                action="NORMALIZED_MULTIPLIER",
                                reasoning=(
                                    "Normalized proposal dollars by ×100 contract multiplier "
                                    f"(estimated_max_loss≈${est_loss:.2f}, reported_max_risk={mr:.2f})."
                                ),
                                inputs={"estimated_max_loss_usd": est_loss, "reported_max_risk": mr},
                                outputs={"normalized_max_risk": proposal.max_risk, "normalized_target_return": proposal.target_return},
                            ))
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

    # Final gate: never allow expired contracts into pending_proposal
    if decision == AgentDecision.PROCEED and proposal is not None:
        today = __import__("datetime").date.today()
        expired: list[dict] = []
        for leg in proposal.legs:
            sym = str(getattr(leg, "symbol", "") or "").strip()
            exp_d = occ_expiry_as_date(sym)
            if exp_d is None:
                exp_d = parse_greeks_expiry_str(str(getattr(leg, "expiry", "") or ""))
            if exp_d is not None and exp_d < today:
                expired.append({"symbol": sym, "expired_on": exp_d.isoformat()})
        if expired:
            decision = AgentDecision.HOLD
            proposal = None
            confidence = 0.0
            reasoning = (
                "Rejected: proposal contains expired option legs. "
                "Regenerate using only future-dated OCC symbols from near_atm_contracts."
            )
            state.reasoning_log.append(ReasoningEntry(
                agent="Strategist",
                action="HOLD",
                reasoning=reasoning,
                inputs={"expired_legs": expired},
                outputs={"rejected": True},
            ))

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
