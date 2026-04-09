"""
Agent Tier Manager
==================

Three-tier execution model based on urgency and compute cost:

┌──────────────────────────────────────────────────────────────────────┐
│  TIER 1  –  ALWAYS ON  (lightweight, no LLM, runs every 30-60 s)   │
│                                                                      │
│  SentimentMonitor   : aggregate headline scores → sentiment signal  │
│  MovementTracker    : EMA cross + vol → price movement signal       │
│                                                                      │
│  Both write to firm_state continuously so other agents always have  │
│  fresh signals without waiting for a full pipeline run.             │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ signals exceed trigger thresholds
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 2  –  PERIODIC  (scheduled, may call APIs / LLMs)             │
│                                                                      │
│  FundamentalsRefresher : yfinance info → firm_state.fundamentals    │
│    → every 4 hours (fundamentals rarely change intraday)            │
│                                                                      │
│  NewsProcessor : AI-enriches headlines → cross-stock impact map     │
│    → every 5 min (LLM: sentiment, category, affected tickers)      │
│                                                                      │
│  Note: Options chain refresh runs in api_server (15s cycle),        │
│  not here — it's a data ingestion task, not an agent loop.          │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ data ready + T1 signal strong
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 3  –  TRIGGERED PIPELINE  (full LLM pipeline, event-driven)   │
│                                                                      │
│  Trigger sources:                                                    │
│    • Manual  : POST /run_cycle from UI                              │
│    • Auto    : |sentiment| ≥ 0.40 AND |movement| ≥ 0.30            │
│    • Scanner : anomaly detected on any S&P 500 stock               │
│    • Timer   : api_server._cycle_task (60s fallback, skips if T3    │
│                already ran within cooldown window)                   │
│                                                                      │
│  Pipeline (LangGraph):                                               │
│    ingest_data → options_specialist → sentiment_analyst             │
│    → bull_researcher → bear_researcher → strategist                │
│    → risk_manager → [adversarial_debate] → desk_head               │
│    → trader (autopilot) OR recommend (advisory) → xai_log          │
└──────────────────────────────────────────────────────────────────────┘

Usage (called from api_server.py on startup):
    await start_tier_loops(firm_state)

Manual T3 trigger:
    await trigger_tier3(firm_state, source="manual")
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.state import FirmState

log = logging.getLogger(__name__)

# ── Trigger thresholds ─────────────────────────────────────────────────────────
T1_SENTIMENT_THRESH  = 0.40   # |score| must exceed this
T1_MOVEMENT_THRESH   = 0.30   # |signal| must exceed this
T3_COOLDOWN_MINUTES  = 15     # minimum gap between auto T3 runs

# ── Intervals ──────────────────────────────────────────────────────────────────
MOVEMENT_INTERVAL_SEC    = 30
SENTIMENT_INTERVAL_SEC   = 60
FUNDAMENTALS_INTERVAL_SEC = 4 * 3600   # 4 h
# More aggressive news processing so cross-stock impact updates quickly.
# Note: this triggers LLM usage — tune via env if you want it cheaper.
NEWS_PROCESS_INTERVAL_SEC = 2 * 60     # 2 min — AI news analysis cycle
# Note: Options chain refresh is handled by api_server._tick_ingestion_task (15s)
# and is NOT a tier loop — it runs as a separate background task.


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — SENTIMENT MONITOR (no LLM, just score aggregation)
# ═══════════════════════════════════════════════════════════════════════════════

async def _sentiment_monitor_loop(firm_state: "FirmState") -> None:
    """
    Lightweight sentiment aggregation from already-cached headline scores.
    Recomputes the recency-weighted average every 60 s — no LLM call.
    Signals are written directly to firm_state.sentiment_monitor_score and
    firm_state.aggregate_sentiment so the UI always has a fresh reading.
    """
    from datetime import timezone, timedelta
    log.info("Tier-1 SentimentMonitor started.")
    firm_state.tier1_active = True

    while True:
        try:
            now    = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=1)

            recent = []
            for n in firm_state.news_feed:
                if not hasattr(n, "published_at") or n.published_at is None:
                    continue
                pub = n.published_at
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub >= cutoff:
                    recent.append(n)

            if recent:
                def _weight(pub: datetime) -> float:
                    p = pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
                    age_min = (now - p).total_seconds() / 60.0
                    return max(0.1, 1.0 - (age_min / 60.0) * 0.9)

                wsum = sum(_weight(n.published_at) * n.sentiment for n in recent)
                wden = sum(_weight(n.published_at) for n in recent) or 1.0
                score = round(wsum / wden, 4)
            else:
                score = 0.0

            firm_state.sentiment_monitor_score = score
            # Keep aggregate_sentiment in sync so the full-LLM agent has a baseline
            if abs(score) > abs(firm_state.aggregate_sentiment):
                firm_state.aggregate_sentiment = score

            log.debug("SentimentMonitor: score=%.3f headlines=%d", score, len(recent))

        except Exception as exc:
            log.debug("SentimentMonitor error: %s", exc)

        await asyncio.sleep(SENTIMENT_INTERVAL_SEC)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — MOVEMENT TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

async def _movement_tracker_loop(firm_state: "FirmState") -> None:
    """
    Fetches recent price bars from yfinance every 30 s and computes
    movement signals. Writes results directly to firm_state fields.
    Anomalies are logged so the T3 auto-trigger can respond.
    """
    from agents.agents.movement_tracker import run_movement_tracker

    log.info("Tier-1 MovementTracker started.")

    while True:
        try:
            ticker = firm_state.ticker
            price  = firm_state.underlying_price or None

            signals = await asyncio.to_thread(
                run_movement_tracker, ticker, price
            )

            firm_state.movement_signal   = signals["movement_signal"]
            firm_state.movement_anomaly  = signals["anomaly"]
            firm_state.price_change_pct  = signals["price_change_pct"]
            firm_state.momentum          = signals["momentum"]
            firm_state.vol_ratio         = signals["vol_ratio"]
            firm_state.movement_updated  = datetime.now(timezone.utc)

            if signals["anomaly"]:
                log.info(
                    "MovementTracker ANOMALY on %s: Δ=%.2f%% mom=%.4f vol=%.1fx signal=%.3f",
                    ticker,
                    signals["price_change_pct"] * 100,
                    signals["momentum"],
                    signals["vol_ratio"],
                    signals["movement_signal"],
                )

        except Exception as exc:
            log.debug("MovementTracker error: %s", exc)

        await asyncio.sleep(MOVEMENT_INTERVAL_SEC)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — FUNDAMENTALS REFRESHER
# ═══════════════════════════════════════════════════════════════════════════════

async def _fundamentals_loop(firm_state: "FirmState") -> None:
    """
    Fetches stock fundamentals from yfinance every 4 hours.
    Fundamentals (P/E, EPS, revenue, analyst targets) rarely change intraday,
    so a 4-hour refresh cycle is plenty.
    """
    log.info("Tier-2 FundamentalsRefresher started.")
    firm_state.tier2_active = True

    while True:
        try:
            ticker = firm_state.ticker
            log.info("Tier-2 FundamentalsRefresher: refreshing %s…", ticker)

            info = await asyncio.to_thread(_fetch_fundamentals, ticker)
            if info:
                firm_state.fundamentals         = info
                firm_state.fundamentals_updated = datetime.now(timezone.utc)
                log.info(
                    "Tier-2 FundamentalsRefresher: %s — P/E=%.1f mktcap=%s",
                    ticker,
                    info.get("pe_ratio", 0),
                    info.get("market_cap_fmt", "?"),
                )

        except Exception as exc:
            log.warning("FundamentalsRefresher error: %s", exc)

        await asyncio.sleep(FUNDAMENTALS_INTERVAL_SEC)


def _fetch_fundamentals(ticker: str) -> dict:
    """Synchronous yfinance fundamentals fetch — runs in thread pool."""
    try:
        import yfinance as yf

        tk   = yf.Ticker(ticker)
        info = tk.info or {}

        mktcap = info.get("marketCap", 0)
        if mktcap >= 1e12:
            mktcap_fmt = f"${mktcap / 1e12:.2f}T"
        elif mktcap >= 1e9:
            mktcap_fmt = f"${mktcap / 1e9:.2f}B"
        else:
            mktcap_fmt = f"${mktcap:,.0f}"

        return {
            "name":            info.get("longName", ticker),
            "sector":          info.get("sector", ""),
            "industry":        info.get("industry", ""),
            "market_cap":      mktcap,
            "market_cap_fmt":  mktcap_fmt,
            "pe_ratio":        info.get("trailingPE", 0) or 0,
            "fwd_pe":          info.get("forwardPE", 0) or 0,
            "peg":             info.get("pegRatio", 0) or 0,
            "eps_ttm":         info.get("trailingEps", 0) or 0,
            "revenue":         info.get("totalRevenue", 0) or 0,
            "gross_margin":    info.get("grossMargins", 0) or 0,
            "net_margin":      info.get("profitMargins", 0) or 0,
            "roe":             info.get("returnOnEquity", 0) or 0,
            "beta":            info.get("beta", 0) or 0,
            "div_yield":       info.get("dividendYield", 0) or 0,
            "52w_high":        info.get("fiftyTwoWeekHigh", 0) or 0,
            "52w_low":         info.get("fiftyTwoWeekLow", 0) or 0,
            "analyst_target":  info.get("targetMeanPrice", 0) or 0,
            "analyst_rating":  info.get("recommendationKey", ""),
            "description":     (info.get("longBusinessSummary", "") or "")[:400],
        }

    except Exception as exc:
        log.debug("_fetch_fundamentals(%s): %s", ticker, exc)
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — NEWS AI PROCESSOR  (every 5 min, uses LLM)
# ═══════════════════════════════════════════════════════════════════════════════

async def _news_processor_loop(firm_state: "FirmState") -> None:
    """
    Every 5 minutes: collect unprocessed headlines, send to LLM for deep
    analysis (sentiment, category, cross-stock impact chains), persist to
    JSONL on disk, and update cross-stock impact map on firm_state.
    """
    from agents.data.news_processor import process_new_headlines
    from agents.data.sp500 import SP500_TOP50

    log.info("Tier-2 NewsProcessor started (interval=%ds).", NEWS_PROCESS_INTERVAL_SEC)

    # Wait 30s on startup to let the news feed accumulate some headlines
    await asyncio.sleep(30)

    while True:
        try:
            news_feed = list(firm_state.news_feed)  # snapshot
            if news_feed:
                new_articles, impact_map = await asyncio.to_thread(
                    process_new_headlines, news_feed, SP500_TOP50,
                )
                if new_articles:
                    # Write impact summary to firm_state for other agents
                    firm_state.news_impact_map = {
                        k: v.model_dump(mode="json")
                        for k, v in impact_map.items()
                    }
                    log.info(
                        "NewsProcessor: processed %d new articles, %d tickers impacted",
                        len(new_articles), len(impact_map),
                    )
                    try:
                        from agents.research.universe_service import mark_dirty_from_impact

                        mark_dirty_from_impact(list(impact_map.keys())[:40], "news_impact_graph")
                    except Exception as exc:
                        log.debug("mark_dirty_from_impact: %s", exc)
            else:
                log.debug("NewsProcessor: no headlines in feed yet")

        except Exception as exc:
            log.warning("NewsProcessor error: %s", exc)

        await asyncio.sleep(NEWS_PROCESS_INTERVAL_SEC)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 3 — AUTO-TRIGGER WATCHDOG
# ═══════════════════════════════════════════════════════════════════════════════

async def _t3_watchdog_loop(firm_state: "FirmState") -> None:
    """
    Checks T1 signals every 60 s. Triggers the full T3 pipeline automatically
    when both sentiment AND movement signals are strong AND cooldown has passed.
    """
    log.info("Tier-3 watchdog started.")

    while True:
        await asyncio.sleep(60)
        try:
            if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
                continue

            sentiment = abs(firm_state.sentiment_monitor_score)
            movement  = abs(firm_state.movement_signal)

            # Check cooldown
            last = firm_state.last_tier3_run
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
                if elapsed < T3_COOLDOWN_MINUTES:
                    continue

            if sentiment >= T1_SENTIMENT_THRESH and movement >= T1_MOVEMENT_THRESH:
                log.info(
                    "T3 watchdog: triggering pipeline — sentiment=%.2f movement=%.2f",
                    sentiment, movement,
                )
                await trigger_tier3(firm_state, source="auto")

        except Exception as exc:
            log.debug("T3 watchdog error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 3 — PIPELINE TRIGGER
# ═══════════════════════════════════════════════════════════════════════════════

async def trigger_tier3(firm_state: "FirmState", source: str = "manual") -> None:
    """
    Fire the T3 research + execution pipeline.
    Called by:
      - Manual UI button (source="manual")
      - T3 watchdog (source="auto")
      - Scanner anomaly hook (source="scanner")
    """
    from agents.graph import run_cycle_async
    from agents.state import ReasoningEntry

    if firm_state.tier3_active:
        log.info("T3 already running — skipping duplicate trigger from '%s'.", source)
        return

    firm_state.tier3_active  = True
    firm_state.tier3_trigger = source

    # Stamp a system entry so the UI shows the trigger source
    firm_state.reasoning_log.append(ReasoningEntry(
        agent     = "System",
        action    = "INFO",
        reasoning = (
            f"Tier-3 pipeline triggered by: {source.upper()}\n"
            f"Sentiment signal: {firm_state.sentiment_monitor_score:+.3f}  "
            f"Movement signal: {firm_state.movement_signal:+.3f}  "
            f"Anomaly: {firm_state.movement_anomaly}"
        ),
        inputs  = {
            "source":    source,
            "sentiment": firm_state.sentiment_monitor_score,
            "movement":  firm_state.movement_signal,
        },
        outputs = {},
    ))

    try:
        result, err = await run_cycle_async(firm_state)

        # Full state merge — copy all fields from the graph result back to
        # the live firm_state so trader_decision, pending_recommendations,
        # pending_proposal, debate_record, etc. are all propagated.
        _SKIP_MERGE = {
            "tier1_active", "tier2_active", "tier3_active",
            "tier3_trigger", "last_tier3_run",
            "news_feed", "news_impact_map",
        }
        for fld in result.model_fields:
            if fld in _SKIP_MERGE:
                continue
            try:
                setattr(firm_state, fld, getattr(result, fld))
            except Exception:
                pass

        firm_state.last_tier3_run = datetime.now(timezone.utc)
        if err:
            log.warning("T3 pipeline completed with error: %s", err)
    except Exception as exc:
        log.error("T3 pipeline failed: %s", exc)
    finally:
        firm_state.tier3_active = False


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC: START ALL LOOPS
# ═══════════════════════════════════════════════════════════════════════════════

_running_tasks: list[asyncio.Task] = []


async def start_tier_loops(firm_state: "FirmState") -> None:
    """
    Start all tier background loops.
    Call once from api_server on startup (inside lifespan or startup event).

    Tasks are stored in module-level _running_tasks so they can be cancelled
    on shutdown.
    """
    global _running_tasks

    loops = [
        ("T1-SentimentMonitor",    _sentiment_monitor_loop(firm_state)),
        ("T1-MovementTracker",     _movement_tracker_loop(firm_state)),
        ("T2-FundamentalsRefresh", _fundamentals_loop(firm_state)),
        ("T2-NewsProcessor",       _news_processor_loop(firm_state)),
        ("T3-Watchdog",            _t3_watchdog_loop(firm_state)),
    ]

    for name, coro in loops:
        task = asyncio.create_task(coro, name=name)
        _running_tasks.append(task)
        log.info("Started background loop: %s", name)


def stop_tier_loops() -> None:
    """Cancel all running tier loops (call from shutdown handler)."""
    for task in _running_tasks:
        if not task.done():
            task.cancel()
    _running_tasks.clear()
    log.info("All tier loops cancelled.")


def tier_status(firm_state: "FirmState") -> dict:
    """
    Return a concise status dict suitable for the /tiers/status API endpoint
    and the UI status bar.
    """
    return {
        "tier1": {
            "active":           firm_state.tier1_active,
            "sentiment_score":  round(firm_state.sentiment_monitor_score, 4),
            "movement_signal":  round(firm_state.movement_signal, 4),
            "movement_anomaly": firm_state.movement_anomaly,
            "price_change_pct": round(firm_state.price_change_pct * 100, 3),
            "momentum":         round(firm_state.momentum, 5),
            "vol_ratio":        round(firm_state.vol_ratio, 3),
            "movement_updated": (
                firm_state.movement_updated.isoformat()
                if firm_state.movement_updated else None
            ),
        },
        "tier2": {
            "active":               firm_state.tier2_active,
            "fundamentals_ticker":  firm_state.ticker,
            "fundamentals_updated": (
                firm_state.fundamentals_updated.isoformat()
                if firm_state.fundamentals_updated else None
            ),
            "has_fundamentals":     bool(firm_state.fundamentals),
        },
        "tier3": {
            "active":        firm_state.tier3_active,
            "last_run":      (
                firm_state.last_tier3_run.isoformat()
                if firm_state.last_tier3_run else None
            ),
            "last_trigger":  firm_state.tier3_trigger,
            "cooldown_ok":   (
                firm_state.last_tier3_run is None
                or (datetime.now(timezone.utc) - firm_state.last_tier3_run).total_seconds() / 60
                   >= T3_COOLDOWN_MINUTES
            ),
        },
        "thresholds": {
            "sentiment": T1_SENTIMENT_THRESH,
            "movement":  T1_MOVEMENT_THRESH,
            "cooldown_minutes": T3_COOLDOWN_MINUTES,
        },
    }
