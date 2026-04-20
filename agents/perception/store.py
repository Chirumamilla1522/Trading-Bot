"""Phase 0 — Append-only SQLite log for perception bundles."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from agents.perception.schemas import PerceptionBundle

log = logging.getLogger(__name__)

_lock = threading.Lock()
_inited = False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _path() -> Path:
    explicit = os.getenv("PERCEPTION_DB_PATH", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
    else:
        agentic = os.getenv("AGENTIC_DATA_DIR", "").strip()
        if agentic:
            p = Path(agentic).expanduser() / "perception.sqlite3"
        else:
            p = _root() / "cache" / "perception.sqlite3"
    if not p.is_absolute():
        p = _root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _ensure() -> None:
    global _inited
    if _inited:
        return
    with _lock:
        if _inited:
            return
        p = _path()
        con = sqlite3.connect(str(p), timeout=5.0)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS perception_cycles (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trace_id TEXT NOT NULL,
                  ticker TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pc_ticker ON perception_cycles(ticker);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pc_trace ON perception_cycles(trace_id);")
            con.commit()
        finally:
            con.close()
        _inited = True
        log.debug("Perception store at %s", p)


def append_bundle(bundle: PerceptionBundle) -> None:
    """Persist bundle JSON for audit / meta-learning (Phase 6)."""
    _ensure()
    p = _path()
    row = bundle.to_log_dict()
    with _lock:
        con = sqlite3.connect(str(p), timeout=5.0)
        try:
            con.execute(
                "INSERT INTO perception_cycles(trace_id, ticker, created_at, payload_json) VALUES(?,?,?,?)",
                (
                    bundle.trace_id,
                    bundle.snapshot.ticker,
                    bundle.created_at.isoformat(),
                    json.dumps(row["payload"], separators=(",", ":"), ensure_ascii=False),
                ),
            )
            con.commit()
        finally:
            con.close()


def fetch_last(ticker: str, limit: int = 5) -> list[dict]:
    _ensure()
    t = ticker.upper().strip()
    out: list[dict] = []
    con = sqlite3.connect(str(_path()), timeout=5.0)
    try:
        cur = con.execute(
            "SELECT trace_id, created_at, payload_json FROM perception_cycles WHERE ticker = ? ORDER BY id DESC LIMIT ?",
            (t, limit),
        )
        for trace_id, created_at, raw in cur.fetchall():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            out.append({"trace_id": trace_id, "created_at": created_at, "payload": payload})
    finally:
        con.close()
    return out
