"""
FastAPI bridge between the Tauri UI and the Python agent graph.

Run from project root: ``python3 agents/api_server.py`` or ``python -m agents.api_server``.

Endpoints:
  GET  /state              – FirmState JSON + `agent_runtime` (UI polling)
  GET  /news               – latest 50 news items
  GET  /reasoning_log      – today's XAI log (?tail=…&agent=ExactAgentId optional)
  GET  /scanner            – S&P 500 options scanner (?sort=iv|pc|oi|ticker|price|chg) + live quotes
  GET  /scanner/quotes     – live last / change only (fast; used for 1 Hz price refresh)
  GET  /scanner/tickers    – full list of tracked tickers
  GET  /quotes/benchmarks  – live last / day %% for index & sector ETF strip (roster prefix order)
  GET  /options/{ticker}   – options chain for a specific ticker (drilldown)
  GET  /bars/{ticker}      – Underlying stock OHLC + summary (Alpaca → Alpha Vantage → Yahoo when configured)
  GET  /quote/{ticker}     – Stock last / day change (Alpaca snapshot → Alpha Vantage → Yahoo; AV is delayed)
  GET  /stock_info/{ticker}– Fundamentals, peers, competitors, and dependency map (yfinance)
  GET  /perception/{ticker} — Phases 0–2 perception bundle (technical, fundamental, events, sentiment, news)
  WS   /ws/market          – Alpaca trades + quotes for embedded L2/L3 panel (see agents/data/alpaca_market_bridge.py)
  GET  /portfolio_series   – rolling NAV / greeks samples for portfolio chart
  POST /set_ticker         – change active ticker (also triggers drilldown cache)
  POST /kill_switch        – software kill switch
  POST /run_cycle          – manually trigger one agent cycle
  GET  /agent_status       – agent loop runtime (last cycle, errors, counters)
  POST /positions/refresh  – force-sync stock + option positions from broker (bypasses TTL)
"""
from __future__ import annotations

# Path bootstrap (must run before `from agents...` imports)
import sys as _sys
import pathlib as _pl

_project_root = _pl.Path(__file__).resolve().parent.parent
if str(_project_root) not in _sys.path:
    _sys.path.insert(0, str(_project_root))

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, date, timezone
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.state              import FirmState, RiskMetrics
from agents.graph              import run_cycle_async
from agents.data.opra_client   import AlpacaDelayedFeed
from agents.data.news_feed     import unified_news_stream
from agents.data.sp500         import SP500Scanner, sort_scan_rows
from agents.data.equity_snapshot import fetch_stock_quotes_batch
from agents.xai.reasoning_log  import get_today_log, log_cycle_failure
from agents.config             import MAX_DAILY_DRAWDOWN, ENABLE_NEWS_FEED

log = logging.getLogger(__name__)

# ── Shared mutable state ──────────────────────────────────────────────────────

firm_state = FirmState(
    ticker           = "SPY",
    underlying_price = 500.0,
    cash_balance       = 100_000.0,
    buying_power       = 100_000.0,
    account_equity     = 100_000.0,
    risk               = RiskMetrics(
        opening_nav      = 100_000.0,
        current_nav      = 100_000.0,
        max_drawdown_pct = MAX_DAILY_DRAWDOWN,
    ),
)

_scanner = SP500Scanner()

# Rolling snapshots for portfolio / Greeks charts (UI polls /portfolio_series)
_PORTFOLIO_HISTORY: deque[dict] = deque(maxlen=2000)

# Targeted option-leg quote prewarm cache (avoid hammering Alpaca on /recommendations poll).
# symbol -> last_prewarm_unix
_LEG_QUOTE_PREWARM_AT: dict[str, float] = {}

# ── WebSocket connection manager ──────────────────────────────────────────────

class _WSManager:
    """Broadcasts state diff messages to all connected WebSocket clients."""

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._last_hash: int = 0

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, data: dict):
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def push_state(self):
        """Build a compact state diff and push to all clients."""
        if not self._clients:
            return
        try:
            r = firm_state.risk
            payload = {
                "type": "state",
                "ticker":             firm_state.ticker,
                "underlying_price":   firm_state.underlying_price,
                "iv_regime":          firm_state.iv_regime,
                "iv_atm":             firm_state.iv_atm,
                "iv_skew_ratio":      firm_state.iv_skew_ratio,
                "market_regime":      firm_state.market_regime.value,
                "aggregate_sentiment": firm_state.aggregate_sentiment,
                "circuit_breaker":    firm_state.circuit_breaker_tripped,
                "kill_switch":        firm_state.kill_switch_active,
                "trader_decision":    firm_state.trader_decision.value,
                "risk": {
                    "portfolio_delta": r.portfolio_delta,
                    "portfolio_gamma": r.portfolio_gamma,
                    "portfolio_vega":  r.portfolio_vega,
                    "portfolio_theta": r.portfolio_theta,
                    "daily_pnl":       r.daily_pnl,
                    "drawdown_pct":    r.drawdown_pct,
                    "current_nav":     r.current_nav,
                    "opening_nav":     r.opening_nav,
                },
                "cash_balance":    firm_state.cash_balance,
                "buying_power":    firm_state.buying_power,
                "account_equity":  firm_state.account_equity,
                "stock_positions": [p.model_dump() for p in firm_state.stock_positions],
                "open_positions":  [p.model_dump() for p in firm_state.open_positions],
                "agent_runtime":   agent_status.to_dict(),
                "news_feed_enabled": ENABLE_NEWS_FEED,
                "trading_mode":    firm_state.trading_mode,
                "pending_recs":    len([r for r in firm_state.pending_recommendations if r.status == "pending"]),
                "news_impacts":    len(firm_state.news_impact_map),
                # Tier signals — T1 continuous updates for UI signal bars
                "tiers": {
                    "t1_active":         firm_state.tier1_active,
                    "t3_active":         firm_state.tier3_active,
                    "sentiment_signal":  round(firm_state.sentiment_monitor_score, 4),
                    "sentiment_monitor_confidence": round(firm_state.sentiment_monitor_confidence, 4),
                    "sentiment_monitor_source":     firm_state.sentiment_monitor_source,
                    "movement_signal":   round(firm_state.movement_signal, 4),
                    "movement_anomaly":  firm_state.movement_anomaly,
                    "price_change_pct":  round(firm_state.price_change_pct * 100, 3),
                    "last_tier3_run":    (
                        firm_state.last_tier3_run.isoformat()
                        if firm_state.last_tier3_run else None
                    ),
                    "tier3_trigger":     firm_state.tier3_trigger,
                    "bull_conviction":   firm_state.bull_conviction,
                    "bear_conviction":   firm_state.bear_conviction,
                },
                "ts": time.time(),
            }
            # Only broadcast if something actually changed
            h = hash(str(payload))
            if h == self._last_hash:
                return
            self._last_hash = h
            await self.broadcast(payload)
        except Exception as e:
            log.debug("WS push_state error: %s", e)


_ws_manager = _WSManager()


async def _probe_llama_cpp() -> None:
    """
    Initialize the LLM server pool and probe all servers at startup.
    Healthy servers are added to the load-balancing pool; unhealthy ones
    are excluded until the pool re-checks them (every HEALTH_RECHECK_S).
    """
    from agents.config import OPENROUTER_ENABLED
    from agents.llm_local import server_pool, llama_local_primary_enabled
    from agents.llm_retry import _mark_local_failed, _reset_local_cooldown

    if OPENROUTER_ENABLED and not llama_local_primary_enabled():
        log.info("LLM backend: OpenRouter PRIMARY (LLAMA_LOCAL_PRIMARY=false)  [local=fallback]")
        server_pool.init_from_env()
        return

    healthy = server_pool.init_from_env()
    all_urls = server_pool.all_urls

    if not OPENROUTER_ENABLED:
        log.info("LLM backend: local servers ONLY (OPENROUTER_ENABLED=false)")
    else:
        log.info("LLM backend: local first, OpenRouter fallback")

    for s in server_pool.status():
        if s["healthy"]:
            log.info("✅ LLM server ONLINE: %s", s["url"])
        else:
            log.warning("⚠️  LLM server OFFLINE: %s", s["url"])

    log.info(
        "LLM pool: %d/%d servers healthy (load-balanced, least-busy routing)",
        len(healthy), len(all_urls),
    )

    if healthy:
        _reset_local_cooldown()
    else:
        fb = (
            "Agents will use OpenRouter until local is back."
            if OPENROUTER_ENABLED
            else " No cloud fallback — start local servers or enable OPENROUTER_ENABLED=true."
        )
        log.warning("⚠️  No local LLM endpoints responded. %s", fb)
        _mark_local_failed()


async def _equity_sync_task():
    """Sync cash, buying power, stock + option positions from Alpaca (paper/live)."""
    from agents.data.equity_snapshot import sync_alpaca_account_into_state

    # First run: yield briefly so the event loop starts, then sync immediately
    await asyncio.sleep(3)
    try:
        await asyncio.to_thread(sync_alpaca_account_into_state, firm_state, True)
    except Exception as e:
        log.warning("equity initial sync: %s", e)

    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(sync_alpaca_account_into_state, firm_state, False)
        except Exception as e:
            log.debug("equity account sync: %s", e)


async def _post_order_sync():
    """
    Triggered after any order placement.
    Waits 2 s (allow broker to register the fill / partial) then forces an
    immediate account + position sync regardless of the 30-second TTL.
    """
    from agents.data.equity_snapshot import sync_alpaca_account_into_state
    await asyncio.sleep(2)
    try:
        await asyncio.to_thread(sync_alpaca_account_into_state, firm_state, True)
    except Exception as e:
        log.debug("post-order sync error: %s", e)


async def _persist_firm_state() -> None:
    """Write firm_state snapshot (includes recommendation history when configured)."""
    try:
        from agents.state_persistence import save_state

        await asyncio.to_thread(save_state, firm_state)
    except Exception:
        pass


async def _portfolio_history_task():
    """Append NAV / greeks points for charting (~every 20s); also writes SQLite for restart durability."""
    while True:
        await asyncio.sleep(20)
        try:
            r = firm_state.risk
            nav = float(r.current_nav or r.opening_nav or 0.0)
            row = {
                "time": time.time(),
                "equity": nav,
                "delta": float(r.portfolio_delta),
                "vega": float(r.portfolio_vega),
                "daily_pnl": float(r.daily_pnl),
                "drawdown_pct": float(r.drawdown_pct),
            }
            _PORTFOLIO_HISTORY.append(row)
            try:
                from agents.data.portfolio_history_db import append_portfolio_point

                await asyncio.to_thread(append_portfolio_point, row)
            except Exception as db_exc:
                log.debug("portfolio_history_db append: %s", db_exc)
        except Exception as e:
            log.debug("portfolio snapshot skipped: %s", e)


# ── Agent runtime status ──────────────────────────────────────────────────────

