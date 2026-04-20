"""Phase 1 — Technical analyst as deterministic indicators + rule-based signal."""
from __future__ import annotations

import logging
from typing import Any

from agents.perception import indicators as ind
from agents.perception.schemas import TechnicalReport, TradeSignal, TrendLabel, VolatilityLevel

log = logging.getLogger(__name__)


def build_technical_report(bars: list[dict[str, Any]], ticker: str) -> TechnicalReport:
    if not bars or len(bars) < 30:
        return TechnicalReport(
            signal_confidence=0.2,
            features={"error": "insufficient_bars", "n": len(bars)},
        )

    closes = [float(b["close"]) for b in bars]
    hi = [float(b["high"]) for b in bars]
    lo = [float(b["low"]) for b in bars]

    rsi_s = ind.rsi(closes, 14)
    macd_line, macd_sig, macd_hist = ind.macd(closes)
    bb_u, bb_m, bb_l = ind.bollinger(closes, 20, 2.0)
    sma20 = ind.sma(closes, 20)
    sma50 = ind.sma(closes, 50)

    last = closes[-1]
    rsi_v = ind.last_valid(rsi_s)
    ml = ind.last_valid(macd_line)
    ms = ind.last_valid(macd_sig)
    mh = ind.last_valid(macd_hist)
    bu = ind.last_valid(bb_u)
    bm = ind.last_valid(bb_m)
    bl = ind.last_valid(bb_l)
    s20 = ind.last_valid(sma20)
    s50 = ind.last_valid(sma50)
    atr_v = ind.atr_last(bars, 14)
    atr_pct = (atr_v / last) if atr_v and last else None

    # loose S/R from recent window
    tail = min(60, len(bars))
    recent_lo = min(lo[-tail:])
    recent_hi = max(hi[-tail:])

    vol_level = VolatilityLevel.MEDIUM
    if atr_pct is not None:
        if atr_pct < 0.012:
            vol_level = VolatilityLevel.LOW
        elif atr_pct > 0.035:
            vol_level = VolatilityLevel.HIGH

    trend = TrendLabel.SIDEWAYS
    tconf = 0.45
    if s20 and s50:
        if last > s20 > s50:
            trend, tconf = TrendLabel.UP, 0.65
        elif last < s20 < s50:
            trend, tconf = TrendLabel.DOWN, 0.65
        elif abs(last - s20) / last < 0.01:
            trend, tconf = TrendLabel.SIDEWAYS, 0.55

    # Rule-based composite score [-1, 1]
    score = 0.0
    n_w = 0
    if rsi_v is not None:
        # mid 50 = 0; oversold positive for bounce, overbought negative
        score += max(-1.0, min(1.0, (50.0 - rsi_v) / 40.0))
        n_w += 1
    if mh is not None:
        scale = abs(last) * 0.001 if last else 1.0
        score += max(-1.0, min(1.0, mh / scale))
        n_w += 1
    if s20 and last:
        score += max(-1.0, min(1.0, (last - s20) / (last * 0.05 or 1.0)))
        n_w += 1
    if n_w:
        score /= n_w

    sig = TradeSignal.HOLD
    sconf = min(0.85, 0.35 + abs(score) * 0.5)
    if score > 0.25:
        sig = TradeSignal.BUY
    elif score < -0.25:
        sig = TradeSignal.SELL

    return TechnicalReport(
        trend=trend,
        trend_confidence=round(tconf, 3),
        volatility_level=vol_level,
        rsi14=round(rsi_v, 3) if rsi_v is not None else None,
        macd_line=round(ml, 6) if ml is not None else None,
        macd_signal=round(ms, 6) if ms is not None else None,
        macd_hist=round(mh, 6) if mh is not None else None,
        bb_upper=round(bu, 4) if bu is not None else None,
        bb_middle=round(bm, 4) if bm is not None else None,
        bb_lower=round(bl, 4) if bl is not None else None,
        sma20=round(s20, 4) if s20 is not None else None,
        sma50=round(s50, 4) if s50 is not None else None,
        atr14=round(atr_v, 6) if atr_v is not None else None,
        atr_pct_of_price=round(atr_pct, 6) if atr_pct is not None else None,
        support_level=round(recent_lo, 4),
        resistance_level=round(recent_hi, 4),
        signal=sig,
        signal_confidence=round(sconf, 3),
        features={
            "ticker": ticker.upper(),
            "last_close": round(last, 4),
            "composite_score": round(score, 4),
        },
    )
