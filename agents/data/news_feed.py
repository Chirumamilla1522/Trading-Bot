"""
News Ingestion Pipeline

Ticker priority tiers (fetched in this order each cycle):
  1. INDICES    — SPY, QQQ, IWM, DIA, VIX proxies (macro pulse)
  2. PORTFOLIO  — tickers from open stock/option positions
  3. ACTIVE     — the current firm_state.ticker being analysed
  4. TOP_STOCKS — top 25 S&P 500 names by market cap

Source priority:
  1. Benzinga REST API  (BENZINGA_API_KEY set)   — structured, fast, ticker-tagged
  2. yfinance           (no API key)              — Yahoo Finance, TTL-cached per tier
  3. Synthetic          (optional, ENABLE_SYNTHETIC_NEWS=true) — dev / offline only

Importance categories detected from headline text:
  HIGH  — earnings, M&A / deals, macro events (Fed/CPI/GDP), FDA/regulatory,
           guidance revision, dividend cuts / specials, bankruptcy / default,
           management change (CEO/CFO departure), activist campaigns
  NORMAL — analyst ratings, product launches, partnerships, sector trends
  LOW   — general company / index commentary

Deduplication: SHA-1 of the lowercased headline, global across all tiers/sources.
Env: BENZINGA_PAGE_SIZE (max 100), BENZINGA_GENERAL_PAGE_SIZE, BENZINGA_GENERAL_EXTRA_PAGES (extra API pages),
YF_NEWS_TTL (per-ticker yfinance cache seconds), BENZINGA_POLL_S (seconds between cycles).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

import httpx

from agents.config import ENABLE_NEWS_FEED, ENABLE_SYNTHETIC_NEWS
from agents.state import NewsItem

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BENZINGA_API_KEY = os.getenv("BENZINGA_API_KEY", "").strip()   # strip accidental whitespace
FINBERT_ENABLED  = os.getenv("FINBERT_ENABLED", "false").lower() == "true"
# More aggressive defaults (override via env in production if needed).
# - BENZINGA_POLL_S: how often we run a full cycle across tiers
# - YF_NEWS_TTL:     per-ticker TTL for yfinance cache (lower = fresher + more requests)
BENZINGA_POLL_S  = int(os.getenv("BENZINGA_POLL_S", "10"))
YF_NEWS_TTL      = int(os.getenv("YF_NEWS_TTL", "30"))

# Benzinga: API allows up to pageSize=100 per request
BENZINGA_PAGE_SIZE = max(10, min(100, int(os.getenv("BENZINGA_PAGE_SIZE", "100"))))
BENZINGA_GENERAL_PAGE_SIZE = max(10, min(100, int(os.getenv("BENZINGA_GENERAL_PAGE_SIZE", "100"))))
# Extra pagination pages (0-based) for the general feed — more Benzinga headlines per cycle
BENZINGA_GENERAL_EXTRA_PAGES = max(0, min(3, int(os.getenv("BENZINGA_GENERAL_EXTRA_PAGES", "3"))))

# Prefer Benzinga heavily; yfinance can be "always" or "fallback" (only when Benzinga is unavailable).
# This is useful when you want the majority of headlines to come from Benzinga.
YF_MODE = os.getenv("YF_NEWS_MODE", "fallback").strip().lower()  # "always" | "fallback"

# When BENZINGA_POLL_S is set very low (e.g., 1s), we still need to avoid hammering
# the Benzinga API. These are per-endpoint minimum cadences.
BENZINGA_GENERAL_MIN_S = float(os.getenv("BENZINGA_GENERAL_MIN_S", "6"))  # general feed (paginated)
BENZINGA_TIER_MIN_S    = float(os.getenv("BENZINGA_TIER_MIN_S", "2"))     # ticker-filtered feed per tier group

# ── Ticker tiers (order = fetch priority) ─────────────────────────────────────

TIER_INDICES = ["SPY", "QQQ", "IWM", "DIA", "VXX"]          # macro pulse

# Use the canonical top-50 list from sp500.py (same universe the scanner uses)
from agents.data.sp500 import SP500_TOP50 as _SP500_TOP50
TIER_TOP_STOCKS = [t for t in _SP500_TOP50 if t not in TIER_INDICES]


# ── Category detection ────────────────────────────────────────────────────────

# (regex_pattern, category, priority)
_CAT_RULES: list[tuple[re.Pattern, str, str]] = []

def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)

_CAT_RULES = [
    # ── HIGH priority ──────────────────────────────────────────────────────────
    # Earnings
    (_r(r"\bearning[s]?\b|\bEPS\b|\bquarterly\b|\bresult[s]?\b|beat[s]? estimate|miss(es)? estimate|revenue (beat|miss|surges|falls)|profit (falls|drops|surges)|Q[1-4]\s*\d{4}"), "earnings", "HIGH"),
    # M&A / Deals
    (_r(r"\bacquir|acquisition|merger|takeover|buyout|bid for|acquires|deal worth|strategic deal|\bIPO\b|\bspin.?off\b"), "deal", "HIGH"),
    # Macro / Fed
    (_r(r"\bFed\b|\bFOMC\b|\bPowell\b|\brate (hike|cut|decision|hold)\b|\binterest rate|\bCPI\b|\bPCE\b|\bGDP\b|\bjobs report|\bpayrolls|\binflation (rises|falls|surges|drops)|\brecession"), "macro", "HIGH"),
    # FDA / Regulatory
    (_r(r"\bFDA\b|\bapproval\b|\bapproved\b|\brejected\b|\brecall\b|\bsanction[s]?\b|\binvestigation\b|\bDOJ\b|\bSEC charges|\bfine[s]?\b"), "regulatory", "HIGH"),
    # Guidance revision
    (_r(r"raise[sd]? guidance|lower[sd]? guidance|cuts guidance|raises (outlook|forecast)|lowers (outlook|forecast)|issues profit warning|updates outlook"), "guidance", "HIGH"),
    # Dividend
    (_r(r"special dividend|cuts dividend|dividend (increase|cut|elimination|suspension)|declares dividend"), "dividend", "HIGH"),
    # Bankruptcy / Default
    (_r(r"\bbankruptcy\b|\bchapter 11\b|\bdefault\b|\binsolvency\b|\bdebt restructur"), "bankruptcy", "HIGH"),
    # Management change
    (_r(r"\bCEO\b.{0,30}(resign|depart|step[ps]|replac|appoint|retire)|\bCFO\b.{0,30}(resign|depart|appoint)|new (CEO|CFO|COO|CTO|chairman)"), "management", "HIGH"),
    # Activist
    (_r(r"\bactivist\b|\bproxy (fight|battle|war)\b|\bstake in\b"), "activist", "HIGH"),
    # Stock split
    (_r(r"stock split|reverse.?split|share split"), "split", "HIGH"),

    # ── NORMAL priority ────────────────────────────────────────────────────────
    (_r(r"\bupgrad(e|ed)\b|\bdowngrad(e|ed)\b|\bprice target\b|\boverweight\b|\bunderweight\b|\boutperform\b|\bunderperform\b"), "analyst", "NORMAL"),
    (_r(r"partnership|collaboration|joint venture|strategic alliance|contract worth|wins contract"), "partnership", "NORMAL"),
    (_r(r"product launch|new product|unveils|announces new|\breleas(es|ed)\b"), "product", "NORMAL"),
    (_r(r"buyback|share repurchase|repurchases shares"), "buyback", "NORMAL"),
]

def _categorise(headline: str) -> tuple[str, str]:
    """Return (category, priority) for a headline using rule-based matching."""
    for pattern, category, priority in _CAT_RULES:
        if pattern.search(headline):
            return category, priority
    return "general", "NORMAL"


# ── FinBERT scorer ────────────────────────────────────────────────────────────

class FinBERTScorer:
    def __init__(self):
        self._pipeline = None
        if FINBERT_ENABLED:
            try:
                from transformers import pipeline as hf_pipeline
                self._pipeline = hf_pipeline(
                    "text-classification",
                    model="ProsusAI/finbert",
                    tokenizer="ProsusAI/finbert",
                    top_k=None,
                )
                log.info("FinBERT loaded (ProsusAI/finbert)")
            except ImportError:
                log.warning("transformers not installed – FinBERT disabled")
            except Exception as e:
                # If HF auth is misconfigured (or offline), avoid crashing the news feed.
                # We'll fall back to the keyword scorer instead.
                self._pipeline = None
                log.warning("FinBERT init failed — falling back to keywords: %s", e)

    def score(self, text: str) -> tuple[float, float]:
        if self._pipeline:
            try:
                results = self._pipeline(text[:512])
                scores = {r["label"]: r["score"] for r in results[0]}
                return (
                    scores.get("positive", 0.0) - scores.get("negative", 0.0),
                    max(scores.values()),
                )
            except Exception as e:
                log.debug("FinBERT error: %s", e)
        return _keyword_sentiment(text)


_finbert = FinBERTScorer()


# ── Keyword sentiment scorer ──────────────────────────────────────────────────

_BULLISH: list[tuple[str, float]] = [
    ("beat", 0.20), ("beats", 0.20), ("surges", 0.25), ("rally", 0.18),
    ("record high", 0.30), ("all-time high", 0.30), ("upgrade", 0.22),
    ("outperform", 0.20), ("strong earnings", 0.25), ("profit up", 0.20),
    ("revenue growth", 0.18), ("raised guidance", 0.25), ("raises outlook", 0.25),
    ("buyback", 0.15), ("dividend increase", 0.18), ("partnership", 0.10),
    ("acqui", 0.12), ("breakthrough", 0.18), ("approval", 0.15),
    ("bullish", 0.22), ("recovery", 0.15), ("rebound", 0.18),
    ("positive", 0.10), ("strong", 0.08), ("growth", 0.08),
    ("upside", 0.15), ("above estimate", 0.22), ("exceeds forecast", 0.22),
    ("new high", 0.20), ("jump", 0.15), ("soar", 0.20), ("gain", 0.10),
    ("buy", 0.08), ("overweight", 0.15), ("boosts", 0.12), ("beats estimate", 0.25),
]
_BEARISH: list[tuple[str, float]] = [
    ("miss", 0.20), ("misses", 0.20), ("plunge", 0.25), ("crash", 0.30),
    ("recall", 0.20), ("investigation", 0.20), ("lawsuit", 0.18),
    ("downgrade", 0.22), ("underperform", 0.20), ("weak earnings", 0.25),
    ("profit warning", 0.28), ("cuts guidance", 0.28), ("lowers outlook", 0.25),
    ("layoffs", 0.20), ("bankruptcy", 0.35), ("default", 0.30),
    ("bearish", 0.22), ("decline", 0.15), ("drop", 0.12), ("fall", 0.10),
    ("negative", 0.10), ("concern", 0.12), ("risk", 0.08), ("warning", 0.15),
    ("sell", 0.08), ("underweight", 0.15), ("below estimate", 0.22),
    ("misses forecast", 0.22), ("loss", 0.15), ("slowdown", 0.15),
    ("tariff", 0.12), ("inflation", 0.10), ("rate hike", 0.15),
    ("uncertainty", 0.10), ("fear", 0.15), ("volatility", 0.08),
    ("misses estimate", 0.25), ("guidance cut", 0.28),
]


def _keyword_sentiment(text: str) -> tuple[float, float]:
    lo = text.lower()
    bull = sum(w for kw, w in _BULLISH if kw in lo)
    bear = sum(w for kw, w in _BEARISH if kw in lo)
    total = bull + bear
    if total == 0:
        return 0.0, 0.3
    raw = (bull - bear) / max(total, 0.5)
    return max(-1.0, min(1.0, raw)), min(0.95, 0.4 + total * 0.4)


def _score(text: str) -> tuple[float, float]:
    return _finbert.score(text)


def _headline_hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:12]


def _parse_bz_datetime(*candidates: str | None) -> datetime:
    """
    Benzinga returns RFC 2822 dates, e.g. 'Fri, 03 Apr 2026 08:14:04 -0400'.
    datetime.fromisoformat cannot parse those — use email.utils.parsedate_to_datetime.
    """
    from email.utils import parsedate_to_datetime

    for raw in candidates:
        if not raw or not str(raw).strip():
            continue
        s = str(raw).strip()
        try:
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


# ── yfinance article parser ───────────────────────────────────────────────────

def _parse_yf_article(art: dict, query_ticker: str) -> tuple[str, str, datetime, list[str], str, str]:
    """
    Handle both old yfinance (flat) and new yfinance (nested under "content").
    Returns (title, publisher, published_at, related_tickers, summary, url).
    """
    body = art.get("content") or art

    title = body.get("title") or ""

    provider = body.get("provider") or body.get("publisher") or {}
    publisher = (
        provider.get("displayName") or provider.get("name")
        or body.get("publisher") or "Yahoo Finance"
    )

    pub_raw = body.get("pubDate") or body.get("displayTime") or ""
    pub_ts  = body.get("providerPublishTime") or body.get("providerPublishedTime") or 0
    try:
        if pub_raw:
            pub = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
        elif pub_ts:
            pub = datetime.fromtimestamp(int(pub_ts), tz=timezone.utc)
        else:
            pub = datetime.now(tz=timezone.utc)
    except Exception:
        pub = datetime.now(tz=timezone.utc)

    related: list[str] = []
    fin = body.get("finance") or {}
    for t in fin.get("stockTickers") or []:
        sym = t.get("symbol") or t.get("ticker") or ""
        if sym:
            related.append(sym)
    if not related:
        for t in art.get("relatedTickers") or []:
            if isinstance(t, str):
                related.append(t)
            elif isinstance(t, dict):
                related.append(t.get("ticker") or t.get("symbol") or "")
    if not related and query_ticker:
        related = [query_ticker]

    # Summary / abstract — yfinance nests content in several possible places
    summary = (
        body.get("summary") or body.get("description") or
        body.get("abstract") or body.get("snippet") or
        # Some versions nest under body.body or body.text
        body.get("body") or body.get("text") or ""
    )
    # Also try top-level art fields
    if not summary:
        summary = art.get("summary") or art.get("description") or ""
    if isinstance(summary, str) and len(summary) > 800:
        summary = summary[:800].rsplit(" ", 1)[0] + "…"

    # Canonical URL (try clickThroughUrl first — it resolves correctly)
    url = ""
    for key in ("clickThroughUrl", "canonicalUrl"):
        obj = body.get(key)
        if isinstance(obj, dict):
            url = obj.get("url", "")
        elif isinstance(obj, str):
            url = obj
        if url:
            break
    if not url:
        url = art.get("link") or art.get("url") or ""

    return title, publisher, pub, [t for t in related if t], summary, url


# ── yfinance fetch (per named tier) ──────────────────────────────────────────

# Cache keyed by ticker, storing (items, expire_monotonic)
_yf_cache: dict[str, tuple[list[NewsItem], float]] = {}


def _fetch_yf_tier(
    tickers: list[str],
    tier_label: str,
    seen: set[str],
    max_tickers: int = 50,
) -> list[NewsItem]:
    """
    Sync: fetch news for `tickers` via yfinance.
    Each ticker result is TTL-cached independently so repeated calls are cheap.
    Tickers whose cache has expired are fetched fresh; cached tickers just emit
    their stored items. This means all tickers are served each cycle but only
    expired ones hit the yfinance API.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.debug("yfinance not installed — skipping news tier %s", tier_label)
        return []

    items: list[NewsItem] = []
    now = time.monotonic()

    for ticker in list(dict.fromkeys(tickers))[:max_tickers]:
        cached, expire = _yf_cache.get(ticker, ([], 0.0))
        if now < expire:
            for item in cached:
                h = _headline_hash(item.headline)
                if h not in seen:
                    seen.add(h)
                    items.append(item)
            continue

        try:
            raw = yf.Ticker(ticker).news or []
        except Exception as e:
            log.debug("yfinance news %s: %s", ticker, e)
            raw = []

        fresh: list[NewsItem] = []
        for art in raw:
            title, publisher, pub, related, summary, url = _parse_yf_article(art, ticker)
            if not title:
                continue
            h = _headline_hash(title)
            sentiment, confidence = _score(title)
            category, priority = _categorise(title)
            ni = NewsItem(
                headline     = title,
                source       = publisher,
                published_at = pub,
                sentiment    = round(sentiment, 3),
                confidence   = round(confidence, 3),
                tickers      = related[:5],
                cached       = False,
                category     = category,
                priority     = priority,
                ticker_tier  = tier_label,
                summary      = summary[:600] if summary else "",
                url          = url,
            )
            fresh.append(ni)
            if h not in seen:
                seen.add(h)
                items.append(ni)

        _yf_cache[ticker] = (fresh, now + YF_NEWS_TTL)
        if fresh:
            log.debug("yf [%s] %s: %d articles", tier_label, ticker, len(fresh))

    return items


