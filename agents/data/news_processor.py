"""
AI News Processor — Tier 2 periodic pipeline
=============================================

Every 5 minutes:
  1. Collect unprocessed headlines from firm_state.news_feed
  2. Batch-send to LLM for deep analysis:
     - Sentiment score + confidence
     - Affected tickers (beyond the explicitly tagged ones)
     - Cross-stock impact chain (e.g. NVDA earnings → MSFT, META, GOOGL affected)
     - Category classification (earnings, macro, M&A, guidance, etc.)
     - Market impact magnitude (1-5)
  3. Store each processed article as a JSONL row on disk
  4. Write cross-stock impact map to firm_state for other agents

The LLM output is structured JSON — cached per headline SHA to avoid reprocessing.
Storage: logs/news/processed_YYYYMMDD.jsonl (one line per article, rotated daily)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ── Storage ───────────────────────────────────────────────────────────────────

NEWS_STORE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "logs" / "news"
NEWS_STORE_DIR.mkdir(parents=True, exist_ok=True)


class ProcessedArticle(BaseModel):
    """Single news article after AI enrichment — stored as JSONL on disk."""
    id:               str                  # SHA-1 of headline (first 16 hex)
    headline:         str
    source:           str   = ""
    url:              str   = ""
    summary:          str   = ""
    published_at:     datetime
    fetched_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Original tags
    original_tickers: list[str] = []       # tickers tagged by the news source

    # AI-enriched fields
    sentiment:        float = 0.0          # [-1.0 … +1.0]
    confidence:       float = 0.0          # [0.0 … 1.0]
    category:         str   = "general"    # earnings|macro|deal|guidance|regulatory|product|analyst|general
    impact_magnitude: int   = 1            # 1 (trivial) – 5 (market-moving)

    # Cross-stock impact: which other tickers are affected and how
    affected_tickers: list[dict[str, Any]] = Field(default_factory=list)
    # Each: {"ticker": "MSFT", "impact": 0.3, "relationship": "major customer of NVDA"}

    # Themes extracted
    themes:           list[str] = []
    tail_risks:       list[str] = []

    # Processing metadata
    processed:        bool  = False
    llm_model:        str   = ""
    processing_time_ms: int = 0


class CrossStockImpact(BaseModel):
    """Aggregated impact of news on a particular ticker from all recent articles."""
    ticker:           str
    total_impact:     float = 0.0   # sum of signed impacts
    article_count:    int   = 0
    relationships:    list[str] = []
    last_updated:     datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── In-memory state ───────────────────────────────────────────────────────────

_processed_ids: set[str] = set()  # IDs already processed this session
_processed_articles: list[ProcessedArticle] = []  # in-memory ring buffer
_cross_impact_map: dict[str, CrossStockImpact] = {}  # ticker → aggregated impact

MAX_MEMORY_ARTICLES = 500


def _article_id(headline: str) -> str:
    return hashlib.sha1(headline.lower().strip().encode()).hexdigest()[:16]


# ── Disk persistence ──────────────────────────────────────────────────────────

def _today_file() -> pathlib.Path:
    return NEWS_STORE_DIR / f"processed_{datetime.now().strftime('%Y%m%d')}.jsonl"


def _append_to_disk(article: ProcessedArticle) -> None:
    try:
        with open(_today_file(), "a", encoding="utf-8") as f:
            f.write(article.model_dump_json() + "\n")
    except Exception as exc:
        log.debug("Failed to persist article %s: %s", article.id, exc)


def load_today_articles() -> list[ProcessedArticle]:
    """Load today's articles from disk (for warm restart)."""
    path = _today_file()
    articles = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            articles.append(ProcessedArticle.model_validate_json(line))
                        except Exception:
                            pass
        except Exception as exc:
            log.warning("Failed to load processed news from %s: %s", path, exc)
    return articles


def _warm_start() -> None:
    """Load today's processed articles into memory on startup."""
    global _processed_articles, _processed_ids
    articles = load_today_articles()
    _processed_articles = articles[-MAX_MEMORY_ARTICLES:]
    _processed_ids = {a.id for a in _processed_articles}
    if articles:
        log.info("News processor warm start: loaded %d articles from disk", len(articles))

_warm_start()


# ── LLM Processing ───────────────────────────────────────────────────────────

