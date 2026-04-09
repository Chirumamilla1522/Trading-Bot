"""
Structured contracts for per-ticker research memory (universe precompute).

Designed for: fast UI on click, Autopilot gates, incremental invalidation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SignalSnapshot(BaseModel):
    """Deterministic inputs hashed for dirty detection (no LLM)."""

    ticker: str
    iv_30d: float = 0.0
    pc_ratio: float = 0.0
    underlying_price: float = 0.0
    change_pct: float | None = None
    news_count_24h: int = 0
    news_sentiment_agg: float = 0.0
    high_priority_news: int = 0
    impact_score: float = 0.0  # from news_impact_map if present
    scanner_ok: bool = False


class EpistemicMeta(BaseModel):
    """Belief clock: when this brief should be treated as stale."""

    valid_until: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_minutes: int = 30
    stale_reason: str = ""


class StressCheck(BaseModel):
    """Lightweight pre-execution sanity (not full MC)."""

    max_loss_usd_estimate: float | None = None
    spot_shock_pct: float = 1.0
    notes: str = ""


class TickerBrief(BaseModel):
    """
    One-screen + machine-readable brief for a single name.
    Contract version bumps when schema meaningfully changes.
    """

    contract_version: str = "1.0.0"
    ticker: str
    thesis_short: str = ""
    key_risks: list[str] = Field(default_factory=list)
    what_changed: list[str] = Field(default_factory=list)
    invalidation_triggers: list[str] = Field(default_factory=list)
    stance: str = "HOLD"  # LONG | SHORT | HOLD | NEUTRAL
    confidence: float = 0.5
    regime_note: str = ""
    next_watch: list[str] = Field(default_factory=list)
    suggested_structure: str = ""  # e.g. "iron condor" — not an order
    epistemic: EpistemicMeta = Field(default_factory=EpistemicMeta)
    stress: StressCheck = Field(default_factory=StressCheck)
    agent_notes: str = ""  # freeform model rationale (short)

    # Lineage
    signal_hash: str = ""
    model_id: str = ""
    prompt_version: str = "universe_brief_v1"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UniverseRowSummary(BaseModel):
    """Row for GET /research/universe/summary."""

    ticker: str
    stance: str = "HOLD"
    confidence: float = 0.0
    updated_at: str | None = None
    valid_until: str | None = None
    dirty: bool = False
    dirty_reasons: list[str] = Field(default_factory=list)
    priority_score: float = 0.0
    signal_hash: str = ""
    has_brief: bool = False


class ResearchJob(BaseModel):
    ticker: str
    priority: float = 0.0
    reasons: list[str] = Field(default_factory=list)

    def __lt__(self, other: Any) -> bool:
        # For heap: higher priority first → negate in heapq
        if not isinstance(other, ResearchJob):
            return NotImplemented
        return self.priority < other.priority
