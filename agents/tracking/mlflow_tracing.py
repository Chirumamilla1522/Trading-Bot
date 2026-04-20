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
    Shows up in MLflow UI as a child run you can click into to inspect artifacts.
    """
    mlflow = _mlflow()
    if not mlflow:
        return
    try:
        with mlflow.start_run(run_name=str(agent), nested=True):
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
