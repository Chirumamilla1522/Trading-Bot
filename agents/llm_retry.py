"""
Retry wrapper for LangChain ChatOpenAI — local llama.cpp and/or OpenRouter.

Default: local-only (OPENROUTER_ENABLED=false). No cloud calls.

When OPENROUTER_ENABLED=true: local first if LLAMA_LOCAL_PRIMARY=true, else OpenRouter first,
with 429 backoff on cloud; optional local fallback.

Health-check cache: after a connectivity failure on local, skip local for LLAMA_LOCAL_COOLDOWN_S
seconds (only relevant when falling back to cloud or retrying).
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = int(os.getenv("OPENROUTER_429_MAX_ATTEMPTS", "8"))
_BASE_DELAY_S = float(os.getenv("OPENROUTER_429_BASE_DELAY_S", "3.0"))
_MAX_DELAY_S = float(os.getenv("OPENROUTER_429_MAX_DELAY_S", "90.0"))

# ── Local llama.cpp health-check state ────────────────────────────────────────
# After a connectivity failure (server not running), skip local for this many
# seconds before retrying, to avoid wasting time on every agent call.
_LOCAL_COOLDOWN_S  = float(os.getenv("LLAMA_LOCAL_COOLDOWN_S", "300"))
_local_failed_until: float = 0.0   # epoch-seconds; 0 = no cooldown active
_local_state_lock  = threading.Lock()

# Tracks which backend was last used (for status reporting)
_last_backend: str = "unknown"   # "local" | "openrouter" | "unknown"


def _normalize_messages_for_strict_local(messages: list) -> list:
    """
    Some OpenAI-compatible local servers (notably certain MLX shims) enforce:
      - roles must strictly alternate: user/assistant/user/assistant/...
      - `system` role may be rejected entirely

    We normalize by:
      - folding any SystemMessage content into the first user message
      - merging consecutive HumanMessages into one
      - leaving assistant messages intact
    """
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except Exception:
        return messages

    sys_chunks: list[str] = []
    out: list = []

    # Collect system messages and keep the rest in order
    for m in messages:
        if isinstance(m, SystemMessage):
            if m.content:
                sys_chunks.append(str(m.content))
            continue
        out.append(m)

    if sys_chunks:
        sys_blob = "\n\n".join(sys_chunks).strip()
        if out and isinstance(out[0], HumanMessage):
            out[0] = HumanMessage(content=f"{sys_blob}\n\n{out[0].content}")
        else:
            out.insert(0, HumanMessage(content=sys_blob))

    # Merge consecutive user messages
    merged: list = []
    for m in out:
        if merged and isinstance(m, HumanMessage) and isinstance(merged[-1], HumanMessage):
            merged[-1] = HumanMessage(content=f"{merged[-1].content}\n\n{m.content}")
            continue
        merged.append(m)

    # If it still starts with assistant, prepend a user shim (rare, but keeps alternation sane)
    if merged and isinstance(merged[0], AIMessage):
        merged.insert(0, HumanMessage(content="(context)"))

    return merged


def get_llm_backend_status() -> dict:
    """Return current backend mode and health for the /llm/status endpoint."""
    from agents.config import OPENROUTER_ENABLED
    from agents.llm_local import (
        iter_unique_local_base_urls,
        llama_local_primary_enabled,
        local_llama_fallback_enabled,
        resolve_local_base_url,
    )

    if not OPENROUTER_ENABLED:
        primary = "local"
    else:
        primary = "local" if llama_local_primary_enabled() else "openrouter"
    with _local_state_lock:
        cooldown_remaining = max(0.0, _local_failed_until - time.time())
        local_healthy = cooldown_remaining == 0.0
    return {
        "openrouter_enabled": OPENROUTER_ENABLED,
        "primary":           primary,
        "local_healthy":     local_healthy,
        "cooldown_remaining_s": round(cooldown_remaining),
        "last_backend_used": _last_backend,
        "local_base_url":    resolve_local_base_url(None),
        "local_base_urls":   iter_unique_local_base_urls(),
    }


def _mark_local_failed() -> None:
    global _local_failed_until
    with _local_state_lock:
        _local_failed_until = time.time() + _LOCAL_COOLDOWN_S
    log.warning(
        "llama.cpp: marked as unavailable for %.0fs. Will retry after cooldown.",
        _LOCAL_COOLDOWN_S,
    )


def _local_in_cooldown() -> bool:
    with _local_state_lock:
        return time.time() < _local_failed_until


def _reset_local_cooldown() -> None:
    global _local_failed_until
    with _local_state_lock:
        _local_failed_until = 0.0


def _is_rate_limit(err: BaseException) -> bool:
    if type(err).__name__ == "RateLimitError":
        return True
    code = getattr(err, "status_code", None)
    if code == 429:
        return True
    resp = getattr(err, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    try:
        import openai

        if isinstance(err, openai.RateLimitError):
            return True
    except Exception:
        pass
    s = str(err).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


def _log_openrouter_not_found(e: BaseException) -> None:
    """HTTP 404 from OpenRouter — model slug, privacy guardrails, or data policy."""
    try:
        import openai

        if isinstance(e, openai.NotFoundError):
            msg = str(e).lower()
            if "guardrail" in msg or "data policy" in msg:
                log.error(
                    "OpenRouter 404: no endpoint matches your privacy/guardrails "
                    "(often fixable at https://openrouter.ai/settings/privacy — relax "
                    "model allowlist / ZDR, or ensure OPENROUTER_DATA_COLLECTION=allow). "
                    "Details: %s body=%s",
                    str(e),
                    getattr(e, "body", None),
                )
            else:
                log.error(
                    "OpenRouter 404 (model or route). Check MODELS.* in agents/config.py "
                    "and OpenRouter model list. message=%s body=%s",
                    str(e),
                    getattr(e, "body", None),
                )
    except Exception:
        pass


def _should_try_local_fallback(err: BaseException) -> bool:
    """Whether to try llama.cpp after OpenRouter failed (OpenRouter-first mode only)."""
    from agents.config import OPENROUTER_ENABLED
    from agents.llm_local import local_llama_fallback_enabled, llama_local_primary_enabled

    if not OPENROUTER_ENABLED:
        return False
    if llama_local_primary_enabled():
        return False
    if not local_llama_fallback_enabled():
        return False
    try:
        import openai

        # Bad key / bad request — local won't help
        if isinstance(err, openai.BadRequestError):
            return False
        if isinstance(err, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return False
    except Exception:
        pass
    code = getattr(err, "status_code", None)
    if code in (400, 401, 403):
        return False
    return True


def _is_connectivity_error(err: BaseException) -> bool:
    """True for errors that mean the local server simply isn't running."""
    s = str(err).lower()
    keywords = ("connection refused", "connrefused", "connect call failed",
                 "connection error", "no route to host", "connection reset",
                 "cannot connect", "failed to connect", "nodename nor servname")
    return any(k in s for k in keywords)


