"""
Persistent per-agent-step timing tracking.

This is a lightweight complement to MLflow: it records one row per step timing so we can
compute averages and percentiles for "how long does each agent take?" even when MLflow is off.

Storage: local SQLite under cache/ (configurable).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    return Path(os.getenv("AGENT_TIMING_DB_PATH", "cache/agent_timing.sqlite3"))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_step_timing (
          ts_unix INTEGER NOT NULL,
          agent TEXT NOT NULL,
          duration_s REAL NOT NULL
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_timing_ts ON agent_step_timing(ts_unix);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_timing_agent ON agent_step_timing(agent);")
    return con


def record_agent_timing(*, agent: str, duration_s: float, ts_unix: int | None = None) -> None:
    """
    Record one step duration. Safe no-op on any database failure.
    """
    try:
        d = float(duration_s)
    except Exception:
        return
    if not (d >= 0.0):
        return
    ts = int(ts_unix or time.time())
    try:
        con = _connect()
        with con:
            con.execute(
                "INSERT INTO agent_step_timing(ts_unix, agent, duration_s) VALUES (?,?,?)",
                (ts, str(agent or "unknown"), float(d)),
            )
            # Bound DB: keep ~60 days.
            cutoff = ts - 60 * 86400
            con.execute("DELETE FROM agent_step_timing WHERE ts_unix < ?", (cutoff,))
    except Exception:
        return
    finally:
        try:
            con.close()
        except Exception:
            pass


def agent_timing_summary(*, lookback_days: int = 30, max_rows_per_agent: int = 5000) -> dict[str, Any]:
    """
    Return avg + p50/p95 per agent over the lookback window.
    """
    now = int(time.time())
    start = now - int(max(1, lookback_days)) * 86400
    out: dict[str, Any] = {
        "window_days": int(lookback_days),
        "overall": {"count": 0, "avg_s": 0.0, "p50_s": None, "p95_s": None},
        "by_agent": {},
    }
    try:
        con = _connect()
        # Basic aggregates
        cur = con.execute(
            """
            SELECT agent,
                   COUNT(1) as n,
                   AVG(duration_s) as avg_s
              FROM agent_step_timing
             WHERE ts_unix >= ?
             GROUP BY agent
            """,
            (start,),
        )
        rows = cur.fetchall()
        if not rows:
            return out

        totals_n = 0
        totals_sum = 0.0
        agents = [str(r[0] or "unknown") for r in rows]
        for agent, n, avg_s in rows:
            n_i = int(n or 0)
            avg_f = float(avg_s or 0.0)
            out["by_agent"][str(agent or "unknown")] = {"count": n_i, "avg_s": avg_f, "p50_s": None, "p95_s": None}
            totals_n += n_i
            totals_sum += avg_f * n_i

        out["overall"]["count"] = totals_n
        out["overall"]["avg_s"] = (totals_sum / totals_n) if totals_n > 0 else 0.0

        # Percentiles (python-side). We cap rows per agent to keep it cheap.
        def _pct(vals: list[float], q: float) -> float | None:
            if not vals:
                return None
            vals.sort()
            i = int(round((len(vals) - 1) * q))
            i = max(0, min(len(vals) - 1, i))
            return float(vals[i])

        overall_vals: list[float] = []
        for a in agents:
            cur2 = con.execute(
                """
                SELECT duration_s
                  FROM agent_step_timing
                 WHERE ts_unix >= ? AND agent = ?
                 ORDER BY ts_unix DESC
                 LIMIT ?
                """,
                (start, a, int(max(1, max_rows_per_agent))),
            )
            vals = [float(x[0]) for x in cur2.fetchall() if x and x[0] is not None]
            out["by_agent"][a]["p50_s"] = _pct(vals[:], 0.50)
            out["by_agent"][a]["p95_s"] = _pct(vals[:], 0.95)
            overall_vals.extend(vals)

        out["overall"]["p50_s"] = _pct(overall_vals[:], 0.50)
        out["overall"]["p95_s"] = _pct(overall_vals[:], 0.95)
    except Exception:
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out

