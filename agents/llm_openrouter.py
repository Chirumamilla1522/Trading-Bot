"""
OpenRouter (cloud) LLM client — ChatOpenAI pointed at https://openrouter.ai/api/v1.

Used only when OPENROUTER_ENABLED=true in agents/config.py. Default stack is local llama.cpp.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MAX_TOKENS = int(os.getenv("OPENROUTER_MAX_TOKENS", "2048"))


def _openrouter_extra_body() -> dict[str, Any] | None:
    """
    OpenRouter-specific JSON fields merged into chat/completions requests.

    Default: provider.data_collection=allow so free-tier upstreams are eligible when
    account privacy is strict (avoids 404 "No endpoints matching … data policy").
    Override with OPENROUTER_EXTRA_BODY='{"provider":{...}}' (full JSON object).
    See https://openrouter.ai/settings/privacy
    """
    raw = os.getenv("OPENROUTER_EXTRA_BODY", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("OPENROUTER_EXTRA_BODY is not valid JSON; using defaults")
    dc = os.getenv("OPENROUTER_DATA_COLLECTION", "allow").strip().lower()
    if dc not in ("allow", "deny"):
        dc = "allow"
    return {"provider": {"data_collection": dc}}


def openrouter_chat_llm(
    model: str,
    *,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    default_headers: dict[str, str] | None = None,
    **kwargs: Any,
):
    """
    Shared ChatOpenAI factory for OpenRouter-compatible API.
    """
    from langchain_openai import ChatOpenAI

    hdr = {"HTTP-Referer": "https://trading-terminal.local"}
    if default_headers:
        hdr.update(default_headers)
    extra = _openrouter_extra_body()
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens if max_tokens is not None else OPENROUTER_MAX_TOKENS,
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
        default_headers=hdr,
        extra_body=extra,
        **kwargs,
    )
