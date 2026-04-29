"""
Tiny local IV history store for computing IV rank.

We persist per-ticker ATM IV snapshots (time, atm_iv) so we can compute
rolling IV rank over a lookback window (default ~30d).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


def _db_path() -> Path:
    return Path(os.getenv("IV_HISTORY_DB_PATH", "cache/iv_history.sqlite3"))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS iv_history (
          ticker TEXT NOT NULL,
          ts_unix INTEGER NOT NULL,
          atm_iv REAL NOT NULL
        );
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_iv_hist_ticker_ts ON iv_history(ticker, ts_unix);"
    )
    return con


def append_atm_iv(ticker: str, atm_iv: float, ts_unix: int | None = None) -> None:
    t = (ticker or "").upper().strip()
    if not t:
        return
    try:
        v = float(atm_iv)
    except Exception:
        return
    if v <= 0 or v != v:
        return
    ts = int(ts_unix or time.time())
    try:
        con = _connect()
        with con:
            con.execute(
                "INSERT INTO iv_history(ticker, ts_unix, atm_iv) VALUES(?,?,?)",
                (t, ts, float(v)),
            )
            # keep table small: drop very old data (>120 days) opportunistically
            cutoff = ts - 120 * 86400
            con.execute("DELETE FROM iv_history WHERE ts_unix < ?", (cutoff,))
    except Exception:
        return
    finally:
        try:
            con.close()
        except Exception:
            pass


def iv_rank(
    ticker: str,
    current_atm_iv: float,
    *,
    lookback_days: int = 30,
) -> float | None:
    """
    Return IV rank in [0,1] vs min/max ATM IV over the lookback window.
    None if insufficient history or degenerate range.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        cur = float(current_atm_iv)
    except Exception:
        return None
    if cur <= 0 or cur != cur:
        return None

    now = int(time.time())
    start = now - int(max(3, lookback_days)) * 86400
    rows: list[float] = []
    try:
        con = _connect()
        cur2 = con.execute(
            "SELECT atm_iv FROM iv_history WHERE ticker = ? AND ts_unix >= ? ORDER BY ts_unix ASC",
            (t, start),
        )
        for (v,) in cur2.fetchall():
            try:
                x = float(v)
                if x > 0 and x == x:
                    rows.append(x)
            except Exception:
                continue
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass

    if len(rows) < 10:
        return None
    lo = min(rows)
    hi = max(rows)
    if hi - lo <= 1e-6:
        return None
    r = (cur - lo) / (hi - lo)
    if r < 0:
        r = 0.0
    if r > 1:
        r = 1.0
    return float(r)