NEWS_ANALYSIS_PROMPT = """You are a financial news analyst for a trading desk monitoring the S&P 500.
Analyse the following batch of news headlines. For EACH headline, provide:

1. **sentiment**: -1.0 (very bearish) to +1.0 (very bullish) for the market
2. **confidence**: 0.0-1.0 how certain you are about the sentiment
3. **category**: one of: earnings, macro, deal, guidance, regulatory, product, analyst, management, general
4. **impact_magnitude**: 1 (trivial) to 5 (market-moving)
5. **affected_tickers**: List of S&P 500 tickers affected BEYOND the ones explicitly mentioned.
   For each, include the ticker, signed impact (-1.0 to +1.0), and the relationship.
   Example: NVDA earnings beat → affected_tickers: [
     {{"ticker": "MSFT", "impact": 0.3, "relationship": "major AI compute customer"}},
     {{"ticker": "AMD", "impact": -0.2, "relationship": "competitor losing share"}}
   ]
6. **themes**: 1-3 short theme tags
7. **tail_risks**: any specific risks detected (empty list if none)

IMPORTANT for cross-stock impacts:
- Think about supply chains: if a chip maker reports strong demand, who benefits?
- Think about competitors: if one company gains, who might lose?
- Think about customers: if a supplier has issues, who is affected?
- Think about sector effects: macro news (Fed, CPI) affects broad sectors
- Only include S&P 500 tickers in affected_tickers

Our trading universe (top 50 S&P 500): {universe}

Output STRICT JSON array, one object per headline:
[
  {{
    "headline_idx": 0,
    "sentiment": 0.4,
    "confidence": 0.8,
    "category": "earnings",
    "impact_magnitude": 4,
    "affected_tickers": [{{"ticker": "MSFT", "impact": 0.3, "relationship": "major customer"}}],
    "themes": ["AI demand", "chip shortage"],
    "tail_risks": []
  }},
  ...
]

Only output the JSON array, nothing else."""


def _build_headlines_payload(articles: list[ProcessedArticle]) -> list[dict]:
    """Build the payload for the LLM."""
    return [
        {
            "idx": i,
            "headline": a.headline,
            "source": a.source,
            "tickers": a.original_tickers,
            "published": a.published_at.isoformat() if a.published_at else "",
            "summary": a.summary[:200] if a.summary else "",
        }
        for i, a in enumerate(articles)
    ]


def process_news_batch_sync(
    articles: list[ProcessedArticle],
    universe: list[str],
) -> list[ProcessedArticle]:
    """
    Send a batch of articles to the LLM for deep analysis.
    Returns the same articles with AI-enriched fields populated.
    Synchronous — call via asyncio.to_thread.
    """
    import time as _time

    if not articles:
        return []

    try:
        from agents.llm_providers import chat_llm
        from agents.llm_retry import invoke_llm
        from agents.config import MODELS
        from langchain_core.messages import HumanMessage, SystemMessage
    except ImportError as exc:
        log.warning("Cannot import LLM stack for news processing: %s", exc)
        return articles

    t0 = _time.monotonic()

    universe_str = ", ".join(universe[:50])
    payload = _build_headlines_payload(articles)

    messages = [
        SystemMessage(content=NEWS_ANALYSIS_PROMPT.format(universe=universe_str)),
        HumanMessage(content=json.dumps(payload, indent=1)),
    ]

    try:
        llm = chat_llm(
            MODELS.sentiment_analyst.active,
            agent_role="news_processor",
            temperature=0.0,
        )
        response = invoke_llm(llm, messages)
        raw = response.content.strip()

        # Extract & parse JSON array robustly (handle fences + trailing junk).
        # Models sometimes append commentary after the closing bracket, which breaks json.loads.
        if "```" in raw:
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
        raw = raw.strip()
        if "[" in raw:
            raw = raw[raw.find("[") :].lstrip()
        dec = json.JSONDecoder()
        results, _end = dec.raw_decode(raw)
        if not isinstance(results, list):
            results = [results]

        elapsed_ms = int((_time.monotonic() - t0) * 1000)

        for entry in results:
            idx = entry.get("headline_idx", -1)
            if 0 <= idx < len(articles):
                a = articles[idx]
                a.sentiment        = float(entry.get("sentiment", 0))
                a.confidence       = float(entry.get("confidence", 0))
                a.category         = entry.get("category", "general")
                a.impact_magnitude = int(entry.get("impact_magnitude", 1))
                a.affected_tickers = entry.get("affected_tickers", [])
                a.themes           = entry.get("themes", [])
                a.tail_risks       = entry.get("tail_risks", [])
                a.processed        = True
                a.llm_model        = MODELS.sentiment_analyst.active
                a.processing_time_ms = elapsed_ms

        log.info(
            "News processor: analysed %d articles in %dms",
            len(articles), elapsed_ms,
        )

    except json.JSONDecodeError as exc:
        log.warning("News processor JSON parse error: %s", exc)
    except Exception as exc:
        log.warning("News processor LLM error: %s", exc)

    return articles


