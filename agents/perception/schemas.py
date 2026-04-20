"""
Phase 0 — Structured I/O for perception agents (Pydantic v2).

All downstream layers (OMA, researchers, strategist) should consume these models
instead of ad-hoc dicts.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TrendLabel(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class VolatilityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TradeSignal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class EventSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EventKind(str, Enum):
    VOLUME_SPIKE = "volume_spike"
    RETURN_SPIKE = "return_spike"
    VOLATILITY_SPIKE = "volatility_spike"
    GAP = "gap"
    CALM_ANOMALY = "calm_anomaly"


class MacroShockRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MarketDataSnapshot(BaseModel):
    """Unified inputs available for one symbol at decision time."""

    ticker: str
    as_of_unix: float = 0.0
    bars_timeframe: str = "1Day"
    bars_count: int = 0
    bars_source: str = ""
    quote: dict[str, Any] = Field(default_factory=dict)
    fundamentals_cached: bool = False


class TechnicalReport(BaseModel):
    trend: TrendLabel = TrendLabel.SIDEWAYS
    trend_confidence: float = Field(0.0, ge=0.0, le=1.0)
    volatility_level: VolatilityLevel = VolatilityLevel.MEDIUM
    rsi14: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    atr14: float | None = None
    atr_pct_of_price: float | None = None
    support_level: float | None = None
    resistance_level: float | None = None
    signal: TradeSignal = TradeSignal.HOLD
    signal_confidence: float = Field(0.0, ge=0.0, le=1.0)
    features: dict[str, Any] = Field(default_factory=dict)


class FundamentalReport(BaseModel):
    ticker: str
    name: str = ""
    sector: str = ""
    pe_ratio: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    market_cap: float | None = None
    revenue_growth: float | None = None
    gross_margin: float | None = None
    profit_margin: float | None = None
    return_on_equity: float | None = None
    dividend_yield: float | None = None
    debt_to_equity_hint: str = ""  # qualitative until we add field from yfinance
    valuation_note: str = ""
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    raw_keys_present: list[str] = Field(default_factory=list)


class SentimentPerceptionReport(BaseModel):
    """Aggregated sentiment from headlines (Phase 2 — deterministic NLP-lite)."""

    aggregate_score: float = Field(0.0, ge=-1.0, le=1.0)
    weighted_score: float = Field(0.0, ge=-1.0, le=1.0)
    hype_score: float = Field(0.0, ge=0.0, le=1.0)
    fear_score: float = Field(0.0, ge=0.0, le=1.0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    anomaly: bool = False
    headline_sample: list[str] = Field(default_factory=list)
    n_headlines_used: int = 0
    source: Literal["news_aggregated"] = "news_aggregated"


class NewsImpactRow(BaseModel):
    headline: str = ""
    impact: float = 0.0
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    urgency_tier: str = "T2"
    category: str = "general"


class NewsPerceptionReport(BaseModel):
    high_impact_count: int = 0
    items: list[NewsImpactRow] = Field(default_factory=list)
    macro_shock_risk: MacroShockRisk = MacroShockRisk.LOW
    dominant_themes: list[str] = Field(default_factory=list)


class EventSignal(BaseModel):
    kind: EventKind
    severity: EventSeverity = EventSeverity.LOW
    detail: str = ""
    metric_value: float | None = None


class EventDetectionReport(BaseModel):
    events: list[EventSignal] = Field(default_factory=list)
    mode: Literal["normal", "elevated", "event_driven"] = "normal"


class PerceptionBundle(BaseModel):
    """Single traceable bundle for Phases 0–2."""

    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    snapshot: MarketDataSnapshot
    technical: TechnicalReport
    fundamental: FundamentalReport
    sentiment: SentimentPerceptionReport
    news: NewsPerceptionReport
    events: EventDetectionReport

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "created_at": self.created_at.isoformat(),
            "ticker": self.snapshot.ticker,
            "payload": self.model_dump(mode="json"),
        }
