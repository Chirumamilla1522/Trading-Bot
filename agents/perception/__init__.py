"""
Perception layer — Phases 0–2 (schemas, deterministic analysts, NLP-lite sentiment/news).

- :func:`run_perception_bundle` — full pipeline for one ticker
- :class:`PerceptionBundle` — structured output for OMA / researchers (Phase 3+)
"""
from agents.perception.pipeline import run_perception_bundle, run_perception_bundle_json
from agents.perception.schemas import PerceptionBundle

__all__ = [
    "PerceptionBundle",
    "run_perception_bundle",
    "run_perception_bundle_json",
]
