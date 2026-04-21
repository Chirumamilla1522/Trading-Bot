"""
SQLite store for **daily** OHLC (Yahoo Finance), warmed for SP500_TOP50 at API startup.

Charts read this first for daily-range timeframes so the UI does not depend on a live
yfinance call per click (avoids timeouts / “Load failed” in the desktop shell).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

def _pg_enabled() -> bool:
    try:
        from agents.data.warehouse import postgres as wh

        return wh.is_postgres_enabled()
    except Exception:
        return False



def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_path() -> Path:
    """
    SQLite file on disk — survives app restarts. Defaults to project ``cache/``.

    - ``BARS_CACHE_DB_PATH`` — explicit file path (recommended for absolute control).
    - ``AGENTIC_DATA_DIR`` — directory for all app data (e.g. ``~/.agentic_trading``);
      uses ``<dir>/daily_bars.sqlite3`` when set and BARS_CACHE_DB_PATH is unset.
    """
    explicit = os.getenv("BARS_CACHE_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    agentic = os.getenv("AGENTIC_DATA_DIR", "").strip()
    if agentic:
        return Path(agentic).expanduser() / "daily_bars.sqlite3"
    return Path("cache/daily_bars.sqlite3")


def _path() -> Path:
    p = _configured_path()
    if not p.is_absolute():
        p = _root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect_raw() -> sqlite3.Connection:
    """Open DB without schema init (used only inside _ensure to avoid recursion)."""
    con = sqlite3.connect(str(_path()), timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


_init_lock = threading.Lock()
_inited = False


def _ensure() -> None:
    """Create ``ticker_daily`` if missing. Safe to call anytime (idempotent)."""
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        con = _connect_raw()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS ticker_daily (
                    ticker TEXT PRIMARY KEY,
                    bars_json TEXT NOT NULL,
                    fetched_at_unix INTEGER NOT NULL
                );
                """
            )
            con.commit()
        finally:
            con.close()
        _inited = True


def _connect() -> sqlite3.Connection:
    _ensure()
    return _connect_raw()


def upsert_daily_bars(ticker: str, bars: list[dict[str, Any]]) -> None:
    t = ticker.upper().strip()
    raw = json.dumps(bars, separators=(",", ":"), ensure_ascii=False)
    # If Postgres warehouse is configured, treat it as the primary store.
    if _pg_enabled():
        try:
            from agents.data.warehouse.postgres import enqueue_daily_bars, ensure_schema

            ensure_schema()
            enqueue_daily_bars(t, bars, "pg_primary")
        except Exception:
            pass
        return
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO ticker_daily(ticker, bars_json, fetched_at_unix) VALUES(?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
              bars_json=excluded.bars_json,
              fetched_at_unix=excluded.fetched_at_unix
            """,
            (t, raw, int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def get_daily_bars(ticker: str) -> tuple[list[dict[str, Any]] | None, int | None]:
    """Return (bars oldest→newest, fetched_at_unix) or (None, None)."""
    t = ticker.upper().strip()
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT bar_date, open, high, low, close, volume, EXTRACT(EPOCH FROM ingested_at)::bigint
                        FROM ohlc_1d
                        WHERE symbol = %s
                        ORDER BY bar_date ASC
                        """,
                        (t,),
                    )
                    rows = cur.fetchall()
            if not rows:
                return None, None
            bars: list[dict[str, Any]] = []
            fetched_at = None
            for d, o, h, lo, c, v, ing in rows:
                # Convert date to UTC midnight unix for chart compatibility
                ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
                entry: dict[str, Any] = {"time": ts, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)}
                if v is not None:
                    entry["volume"] = float(v)
                bars.append(entry)
                if fetched_at is None or int(ing) > int(fetched_at):
                    fetched_at = int(ing)
            return bars, fetched_at
        except Exception:
            return None, None
    con = _connect()
    try:
        row = con.execute(
            "SELECT bars_json, fetched_at_unix FROM ticker_daily WHERE ticker = ?",
            (t,),
        ).fetchone()
        if not row:
            return None, None
        return json.loads(row[0]), int(row[1])
    finally:
        con.close()


def download_yahoo_daily_max(ticker: str) -> list[dict[str, Any]]:
    """Full daily history from Yahoo (same bar shape as chart_data)."""
    try:
        import yfinance as yf
    except ImportError:
        return []

    t = ticker.upper().strip()
    try:
        df = yf.Ticker(t).history(period="max", interval="1d", auto_adjust=True, prepost=False)
        if df is None or df.empty:
            return []
    except Exception as e:
        log.debug("yahoo daily %s: %s", t, e)
        return []

    rows: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            o = float(row["Open"])
            h = float(row["High"])
            lo = float(row["Low"])
            c = float(row["Close"])
            v = float(row.get("Volume", 0) or 0)
            if not all(map(lambda x: x == x and x >= 0, [o, h, lo, c])):
                continue
            entry: dict[str, Any] = {"time": ts, "open": o, "high": h, "low": lo, "close": c}
            if v > 0:
                entry["volume"] = v
            rows.append(entry)
        except Exception:
            continue
    rows.sort(key=lambda x: int(x["time"]))
    # One row per UTC calendar day (Yahoo can return duplicates → chart assertion errors).
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = int(r["time"])
        dk = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        prev = by_day.get(dk)
        if not prev or ts >= int(prev["time"]):
            by_day[dk] = r
    merged = [by_day[k] for k in sorted(by_day.keys())]
    return merged


def warm_sp500_top50_daily(delay_s: float | None = None) -> int:
    """Download and store daily bars for SP500_TOP50. Returns count stored."""
    from agents.data.sp500 import SP500_TOP50

    d = delay_s if delay_s is not None else float(os.getenv("BARS_WARM_DELAY_S", "0.35"))
    n_ok = 0
    for sym in SP500_TOP50:
        try:
            bars = download_yahoo_daily_max(sym)
            if len(bars) >= 20:
                upsert_daily_bars(sym, bars)
                n_ok += 1
        except Exception as e:
            log.warning("Bars cache warm failed %s: %s", sym, e)
        time.sleep(max(0.05, d))
    log.info("Bars cache warm: stored %d/%d tickers (daily, Yahoo max)", n_ok, len(SP500_TOP50))
    return n_ok


# Create schema as soon as this module loads so ``cache/daily_bars.sqlite3`` is never a 0-byte
# file with no tables when you open it in the sqlite3 CLI before the API has run.
try:
    _ensure()
except Exception as exc:
    log.debug("bars_cache_db eager init: %s", exc)
