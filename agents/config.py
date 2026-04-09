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

# Cloud LLM (OpenRouter) — off by default; implementation in agents/llm_openrouter.py
OPENROUTER_ENABLED = os.getenv("OPENROUTER_ENABLED", "false").lower() in (
    "1", "true", "yes",
)

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
