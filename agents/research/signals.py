"""
Deterministic signal vectors per ticker for dirty detection + priority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from agents.research.schema import SignalSnapshot

if TYPE_CHECKING:
    from agents.state import FirmState


def _hash_snapshot(s: SignalSnapshot) -> str:
    d = s.model_dump(mode="json")
    raw = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def aggregate_news_for_ticker(
    news_feed: list,
    ticker: str,
    hours: float = 24.0,
) -> tuple[int, float, int]:
    """Returns (count, weighted sentiment, high_priority_count)."""
    t = ticker.upper()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    count = 0
    wsum = 0.0
    wden = 0.0
    high_n = 0
    for n in news_feed:
        pub = getattr(n, "published_at", None)
        if pub is None:
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if pub < cutoff:
            continue
        tickers = [x.upper() for x in (getattr(n, "tickers", None) or [])]
        if t not in tickers and t not in (getattr(n, "headline", "") or "").upper():
            continue
        count += 1
        sent = float(getattr(n, "sentiment", 0.0) or 0.0)
        w = 1.0
        wsum += sent * w
        wden += w
        if (getattr(n, "priority", "") or "") == "HIGH":
            high_n += 1
    agg = (wsum / wden) if wden > 0 else 0.0
    return count, agg, high_n


def build_snapshot(
    ticker: str,
    scanner_row: dict[str, Any] | None,
    firm_state: "FirmState",
    news_impact_map: dict[str, Any],
) -> SignalSnapshot:
    t = ticker.upper()
    iv = float((scanner_row or {}).get("avg_iv_30d") or 0.0)
    pc = float((scanner_row or {}).get("pc_ratio") or 0.0)
    px = float((scanner_row or {}).get("last") or (scanner_row or {}).get("underlying_price") or 0.0)
    chg = (scanner_row or {}).get("change_pct")
    if chg is not None:
        chg = float(chg)
    nf = list(firm_state.news_feed)
    n_cnt, n_agg, n_high = aggregate_news_for_ticker(nf, t)
    imp = 0.0
    if t in news_impact_map:
        try:
            imp = float(news_impact_map[t].get("total_impact", 0) or 0)
        except Exception:
            pass
    scan_ok = scanner_row is not None and not (scanner_row or {}).get("error")
    return SignalSnapshot(
        ticker=t,
        iv_30d=iv,
        pc_ratio=pc,
        underlying_price=px,
        change_pct=chg,
        news_count_24h=n_cnt,
        news_sentiment_agg=round(n_agg, 4),
        high_priority_news=n_high,
        impact_score=round(imp, 4),
        scanner_ok=bool(scan_ok),
    )


def snapshot_hash(s: SignalSnapshot) -> str:
    return _hash_snapshot(s)


def build_all_snapshots(
    tickers: list[str],
    get_scan_row: Any,
    firm_state: "FirmState",
) -> dict[str, SignalSnapshot]:
    """get_scan_row: callable(ticker) -> dict | None"""
    nim = getattr(firm_state, "news_impact_map", None) or {}
    out: dict[str, SignalSnapshot] = {}
    for t in tickers:
        row = get_scan_row(t.upper())
        out[t.upper()] = build_snapshot(t, row, firm_state, nim)
    return out
