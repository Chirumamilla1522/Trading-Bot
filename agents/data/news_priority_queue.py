"""
News Priority Queue
===================

A single in-memory store of ingested news items, ordered by a composite
**priority score** derived from ingestion-time signals (FinBERT sentiment +
confidence, heuristic impact, urgency tier, volatility probability).

Consumers (NewsProcessor / SentimentAnalyst) pull **top items first**, then the
next batch, and so on — so over multiple cycles *all* news gets processed,
starting with the most important.

Design:
- One global queue, dedup by headline hash.
- Per-agent "seen" flags so different agents can drain the backlog
  independently (e.g. NewsProcessor vs SentimentAnalyst).
- Capacity (max_size) with TTL (ttl_hours) to bound memory. When overfull,
  fully-seen / oldest / lowest-priority items are evicted first.
- Thread-safe (RLock) so sync + async paths can share it.

This is intentionally in-memory (fast, simple). Persist later if needed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# ── Well-known consumer names ────────────────────────────────────────────────
AGENT_NEWS_ANALYST = "news_analyst"          # Tier-2 NewsProcessor LLM
AGENT_SENTIMENT_ANALYST = "sentiment_analyst"  # Tier-3 SentimentAnalyst LLM


def _fget(obj: Any, attr: str, default: float = 0.0) -> float:
    try:
        v = getattr(obj, attr, default)
        return float(v) if v is not None else float(default)
    except Exception:
        return float(default)


def _sget(obj: Any, attr: str, default: str = "") -> str:
    try:
        v = getattr(obj, attr, default)
        return str(v) if v is not None else str(default)
    except Exception:
        return str(default)


def compute_priority_score(item: Any) -> float:
    """
    Composite priority combining FinBERT-derived fields and ingestion heuristics.

    Higher = processed sooner. Bounded ~[0, 5+].
    Inputs read from NewsItem (see agents/state.py):
      - impact_score   (0..1)        heuristic + category weighting
      - vol_prob       (0..1)        volatility proxy
      - confidence     (0..1)        FinBERT / source confidence
      - sentiment      (-1..1)       FinBERT (abs magnitude matters here)
      - urgency_tier   T0|T1|T2|T3
    """
    impact = max(0.0, min(1.0, _fget(item, "impact_score", 0.0)))
    volp = max(0.0, min(1.0, _fget(item, "vol_prob", 0.0)))
    conf = max(0.0, min(1.0, _fget(item, "confidence", 0.0)))
    sent_mag = min(1.0, abs(_fget(item, "sentiment", 0.0)))
    urg = _sget(item, "urgency_tier", "T2").upper()

    urg_bonus = {"T0": 1.0, "T1": 0.6, "T2": 0.2, "T3": 0.0}.get(urg, 0.0)

    score = (
        urg_bonus * 1.5
        + impact * 1.2
        + volp * 0.6
        + sent_mag * conf * 0.8
    )
    return round(float(score), 6)


def headline_id(text: str) -> str:
    return hashlib.sha1((text or "").lower().strip().encode()).hexdigest()[:16]


@dataclass
class QueuedNews:
    id: str
    item: Any               # NewsItem
    priority: float
    added_at: datetime
    seen: set[str] = field(default_factory=set)


class NewsPriorityQueue:
    def __init__(
        self,
        *,
        max_size: int = 10_000,
        ttl_hours: float = 24.0,
    ) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, QueuedNews] = {}
        self._max_size = int(max_size)
        self._ttl_hours = float(ttl_hours)
        self._order = (os.getenv("NEWS_QUEUE_ORDER", "fifo") or "fifo").strip().lower()

    # ── mutation ───────────────────────────────────────────────────────────
    def push(self, item: Any) -> bool:
        """Add a news item (NewsItem). Dedup by headline id. Returns True if added."""
        headline = getattr(item, "headline", "") or ""
        if not headline:
            return False
        nid = headline_id(headline)
        with self._lock:
            existing = self._items.get(nid)
            if existing is not None:
                # Re-score if we got stronger signals (e.g. FinBERT filled in later)
                new_p = compute_priority_score(item)
                if new_p > existing.priority:
                    existing.priority = new_p
                    existing.item = item
                return False
            self._items[nid] = QueuedNews(
                id=nid,
                item=item,
                priority=compute_priority_score(item),
                added_at=datetime.now(timezone.utc),
            )
            self._gc_locked()
            return True

    def push_many(self, items: Iterable[Any]) -> int:
        added = 0
        for it in items:
            if self.push(it):
                added += 1
        return added

    def mark_seen(self, agent: str, ids: Iterable[str]) -> int:
        if not agent:
            return 0
        count = 0
        with self._lock:
            for nid in ids:
                q = self._items.get(nid)
                if q is not None and agent not in q.seen:
                    q.seen.add(agent)
                    count += 1
        return count

    def remove(self, ids: Iterable[str]) -> int:
        count = 0
        with self._lock:
            for nid in ids:
                if self._items.pop(nid, None) is not None:
                    count += 1
        return count

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    # ── reads ─────────────────────────────────────────────────────────────
    def take_unseen(self, agent: str, limit: int) -> list[QueuedNews]:
        """
        Return unseen items for `agent` (up to `limit`).

        Ordering is controlled by `NEWS_QUEUE_ORDER`:
        - fifo (default): oldest `added_at` first (strict first-in-first-out)
        - priority: higher computed priority first, then FIFO tiebreak
        """
        if limit <= 0:
            return []
        with self._lock:
            self._gc_locked()
            unseen = [q for q in self._items.values() if agent not in q.seen]
            if self._order == "priority":
                unseen.sort(key=lambda q: (-q.priority, q.added_at))
            else:
                unseen.sort(key=lambda q: (q.added_at, -q.priority))
            return list(unseen[:limit])

    def peek_top(self, limit: int) -> list[QueuedNews]:
        if limit <= 0:
            return []
        with self._lock:
            items = list(self._items.values())
            if self._order == "priority":
                items.sort(key=lambda q: (-q.priority, q.added_at))
            else:
                items.sort(key=lambda q: (q.added_at, -q.priority))
            return items[:limit]

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = len(self._items)
            unseen_na = sum(1 for q in self._items.values() if AGENT_NEWS_ANALYST not in q.seen)
            unseen_sa = sum(1 for q in self._items.values() if AGENT_SENTIMENT_ANALYST not in q.seen)
            top = self.peek_top(5)
            return {
                "total": total,
                "unseen_news_analyst": unseen_na,
                "unseen_sentiment_analyst": unseen_sa,
                "top5_priority": [round(q.priority, 4) for q in top],
                "ttl_hours": self._ttl_hours,
                "max_size": self._max_size,
            }

    # ── internal GC / eviction ────────────────────────────────────────────
    def _gc_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._ttl_hours)
        stale = [k for k, v in self._items.items() if v.added_at < cutoff]
        for k in stale:
            self._items.pop(k, None)

        if len(self._items) <= self._max_size:
            return

        # Evict the least useful items first:
        #   1) fully-seen by both primary agents
        #   2) lowest priority
        #   3) oldest
        seen_both = (AGENT_NEWS_ANALYST, AGENT_SENTIMENT_ANALYST)
        ordered = sorted(
            self._items.values(),
            key=lambda q: (
                0 if all(a in q.seen for a in seen_both) else 1,
                q.priority,
                q.added_at,
            ),
        )
        overflow = len(self._items) - self._max_size
        for q in ordered[:overflow]:
            self._items.pop(q.id, None)


# ── Singleton accessor ───────────────────────────────────────────────────────
_queue: Optional[NewsPriorityQueue] = None
_queue_lock = threading.Lock()


def _queue_params_from_env() -> tuple[int, float]:
    try:
        max_size = int(os.getenv("NEWS_QUEUE_MAX_SIZE", "10000"))
    except Exception:
        max_size = 10000
    try:
        ttl_hours = float(os.getenv("NEWS_QUEUE_TTL_HOURS", "24"))
    except Exception:
        ttl_hours = 24.0
    return max_size, ttl_hours


def get_queue() -> NewsPriorityQueue:
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                max_size, ttl = _queue_params_from_env()
                _queue = NewsPriorityQueue(max_size=max_size, ttl_hours=ttl)
                log.info(
                    "NewsPriorityQueue initialized (max_size=%d, ttl_hours=%.1f).",
                    max_size, ttl,
                )
    return _queue
