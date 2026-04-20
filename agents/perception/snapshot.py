"""Assemble MarketDataSnapshot (quote + bar metadata)."""
from __future__ import annotations

import time
from typing import Any

from agents.data.chart_data import fetch_bars
from agents.data.equity_snapshot import fetch_stock_quote

from agents.perception.schemas import MarketDataSnapshot


def build_snapshot(ticker: str, timeframe: str = "1Day", limit: int = 260) -> tuple[MarketDataSnapshot, list[dict[str, Any]]]:
    t = ticker.upper().strip()
    bars, src = fetch_bars(t, timeframe=timeframe, limit=limit)
    q: dict[str, Any] = {}
    try:
        q = fetch_stock_quote(t) or {}
    except Exception:
        pass
    snap = MarketDataSnapshot(
        ticker=t,
        as_of_unix=time.time(),
        bars_timeframe=timeframe,
        bars_count=len(bars),
        bars_source=src,
        quote=q,
        fundamentals_cached=True,
    )
    return snap, bars
