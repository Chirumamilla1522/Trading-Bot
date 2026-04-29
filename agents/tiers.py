"""
Agent Tier Manager
==================

Three-tier model (multi-horizon: tens of seconds to hours; not millisecond HFT):

┌──────────────────────────────────────────────────────────────────────┐
│  TIER 1  –  ALWAYS ON  (lightweight, no LLM, ~30–60 s)               │
│                                                                      │
│  SentimentMonitor  : LLM over structured Tier-2 news + news timing     │
│  MovementTracker   : price/vol signals + market_bias (non-news bias)   │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ multi-factor auto-triggers (below)
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 2  –  PERIODIC  (APIs / LLMs on a schedule)                    │
│                                                                      │
│  FundamentalsRefresher — yfinance; fingerprint change → T3 flag      │
│  NewsProcessor — LLM headlines → news_impact_map                   │
│  (Options chain lives in api_server 15s task.)                       │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  TIER 3  –  TRIGGERED LLM GRAPH                                      │
│                                                                      │
│  Auto: sentiment+movement OR technical anomaly OR fundamentals OR     │
│        market_bias+movement; + manual UI, scanner, api timer.         │
│  Flow: ingest → options → sentiment → bull/bear → strategist →       │
│        risk → [debate] → desk_head → trader/recommend → xai_log      │
└──────────────────────────────────────────────────────────────────────┘

Usage (called from api_server.py on startup):
    await start_tier_loops(firm_state)

Manual T3 trigger:
    await trigger_tier3(firm_state, source="manual")
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.state import FirmState

log = logging.getLogger(__name__)

from agents.config import ENABLE_NEWS_PROCESSOR, ENABLE_SENTIMENT_MONITOR

# ── Trigger thresholds ─────────────────────────────────────────────────────────
T1_SENTIMENT_THRESH  = 0.40   # |score| must exceed this
T1_MOVEMENT_THRESH   = 0.30   # |signal| must exceed this
# Non-news / technical auto-run: anomaly + movement, or strong structure + movement
T3_MOVEMENT_ALONE_THRESH = 0.34
T3_MARKET_BIAS_THRESH    = 0.42
T3_COOLDOWN_MINUTES  = 15     # minimum gap between auto T3 runs

# Prior fundamentals fingerprint (module state; compared on each Tier-2 refresh)
_last_fundamentals_fingerprint: str | None = None

# ── Intervals ──────────────────────────────────────────────────────────────────
MOVEMENT_INTERVAL_SEC    = 30
SENTIMENT_INTERVAL_SEC   = 60
FUNDAMENTALS_INTERVAL_SEC = 4 * 3600   # 4 h
# More aggressive news processing so cross-stock impact updates quickly.
# Note: this triggers LLM usage — tune via env if you want it cheaper.
NEWS_PROCESS_INTERVAL_SEC = 2 * 60     # 2 min — AI news analysis cycle
# Note: Options chain refresh is handled by api_server._tick_ingestion_task (15s)
# and is NOT a tier loop — it runs as a separate background task.

def _max_pending_recs_per_ticker() -> int:
    try:
        v = int(float(os.getenv("MAX_PENDING_RECS_PER_TICKER", "5")))
    except Exception:
        v = 5
    return max(1, min(50, v))


def _pending_recs_for_ticker(firm_state: "FirmState", ticker: str) -> int:
    t = (ticker or "").upper().strip()
    if not t:
        return 0
    return sum(
        1
        for r in (firm_state.pending_recommendations or [])
        if (getattr(r, "ticker", "") or "").upper().strip() == t
        and getattr(r, "status", "") == "pending"
    )


def _portfolio_tickers(firm_state: "FirmState") -> list[str]:
    out: list[str] = []
    for p in (firm_state.stock_positions or []):
        t = (getattr(p, "ticker", None) or "").upper().strip()
        if t:
            out.append(t)
    # Option positions may not have a simple ticker, but many do store `symbol` as OCC.
    # Keep it conservative: don't attempt parsing here.
    return list(dict.fromkeys(out))


def _select_next_focus_ticker(firm_state: "FirmState", *, exclude: set[str] | None = None) -> str | None:
    """
    Pick the next Tier-3 focus ticker from indices/portfolio/ui_active/top50,
    excluding symbols that already have too many pending recs.
    """
    from agents.data.news_feed import TIER_INDICES
    from agents.data.sp500 import SP500_TOP50

    cap = _max_pending_recs_per_ticker()
    ex = {x.upper().strip() for x in (exclude or set()) if str(x).strip()}

    ui_active = (firm_state.ticker or "").upper().strip()
    candidates: list[str] = []
    candidates.extend([t.upper().strip() for t in (TIER_INDICES or []) if str(t).strip()])
    candidates.extend(_portfolio_tickers(firm_state))
    if ui_active:
        candidates.append(ui_active)
    candidates.extend([t.upper().strip() for t in (SP500_TOP50 or []) if str(t).strip()])

    # De-dupe, filter excluded + cap
    uniq: list[str] = []
    seen: set[str] = set()
    for t in candidates:
        if not t or t in seen:
            continue
        seen.add(t)
        if t in ex:
            continue
        if _pending_recs_for_ticker(firm_state, t) >= cap:
            continue
        uniq.append(t)

    if not uniq:
        return None

    # Random-ish but stable enough: seed changes over time and with current focus.
    seed = f"{datetime.now(timezone.utc).date().isoformat()}|{ui_active}|{firm_state.last_tier3_run}"
    rng = random.Random(seed)

    # Prefer portfolio tickers first, then UI ticker, then the rest.
    port = set(_portfolio_tickers(firm_state))
    preferred: list[str] = [t for t in uniq if t in port]
    if ui_active and ui_active in uniq and ui_active not in preferred:
        preferred.append(ui_active)
    rest = [t for t in uniq if t not in set(preferred)]
    rng.shuffle(rest)

    pool = preferred + rest
    return pool[0] if pool else None


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — SENTIMENT MONITOR (LLM over structured Tier-2 news outputs)
# ═══════════════════════════════════════════════════════════════════════════════

async def _sentiment_monitor_loop(firm_state: "FirmState") -> None:
    """
    SentimentMonitor: LLM synthesizes ONE desk score from structured articles
    (sentiment, confidence, impact_magnitude, digests) produced by Tier-2 NewsProcessor
    and stored in SQLite / in-memory buffer — not raw keyword headline scores.

    On LLM failure, falls back to a deterministic blend of the same structured fields.
    """
    log.info("Tier-1 SentimentMonitor started (structured + LLM).")
    firm_state.tier1_active = True

    while True:
        try:
            from agents.desk_context import update_news_timing_from_feed
            from agents.sentiment_monitor_llm import run_sentiment_monitor_cycle

            result = await asyncio.to_thread(
                run_sentiment_monitor_cycle,
                firm_state.ticker,
            )
            score = float(result.get("desk_sentiment", 0.0))

            firm_state.sentiment_monitor_score = score
            firm_state.sentiment_monitor_confidence = float(result.get("confidence") or 0.0)
            firm_state.sentiment_monitor_reasoning = str(result.get("reasoning") or "")[:500]
            firm_state.sentiment_monitor_source = str(result.get("source") or "none")

            if abs(score) > abs(firm_state.aggregate_sentiment):
                firm_state.aggregate_sentiment = score

            log.debug(
                "SentimentMonitor: score=%.3f source=%s conf=%.2f",
                score,
                firm_state.sentiment_monitor_source,
                firm_state.sentiment_monitor_confidence,
            )

            update_news_timing_from_feed(firm_state)

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

            from agents.desk_context import update_market_bias_score

            update_market_bias_score(firm_state)

        except Exception as exc:
            log.debug("MovementTracker error: %s", exc)

        await asyncio.sleep(MOVEMENT_INTERVAL_SEC)


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — FUNDAMENTALS REFRESHER
# ═══════════════════════════════════════════════════════════════════════════════

async def _fundamentals_loop(firm_state: "FirmState") -> None:
    """
    Fetches stock fundamentals from yfinance every 4 hours.
    When the fingerprint of key fields changes, sets fundamentals_material_change
    so Tier-3 can run a full research pass (valuation / thesis update).
    """
    global _last_fundamentals_fingerprint

    log.info("Tier-2 FundamentalsRefresher started.")
    firm_state.tier2_active = True

    while True:
        try:
            ticker = firm_state.ticker
            log.info("Tier-2 FundamentalsRefresher: refreshing %s…", ticker)

            info = await asyncio.to_thread(_fetch_fundamentals, ticker)
            if info:
                from agents.desk_context import fundamentals_fingerprint

                fp = fundamentals_fingerprint(info)
                if (
                    _last_fundamentals_fingerprint is not None
                    and fp != _last_fundamentals_fingerprint
                ):
                    firm_state.fundamentals_material_change = True
                    log.info(
                        "Fundamentals fingerprint changed for %s — Tier-3 may auto-trigger.",
                        ticker,
                    )
                _last_fundamentals_fingerprint = fp

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

        from agents.data.fundamentals import dividend_yield_from_yfinance_info

        _dy = dividend_yield_from_yfinance_info(info)
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
            "div_yield":       _dy if _dy is not None else 0,
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
    Every 60 s: evaluates multi-factor auto-triggers (cooldown-aware).

    Not HFT: horizons are minutes+. Triggers include headline+price alignment,
    technical anomaly without fresh news, material fundamentals refresh, or
    strong market_structure + movement.
    """
    log.info("Tier-3 watchdog started.")

    while True:
        await asyncio.sleep(60)
        try:
            if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
                continue

            # If SentimentMonitor is disabled, don't use its score as an auto-trigger input.
            sentiment = abs(firm_state.sentiment_monitor_score) if ENABLE_SENTIMENT_MONITOR else 0.0
            movement  = abs(firm_state.movement_signal)
            bias      = abs(firm_state.market_bias_score)

            # Check cooldown
            last = firm_state.last_tier3_run
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
                if elapsed < T3_COOLDOWN_MINUTES:
                    continue

            source: str | None = None
            if sentiment >= T1_SENTIMENT_THRESH and movement >= T1_MOVEMENT_THRESH:
                source = "auto_sentiment_movement"
            elif firm_state.fundamentals_material_change:
                source = "auto_fundamentals_change"
            elif (
                firm_state.movement_anomaly
                and movement >= T3_MOVEMENT_ALONE_THRESH
            ):
                source = "auto_technical_anomaly"
            elif bias >= T3_MARKET_BIAS_THRESH and movement >= T1_MOVEMENT_THRESH:
                source = "auto_market_structure"

            if source:
                log.info(
                    "T3 watchdog: triggering pipeline — source=%s sen=%.2f move=%.2f "
                    "bias=%.2f anomaly=%s fundamentals_flag=%s",
                    source,
                    sentiment,
                    movement,
                    bias,
                    firm_state.movement_anomaly,
                    firm_state.fundamentals_material_change,
                )
                await trigger_tier3(firm_state, source=source)

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
    focus = (firm_state.tier3_focus_ticker or firm_state.ticker or "SPY").upper().strip()
    firm_state.tier3_focus_ticker = focus
    firm_state.tier3_focus_locked = True
    firm_state.tier3_focus_locked_at = datetime.now(timezone.utc)
    firm_state.tier3_focus_lock_reason = f"tier3:{source}"

    # Stamp a system entry so the UI shows the trigger source
    firm_state.reasoning_log.append(ReasoningEntry(
        agent     = "System",
        action    = "INFO",
        reasoning = (
            f"Tier-3 pipeline triggered by: {source.upper()}\n"
            f"Focus ticker (locked): {focus}\n"
            f"Sentiment: {firm_state.sentiment_monitor_score:+.3f}  "
            f"Movement: {firm_state.movement_signal:+.3f}  "
            f"Market bias (non-news): {firm_state.market_bias_score:+.3f}  "
            f"News timing: {firm_state.news_timing_regime}  "
            f"Newest headline age (min): {firm_state.news_newest_age_minutes}  "
            f"Anomaly: {firm_state.movement_anomaly}  "
            f"Fundamentals change pending: {firm_state.fundamentals_material_change}"
        ),
        inputs  = {
            "source":             source,
            "sentiment":          firm_state.sentiment_monitor_score,
            "movement":           firm_state.movement_signal,
            "market_bias_score":  firm_state.market_bias_score,
            "news_timing_regime": firm_state.news_timing_regime,
            "fundamentals_flag":  firm_state.fundamentals_material_change,
        },
        outputs = {},
    ))

    try:
        # Run Tier-3 on a snapshot state so UI ticker switches can't contaminate this run.
        run_state = firm_state.model_copy(deep=True)
        run_state.ticker = focus
        result, err = await run_cycle_async(run_state)

        # Full state merge — copy all fields from the graph result back to
        # the live firm_state so trader_decision, pending_recommendations,
        # pending_proposal, debate_record, etc. are all propagated.
        _SKIP_MERGE = {
            # UI active ticker stays user-controlled; Tier-3 is decoupled.
            "ticker",
            "tier1_active", "tier2_active", "tier3_active",
            "tier3_trigger", "last_tier3_run",
            "news_feed", "news_impact_map",
            # Background loops / watchdog-owned (do not overwrite from graph snapshot)
            "fundamentals_material_change",
            "news_newest_age_minutes", "news_timing_regime", "market_bias_score",
            "sentiment_monitor_score", "sentiment_monitor_confidence",
            "sentiment_monitor_reasoning", "sentiment_monitor_source",
            "movement_signal", "movement_anomaly",
            "price_change_pct", "momentum", "vol_ratio", "movement_updated",
        }
        for fld in result.model_fields:
            if fld in _SKIP_MERGE:
                continue
            try:
                setattr(firm_state, fld, getattr(result, fld))
            except Exception:
                pass

        firm_state.last_tier3_run = datetime.now(timezone.utc)

        # If this focus ticker is saturated with pending approvals, rotate focus.
        cap = _max_pending_recs_per_ticker()
        if _pending_recs_for_ticker(firm_state, focus) >= cap:
            nxt = _select_next_focus_ticker(firm_state, exclude={focus})
            if nxt and nxt != focus:
                firm_state.tier3_focus_ticker = nxt
                firm_state.reasoning_log.append(ReasoningEntry(
                    agent="System",
                    action="INFO",
                    reasoning=(
                        f"Tier-3 focus rotated from {focus} → {nxt}: "
                        f"{cap}+ pending recommendations awaiting approval."
                    ),
                    inputs={"from": focus, "to": nxt, "cap": cap},
                    outputs={},
                ))
        if err:
            log.warning("T3 pipeline completed with error: %s", err)
    except Exception as exc:
        log.error("T3 pipeline failed: %s", exc)
    finally:
        firm_state.tier3_active = False
        firm_state.tier3_focus_locked = False
        firm_state.fundamentals_material_change = False


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
        ("T1-MovementTracker",     _movement_tracker_loop(firm_state)),
        ("T2-FundamentalsRefresh", _fundamentals_loop(firm_state)),
        ("T3-Watchdog",            _t3_watchdog_loop(firm_state)),
    ]
    if ENABLE_SENTIMENT_MONITOR:
        loops.insert(0, ("T1-SentimentMonitor", _sentiment_monitor_loop(firm_state)))
    if ENABLE_NEWS_PROCESSOR:
        # NewsProcessor is Tier-2 LLM workload; keep it optional even when the feed is enabled.
        loops.insert(3, ("T2-NewsProcessor", _news_processor_loop(firm_state)))

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
            "sentiment_monitor_confidence": round(firm_state.sentiment_monitor_confidence, 4),
            "sentiment_monitor_source": firm_state.sentiment_monitor_source,
            "movement_signal":  round(firm_state.movement_signal, 4),
            "market_bias_score": round(firm_state.market_bias_score, 4),
            "news_timing_regime": firm_state.news_timing_regime,
            "news_newest_age_minutes": firm_state.news_newest_age_minutes,
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
            "fundamentals_change_pending": firm_state.fundamentals_material_change,
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
            "movement_anomaly": T3_MOVEMENT_ALONE_THRESH,
            "market_bias": T3_MARKET_BIAS_THRESH,
            "cooldown_minutes": T3_COOLDOWN_MINUTES,
        },
    }