# ── Benzinga fetch ────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities for plain-text summary."""
    import re as _re
    text = _re.sub(r"<[^>]+>", " ", text)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')]:
        text = text.replace(entity, char)
    return " ".join(text.split())   # collapse whitespace


async def _fetch_benzinga_tier(
    tickers: list[str],
    tier_label: str,
    seen: set[str],
    client: httpx.AsyncClient,
    max_tickers: int = 50,
) -> tuple[list[NewsItem], bool]:
    """
    Returns (items, ok) where ok=False means a hard API error (auth/network).
    Empty results with ok=True means the tier had no new articles.
    """
    bz_url = "https://api.benzinga.com/api/v2/news"
    params = {
        "token":         BENZINGA_API_KEY,
        "tickers":       ",".join(tickers[:max_tickers]),
        "pageSize":      BENZINGA_PAGE_SIZE,
        "page":          0,
        "sort":          "created:desc",
        "displayOutput": "abstract",   # teaser + metadata, avoids large HTML bodies
    }
    # Without Accept: application/json, Benzinga returns XML (same HTTP 200)
    _JSON_HEADERS = {"Accept": "application/json"}
    try:
        resp = await client.get(bz_url, params=params, headers=_JSON_HEADERS, timeout=10.0)
        if resp.status_code in (401, 403):
            log.error(
                "Benzinga auth failed (HTTP %d). Check BENZINGA_API_KEY in .env "
                "(watch for leading/trailing spaces). Key prefix: %s…",
                resp.status_code,
                BENZINGA_API_KEY[:8] if BENZINGA_API_KEY else "(empty)",
            )
            return [], False
        resp.raise_for_status()
        articles = resp.json()
        if not isinstance(articles, list):
            log.warning("Benzinga [%s] unexpected response type: %s — raw: %s",
                        tier_label, type(articles).__name__, str(articles)[:200])
            return [], True   # not a hard error, just no data
    except Exception as e:
        log.warning("Benzinga [%s] fetch error: %s", tier_label, e)
        return [], False

    items: list[NewsItem] = []
    for art in articles:
        title = (art.get("title") or "").strip()
        if not title:
            continue
        h = _headline_hash(title)
        if h in seen:
            continue
        seen.add(h)
        sentiment, confidence = _score(title)
        category, priority = _categorise(title)

        pub = _parse_bz_datetime(art.get("created"), art.get("updated"))

        # Extract summary: teaser first (clean plain text), then strip HTML body
        bz_teaser = (art.get("teaser") or "").strip()
        bz_body   = (art.get("body")   or "").strip()
        if bz_body:
            bz_body = _strip_html(bz_body)
        bz_summary = bz_teaser or bz_body
        if len(bz_summary) > 800:
            bz_summary = bz_summary[:800].rsplit(" ", 1)[0] + "…"

        # Tickers from stocks array (symbol field)
        bz_tickers = []
        for stk in art.get("stocks") or []:
            sym = stk.get("name") or stk.get("symbol") or ""
            if sym:
                bz_tickers.append(sym.upper())

        items.append(NewsItem(
            headline     = title,
            source       = "Benzinga",
            published_at = pub,
            sentiment    = round(sentiment, 3),
            confidence   = round(confidence, 3),
            tickers      = bz_tickers[:6],
            cached       = False,
            category     = category,
            priority     = priority,
            ticker_tier  = tier_label,
            summary      = bz_summary,
            url          = art.get("url") or art.get("link") or "",
        ))

    if items:
        log.info("Benzinga [%s] %d new articles (tickers: %s)",
                 tier_label, len(items), ",".join(tickers[:5]))
    return items, True


