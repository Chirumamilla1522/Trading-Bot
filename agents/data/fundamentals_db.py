from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


# Full `fetch_stock_info` payload (description, peers, ecosystem, valuation rows).
# Refresh cadence is enforced in api_server via FUNDAMENTALS_DYNAMIC_TTL_S (default 7d).
#
# Persistence: defaults to ``cache/fundamentals.sqlite3`` under the project root.
# Set ``FUNDAMENTALS_DB_PATH`` for an explicit file, or ``AGENTIC_DATA_DIR`` to use
# ``<dir>/fundamentals.sqlite3`` (same dir as daily bars when both env vars are used).


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_db_path() -> Path:
    explicit = os.getenv("FUNDAMENTALS_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    agentic = os.getenv("AGENTIC_DATA_DIR", "").strip()
    if agentic:
        return Path(agentic).expanduser() / "fundamentals.sqlite3"
    return Path("cache/fundamentals.sqlite3")


def _db_path() -> Path:
    p = _configured_db_path()
    if not p.is_absolute():
        p = _project_root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()), timeout=2.5)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA busy_timeout=2500;")
    return con


def _init_db() -> None:
    con = _connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_info (
              ticker TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              fetched_at_unix INTEGER NOT NULL
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_fetched_at ON stock_info(fetched_at_unix);")
        con.commit()
    finally:
        con.close()


_init_once = False
_init_lock = threading.Lock()


def _ensure_init() -> None:
    global _init_once
    if _init_once:
        return
    with _init_lock:
        if _init_once:
            return
        _init_db()
        _init_once = True


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def get_stock_info_cached(ticker: str) -> tuple[dict[str, Any] | None, int | None]:
    """
    Returns (payload, fetched_at_unix) or (None, None) if missing.
    """
    _ensure_init()
    t = ticker.upper().strip()
    con = _connect()
    try:
        row = con.execute(
            "SELECT payload_json, fetched_at_unix FROM stock_info WHERE ticker = ?",
            (t,),
        ).fetchone()
        if not row:
            return None, None
        payload = json.loads(row[0])
        fetched_at = int(row[1])
        return payload, fetched_at
    finally:
        con.close()


def upsert_stock_info(ticker: str, payload: dict[str, Any]) -> bool:
    """
    Insert/update payload. Returns True if content changed (hash changed).
    Always updates fetched_at_unix to 'now'.
    """
    _ensure_init()
    t = ticker.upper().strip()
    now = int(time.time())
    h = _stable_hash(payload)
    con = _connect()
    try:
        prev = con.execute(
            "SELECT payload_hash FROM stock_info WHERE ticker = ?",
            (t,),
        ).fetchone()
        prev_h = prev[0] if prev else None
        changed = prev_h != h

        con.execute(
            """
            INSERT INTO stock_info(ticker, payload_json, payload_hash, fetched_at_unix)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
              payload_json = excluded.payload_json,
              payload_hash = excluded.payload_hash,
              fetched_at_unix = excluded.fetched_at_unix
            """,
            (t, json.dumps(payload, sort_keys=True, ensure_ascii=False), h, now),
        )
        con.commit()
        try:
            from agents.data.warehouse.postgres import enqueue_fundamentals

            enqueue_fundamentals(t, payload, now)
        except Exception:
            pass
        return bool(changed)
    finally:
        con.close()

