"""
Reflex LLM: ultra-fast, low-token "pulse check" used as a gate before expensive reasoning.

Backend options:
- local:     your local OpenAI-compatible server (llama.cpp / vLLM / etc.)
- openrouter: OpenRouter using REFLEX_OPENROUTER_MODEL (e.g. deepseek-v4-flash)

This module is intentionally small and safe: it never places orders. It only returns a boolean
and a short text snippet for logging/UX.
"""

from __future__ import annotations

from typing import Any

from agents.config import (
    OPENROUTER_ENABLED,
    REFLEX_BACKEND,
    REFLEX_MAX_TOKENS,
    REFLEX_OPENROUTER_MODEL,
)


def _reflex_chat_llm(*, temperature: float = 0.0):
    """
    Returns a LangChain ChatOpenAI client configured for the reflex backend.
    """
    if REFLEX_BACKEND == "openrouter":
        from agents.llm_openrouter import openrouter_chat_llm

        llm = openrouter_chat_llm(
            REFLEX_OPENROUTER_MODEL,
            temperature=temperature,
        )
        try:
            setattr(llm, "_trading_agent_role", "reflex")
        except Exception:
            pass
        return llm

    # local backend (default): reuse normal routing but force local by requiring OPENROUTER_ENABLED=false.
    # If the project is currently in OpenRouter mode globally, still allow local reflex by using local_chat_llm directly.
    if OPENROUTER_ENABLED:
        from agents.llm_local import local_chat_llm

        llm = local_chat_llm(agent_role="reflex", temperature=temperature, max_tokens=REFLEX_MAX_TOKENS)
        try:
            setattr(llm, "_trading_agent_role", "reflex")
        except Exception:
            pass
        return llm

    from agents.llm_providers import chat_llm

    return chat_llm("reflex", agent_role="reflex", temperature=temperature)


def reflex_yes_no(
    *,
    prompt: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Runs a tiny YES/NO classification. Returns:
      {"ok": bool, "answer": "YES|NO|UNKNOWN", "raw": "..."}
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    sys = (
        "You are a fast reflex validator.\n"
        "Return ONLY one token: YES or NO.\n"
        "If you are unsure, return NO.\n"
    )
    blob = prompt.strip()
    if context:
        import json

        blob = f"{blob}\n\nCONTEXT JSON:\n{json.dumps(context, ensure_ascii=False)[:6000]}"

    llm = _reflex_chat_llm(temperature=0.0)
    # Use the shared retry wrapper so OpenRouter-first reflex can fall back to local
    # when LLAMA_LOCAL_FALLBACK=true (default).
    from agents.llm_retry import invoke_llm

    resp = invoke_llm(llm, [SystemMessage(content=sys), HumanMessage(content=blob)])
    raw = str(getattr(resp, "content", "") or "").strip().upper()
    ans = "YES" if raw.startswith("YES") else "NO" if raw.startswith("NO") else "UNKNOWN"
    return {"ok": ans == "YES", "answer": ans, "raw": raw[:120]}

