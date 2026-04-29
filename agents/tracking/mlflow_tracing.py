"""
Optional MLflow logging for Tier-3 agent cycles.

Enable by setting MLFLOW_TRACKING_URI (e.g. http://127.0.0.1:5000).
Disable explicitly with MLFLOW_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from agents.config import (
    MLFLOW_ENABLED,
    MLFLOW_EXPERIMENT_NAME,
    MLFLOW_LLM_TEXT_MAX_CHARS,
    MLFLOW_LOG_LLM_CALLS,
    MLFLOW_TRACKING_URI,
)

if TYPE_CHECKING:
    from agents.state import FirmState

log = logging.getLogger(__name__)


def tracking_uri_public_hint() -> str:
    """Return tracking URI without credentials for UI display."""
    uri = MLFLOW_TRACKING_URI
    if not uri:
        return ""
    try:
        p = urlparse(uri)
        if p.username:
            host = p.hostname or ""
            port = f":{p.port}" if p.port else ""
            netloc = f"{host}{port}"
            return urlunparse((p.scheme, netloc, p.path or "", "", "", ""))
    except Exception:
        pass
    return uri


def mlflow_status_dict() -> dict[str, Any]:
    return {
        "enabled": MLFLOW_ENABLED,
        "experiment": MLFLOW_EXPERIMENT_NAME,
        "tracking_uri_hint": tracking_uri_public_hint() if MLFLOW_ENABLED else "",
        "log_llm_calls": bool(MLFLOW_ENABLED and MLFLOW_LOG_LLM_CALLS),
        "llm_text_max_chars": int(MLFLOW_LLM_TEXT_MAX_CHARS),
    }


def log_t3_cycle(state: FirmState, err: Exception | None, duration_s: float) -> None:
    if not MLFLOW_ENABLED:
        return
    try:
        import mlflow
    except ImportError:
        log.warning("MLFLOW_TRACKING_URI is set but mlflow is not installed")
        return

    run_name = f"{state.ticker}_{int(time.time())}"
    success = 1 if err is None else 0

    try:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)

        tags: dict[str, str] = {
            "pipeline": "tier3",
            "ticker": state.ticker,
            "trading_mode": state.trading_mode,
            "tier3_trigger": state.tier3_trigger,
            "market_regime": state.market_regime.value
            if hasattr(state.market_regime, "value")
            else str(state.market_regime),
        }

        params: dict[str, str] = {
            "ticker": state.ticker,
            "trading_mode": state.trading_mode,
            "tier3_trigger": state.tier3_trigger,
            "trader_decision": state.trader_decision.value
            if hasattr(state.trader_decision, "value")
            else str(state.trader_decision),
        }
        if err is not None:
            params["error_type"] = type(err).__name__
            params["error_message"] = str(err)[:500]

        metrics: dict[str, float] = {
            "success": float(success),
            "cycle_duration_s": float(duration_s),
            "reasoning_steps": float(len(state.reasoning_log)),
            "bull_conviction": float(state.bull_conviction),
            "bear_conviction": float(state.bear_conviction),
            "strategy_confidence": float(state.strategy_confidence),
            "aggregate_sentiment": float(state.aggregate_sentiment),
            "movement_signal": float(state.movement_signal),
            "circuit_breaker_tripped": 1.0 if state.circuit_breaker_tripped else 0.0,
            "kill_switch_active": 1.0 if state.kill_switch_active else 0.0,
        }

        with mlflow.start_run(run_name=run_name):
            mlflow.set_tags(tags)
            for k, v in params.items():
                mlflow.log_param(k, v)
            for k, v in metrics.items():
                mlflow.log_metric(k, v)

            tail = state.reasoning_log[-25:]
            if tail:
                payload = [e.model_dump(mode="json") for e in tail]
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "reasoning_tail.json"
                    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    mlflow.log_artifact(str(path), artifact_path="xai")
    except Exception:
        log.warning("MLflow logging failed", exc_info=True)


def _mlflow() -> Any | None:
    if not MLFLOW_ENABLED:
        return None
    try:
        import mlflow  # type: ignore
    except ImportError:
        return None
    try:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)
    except Exception:
        pass
    return mlflow


def start_cycle_run(state: FirmState) -> str | None:
    """
    Start a parent MLflow run for one full agent cycle.
    Returns run_id, or None if disabled/unavailable.
    """
    mlflow = _mlflow()
    if not mlflow:
        return None
    try:
        run_name = f"{state.ticker}_{int(time.time())}"
        tags: dict[str, str] = {
            "pipeline": "tier3",
            "ticker": state.ticker,
            "trading_mode": state.trading_mode,
            "tier3_trigger": getattr(state, "tier3_trigger", "manual") or "manual",
        }
        run = mlflow.start_run(run_name=run_name)
        mlflow.set_tags(tags)
        mlflow.log_param("ticker", state.ticker)
        mlflow.log_param("trading_mode", state.trading_mode)
        mlflow.log_param("tier3_trigger", getattr(state, "tier3_trigger", "manual") or "manual")
        return getattr(run, "info", None).run_id if run else None
    except Exception:
        log.debug("MLflow start_cycle_run failed", exc_info=True)
        return None


def end_cycle_run(state: FirmState, err: Exception | None, duration_s: float) -> None:
    """Finalize the currently active parent run (if any)."""
    mlflow = _mlflow()
    if not mlflow:
        return
    try:
        mlflow.log_metric("success", 1.0 if err is None else 0.0)
        mlflow.log_metric("cycle_duration_s", float(duration_s))
        mlflow.log_metric("reasoning_steps", float(len(state.reasoning_log or [])))
        mlflow.log_metric("strategy_confidence", float(getattr(state, "strategy_confidence", 0.0) or 0.0))
        mlflow.log_metric("aggregate_sentiment", float(getattr(state, "aggregate_sentiment", 0.0) or 0.0))
        if err is not None:
            mlflow.set_tag("error_type", type(err).__name__)
            mlflow.set_tag("error_message", str(err)[:500])
        # Keep a small tail artifact for quick browsing
        tail = (state.reasoning_log or [])[-25:]
        if tail:
            payload = [e.model_dump(mode="json") for e in tail]
            mlflow.log_dict(payload, "xai/reasoning_tail.json")
    except Exception:
        log.debug("MLflow end_cycle_run logging failed", exc_info=True)
    try:
        mlflow.end_run()
    except Exception:
        pass


def _has_active_run(mlflow: Any) -> bool:
    try:
        return mlflow.active_run() is not None
    except Exception:
        return False


def log_agent_step(
    agent: str,
    *,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    metrics: dict[str, float] | None = None,
    tags: dict[str, str] | None = None,
    duration_s: float | None = None,
) -> None:
    """
    Log one agent step as a nested run under the active cycle run.
    If no cycle run is active (e.g. Tier-2 calls outside Tier-3), log a standalone run
    so nothing is lost.
    """
    # Always persist timing locally when available (independent of MLflow).
    try:
        if duration_s is not None:
            from agents.tracking.agent_timing import record_agent_timing
            record_agent_timing(agent=str(agent), duration_s=float(duration_s))
    except Exception:
        pass

    mlflow = _mlflow()
    if not mlflow:
        return
    try:
        nested = _has_active_run(mlflow)
        with mlflow.start_run(run_name=str(agent), nested=nested):
            mlflow.set_tag("agent", str(agent))
            if tags:
                mlflow.set_tags({str(k): str(v) for k, v in tags.items()})
            if duration_s is not None:
                mlflow.log_metric("duration_s", float(duration_s))
            if metrics:
                for k, v in metrics.items():
                    try:
                        mlflow.log_metric(str(k), float(v))
                    except Exception:
                        continue
            if inputs is not None:
                mlflow.log_dict(inputs, "inputs.json")
            if outputs is not None:
                mlflow.log_dict(outputs, "outputs.json")
    except Exception:
        log.debug("MLflow log_agent_step failed", exc_info=True)


# ── LLM call tracing ──────────────────────────────────────────────────────────

def _messages_to_payload(messages: Any) -> list[dict[str, Any]]:
    """Serialize LangChain message list (or plain list of dicts) to JSON-safe form."""
    out: list[dict[str, Any]] = []
    try:
        for m in messages or []:
            if isinstance(m, dict):
                out.append({
                    "role": str(m.get("role") or m.get("type") or "user"),
                    "content": str(m.get("content") or ""),
                })
                continue
            role = getattr(m, "type", None) or m.__class__.__name__
            role_s = str(role).lower().replace("message", "")
            # Map LangChain class names to OpenAI-style roles
            role_map = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}
            out.append({
                "role": role_map.get(role_s, role_s or "user"),
                "content": str(getattr(m, "content", "") or ""),
            })
    except Exception:
        return []
    return out


def _truncate_payload(payload: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    if max_chars <= 0:
        return payload
    out: list[dict[str, Any]] = []
    budget = int(max_chars)
    for m in payload:
        c = str(m.get("content") or "")
        if len(c) > budget:
            m = {**m, "content": c[: max(0, budget)] + f"... [truncated {len(c) - budget} chars]"}
            budget = 0
        else:
            budget -= len(c)
        out.append(m)
        if budget <= 0:
            out.append({"role": "system", "content": "[further messages truncated by MLFLOW_LLM_TEXT_MAX_CHARS]"})
            break
    return out


def _extract_response_text(response: Any) -> str:
    try:
        c = getattr(response, "content", None)
        if isinstance(c, str):
            if c.strip():
                return c
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict):
                    parts.append(str(p.get("text") or p.get("content") or ""))
                else:
                    parts.append(str(p))
            out = "\n".join(x for x in parts if str(x).strip())
            if out.strip():
                return out
    except Exception:
        pass
    # Some adapters stash text in additional_kwargs even when content is empty.
    try:
        ak = getattr(response, "additional_kwargs", None) or {}
        if isinstance(ak, dict):
            for k in ("text", "output_text", "reasoning", "thinking", "answer"):
                v = ak.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            msg = ak.get("message")
            if isinstance(msg, dict):
                v = msg.get("content") or msg.get("text")
                if isinstance(v, str) and v.strip():
                    return v
    except Exception:
        pass
    # OpenAI-style choices in metadata
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
    try:
        return str(response)
    except Exception:
        return ""


def _extract_token_usage(response: Any) -> dict[str, int]:
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


def log_llm_call(
    *,
    agent_role: str | None,
    model: str | None,
    backend: str | None,
    messages: Any,
    response: Any,
    duration_s: float,
    error: BaseException | None = None,
    extra_tags: dict[str, str] | None = None,
) -> None:
    """
    Persist a single LLM call to MLflow as a nested run (or standalone when no parent).

    Logs:
      - params: agent, model, backend
      - metrics: duration_s, prompt_chars, completion_chars, prompt/completion/total tokens (if provided)
      - artifacts: prompt.json (messages), response.json, error.json (on failure)
    """
    if not MLFLOW_LOG_LLM_CALLS:
        return
    mlflow = _mlflow()
    if not mlflow:
        return
    try:
        role = str(agent_role or "unknown")
        prompt_payload = _messages_to_payload(messages)
        prompt_payload = _truncate_payload(prompt_payload, MLFLOW_LLM_TEXT_MAX_CHARS)
        response_text = _extract_response_text(response) if response is not None else ""
        if MLFLOW_LLM_TEXT_MAX_CHARS > 0 and len(response_text) > MLFLOW_LLM_TEXT_MAX_CHARS:
            response_text = response_text[:MLFLOW_LLM_TEXT_MAX_CHARS] + "... [truncated]"
        usage = _extract_token_usage(response) if response is not None else {}
        nested = _has_active_run(mlflow)
        with mlflow.start_run(run_name=f"llm:{role}", nested=nested):
            mlflow.set_tag("kind", "llm_call")
            mlflow.set_tag("agent", role)
            if model:
                mlflow.set_tag("model", str(model))
                mlflow.log_param("model", str(model))
            if backend:
                mlflow.set_tag("backend", str(backend))
                mlflow.log_param("backend", str(backend))
            mlflow.log_param("agent", role)
            if extra_tags:
                try:
                    mlflow.set_tags({str(k): str(v) for k, v in extra_tags.items()})
                except Exception:
                    pass
            try:
                mlflow.log_metric("duration_s", float(max(0.0, duration_s)))
                prompt_chars = sum(len(str(m.get("content") or "")) for m in prompt_payload)
                mlflow.log_metric("prompt_chars", float(prompt_chars))
                mlflow.log_metric("completion_chars", float(len(response_text or "")))
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if k in usage:
                        mlflow.log_metric(k, float(usage[k]))
                mlflow.log_metric("success", 0.0 if error is not None else 1.0)
            except Exception:
                pass
            try:
                mlflow.log_dict({"messages": prompt_payload}, "prompt.json")
            except Exception:
                pass
            try:
                mlflow.log_dict(
                    {"content": response_text, "usage": usage},
                    "response.json",
                )
            except Exception:
                pass
            if error is not None:
                try:
                    mlflow.set_tag("error_type", type(error).__name__)
                    mlflow.log_dict(
                        {"error_type": type(error).__name__, "message": str(error)[:2000]},
                        "error.json",
                    )
                except Exception:
                    pass
    except Exception:
        log.debug("MLflow log_llm_call failed", exc_info=True)
