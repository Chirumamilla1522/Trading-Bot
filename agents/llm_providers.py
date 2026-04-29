"""
LLM routing: local llama.cpp primary with optional OpenRouter fallback.

Agents should use ``chat_llm(MODELS.<agent>.active, ...)`` — never import OpenRouter directly.
"""
from __future__ import annotations

from typing import Any

from agents.config import OPENROUTER_ENABLED


def chat_llm(model: str, **kwargs: Any):
    """
    Return a LangChain ChatOpenAI client.

    - Local is always constructed as the primary client.
    - If OPENROUTER_ENABLED=true, the retry layer can fall back to OpenRouter using ``model``
      as the route slug (see ``agents/llm_retry.py``).

    Optional ``agent_role`` (e.g. ``\"options_specialist\"``) selects per-agent local URLs
    when ``OPENROUTER_ENABLED=false`` (see ``agents/llm_local.resolve_local_base_url``).
    """
    agent_role = kwargs.pop("agent_role", None)

    # Always build the local client first. If OPENROUTER_ENABLED is true, the invoke_llm()
    # wrapper can fall back to OpenRouter using the provided `model` slug.
    from agents.llm_local import local_chat_llm

    llm = local_chat_llm(agent_role=agent_role, **kwargs)
    try:
        setattr(llm, "_trading_agent_role", agent_role)
        # Stash the OpenRouter route so invoke_llm can fall back without rebuilding context.
        if OPENROUTER_ENABLED and isinstance(model, str) and model:
            setattr(llm, "_openrouter_model", model)
    except Exception:
        pass
    return llm