@dataclass
class AgentRuntimeStatus:
    in_progress: bool = False
    last_cycle_started_at: float = 0.0
    last_cycle_finished_at: float = 0.0
    last_cycle_duration_s: float = 0.0
    last_success_at: float = 0.0
    last_error_at: float = 0.0
    last_error: str = ""
    last_trader_decision: str = ""
    cycles_total: int = 0
    cycles_ok: int = 0
    cycles_error: int = 0

    def to_dict(self) -> dict:
        now = time.time()
        from agents.config import llm_models_snapshot

        d = asdict(self)
        d["now"] = now
        d["llm_models"] = llm_models_snapshot()
        # Use > 0, not truthiness: 0.0 is a valid float but means "never set" here
        d["age_since_success_s"] = (
            (now - self.last_success_at) if self.last_success_at > 0 else None
        )
        d["age_since_error_s"] = (
            (now - self.last_error_at) if self.last_error_at > 0 else None
        )
        d["age_since_finish_s"] = (
            (now - self.last_cycle_finished_at) if self.last_cycle_finished_at > 0 else None
        )
        return d


agent_status = AgentRuntimeStatus()

# ── Concurrency lock (prevents simultaneous agent cycles) ─────────────────────
_cycle_lock = asyncio.Lock()

# ── Background tasks ──────────────────────────────────────────────────────────

async def _tick_ingestion_task():
    """
    Streams the active ticker's options chain into firm_state.latest_greeks.
    Re-fetches whenever firm_state.ticker changes.
    """
    feed = AlpacaDelayedFeed()
    last_ticker = None
    while True:
        ticker = firm_state.ticker
        if ticker != last_ticker:
            last_ticker = ticker
            log.info("Active ticker changed to %s – fetching options chain", ticker)
        try:
            snaps = await asyncio.to_thread(feed._fetch_snapshots, ticker)
            if snaps:
                atm = min(snaps, key=lambda g: abs(abs(g.delta) - 0.5))
                # Prefer stock quote for spot; only fall back to option-derived proxy.
                spot = None
                try:
                    from agents.data.equity_snapshot import fetch_stock_quote

                    q = await asyncio.to_thread(fetch_stock_quote, str(ticker or "").upper())
                    if isinstance(q, dict) and q.get("last") is not None:
                        spot = float(q["last"])
                except Exception:
                    spot = None
                if not spot or spot <= 0:
                    mid = (atm.bid + atm.ask) / 2 if (atm.bid or atm.ask) else 0.0
                    spot = float(mid or atm.strike or 0.0)
                firm_state.underlying_price = float(spot or 0.0)
                from agents.data.options_chain_filter import filter_greeks_for_agents

                firm_state.latest_greeks = filter_greeks_for_agents(
                    snaps, firm_state.underlying_price
                )
            else:
                firm_state.latest_greeks = []
        except Exception as e:
            log.warning("Tick ingestion error for %s: %s", ticker, e)
        await asyncio.sleep(15)


async def _greeks_update_task():
    """
    Every 15 s: recompute live option P&L and portfolio Greeks from
    current positions × latest greeks chain. This keeps RiskMetrics accurate
    without waiting for a full agent cycle.
    """
    from agents.features import compute_portfolio_greeks
    while True:
        await asyncio.sleep(15)
        try:
            if not firm_state.open_positions or not firm_state.latest_greeks:
                continue
            greeks_map = {g.symbol: g for g in firm_state.latest_greeks}
            pg = compute_portfolio_greeks(
                firm_state.open_positions, greeks_map, firm_state.underlying_price
            )
            r = firm_state.risk
            r.portfolio_delta = pg["portfolio_delta"]
            r.portfolio_gamma = pg["portfolio_gamma"]
            r.portfolio_vega  = pg["portfolio_vega"]
            r.portfolio_theta = pg["portfolio_theta"]
            r.daily_pnl       = pg["daily_pnl"]
            # Recompute drawdown from opening NAV
            if r.opening_nav > 0 and r.current_nav > 0:
                r.drawdown_pct = max(0.0, (r.opening_nav - r.current_nav) / r.opening_nav)
        except Exception as e:
            log.debug("greeks_update_task error: %s", e)


async def _position_monitor_task():
    """
    Every 20 s: check open option positions against their stop-loss and
    take-profit thresholds. Auto-submits a closing order when either is hit.
    Uses the last trade proposal's stop_loss_pct / take_profit_pct, defaulting
    to 50% loss / 75% gain if no proposal is stored.
    """
    while True:
        await asyncio.sleep(20)
        try:
            if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
                continue
            if not firm_state.open_positions:
                continue

            ems = _get_ems()
            for pos in list(firm_state.open_positions):
                cost_basis = abs(pos.avg_cost) * abs(pos.quantity) * 100
                if cost_basis <= 0:
                    continue

                pnl_pct = pos.current_pnl / cost_basis   # signed

                # Find matching proposal thresholds (if still current)
                sl_pct = 0.50   # default: close at 50% loss
                tp_pct = 0.75   # default: close at 75% gain
                if firm_state.pending_proposal:
                    sl_pct = firm_state.pending_proposal.stop_loss_pct
                    tp_pct = firm_state.pending_proposal.take_profit_pct

                hit_sl = pnl_pct <= -sl_pct
                hit_tp = pnl_pct >= tp_pct

                if hit_sl or hit_tp:
                    reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                    log.warning(
                        "Position monitor: %s on %s (P&L %.1f%%)",
                        reason, pos.symbol, pnl_pct * 100,
                    )
                    # Determine closing side: if we're long, sell; if short, buy
                    close_side = "sell" if pos.quantity > 0 else "buy"
                    result = await asyncio.to_thread(
                        ems.place_option_order,
                        pos.symbol, close_side, abs(pos.quantity),
                        "market", None, "day",
                    )
                    log.info("Auto-close order result: %s", result)
                    # Schedule a position refresh
                    asyncio.create_task(_post_order_sync())
        except Exception as e:
            log.debug("position_monitor_task error: %s", e)


async def _news_task():
    """
    Consume the unified news stream (Benzinga + yfinance; synthetic only if ENABLE_SYNTHETIC_NEWS=true).
    Tiers covered each cycle (every ~45s):
      index (SPY/QQQ/IWM) → portfolio positions → active ticker → top-50 S&P 500
    HIGH-priority items (earnings, M&A, macro) are yielded first in each batch.
    """
    def _current_tickers() -> list[str]:
        return [firm_state.ticker or "SPY"]

    def _portfolio_tickers() -> list[str]:
        """Return unique tickers from all open stock and option positions."""
        tickers: list[str] = []
        for p in firm_state.stock_positions:
            if p.ticker:
                tickers.append(p.ticker)
        for p in firm_state.open_positions:
            # Option symbol → extract underlying (first alpha chars)
            sym = p.symbol or ""
            import re as _re
            m = _re.match(r"^([A-Z]{1,6})", sym.upper())
            if m:
                tickers.append(m.group(1))
        return list(dict.fromkeys(tickers))

    async for item in unified_news_stream(_current_tickers, _portfolio_tickers):
        firm_state.news_feed.append(item)
        try:
            from agents.data.news_priority_queue import get_queue
            get_queue().push(item)
        except Exception as _exc:
            log.debug("NewsPriorityQueue push failed: %s", _exc)
        if len(firm_state.news_feed) > 300:
            # Keep last 300 but always preserve HIGH-priority items
            highs  = [n for n in firm_state.news_feed if n.priority == "HIGH"]
            others = [n for n in firm_state.news_feed if n.priority != "HIGH"]
            # Keep newest HIGH items (max 100) + newest others (200 max)
            firm_state.news_feed = (highs[-100:] + others[-200:])
        log.debug(
            "News [%s|%s|%s] %.2f — %s",
            item.source, item.category, item.priority,
            item.sentiment, item.headline[:60],
        )
        # Fast-track: urgent news jumps to UI immediately via /ws (delta update)
        try:
            if getattr(item, "urgency_tier", "T2") == "T0":
                await _ws_manager.broadcast({
                    "type": "news",
                    "tier": "T0",
                    "item": item.model_dump(mode="json"),
                    "ts": time.time(),
                })
        except Exception:
            pass


async def _run_one_cycle(ticker_override: str | None = None):
    """
    Execute a single agent cycle under the cycle lock.
    If ticker_override is set, temporarily switches the active ticker,
    runs the cycle, then restores the original ticker.
    """
    if _cycle_lock.locked():
        log.debug("Cycle already in progress — skipping")
        return
    async with _cycle_lock:
        cycle_err = None
        cycle_run_started = False
        original_ticker = firm_state.ticker
        if ticker_override:
            firm_state.ticker = ticker_override
        # Ensure underlying_price is current for the active ticker before running the graph.
        try:
            from agents.data.equity_snapshot import fetch_stock_quote

            q = await asyncio.to_thread(fetch_stock_quote, str(firm_state.ticker or "").upper())
            if isinstance(q, dict) and q.get("last") is not None:
                firm_state.underlying_price = float(q["last"])
        except Exception:
            pass

        agent_status.in_progress = True
        agent_status.last_cycle_started_at = time.time()
        try:
            # Optional MLflow parent run for this cycle (enables per-agent nested runs).
            try:
                from agents.tracking.mlflow_tracing import start_cycle_run
                cycle_run_started = bool(start_cycle_run(firm_state))
            except Exception:
                cycle_run_started = False

            result, cycle_err = await run_cycle_async(firm_state)
            for fld in FirmState.model_fields:
                setattr(firm_state, fld, getattr(result, fld))
            agent_status.cycles_total += 1
            if cycle_err is None:
                agent_status.cycles_ok += 1
                agent_status.last_success_at = time.time()
                agent_status.last_trader_decision = firm_state.trader_decision.value
                agent_status.last_error = ""
            else:
                agent_status.cycles_error += 1
                agent_status.last_error_at = time.time()
                agent_status.last_error = f"{type(cycle_err).__name__}: {cycle_err}"[:500]
                log.error("Agent cycle error: %s", cycle_err, exc_info=True)
                try:
                    log_cycle_failure(
                        f"{type(cycle_err).__name__}: {cycle_err}"[:4000],
                        ticker=firm_state.ticker,
                    )
                except Exception:
                    pass
            # Persist state after each cycle (success or partial after failure)
            try:
                from agents.state_persistence import save_state
                await asyncio.to_thread(save_state, firm_state)
            except Exception:
                pass
        except Exception as e:
            cycle_err = e
            agent_status.cycles_total += 1
            agent_status.cycles_error += 1
            agent_status.last_error_at = time.time()
            agent_status.last_error = f"{type(e).__name__}: {e}"[:500]
            log.error("Agent cycle error: %s", e, exc_info=True)
            try:
                log_cycle_failure(f"{type(e).__name__}: {e}"[:4000], ticker=firm_state.ticker)
            except Exception:
                pass
        finally:
            agent_status.in_progress = False
            agent_status.last_cycle_finished_at = time.time()
            agent_status.last_cycle_duration_s  = max(
                0.0, agent_status.last_cycle_finished_at - agent_status.last_cycle_started_at
            )
            try:
                if cycle_run_started:
                    from agents.tracking.mlflow_tracing import end_cycle_run
                    end_cycle_run(firm_state, cycle_err, agent_status.last_cycle_duration_s)
            except Exception:
                log.debug("MLflow cycle log skipped", exc_info=True)
            if ticker_override:
                firm_state.ticker = original_ticker


