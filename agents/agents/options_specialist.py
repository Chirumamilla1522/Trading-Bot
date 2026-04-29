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
from agents.config import (
    ENABLE_LONG_OPTION_CANDIDATES_TABLE,
    LONG_OPTION_CANDIDATES_LIMIT,
    MODELS,
)
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm, invoke_llm_with_metrics
from agents.features import build_chain_analytics, compute_iv_metrics
from agents.schemas import OptionsSpecialistOutput, parse_and_validate

SYSTEM_PROMPT = """ROLE: OptionsSpecialist (naked short options only)
You are the options surface specialist. Your job is to decide if the surface supports a conservative SINGLE-leg naked short (call or put).

You will be given a JSON context containing:
- ticker, underlying_price, market_regime
- technical_context (if present): regime_label/volume_state + key levels + outside-week/triangle flags
- chain_analytics (precomputed): atm_iv, skew_ratio, iv_regime, term_structure, near_atm_contracts
- portfolio_delta/vega/theta, open_positions count

Rules:
- You MUST use ONLY the provided JSON context. Do NOT invent events, earnings, or liquidity.
- Output must be VALID JSON only (no markdown).
- If key fields are missing/unknown (underlying_price <= 0, iv_regime missing, near_atm_contracts empty), output HOLD with low confidence.
- Safety: if technical_context implies runaway-move risk (trend+confirming volume, confirmed outside-week, triangle+confirming volume), prefer HOLD unless clearly level-based mean reversion.
- If you output PROCEED, `opportunity` MUST be exactly "short put (naked)" or "short call (naked)" (or null when HOLD/ABORT).

Output STRICT JSON (exact keys):
{
  "decision":"PROCEED" | "HOLD" | "ABORT",
  "iv_regime":"LOW" | "NORMAL" | "ELEVATED" | "EXTREME",
  "skew_signal":"PUT_PREMIUM" | "CALL_PREMIUM" | "NEUTRAL",
  "term_signal":"CONTANGO" | "BACKWARDATION" | "FLAT",
  "opportunity":"short put (naked)" | "short call (naked)" | null,
  "preferred_dte_bucket":"string",
  "insufficient_data":true | false,
  "bias":"bullish" | "bearish" | "neutral",
  "setup_type":"range_fade" | "breakout_continuation" | "breakout_rejection" | "trend_pullback",
  "key_levels":[{"kind":"support|resistance","price":0.0,"source":"...","distance_pct":0.0,"confidence":0.0}],
  "confirmation":"string",
  "invalidation":"string",
  "risk_notes":["string"],
  "confidence":0.0-1.0,
  "reasoning":"<2-3 sentences citing concrete fields from context>"
}"""


