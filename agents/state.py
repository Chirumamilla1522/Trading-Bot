"""
Pydantic v2 State Schema – the single source of truth shared across all agents.

Every agent reads from and writes to a FirmState instance. LangGraph passes this
object through the graph so all mutations are traceable and auditable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─── Domain enumerations ────────────────────────────────────────────────────────

class OptionRight(str, Enum):
    CALL = "CALL"
    PUT  = "PUT"

class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class AgentDecision(str, Enum):
    PROCEED    = "PROCEED"
    HOLD       = "HOLD"
    ABORT      = "ABORT"

class MarketRegime(str, Enum):
    TRENDING_UP   = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    MEAN_REVERTING= "MEAN_REVERTING"
    HIGH_VOL      = "HIGH_VOL"
    LOW_VOL       = "LOW_VOL"
    UNKNOWN       = "UNKNOWN"


# ─── Greeks snapshot ────────────────────────────────────────────────────────────

class GreeksSnapshot(BaseModel):
    symbol:    str
    expiry:    str
    strike:    float
    right:     OptionRight
    iv:        float = 0.0
    delta:     float = 0.0
    gamma:     float = 0.0
    theta:     float = 0.0
    vega:      float = 0.0
    rho:       float = 0.0
    bid:       float = 0.0
    ask:       float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─── Volatility surface snapshot ────────────────────────────────────────────────

class VolSurfacePoint(BaseModel):
    strike: float
    expiry: str
    iv:     float
    delta:  float

class VolSurface(BaseModel):
    underlying: str
    points:     list[VolSurfacePoint] = []
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── News / sentiment ────────────────────────────────────────────────────────────

class NewsItem(BaseModel):
    headline:      str
    source:        str
    published_at:  datetime
    sentiment:     float = 0.0   # [-1.0 bearish … +1.0 bullish]
    confidence:    float = 0.0
    tickers:       list[str] = []
    cached:        bool = False   # True if served from semantic cache
    # Category labels: "earnings"|"deal"|"macro"|"guidance"|"dividend"|
    #                  "regulatory"|"management"|"general"
    category:      str = "general"
    # Priority tier: "HIGH" (earnings/deals/macro) | "NORMAL" | "LOW"
    priority:      str = "NORMAL"
    # Ticker tier that triggered this article: "index"|"portfolio"|"active"|"top"
    ticker_tier:   str = "top"
    # Optional extended content (populated when available from source)
    summary:       str = ""   # article body snippet / abstract
    url:           str = ""   # canonical article link

    # ── Intelligence-first fields (computed server-side) ───────────────────────
    # Impact score: 0..1 (higher = more likely to move price / require attention)
    impact_score:  float = 0.0
    # Urgency tier: T0 (urgent) | T1 (high) | T2 (normal) | T3 (noise)
    urgency_tier:  str = "T2"
    # Model/extractor-discovered tickers mentioned in text (beyond headline tags)
    mentioned_tickers: list[str] = []
    # Per-source reliability weight (1.0 neutral, >1 more trusted, <1 less trusted)
    reliability_weight: float = 1.0
    # Volatility probability proxy (0..1). Derived from impact + category + source.
    vol_prob:      float = 0.0

    @field_validator("published_at", mode="before")
    @classmethod
    def _ensure_utc(cls, v: Any) -> datetime:
        """Coerce naive datetimes to UTC-aware so comparisons never raise TypeError."""
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


# ─── Open position (options) ───────────────────────────────────────────────────

class Position(BaseModel):
    leg_id:     str
    symbol:     str
    right:      OptionRight
    strike:     float
    expiry:     str
    quantity:   int
    avg_cost:   float
    current_pnl: float = 0.0
    greeks:     GreeksSnapshot | None = None


# ─── Stock / ETF position (cash equity) ─────────────────────────────────────────

class StockPosition(BaseModel):
    """US equity lot — distinct from option legs in `Position`."""

    ticker:        str
    quantity:      float = 0.0  # fractional shares allowed
    avg_cost:      float = 0.0
    market_value:  float = 0.0
    unrealized_pl: float = 0.0
    cost_basis:    float = 0.0


class StockTradeProposal(BaseModel):
    """Single-lot stock order recommendation (used in advisory mode)."""

    side:        OrderSide
    qty:         float = 0.0
    order_type:  str = "market"          # "market" | "limit"
    limit_price: float | None = None
    rationale:   str = ""
    confidence:  float = 0.0
    stop_loss_pct: float | None = None   # optional guidance (not enforced by broker order)
    take_profit_pct: float | None = None # optional guidance (not enforced by broker order)


# ─── Proposed trade ─────────────────────────────────────────────────────────────

class TradeLeg(BaseModel):
    symbol: str
    right:  OptionRight
    strike: float
    expiry: str
    side:   OrderSide
    qty:    int

class TradeProposal(BaseModel):
    strategy_name: str            # e.g. "Iron Condor", "Bull Put Spread"
    legs:          list[TradeLeg]
    max_risk:      float          # in dollars (max loss)
    target_return: float          # in dollars (target P&L)
    rationale:     str
    confidence:    float = 0.0   # 0.0-1.0
    stop_loss_pct: float = 0.50  # exit if position loses >50% of max_risk
    take_profit_pct: float = 0.75  # exit at 75% of target_return


# ─── Adversarial debate record ───────────────────────────────────────────────────

class DebateTurn(BaseModel):
    agent:    str   # "Bull" | "Bear" | "DeskHead"
    argument: str
    turn:     int

class DebateRecord(BaseModel):
    proposal: str
    turns:    list[DebateTurn] = []
    verdict:  AgentDecision = AgentDecision.HOLD
    summary:  str = ""


# ─── Recommendation (Advisory mode output) ────────────────────────────────────────

class Recommendation(BaseModel):
    """
    Created by the recommend_node when trading_mode == 'advisory'.
    The user can approve (→ execute via EMS) or dismiss from the UI.
    """
    id:                   str   = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    ticker:               str
    asset_type:           str = "option"   # "option" | "stock"
    strategy_name:        str
    proposal:             TradeProposal | None = None
    stock_proposal:       StockTradeProposal | None = None
    bull_conviction:      int   = 0
    bear_conviction:      int   = 0
    desk_head_reasoning:  str   = ""
    confidence:           float = 0.0
    created_at:           datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status:               str   = "pending"   # "pending" | "approved" | "dismissed" | "expired"
    # Set when status moves away from pending (approve / dismiss / expire).
    resolved_at:          datetime | None = None


# ─── Risk metrics ────────────────────────────────────────────────────────────────

class RiskMetrics(BaseModel):
    portfolio_delta:  float = 0.0
    portfolio_gamma:  float = 0.0
    portfolio_vega:   float = 0.0
    portfolio_theta:  float = 0.0
    daily_pnl:        float = 0.0
    opening_nav:      float = 0.0
    current_nav:      float = 0.0
    drawdown_pct:     float = 0.0
    max_drawdown_pct: float = 0.05   # 5 % kill-switch threshold
    capital_at_risk:  float = 0.0
    position_cap_pct: float = 0.02   # 2 % per position


# ─── XAI reasoning log entry ─────────────────────────────────────────────────────

class ReasoningEntry(BaseModel):
    agent:      str
    action:     str
    reasoning:  str
    inputs:     dict[str, Any] = {}
    outputs:    dict[str, Any] = {}
    timestamp:  datetime = Field(default_factory=datetime.utcnow)
    trade_id:   str | None = None


# ─── Master firm state ───────────────────────────────────────────────────────────

class FirmState(BaseModel):
    """Single mutable state object threaded through the LangGraph agent graph."""

    # Market data
    ticker:           str = "SPY"
    underlying_price: float = 0.0
    vol_surface:      VolSurface | None = None
    latest_greeks:    list[GreeksSnapshot] = []
    market_regime:    MarketRegime = MarketRegime.UNKNOWN

    # News & sentiment
    news_feed:        list[NewsItem] = []
    # If set, SentimentAnalyst uses this many hours for headline recency (e.g. dry run ties to --news-hours).
    sentiment_headline_lookback_hours: float | None = None
    aggregate_sentiment: float = 0.0   # rolling window — see SentimentAnalyst lookback

    # Positions & orders
    open_positions:   list[Position] = []
    stock_positions:  list[StockPosition] = []
    cash_balance:     float = 0.0
    buying_power:     float = 0.0
    account_equity:   float = 0.0  # broker total equity (cash + positions); 0 = unknown
    pending_proposal: TradeProposal | None = None
    pending_stock_proposal: StockTradeProposal | None = None
    debate_record:    DebateRecord | None = None

    # User / desk preferences
    # Restrict what option rights the desk should consider for new proposals.
    # "BOTH" (default) means no restriction.
    allowed_option_rights: str = "BOTH"   # "CALL" | "PUT" | "BOTH"

    # Restrict which option *structures* the Strategist may propose.
    # Values:
    # - "SINGLE"        (1-leg long/short option)
    # - "VERTICAL"      (2-leg same right+expiry spread)
    # - "IRON_CONDOR"   (4 legs: 2 puts + 2 calls, same expiry)
    # - "CALENDAR"      (2 legs, same right+strike, different expiry)
    # "ALL" (default) means no restriction.
    allowed_option_structures: list[str] = Field(default_factory=lambda: ["SINGLE"])

    # Risk
    risk:             RiskMetrics = Field(default_factory=RiskMetrics)

    # Agent decisions (written by each agent, read by supervisor)
    analyst_decision:   AgentDecision = AgentDecision.HOLD
    stock_decision:     AgentDecision = AgentDecision.HOLD
    sentiment_decision: AgentDecision = AgentDecision.HOLD
    risk_decision:      AgentDecision = AgentDecision.HOLD
    trader_decision:    AgentDecision = AgentDecision.HOLD

    # Agent confidence scores (0.0–1.0)
    analyst_confidence:    float = 0.0
    stock_confidence:      float = 0.0
    sentiment_confidence:  float = 0.0
    risk_confidence:       float = 0.0
    strategy_confidence:   float = 0.0

    # Derived IV analytics (populated by ingest_data via features.py)
    iv_atm:       float = 0.0   # front-month ATM IV
    iv_skew_ratio: float = 1.0  # put_25d / call_25d (>1 = put fear premium)
    iv_regime:    str = "UNKNOWN"  # LOW / NORMAL / ELEVATED / EXTREME
    iv_term_structure: dict[str, float] = Field(default_factory=dict)

    # Sentiment themes from analyst (list of short strings)
    sentiment_themes:  list[str] = []
    sentiment_tail_risks: list[str] = []

    # ── Tier-1: Movement Tracker signals (updated every ~30s, no LLM) ──────────
    movement_signal:    float = 0.0    # [-1.0 bearish … +1.0 bullish] composite
    movement_anomaly:   bool  = False   # True when any signal exceeds threshold
    price_change_pct:   float = 0.0    # % change from prev close
    momentum:           float = 0.0    # EMA9 - EMA21 normalised
    vol_ratio:          float = 1.0    # recent vol / 10d avg vol
    movement_updated:   datetime | None = None

    # ── Tier-1: Sentiment Monitor (LLM synthesis over Tier-2 structured news) ─
    sentiment_monitor_score: float = 0.0  # desk score [-1..1] from structured pipeline
    sentiment_monitor_confidence: float = 0.0  # 0..1 from last monitor LLM (or structured fallback)
    sentiment_monitor_reasoning: str = ""  # short — last cycle (UI / debug)
    # llm_structured | fallback_structured | none
    sentiment_monitor_source: str = "none"

    # ── Desk context (Tier-1 + ingest_data; multi-horizon, not HFT) ────────────
    # Age of the newest headline in the 1h window (minutes); None if no headlines.
    news_newest_age_minutes: float | None = None
    # fresh (<15m) | moderate (15–60m) | stale (>60m) | none — drives chase vs risk-first policy
    news_timing_regime: str = "none"
    # Non-news bias from movement/momentum/volume [-1..1]; usable when headlines are stale/absent
    market_bias_score: float = 0.0

    # ── Tier-2: Fundamentals snapshot (refreshed every 4h) ──────────────────────
    fundamentals:       dict[str, Any] = Field(default_factory=dict)
    fundamentals_updated: datetime | None = None
    # Set True when yfinance snapshot fingerprint changes (Tier-2); cleared after T3 consumes it
    fundamentals_material_change: bool = False

    # ── Tier-2: AI-processed news (refreshed every 5min) ─────────────────────
    news_impact_map:    dict[str, Any] = Field(default_factory=dict)
    # Cross-stock impact map: ticker → {total_impact, article_count, relationships}
    # Populated by the news processor loop in tiers.py

    # ── Tier 3 ingest (filled in ingest_data_node; Tier-2 LLM digests for prompts) ─
    tier3_structured_digests: list[str] = Field(default_factory=list)

    # ── Tier metadata (managed by tiers.py) ─────────────────────────────────────
    tier1_active:       bool  = False
    tier2_active:       bool  = False
    tier3_active:       bool  = False
    last_tier3_run:     datetime | None = None
    tier3_trigger:      str   = "manual"   # "manual" | "sentiment" | "movement" | "scanner"

    # ── Tier-3 focus ticker (lock semantics) ───────────────────────────────────
    # UI may switch `ticker` freely; Tier-3 will run on `tier3_focus_ticker` when set.
    tier3_focus_ticker: str | None = None
    tier3_focus_locked: bool = False
    tier3_focus_lock_reason: str = ""
    tier3_focus_locked_at: datetime | None = None

    # Bull/Bear researcher outputs (populated in T3, read by Strategist)
    bull_argument:      str   = ""
    bear_argument:      str   = ""
    bull_conviction:    int   = 0    # 1-10
    bear_conviction:    int   = 0    # 1-10

    # ── Trading mode ─────────────────────────────────────────────────────────
    trading_mode:              str = "advisory"   # "advisory" | "autopilot"
    pending_recommendations:   list[Recommendation] = []

    # XAI audit trail
    reasoning_log:    list[ReasoningEntry] = []

    # Control flags
    circuit_breaker_tripped: bool = False
    kill_switch_active:       bool = False

    class Config:
        arbitrary_types_allowed = True
