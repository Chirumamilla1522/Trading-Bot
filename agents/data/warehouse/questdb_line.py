"""
QuestDB — optimized for tick / high-frequency time series.

This module is **optional**. Point ``QUESTDB_HTTP_URL`` at your instance, e.g.:
  http://localhost:9000

Use QuestDB for:
  - Sub-second or tick quotes, L2 deltas, many symbols at high cadence
  - Fast range scans and downsampling

Keep **PostgreSQL** for relational snapshots (fundamentals JSON, news metadata) and
**SQLite** for the UI hot path.

Env:
  QUESTDB_HTTP_URL   — base URL (ILP HTTP endpoint is ``/write``)
  QUESTDB_ENABLED    — ``1`` / ``true`` to send lines
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def is_questdb_enabled() -> bool:
    raw = os.getenv("QUESTDB_ENABLED", "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return bool(_base_url())


def _base_url() -> str:
    return (os.getenv("QUESTDB_HTTP_URL") or "").strip().rstrip("/")


def ingest_line(line: str) -> bool:
    """
    Send one ILP line (newline-terminated) to QuestDB HTTP ``/write``.
    Example line: ``quotes,symbol=AAPL last=187.32 1704067200000000000``
    """
    if not is_questdb_enabled():
        return False
    base = _base_url()
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"{base}/write",
            data=(line if line.endswith("\n") else line + "\n").encode("utf-8"),
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.debug("QuestDB ingest failed: %s", e)
        return False


def ingest_quote_tick(symbol: str, last: float, ts_ns: int | None = None, table: str = "quotes") -> bool:
    """Minimal helper: last trade price as a single-field row."""
    if ts_ns is None:
        import time

        ts_ns = time.time_ns()
    sym = symbol.upper().replace(",", "_").replace(" ", "")
    line = f"{table},symbol={sym} last={last} {ts_ns}"
    return ingest_line(line)
