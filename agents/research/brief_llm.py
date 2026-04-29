"""
Single-call LLM to produce a TickerBrief from a signal snapshot + optional prior brief.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.research.schema import EpistemicMeta, SignalSnapshot, TickerBrief
from agents.research.stress import attach_default_stress

log = logging.getLogger(__name__)

PROMPT_VERSION = "universe_brief_v1"

SYSTEM = """ROLE: UniverseBrief (per-ticker cached brief)
You are a senior equity-derivatives strategist producing a fast cached brief for a SINGLE ticker.
You will be given a JSON payload containing:
- ticker
- snapshot (current signals: price/iv/news counts/impact/movement/etc.)
- prior_thesis and prior_stance

Your job is to write a brief that is explicitly grounded in the snapshot (THIS ticker, NOW),
and to state what changed vs the prior. Do NOT be generic.

STRICTNESS:
- Use ONLY the provided JSON snapshot + prior fields. Do NOT use outside knowledge, do NOT invent earnings dates.
- If key snapshot fields are missing/zero/unknown, explicitly say “not provided” and lower confidence.

GROUNDING REQUIREMENTS (must follow):
- In `thesis_short` or `agent_notes`, cite at least 5 concrete snapshot fields (numbers).
- `what_changed` must list the top 1-4 deltas vs prior (if prior exists), otherwise say “first pass”.
- `invalidation_triggers` must be concrete (numeric thresholds or events).

Output ONLY valid JSON matching the schema below. No markdown fences. Be concise.

Schema:
{
  "thesis_short": "string, max 2 sentences",
  "key_risks": ["string"],
  "what_changed": ["string"],
  "invalidation_triggers": ["string — concrete numeric or event conditions"],
  "stance": "LONG | SHORT | HOLD | NEUTRAL",
  "confidence": 0.0-1.0,
  "regime_note": "string",
  "next_watch": ["string"],
  "suggested_structure": "string or empty",
  "agent_notes": "string, max 4 sentences",
  "ttl_minutes": integer (15-240, how long this view is valid without new data)
}

Rules:
- If signals are thin, say so and lower confidence.
- Never invent earnings dates; use only provided snapshot.
- suggested_structure is educational only, not an order.
"""


def _parse_json_loose(raw: str) -> dict[str, Any]:
    """
    Parse a JSON object from an LLM response robustly.

    Common failure modes:
    - Markdown fences (```json ... ```)
    - Leading prose before the first '{'
    - Trailing tokens after a valid JSON object ("extra data")
    """
    s = (raw or "").strip()

    # Strip markdown fences by grabbing the first { ... }-looking region.
    if "```" in s:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            s = m.group(0).strip()

    # Find the first JSON object start.
    i = s.find("{")
    if i > 0:
        s = s[i:].lstrip()

    # Use raw_decode so we can ignore trailing junk after a valid object.
    dec = json.JSONDecoder()
    obj, _end = dec.raw_decode(s)
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")
    return obj


def run_brief_llm(
    ticker: str,
    snap: SignalSnapshot,
    prior: TickerBrief | None,
    *,
    signal_hash: str,
) -> TickerBrief:
    from agents.config import MODELS
    from agents.llm_providers import chat_llm
    from agents.llm_retry import invoke_llm

    llm = chat_llm(
        MODELS.sentiment_analyst.active,
        agent_role="universe_brief",
        temperature=0.15,
    )
    payload = {
        "ticker": ticker,
        "snapshot": snap.model_dump(mode="json"),
        "prior_thesis": prior.thesis_short if prior else "",
        "prior_stance": prior.stance if prior else "",
    }
    messages = [
        SystemMessage(content=SYSTEM),
        HumanMessage(content=json.dumps(payload, indent=1)),
    ]
    try:
        resp = invoke_llm(llm, messages)
        raw = (resp.content or "").strip()
        try:
            data = _parse_json_loose(raw)
        except Exception:
            # One-shot repair pass: coerce the model into STRICT JSON only.
            repair_sys = (
                "You are a strict JSON repair tool.\n"
                "Return ONLY valid JSON matching exactly this schema (no markdown, no prose):\n\n"
                "{\n"
                '  "thesis_short": "string",\n'
                '  "key_risks": ["string"],\n'
                '  "what_changed": ["string"],\n'
                '  "invalidation_triggers": ["string"],\n'
                '  "stance": "LONG|SHORT|HOLD|NEUTRAL",\n'
                '  "confidence": 0.0,\n'
                '  "regime_note": "string",\n'
                '  "next_watch": ["string"],\n'
                '  "suggested_structure": "string or empty",\n'
                '  "agent_notes": "string",\n'
                '  "ttl_minutes": 60\n'
                "}\n\n"
                "If you cannot comply due to missing info, output HOLD/NEUTRAL with low confidence and "
                "explicitly say what is missing in agent_notes."
            )
            repair_msgs = [
                SystemMessage(content=repair_sys),
                HumanMessage(content=(raw or "")[:2600]),
            ]
            llm_repair = chat_llm(
                MODELS.sentiment_analyst.active,
                agent_role="universe_brief",
                temperature=0.0,
            )
            resp2 = invoke_llm(llm_repair, repair_msgs)
            raw2 = (resp2.content or "").strip()
            data = _parse_json_loose(raw2)
    except Exception as exc:
        log.warning("brief_llm failed for %s: %s", ticker, exc)
        ttl = 45
        now = datetime.now(timezone.utc)
        return TickerBrief(
            ticker=ticker.upper(),
            thesis_short=f"LLM brief unavailable ({type(exc).__name__}). Signals only.",
            key_risks=["Model error — refresh later"],
            what_changed=[],
            invalidation_triggers=["Fresh signal hash differs from stored"],
            stance="HOLD",
            confidence=0.2,
            regime_note=f"iv_30d={snap.iv_30d:.3f}, news_24h={snap.news_count_24h}",
            signal_hash=signal_hash,
            model_id="error",
            prompt_version=PROMPT_VERSION,
            updated_at=now,
            epistemic=EpistemicMeta(
                valid_until=now + timedelta(minutes=ttl),
                ttl_minutes=ttl,
                stale_reason="llm_failure",
            ),
        )

    ttl = int(max(15, min(240, int(data.get("ttl_minutes", 60)))))
    now = datetime.now(timezone.utc)
    brief = TickerBrief(
        ticker=ticker.upper(),
        thesis_short=str(data.get("thesis_short", ""))[:800],
        key_risks=list(data.get("key_risks") or [])[:12],
        what_changed=list(data.get("what_changed") or [])[:12],
        invalidation_triggers=list(data.get("invalidation_triggers") or [])[:12],
        stance=str(data.get("stance", "HOLD")).upper()[:16],
        confidence=float(data.get("confidence", 0.5)),
        regime_note=str(data.get("regime_note", ""))[:500],
        next_watch=list(data.get("next_watch") or [])[:12],
        suggested_structure=str(data.get("suggested_structure", ""))[:300],
        agent_notes=str(data.get("agent_notes", ""))[:1200],
        signal_hash=signal_hash,
        model_id=getattr(MODELS.sentiment_analyst, "active", "unknown"),
        prompt_version=PROMPT_VERSION,
        updated_at=now,
        epistemic=EpistemicMeta(
            valid_until=now + timedelta(minutes=ttl),
            ttl_minutes=ttl,
            stale_reason="",
        ),
    )
    brief = attach_default_stress(brief)
    return brief
