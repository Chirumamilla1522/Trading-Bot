"""
Agent Output Schemas – Pydantic v2 validation for every LLM response.

Each agent calls `parse_and_validate(raw_json_str, SchemaClass)` instead of
directly using `parse_llm_json`. This provides:
  - Field-level type coercion (str → float, "PROCEED" → AgentDecision enum)
  - Explicit defaults so missing fields never silently cause downstream bugs
  - Clear error messages logged when the model returns unexpected shapes
  - No external dependency beyond Pydantic (already a project dependency)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

from agents.state import AgentDecision

log = logging.getLogger(__name__)
T   = TypeVar("T", bound=BaseModel)


# ─── Shared helpers ────────────────────────────────────────────────────────────

def _coerce_decision(v: Any) -> str:
    """Accept lowercase / partial values and normalise to enum-safe uppercase."""
    if isinstance(v, str):
        v = v.strip().upper()
        if v.startswith("PROC"):  return "PROCEED"
        if v.startswith("HOLD"):  return "HOLD"
        if v.startswith("ABOR"):  return "ABORT"
    return "HOLD"


def _clamp_confidence(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def parse_and_validate(raw: str, schema: Type[T], agent_name: str = "") -> T | None:
    """
    Parse `raw` as JSON then validate against `schema`.
    Returns the validated model on success, or None on any failure.
    Failures are logged as warnings, not exceptions, so the agent can fall back
    to a safe default rather than crashing the cycle.
    """
    data: Any = {}
    try:
        from agents.parse_llm_json import parse_llm_json
        data = parse_llm_json(raw)
    except Exception as e:
        # Be tolerant: if parsing fails (empty / truncated / braces), fall back to
        # schema defaults rather than failing the entire agent cycle.
        log.warning("[%s] JSON parse failed: %s | raw[:200]=%r", agent_name, e, raw[:200])
        data = {}
    try:
        return schema.model_validate(data)
    except Exception as e:
        # Second fallback: validate an empty dict to get defaults.
        log.warning("[%s] Schema validation failed: %s | data=%r", agent_name, e, data)
        try:
            return schema.model_validate({})
        except Exception:
            return None


# ─── Options Specialist ────────────────────────────────────────────────────────

class KeyLevelOut(BaseModel):
    kind: str = ""     # "support" | "resistance"
    price: float = 0.0
    source: str = ""
    distance_pct: float = 0.0
    confidence: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _coerce_from_scalar(cls, v):
        # Some models emit key_levels as a list of floats (prices). Accept that shape
        # and coerce into the expected object so the cycle doesn't fail.
        if isinstance(v, (int, float)):
            return {
                "kind": "",
                "price": float(v),
                "source": "llm_scalar",
                "distance_pct": 0.0,
                "confidence": 0.0,
            }
        return v

    @field_validator("kind", mode="before")
    @classmethod
    def norm_kind(cls, v):
        s = str(v or "").strip().lower()
        return "support" if s.startswith("sup") else "resistance" if s.startswith("res") else s or ""

    @field_validator("price", "distance_pct", "confidence", mode="before")
    @classmethod
    def norm_float(cls, v):
        try:
            return float(v)
        except Exception:
            return 0.0


class OptionsSpecialistOutput(BaseModel):
    decision:         str     = "HOLD"
    iv_regime:        str     = "UNKNOWN"
    skew_signal:      str     = "NEUTRAL"
    term_signal:      str     = "FLAT"
    opportunity:      Optional[str] = None
    preferred_dte_bucket: Optional[str] = None
    confidence:       float   = 0.5
    reasoning:        str     = ""
    # Prompt contract fields (no hallucinated charting)
    insufficient_data: bool = False
    bias: str = "neutral"  # bullish | bearish | neutral
    setup_type: str = "unknown"  # range_fade | breakout_continuation | breakout_rejection | trend_pullback | unknown
    key_levels: list[KeyLevelOut] = Field(default_factory=list)
    confirmation: str = ""
    invalidation: str = ""
    risk_notes: list[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v): return _clamp_confidence(v)

    @field_validator("bias", mode="before")
    @classmethod
    def norm_bias(cls, v):
        s = str(v or "").strip().lower()
        if s.startswith("bull"):
            return "bullish"
        if s.startswith("bear"):
            return "bearish"
        if s in ("neutral", "flat"):
            return "neutral"
        return "neutral"

    @field_validator("setup_type", mode="before")
    @classmethod
    def norm_setup(cls, v):
        s = str(v or "").strip().lower()
        allowed = {
            "range_fade",
            "breakout_continuation",
            "breakout_rejection",
            "trend_pullback",
            "unknown",
        }
        return s if s in allowed else "unknown"

    @field_validator("confirmation", "invalidation", mode="before")
    @classmethod
    def norm_required_strings(cls, v):
        # LLMs sometimes emit null for required prompt-contract strings, especially on ABORT/HOLD.
        # Coerce to empty string so validation doesn't fail and the agent can proceed safely.
        if v is None:
            return ""
        return str(v)

    @field_validator("insufficient_data", mode="before")
    @classmethod
    def norm_insuf(cls, v):
        try:
            if isinstance(v, bool):
                return v
            s = str(v or "").strip().lower()
            return s in ("1", "true", "yes", "y")
        except Exception:
            return False


# ─── Stock Specialist ─────────────────────────────────────────────────────────

class StockSpecialistOutput(BaseModel):
    decision:     str   = "HOLD"
    side:         str   = "BUY"    # BUY | SELL (ignored if HOLD)
    qty:          float = 0.0
    order_type:   str   = "market" # market | limit
    limit_price:  float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    confidence:   float = 0.5
    reasoning:    str   = ""

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("side", mode="before")
    @classmethod
    def norm_side(cls, v):
        s = str(v or "").strip().upper()
        return "SELL" if s.startswith("S") else "BUY"

    @field_validator("qty", mode="before")
    @classmethod
    def norm_qty(cls, v):
        try:
            return max(0.0, float(v))
        except Exception:
            return 0.0

    @field_validator("order_type", mode="before")
    @classmethod
    def norm_ot(cls, v):
        s = str(v or "").strip().lower()
        return "limit" if s.startswith("l") else "market"

    @field_validator("limit_price", mode="before")
    @classmethod
    def norm_lp(cls, v):
        if v is None or v == "":
            return None
        try:
            x = float(v)
            return x if x > 0 else None
        except Exception:
            return None

    @field_validator("stop_loss_pct", "take_profit_pct", "confidence", mode="before")
    @classmethod
    def norm_pct(cls, v):
        if v is None or v == "":
            return None if v is None else None
        try:
            # pct fields can be null; confidence must be clamped
            return _clamp_confidence(v)
        except Exception:
            return None


# ─── Sentiment Analyst ─────────────────────────────────────────────────────────

class HeadlineScore(BaseModel):
    text:   str   = ""
    score:  float = 0.0
    weight: float = 1.0

    @field_validator("score", "weight", mode="before")
    @classmethod
    def norm_float(cls, v):
        try:   return float(v)
        except: return 0.0


class SentimentAnalystOutput(BaseModel):
    decision:            str          = "HOLD"
    aggregate_sentiment: float        = 0.0
    weighted_sentiment:  float        = 0.0
    headline_scores:     list[HeadlineScore] = Field(default_factory=list)
    key_themes:          list[str]    = Field(default_factory=list)
    tail_risks:          list[str]    = Field(default_factory=list)
    catalyst_detected:   bool         = False
    confidence:          float        = 0.5
    reasoning:           str          = ""

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("aggregate_sentiment", "weighted_sentiment", mode="before")
    @classmethod
    def norm_sentiment(cls, v):
        try:   return max(-1.0, min(1.0, float(v)))
        except: return 0.0

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v): return _clamp_confidence(v)


# ─── Strategist ────────────────────────────────────────────────────────────────

class StrategyLegOutput(BaseModel):
    symbol: str   = ""
    right:  str   = "CALL"
    strike: float = 0.0
    expiry: str   = ""
    side:   str   = "BUY"
    qty:    int   = 1

    @field_validator("right", mode="before")
    @classmethod
    def norm_right(cls, v):
        v = str(v).upper()
        return "CALL" if v.startswith("C") else "PUT"

    @field_validator("side", mode="before")
    @classmethod
    def norm_side(cls, v):
        v = str(v).upper()
        return "BUY" if v.startswith("B") else "SELL"


class StrategyProposalOutput(BaseModel):
    strategy_name:    str                     = "Unknown"
    legs:             list[StrategyLegOutput] = Field(default_factory=list)
    max_risk:         float                   = 0.0
    target_return:    float                   = 0.0
    stop_loss_pct:    float                   = 0.50
    take_profit_pct:  float                   = 0.75
    rationale:        str                     = ""
    confidence:       float                   = 0.0

    @field_validator("max_risk", "target_return", mode="before")
    @classmethod
    def norm_dollars(cls, v):
        try:   return max(0.0, float(v))
        except: return 0.0

    @field_validator("stop_loss_pct", "take_profit_pct", "confidence", mode="before")
    @classmethod
    def norm_pct(cls, v): return _clamp_confidence(v)


class StockProposalOutput(BaseModel):
    side: str = "BUY"          # BUY | SELL
    qty: float = 0.0
    order_type: str = "market" # market | limit
    limit_price: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    rationale: str = ""
    confidence: float = 0.0

    @field_validator("side", mode="before")
    @classmethod
    def norm_side(cls, v):
        s = str(v or "").strip().upper()
        return "SELL" if s.startswith("S") else "BUY"

    @field_validator("qty", mode="before")
    @classmethod
    def norm_qty(cls, v):
        try:
            return max(0.0, float(v))
        except Exception:
            return 0.0

    @field_validator("order_type", mode="before")
    @classmethod
    def norm_ot(cls, v):
        s = str(v or "").strip().lower()
        return "limit" if s.startswith("l") else "market"

    @field_validator("limit_price", mode="before")
    @classmethod
    def norm_lp(cls, v):
        if v is None or v == "":
            return None
        try:
            x = float(v)
            return x if x > 0 else None
        except Exception:
            return None

    @field_validator("stop_loss_pct", "take_profit_pct", "confidence", mode="before")
    @classmethod
    def norm_pct(cls, v):
        if v is None or v == "":
            return None
        return _clamp_confidence(v)


class StrategistOutput(BaseModel):
    decision: str                          = "HOLD"
    reason:   Optional[str]                = None
    proposal: Optional[StrategyProposalOutput] = None
    stock_proposal: Optional[StockProposalOutput] = None
    # Prompt contract fields (thesis must be falsifiable)
    insufficient_data: bool = False
    bias: str = "neutral"
    setup_type: str = "unknown"
    key_levels: list[KeyLevelOut] = Field(default_factory=list)
    confirmation: str = ""
    invalidation: str = ""
    risk_notes: list[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("bias", mode="before")
    @classmethod
    def norm_bias(cls, v):
        s = str(v or "").strip().lower()
        if s.startswith("bull"):
            return "bullish"
        if s.startswith("bear"):
            return "bearish"
        if s in ("neutral", "flat"):
            return "neutral"
        return "neutral"

    @field_validator("setup_type", mode="before")
    @classmethod
    def norm_setup(cls, v):
        s = str(v or "").strip().lower()
        allowed = {
            "range_fade",
            "breakout_continuation",
            "breakout_rejection",
            "trend_pullback",
            "unknown",
        }
        return s if s in allowed else "unknown"

    @field_validator("insufficient_data", mode="before")
    @classmethod
    def norm_insuf(cls, v):
        try:
            if isinstance(v, bool):
                return v
            s = str(v or "").strip().lower()
            return s in ("1", "true", "yes", "y")
        except Exception:
            return False


# ─── Risk Manager ─────────────────────────────────────────────────────────────

class RiskManagerOutput(BaseModel):
    decision:        str       = "HOLD"
    violations:      list[str] = Field(default_factory=list)
    risk_reward_ok:  bool      = True
    execution_risk:  str       = "MEDIUM"
    reasoning:       str       = ""

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("execution_risk", mode="before")
    @classmethod
    def norm_exec_risk(cls, v):
        v = str(v).upper()
        return v if v in ("LOW", "MEDIUM", "HIGH") else "MEDIUM"


# ─── Adversarial debate judge ─────────────────────────────────────────────────

class DebateJudgeOutput(BaseModel):
    verdict:      str   = "HOLD"
    bull_score:   int   = 5
    bear_score:   int   = 5
    winning_side: str   = "TIE"
    summary:      str   = ""
    confidence:   float = 0.5

    @field_validator("verdict", mode="before")
    @classmethod
    def norm_verdict(cls, v): return _coerce_decision(v)

    @field_validator("bull_score", "bear_score", mode="before")
    @classmethod
    def norm_score(cls, v):
        try:   return max(1, min(10, int(v)))
        except: return 5

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v): return _clamp_confidence(v)

    @field_validator("winning_side", mode="before")
    @classmethod
    def norm_side(cls, v):
        v = str(v).upper()
        return v if v in ("BULL", "BEAR", "TIE") else "TIE"


# ─── Desk Head ────────────────────────────────────────────────────────────────

class DeskHeadOutput(BaseModel):
    decision:       str            = "HOLD"
    confidence:     float          = 0.0
    signal_weights: dict[str, Any] = Field(default_factory=dict)
    reasoning:      str            = ""

    @field_validator("decision", mode="before")
    @classmethod
    def norm_decision(cls, v): return _coerce_decision(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v): return _clamp_confidence(v)