async def _cycle_task():
    """
    Agent reasoning cycle — defers to the tier system.
    Only runs when no T3 cycle has run recently (respects cooldown)
    and kill switch is off.  This prevents double-firing when the
    T3 watchdog already triggered a pipeline run.
    """
    from agents.tiers import T3_COOLDOWN_MINUTES
    while True:
        await asyncio.sleep(60)
        if firm_state.kill_switch_active:
            continue
        # Skip if a tier-3 cycle ran within the cooldown window
        if firm_state.last_tier3_run:
            from datetime import datetime, timezone
            elapsed = (datetime.now(timezone.utc) - firm_state.last_tier3_run).total_seconds() / 60
            if elapsed < T3_COOLDOWN_MINUTES:
                continue
        await _run_one_cycle()


async def _scanner_driven_cycle_task():
    """
    Watches scanner results for anomalous tickers and triggers agent cycles.
    Conditions: IV rank in top 15% of scanned tickers AND put/call ratio > 1.3.
    Avoids spamming: tracks last-triggered time per ticker (min 15 min gap).
    """
    _last_triggered: dict[str, float] = {}
    _MIN_GAP = 900.0   # 15 minutes between cycles on same ticker

    while True:
        await asyncio.sleep(120)   # check every 2 minutes
        try:
            if firm_state.kill_switch_active:
                continue
            scans = _scanner.get_all_scans(sort="iv")
            if not scans:
                continue

            # Compute IV 85th percentile threshold
            ivs = [s.get("avg_iv_30d", 0.0) or 0.0 for s in scans if not s.get("error")]
            if len(ivs) < 10:
                continue
            ivs_sorted = sorted(ivs, reverse=True)
            threshold = ivs_sorted[max(0, len(ivs_sorted) // 7)]  # top ~15%

            now = time.time()
            for scan in scans[:30]:   # only look at top 30 by IV
                ticker = scan.get("ticker", "")
                if not ticker or scan.get("error"):
                    continue
                iv_30d  = scan.get("avg_iv_30d", 0.0) or 0.0
                pc_ratio = scan.get("pc_ratio", 1.0) or 1.0

                if iv_30d < threshold:
                    break   # sorted by IV, safe to break

                # Skip if recently triggered or if it's the active ticker (main cycle covers it)
                last = _last_triggered.get(ticker, 0.0)
                if now - last < _MIN_GAP:
                    continue
                if ticker == firm_state.ticker:
                    continue

                if pc_ratio > 1.3:
                    log.info(
                        "Scanner-driven cycle: %s (IV=%.1f%%, P/C=%.2f)",
                        ticker, iv_30d * 100, pc_ratio,
                    )
                    _last_triggered[ticker] = now
                    await _run_one_cycle(ticker_override=ticker)
                    break  # one per check-in to avoid overwhelming the LLMs
        except Exception as e:
            log.debug("scanner_driven_cycle_task: %s", e)


async def _ws_broadcast_task():
    """Push state diffs to all WebSocket clients every 2 seconds."""
    while True:
        await asyncio.sleep(2)
        await _ws_manager.push_state()


async def _ws_quote_push_task():
    """
    Push active ticker quote to UI via /ws.
    Goal: remove the “wait for REST poll” feel on ticker switches.
    Rate limited (default 1s) to avoid hammering providers.
    """
    from agents.data.equity_snapshot import fetch_stock_quote

    min_s = float(os.getenv("WS_QUOTE_MIN_S", "1.0"))
    last_sent_t = ""
    last_sent_at = 0.0
    last_payload = None

    while True:
        await asyncio.sleep(0.10)
        t = (firm_state.ticker or "").upper().strip()
        if not t:
            continue
        now = time.time()
        if t == last_sent_t and (now - last_sent_at) < min_s:
            continue

        try:
            q = await asyncio.to_thread(fetch_stock_quote, t)
        except Exception:
            continue

        try:
            from agents.data.warehouse.postgres import enqueue_quote

            enqueue_quote(
                t,
                {
                    "bid": q.get("bid"),
                    "ask": q.get("ask"),
                    "last": q.get("last"),
                    "prev_close": q.get("prev_close"),
                    "change_pct": q.get("change_pct"),
                    "source": q.get("source"),
                },
            )
        except Exception:
            pass

        payload = {
            "type": "quote",
            "ticker": t,
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "last": q.get("last"),
            "prev_close": q.get("prev_close"),
            "change_pct": q.get("change_pct"),
            "source": q.get("source"),
            "session": q.get("session"),
            "trade_time": q.get("trade_time"),
            "ts": time.time(),
        }

        # Avoid spamming identical payloads
        if payload == last_payload and t == last_sent_t:
            last_sent_at = now
            continue

        last_payload = payload
        last_sent_t = t
        last_sent_at = now
        try:
            await _ws_manager.broadcast(payload)
        except Exception:
            pass


async def _scanner_task():
    """Continuously scans all S&P 500 tickers in the background."""
    await _scanner.run_forever()


async def _warm_fundamentals_cache_task():
    """
    Warm SQLite fundamentals cache in the background so UI clicks are instant.
    Rate-limited and low-concurrency to avoid hammering Yahoo/yfinance.
    """
    from agents.data.fundamentals import fetch_stock_info
    from agents.data.fundamentals_db import get_stock_info_cached, upsert_stock_info
    from agents.data.sp500 import SP500_TOP50

    # Delay a bit so startup critical paths (UI, state sync) aren't impacted.
    await asyncio.sleep(4)

    max_to_warm = int(float(os.getenv("FUNDAMENTALS_WARM_MAX", "40")))
    per_item_delay_s = float(os.getenv("FUNDAMENTALS_WARM_DELAY_S", "0.35"))

    def _candidate_tickers() -> list[str]:
        out: list[str] = []
        try:
            out.append(str(firm_state.ticker or "SPY").upper())
        except Exception:
            pass
        try:
            for p in list(getattr(firm_state, "stock_positions", []) or []):
                t = getattr(p, "ticker", None) or getattr(p, "symbol", None)
                if t:
                    out.append(str(t).upper())
        except Exception:
            pass
        try:
            for p in list(getattr(firm_state, "open_positions", []) or []):
                t = getattr(p, "ticker", None) or getattr(p, "symbol", None)
                if t:
                    out.append(str(t).upper())
        except Exception:
            pass
        try:
            out.extend([str(t).upper() for t in SP500_TOP50[: max(0, max_to_warm - len(out))]])
        except Exception:
            pass
        # De-dupe while preserving order
        dedup = []
        seen = set()
        for t in out:
            if not t or t in seen:
                continue
            seen.add(t)
            dedup.append(t)
        return dedup[:max_to_warm]

    tickers = _candidate_tickers()
    if not tickers:
        return

    warmed = 0
    for t in tickers:
        try:
            cached, fetched_at = await asyncio.to_thread(get_stock_info_cached, t)
            if cached and fetched_at:
                continue
            fresh = await asyncio.to_thread(fetch_stock_info, t)
            await asyncio.to_thread(upsert_stock_info, t, fresh)
            warmed += 1
        except Exception:
            pass
        await asyncio.sleep(per_item_delay_s)

    if warmed:
        log.info("Fundamentals cache warmed: %d tickers", warmed)


async def _warm_daily_bars_cache_task():
    """
    Download Yahoo **daily** OHLC for SP500_TOP50 into SQLite so /bars serves plots
    without a per-click network call (fixes flaky desktop WebView “Load failed”).
    """
    await asyncio.sleep(2)
    try:
        from agents.data.bars_cache_db import warm_sp500_top50_daily

        await asyncio.to_thread(warm_sp500_top50_daily)
    except Exception as e:
        log.warning("Daily bars cache warm skipped: %s", e)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load persisted state (positions, proposals, risk from last session)
    try:
        from agents.state_persistence import load_state
        loaded = await asyncio.to_thread(load_state)
        if loaded:
            for fld in FirmState.model_fields:
                try:
                    setattr(firm_state, fld, getattr(loaded, fld))
                except Exception:
                    pass
            log.info("Restored firm_state from disk (equity=%.2f)", firm_state.account_equity)
    except Exception as e:
        log.debug("state load skipped: %s", e)

    # Guardrails: drop expired greeks + mark expired recommendations after restore.
    try:
        from datetime import date as _date
        from agents.data.options_chain_filter import parse_greeks_expiry_str
        from agents.data.opra_client import occ_expiry_as_date

        today = _date.today()
        # latest_greeks: remove anything clearly expired
        cleaned = []
        for g in (firm_state.latest_greeks or []):
            sym = str(getattr(g, "symbol", "") or "").strip()
            exp_d = occ_expiry_as_date(sym) or parse_greeks_expiry_str(str(getattr(g, "expiry", "") or ""))
            if exp_d is not None and exp_d < today:
                continue
            cleaned.append(g)
        firm_state.latest_greeks = cleaned

        # open_positions: drop expired legs so agents never see them
        try:
            pos_clean = []
            for p in (firm_state.open_positions or []):
                exp_d = parse_greeks_expiry_str(str(getattr(p, "expiry", "") or ""))
                if exp_d is not None and exp_d < today:
                    continue
                pos_clean.append(p)
            firm_state.open_positions = pos_clean
        except Exception:
            pass

        # pending_proposal: clear if it contains any expired leg
        try:
            prop = getattr(firm_state, "pending_proposal", None)
            if prop and getattr(prop, "legs", None):
                bad = False
                for leg in prop.legs:
                    sym = str(getattr(leg, "symbol", "") or "").strip()
                    exp_d = occ_expiry_as_date(sym) or parse_greeks_expiry_str(str(getattr(leg, "expiry", "") or ""))
                    if exp_d is not None and exp_d < today:
                        bad = True
                        break
                if bad:
                    firm_state.pending_proposal = None
        except Exception:
            pass

        # pending_recommendations: remove any rec that contains expired legs
        try:
            recs = list(firm_state.pending_recommendations or [])
            kept = []
            for r in recs:
                legs = (r.proposal.legs or []) if getattr(r, "proposal", None) else []
                expired = False
                for leg in legs:
                    sym = str(getattr(leg, "symbol", "") or "").strip()
                    exp_d = occ_expiry_as_date(sym) or parse_greeks_expiry_str(str(getattr(leg, "expiry", "") or ""))
                    if exp_d is not None and exp_d < today:
                        expired = True
                        break
                if not expired:
                    kept.append(r)
            firm_state.pending_recommendations = kept
        except Exception:
            pass

        # Persist the cleaned snapshot so old expired contracts don't keep returning.
        try:
            from agents.state_persistence import save_state

            await asyncio.to_thread(save_state, firm_state)
        except Exception:
            pass
    except Exception as e:
        log.debug("post-restore expiry cleanup skipped: %s", e)

    # Optional PostgreSQL warehouse: durable copy of pulled data via background queue (SQLite stays UI L1).
    try:
        from agents.data.warehouse import (
            ensure_schema,
            is_postgres_enabled,
            start_warehouse_writer,
        )

        wh_auto = os.getenv("WAREHOUSE_AUTO_SCHEMA", "1").strip().lower() not in ("0", "false", "no")
        if is_postgres_enabled():
            if wh_auto:
                try:
                    await asyncio.to_thread(ensure_schema)
                except Exception as e:
                    log.warning("PostgreSQL warehouse schema ensure failed: %s", e)
            start_warehouse_writer()
            log.info("PostgreSQL warehouse writer started (non-blocking queue)")
    except Exception as e:
        log.debug("warehouse startup skipped: %s", e)

    # Restore portfolio chart from SQLite, or seed one point from current risk
    try:
        from agents.data.portfolio_history_db import load_portfolio_points

        loaded_pts = await asyncio.to_thread(load_portfolio_points, 2000)
        if loaded_pts:
            _PORTFOLIO_HISTORY.clear()
            for p in loaded_pts:
                _PORTFOLIO_HISTORY.append(p)
        else:
            r0 = firm_state.risk
            _PORTFOLIO_HISTORY.append({
                "time": time.time(),
                "equity": float(r0.current_nav or r0.opening_nav or 0.0),
                "delta": float(r0.portfolio_delta),
                "vega": float(r0.portfolio_vega),
                "daily_pnl": float(r0.daily_pnl),
                "drawdown_pct": float(r0.drawdown_pct),
            })
    except Exception as exc:
        log.debug("portfolio history restore: %s", exc)

    # ── Probe local llama.cpp and log the active LLM backend ──────────────────
    await _probe_llama_cpp()

    # ── Start Tier-1/2 background loops (sentiment monitor + movement tracker
    #    + fundamentals refresher + T3 auto-trigger watchdog) ─────────────────
    from agents.tiers import start_tier_loops
    await start_tier_loops(firm_state)
    log.info(
        "Agent tier loops started (T1: SentimentMonitor/structured LLM + MovementTracker; "
        "T2: Fundamentals + NewsProcessor)"
    )

    from agents.research.universe_service import start_universe_research
    start_universe_research(firm_state, _scanner)
    log.info("Universe research service started (top-50 briefs + dirty queue)")

    tasks = [
        asyncio.create_task(_tick_ingestion_task()),
        asyncio.create_task(_greeks_update_task()),
        asyncio.create_task(_position_monitor_task()),
        asyncio.create_task(_cycle_task()),
        asyncio.create_task(_scanner_task()),
        asyncio.create_task(_scanner_driven_cycle_task()),
        asyncio.create_task(_portfolio_history_task()),
        asyncio.create_task(_equity_sync_task()),
        asyncio.create_task(_ws_broadcast_task()),
        asyncio.create_task(_ws_quote_push_task()),
        asyncio.create_task(_warm_fundamentals_cache_task()),
        asyncio.create_task(_warm_daily_bars_cache_task()),
    ]
    if ENABLE_NEWS_FEED:
        tasks.insert(1, asyncio.create_task(_news_task()))

    # Initial account sync — populate positions before the first UI request arrives
    try:
        from agents.data.equity_snapshot import sync_alpaca_account_into_state
        ok = await asyncio.to_thread(sync_alpaca_account_into_state, firm_state, True)
        if not ok:
            log.warning("Initial Alpaca sync skipped (no API key or TTL block).")
    except Exception as exc:
        log.warning("Initial Alpaca sync error: %s", exc)

    yield
    # Shutdown: persist firm state, then warehouse writer, universe workers, tier loops, cancel tasks
    try:
        await _persist_firm_state()
    except Exception as exc:
        log.debug("shutdown persist firm_state: %s", exc)
    try:
        from agents.data.warehouse import stop_warehouse_writer

        stop_warehouse_writer()
    except Exception as exc:
        log.debug("stop_warehouse_writer: %s", exc)
    try:
        from agents.research.universe_service import stop_universe_research
        stop_universe_research()
    except Exception as exc:
        log.debug("stop_universe_research: %s", exc)
    from agents.tiers import stop_tier_loops
    stop_tier_loops()
    for t in tasks:
        t.cancel()


app = FastAPI(title="Agentic Trading Terminal API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Helps verify you are hitting this app (if you see 404 on other paths, restart the server)."""
    return {
        "service": "agentic-trading-terminal",
        "docs": "/docs",
        "endpoints": [
            "/state", "/news", "/reasoning_log", "/agent_status",
            "/scanner", "/scanner/quotes", "/scanner/tickers", "/options/{ticker}",
            "/bars/{ticker}", "/quote/{ticker}", "/stock_info/{ticker}", "/perception/{ticker}",
            "/portfolio_series",
            "/set_ticker", "/kill_switch", "/run_cycle",
            "/order/stock", "/order/option", "/orders", "/order/{id}",
            "/positions/refresh", "/positions/debug", "/market/clock",
            "/llm/status",
            "/research/{ticker}", "/research/universe/summary", "/research/refresh/{ticker}",
            "/ws/market",
        ],
    }


@app.websocket("/ws/market")
async def websocket_market_feed(websocket: WebSocket):
    """
    Alpaca-backed trades + quotes for the embedded L2/L3 trading-viz panel.

    Uses **one** shared Alpaca stream for all browser tabs (per api_server process) to stay
    within Alpaca's concurrent WebSocket limit.

    - Query ``?symbol=SPY`` (defaults to ``firm_state.ticker``).
    - Client may send ``{\"symbol\": \"AAPL\"}`` to set ``firm_state.ticker`` and resync the hub.
    - Real-time Alpaca data only when the focus ticker is in the top-50 universe (see ``agents/data/sp500.py``).
    """
    import queue as _queue

    from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
    from agents.data import market_activity
    from agents.data.alpaca_market_bridge import get_market_hub

    await websocket.accept()
    qs = websocket.query_params.get("symbol")
    if qs and str(qs).strip():
        sym = str(qs).upper().strip()
        market_activity.touch(sym)
        firm_state.ticker = sym
    else:
        sym = (firm_state.ticker or "SPY").upper().strip()

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        await websocket.send_json(
            {
                "channel": "error",
                "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY for live market data.",
            }
        )
        await websocket.close(code=1008)
        return

    hub = get_market_hub()
    out_q: _queue.Queue = _queue.Queue(maxsize=2000)

    try:
        hub.register(out_q, sym)
    except Exception as e:
        log.exception("Alpaca market hub failed to register")
        await websocket.send_json({"channel": "error", "message": str(e)})
        await websocket.close(code=1011)
        return

    async def pump_out() -> None:
        while True:
            try:
                while True:
                    item = out_q.get_nowait()
                    await websocket.send_json(item)
            except _queue.Empty:
                pass
            await asyncio.sleep(0.02)

    async def pump_in() -> None:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                continue
            if isinstance(raw, dict):
                ns = raw.get("symbol")
                if isinstance(ns, str) and ns.strip():
                    t = ns.strip().upper()
                    market_activity.touch(t)
                    firm_state.ticker = t
                    await asyncio.to_thread(hub.resubscribe_on_focus_change, t)

    pump_task = asyncio.create_task(pump_out())
    try:
        await pump_in()
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        hub.unregister(out_q)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Real-time state push via WebSocket.
    The server pushes a compact state diff every 2 s (see _ws_broadcast_task).
    The client can also send {"type":"ping"} to verify connectivity.
    UI should connect here first and fall back to polling /state if unavailable.
    """
    await _ws_manager.connect(ws)
    try:
        # Send current state immediately on connect
        await _ws_manager.push_state()
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        _ws_manager.disconnect(ws)
    except Exception:
        _ws_manager.disconnect(ws)


@app.get("/state")
async def get_state():
    """FirmState plus `agent_runtime` for UI (agent loop health without a second request)."""
    payload = firm_state.model_dump()
    payload["agent_runtime"] = agent_status.to_dict()
    payload["news_feed_enabled"] = ENABLE_NEWS_FEED
    return payload


@app.get("/news")
async def get_news(limit: int = Query(80, ge=1, le=300)):
    """
    Latest headlines for the UI.
    Sorted strictly by published_at (newest first).
    """
    items = list(firm_state.news_feed)
    items.sort(key=lambda n: n.published_at.timestamp() if getattr(n, "published_at", None) else 0, reverse=True)
    return [n.model_dump() for n in items[:limit]]


@app.get("/news/queue")
async def get_news_queue(limit: int = Query(20, ge=1, le=200)):
    """
    Snapshot of the NewsPriorityQueue (FinBERT-scored backlog).
    Shows queue stats + the highest-priority items currently waiting.
    """
    try:
        from agents.data.news_priority_queue import get_queue

        q = get_queue()
        top = q.peek_top(limit)
        return {
            "stats": q.stats(),
            "items": [
                {
                    "id": qn.id,
                    "priority": round(qn.priority, 4),
                    "added_at": qn.added_at.isoformat(),
                    "seen": sorted(list(qn.seen)),
                    "item": qn.item.model_dump(mode="json")
                    if hasattr(qn.item, "model_dump")
                    else {"headline": getattr(qn.item, "headline", "")},
                }
                for qn in top
            ],
        }
    except Exception as exc:
        return {"error": str(exc), "stats": {}, "items": []}


@app.get("/research/{ticker}")
async def get_research_ticker(ticker: str):
    """Cached per-ticker agent brief (universe precompute)."""
    from agents.research.store import get_brief

    b = get_brief(ticker)
    if not b:
        raise HTTPException(status_code=404, detail="No research row for ticker yet")
    return b.model_dump(mode="json")


@app.get("/research/universe/summary")
async def get_research_universe_summary():
    """All tickers with freshness, dirty flags, and priority (top-50 universe)."""
    from agents.research.store import list_universe_summaries

    return [r.model_dump(mode="json") for r in list_universe_summaries()]


@app.post("/research/refresh/{ticker}")
async def post_research_refresh(ticker: str):
    """Enqueue a manual refresh (raises priority)."""
    from agents.research.store import set_dirty

    set_dirty(ticker.upper(), ["manual_refresh"], priority_boost=10.0)
    return {"ok": True, "ticker": ticker.upper()}


@app.get("/reasoning_log")
async def reasoning_log(
    tail: int = Query(500, ge=1, le=5000),
    agent: str | None = Query(
        None,
        description="If set, only rows where the `agent` field matches exactly (e.g. DeskHead, BullResearcher).",
        max_length=160,
    ),
):
    """Recent reasoning rows from today's JSONL (default last 500). Optional per-agent filter."""
    a = agent.strip() if agent else None
    return get_today_log(tail=tail, agent=a or None)

@app.get("/agent_status")
async def get_agent_status():
    """
    Lightweight runtime view of the agent loop:
    - in_progress, last cycle start/finish, last success, last error, counters.
    """
    return agent_status.to_dict()


@app.get("/scanner")
async def get_scanner(
    sort: str = Query("iv", pattern="^(iv|pc|oi|ticker|price|chg)$"),
):
    """
    Cached options metrics per ticker, merged with **live** stock quotes (Alpaca
    batch snapshot when keys are configured). Falls back to ATM strike estimate
    for price when a quote is missing.

    sort: iv | pc | oi | ticker | price (last) | chg (day %% change, desc)
    """
    rows = _scanner.get_scan_rows()
    if rows:
        tickers = [r["ticker"] for r in rows]
        quotes = await asyncio.to_thread(fetch_stock_quotes_batch, tickers)
        for r in rows:
            t = r["ticker"].upper()
            q = quotes.get(t)
            if q:
                r["last"] = q.get("last")
                r["change_pct"] = q.get("change_pct")
                r["quote_source"] = q.get("source")
                r["quote_session"] = q.get("session")
    sort_scan_rows(rows, sort)
    return rows


@app.get("/scanner/quotes")
async def get_scanner_quotes():
    """
    Batch live quotes for tickers currently in the scanner cache — small payload,
    same Alpaca path as `/scanner`, for ~1 Hz UI refresh without re-pulling IV/OI.
    """
    rows = _scanner.get_scan_rows()
    if not rows:
        return {"quotes": {}}
    tickers = [r["ticker"] for r in rows]
    batch = await asyncio.to_thread(fetch_stock_quotes_batch, tickers)
    out: dict = {}
    for t in tickers:
        u = t.upper()
        q = batch.get(u)
        if q:
            out[u] = {
                "last": q.get("last"),
                "change_pct": q.get("change_pct"),
                "quote_source": q.get("source"),
                "session": q.get("session"),
            }
    return {"quotes": out}


@app.get("/scanner/tickers")
async def get_scanner_tickers():
    """Full list of tracked S&P 500 tickers."""
    return {"tickers": _scanner.all_tickers(), "count": len(_scanner.all_tickers())}


@app.get("/quotes/benchmarks")
async def get_benchmark_quotes():
    """
    Batch equity quotes for the index / sector / macro ETF prefix (see
    ``agents.data.sp500.INDEX_SECTOR_ETF_TICKERS``). Used by the Atlas context strip.

    Returns ``sections`` (grouped labels + quotes) and flat ``items`` (backward compatible).
    """
    from agents.data.sp500 import BENCHMARK_SCANNER_SECTIONS, INDEX_SECTOR_ETF_TICKERS

    tickers = list(INDEX_SECTOR_ETF_TICKERS)
    if not tickers:
        return {"items": [], "sections": []}
    batch = await asyncio.to_thread(fetch_stock_quotes_batch, tickers)

    def _one(u: str) -> dict:
        q = batch.get(u) if isinstance(batch, dict) else None
        if isinstance(q, dict):
            return {
                "ticker":       u,
                "last":         q.get("last"),
                "change_pct":   q.get("change_pct"),
                "quote_source": q.get("source"),
                "session":      q.get("session"),
            }
        return {"ticker": u, "last": None, "change_pct": None, "quote_source": None, "session": None}

    items: list[dict] = [_one(t.upper()) for t in tickers]
    sections: list[dict] = []
    for sec in BENCHMARK_SCANNER_SECTIONS:
        st = sec.get("tickers") or []
        if not isinstance(st, list):
            continue
        sections.append({
            "id":    sec.get("id", ""),
            "label": sec.get("label", ""),
            "items": [_one(str(x).upper()) for x in st],
        })
    return {"items": items, "sections": sections}


_ALLOWED_BAR_TF = frozenset({
    "1D",
    "1Day", "1Hour", "15Min", "5Min", "1Min",
    "5D", "1M", "3M", "6M", "1Y", "2Y", "5Y", "MAX",
})


@app.get("/market/clock")
async def market_clock():
    """
    Proxy Alpaca's /v2/clock to get real-time market open/close status.
    Also checks US market holidays (via Alpaca, which knows about them).
    Returns: { is_open, next_open, next_close, timestamp }
    """
    import os, httpx as _hx
    key    = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    base   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not key or not secret:
        return {"is_open": None, "error": "no_api_key"}
    try:
        async with _hx.AsyncClient(timeout=6.0) as cli:
            r = await cli.get(
                f"{base}/v2/clock",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"is_open": None, "error": str(e)}


@app.get("/llm/status")
async def llm_status():
    """
    Reports LLM backend status including the load-balanced server pool.
    """
    from agents.llm_retry import get_llm_backend_status
    from agents.llm_local import server_pool

    status = get_llm_backend_status()
    status["pool"] = server_pool.status()
    status["pool_healthy"] = server_pool.healthy_count
    status["pool_total"] = len(server_pool.all_urls)
    return status


@app.get("/perception/{ticker}")
async def get_perception(ticker: str):
    """
    Phases 0–2 perception bundle: technical, fundamental, events, sentiment, news.
    Uses the in-memory news feed for sentiment/news layers. Persists to ``cache/perception.sqlite3``.
    """
    from agents.perception.pipeline import run_perception_bundle

    t = ticker.upper().strip()
    bundle = await asyncio.to_thread(
        run_perception_bundle,
        t,
        list(firm_state.news_feed),
    )
    return bundle.model_dump(mode="json")


@app.get("/quote/{ticker}")
async def get_quote(ticker: str):
    """Stock NBBO / last trade / vs prev close — for the equity quote strip."""
    from agents.data.equity_snapshot import fetch_stock_quote

    q = await asyncio.to_thread(fetch_stock_quote, ticker.upper())
    if q.get("source") == "none" and firm_state.ticker.upper() == ticker.upper():
        lp = float(firm_state.underlying_price or 0)
        if lp > 0:
            q = {
                "ticker": ticker.upper(),
                "bid": None,
                "ask": None,
                "last": lp,
                "prev_close": None,
                "change_pct": None,
                "source": "underlying_proxy",
                "session": None,
                "trade_time": None,
            }
    return q


@app.get("/stock_info/{ticker}")
async def get_stock_info(ticker: str):
    """
    Fundamental data + peer/competitor/dependency map for the given ticker.
    Data sourced from yfinance (free) and cached in SQLite.
    Behavior:
      - Return cached payload immediately when available (fast UI clicks).
      - If cached payload is stale (dynamic TTL), refresh in background.
      - If missing, fetch now and persist.
    """
    from agents.data.fundamentals import fetch_stock_info
    from agents.data.fundamentals_db import get_stock_info_cached, upsert_stock_info

    t = ticker.upper().strip()
    # Description, peers, ecosystem maps, and most fundamentals change rarely.
    # SQLite already stores the full payload; this TTL controls how often we hit yfinance
    # for background refresh (default 7 days). Lower for more frequent P/E etc.
    dynamic_ttl_s = int(float(os.getenv("FUNDAMENTALS_DYNAMIC_TTL_S", "604800")))

    cached, fetched_at = await asyncio.to_thread(get_stock_info_cached, t)
    now = int(time.time())

    # Serve cached immediately when present.
    if cached and fetched_at:
        age = max(0, now - int(fetched_at))

        # Stale-while-revalidate: refresh in background, but don't block UI.
        if age >= dynamic_ttl_s:
            if not hasattr(get_stock_info, "_refreshing"):
                get_stock_info._refreshing = set()  # type: ignore[attr-defined]
            refreshing = get_stock_info._refreshing  # type: ignore[attr-defined]
            if t in refreshing:
                return cached
            refreshing.add(t)

            async def _refresh():
                try:
                    fresh = await asyncio.to_thread(fetch_stock_info, t)
                    await asyncio.to_thread(upsert_stock_info, t, fresh)
                except Exception:
                    pass
                finally:
                    try:
                        refreshing.discard(t)
                    except Exception:
                        pass

            try:
                asyncio.create_task(_refresh())
            except Exception:
                pass

        return cached

    # Cache miss:
    # Default to "fast miss" so UI clicks feel instant even when yfinance is slow.
    # The UI already handles "pending" responses by retrying shortly after.
    fast_miss = (os.getenv("FUNDAMENTALS_FAST_MISS", "true") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if fast_miss:
        if not hasattr(get_stock_info, "_refreshing"):
            get_stock_info._refreshing = set()  # type: ignore[attr-defined]
        refreshing = get_stock_info._refreshing  # type: ignore[attr-defined]
        if t not in refreshing:
            refreshing.add(t)

            async def _refresh_missing():
                try:
                    fresh = await asyncio.to_thread(fetch_stock_info, t)
                    await asyncio.to_thread(upsert_stock_info, t, fresh)
                except Exception:
                    pass
                finally:
                    try:
                        refreshing.discard(t)
                    except Exception:
                        pass

            try:
                asyncio.create_task(_refresh_missing())
            except Exception:
                refreshing.discard(t)

        return {
            "ticker": t,
            "name": t,
            "description": "Loading fundamentals…",
            "sector": "",
            "industry": "",
            "data_source": "pending",
        }

    # Legacy behavior (if FUNDAMENTALS_FAST_MISS=false): block until we have a real payload.
    try:
        fresh = await asyncio.to_thread(fetch_stock_info, t)
        await asyncio.to_thread(upsert_stock_info, t, fresh)
        return fresh
    except Exception as exc:
        log.warning("stock_info initial fetch failed for %s: %s", t, exc)
        return {
            "ticker": t,
            "name": t,
            "description": "Fundamentals unavailable (yfinance/network error). Retry or check logs.",
            "sector": "",
            "industry": "",
            "data_source": "error",
        }


@app.get("/bars/{ticker}")
async def get_bars(
    ticker: str,
    timeframe: str = Query("5D"),
    limit: int = Query(120, ge=10, le=10000),
    bust: bool = Query(False),
):
    """
    Underlying **stock** OHLC for the price chart (not options).

    Timeframes: intraday (1Min…1Day bar size) or multi-session history (5D, 1M, 3M, 6M, 1Y daily).
    Response includes `underlying` summary (last, range, period change, volume when available).
    Pass `?bust=true` to bypass the short-lived server cache.

    Historic source: ``CHART_HISTORY_PREFERENCE`` in ``chart_data`` (default: Yahoo first to spare
    Alpaca limits; use ``alpaca_first`` for broker-quality history). Real-time quotes use ``/quote`` / WS.
    """
    from agents.data.chart_data import fetch_bars, summary_from_bars, _bars_cache

    tf = timeframe.strip()
    if tf not in _ALLOWED_BAR_TF:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {sorted(_ALLOWED_BAR_TF)}",
        )

    if bust:
        key = (ticker.upper(), tf, int(limit))
        _bars_cache.pop(key, None)

    bars, source = await asyncio.to_thread(
        lambda: fetch_bars(ticker.upper(), tf, limit, bypass_disk=bust),
    )
    summary = summary_from_bars(bars, ticker.upper())
    return {
        "ticker": ticker.upper(),
        "timeframe": tf,
        "source": source,
        "count": len(bars),
        "bars": bars,
        "underlying": summary,
    }


@app.get("/portfolio_series")
async def get_portfolio_series(
    points: int = Query(200, ge=10, le=2000),
):
    """Time series for portfolio / greeks charts (sampled server-side ~20s)."""
    lst = list(_PORTFOLIO_HISTORY)
    if len(lst) > points:
        lst = lst[-points:]
    return {"count": len(lst), "points": lst}


@app.get("/options/{ticker}")
async def get_options(ticker: str):
    """
    Returns the options chain for `ticker`.
    Checks the scanner cache first; triggers a fresh drilldown fetch if stale.
    """
    t = ticker.upper()
    chain = _scanner.get_chain(t)
    if not chain:
        chain = await _scanner.fetch_drilldown(t)

    # Max DTE + asymmetric strikes (same rules as ``filter_greeks_for_agents``). When UI env
    # vars are unset, use agent defaults so behaviour matches the graph for every ticker.
    from agents.data.options_chain_filter import (
        agent_options_max_dte_days,
        agent_options_strike_band_pct,
        strike_bounds_for_contract,
    )

    try:
        raw_d = os.getenv("OPTIONS_MAX_DTE_DAYS", "").strip()
        days = int(float(raw_d)) if raw_d else agent_options_max_dte_days()
    except Exception:
        days = agent_options_max_dte_days()
    try:
        raw_b = os.getenv("OPTIONS_STRIKE_PCT_BAND", "").strip()
        strike_pct = float(raw_b) if raw_b else agent_options_strike_band_pct()
        strike_pct = max(0.05, min(0.95, strike_pct))
    except Exception:
        strike_pct = agent_options_strike_band_pct()

    # Spot for strike bands: **live equity quote for this ticker** first (works for any symbol),
    # then desk state / scanner / option-delta fallback.
    spot = 0.0
    try:
        from agents.data.equity_snapshot import fetch_stock_quote

        q = await asyncio.to_thread(fetch_stock_quote, t)
        last = q.get("last")
        if last is None:
            bid, ask = q.get("bid"), q.get("ask")
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                last = (float(bid) + float(ask)) / 2.0
        if last is not None and float(last) > 0:
            spot = float(last)
    except Exception:
        pass
    if spot <= 0:
        try:
            if firm_state.ticker and firm_state.ticker.upper() == t:
                spot = float(firm_state.underlying_price or 0.0)
        except Exception:
            spot = 0.0
    if spot <= 0:
        try:
            scan = _scanner.get_scan(t) or {}
            spot = float(scan.get("underlying_price") or 0.0)
        except Exception:
            spot = 0.0
    if spot <= 0 and chain:
        # Fallback: estimate underlying from contract closest to |delta|≈0.5
        try:
            best = None
            best_diff = 9e9
            for c in chain:
                k = c.get("strike")
                d = c.get("delta")
                if k is None or d is None:
                    continue
                strike = float(k)
                if strike <= 0:
                    continue
                diff = abs(abs(float(d)) - 0.5)
                if diff < best_diff:
                    best_diff = diff
                    best = strike
            if best and best > 0:
                spot = float(best)
        except Exception:
            pass

    today = date.today()

    def _parse_expiry(s: str) -> date | None:
        try:
            s = (s or "").strip()
            if len(s) == 6:  # YYMMDD
                return date(int("20" + s[:2]), int(s[2:4]), int(s[4:6]))
            if len(s) == 8:  # YYYYMMDD
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except Exception:
            return None
        return None

    filtered = []
    for c in chain or []:
        try:
            exp = _parse_expiry(str(c.get("expiry") or ""))
            if exp:
                dte = (exp - today).days
                if dte < 0 or dte > days:
                    continue
            # If expiry missing/unparseable, keep it (avoid hiding data due to schema mismatch)
            k = c.get("strike")
            strike = float(k) if k is not None else None
            occ = str(c.get("symbol") or "").strip()
            low, high = strike_bounds_for_contract(
                c.get("right"), spot, strike_pct, occ_symbol=occ or None
            )
            if strike is not None and low is not None and high is not None:
                if strike < low or strike > high:
                    continue
            filtered.append(c)
        except Exception:
            continue

    # Hard cap payload for UI perf (client also caps, but server cap saves bandwidth).
    try:
        max_contracts = int(float(os.getenv("OPTIONS_MAX_CONTRACTS", "800")))
        max_contracts = max(200, min(3000, max_contracts))
    except Exception:
        max_contracts = 800
    if len(filtered) > max_contracts:
        try:
            filtered.sort(key=lambda c: (str(c.get("expiry") or ""), float(c.get("strike") or 0.0)))
        except Exception:
            pass
        filtered = filtered[:max_contracts]

    return {"ticker": t, "count": len(filtered), "contracts": filtered}


# ── Order request models ──────────────────────────────────────────────────────

class StockOrderRequest(BaseModel):
    ticker:      str
    side:        str          # "buy" | "sell"
    qty:         float
    order_type:  str = "market"   # "market" | "limit"
    limit_price: float | None = None
    tif:         str = "day"

class OptionOrderRequest(BaseModel):
    symbol:      str          # OCC option symbol, e.g. "SPY240119C00480000"
    side:        str          # "buy" | "sell"
    qty:         int
    order_type:  str = "limit"
    limit_price: float | None = None
    tif:         str = "day"

_ems = None   # lazy singleton to avoid import at startup

def _get_ems():
    global _ems
    if _ems is None:
        from agents.execution.ems import ExecutionManagementSystem
        _ems = ExecutionManagementSystem()
    return _ems


@app.post("/order/stock")
async def place_stock_order(req: StockOrderRequest):
    """Place a market or limit stock order via the configured broker (paper/live)."""
    if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
        raise HTTPException(status_code=403, detail="Kill switch / circuit breaker active")
    if req.side.lower() not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail="side must be 'buy' or 'sell'")
    if req.order_type.lower() == "limit" and req.limit_price is None:
        raise HTTPException(status_code=422, detail="limit_price required for limit orders")
    result = await asyncio.to_thread(
        _get_ems().place_stock_order,
        req.ticker, req.side, req.qty, req.order_type, req.limit_price, req.tif,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    # Trigger position refresh after order so UI reflects the change quickly
    asyncio.create_task(_post_order_sync())
    return result


@app.post("/order/option")
async def place_option_order(req: OptionOrderRequest):
    """Place a single-leg option order via the configured broker (paper/live)."""
    if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
        raise HTTPException(status_code=403, detail="Kill switch / circuit breaker active")
    if req.side.lower() not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail="side must be 'buy' or 'sell'")
    if req.order_type.lower() == "limit" and req.limit_price is None:
        raise HTTPException(status_code=422, detail="limit_price required for limit orders")
    result = await asyncio.to_thread(
        _get_ems().place_option_order,
        req.symbol, req.side, req.qty, req.order_type, req.limit_price, req.tif,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    # Trigger position refresh after order so UI reflects the change quickly
    asyncio.create_task(_post_order_sync())
    return result


@app.get("/orders")
async def get_orders(limit: int = Query(20, ge=1, le=100)):
    """Recent orders from the broker (paper or live). Returns empty list if no broker key."""
    rows = await asyncio.to_thread(_get_ems().get_orders, limit)
    return rows


@app.post("/positions/refresh")
async def refresh_positions():
    """
    Force an immediate account + position sync from the broker, bypassing the TTL cache.
    Call this after placing an order if you need instant position feedback.
    Returns updated stock_positions, open_positions, and account balances.
    """
    from agents.data.equity_snapshot import sync_alpaca_account_into_state
    ok = await asyncio.to_thread(sync_alpaca_account_into_state, firm_state, True)
    return {
        "synced":          ok,
        "stock_positions": [p.model_dump() for p in firm_state.stock_positions],
        "open_positions":  [p.model_dump() for p in firm_state.open_positions],
        "cash_balance":    firm_state.cash_balance,
        "buying_power":    firm_state.buying_power,
        "account_equity":  firm_state.account_equity,
    }


@app.get("/positions/debug")
async def debug_positions():
    """
    Raw Alpaca positions response + current firm_state positions.
    Useful for diagnosing why positions may not appear in the UI.
    """
    import os, httpx as _hx
    key    = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    base   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    raw_positions: list | str = "no_api_key"
    if key and secret:
        try:
            async with _hx.AsyncClient(timeout=8.0) as cli:
                r = await cli.get(
                    f"{base}/v2/positions",
                    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                )
            raw_positions = r.json()
        except Exception as e:
            raw_positions = str(e)
    return {
        "alpaca_raw_positions":  raw_positions,
        "state_stock_positions": [p.model_dump() for p in firm_state.stock_positions],
        "state_open_positions":  [p.model_dump() for p in firm_state.open_positions],
        "cash_balance":          firm_state.cash_balance,
        "buying_power":          firm_state.buying_power,
        "account_equity":        firm_state.account_equity,
    }


@app.delete("/order/{order_id}")
async def cancel_order(order_id: str):
    """Cancel a specific open order by its broker order ID."""
    result = await asyncio.to_thread(_get_ems().cancel_order, order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result


class TickerRequest(BaseModel):
    ticker: str

class OptionRightsRequest(BaseModel):
    rights: str  # CALL | PUT | BOTH


class OptionStructuresRequest(BaseModel):
    structures: list[str]


class RiskLimitsRequest(BaseModel):
    max_drawdown_pct: float | None = None  # percent (0-100) in UI
    position_cap_pct: float | None = None  # percent (0-100) in UI

@app.post("/set_ticker")
async def set_ticker(req: TickerRequest):
    from agents.data import market_activity
    from agents.data.alpaca_market_bridge import get_market_hub

    t = req.ticker.upper()
    market_activity.touch(t)
    firm_state.ticker = t
    # Always refresh spot on ticker switch so chain filters + agents start from real stock price.
    try:
        from agents.data.equity_snapshot import fetch_stock_quote

        q = await asyncio.to_thread(fetch_stock_quote, t)
        if isinstance(q, dict) and q.get("last") is not None:
            firm_state.underlying_price = float(q["last"])
    except Exception:
        pass
    try:
        get_market_hub().resubscribe_on_focus_change(t)
    except Exception as e:
        log.debug("market hub focus: %s", e)
    # Pre-warm the drilldown cache for the new ticker
    asyncio.create_task(_scanner.fetch_drilldown(t))
    return {"ticker": firm_state.ticker}


@app.post("/set_option_rights")
async def set_option_rights(req: OptionRightsRequest):
    """
    Persist a user preference restricting what option rights are considered for new proposals.
    Values: CALL | PUT | BOTH.
    """
    val = (req.rights or "").strip().upper()
    if val not in ("CALL", "PUT", "BOTH"):
        raise HTTPException(status_code=400, detail={"error": "rights must be CALL|PUT|BOTH"})
    firm_state.allowed_option_rights = val
    try:
        from agents.state_persistence import save_state

        await asyncio.to_thread(save_state, firm_state)
    except Exception:
        pass
    return {"ok": True, "allowed_option_rights": firm_state.allowed_option_rights}


@app.post("/set_option_structures")
async def set_option_structures(req: OptionStructuresRequest):
    """
    Persist a user preference restricting what option *structures* are considered for new proposals.
    Values: ALL | SINGLE | VERTICAL | IRON_CONDOR | CALENDAR.
    """
    items = req.structures or []
    vals = []
    for x in items:
        v = str(x or "").strip().upper()
        if not v:
            continue
        vals.append(v)
    if not vals:
        vals = ["ALL"]
    allowed = {"ALL", "SINGLE", "VERTICAL", "IRON_CONDOR", "CALENDAR"}
    if any(v not in allowed for v in vals):
        raise HTTPException(status_code=400, detail={"error": f"structures must be subset of {sorted(list(allowed))}"})
    # If ALL is present, normalize to just ALL.
    if "ALL" in vals:
        vals = ["ALL"]
    firm_state.allowed_option_structures = vals
    try:
        from agents.state_persistence import save_state

        await asyncio.to_thread(save_state, firm_state)
    except Exception:
        pass
    return {"ok": True, "allowed_option_structures": firm_state.allowed_option_structures}


@app.post("/risk/limits")
async def set_risk_limits(req: RiskLimitsRequest):
    """
    User-provided risk inputs for RiskManager hard gates.
    Values are percentages in the UI (0-100). Stored as fractions (0-1).
    """
    r = firm_state.risk
    if req.max_drawdown_pct is not None:
        v = max(0.0, min(100.0, float(req.max_drawdown_pct)))
        r.max_drawdown_pct = v / 100.0
    if req.position_cap_pct is not None:
        v = max(0.0, min(100.0, float(req.position_cap_pct)))
        r.position_cap_pct = v / 100.0
    firm_state.risk = r
    try:
        from agents.state_persistence import save_state

        await asyncio.to_thread(save_state, firm_state)
    except Exception:
        pass
    return {
        "ok": True,
        "risk": {
            "max_drawdown_pct": firm_state.risk.max_drawdown_pct,
            "position_cap_pct": firm_state.risk.position_cap_pct,
        },
    }


@app.post("/state/reset")
async def reset_state():
    """
    Delete the persisted state file so the next server restart begins fresh.
    Does NOT affect the running server — restart required for a clean state.
    """
    from agents.state_persistence import delete_state
    ok = await asyncio.to_thread(delete_state)
    return {"deleted": ok, "note": "Restart server to begin with a clean state."}


@app.post("/kill_switch")
async def kill_switch():
    firm_state.kill_switch_active      = True
    firm_state.circuit_breaker_tripped = True
    log.error("SOFTWARE KILL SWITCH activated via API")
    return {"status": "killed", "timestamp": datetime.utcnow().isoformat()}


@app.post("/run_cycle")
async def run_cycle_endpoint():
    if firm_state.kill_switch_active:
        return {"error": "kill switch active"}
    if _cycle_lock.locked():
        return {"error": "cycle already in progress"}
    await _run_one_cycle()
    return {"trader_decision": firm_state.trader_decision.value}


@app.get("/tiers/status")
async def tiers_status_endpoint():
    """
    Returns live status for all three agent tiers plus current T1 signals.
    Used by the UI status bar and agent panel to show what's running.
    """
    from agents.tiers import tier_status
    return tier_status(firm_state)


@app.post("/tiers/trigger")
async def tiers_trigger_endpoint():
    """
    Manually fire the T3 research pipeline from the UI.
    Equivalent to POST /run_cycle but goes through the tier system
    so the trigger source is logged correctly.
    """
    if firm_state.kill_switch_active:
        return {"error": "kill switch active"}
    if firm_state.tier3_active:
        return {"error": "tier3 already running"}
    from agents.tiers import trigger_tier3
    asyncio.create_task(trigger_tier3(firm_state, source="manual"))
    return {"status": "tier3 triggered", "source": "manual"}


@app.get("/agents/flow")
async def agents_flow_endpoint():
    """How data moves through tiers and the T3 LangGraph (for UI diagram + integrations)."""
    from agents.agent_flow_spec import AGENT_FLOW_RESPONSE
    return AGENT_FLOW_RESPONSE


@app.get("/agents/mlflow_status")
async def agents_mlflow_status_endpoint():
    """Whether MLflow logging is active and where runs are stored (no secrets)."""
    from agents.tracking.mlflow_tracing import mlflow_status_dict
    return mlflow_status_dict()


# ── AI-Processed News & Cross-Stock Impacts ────────────────────────────────────

@app.get("/news/processed")
async def get_processed_news(limit: int = 50):
    """Return recent AI-processed articles with sentiment and impact analysis."""
    from agents.data.news_processor import get_processed_articles
    return get_processed_articles(limit)


@app.get("/news/impacts")
async def get_all_news_impacts():
    """Return the full cross-stock impact map."""
    from agents.data.news_processor import get_all_impacts
    return get_all_impacts()


@app.get("/news/impacts/{ticker}")
async def get_ticker_impact(ticker: str):
    """Return cross-stock impact data for a specific ticker."""
    from agents.data.news_processor import get_impact_for_ticker
    impact = get_impact_for_ticker(ticker)
    if not impact:
        return {"ticker": ticker.upper(), "total_impact": 0, "article_count": 0, "relationships": []}
    return impact


@app.get("/news/affecting/{ticker}")
async def get_news_affecting_ticker(ticker: str, limit: int = 20):
    """Return articles that mention or have cross-stock impact on a ticker."""
    from agents.data.news_processor import get_articles_affecting_ticker
    return get_articles_affecting_ticker(ticker, limit)


@app.get("/news/digests/{ticker}")
async def get_news_digests_for_ticker(ticker: str, limit: int = 12):
    """
    Return token-efficient LLM context for a ticker using a decaying lookback window.
    - recent (last 24–72h): primary context (digests)
    - week (2–7d): secondary context (digests)
    - rollup (8–30d): meta context (daily rollups)
    """
    from agents.data.news_processed_db import get_tiered_llm_context

    # Back-compat: `limit` roughly maps to (recent+week). Keep it simple.
    lim = max(4, min(30, int(limit)))
    ctx = get_tiered_llm_context(
        ticker,
        recent_hours=72,
        days_detail=7,
        days_rollup=30,
        limit_recent=min(10, max(4, lim // 2)),
        limit_week=min(16, max(4, lim)),
    )
    return ctx


# ── DB Explorer (SQLite + Postgres warehouse introspection) ────────────────────

@app.get("/db/sources")
async def db_sources():
    """List known data stores (SQLite files + optional Postgres warehouse)."""
    from agents.db_explorer import list_sources
    out = []
    for s in list_sources():
        d = {"key": s.key, "kind": s.kind, "label": s.label}
        if s.kind == "sqlite":
            d["path"] = s.target
            try:
                from pathlib import Path
                d["exists"] = Path(s.target).exists()
            except Exception:
                d["exists"] = None
        else:
            # never return secrets
            from agents.db_explorer import _postgres_redact_dsn
            d["dsn"] = _postgres_redact_dsn(s.target)
        out.append(d)
    return {"sources": out}


@app.get("/db/{source_key}/tables")
async def db_tables(source_key: str):
    """List tables in a source."""
    from agents.db_explorer import list_tables
    try:
        return list_tables(source_key)
    except KeyError:
        raise HTTPException(404, f"Unknown source: {source_key}")


@app.get("/db/{source_key}/table/{table}/schema")
async def db_table_schema(source_key: str, table: str):
    """Return columns + foreign keys for a table."""
    from agents.db_explorer import table_schema
    try:
        return table_schema(source_key, table)
    except KeyError:
        raise HTTPException(404, f"Unknown source: {source_key}")


@app.get("/db/{source_key}/table/{table}/rows")
async def db_table_rows(source_key: str, table: str, limit: int = 100, offset: int = 0):
    """Return sample rows for a table."""
    from agents.db_explorer import table_rows
    try:
        return table_rows(source_key, table, limit=limit, offset=offset)
    except KeyError:
        raise HTTPException(404, f"Unknown source: {source_key}")


@app.get("/db/{source_key}/graph")
async def db_graph(source_key: str):
    """Return a simple table relationship graph (FK edges)."""
    from agents.db_explorer import relationship_graph
    try:
        return relationship_graph(source_key)
    except KeyError:
        raise HTTPException(404, f"Unknown source: {source_key}")


# ── Trading Mode & Recommendations ─────────────────────────────────────────────

class _ModeBody(BaseModel):
    mode: str   # "advisory" | "autopilot"

@app.post("/mode")
async def set_mode(body: _ModeBody):
    if body.mode not in ("advisory", "autopilot"):
        raise HTTPException(400, f"Invalid mode: {body.mode!r}")
    firm_state.trading_mode = body.mode
    log.info("Trading mode changed to: %s", body.mode)
    return {"mode": firm_state.trading_mode}


@app.get("/mode")
async def get_mode():
    return {"mode": firm_state.trading_mode}


@app.get("/recommendations")
async def list_recommendations():
    # Enrich with live quotes (latest_greeks) so the UI can explain:
    # - why approvals may not execute (missing bid/ask → no mid → no limit price)
    # - where max_risk/target_return came from vs quote-based estimates
    def _mid(bid: float, ask: float) -> float | None:
        try:
            bid = float(bid)
            ask = float(ask)
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        return round((bid + ask) / 2.0, 2)

    greeks_by_symbol = {g.symbol: g for g in (firm_state.latest_greeks or [])}

    def _quote_for_symbol(sym: str) -> dict:
        g = greeks_by_symbol.get(sym)
        if not g:
            return {"symbol": sym, "bid": None, "ask": None, "mid": None, "age_s": None}
        m = _mid(g.bid, g.ask)
        try:
            age_s = max(0.0, (datetime.utcnow() - g.timestamp).total_seconds())
        except Exception:
            age_s = None
        return {
            "symbol": sym,
            "bid": round(float(g.bid), 2),
            "ask": round(float(g.ask), 2),
            "mid": m,
            "age_s": round(age_s, 1) if age_s is not None else None,
            "ts": g.timestamp.isoformat() if getattr(g, "timestamp", None) else None,
        }

    def _pricing_summary(rec_dict: dict) -> dict:
        from agents.data.opra_client import occ_expiry_as_date

        proposal = rec_dict.get("proposal") or {}
        legs = proposal.get("legs") or []
        today = date.today()

        def _build_quotes() -> tuple[list[dict], list[str], float]:
            leg_quotes: list[dict] = []
            missing: list[str] = []
            net_premium_mid = 0.0
            for leg in legs:
                sym = str(leg.get("symbol") or "")
                q = _quote_for_symbol(sym)
                side = str(leg.get("side") or "").upper()
                qty = int(leg.get("qty") or 1)
                exp_d = occ_expiry_as_date(sym)
                expired = exp_d is not None and exp_d < today
                leg_quotes.append({
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "right": leg.get("right"),
                    "strike": leg.get("strike"),
                    "expiry": leg.get("expiry"),
                    "bid": q.get("bid"),
                    "ask": q.get("ask"),
                    "mid": q.get("mid"),
                    "age_s": q.get("age_s"),
                    "expired": expired,
                    "occ_expiry": exp_d.isoformat() if exp_d else None,
                })
                m = q.get("mid")
                if m is None:
                    missing.append(sym)
                    continue
                # SELL collects premium (+), BUY pays (-)
                sign = 1.0 if side.startswith("S") else -1.0
                net_premium_mid += sign * float(m) * float(qty) * 100.0
            return leg_quotes, missing, net_premium_mid

        leg_quotes, missing, net_premium_mid = _build_quotes()

        # If quotes are missing, attempt a targeted snapshot prewarm (best-effort).
        # This reduces user-facing "—" when the rolling chain snapshot didn't include the exact leg strikes.
        need = []
        if missing:
            now = time.time()
            for sym in missing:
                exp_d = occ_expiry_as_date(sym)
                if exp_d is not None and exp_d < today:
                    continue
                last = _LEG_QUOTE_PREWARM_AT.get(sym, 0.0)
                if now - last >= 15.0:
                    need.append(sym)
            need = need[:20]
        if need:
            try:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                from alpaca.data.requests import OptionSnapshotRequest
                from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
                from agents.data.opra_client import _alpaca_chain_to_greeks

                if ALPACA_API_KEY and ALPACA_SECRET_KEY:
                    client = OptionHistoricalDataClient(
                        api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY
                    )
                    raw = client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=list(need))
                    )
                    snaps = []
                    if isinstance(raw, dict):
                        for occ, snap in raw.items():
                            try:
                                snaps.append(_alpaca_chain_to_greeks(str(occ), snap))
                            except Exception:
                                continue
                    if snaps:
                        gmap = {g.symbol: g for g in (firm_state.latest_greeks or [])}
                        for s in snaps:
                            gmap[s.symbol] = s
                        firm_state.latest_greeks = list(gmap.values())
                        # Refresh local lookup for this response
                        greeks_by_symbol.clear()
                        greeks_by_symbol.update({g.symbol: g for g in (firm_state.latest_greeks or [])})
                        for sym in need:
                            _LEG_QUOTE_PREWARM_AT[sym] = time.time()
                        # Rebuild after prewarm
                        leg_quotes, missing, net_premium_mid = _build_quotes()
            except Exception:
                pass

        expired_syms = sorted({str(lq["symbol"]) for lq in leg_quotes if lq.get("expired")})
        out: dict = {
            "legs": leg_quotes,
            "missing_quotes": sorted(list(set(missing))),
            "expired_leg_symbols": expired_syms,
            "net_premium_mid_usd": round(net_premium_mid, 2) if not missing else None,
        }
        if expired_syms:
            out["quote_note"] = (
                "Some legs use expired OCC symbols — brokers do not publish bid/ask on expired series. "
                "Dismiss this recommendation and run a fresh cycle so strikes match the live chain."
            )

        # Pattern-based max-loss estimate (only when we have complete mids).
        try:
            if not missing and legs:
                puts = [l for l in legs if str(l.get("right") or "").upper().startswith("P")]
                calls = [l for l in legs if str(l.get("right") or "").upper().startswith("C")]
                # Iron condor heuristic: 2 puts + 2 calls, same expiry, qty 1.
                if len(puts) == 2 and len(calls) == 2:
                    exp = {str(l.get("expiry") or "") for l in legs}
                    if len(exp) == 1:
                        put_strikes = sorted([float(l.get("strike") or 0) for l in puts])
                        call_strikes = sorted([float(l.get("strike") or 0) for l in calls])
                        width_put = abs(put_strikes[1] - put_strikes[0])
                        width_call = abs(call_strikes[1] - call_strikes[0])
                        credit = max(0.0, net_premium_mid / 100.0)  # per 1-lot in option price units
                        max_loss_est = (max(width_put, width_call) - credit) * 100.0
                        out["max_loss_estimate_usd"] = round(max(0.0, max_loss_est), 2)
                        out["risk_math"] = (
                            "Estimate (Iron Condor): max_loss ≈ max(put_width, call_width)×100 − net_credit×100 "
                            f"(put_width={width_put:.2f}, call_width={width_call:.2f}, "
                            f"net_credit≈${credit:.2f})."
                        )
                # Vertical debit/credit spread heuristic: 2 legs, same right+expiry.
                if "max_loss_estimate_usd" not in out and len(legs) == 2:
                    exp = {str(l.get("expiry") or "") for l in legs}
                    rights = {str(l.get("right") or "").upper() for l in legs}
                    if len(exp) == 1 and len(rights) == 1:
                        strikes = sorted([float(l.get("strike") or 0) for l in legs])
                        width = abs(strikes[1] - strikes[0])
                        # net premium: +credit or -debit (USD)
                        if net_premium_mid >= 0:
                            # credit spread max loss = width*100 - credit
                            max_loss_est = width * 100.0 - net_premium_mid
                            out["max_loss_estimate_usd"] = round(max(0.0, max_loss_est), 2)
                            out["risk_math"] = (
                                "Estimate (Credit spread): max_loss ≈ width×100 − net_credit "
                                f"(width={width:.2f}, net_credit≈${net_premium_mid:.2f})."
                            )
                        else:
                            # debit spread max loss = debit
                            out["max_loss_estimate_usd"] = round(abs(net_premium_mid), 2)
                            out["risk_math"] = (
                                "Estimate (Debit spread): max_loss ≈ net_debit "
                                f"(net_debit≈${abs(net_premium_mid):.2f})."
                            )
        except Exception as exc:
            out["pricing_error"] = str(exc)[:200]

        return out

    enriched: list[dict] = []
    for r in firm_state.pending_recommendations:
        d = r.model_dump(mode="json")
        try:
            d["pricing"] = _pricing_summary(d)
        except Exception as exc:
            d["pricing"] = {"error": str(exc)[:200]}
        enriched.append(d)
    return enriched


@app.post("/recommendations/{rec_id}/approve")
async def approve_recommendation(rec_id: str):
    rec = next((r for r in firm_state.pending_recommendations if r.id == rec_id), None)
    if not rec:
        raise HTTPException(404, f"Recommendation {rec_id} not found")
    if rec.status != "pending":
        return {"error": f"Recommendation already {rec.status}"}

    if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
        return {"error": "Cannot execute: kill switch or circuit breaker active"}

    # Execute the recommendation (options multi-leg OR stock order)
    try:
        if getattr(rec, "asset_type", "option") == "stock":
            sp = rec.stock_proposal
            if sp is None:
                return {"error": "Stock recommendation missing stock_proposal"}
            result = await asyncio.to_thread(
                _get_ems().place_stock_order,
                rec.ticker,
                sp.side.value.lower(),
                float(sp.qty),
                str(sp.order_type),
                float(sp.limit_price) if sp.limit_price is not None else None,
                "day",
            )
            if "error" in (result or {}):
                return {"error": str(result)[:200]}
            rec.status = "approved"
            rec.resolved_at = datetime.now(timezone.utc)
            firm_state.reasoning_log.append(ReasoningEntry(
                agent="System", action="PROCEED",
                reasoning=f"User approved STOCK recommendation for {rec.ticker}. Order submitted.",
                inputs={"recommendation_id": rec.id},
                outputs={"order_result": str(result)[:200]},
            ))
            asyncio.create_task(_post_order_sync())
            await _persist_firm_state()
            return {"status": "approved", "order_result": str(result)[:200]}

        from agents.agents.trader import _validate_and_build_legs
        from agents.state import GreeksSnapshot

        proposal = rec.proposal

        from agents.data.opra_client import occ_expiry_as_date

        expired_meta: list[dict[str, str]] = []
        for leg in proposal.legs:
            exp_d = occ_expiry_as_date(leg.symbol)
            if exp_d is not None and exp_d < date.today():
                expired_meta.append({"symbol": leg.symbol, "expired_on": exp_d.isoformat()})
        if expired_meta:
            return {
                "error": (
                    "Recommendation uses expired OCC legs; brokers cannot quote them. "
                    "Dismiss this recommendation and run a fresh cycle so legs match the live chain."
                ),
                "expired_legs": expired_meta,
            }

        greeks_by_symbol = {g.symbol: g for g in firm_state.latest_greeks}
        legs_payload, warnings = _validate_and_build_legs(proposal, greeks_by_symbol)

        legs_without_price = [l["symbol"] for l in legs_payload if l["limit_price"] is None]
        if legs_without_price:
            # Attempt a targeted quote prewarm for the missing OCC symbols, then retry once.
            # This avoids user-facing "missing quotes" when the rolling chain snapshot didn't include a leg.
            try:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                from alpaca.data.requests import OptionSnapshotRequest
                from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY
                from agents.data.opra_client import _alpaca_chain_to_greeks

                if ALPACA_API_KEY and ALPACA_SECRET_KEY:
                    client = OptionHistoricalDataClient(
                        api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY
                    )
                    raw = client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=list(legs_without_price))
                    )
                    # alpaca-py returns dict[occ_symbol, OptionSnapshot]
                    snaps: list[GreeksSnapshot] = []
                    if isinstance(raw, dict):
                        for occ, snap in raw.items():
                            try:
                                snaps.append(_alpaca_chain_to_greeks(str(occ), snap))
                            except Exception:
                                continue
                    if snaps:
                        # Merge into latest_greeks (replace per symbol)
                        gmap = {g.symbol: g for g in (firm_state.latest_greeks or [])}
                        for s in snaps:
                            gmap[s.symbol] = s
                        firm_state.latest_greeks = list(gmap.values())

                        # Retry build
                        greeks_by_symbol = {g.symbol: g for g in firm_state.latest_greeks}
                        legs_payload, warnings2 = _validate_and_build_legs(proposal, greeks_by_symbol)
                        warnings = list(warnings) + list(warnings2)
                        legs_without_price = [l["symbol"] for l in legs_payload if l["limit_price"] is None]
            except Exception:
                # Fall back to user-facing missing quotes message.
                pass

            if legs_without_price:
                # Don't mark as approved if we didn't actually submit an order.
                hint: list[str] = []
                for sym in legs_without_price:
                    ed = occ_expiry_as_date(sym)
                    if ed is not None and ed < date.today():
                        hint.append(f"{sym} expired {ed.isoformat()}")
                return {
                    "error": (
                        "Missing bid/ask (limit price) for legs: "
                        f"{legs_without_price}. "
                        + (
                            "Some appear expired — " + "; ".join(hint)
                            if hint
                            else "Check options data subscription / chain cache, or dismiss and regenerate."
                        )
                    ),
                    "missing_legs": legs_without_price,
                    **({"expired_hint": hint} if hint else {}),
                }

        order_payload = {
            "order_type": "MULTI_LEG",
            "strategy":   proposal.strategy_name,
            "ticker":     rec.ticker,
            "legs":       legs_payload,
            "tif":        "DAY",
            "max_risk":   proposal.max_risk,
            "target_return": proposal.target_return,
            "notes":      f"Advisory approval: {rec.id}",
            "warnings":   warnings,
        }

        ems = _get_ems()
        result = await asyncio.to_thread(ems.submit, order_payload, firm_state)
        rec.status = "approved"
        rec.resolved_at = datetime.now(timezone.utc)

        from agents.state import ReasoningEntry
        firm_state.reasoning_log.append(ReasoningEntry(
            agent="System", action="PROCEED",
            reasoning=f"User approved recommendation '{rec.strategy_name}' for {rec.ticker}. Order submitted.",
            inputs={"recommendation_id": rec.id},
            outputs={"order_result": str(result)[:200]},
        ))
        asyncio.create_task(_post_order_sync())
        await _persist_firm_state()
        return {"status": "approved", "order_result": str(result)[:200]}
    except Exception as exc:
        log.error("Failed to execute approved recommendation %s: %s", rec_id, exc)
        return {"error": str(exc)[:200]}


@app.post("/recommendations/{rec_id}/dismiss")
async def dismiss_recommendation(rec_id: str):
    rec = next((r for r in firm_state.pending_recommendations if r.id == rec_id), None)
    if not rec:
        raise HTTPException(404, f"Recommendation {rec_id} not found")
    rec.status = "dismissed"
    rec.resolved_at = datetime.now(timezone.utc)
    from agents.state import ReasoningEntry
    firm_state.reasoning_log.append(ReasoningEntry(
        agent="System", action="INFO",
        reasoning=f"User dismissed recommendation '{rec.strategy_name}' for {rec.ticker}.",
        inputs={"recommendation_id": rec.id},
        outputs={},
    ))
    await _persist_firm_state()
    return {"status": "dismissed"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
