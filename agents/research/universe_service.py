"""
Background universe research: scan top-50, dirty flags, priority queue, LLM briefs.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from agents.data.sp500 import SP500_TOP50
from agents.research import store
from agents.research.brief_llm import run_brief_llm
from agents.research.priority import compute_priority
from agents.research.schema import TickerBrief
from agents.research.signals import build_all_snapshots, snapshot_hash

if TYPE_CHECKING:
    from agents.state import FirmState

log = logging.getLogger(__name__)

SCAN_INTERVAL_S = float(os.getenv("UNIVERSE_SCAN_INTERVAL_S", "60"))
MAX_JOBS_PER_TICK = int(os.getenv("UNIVERSE_MAX_JOBS_PER_SCAN", "8"))
WORKER_COUNT = int(os.getenv("UNIVERSE_LLM_WORKERS", "2"))
_seq = 0


class _PriorItem:
    __slots__ = ("neg_pri", "seq", "ticker", "reasons", "snap", "h")

    def __init__(self, priority: float, seq: int, ticker: str, reasons: list[str], snap: Any, h: str):
        self.neg_pri = -priority
        self.seq = seq
        self.ticker = ticker
        self.reasons = reasons
        self.snap = snap
        self.h = h

    def __lt__(self, other: Any) -> bool:
        if self.neg_pri != other.neg_pri:
            return self.neg_pri < other.neg_pri
        return self.seq < other.seq


_tasks: list[asyncio.Task] = []
_stop = asyncio.Event()


def _needs_refresh(
    ticker: str,
    new_hash: str,
    prior: TickerBrief | None,
    stored_hash: str,
    dirty_db: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if dirty_db:
        reasons.append("dirty_flag")
    if stored_hash != new_hash:
        reasons.append("signal_hash_changed")
    if prior:
        try:
            vu = prior.epistemic.valid_until
            if vu.tzinfo is None:
                vu = vu.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > vu:
                reasons.append("epistemic_expired")
        except Exception:
            reasons.append("epistemic_parse")
    if not prior or prior.confidence < 0.15 and "pending" in (prior.thesis_short or "").lower():
        reasons.append("placeholder")
    # de-dupe
    reasons = list(dict.fromkeys(reasons))
    return bool(reasons), reasons


async def _worker_loop(
    firm_state: "FirmState",
    job_queue: asyncio.Queue,
    worker_id: int,
) -> None:
    while not _stop.is_set():
        try:
            item: _PriorItem = await asyncio.wait_for(job_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        t = item.ticker
        try:
            prior = store.get_brief(t)
            brief = await asyncio.to_thread(
                run_brief_llm,
                t,
                item.snap,
                prior,
                signal_hash=item.h,
            )
            store.upsert_brief(brief, dirty=False, dirty_reasons=[])
            store.clear_dirty(t)
            store.log_eval_event(t, "brief_refresh_ok", {"worker": worker_id, "hash": item.h})
        except Exception as exc:
            log.warning("Universe worker %d failed %s: %s", worker_id, t, exc)
            store.log_eval_event(t, "brief_refresh_err", {"error": str(exc)[:200]})


async def _scan_loop(
    firm_state: "FirmState",
    get_scan: Callable[[str], dict | None],
    job_queue: asyncio.Queue,
) -> None:
    global _seq
    # Universe research should follow the same restricted universe as the UI/scanner
    # when configured, instead of always using SP500_TOP50.
    def _parse_csv_env(name: str) -> list[str]:
        raw = os.getenv(name, "").strip()
        if not raw:
            return []
        out: list[str] = []
        for x in raw.replace(";", ",").split(","):
            t = str(x or "").strip().upper()
            if t:
                out.append(t)
        return list(dict.fromkeys(out))

    # Default restricted universe (matches user-requested shortlist).
    # Note: indices are quotes-only and excluded here because this loop depends on options scanner signals.
    default_equities = ["SPY", "NVDA", "GOOG", "GOOGL", "MU", "LITE", "SNDK"]
    tickers = _parse_csv_env("SCANNER_TICKERS") or default_equities
    # Filter out index-like symbols (quotes-only) if someone accidentally includes them.
    tickers = [t for t in tickers if not t.startswith("^")]
    store.ensure_seed_tickers(tickers)

    while not _stop.is_set():
        try:
            snaps = await asyncio.to_thread(build_all_snapshots, tickers, get_scan, firm_state)
            heap: list[_PriorItem] = []

            for t, snap in snaps.items():
                h = snapshot_hash(snap)
                prev_h = store.get_signal_hash(t)
                prior = store.get_brief(t)
                dirty_db, _ = store.get_dirty_meta(t)
                need, reasons = _needs_refresh(t, h, prior, prev_h, dirty_db)
                pr = compute_priority(
                    t, snap, prev_h, h,
                    list(firm_state.stock_positions),
                    list(firm_state.open_positions),
                )
                if not need:
                    continue
                if not reasons:
                    reasons = ["rescan"]
                _seq += 1
                heapq.heappush(heap, _PriorItem(pr + (5.0 if dirty_db else 0.0), _seq, t, reasons, snap, h))

            # take top N jobs this scan
            for _ in range(min(MAX_JOBS_PER_TICK, len(heap))):
                item = heapq.heappop(heap)
                await job_queue.put(item)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("universe scan loop error: %s", exc)

        try:
            await asyncio.wait_for(_stop.wait(), timeout=SCAN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


def start_universe_research(firm_state: "FirmState", scanner: Any) -> None:
    """Start scan + worker tasks. Call from FastAPI lifespan."""
    global _tasks
    _tasks = []
    _stop.clear()
    store.init_db()

    def _get_scan(sym: str):
        try:
            return scanner.get_scan(sym)
        except Exception:
            return None

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    for i in range(WORKER_COUNT):
        _tasks.append(asyncio.create_task(_worker_loop(firm_state, q, i), name=f"universe-worker-{i}"))
    _tasks.append(asyncio.create_task(_scan_loop(firm_state, _get_scan, q), name="universe-scan"))
    log.info(
        "Universe research started: %d tickers, scan every %.0fs, max %d jobs/scan, %d workers",
        len(SP500_TOP50), SCAN_INTERVAL_S, MAX_JOBS_PER_TICK, WORKER_COUNT,
    )


def stop_universe_research() -> None:
    _stop.set()
    for t in _tasks:
        t.cancel()
    _tasks.clear()
    log.info("Universe research stopped.")


def mark_dirty_from_impact(tickers: list[str], reason: str = "news_graph") -> None:
    """Called when cross-stock impacts update — propagate dirty flags."""
    for t in tickers:
        store.set_dirty(t.upper(), [reason], priority_boost=2.0)