async def _fetch_benzinga_general(
    seen: set[str],
    client: httpx.AsyncClient,
) -> tuple[list[NewsItem], bool]:
    """
    Fetch the latest Benzinga headlines with no ticker filter — broad market news.
    Called every cycle when the API key is valid. Uses pagination for volume.
    """
    bz_url = "https://api.benzinga.com/api/v2/news"
    _JSON_HEADERS = {"Accept": "application/json"}
    items: list[NewsItem] = []
    total_raw = 0

    for page in range(BENZINGA_GENERAL_EXTRA_PAGES + 1):
        params = {
            "token":         BENZINGA_API_KEY,
            "pageSize":      BENZINGA_GENERAL_PAGE_SIZE,
            "page":          page,
            "sort":          "created:desc",
            "displayOutput": "abstract",
        }
        try:
            resp = await client.get(bz_url, params=params, headers=_JSON_HEADERS, timeout=15.0)
            if resp.status_code in (401, 403):
                log.error("Benzinga general feed: auth failed (HTTP %d).", resp.status_code)
                return [], False
            resp.raise_for_status()
            articles = resp.json()
            if not isinstance(articles, list):
                break
            if not articles:
                break   # no rows on this page — stop (later pages won't help)
        except Exception as e:
            log.warning("Benzinga general feed page %d error: %s", page, e)
            return ([], False) if page == 0 else (items, True)

        total_raw += len(articles)
        for art in articles:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            h = _headline_hash(title)
            if h in seen:
                continue
            seen.add(h)
            sentiment, confidence = _score(title)
            category, priority = _categorise(title)
            pub = _parse_bz_datetime(art.get("created"), art.get("updated"))
            teaser = (art.get("teaser") or "").strip()
            if len(teaser) > 800:
                teaser = teaser[:800].rsplit(" ", 1)[0] + "…"
            bz_tickers = [
                (stk.get("name") or stk.get("symbol") or "").upper()
                for stk in (art.get("stocks") or [])
                if stk.get("name") or stk.get("symbol")
            ]
            items.append(NewsItem(
                headline     = title,
                source       = "Benzinga",
                published_at = pub,
                sentiment    = round(sentiment, 3),
                confidence   = round(confidence, 3),
                tickers      = bz_tickers[:6],
                cached       = False,
                category     = category,
                priority     = priority,
                ticker_tier  = "index",
                summary      = teaser,
                url          = art.get("url") or "",
            ))

    if items:
        log.info(
            "Benzinga general: %d new articles (raw rows=%d, pages=%d)",
            len(items), total_raw, BENZINGA_GENERAL_EXTRA_PAGES + 1,
        )
    return items, True


