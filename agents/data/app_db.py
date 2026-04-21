"""
Single-file durable SQLite store for the app.

Replaces ad-hoc JSON/JSONL persistence for:
- FirmState snapshot
- XAI reasoning log
- Processed news JSONL (news_processed_db already exists, but we also offer a unified DB)
- Market data JSONL logs

Default location: cache/app.sqlite3 (or AGENTIC_DATA_DIR/app.sqlite3).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_inited = False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_path() -> Path:
    explicit = os.getenv("APP_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    agentic = os.getenv("AGENTIC_DATA_DIR", "").strip()
    if agentic:
        return Path(agentic).expanduser() / "app.sqlite3"
    return Path("cache/app.sqlite3")


def db_path() -> Path:
    p = _configured_path()
    if not p.is_absolute():
        p = _root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _pg_enabled() -> bool:
    try:
        from agents.data.warehouse import postgres as wh

        return wh.is_postgres_enabled()
    except Exception:
        return False


def connect() -> sqlite3.Connection:
    """SQLite connection (used when PostgreSQL is not configured)."""
    con = sqlite3.connect(str(db_path()), timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=8000;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def ensure_schema() -> None:
    global _inited
    if _inited:
        return
    with _lock:
        if _inited:
            return
        # Postgres: schema is managed by warehouse schema.sql
        if _pg_enabled():
            try:
                from agents.data.warehouse import postgres as wh

                wh.ensure_schema()
            except Exception:
                pass
            _inited = True
            return
        con = connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    k TEXT PRIMARY KEY,
                    v_json TEXT NOT NULL,
                    updated_at_unix REAL NOT NULL
                );
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS xai_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_iso TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    outputs_json TEXT NOT NULL,
                    trade_id TEXT
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_xai_ts ON xai_log(ts_iso);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_xai_agent ON xai_log(agent);")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS market_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_unix REAL NOT NULL,
                    ticker TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_me_ticker_ts ON market_event(ticker, ts_unix);")
            con.commit()
            _inited = True
        finally:
            con.close()


def kv_put(key: str, value: Any) -> None:
    ensure_schema()
    if _pg_enabled():
        from agents.data.warehouse import postgres as wh

        with wh.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_kv(k, v_json, updated_at)
                    VALUES (%s, %s::jsonb, now())
                    ON CONFLICT(k) DO UPDATE SET
                      v_json = EXCLUDED.v_json,
                      updated_at = now();
                    """,
                    (key, json.dumps(value, default=str)),
                )
        return
    con = connect()
    try:
        con.execute(
            "INSERT INTO kv(k, v_json, updated_at_unix) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v_json=excluded.v_json, updated_at_unix=excluded.updated_at_unix;",
            (key, json.dumps(value, default=str), time.time()),
        )
        con.commit()
    finally:
        con.close()


def kv_get(key: str) -> Any | None:
    ensure_schema()
    if _pg_enabled():
        from agents.data.warehouse import postgres as wh

        with wh.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT v_json FROM app_kv WHERE k = %s;", (key,))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
                except Exception:
                    return None
    con = connect()
    try:
        cur = con.execute("SELECT v_json FROM kv WHERE k = ?;", (key,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None
    finally:
        con.close()


def append_xai_row(row: dict[str, Any]) -> None:
    ensure_schema()
    if _pg_enabled():
        from agents.data.warehouse import postgres as wh

        sym = str(row.get("ticker") or "").upper().strip() or "SPY"
        with wh.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (sym,))
                cur.execute(
                    """
                    INSERT INTO xai_log(ts_iso, symbol, agent, action, reasoning, inputs_json, outputs_json, trade_id)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s);
                    """,
                    (
                        str(row.get("timestamp") or ""),
                        sym,
                        str(row.get("agent") or ""),
                        str(row.get("action") or ""),
                        str(row.get("reasoning") or ""),
                        json.dumps(row.get("inputs") or {}, default=str),
                        json.dumps(row.get("outputs") or {}, default=str),
                        row.get("trade_id"),
                    ),
                )
        return
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO xai_log(ts_iso, ticker, agent, action, reasoning, inputs_json, outputs_json, trade_id)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                str(row.get("timestamp") or ""),
                str(row.get("ticker") or ""),
                str(row.get("agent") or ""),
                str(row.get("action") or ""),
                str(row.get("reasoning") or ""),
                json.dumps(row.get("inputs") or {}, default=str),
                json.dumps(row.get("outputs") or {}, default=str),
                row.get("trade_id"),
            ),
        )
        con.commit()
    finally:
        con.close()


