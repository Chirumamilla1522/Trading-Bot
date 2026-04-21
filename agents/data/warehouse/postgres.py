"""
PostgreSQL warehouse — durable copy of pulled market data.

Env:
  WAREHOUSE_POSTGRES_URL  — postgresql://user:pass@host:5432/dbname (required to enable)
  WAREHOUSE_AUTO_SCHEMA   — default true: run DDL on API startup

Writes go through a **background thread + queue** so HTTP handlers and SQLite paths stay fast.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_writer_thread: threading.Thread | None = None
_write_queue: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=20_000)
_running = threading.Event()
_psycopg_ok: bool | None = None
_missing_driver_logged = False


def _dsn() -> str:
    import os

    return (os.getenv("WAREHOUSE_POSTGRES_URL") or os.getenv("DATABASE_URL") or "").strip()


def _psycopg_available() -> bool:
    global _psycopg_ok
    if _psycopg_ok is not None:
        return _psycopg_ok
    try:
        import psycopg  # noqa: F401

        _psycopg_ok = True
    except ImportError:
        _psycopg_ok = False
    return _psycopg_ok


def _warn_missing_psycopg_if_configured() -> None:
    global _missing_driver_logged
    if _dsn() and not _psycopg_available() and not _missing_driver_logged:
        _missing_driver_logged = True
        log.warning(
            "WAREHOUSE_POSTGRES_URL is set but psycopg is not installed; "
            "pip install 'psycopg[binary]' to enable the warehouse."
        )


def is_postgres_enabled() -> bool:
    if not _dsn():
        return False
    if not _psycopg_available():
        _warn_missing_psycopg_if_configured()
        return False
    return True


def _connect():
    import psycopg

    return psycopg.connect(_dsn(), autocommit=True)


def connect():
    """Public connection helper (autocommit)."""
    return _connect()


def _split_ddl(sql_text: str) -> list[str]:
    """psycopg executes one statement per call; split on statement boundaries."""
    parts: list[str] = []
    buf: list[str] = []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            block = "\n".join(buf).strip()
            if block:
                parts.append(block)
            buf = []
    if buf:
        block = "\n".join(buf).strip()
        if block:
            parts.append(block)
    return parts


def ensure_schema() -> None:
    if not is_postgres_enabled():
        return
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    statements = _split_ddl(sql)
    with _connect() as conn:
        with conn.cursor() as cur:
            for st in statements:
                cur.execute(st)
    log.info("PostgreSQL warehouse schema ensured (%d statements, %s)", len(statements), schema_path.name)


def _ensure_instrument(cur, symbol: str) -> None:
    cur.execute(
        "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING",
        (symbol.upper(),),
    )


def _flush_daily_bars(symbol: str, bars: list[dict[str, Any]], source: str) -> None:
    if not bars or not is_postgres_enabled():
        return
    sym = symbol.upper()
    rows = []
    for b in bars:
        try:
            ts = int(b["time"])
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            rows.append(
                (
                    sym,
                    d,
                    float(b["open"]),
                    float(b["high"]),
                    float(b["low"]),
                    float(b["close"]),
                    float(b["volume"]) if b.get("volume") is not None else None,
                    source[:32],
                )
            )
        except Exception:
            continue
    if not rows:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_instrument(cur, sym)
            cur.executemany(
                """
                INSERT INTO ohlc_1d (symbol, bar_date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, bar_date) DO UPDATE SET
                  open = EXCLUDED.open,
                  high = EXCLUDED.high,
                  low = EXCLUDED.low,
                  close = EXCLUDED.close,
                  volume = EXCLUDED.volume,
                  source = EXCLUDED.source,
                  ingested_at = now()
                """,
                rows,
            )


def _flush_fundamentals(symbol: str, payload: dict[str, Any], fetched_at_unix: int) -> None:
    if not is_postgres_enabled():
        return
    sym = symbol.upper()
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    ft = datetime.fromtimestamp(int(fetched_at_unix), tz=timezone.utc)
    import hashlib

    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_instrument(cur, sym)
            cur.execute(
                """
                INSERT INTO fundamentals_snapshot (symbol, fetched_at, payload, payload_hash)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (symbol, fetched_at) DO NOTHING
                """,
                (sym, ft, raw, h),
            )
            cur.execute(
                """
                INSERT INTO fundamentals_latest (symbol, fetched_at, payload, payload_hash)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                  fetched_at = EXCLUDED.fetched_at,
                  payload = EXCLUDED.payload,
                  payload_hash = EXCLUDED.payload_hash
                """,
                (sym, ft, raw, h),
            )


def _flush_quote(symbol: str, q: dict[str, Any]) -> None:
    if not is_postgres_enabled():
        return
    sym = symbol.upper()
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_instrument(cur, sym)
            cur.execute(
                """
                INSERT INTO quote_snapshot (symbol, bid, ask, last, prev_close, change_pct, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sym,
                    q.get("bid"),
                    q.get("ask"),
                    q.get("last"),
                    q.get("prev_close"),
                    q.get("change_pct"),
                    (q.get("source") or "")[:64] or None,
                ),
            )


def _writer_loop() -> None:
    while _running.is_set():
        try:
            item = _write_queue.get(timeout=0.4)
        except queue.Empty:
            continue
        try:
            kind = item[0]
            if kind == "daily_bars":
                _, sym, bars, src = item
                _flush_daily_bars(sym, bars, src)
            elif kind == "fundamentals":
                _, sym, payload, ts = item
                _flush_fundamentals(sym, payload, ts)
            elif kind == "quote":
                _, sym, q = item
                _flush_quote(sym, q)
        except Exception as e:
            log.debug("warehouse write failed: %s", e)


def start_warehouse_writer() -> None:
    global _writer_thread
    if not is_postgres_enabled():
        return
    if _writer_thread and _writer_thread.is_alive():
        return
    _running.set()
    _writer_thread = threading.Thread(target=_writer_loop, name="warehouse-pg-writer", daemon=True)
    _writer_thread.start()
    log.info("PostgreSQL warehouse writer thread started (queue=%d max)", _write_queue.maxsize)


def stop_warehouse_writer() -> None:
    _running.clear()
    global _writer_thread
    if _writer_thread:
        _writer_thread.join(timeout=2.0)
        _writer_thread = None


def enqueue_daily_bars(symbol: str, bars: list[dict[str, Any]], source: str = "sqlite_daily") -> None:
    if not is_postgres_enabled():
        return
    try:
        _write_queue.put_nowait(("daily_bars", symbol, bars, source))
    except queue.Full:
        log.warning("warehouse queue full; dropped daily_bars %s", symbol)


def enqueue_fundamentals(symbol: str, payload: dict[str, Any], fetched_at_unix: int) -> None:
    if not is_postgres_enabled():
        return
    try:
        _write_queue.put_nowait(("fundamentals", symbol, payload, int(fetched_at_unix)))
    except queue.Full:
        log.warning("warehouse queue full; dropped fundamentals %s", symbol)


def enqueue_quote(symbol: str, quote: dict[str, Any]) -> None:
    if not is_postgres_enabled():
        return
    try:
        _write_queue.put_nowait(("quote", symbol, quote))
    except queue.Full:
        pass  # quotes are noisy; drop silently
