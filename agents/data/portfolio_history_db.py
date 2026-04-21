"""
SQLite-backed portfolio / greeks time series for ``/portfolio_series``.

Survives API server restarts (unlike the in-memory deque alone).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_inited = False


def _pg_enabled() -> bool:
    try:
        from agents.data.warehouse import postgres as wh

        return wh.is_postgres_enabled()
    except Exception:
        return False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_path() -> Path:
    explicit = os.getenv("PORTFOLIO_HISTORY_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    agentic = os.getenv("AGENTIC_DATA_DIR", "").strip()
    if agentic:
        return Path(agentic).expanduser() / "portfolio_series.sqlite3"
    return Path("cache/portfolio_series.sqlite3")


def _path() -> Path:
    p = _configured_path()
    if not p.is_absolute():
        p = _root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_path()), timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=8000;")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_point (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            equity REAL NOT NULL,
            delta REAL NOT NULL,
            vega REAL NOT NULL,
            daily_pnl REAL NOT NULL,
            drawdown_pct REAL NOT NULL
        );
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_portfolio_point_ts ON portfolio_point(ts);"
    )
    con.commit()


def _ensure() -> None:
    global _inited
    if _inited:
        return
    with _lock:
        if _inited:
            return
        con = _connect()
        try:
            _ensure_schema(con)
            _inited = True
        finally:
            con.close()


def append_portfolio_point(row: dict[str, Any]) -> None:
    """Insert one sample (same keys as in-memory chart: time, equity, delta, …)."""
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO portfolio_point (ts, equity, delta, vega, daily_pnl, drawdown_pct)
                        VALUES (%s, %s, %s, %s, %s, %s);
                        """,
                        (
                            float(row.get("time") or 0.0),
                            float(row.get("equity") or 0.0),
                            float(row.get("delta") or 0.0),
                            float(row.get("vega") or 0.0),
                            float(row.get("daily_pnl") or 0.0),
                            float(row.get("drawdown_pct") or 0.0),
                        ),
                    )
            return
        except Exception:
            pass
    _ensure()
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO portfolio_point (ts, equity, delta, vega, daily_pnl, drawdown_pct)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                float(row.get("time") or 0.0),
                float(row.get("equity") or 0.0),
                float(row.get("delta") or 0.0),
                float(row.get("vega") or 0.0),
                float(row.get("daily_pnl") or 0.0),
                float(row.get("drawdown_pct") or 0.0),
            ),
        )
        con.commit()
    except Exception as e:
        log.debug("portfolio_history append failed: %s", e)
    finally:
        con.close()


def load_portfolio_points(limit: int = 2000) -> list[dict[str, Any]]:
    """Return up to ``limit`` points in chronological order (oldest → newest)."""
    if limit <= 0:
        return []
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ts, equity, delta, vega, daily_pnl, drawdown_pct
                        FROM portfolio_point
                        ORDER BY id DESC
                        LIMIT %s;
                        """,
                        (int(limit),),
                    )
                    rows = cur.fetchall()
            rows = rows[::-1]
            return [
                {
                    "time": float(ts),
                    "equity": float(equity),
                    "delta": float(delta),
                    "vega": float(vega),
                    "daily_pnl": float(daily_pnl),
                    "drawdown_pct": float(drawdown_pct),
                }
                for ts, equity, delta, vega, daily_pnl, drawdown_pct in rows
            ]
        except Exception:
            return []
    _ensure()
    con = _connect()
    try:
        cur = con.execute(
            """
            SELECT sub.ts, sub.equity, sub.delta, sub.vega, sub.daily_pnl, sub.drawdown_pct
            FROM (
                SELECT id, ts, equity, delta, vega, daily_pnl, drawdown_pct
                FROM portfolio_point
                ORDER BY id DESC
                LIMIT ?
            ) AS sub
            ORDER BY sub.id ASC;
            """,
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for ts, equity, delta, vega, daily_pnl, drawdown_pct in cur.fetchall():
            out.append(
                {
                    "time": float(ts),
                    "equity": float(equity),
                    "delta": float(delta),
                    "vega": float(vega),
                    "daily_pnl": float(daily_pnl),
                    "drawdown_pct": float(drawdown_pct),
                }
            )
        return out
    except Exception as e:
        log.warning("portfolio_history load failed: %s", e)
        return []
    finally:
        con.close()
