"""
Agent configuration – model routing, API keys, and feature flags.

Default LLM stack: local llama.cpp (see agents/llm_local.py, agents/llm_providers.py).
Optional cloud: set OPENROUTER_ENABLED=true and use OpenRouter model IDs in MODELS.*.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

# Load .env from the project root (two levels up from this file)
try:
    from dotenv import load_dotenv
    _env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment


@dataclass
class ModelConfig:
    testing:    str
    production: str

    @property
    def active(self) -> str:
        env = os.getenv("TRADING_ENV", "testing").lower()
        return self.production if env == "production" else self.testing


@dataclass
class AgentModels:
    """
    Model routing per agent (used when OPENROUTER_ENABLED=true; slugs are OpenRouter routes).
    When using local llama.cpp only, these strings are ignored for inference — keep them
    for documentation / future cloud toggle.

    Example OpenRouter free-tier slugs (TRADING_ENV=testing):
      - meta-llama/llama-4-scout:free      — 109B MoE, strong reasoning
      - meta-llama/llama-4-maverick:free   — 400B MoE, best free quality
      - google/gemini-2.0-flash-thinking-exp:free — reasoning model
      - deepseek/deepseek-r1:free          — strong math/analysis
      - qwen/qwq-32b:free                  — good quantitative reasoning
    """
    desk_head: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "meta-llama/llama-4-maverick:free",
        production = "anthropic/claude-3.5-sonnet",
    ))
    options_specialist: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "google/gemini-2.0-flash-thinking-exp:free",
        production = "anthropic/claude-3.5-sonnet",
    ))
    sentiment_analyst: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "meta-llama/llama-4-scout:free",
        production = "google/gemini-1.5-flash",
    ))
    risk_manager: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "deepseek/deepseek-r1:free",
        production = "openai/gpt-4o",
    ))
    trader_agent: ModelConfig = field(default_factory=lambda: ModelConfig(
        # Trader is now deterministic — model only used for fallback logging.
        testing    = "meta-llama/llama-4-scout:free",
        production = "anthropic/claude-3.5-sonnet",
    ))
    bull_researcher: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "meta-llama/llama-4-scout:free",
        production = "anthropic/claude-3.5-sonnet",
    ))
    bear_researcher: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "qwen/qwq-32b:free",
        production = "anthropic/claude-3.5-sonnet",
    ))
    strategist: ModelConfig = field(default_factory=lambda: ModelConfig(
        testing    = "meta-llama/llama-4-maverick:free",
        production = "anthropic/claude-3.5-sonnet",
    ))


MODELS = AgentModels()


def llm_models_snapshot() -> dict[str, str]:
    """
    Active model id per LangGraph role (matches ``AgentModels`` fields).
    Used by ``/state`` / ``/agent_status`` so the UI can show the same IDs as the backend.
    """
    m = MODELS
    return {
        "desk_head": m.desk_head.active,
        "options_specialist": m.options_specialist.active,
        "sentiment_analyst": m.sentiment_analyst.active,
        "risk_manager": m.risk_manager.active,
        "trader_agent": m.trader_agent.active,
        "bull_researcher": m.bull_researcher.active,
        "bear_researcher": m.bear_researcher.active,
        "strategist": m.strategist.active,
    }


# Cloud LLM (OpenRouter) — off by default; implementation in agents/llm_openrouter.py
OPENROUTER_ENABLED = os.getenv("OPENROUTER_ENABLED", "false").lower() in (
    "1", "true", "yes",
)

# Local LLM (OpenAI-compatible HTTP: llama.cpp, vLLM, etc.)
# ---------------------------------------------------------------------------
# Per-agent hosts: LLAMA_LOCAL_BASE_URL plus LLAMA_LOCAL_BASE_URL_OPTIONS_SPECIALIST,
# _SENTIMENT_ANALYST, _STRATEGIST, … (see agents/llm_local._KNOWN_AGENT_ROLES).
#
# Model id for POST /v1/chat/completions JSON: set LLAMA_LOCAL_MODEL to the name your
# server advertises in GET /v1/models (often the id you passed with -m). Example:
#   LLAMA_LOCAL_MODEL=qwen-14b-q4
# The same model on every port → still one env var; no per-port model name required.
#
# The GGUF / weights *path* (e.g. ~/models/qwen-14b-q4) is NOT read here — configure it
# only when launching each server, e.g. llama-server --model ~/models/qwen-14b-q4/....gguf -p 8001
# ---------------------------------------------------------------------------

# Broker
BROKER              = os.getenv("BROKER", "alpaca")        # alpaca | ibkr | lime
ALPACA_API_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL     = os.getenv("ALPACA_BASE_URL",
                        "https://paper-api.alpaca.markets")  # trading / orders
ALPACA_DATA_URL     = os.getenv("ALPACA_DATA_URL",
                        "https://data.alpaca.markets")       # market data (options, quotes)
# Equity bars/snapshots: iex (default, no SIP subscription) | sip | delayed_sip
ALPACA_STOCK_DATA_FEED = os.getenv("ALPACA_STOCK_DATA_FEED", "iex").strip().lower()

# Stock OHLC + quotes (optional primary source for charts / quote strip)
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()

# News feeds
BENZINGA_API_KEY    = os.getenv("BENZINGA_API_KEY", "")
REUTERS_MRN_TOKEN   = os.getenv("REUTERS_MRN_TOKEN", "")
# Master switch: Benzinga + yfinance ingestion (api_server + paper_trader background tasks)
# Falls back to yfinance if no Benzinga key. Synthetic headlines are off by default.
ENABLE_NEWS_FEED = os.getenv("ENABLE_NEWS_FEED", "true").lower() in ("1", "true", "yes")
ENABLE_SYNTHETIC_NEWS = os.getenv("ENABLE_SYNTHETIC_NEWS", "false").lower() in (
    "1", "true", "yes",
)

# Tier-3 SentimentAnalyst: recent-headline window (hours). Must overlap the feed you keep
# (e.g. dry run `--news-hours`). Default 6 matches multi-horizon use; override with .env or FirmState.
SENTIMENT_HEADLINE_LOOKBACK_HOURS = float(
    os.getenv("SENTIMENT_HEADLINE_LOOKBACK_HOURS", "6")
)

# Databases
QUESTDB_HOST        = os.getenv("QUESTDB_HOST", "localhost")
QUESTDB_PORT        = int(os.getenv("QUESTDB_PORT", "8812"))
REDIS_URL           = os.getenv("REDIS_URL", "redis://localhost:6379")

# Risk parameters
MAX_DAILY_DRAWDOWN  = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.05"))  # 5%
MAX_POSITION_PCT    = float(os.getenv("MAX_POSITION_PCT",   "0.02"))  # 2%

# Feature flags
ENABLE_ADVERSARIAL_DEBATE = os.getenv("ENABLE_ADVERSARIAL_DEBATE", "true").lower() == "true"
ENABLE_SEMANTIC_CACHE     = os.getenv("ENABLE_SEMANTIC_CACHE",     "true").lower() == "true"
DEBATE_ROUNDS             = int(os.getenv("DEBATE_ROUNDS", "3"))

# News prioritization (reduce LLM pressure)
# ---------------------------------------------------------------------------
# SentimentAnalyst will rank headlines by impact/urgency/recency and only send
# the top-K to the LLM (still enough for reasoning; avoids huge prompt payloads).
SENTIMENT_ANALYST_TOPK_HEADLINES = int(os.getenv("SENTIMENT_ANALYST_TOPK_HEADLINES", "25"))

# Tier-2 NewsProcessor LLM batch: only process the most important *new* headlines
# per cycle (T0/T1 first). Remaining items can wait for future cycles.
NEWS_PROCESSOR_MAX_ARTICLES_PER_CYCLE = int(
    os.getenv("NEWS_PROCESSOR_MAX_ARTICLES_PER_CYCLE", "30")
)
# Minimum impact_score required to be considered for LLM processing unless urgency is T0.
NEWS_PROCESSOR_MIN_IMPACT = float(os.getenv("NEWS_PROCESSOR_MIN_IMPACT", "0.55"))

# NewsPriorityQueue: a shared backlog of ingested news ordered by FinBERT/heuristic
# priority. Both the Tier-2 NewsProcessor ("news analyst") and Tier-3 SentimentAnalyst
# drain from the top each cycle, so *all* news is eventually processed (highest first).
NEWS_QUEUE_MAX_SIZE  = int(os.getenv("NEWS_QUEUE_MAX_SIZE", "10000"))
NEWS_QUEUE_TTL_HOURS = float(os.getenv("NEWS_QUEUE_TTL_HOURS", "24"))

# MLflow experiment tracking (optional). Set MLFLOW_TRACKING_URI to enable, e.g. http://127.0.0.1:5000
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "").strip()
MLFLOW_EXPERIMENT_NAME = (os.getenv("MLFLOW_EXPERIMENT_NAME", "atlas-agents") or "atlas-agents").strip()
MLFLOW_DISABLED = os.getenv("MLFLOW_DISABLED", "").lower() in ("1", "true", "yes")
MLFLOW_ENABLED = bool(MLFLOW_TRACKING_URI) and not MLFLOW_DISABLED