# ── Main processing entry point ───────────────────────────────────────────────

def process_new_headlines(
    news_feed: list,
    universe: list[str],
) -> tuple[list[ProcessedArticle], dict[str, CrossStockImpact]]:
    """
    Process any unprocessed headlines from the news feed.
    Returns (newly_processed, updated_cross_impact_map).

    Called by the tier-2 loop every 5 minutes.
    """
    global _processed_articles, _cross_impact_map

    # Find unprocessed articles
    new_articles: list[ProcessedArticle] = []
    for item in news_feed:
        aid = _article_id(item.headline)
        if aid in _processed_ids:
            continue
        _processed_ids.add(aid)
        new_articles.append(ProcessedArticle(
            id               = aid,
            headline         = item.headline,
            source           = getattr(item, "source", ""),
            url              = getattr(item, "url", ""),
            summary          = getattr(item, "summary", ""),
            published_at     = item.published_at,
            original_tickers = getattr(item, "tickers", []),
            sentiment        = getattr(item, "sentiment", 0.0),
        ))

    if not new_articles:
        return [], _cross_impact_map

    # Process through LLM (batches of 15)
    BATCH_SIZE = 15
    processed: list[ProcessedArticle] = []

    for i in range(0, len(new_articles), BATCH_SIZE):
        batch = new_articles[i : i + BATCH_SIZE]
        result = process_news_batch_sync(batch, universe)
        processed.extend(result)

    # Persist to disk and memory
    for a in processed:
        _append_to_disk(a)
        _processed_articles.append(a)

    # Trim memory buffer
    if len(_processed_articles) > MAX_MEMORY_ARTICLES:
        _processed_articles = _processed_articles[-MAX_MEMORY_ARTICLES:]

    # Rebuild cross-stock impact map from recent processed articles
    _rebuild_cross_impact_map()

    return processed, _cross_impact_map


def _rebuild_cross_impact_map() -> None:
    """Rebuild the cross-stock impact map from recent in-memory articles (last 200)."""
    global _cross_impact_map

    recent = _processed_articles[-200:]
    impact_map: dict[str, CrossStockImpact] = {}

    for article in recent:
        if not article.processed:
            continue
        for affected in article.affected_tickers:
            ticker = affected.get("ticker", "")
            if not ticker:
                continue
            if ticker not in impact_map:
                impact_map[ticker] = CrossStockImpact(ticker=ticker)

            entry = impact_map[ticker]
            entry.total_impact += float(affected.get("impact", 0))
            entry.article_count += 1
            rel = affected.get("relationship", "")
            if rel and rel not in entry.relationships:
                entry.relationships.append(rel)
                if len(entry.relationships) > 10:
                    entry.relationships = entry.relationships[-10:]
            entry.last_updated = datetime.now(timezone.utc)

    _cross_impact_map = impact_map


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_processed_articles(limit: int = 50) -> list[dict]:
    """Return recent processed articles as dicts for the API."""
    return [
        a.model_dump(mode="json") for a in _processed_articles[-limit:]
    ]


def get_impact_for_ticker(ticker: str) -> dict | None:
    """Return cross-stock impact data for a specific ticker."""
    entry = _cross_impact_map.get(ticker.upper())
    if entry:
        return entry.model_dump(mode="json")
    return None


def get_all_impacts() -> dict[str, dict]:
    """Return the full cross-stock impact map."""
    return {
        k: v.model_dump(mode="json")
        for k, v in sorted(_cross_impact_map.items(), key=lambda x: abs(x[1].total_impact), reverse=True)
    }


def get_articles_affecting_ticker(ticker: str, limit: int = 20) -> list[dict]:
    """Return articles that mention or affect a given ticker."""
    ticker_upper = ticker.upper()
    matching = []
    for a in reversed(_processed_articles):
        if ticker_upper in a.original_tickers:
            matching.append(a)
        elif any(af.get("ticker") == ticker_upper for af in a.affected_tickers):
            matching.append(a)
        if len(matching) >= limit:
            break
    return [a.model_dump(mode="json") for a in matching]