def invoke_llm(llm: Any, messages: list, **kwargs: Any):
    """
    OPENROUTER_ENABLED=false: invoke the local client only (no OpenRouter).

    OPENROUTER_ENABLED=true: local first if LLAMA_LOCAL_PRIMARY, else cloud first,
    with rate-limit retries on cloud and optional local fallback.
    """
    global _last_backend
    from agents.config import OPENROUTER_ENABLED
    from agents.llm_local import llama_local_chat_llm, llama_local_primary_enabled

    if not OPENROUTER_ENABLED:
        from agents.llm_local import server_pool

        pool_url = getattr(llm, "_pool_base_url", None)
        if server_pool.healthy_count == 0:
            # All servers down — try one last probe before giving up
            healthy = server_pool.probe_all()
            if not healthy:
                raise RuntimeError(
                    "All local LLM servers are unreachable and OpenRouter is disabled. "
                    "Start a local LLM server or set OPENROUTER_ENABLED=true."
                )
        try:
            msgs = _normalize_messages_for_strict_local(messages)
            result = llm.invoke(msgs, **kwargs)
            _last_backend = "local"
            _reset_local_cooldown()
            if pool_url:
                server_pool.release(pool_url, success=True)
            return result
        except Exception as e:
            if pool_url:
                is_conn = _is_connectivity_error(e)
                server_pool.release(pool_url, success=not is_conn)
            if _is_connectivity_error(e):
                _mark_local_failed()
            raise

    if llama_local_primary_enabled():
        from agents.llm_local import resolve_local_base_url

        role = getattr(llm, "_trading_agent_role", None)
        base = resolve_local_base_url(role if isinstance(role, str) else None)
        if _local_in_cooldown():
            log.debug("invoke_llm: local llama.cpp in cooldown, using OpenRouter directly")
        else:
            try:
                local = llama_local_chat_llm(llm)
                log.debug("invoke_llm: trying local llama.cpp at %s", base)
                msgs = _normalize_messages_for_strict_local(messages)
                result = local.invoke(msgs, **kwargs)
                _reset_local_cooldown()   # success → clear any prior cooldown
                _last_backend = "local"
                return result
            except Exception as e:
                if _is_connectivity_error(e):
                    log.warning(
                        "LOCAL llama.cpp at %s not reachable (%s: %s). "
                        "Falling back to OpenRouter for %.0fs.",
                        base, type(e).__name__, str(e)[:120], _LOCAL_COOLDOWN_S,
                    )
                    _mark_local_failed()
                else:
                    log.warning(
                        "LOCAL llama.cpp error (%s: %s); falling back to OpenRouter",
                        type(e).__name__, str(e)[:200],
                    )

    last: BaseException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            result = llm.invoke(messages, **kwargs)
            _last_backend = "openrouter"
            return result
        except Exception as e:
            last = e
            _log_openrouter_not_found(e)
            if _is_rate_limit(e) and attempt < _MAX_ATTEMPTS - 1:
                delay = min(_MAX_DELAY_S, _BASE_DELAY_S * (2**attempt))
                delay += random.uniform(0, min(2.0, delay * 0.15))
                log.warning(
                    "OpenRouter rate limit (attempt %d/%d): %s — sleeping %.1fs",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    type(e).__name__,
                    delay,
                )
                time.sleep(delay)
                continue
            break

    if last is not None and _should_try_local_fallback(last):
        if not _local_in_cooldown():
            try:
                from agents.llm_local import resolve_local_base_url

                local = llama_local_chat_llm(llm)
                role = getattr(llm, "_trading_agent_role", None)
                base = resolve_local_base_url(role if isinstance(role, str) else None)
                log.warning(
                    "OpenRouter failed (%s); using local llama.cpp at %s",
                    type(last).__name__, base,
                )
                msgs = _normalize_messages_for_strict_local(messages)
                result = local.invoke(msgs, **kwargs)
                _last_backend = "local"
                return result
            except Exception as e2:
                if _is_connectivity_error(e2):
                    _mark_local_failed()
                log.error("Local llama.cpp fallback also failed: %s", e2)
                raise last from e2

    if last is not None:
        raise last
    raise RuntimeError("invoke_llm: no result and no exception")
