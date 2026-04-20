#!/usr/bin/env python3
"""
Dry-run all Tier-3 agents on a stock with production-like data hydration.

Pulls:
  - Options chain + underlying (Alpaca delayed feed, same as api_server tick task)
  - News: `unified` (async stream — first yield waits for a full cycle; use --unified-timeout 120+),
    or automatic yfinance fallback if unified returns 0 under timeout; `yf` (yfinance-only); `none`
  - Movement + SentimentMonitor (same helpers as tier loops)
  - Optional Tier-2 NewsProcessor LLM batch (`--tier2-llm`) to fill structured digests

Runs:
  - `graph.run_cycle` once (advisory mode → recommend path, no autopilot EMS unless you change mode)

Usage (from repo root):
  python3 agents/dry_run_agents.py --ticker NVDA --scenario full
  python3 agents/dry_run_agents.py --ticker AAPL --news yf --tier2-llm
  python3 agents/dry_run_agents.py --ticker SPY --scenario technical --no-graph
  python3 agents/dry_run_agents.py --ticker NVDA --local-model 'mlx-community/Mistral-7B-Instruct-v0.3-4bit'

Environment: load `.env` via `agents.config` (ALPACA, Benzinga, etc.).

**Tier-3 LangGraph and `--tier2-llm` need an LLM backend** (default stack is local llama, not cloud):

- **OpenRouter:** `OPENROUTER_ENABLED=true` and `OPENROUTER_API_KEY=...` in `.env`
- **Local:** OpenAI-compatible server (e.g. llama-server) at `LLAMA_LOCAL_BASE_URL` (default `http://127.0.0.1:8080/v1`)

Use `--no-graph` to pull chain/news/movement only (no LangGraph). The script checks the LLM before long-running steps unless `--skip-llm-check`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Repo root on path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import agents.config  # noqa: F401 — loads .env

from agents.graph import run_cycle
from agents.state import FirmState, RiskMetrics
from agents.config import ENABLE_NEWS_FEED, MAX_DAILY_DRAWDOWN

log = logging.getLogger("dry_run")

# Default local model id for dry-run when LLAMA_LOCAL_MODEL is unset (MLX LM Studio / llama.cpp style id)
DRY_RUN_DEFAULT_LOCAL_MODEL = "mlx-community/Mistral-7B-Instruct-v0.3-4bit"


def _llm_backend_ready() -> tuple[bool, str]:
    """
    Return (True, detail) if Tier-3 / OpenRouter / local pool can run invoke_llm,
    else (False, user-facing reason).
    """
    from agents.config import OPENROUTER_ENABLED

    if OPENROUTER_ENABLED:
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            return False, "OPENROUTER_ENABLED is true but OPENROUTER_API_KEY is empty."
        return True, "OpenRouter (OPENROUTER_API_KEY set)"

    from agents.llm_local import server_pool

    server_pool.init_from_env()
    healthy = server_pool.probe_all()
    if healthy:
        u = healthy[0][:80] + ("…" if len(healthy[0]) > 80 else "")
        return True, f"local OpenAI-compatible server(s): {len(healthy)} healthy — {u}"

    urls = list(server_pool.all_urls) or ["(no URLs discovered — check .env loading)"]
    lines = "\n".join(f"    - {u} (unhealthy or unreachable)" for u in urls[:12])
    if len(urls) > 12:
        lines += f"\n    … +{len(urls) - 12} more"
    return (
        False,
        "No LLM server passed the health probe. Discovered base URL(s):\n"
        f"{lines}\n"
        "  → From this machine, curl each:  curl -sS -o /dev/null -w '%{http_code}' "
        "'http://<host>:<port>/v1/models'\n"
        "  → If 404 on /v1/models only: upgrade agents (probe accepts 404) or ensure "
        "GET /v1/chat/completions works.\n"
        "  → Remote hosts: firewall / same LAN; try LLAMA_PROBE_TIMEOUT_S=15 in .env",
    )


def _print_llm_unavailable(reason: str) -> None:
    print(
        "\n"
        + "=" * 72
        + "\n"
        "LLM backend required for LangGraph (`--tier2-llm` or default pipeline).\n"
        f"\n{reason}\n\n"
        "Choose one:\n"
        "  • Cloud: set in .env  OPENROUTER_ENABLED=true  and  OPENROUTER_API_KEY=sk-or-v1-...\n"
        "  • Local: LLAMA_LOCAL_BASE_URL and/or LLAMA_LOCAL_BASE_URL_<ROLE> must be reachable\n"
        "           from *this* machine (see list above). Set LLAMA_PROBE_TIMEOUT_S=15 on slow LAN.\n\n"
        "To only test Greeks + news + movement (no graph):  --no-graph\n"
        "To bypass this check (will still fail on first agent LLM call):  --skip-llm-check\n"
        + "=" * 72
        + "\n",
        file=sys.stderr,
    )


def _build_state(ticker: str) -> FirmState:
    return FirmState(
        ticker=ticker.upper().strip(),
        trading_mode="advisory",
        kill_switch_active=False,
        circuit_breaker_tripped=False,
        cash_balance=100_000.0,
        buying_power=100_000.0,
        account_equity=100_000.0,
        risk=RiskMetrics(
            opening_nav=100_000.0,
            current_nav=100_000.0,
            max_drawdown_pct=MAX_DAILY_DRAWDOWN,
        ),
    )


def _spot_underlying(ticker: str) -> float | None:
    """Best-effort spot (yfinance) — option-chain mids alone can be misleading for index ETFs."""
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        fi = getattr(t, "fast_info", {}) or {}
        for key in ("last_price", "regularMarketPrice"):
            v = fi.get(key) if isinstance(fi, dict) else None
            if v is not None and float(v) > 1.0:
                return float(v)
        info = t.info or {}
        v = info.get("regularMarketPrice") or info.get("previousClose")
        if v is not None and float(v) > 1.0:
            return float(v)
    except Exception:
        pass
    return None


def _hydrate_greeks(state: FirmState) -> None:
    from agents.data.opra_client import AlpacaDelayedFeed

    feed = AlpacaDelayedFeed()
    snaps = feed._fetch_snapshots(state.ticker) or []
    spot = _spot_underlying(state.ticker)
    if spot is not None:
        state.underlying_price = spot
    elif snaps:
        atm = min(snaps, key=lambda g: abs(abs(g.delta) - 0.5))
        mid = (atm.bid + atm.ask) / 2 if (atm.bid or atm.ask) else 0.0
        state.underlying_price = float(mid or atm.strike or 0.0)
    from agents.data.options_chain_filter import filter_greeks_for_agents

    state.latest_greeks = filter_greeks_for_agents(snaps, state.underlying_price)
    log.info("Greeks: %d contracts, underlying≈%.2f", len(state.latest_greeks), state.underlying_price)


def _hydrate_news_yf(state: FirmState) -> int:
    """One-shot yfinance news for the active ticker (sync, same stack as news_feed tiers)."""
    from agents.data.news_feed import _fetch_yf_tier

    seen: set[str] = set()
    items = _fetch_yf_tier([state.ticker], "dry_run", seen, max_tickers=5)
    state.news_feed = list(items)
    return len(state.news_feed)

def _filter_news_window_inplace(state: FirmState, *, hours: float) -> int:
    """
    Keep only news published within the last `hours`.
    This matches the SentimentAnalyst 1h cutoff and avoids older headlines
    causing "0 recent" confusion in dry runs.
    """
    hours = max(0.1, float(hours))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    kept = []
    for n in (state.news_feed or []):
        try:
            pub = getattr(n, "published_at", None)
            if pub is None:
                continue
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub >= cutoff:
                kept.append(n)
        except Exception:
            continue
    before = len(state.news_feed or [])
    state.news_feed = kept
    return before - len(kept)


async def _hydrate_news_unified(state: FirmState, *, max_items: int, timeout_s: float) -> int:
    """
    Sample the unified async news stream (Benzinga + yfinance when configured).

    Important: the stream only **yields after one full ingestion cycle** (Benzinga pages,
    optional FinBERT cold-start on first article, per-tier calls, yfinance for many tickers).
    Short timeouts often cancel **before the first yield** → 0 items. Use a generous
    ``timeout_s`` (e.g. 120+) or rely on yfinance fallback in ``main``.
    """
    from agents.data.news_feed import unified_news_stream

    acc: list = []

    async def _collect() -> None:
        async for item in unified_news_stream(lambda: [state.ticker], lambda: []):
            acc.append(item)
            if len(acc) >= max_items:
                return

    try:
        await asyncio.wait_for(_collect(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.info(
            "Unified news: timeout after %.1fs with %d items (first cycle may not have yielded yet; "
            "see FinBERT/Benzinga/yfinance cold start). Use --unified-timeout 120+ or --news yf.",
            timeout_s,
            len(acc),
        )
    state.news_feed = acc
    return len(acc)


def _hydrate_movement_and_monitor(state: FirmState) -> None:
    from agents.agents.movement_tracker import run_movement_tracker
    from agents.desk_context import update_market_bias_score, update_news_timing_from_feed
    from agents.sentiment_monitor_llm import run_sentiment_monitor_cycle

    price = state.underlying_price or None
    sig = run_movement_tracker(state.ticker, price)
    state.movement_signal = sig["movement_signal"]
    state.movement_anomaly = sig["anomaly"]
    state.price_change_pct = sig["price_change_pct"]
    state.momentum = sig["momentum"]
    state.vol_ratio = sig["vol_ratio"]
    state.movement_updated = datetime.now(timezone.utc)
    update_news_timing_from_feed(state)
    update_market_bias_score(state)

    mon = run_sentiment_monitor_cycle(state.ticker)
    state.sentiment_monitor_score = float(mon.get("desk_sentiment", 0.0))
    state.sentiment_monitor_confidence = float(mon.get("confidence") or 0.0)
    state.sentiment_monitor_reasoning = str(mon.get("reasoning") or "")[:500]
    state.sentiment_monitor_source = str(mon.get("source") or "none")

def _refresh_sentiment_monitor_only(state: FirmState) -> None:
    """Re-run SentimentMonitor after Tier-2 structured news is updated."""
    from agents.sentiment_monitor_llm import run_sentiment_monitor_cycle

    mon = run_sentiment_monitor_cycle(state.ticker)
    state.sentiment_monitor_score = float(mon.get("desk_sentiment", 0.0))
    state.sentiment_monitor_confidence = float(mon.get("confidence") or 0.0)
    state.sentiment_monitor_reasoning = str(mon.get("reasoning") or "")[:500]
    state.sentiment_monitor_source = str(mon.get("source") or "none")


def _optional_tier2_llm(state: FirmState) -> None:
    from agents.data.news_processor import process_new_headlines
    from agents.data.sp500 import SP500_TOP50

    if not state.news_feed:
        log.warning("Tier-2 LLM skipped: empty news_feed")
        return
    new_articles, impact_map = process_new_headlines(list(state.news_feed), SP500_TOP50)
    state.news_impact_map = {k: v.model_dump(mode="json") for k, v in impact_map.items()}
    log.info("Tier-2 NewsProcessor: %d new articles, %d impact keys", len(new_articles), len(impact_map))


def _print_cycle_report(state: FirmState, err: Exception | None) -> None:
    print("\n" + "=" * 72)
    print("DRY RUN SUMMARY")
    print("=" * 72)
    print(f"Ticker: {state.ticker}  underlying≈{state.underlying_price:.2f}")
    print(f"News items: {len(state.news_feed)}  digests (post-ingest would load in graph): see run log")
    print(f"Greeks rows: {len(state.latest_greeks)}")
    print(
        f"Tier-1: sentiment_monitor={state.sentiment_monitor_score:+.3f} "
        f"({state.sentiment_monitor_source})  movement={state.movement_signal:+.3f} "
        f"bias={state.market_bias_score:+.3f}  timing={state.news_timing_regime}",
    )
    print(
        f"Decisions: analyst={state.analyst_decision.value} "
        f"sentiment={state.sentiment_decision.value} "
        f"risk={state.risk_decision.value} "
        f"desk→trader={state.trader_decision.value}",
    )
    print(f"Strategy confidence: {state.strategy_confidence:.2f}")
    if state.pending_proposal:
        print(f"Proposal: {state.pending_proposal.strategy_name}  max_risk=${state.pending_proposal.max_risk:.0f}")
    if err:
        print(f"\nERROR: {type(err).__name__}: {err}")
    print("\n--- Reasoning log (agent order) ---")
    for i, e in enumerate(state.reasoning_log, 1):
        preview = (e.reasoning or "").replace("\n", " ")[:220]
        print(f"{i:02d}. {e.agent:22s} {e.action:8s} {preview}")

    pend = [r for r in state.pending_recommendations if r.status == "pending"]
    if pend:
        print(f"\nPending recommendations: {len(pend)} (advisory mode)")
    print("=" * 72 + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Dry-run agents with production-like data pulls.")
    p.add_argument("--ticker", default="SPY", help="Underlying symbol")
    p.add_argument(
        "--scenario",
        choices=("full", "technical", "news_yf"),
        default="full",
        help="full=unified news sample + chain; technical=greeks+movement only; news_yf=yfinance headlines only",
    )
    p.add_argument(
        "--news",
        choices=("unified", "yf", "none"),
        default=None,
        help="Override news hydration (default: unified for full, yf for news_yf, none for technical)",
    )
    p.add_argument(
        "--unified-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for first unified stream yields (first cycle can be slow; default 120)",
    )
    p.add_argument("--unified-max", type=int, default=45, help="Max news items from unified stream")
    p.add_argument(
        "--no-yf-fallback",
        action="store_true",
        help="If unified returns 0 items, do not fall back to yfinance (default: fallback on)",
    )
    p.add_argument("--tier2-llm", action="store_true", help="Run Tier-2 NewsProcessor LLM on collected headlines")
    p.add_argument("--no-graph", action="store_true", help="Only hydrate Tier-1 context, skip LangGraph")
    p.add_argument(
        "--news-hours",
        type=float,
        default=1.0,
        help="After collecting headlines, keep only items published in the last N hours (default 1.0)",
    )
    p.add_argument(
        "--skip-llm-check",
        action="store_true",
        help="Do not verify LLM before hydration/graph (fails on first invoke_llm if misconfigured)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--local-model",
        metavar="MODEL_ID",
        default=None,
        help=(
            "Sets LLAMA_LOCAL_MODEL for this process (chat/completions JSON). "
            f"Precedence: this flag > .env LLAMA_LOCAL_MODEL > default {DRY_RUN_DEFAULT_LOCAL_MODEL!r}"
        ),
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Resolve model id before any invoke_llm (must match GET /v1/models id on your server)
    resolved_model = (args.local_model or "").strip()
    if not resolved_model:
        resolved_model = (os.getenv("LLAMA_LOCAL_MODEL") or "").strip()
    if not resolved_model:
        resolved_model = DRY_RUN_DEFAULT_LOCAL_MODEL
    os.environ["LLAMA_LOCAL_MODEL"] = resolved_model
    log.info("LLAMA_LOCAL_MODEL for dry run: %s", resolved_model)

    ticker = args.ticker.upper().strip()
    state = _build_state(ticker)

    news_mode = args.news
    if news_mode is None:
        if args.scenario == "technical":
            news_mode = "none"
        elif args.scenario == "news_yf":
            news_mode = "yf"
        else:
            news_mode = "unified"

    need_llm_for_pipeline = (not args.no_graph) or args.tier2_llm
    if need_llm_for_pipeline and not args.skip_llm_check:
        ok, detail = _llm_backend_ready()
        if not ok:
            _print_llm_unavailable(detail)
            return 2
        log.info("LLM backend OK — %s", detail)

    print(f"\n>>> Dry run — ticker={ticker} scenario={args.scenario} news={news_mode} <<<\n")

    # 1) Chain / price (production path)
    try:
        _hydrate_greeks(state)
    except Exception as e:
        log.warning("Greeks fetch failed (check ALPACA_* keys): %s", e)
        if state.underlying_price <= 0:
            state.underlying_price = 100.0

    # 2) News
    if news_mode == "unified":
        if not ENABLE_NEWS_FEED:
            log.warning("ENABLE_NEWS_FEED=false — enable in .env for unified stream")
        try:
            n = asyncio.run(
                _hydrate_news_unified(
                    state, max_items=args.unified_max, timeout_s=args.unified_timeout
                )
            )
            log.info("Unified news collected: %d items", n)
            if n == 0 and not args.no_yf_fallback:
                log.warning(
                    "Unified produced 0 items (timeout before first yield is common). "
                    "Falling back to yfinance for this ticker."
                )
                n = _hydrate_news_yf(state)
                log.info("yfinance fallback: %d items", n)
        except Exception as e:
            log.warning("Unified news failed: %s — falling back to yfinance", e)
            _hydrate_news_yf(state)
    elif news_mode == "yf":
        n = _hydrate_news_yf(state)
        log.info("yfinance news: %d items", n)
    else:
        state.news_feed = []
        log.info("News: skipped (technical scenario)")

    # Keep last N hours only (default 1h) so SentimentAnalyst sees a consistent window.
    if state.news_feed:
        removed = _filter_news_window_inplace(state, hours=args.news_hours)
        if removed:
            log.info(
                "News window filter: removed %d items older than %.2fh (kept %d).",
                removed,
                float(args.news_hours),
                len(state.news_feed),
            )

        try:
            from agents.data.news_priority_queue import get_queue

            added = get_queue().push_many(state.news_feed)
            log.info(
                "NewsPriorityQueue: pushed %d items (size=%d, stats=%s)",
                added,
                get_queue().size(),
                get_queue().stats(),
            )
        except Exception as _exc:
            log.warning("NewsPriorityQueue push failed: %s", _exc)

    # 3) Tier-1 parity: movement + SentimentMonitor LLM
    try:
        _hydrate_movement_and_monitor(state)
    except Exception as e:
        log.warning("Movement/monitor: %s", e)

    # 4) Optional Tier-2 batch LLM (structured DB + digests)
    if args.tier2_llm:
        try:
            _optional_tier2_llm(state)
            # SentimentMonitor uses structured Tier-2 outputs; refresh it now so the summary reflects this run.
            try:
                _refresh_sentiment_monitor_only(state)
                log.info(
                    "SentimentMonitor refreshed after Tier-2: score=%+.3f source=%s conf=%.2f",
                    state.sentiment_monitor_score,
                    state.sentiment_monitor_source,
                    state.sentiment_monitor_confidence,
                )
            except Exception as e:
                log.warning("SentimentMonitor refresh (post Tier-2) failed: %s", e)
        except Exception as e:
            log.warning("Tier-2 NewsProcessor: %s", e)

    if args.no_graph:
        print("\n--no-graph: hydration only.\n")
        s = state.model_dump(mode="json")
        # Drop huge lists for stdout
        s.pop("news_feed", None)
        s.pop("latest_greeks", None)
        s.pop("reasoning_log", None)
        print(json.dumps(s, indent=2, default=str)[:8000])
        return 0

    # 5) Full agent graph (production run_cycle)
    # Match SentimentAnalyst headline window to the same N hours we kept in news_feed.
    state.sentiment_headline_lookback_hours = float(args.news_hours)

    log.info("Running LangGraph pipeline (advisory mode)...")
    result, err = run_cycle(state)
    _print_cycle_report(result, err)
    return 1 if err else 0


if __name__ == "__main__":
    raise SystemExit(main())
