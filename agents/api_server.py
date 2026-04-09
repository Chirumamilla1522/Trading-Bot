"""
FastAPI bridge between the Tauri UI and the Python agent graph.

Run from project root: ``python3 agents/api_server.py`` or ``python -m agents.api_server``.

Endpoints:
  GET  /state              – FirmState JSON + `agent_runtime` (UI polling)
  GET  /news               – latest 50 news items
  GET  /reasoning_log      – today's XAI log
  GET  /scanner            – S&P 500 options scanner (?sort=iv|pc|oi|ticker|price|chg) + live quotes
  GET  /scanner/quotes     – live last / change only (fast; used for 1 Hz price refresh)
  GET  /scanner/tickers    – full list of tracked tickers
  GET  /options/{ticker}   – options chain for a specific ticker (drilldown)
  GET  /bars/{ticker}      – Underlying stock OHLC + summary (Alpaca → Alpha Vantage → Yahoo when configured)
  GET  /quote/{ticker}     – Stock last / day change (Alpaca snapshot → Alpha Vantage → Yahoo; AV is delayed)
  GET  /stock_info/{ticker}– Fundamentals, peers, competitors, and dependency map (yfinance)
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
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
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


async def _portfolio_history_task():
    """Append NAV / greeks points for charting (~every 20s)."""
    while True:
        await asyncio.sleep(20)
        try:
            r = firm_state.risk
            nav = float(r.current_nav or r.opening_nav or 0.0)
            _PORTFOLIO_HISTORY.append(
                {
                    "time": time.time(),
                    "equity": nav,
                    "delta": float(r.portfolio_delta),
                    "vega": float(r.portfolio_vega),
                    "daily_pnl": float(r.daily_pnl),
                    "drawdown_pct": float(r.drawdown_pct),
                }
            )
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
        d = asdict(self)
        d["now"] = now
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
            firm_state.latest_greeks = snaps
            if snaps:
                atm = min(snaps, key=lambda g: abs(abs(g.delta) - 0.5))
                firm_state.underlying_price = (atm.bid + atm.ask) / 2 or atm.strike
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
        original_ticker = firm_state.ticker
        if ticker_override:
            firm_state.ticker = ticker_override

        agent_status.in_progress = True
        agent_status.last_cycle_started_at = time.time()
        try:
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


async def _scanner_task():
    """Continuously scans all S&P 500 tickers in the background."""
    await _scanner.run_forever()


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

    # Seed portfolio chart
    try:
        r0 = firm_state.risk
        _PORTFOLIO_HISTORY.append({
            "time": time.time(),
            "equity": float(r0.current_nav or r0.opening_nav or 0.0),
            "delta": float(r0.portfolio_delta),
            "vega": float(r0.portfolio_vega),
            "daily_pnl": float(r0.daily_pnl),
            "drawdown_pct": float(r0.drawdown_pct),
        })
    except Exception:
        pass

    # ── Probe local llama.cpp and log the active LLM backend ──────────────────
    await _probe_llama_cpp()

    # ── Start Tier-1/2 background loops (sentiment monitor + movement tracker
    #    + fundamentals refresher + T3 auto-trigger watchdog) ─────────────────
    from agents.tiers import start_tier_loops
    await start_tier_loops(firm_state)
    log.info("Agent tier loops started (T1: SentimentMonitor + MovementTracker; T2: Fundamentals)")

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
    # Shutdown: stop universe workers, tier loops, then cancel asyncio tasks
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
            "/bars/{ticker}", "/quote/{ticker}", "/stock_info/{ticker}", "/portfolio_series",
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
async def reasoning_log(tail: int = Query(500, ge=1, le=5000)):
    """Recent reasoning rows from today's JSONL (default last 500)."""
    return get_today_log(tail=tail)

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


_ALLOWED_BAR_TF = frozenset({
    "1D",
    "1Day", "1Hour", "15Min", "5Min", "1Min",
    "5D", "1M", "3M", "6M", "1Y",
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
    Data sourced from yfinance (free). Cached server-side for 1 hour.
    """
    from agents.data.fundamentals import fetch_stock_info

    info = await asyncio.to_thread(fetch_stock_info, ticker.upper())
    return info


@app.get("/bars/{ticker}")
async def get_bars(
    ticker: str,
    timeframe: str = Query("5D"),
    limit: int = Query(120, ge=10, le=2000),
    bust: bool = Query(False),
):
    """
    Underlying **stock** OHLC for the price chart (not options).

    Timeframes: intraday (1Min…1Day bar size) or multi-session history (5D, 1M, 3M, 6M, 1Y daily).
    Response includes `underlying` summary (last, range, period change, volume when available).
    Pass `?bust=true` to bypass the short-lived server cache.
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

    bars, source = await asyncio.to_thread(fetch_bars, ticker.upper(), tf, limit)
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
    return {"ticker": t, "count": len(chain), "contracts": chain}


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

@app.post("/set_ticker")
async def set_ticker(req: TickerRequest):
    from agents.data import market_activity
    from agents.data.alpaca_market_bridge import get_market_hub

    t = req.ticker.upper()
    market_activity.touch(t)
    firm_state.ticker = t
    try:
        get_market_hub().resubscribe_on_focus_change(t)
    except Exception as e:
        log.debug("market hub focus: %s", e)
    # Pre-warm the drilldown cache for the new ticker
    asyncio.create_task(_scanner.fetch_drilldown(t))
    return {"ticker": firm_state.ticker}


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
    return [r.model_dump() for r in firm_state.pending_recommendations]


@app.post("/recommendations/{rec_id}/approve")
async def approve_recommendation(rec_id: str):
    rec = next((r for r in firm_state.pending_recommendations if r.id == rec_id), None)
    if not rec:
        raise HTTPException(404, f"Recommendation {rec_id} not found")
    if rec.status != "pending":
        return {"error": f"Recommendation already {rec.status}"}

    if firm_state.kill_switch_active or firm_state.circuit_breaker_tripped:
        return {"error": "Cannot execute: kill switch or circuit breaker active"}

    # Execute the proposal by temporarily setting autopilot and running the trader node
    try:
        from agents.agents.trader import _validate_and_build_legs

        proposal = rec.proposal
        greeks_by_symbol = {g.symbol: g for g in firm_state.latest_greeks}
        legs_payload, warnings = _validate_and_build_legs(proposal, greeks_by_symbol)

        legs_without_price = [l["symbol"] for l in legs_payload if l["limit_price"] is None]
        if legs_without_price:
            rec.status = "approved"
            return {"status": "approved", "note": f"Missing quotes for legs: {legs_without_price}"}

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

        from agents.state import ReasoningEntry
        firm_state.reasoning_log.append(ReasoningEntry(
            agent="System", action="PROCEED",
            reasoning=f"User approved recommendation '{rec.strategy_name}' for {rec.ticker}. Order submitted.",
            inputs={"recommendation_id": rec.id},
            outputs={"order_result": str(result)[:200]},
        ))
        asyncio.create_task(_post_order_sync())
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
    from agents.state import ReasoningEntry
    firm_state.reasoning_log.append(ReasoningEntry(
        agent="System", action="INFO",
        reasoning=f"User dismissed recommendation '{rec.strategy_name}' for {rec.ticker}.",
        inputs={"recommendation_id": rec.id},
        outputs={},
    ))
    return {"status": "dismissed"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
