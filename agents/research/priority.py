"""
Bandit-lite priority: allocate LLM budget to names that matter most.

Score = w1 * signal_change + w2 * portfolio + w3 * news + w4 * impact
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agents.research.schema import SignalSnapshot

if TYPE_CHECKING:
    pass

_W1 = float(os.getenv("RESEARCH_PRIORITY_SIGNAL", "2.0"))
_W2 = float(os.getenv("RESEARCH_PRIORITY_PORTFOLIO", "3.0"))
_W3 = float(os.getenv("RESEARCH_PRIORITY_NEWS", "1.5"))
_W4 = float(os.getenv("RESEARCH_PRIORITY_IMPACT", "2.5"))


def portfolio_weight(ticker: str, stock_positions: list, open_positions: list) -> float:
    t = ticker.upper()
    w = 0.0
    for p in stock_positions or []:
        if getattr(p, "ticker", "").upper() == t:
            w += max(float(getattr(p, "market_value", 0) or 0), 1.0)
    for p in open_positions or []:
        sym = (getattr(p, "symbol", "") or "").upper()
        if sym.startswith(t):
            w += 1.0
    return min(w / 50_000.0, 1.0)  # normalize rough


def compute_priority(
    ticker: str,
    snap: SignalSnapshot,
    prev_hash: str,
    new_hash: str,
    stock_positions: list,
    open_positions: list,
) -> float:
    signal_change = 1.0 if prev_hash != new_hash else 0.0
    pw = portfolio_weight(ticker, stock_positions, open_positions)
    news_term = min(snap.news_count_24h / 10.0, 1.0) + min(snap.high_priority_news * 0.2, 1.0)
    impact_term = min(abs(snap.impact_score), 1.0)
    score = (
        _W1 * signal_change
        + _W2 * pw
        + _W3 * news_term
        + _W4 * impact_term
    )
    return round(score, 4)
