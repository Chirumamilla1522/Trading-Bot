"""
SQLite persistence for per-ticker research briefs and dirty flags.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.research.schema import TickerBrief, UniverseRowSummary

log = logging.getLogger(__name__)

_lock = threading.Lock()
_DB_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "research" / "universe.db"


def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    _ensure_dir()
    c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def ensure_seed_tickers(tickers: list[str]) -> None:
    """Create placeholder rows so the UI has one row per universe name."""
    init_db()
    from datetime import timedelta

    from agents.research.schema import EpistemicMeta, TickerBrief

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _lock:
        c = _conn()
        try:
            for t in tickers:
                tu = t.upper()
                row = c.execute("SELECT 1 FROM ticker_research WHERE ticker = ?", (tu,)).fetchone()
                if row:
                    continue
                ph = TickerBrief(
                    ticker=tu,
                    thesis_short="(Universe research pending — first refresh scheduled)",
                    stance="HOLD",
                    confidence=0.0,
                    epistemic=EpistemicMeta(
                        valid_until=now_dt + timedelta(hours=24),
                        ttl_minutes=1440,
                        stale_reason="seed",
                    ),
                )
                c.execute(
                    """
                    INSERT INTO ticker_research (ticker, brief_json, signal_hash, updated_at, valid_until, dirty, dirty_reasons, priority_score)
                    VALUES (?, ?, '', ?, NULL, 1, ?, 5.0)
                    """,
                    (tu, ph.model_dump_json(), now, json.dumps(["seed"])),
                )
            c.commit()
        finally:
            c.close()


def init_db() -> None:
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS ticker_research (
                    ticker TEXT PRIMARY KEY,
                    brief_json TEXT NOT NULL,
                    signal_hash TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    valid_until TEXT,
                    dirty INTEGER NOT NULL DEFAULT 0,
                    dirty_reasons TEXT NOT NULL DEFAULT '[]',
                    priority_score REAL NOT NULL DEFAULT 0,
                    portfolio_weight REAL NOT NULL DEFAULT 0
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS ticker_edges (
                    src TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'news_impact',
                    weight REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (src, dst, kind)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS research_eval (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            c.commit()
        finally:
            c.close()


def upsert_brief(brief: TickerBrief, *, dirty: bool = False, dirty_reasons: list[str] | None = None) -> None:
    init_db()
    dirty_reasons = dirty_reasons or []
    with _lock:
        c = _conn()
        try:
            vu = brief.epistemic.valid_until.isoformat() if brief.epistemic else None
            c.execute(
                """
                INSERT INTO ticker_research (ticker, brief_json, signal_hash, updated_at, valid_until, dirty, dirty_reasons, priority_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    brief_json = excluded.brief_json,
                    signal_hash = excluded.signal_hash,
                    updated_at = excluded.updated_at,
                    valid_until = excluded.valid_until,
                    dirty = excluded.dirty,
                    dirty_reasons = excluded.dirty_reasons,
                    priority_score = excluded.priority_score
                """,
                (
                    brief.ticker.upper(),
                    brief.model_dump_json(),
                    brief.signal_hash,
                    brief.updated_at.isoformat(),
                    vu,
                    1 if dirty else 0,
                    json.dumps(dirty_reasons),
                    0.0,
                ),
            )
            c.commit()
        finally:
            c.close()


def get_brief(ticker: str) -> TickerBrief | None:
    init_db()
    t = ticker.upper()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT brief_json FROM ticker_research WHERE ticker = ?", (t,)
            ).fetchone()
        finally:
            c.close()
    if not row:
        return None
    try:
        return TickerBrief.model_validate_json(row["brief_json"])
    except Exception as exc:
        log.debug("get_brief parse error %s: %s", t, exc)
        return None


def get_signal_hash(ticker: str) -> str:
    init_db()
    t = ticker.upper()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT signal_hash FROM ticker_research WHERE ticker = ?", (t,)
            ).fetchone()
        finally:
            c.close()
    return (row["signal_hash"] or "") if row else ""


def set_dirty(ticker: str, reasons: list[str], priority_boost: float = 0.0) -> None:
    init_db()
    t = ticker.upper()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT dirty_reasons, priority_score FROM ticker_research WHERE ticker = ?",
                (t,),
            ).fetchone()
            prev = json.loads(row["dirty_reasons"]) if row else []
            merged = list(dict.fromkeys(prev + reasons))
            pr = (row["priority_score"] if row else 0.0) + priority_boost
            if row:
                c.execute(
                    "UPDATE ticker_research SET dirty = 1, dirty_reasons = ?, priority_score = ? WHERE ticker = ?",
                    (json.dumps(merged), pr, t),
                )
            else:
                placeholder = TickerBrief(ticker=t, thesis_short="(pending universe refresh)")
                c.execute(
                    """
                    INSERT INTO ticker_research (ticker, brief_json, signal_hash, updated_at, valid_until, dirty, dirty_reasons, priority_score)
                    VALUES (?, ?, '', ?, NULL, 1, ?, ?)
                    """,
                    (t, placeholder.model_dump_json(), datetime.now(timezone.utc).isoformat(), json.dumps(merged), pr),
                )
            c.commit()
        finally:
            c.close()


def get_dirty_meta(ticker: str) -> tuple[bool, list[str]]:
    init_db()
    t = ticker.upper()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT dirty, dirty_reasons FROM ticker_research WHERE ticker = ?",
                (t,),
            ).fetchone()
        finally:
            c.close()
    if not row:
        return False, []
    return bool(row["dirty"]), json.loads(row["dirty_reasons"] or "[]")


def clear_dirty(ticker: str) -> None:
    init_db()
    with _lock:
        c = _conn()
        try:
            c.execute(
                "UPDATE ticker_research SET dirty = 0, dirty_reasons = '[]' WHERE ticker = ?",
                (ticker.upper(),),
            )
            c.commit()
        finally:
            c.close()


def list_universe_summaries() -> list[UniverseRowSummary]:
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT ticker, brief_json, signal_hash, updated_at, valid_until, dirty, dirty_reasons, priority_score FROM ticker_research ORDER BY ticker"
            ).fetchall()
        finally:
            c.close()
    out: list[UniverseRowSummary] = []
    for row in rows:
        try:
            b = TickerBrief.model_validate_json(row["brief_json"])
            reasons = json.loads(row["dirty_reasons"] or "[]")
            out.append(
                UniverseRowSummary(
                    ticker=row["ticker"],
                    stance=b.stance,
                    confidence=b.confidence,
                    updated_at=row["updated_at"],
                    valid_until=row["valid_until"],
                    dirty=bool(row["dirty"]),
                    dirty_reasons=reasons,
                    priority_score=float(row["priority_score"] or 0),
                    signal_hash=row["signal_hash"] or "",
                    has_brief=True,
                )
            )
        except Exception:
            out.append(
                UniverseRowSummary(
                    ticker=row["ticker"],
                    dirty=bool(row["dirty"]),
                    dirty_reasons=json.loads(row["dirty_reasons"] or "[]"),
                    priority_score=float(row["priority_score"] or 0),
                    signal_hash=row["signal_hash"] or "",
                    has_brief=False,
                )
            )
    return out


def upsert_edge(src: str, dst: str, kind: str = "news_impact", weight: float = 1.0) -> None:
    init_db()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                INSERT INTO ticker_edges (src, dst, kind, weight) VALUES (?, ?, ?, ?)
                ON CONFLICT(src, dst, kind) DO UPDATE SET weight = excluded.weight
                """,
                (src.upper(), dst.upper(), kind, weight),
            )
            c.commit()
        finally:
            c.close()


def log_eval_event(ticker: str, event: str, payload: dict[str, Any] | None = None) -> None:
    init_db()
    with _lock:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO research_eval (ticker, event, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (ticker.upper(), event, json.dumps(payload or {}), datetime.now(timezone.utc).isoformat()),
            )
            c.commit()
        finally:
            c.close()


def update_priority(ticker: str, score: float) -> None:
    init_db()
    with _lock:
        c = _conn()
        try:
            c.execute(
                "UPDATE ticker_research SET priority_score = ? WHERE ticker = ?",
                (score, ticker.upper()),
            )
            c.commit()
        finally:
            c.close()
