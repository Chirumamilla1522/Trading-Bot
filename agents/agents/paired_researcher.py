"""
Paired Researcher – Bull + Bear in one call (token saver)

This replaces running BullResearcher + BearResearcher as two separate LLM calls.
It produces two short arguments plus conviction scores in one STRICT JSON response,
and writes into the existing FirmState fields:
- bull_argument, bull_conviction
- bear_argument, bear_conviction
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from agents.config import MODELS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.state import FirmState, ReasoningEntry


SYSTEM = """ROLE: PairedResearcher (Bull+Bear in one pass)
You are producing BOTH sides in one response:
- BULL case: strongest argument FOR upside / bullish thesis
- BEAR case: strongest argument AGAINST / failure modes

STRICTNESS:
- Use ONLY the provided JSON context. Do NOT invent earnings dates, catalysts, or fundamentals not present.
- Keep each argument concise and grounded in numbers from the context.

Output ONLY valid JSON (no markdown, no prose) matching:
{
  "bull": {"argument": "3-4 sentences", "conviction": 1-10},
  "bear": {"argument": "3-4 sentences", "conviction": 1-10}
}

Both arguments MUST cite concrete fields (price_change_pct, market_regime, iv_regime/iv_atm, skew_ratio, sentiment score, market_bias_score, and at least one technical_context field if present)."""


def _parse_paired_json(raw: str) -> tuple[str, int, str, int] | None:
    s = (raw or "").strip()
    if "```" in s:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            s = m.group(0).strip()
    try:
        obj = json.loads(s)
    except Exception:
        # try to salvage first JSON object region
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    bull = obj.get("bull") or {}
    bear = obj.get("bear") or {}
    bull_arg = str((bull.get("argument") or "")).strip()
    bear_arg = str((bear.get("argument") or "")).strip()
    try:
        bull_conv = int(bull.get("conviction") or 0)
    except Exception:
        bull_conv = 0
    try:
        bear_conv = int(bear.get("conviction") or 0)
    except Exception:
        bear_conv = 0
    bull_conv = max(1, min(10, bull_conv or 5))
    bear_conv = max(1, min(10, bear_conv or 5))
    if not bull_arg or not bear_arg:
        return None
    return bull_arg, bull_conv, bear_arg, bear_conv


def paired_researcher_node(state: FirmState) -> FirmState:
    llm = chat_llm(
        MODELS.bull_researcher.active,
        agent_role="paired_researcher",
        temperature=0.25,
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

    ctx = {
        "ticker": state.ticker,
        "price_change_pct": round(state.price_change_pct * 100, 3),
        "market_regime": state.market_regime.value,
        "iv_regime": state.iv_regime,
        "iv_atm": state.iv_atm,
        "skew_ratio": state.iv_skew_ratio,
        "sentiment": _sent,
        "market_bias_score": state.market_bias_score,
        "news_timing_regime": state.news_timing_regime,
        "news_newest_age_minutes": _news_age,
        "tail_risks": state.sentiment_tail_risks,
        "themes": state.sentiment_themes,
        "movement_signal": state.movement_signal,
        "vol_ratio": state.vol_ratio,
        "technical_context": (state.technical_context.model_dump() if state.technical_context else None),
        "tier3_structured_digests": _dig[:900],
        "risk": {
            "drawdown_pct": float(state.risk.drawdown_pct or 0.0),
            "portfolio_delta": float(state.risk.portfolio_delta or 0.0),
            "portfolio_vega": float(state.risk.portfolio_vega or 0.0),
        },
    }

    resp = invoke_llm(llm, [
        SystemMessage(content=SYSTEM),
        HumanMessage(content=json.dumps(ctx, indent=2)),
    ])

    parsed = _parse_paired_json(getattr(resp, "content", "") or "")
    if not parsed:
        # Fall back to minimal outputs rather than breaking the pipeline.
        state.bull_argument = ""
        state.bear_argument = ""
        state.bull_conviction = 0
        state.bear_conviction = 0
        state.reasoning_log.append(ReasoningEntry(
            agent="PairedResearcher",
            action="HOLD",
            reasoning="Paired researcher output could not be parsed (expected strict JSON).",
            inputs={"ticker": state.ticker},
            outputs={"raw_head": (getattr(resp, "content", "") or "")[:200]},
        ))
        return state

    bull_arg, bull_conv, bear_arg, bear_conv = parsed
    state.bull_argument = bull_arg
    state.bull_conviction = bull_conv
    state.bear_argument = bear_arg
    state.bear_conviction = bear_conv

    # Log one entry (not two) to save UI noise + storage.
    state.reasoning_log.append(ReasoningEntry(
        agent="PairedResearcher",
        action="HOLD",
        reasoning=f"BULL({bull_conv}/10): {bull_arg[:500]}\n\nBEAR({bear_conv}/10): {bear_arg[:500]}",
        inputs={
            "regime": state.market_regime.value,
            "iv_regime": state.iv_regime,
            "news_timing_regime": state.news_timing_regime,
        },
        outputs={"bull_conviction": bull_conv, "bear_conviction": bear_conv},
    ))
    return state