def options_specialist_node(state: FirmState) -> FirmState:
    _t0 = __import__("time").time()
    llm = chat_llm(
        MODELS.options_specialist.active,
        agent_role="options_specialist",
        temperature=0.05,
    )

    # Build rich pre-computed analytics — no arithmetic for the LLM.
    #
    # Why this exists:
    # - We want the model to *interpret* a consistent options surface snapshot, not do math.
    # - Deterministic metrics avoid prompt-time calculation drift between agents.
    # - Most of our JSON parsing incidents came from the model emitting arithmetic (or partial JSON)
    #   while "calculating"; pushing computation into Python keeps outputs stable.
    analytics = build_chain_analytics(state.latest_greeks, state.underlying_price)
    iv_metrics = compute_iv_metrics(state.latest_greeks, state.underlying_price)
    long_candidate_metrics = None
    if ENABLE_LONG_OPTION_CANDIDATES_TABLE:
        try:
            from datetime import date as _date
            from agents.options_math import expected_value_long_option, pop_long_option
            from agents.utils import occ_expiry_as_date, parse_greeks_expiry_str

            today = _date.today()
            u_px = float(state.underlying_price or 0.0)
            long_candidate_metrics = []
            for g in list(state.latest_greeks or []):
                try:
                    if g.delta is None or g.iv is None:
                        continue
                    exp_d = occ_expiry_as_date(str(g.symbol or ""))
                    if exp_d is None:
                        exp_d = parse_greeks_expiry_str(str(getattr(g, "expiry", "") or ""))
                    if exp_d is None:
                        continue
                    dte = (exp_d - today).days
                    if dte < 7 or dte > 14:
                        continue
                    if not (g.bid > 0 and g.ask > 0 and g.ask >= g.bid):
                        continue
                    mid = (float(g.bid) + float(g.ask)) / 2.0
                    if mid <= 0:
                        continue
                    pop = pop_long_option(
                        right=str(getattr(g, "right", "") or ""),
                        s0=u_px,
                        strike=float(g.strike),
                        premium=float(mid),
                        iv=float(g.iv),
                        dte=int(dte),
                        mu=0.0,
                    )
                    ev = expected_value_long_option(
                        right=str(getattr(g, "right", "") or ""),
                        s0=u_px,
                        strike=float(g.strike),
                        premium=float(mid),
                        iv=float(g.iv),
                        dte=int(dte),
                        mu=0.0,
                    )
                    long_candidate_metrics.append({
                        "symbol": str(g.symbol),
                        "right": str(getattr(g, "right", "") or ""),
                        "strike": float(g.strike),
                        "expiry": str(getattr(g, "expiry", "") or ""),
                        "dte": int(dte),
                        "delta": float(g.delta),
                        "iv": float(g.iv),
                        "mid": round(float(mid), 4),
                        "pop": (round(float(pop), 4) if pop is not None else None),
                        "ev_usd": (round(float(ev), 2) if ev is not None else None),
                        "delta_dist": round(abs(abs(float(g.delta)) - 0.60), 4),
                    })
                except Exception:
                    continue
            long_candidate_metrics.sort(
                key=lambda d: (
                    d.get("delta_dist", 9e9),
                    -(d.get("pop") or -1.0),
                    -float(d.get("ev_usd") or -1e18),
                )
            )
            lim = max(0, int(LONG_OPTION_CANDIDATES_LIMIT or 0))
            long_candidate_metrics = long_candidate_metrics[:lim] if lim else []
        except Exception:
            long_candidate_metrics = None

    # IMPORTANT: keep the LLM payload small to avoid provider-side truncation.
    # We only include the minimal subset of chain analytics needed for reasoning.
    try:
        near_atm = list((analytics or {}).get("near_atm_contracts") or [])
    except Exception:
        near_atm = []
    try:
        high_iv = list((analytics or {}).get("highest_iv_contracts") or [])
    except Exception:
        high_iv = []
    chain_analytics_slim = {
        "iv_metrics": {
            "atm_iv": float(iv_metrics.atm_iv or 0.0),
            "skew_ratio": float(iv_metrics.skew_ratio or 0.0),
            "iv_regime": str(iv_metrics.iv_regime or ""),
            "term_structure": str(getattr(iv_metrics, "term_structure", "") or iv_metrics.term_structure),
        },
        "term_structure": str(iv_metrics.term_structure or ""),
        "near_atm_contracts": near_atm[:25],
        "highest_iv_contracts": high_iv[:15],
    }

    context = {
        "ticker":            state.ticker,
        "underlying_price":  state.underlying_price,
        "market_regime":     state.market_regime.value,
        "technical_context": (state.technical_context.model_dump() if state.technical_context else None),
        "aplus_setup":       (state.aplus_setup.model_dump() if getattr(state, "aplus_setup", None) else None),
        "portfolio_delta":   state.risk.portfolio_delta,
        "portfolio_vega":    state.risk.portfolio_vega,
        "portfolio_theta":   state.risk.portfolio_theta,
        "open_positions":    len(state.open_positions),
        "chain_analytics":   chain_analytics_slim,
        "long_call_put_candidates_7_14d": long_candidate_metrics,
        "desk": {
            "sentiment_monitor_score":    round(state.sentiment_monitor_score, 4),
            "sentiment_monitor_source":   state.sentiment_monitor_source,
            "news_timing_regime":        state.news_timing_regime,
            "market_bias_score":         round(state.market_bias_score, 4),
        },
        # Structured digests can be large; keep a small slice.
        "tier3_structured_digests": state.tier3_structured_digests[:3],
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        # Compact JSON saves tokens materially vs pretty indent.
        HumanMessage(content=json.dumps(context, separators=(",", ":"), ensure_ascii=False)),
    ]

    response, llm_meta = invoke_llm_with_metrics(llm, messages)
    # Some providers occasionally return an empty content string with 200 OK.
    # Retry once before attempting repair/parse (bounded).
    if not (response and str(getattr(response, "content", "") or "").strip()):
        try:
            response, llm_meta_retry = invoke_llm_with_metrics(llm, messages)
            llm_meta = {"first": llm_meta, "retry": llm_meta_retry}
        except Exception:
            pass
    out = parse_and_validate(response.content, OptionsSpecialistOutput, "OptionsSpecialist")

    # Bounded retry for truncated/unparseable JSON (e.g. unbalanced braces).
    if not out:
        try:
            response2, llm_meta_retry2 = invoke_llm_with_metrics(llm, messages)
            if response2 and str(getattr(response2, "content", "") or "").strip():
                llm_meta = {"first": llm_meta, "retry": llm_meta_retry2}
                response = response2
                out = parse_and_validate(response.content, OptionsSpecialistOutput, "OptionsSpecialist")
        except Exception:
            pass

    if not out:
        # One-shot repair pass (bounded).
        #
        # Why: models occasionally output unbalanced braces, single quotes, or trailing commas.
        # We do exactly one repair attempt to keep latency bounded and avoid infinite loops.
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
            '  "insufficient_data": false,\n'
            '  "bias": "bullish|bearish|neutral",\n'
            '  "setup_type": "range_fade|breakout_continuation|breakout_rejection|trend_pullback",\n'
            '  "key_levels": [{"kind":"support|resistance","price":0.0,"source":"...","distance_pct":0.0,"confidence":0.0}],\n'
            '  "confirmation": "string",\n'
            '  "invalidation": "string",\n'
            '  "risk_notes": ["..."],\n'
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
        )
        resp2, llm_meta_repair = invoke_llm_with_metrics(llm_repair, repair_msgs)
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
            "llm_call": llm_meta,
            "llm_repair_call": (llm_meta_repair if "llm_meta_repair" in locals() else None),
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
                "has_technical_context": bool(state.technical_context is not None),
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
