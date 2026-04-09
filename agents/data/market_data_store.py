"""
Append-only JSONL market events (ticks / book snapshots) when the hub is active.

Layout: ``logs/market_data/{TICKER}.jsonl`` (one file per symbol, rotated by size).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_root = Path(__file__).resolve().parent.parent.parent / "logs" / "market_data"
_MAX_BYTES = int(os.getenv("MARKET_DATA_LOG_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB per ticker file


def _path(ticker: str) -> Path:
    _root.mkdir(parents=True, exist_ok=True)
    return _root / f"{ticker.upper()}.jsonl"


def append_event(ticker: str, channel: str, payload: Any) -> None:
    """Record one hub message (tick, book, candle, reset)."""
    try:
        rec = {
            "ts": time.time(),
            "channel": channel,
            "payload": payload,
        }
        line = json.dumps(rec, default=str) + "\n"
        p = _path(ticker)
        with _lock:
            if p.exists() and p.stat().st_size + len(line) > _MAX_BYTES:
                rotated = p.with_suffix(".jsonl.bak")
                try:
                    if rotated.exists():
                        rotated.unlink()
                    p.rename(rotated)
                except OSError as e:
                    log.debug("market_data rotate %s: %s", p, e)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        log.debug("market_data append %s: %s", ticker, e)