# ── Unified stream ────────────────────────────────────────────────────────────

async def unified_news_stream(
    get_tickers: Callable[[], list[str]],
    get_portfolio_tickers: Callable[[], list[str]] | None = None,
) -> AsyncIterator[NewsItem]:
    """
    Async generator — yields NewsItems indefinitely in priority order.

    Tier fetch order each cycle:
      1. Benzinga general (broad, paginated)       — every cycle
      2. indices → portfolio → active → top names — Benzinga per tier, then yfinance

    Within each yield batch, items are sorted:
      HIGH priority → newest first → Benzinga before Yahoo when tied
    """
    if not ENABLE_NEWS_FEED:
        log.info("News feed disabled (ENABLE_NEWS_FEED=false)")
        return

    seen: set[str] = set()
    bz_available   = bool(BENZINGA_API_KEY)   # False after hard auth failure
    bz_hard_fail   = False                    # True on 401/403 — stop retrying forever
    cycle          = 0
    last_bz_general_fetch = 0.0
    last_bz_tier_fetch    = 0.0

    log.info(
        "News stream started: Benzinga=%s  yfinance=always  tiers=indices+portfolio+active+top_stocks",
        "enabled (key set)" if bz_available else "disabled (no key)",
    )

    async with httpx.AsyncClient() as client:
        while True:
            cycle += 1

            # Build tier lists
            active_tickers    = get_tickers()
            portfolio_tickers = get_portfolio_tickers() if get_portfolio_tickers else []
            # Pull top names every cycle for volume (yfinance + Benzinga per ticker)
            include_top = True

            tiers: list[tuple[str, list[str], int]] = [
                ("index",     TIER_INDICES,                           5),
                ("portfolio", list(dict.fromkeys(portfolio_tickers)), 12),
                ("active",    list(dict.fromkeys(active_tickers)),    6),
            ]
            if include_top:
                # Benzinga handles all tickers in one API call (comma-separated),
                # yfinance rotates through them with TTL caching per ticker.
                tiers.append(("top", TIER_TOP_STOCKS, 50))

            all_items: list[NewsItem] = []

            # ── Benzinga general (no ticker filter) — every cycle, paginated for volume ──
            if bz_available and not bz_hard_fail:
                now_m = time.monotonic()
                if (now_m - last_bz_general_fetch) >= BENZINGA_GENERAL_MIN_S:
                    last_bz_general_fetch = now_m
                    gen_items, gen_ok = await _fetch_benzinga_general(seen, client)
                    if not gen_ok:
                        bz_hard_fail = True
                        log.error(
                            "Benzinga permanently disabled this session due to auth/parse error. "
                            "Fix BENZINGA_API_KEY in .env and restart."
                        )
                    else:
                        all_items.extend(gen_items)

            for tier_label, ticker_list, max_t in tiers:
                if not ticker_list:
                    continue

                # ── Benzinga ticker-specific ───────────────────────────────────
                if bz_available and not bz_hard_fail:
                    now_m = time.monotonic()
                    if (now_m - last_bz_tier_fetch) >= BENZINGA_TIER_MIN_S:
                        last_bz_tier_fetch = now_m
                        bz_items, bz_ok = await _fetch_benzinga_tier(
                            ticker_list, tier_label, seen, client, max_tickers=max_t
                        )
                        if not bz_ok:
                            bz_hard_fail = True
                            log.error(
                                "Benzinga permanently disabled this session due to auth/parse error. "
                                "Fix BENZINGA_API_KEY in .env and restart."
                            )
                        elif bz_items:
                            all_items.extend(bz_items)

                # ── yfinance (supplement) ─────────────────────────────────────
                # If you want "most news from Benzinga", set YF_NEWS_MODE=fallback.
                # In fallback mode, only use yfinance when Benzinga is unavailable/hard-failed.
                use_yf = (YF_MODE == "always") or (not (bz_available and not bz_hard_fail))
                if use_yf:
                    # Deduplication via `seen` ensures no headline appears twice.
                    yf_items = await asyncio.to_thread(
                        _fetch_yf_tier, ticker_list, tier_label, seen, max_t
                    )
                    all_items.extend(yf_items)

            # Synthetic last resort (disabled by default — real feeds cover normal use)
            if not all_items and ENABLE_SYNTHETIC_NEWS:
                async for item in _synthetic_batch(seen):
                    all_items.append(item)

            # Yield: HIGH first → newest → prefer Benzinga over Yahoo for same slot
            _PRIO = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

            def _src_rank(src: str) -> int:
                return 0 if (src or "").upper().startswith("BENZINGA") else 1

            for item in sorted(
                all_items,
                key=lambda x: (
                    _PRIO.get(x.priority, 1),
                    -x.published_at.timestamp(),
                    _src_rank(x.source),
                ),
            ):
                yield item

            bz_src = "Benzinga+yf" if (bz_available and not bz_hard_fail) else "yfinance"
            n_bz = sum(1 for i in all_items if (i.source or "").upper().startswith("BENZINGA"))
            log.info(
                "News cycle %d [%s]: %d new items (Benzinga=%d, Yahoo/other=%d, HIGH=%d)",
                cycle, bz_src,
                len(all_items),
                n_bz,
                len(all_items) - n_bz,
                sum(1 for i in all_items if i.priority == "HIGH"),
            )

            await asyncio.sleep(BENZINGA_POLL_S)


