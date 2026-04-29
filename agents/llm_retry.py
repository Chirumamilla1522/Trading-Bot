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
        local_llama_fallback_enabled,
        resolve_local_base_url,
    )

    if not OPENROUTER_ENABLED:
        primary = "local"
    else:
        # This repo's policy: local is primary whenever OpenRouter is enabled,
        # OpenRouter is used only as a fallback.
        primary = "local"
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


def _resolve_model_name(llm: Any) -> str:
    """Best-effort: pull a readable model identifier off the LangChain client."""
    for attr in ("model_name", "model", "deployment_name"):
        v = getattr(llm, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


def _mlflow_log_llm_call(
    llm: Any,
    messages: list,
    response: Any,
    *,
    duration_s: float,
    backend: str,
    error: BaseException | None = None,
) -> None:
    """Thin wrapper around mlflow_tracing.log_llm_call so invoke_llm stays readable."""
    try:
        from agents.tracking.mlflow_tracing import log_llm_call

        role = getattr(llm, "_trading_agent_role", None)
        extra: dict[str, str] = {}
        pool = getattr(llm, "_pool_base_url", None)
        if isinstance(pool, str) and pool:
            extra["local_base_url"] = pool
        log_llm_call(
            agent_role=role if isinstance(role, str) else None,
            model=_resolve_model_name(llm),
            backend=backend,
            messages=messages,
            response=response,
            duration_s=duration_s,
            error=error,
            extra_tags=extra or None,
        )
    except Exception:
        # MLflow should never break the real LLM call
        log.debug("MLflow log_llm_call wrapper failed", exc_info=True)


def _extract_token_usage(response: Any) -> dict[str, int]:
    """
    Best-effort extraction of token usage from LangChain response objects.
    Mirrors the logic used in mlflow_tracing so we can persist usage even when MLflow is disabled.
    """
    usage: dict[str, int] = {}
    try:
        meta = getattr(response, "response_metadata", None) or {}
        u = (meta or {}).get("token_usage") or (meta or {}).get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = u.get(k) if isinstance(u, dict) else None
            if isinstance(v, (int, float)):
                usage[k] = int(v)
    except Exception:
        return {}
    try:
        um = getattr(response, "usage_metadata", None) or {}
        alias = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens", "total_tokens": "total_tokens"}
        for k, std in alias.items():
            v = um.get(k) if isinstance(um, dict) else None
            if isinstance(v, (int, float)) and std not in usage:
                usage[std] = int(v)
    except Exception:
        pass
    return usage


def _rough_token_estimate(text: str) -> int:
    """
    Fallback token estimator when providers don't report usage.
    - Prefer tiktoken when available (close enough for most chat-style prompts).
    - Otherwise use a simple chars/4 heuristic.
    """
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore

        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            enc = tiktoken.encoding_for_model("gpt-4o-mini")
        return int(len(enc.encode(text)))
    except Exception:
        return max(1, len(text) // 4)


def _messages_to_plaintext(messages: list) -> str:
    """
    Deterministic prompt text used for token estimation.
    This does NOT need to match provider tokenization exactly; it's for sane UX metrics.
    """
    parts: list[str] = []
    for m in messages or []:
        role = getattr(m, "type", None) or getattr(m, "role", None) or m.__class__.__name__
        content = getattr(m, "content", None)
        if isinstance(content, list):
            # LangChain sometimes stores rich content parts; collapse to text-ish.
            chunk = []
            for p in content:
                if isinstance(p, dict):
                    chunk.append(str(p.get("text") or p.get("content") or ""))
                else:
                    chunk.append(str(p))
            content = "\n".join(x for x in chunk if x.strip())
        if not isinstance(content, str):
            content = str(content or "")
        parts.append(f"[{role}]\n{content}".strip())
    return "\n\n".join(p for p in parts if p)


def _sanitize_usage_with_estimates(*, usage: dict[str, int], messages: list, response: Any, backend: str) -> dict[str, int]:
    """
    If the provider doesn't report usage (common on local), or reports obviously-wrong
    values (e.g. completion_tokens == max_tokens), replace with estimates.
    """
    from agents.llm_local import LLAMA_LOCAL_MAX_TOKENS

    out = dict(usage or {})
    try:
        completion_text = _best_effort_response_text(response)
    except Exception:
        completion_text = ""
    prompt_text = _messages_to_plaintext(messages)

    pt = out.get("prompt_tokens")
    ct = out.get("completion_tokens")

    # Estimate prompt tokens if missing or zero.
    if not isinstance(pt, int) or pt <= 0:
        est_pt = _rough_token_estimate(prompt_text)
        if est_pt:
            out["prompt_tokens"] = int(est_pt)

    # Estimate completion tokens if missing or suspiciously equals the cap.
    suspicious_ct = False
    if isinstance(ct, int):
        if backend == "local" and ct >= int(LLAMA_LOCAL_MAX_TOKENS * 0.95):
            suspicious_ct = True
        # Some OpenAI-compatible adapters report the completion budget/cap as
        # completion_tokens. If the visible text is short, prefer our estimate.
        if completion_text and len(completion_text) < 5000 and ct > 2000:
            suspicious_ct = True
    if (not isinstance(ct, int) or ct <= 0 or suspicious_ct) and completion_text:
        est_ct = _rough_token_estimate(completion_text)
        if est_ct:
            out["completion_tokens"] = int(est_ct)

    # Total tokens
    pt2 = out.get("prompt_tokens")
    ct2 = out.get("completion_tokens")
    if isinstance(pt2, int) and isinstance(ct2, int):
        out["total_tokens"] = int(pt2 + ct2)
    return out


def _extract_model_name_from_response(response: Any) -> str | None:
    """Best-effort model id from response metadata (provider-specific)."""
    try:
        meta = getattr(response, "response_metadata", None) or {}
        if isinstance(meta, dict):
            for k in ("model", "model_name", "model_id"):
                v = meta.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        pass
    return None


def _best_effort_response_text(response: Any) -> str:
    """
    Provider-agnostic extraction of assistant text.

    Some providers/LangChain adapters can return empty `response.content` while still
    reporting non-zero completion tokens (text may live in `additional_kwargs` or
    nested metadata). We try a few common locations and return the first non-empty string.
    """
    if response is None:
        return ""
    # 1) Standard LangChain: AIMessage.content
    try:
        c = getattr(response, "content", None)
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            parts: list[str] = []
            for p in c:
                if isinstance(p, dict):
                    parts.append(str(p.get("text") or p.get("content") or ""))
                else:
                    parts.append(str(p))
            out = "\n".join(x for x in parts if x.strip())
            if out.strip():
                return out
    except Exception:
        pass

    # 2) Common adapter fields: additional_kwargs / tool_calls / reasoning
    try:
        ak = getattr(response, "additional_kwargs", None) or {}
        if isinstance(ak, dict):
            for k in ("text", "output_text", "reasoning", "thinking", "answer"):
                v = ak.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            # Some adapters stash a nested message dict
            msg = ak.get("message")
            if isinstance(msg, dict):
                v = msg.get("content") or msg.get("text")
                if isinstance(v, str) and v.strip():
                    return v
    except Exception:
        pass

    # 3) Raw provider metadata (OpenAI-style)
    try:
        meta = getattr(response, "response_metadata", None) or {}
        if isinstance(meta, dict):
            choices = meta.get("choices")
            if isinstance(choices, list) and choices:
                m = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(m, dict):
                    v = m.get("content")
                    if isinstance(v, str) and v.strip():
                        return v
    except Exception:
        pass

    # 4) Last resort
    try:
        s = str(response)
        return s if s and s != "None" else ""
    except Exception:
        return ""


def _ensure_response_content(response: Any) -> Any:
    """
    If response.content is empty but we can extract text elsewhere, populate it so
    downstream JSON parsing/repair has something to work with.
    """
    try:
        c = getattr(response, "content", None)
        if isinstance(c, str) and c.strip():
            return response
    except Exception:
        return response
    text = _best_effort_response_text(response)
    if text.strip():
        try:
            setattr(response, "content", text)
        except Exception:
            pass
    return response


def invoke_llm_with_metrics(llm: Any, messages: list, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """
    Invoke LLM and return (response, metrics).

    Metrics are intended for UX/observability (e.g., ReasoningEntry display):
      - prompt_tokens / completion_tokens / total_tokens (when provider reports)
      - backend (local/openrouter)
      - model (best-effort)
      - duration_s

    This does NOT change retry/fallback behavior: it wraps `invoke_llm`.
    """
    import time as _time

    t0 = _time.time()
    resp = invoke_llm(llm, messages, **kwargs)
    resp = _ensure_response_content(resp)
    dt = max(0.0, _time.time() - t0)
    usage = _extract_token_usage(resp) if resp is not None else {}
    usage = _sanitize_usage_with_estimates(usage=usage, messages=messages, response=resp, backend=str(_last_backend or ""))
    model = _extract_model_name_from_response(resp) or _resolve_model_name(llm)
    # `_last_backend` is set inside invoke_llm based on which backend actually returned.
    metrics = {
        "backend": _last_backend,
        "model": model,
        "duration_s": round(float(dt), 4),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    return resp, metrics


def _persist_token_usage(
    *,
    llm: Any,
    backend: str,
    duration_s: float,
    response: Any | None,
    success: bool,
) -> None:
    """Persist one LLM call usage row to the local token usage DB."""
    try:
        from agents.tracking.token_usage import record_llm_tokens

        role = getattr(llm, "_trading_agent_role", None)
        model = _resolve_model_name(llm)
        usage = _extract_token_usage(response) if response is not None else {}
        record_llm_tokens(
            agent_role=role if isinstance(role, str) else None,
            model=model or None,
            backend=str(backend or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            duration_s=float(duration_s),
            success=bool(success),
        )
    except Exception:
        # Token tracking should never break real execution
        log.debug("token usage persistence failed", exc_info=True)


def invoke_llm(llm: Any, messages: list, **kwargs: Any):
    """
    OPENROUTER_ENABLED=false: invoke the local client only (no OpenRouter).

    OPENROUTER_ENABLED=true: local first if LLAMA_LOCAL_PRIMARY, else cloud first,
    with rate-limit retries on cloud and optional local fallback.

    Design intent:
    - This wrapper is the *only* place that should implement retry/fallback logic.
      Agents call `invoke_llm(...)` so behavior is consistent across the system.
    - When cloud is primary, we still want high uptime: transient cloud failures
      (429s, provider outages) should degrade to local inference if available.
    - Token/timing/MLflow logging is also centralized here so observability stays
      correct even when we switch backends mid-call.
    """
    global _last_backend
    from agents.config import OPENROUTER_ENABLED
    from agents.llm_local import llama_local_chat_llm, llama_local_primary_enabled
    from agents.llm_openrouter import openrouter_chat_llm

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
        msgs = _normalize_messages_for_strict_local(messages)
        t0 = time.time()
        try:
            result = llm.invoke(msgs, **kwargs)
            _last_backend = "local"
            _reset_local_cooldown()
            if pool_url:
                server_pool.release(pool_url, success=True)
            _mlflow_log_llm_call(llm, msgs, result, duration_s=time.time() - t0, backend="local")
            _persist_token_usage(llm=llm, backend="local", duration_s=time.time() - t0, response=result, success=True)
            return result
        except Exception as e:
            if pool_url:
                is_conn = _is_connectivity_error(e)
                server_pool.release(pool_url, success=not is_conn)
            if _is_connectivity_error(e):
                _mark_local_failed()
            _mlflow_log_llm_call(llm, msgs, None, duration_s=time.time() - t0, backend="local", error=e)
            _persist_token_usage(llm=llm, backend="local", duration_s=time.time() - t0, response=None, success=False)
            raise

    # When OpenRouter is enabled, always keep a way to construct a cloud client for fallback.
    # In "local-primary" mode, `llm` may be a local client (from llm_providers.chat_llm).
    # We stash the intended OpenRouter route on the local client as `_openrouter_model`.
    try:
        or_model = getattr(llm, "_openrouter_model", None)
        if not isinstance(or_model, str) or not or_model.strip():
            or_model = _resolve_model_name(llm)
        or_model = str(or_model or "").strip()
    except Exception:
        or_model = ""

    def _openrouter_client() -> Any:
        # If we don't have a model slug, we still try to call `llm` directly as a last resort.
        if not or_model:
            return llm
        try:
            temp = float(getattr(llm, "temperature", 0.1))
        except Exception:
            temp = 0.1
        # Do not force max_tokens here; provider/env defaults apply.
        out = openrouter_chat_llm(or_model, temperature=temp)
        try:
            setattr(out, "_trading_agent_role", getattr(llm, "_trading_agent_role", None))
        except Exception:
            pass
        return out

    if llama_local_primary_enabled():
        from agents.llm_local import resolve_local_base_url

        role = getattr(llm, "_trading_agent_role", None)
        base = resolve_local_base_url(role if isinstance(role, str) else None)
        if _local_in_cooldown():
            log.debug("invoke_llm: local llama.cpp in cooldown, using OpenRouter directly")
        else:
            local = llama_local_chat_llm(llm)
            log.debug("invoke_llm: trying local llama.cpp at %s", base)
            msgs = _normalize_messages_for_strict_local(messages)
            t0 = time.time()
            try:
                result = local.invoke(msgs, **kwargs)
                _reset_local_cooldown()   # success → clear any prior cooldown
                _last_backend = "local"
                _mlflow_log_llm_call(local, msgs, result, duration_s=time.time() - t0, backend="local")
                _persist_token_usage(llm=local, backend="local", duration_s=time.time() - t0, response=result, success=True)
                return result
            except Exception as e:
                _mlflow_log_llm_call(local, msgs, None, duration_s=time.time() - t0, backend="local", error=e)
                _persist_token_usage(llm=local, backend="local", duration_s=time.time() - t0, response=None, success=False)
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
        t0 = time.time()
        try:
            cloud = _openrouter_client()
            result = cloud.invoke(messages, **kwargs)
            _last_backend = "openrouter"
            _mlflow_log_llm_call(cloud, messages, result, duration_s=time.time() - t0, backend="openrouter")
            _persist_token_usage(llm=cloud, backend="openrouter", duration_s=time.time() - t0, response=result, success=True)
            return result
        except Exception as e:
            last = e
            _log_openrouter_not_found(e)
            # Only log the final failed attempt to avoid spamming MLflow with every 429.
            if not (_is_rate_limit(e) and attempt < _MAX_ATTEMPTS - 1):
                cloud = _openrouter_client()
                _mlflow_log_llm_call(cloud, messages, None, duration_s=time.time() - t0, backend="openrouter", error=e)
                _persist_token_usage(llm=cloud, backend="openrouter", duration_s=time.time() - t0, response=None, success=False)
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
            except Exception as e_setup:
                log.error("Local llama.cpp fallback setup failed: %s", e_setup)
                raise last from e_setup
            t0 = time.time()
            try:
                result = local.invoke(msgs, **kwargs)
            except Exception as e2:
                _mlflow_log_llm_call(local, msgs, None, duration_s=time.time() - t0, backend="local", error=e2)
                _persist_token_usage(llm=local, backend="local", duration_s=time.time() - t0, response=None, success=False)
                if _is_connectivity_error(e2):
                    _mark_local_failed()
                log.error("Local llama.cpp fallback also failed: %s", e2)
                raise last from e2
            _last_backend = "local"
            _mlflow_log_llm_call(local, msgs, result, duration_s=time.time() - t0, backend="local")
            _persist_token_usage(llm=local, backend="local", duration_s=time.time() - t0, response=result, success=True)
            return result

    if last is not None:
        raise last
    raise RuntimeError("invoke_llm: no result and no exception")
