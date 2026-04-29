"""
Persistent token usage tracking for all LLM calls.

This is intentionally lightweight: we store one row per LLM call with (agent_role, model, backend,
prompt_tokens, completion_tokens, total_tokens, duration_s, success). This allows:

- total tokens by agent/model/day
- input vs output token totals
- correlating latency with token volume

Storage: local SQLite under cache/ (configurable).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    return Path(os.getenv("TOKEN_USAGE_DB_PATH", "cache/token_usage.sqlite3"))


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_token_usage (
          ts_unix INTEGER NOT NULL,
          agent_role TEXT,
          model TEXT,
          backend TEXT,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          total_tokens INTEGER,
          duration_s REAL,
          success INTEGER
        );
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_tok_ts ON llm_token_usage(ts_unix);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tok_agent ON llm_token_usage(agent_role);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tok_model ON llm_token_usage(model);")
    return con


def record_llm_tokens(
    *,
    agent_role: str | None,
    model: str | None,
    backend: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    duration_s: float | None,
    success: bool,
    ts_unix: int | None = None,
) -> None:
    """
    Record one LLM call's token usage (when available).
    Safe no-op on any database failure.
    """
    ts = int(ts_unix or time.time())
    try:
        con = _connect()
        with con:
            con.execute(
                """
                INSERT INTO llm_token_usage(
                  ts_unix, agent_role, model, backend,
                  prompt_tokens, completion_tokens, total_tokens,
                  duration_s, success
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    str(agent_role) if agent_role else None,
                    str(model) if model else None,
                    str(backend) if backend else None,
                    int(prompt_tokens) if isinstance(prompt_tokens, int) else None,
                    int(completion_tokens) if isinstance(completion_tokens, int) else None,
                    int(total_tokens) if isinstance(total_tokens, int) else None,
                    float(duration_s) if duration_s is not None else None,
                    1 if success else 0,
                ),
            )
            # Keep DB bounded: opportunistically delete rows older than ~60 days.
            cutoff = ts - 60 * 86400
            con.execute("DELETE FROM llm_token_usage WHERE ts_unix < ?", (cutoff,))
    except Exception:
        return
    finally:
        try:
            con.close()
        except Exception:
            pass


def total_tokens_summary(*, lookback_days: int = 30) -> dict[str, Any]:
    """
    Return total prompt/completion/total tokens over the lookback window.
    Also returns per-agent totals for quick debugging.
    """
    now = int(time.time())
    start = now - int(max(1, lookback_days)) * 86400
    out: dict[str, Any] = {
        "window_days": int(lookback_days),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "by_agent": {},
    }
    try:
        con = _connect()
        cur = con.execute(
            """
            SELECT agent_role,
                   COALESCE(SUM(prompt_tokens),0) as p,
                   COALESCE(SUM(completion_tokens),0) as c,
                   COALESCE(SUM(total_tokens),0) as t
              FROM llm_token_usage
             WHERE ts_unix >= ?
             GROUP BY agent_role
            """,
            (start,),
        )
        rows = cur.fetchall()
        for agent_role, p, c, t in rows:
            key = str(agent_role or "unknown")
            out["by_agent"][key] = {
                "prompt_tokens": int(p or 0),
                "completion_tokens": int(c or 0),
                "total_tokens": int(t or 0),
            }
            out["prompt_tokens"] += int(p or 0)
            out["completion_tokens"] += int(c or 0)
            out["total_tokens"] += int(t or 0)
    except Exception:
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out