# ── Synthetic fallback ────────────────────────────────────────────────────────

_SYNTHETIC_HEADLINES = [
    # (headline, sentiment, category, priority)
    ("Fed holds rates steady, signals two cuts in 2026",               +0.55, "macro",    "HIGH"),
    ("SEC launches investigation into options market manipulation",     -0.65, "regulatory","HIGH"),
    ("SPY ETF hits record inflows amid AI-driven optimism",             +0.75, "general",  "NORMAL"),
    ("Unexpected CPI surge rattles bond markets",                       -0.50, "macro",    "HIGH"),
    ("Tech earnings beat estimates, VIX falls to 12",                  +0.70, "earnings", "HIGH"),
    ("Treasury yields spike on stronger-than-expected jobs data",       -0.35, "macro",    "HIGH"),
    ("Mega-cap tech leads broad rally after soft PCE print",            +0.65, "macro",    "HIGH"),
    ("Credit spreads widen on recession fears",                        -0.55, "general",  "NORMAL"),
    ("AAPL acquires AI startup in $3B deal",                           +0.50, "deal",     "HIGH"),
    ("NVDA raises FY guidance on surging data-centre demand",           +0.70, "guidance", "HIGH"),
]


async def _synthetic_batch(seen: set[str]) -> AsyncIterator[NewsItem]:
    slot = int(time.time() / 60) % len(_SYNTHETIC_HEADLINES)
    text, sent, cat, prio = _SYNTHETIC_HEADLINES[slot]
    h = _headline_hash(text)
    if h not in seen:
        seen.add(h)
        yield NewsItem(
            headline     = text,
            source       = "SYNTHETIC",
            published_at = datetime.now(tz=timezone.utc),
            sentiment    = sent,
            confidence   = 0.85,
            tickers      = ["SPY"],
            cached       = False,
            category     = cat,
            priority     = prio,
            ticker_tier  = "index",
        )


# ── Legacy compat shim ────────────────────────────────────────────────────────

async def benzinga_stream(tickers: list[str]) -> AsyncIterator[NewsItem]:
    """Backward-compatible shim — routes through unified_news_stream."""
    async for item in unified_news_stream(lambda: tickers):
        yield item
