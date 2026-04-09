"""
LLM routing: local llama.cpp (default) or OpenRouter when OPENROUTER_ENABLED=true.

Agents should use ``chat_llm(MODELS.<agent>.active, ...)`` — never import OpenRouter directly.
"""
from __future__ import annotations

from typing import Any

from agents.config import OPENROUTER_ENABLED


def chat_llm(model: str, **kwargs: Any):
    """
    Return a LangChain ChatOpenAI client.

    - OPENROUTER_ENABLED=false (default): local llama.cpp only (ignores ``model`` for API id;
      the running server loads one GGUF — set LLAMA_LOCAL_MODEL if the server requires it).
    - OPENROUTER_ENABLED=true: OpenRouter cloud using ``model`` as the route slug.

    Optional ``agent_role`` (e.g. ``\"options_specialist\"``) selects per-agent local URLs
    when ``OPENROUTER_ENABLED=false`` (see ``agents/llm_local.resolve_local_base_url``).
    """
    agent_role = kwargs.pop("agent_role", None)

    if OPENROUTER_ENABLED:
        from agents.llm_openrouter import openrouter_chat_llm

        return openrouter_chat_llm(model, **kwargs)
    from agents.llm_local import local_chat_llm

    return local_chat_llm(agent_role=agent_role, **kwargs)
