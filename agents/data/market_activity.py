"""
Top-50 universe + focus for real-time market streaming and JSONL storage.

The UI/agents set ``firm_state.ticker``; Alpaca real-time and disk persistence run only
when that ticker is in ``SP500_TOP50``. Outside the universe: no live stream (scanner
may still use other endpoints).
"""
from __future__ import annotations

import threading
import time
from typing import Iterable

from agents.data.sp500 import SP500_TOP50

_UNIVERSE: frozenset[str] = frozenset(t.upper() for t in SP500_TOP50)

_lock = threading.Lock()
_last_touch: dict[str, float] = {}


def universe_top50() -> frozenset[str]:
    return _UNIVERSE


def is_in_universe(ticker: str) -> bool:
    return ticker.upper().strip() in _UNIVERSE


def touch(ticker: str) -> None:
    """Record last interest in a symbol (auditing / future TTL)."""
    t = ticker.upper().strip()
    if not t:
        return
    with _lock:
        _last_touch[t] = time.time()


def touch_many(tickers: Iterable[str]) -> None:
    for t in tickers:
        touch(t)


def should_stream_realtime(focus_ticker: str) -> bool:
    """True ⇔ we may open Alpaca WS for this focus symbol."""
    return is_in_universe(focus_ticker)


def should_persist_market_data(focus_ticker: str) -> bool:
    """Append JSONL only while streaming is allowed for this focus."""
    return should_stream_realtime(focus_ticker)