def read_xai_rows(*, tail: int | None = None, agent: str | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    if _pg_enabled():
        from agents.data.warehouse import postgres as wh

        want = (agent or "").strip()
        lim = int(tail) if tail is not None and int(tail) > 0 else None
        q = (
            "SELECT ts_iso,symbol,agent,action,reasoning,inputs_json,outputs_json,trade_id "
            "FROM xai_log "
        )
        params: list[Any] = []
        if want:
            q += "WHERE agent=%s "
            params.append(want)
        q += "ORDER BY id DESC "
        if lim:
            q += "LIMIT %s"
            params.append(lim)
        with wh.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(q, tuple(params))
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for ts_iso, symbol, a, action, reasoning, inputs_json, outputs_json, trade_id in rows[::-1]:
            out.append(
                {
                    "timestamp": ts_iso,
                    "ticker": symbol,
                    "agent": a,
                    "action": action,
                    "reasoning": reasoning,
                    "inputs": inputs_json or {},
                    "outputs": outputs_json or {},
                    "trade_id": trade_id,
                }
            )
        return out
    con = connect()
    try:
        want = (agent or "").strip()
        lim = int(tail) if tail is not None and int(tail) > 0 else None
        if want and lim:
            cur = con.execute(
                "SELECT ts_iso,ticker,agent,action,reasoning,inputs_json,outputs_json,trade_id "
                "FROM xai_log WHERE agent=? ORDER BY id DESC LIMIT ?;",
                (want, lim),
            )
        elif want:
            cur = con.execute(
                "SELECT ts_iso,ticker,agent,action,reasoning,inputs_json,outputs_json,trade_id "
                "FROM xai_log WHERE agent=? ORDER BY id DESC;",
                (want,),
            )
        elif lim:
            cur = con.execute(
                "SELECT ts_iso,ticker,agent,action,reasoning,inputs_json,outputs_json,trade_id "
                "FROM xai_log ORDER BY id DESC LIMIT ?;",
                (lim,),
            )
        else:
            cur = con.execute(
                "SELECT ts_iso,ticker,agent,action,reasoning,inputs_json,outputs_json,trade_id "
                "FROM xai_log ORDER BY id DESC;"
            )
        rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for ts_iso, ticker, a, action, reasoning, inputs_json, outputs_json, trade_id in rows[::-1]:
            try:
                inputs = json.loads(inputs_json) if inputs_json else {}
            except Exception:
                inputs = {}
            try:
                outputs = json.loads(outputs_json) if outputs_json else {}
            except Exception:
                outputs = {}
            out.append(
                {
                    "timestamp": ts_iso,
                    "ticker": ticker,
                    "agent": a,
                    "action": action,
                    "reasoning": reasoning,
                    "inputs": inputs,
                    "outputs": outputs,
                    "trade_id": trade_id,
                }
            )
        return out
    finally:
        con.close()


def append_market_event(*, ticker: str, channel: str, payload: Any) -> None:
    ensure_schema()
    if _pg_enabled():
        from agents.data.warehouse import postgres as wh

        sym = ticker.upper().strip() or "SPY"
        with wh.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (sym,))
                cur.execute(
                    "INSERT INTO market_event(ts_unix, symbol, channel, payload_json) VALUES(%s, %s, %s, %s::jsonb);",
                    (time.time(), sym, str(channel), json.dumps(payload, default=str)),
                )
        return
    con = connect()
    try:
        con.execute(
            "INSERT INTO market_event(ts_unix, ticker, channel, payload_json) VALUES(?,?,?,?);",
            (time.time(), ticker.upper().strip(), str(channel), json.dumps(payload, default=str)),
        )
        con.commit()
    finally:
        con.close()

