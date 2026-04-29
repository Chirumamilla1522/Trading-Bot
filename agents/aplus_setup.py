"""
Deterministic A+ setup scorecard for naked calls/puts (LONG premium).

This is intentionally lightweight and uses only data already present in FirmState:
- technical_context (EMA200 regime, levels, RSI, volume ratio, stop/targets)
- iv_rank_30d (from persisted ATM IV history)
- desk sentiment + news recency (sentiment_monitor_score, news_newest_age_minutes)
"""

from __future__ import annotations

import time
from typing import Any

from agents.state import APlusSetup, FirmState, TechnicalContext
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

_ET = ZoneInfo("America/New_York")


def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except Exception:
        return None


def compute_aplus_setup(state: FirmState) -> APlusSetup:
    tc: TechnicalContext | None = state.technical_context
    now_unix = int(time.time())
    if not tc:
        return APlusSetup(as_of_unix=now_unix, direction="none", score=0, passed=[], failed=["no_technical_context"])

    px = float(tc.px_last or state.underlying_price or 0.0)
    sent = float(state.sentiment_monitor_score or 0.0)  # [-1, 1]
    news_age_min = _safe_float(state.news_newest_age_minutes)

    # Decide direction (call/put) only when both structure + sentiment agree.
    regime = str(tc.regime_label or "unknown").lower()
    direction = "none"
    if sent >= 0.20 and regime == "trend_up":
        direction = "call"
    elif sent <= -0.20 and regime == "trend_down":
        direction = "put"

    passed: list[str] = []
    failed: list[str] = []
    details: dict[str, Any] = {}

    def _mark(name: str, ok: bool, extra: dict[str, Any] | None = None) -> None:
        (passed if ok else failed).append(name)
        if extra:
            details[name] = extra

    # Mode: if we're in a strong uptrend and near ATH, use an ATH-weekly-call scorecard.
    # Otherwise fall back to the generic A+ confluence scorecard.
    dist_ath = _safe_float(getattr(tc, "dist_to_ath_pct", None))
    near_ath = bool(dist_ath is not None and dist_ath >= -0.5)  # within 0.5% of 1y high
    mode = "generic"
    if direction == "call" and regime == "trend_up" and near_ath:
        mode = "ath_weekly_call"

    # 1) Trend alignment (proxy): EMA200-based regime.
    trend_ok = (direction == "call" and regime == "trend_up") or (direction == "put" and regime == "trend_down")
    _mark("trend_alignment", bool(trend_ok), {"regime_label": regime, "direction": direction, "mode": mode, "dist_to_ath_pct": dist_ath})

    if mode == "ath_weekly_call":
        # ATH checklist (weekly calls)
        # A) VCP / contraction proxy
        vcp_ok = bool(getattr(tc, "vcp_contraction", None))
        _mark(
            "vcp_contraction",
            bool(vcp_ok),
            {
                "range_std_5": getattr(tc, "range_std_5", None),
                "range_std_20": getattr(tc, "range_std_20", None),
                "range_avg_20": getattr(tc, "range_avg_20", None),
            },
        )

        # B) 10D EMA magnet pullback (price near EMA10, not broken)
        dist10 = _safe_float(getattr(tc, "dist_to_ema10_pct", None))
        ema10_ok = bool(dist10 is not None and dist10 >= -1.0 and dist10 <= 1.0)
        _mark("ema10_magnet", bool(ema10_ok), {"dist_to_ema10_pct": dist10, "ema10": getattr(tc, "ema10", None)})

        # C) Relative volume > 2.0 (30d proxy)
        rvol = _safe_float(getattr(tc, "vol_ratio30", None))
        rvol_ok = bool(rvol is not None and rvol >= 2.0)
        _mark("rvol_gt_2", bool(rvol_ok), {"vol_ratio30": rvol})

        # C2) Trigger: must be at/through the ATH (blue-sky breakout), not just "near"
        ath_break_ok = bool(dist_ath is not None and dist_ath >= 0.0)
        _mark("ath_breakout", bool(ath_break_ok), {"dist_to_ath_pct": dist_ath, "ath_252_high": getattr(tc, "ath_252_high", None)})

        # D) RSI 60-68 sweet spot
        rsi = _safe_float(getattr(tc, "rsi14", None))
        rsi_ok = bool(rsi is not None and 60.0 <= rsi <= 68.0)
        _mark("rsi_sweet_spot", bool(rsi_ok), {"rsi14": rsi})

        # E) Power-hour entry window 10:30–11:30 ET (calendar clock)
        # This is timing guidance for weeklies. We should still surface a setup outside the
        # window, but mark it as "wait" rather than hard-fail the whole scorecard.
        try:
            now_et = datetime.fromtimestamp(now_unix, tz=timezone.utc).astimezone(_ET)
            mins = now_et.hour * 60 + now_et.minute
            power_ok = (mins >= (10 * 60 + 30)) and (mins <= (11 * 60 + 30))
        except Exception:
            power_ok = False
            now_et = None
        _mark("power_hour_window", bool(power_ok), {"now_et": (now_et.isoformat() if now_et else None)})

        # Override: structural A+ is 5 filters; timing is guidance.
        structural_passed = [x for x in passed if x != "power_hour_window"]
        structural_failed = [x for x in failed if x != "power_hour_window"]
        structural_score = len(structural_passed)
        required = 5
        rec = "PROCEED" if (direction == "call" and structural_score >= required) else "ABORT"
        details["timing"] = {
            "power_hour_ok": bool(power_ok),
            "recommendation": ("ENTER_IN_POWER_HOUR" if power_ok else "WAIT_FOR_POWER_HOUR"),
        }
        details["aplus_mode"] = mode
        return APlusSetup(
            as_of_unix=int(tc.as_of_unix or now_unix),
            direction=direction,
            score=int(structural_score),
            required=int(required),
            passed=structural_passed,
            failed=structural_failed,
            details=details,
            recommendation=rec,
        )

    # 2) Key level confluence: within 0.2% of a major level (support for calls, resistance for puts).
    levels = (tc.supports or []) if direction == "call" else (tc.resistances or []) if direction == "put" else (tc.supports or []) + (tc.resistances or [])
    min_dist = None
    min_level = None
    for lv in (levels or [])[:8]:
        d = _safe_float(getattr(lv, "distance_pct", None))
        if d is None:
            continue
        ad = abs(float(d))
        if min_dist is None or ad < min_dist:
            min_dist = ad
            min_level = {"kind": getattr(lv, "kind", ""), "price": getattr(lv, "price", None), "source": getattr(lv, "source", ""), "distance_pct": d}
    level_ok = (min_dist is not None and min_dist <= 0.2)
    _mark("key_level_confluence", bool(level_ok), {"min_distance_pct": min_dist, "nearest_level": min_level})

    # 3) High volume confirmation: volume ratio > 2.0 (or unusual_volume flag).
    vr = _safe_float(tc.vol_ratio20)
    vol_ok = bool(tc.unusual_volume) or (vr is not None and vr >= 2.0)
    _mark("volume_spike", bool(vol_ok), {"vol_ratio20": vr, "unusual_volume": tc.unusual_volume})

    # 4) IV sweet spot: IV rank <= 0.80
    ivr = _safe_float(tc.iv_rank_30d)
    iv_ok = (ivr is not None and ivr <= 0.80)
    _mark("iv_rank_low", bool(iv_ok), {"iv_rank_30d": ivr})

    # 5) Catalyst: fresh news (<= 120 min) and high conviction sentiment for direction.
    news_fresh = (news_age_min is not None and news_age_min <= 120.0)
    sent_ok = (direction == "call" and sent >= 0.80) or (direction == "put" and sent <= -0.80)
    catalyst_ok = bool(news_fresh and sent_ok)
    _mark("catalyst", bool(catalyst_ok), {"news_age_min": news_age_min, "sentiment_monitor_score": sent, "direction": direction})

    # 6) Clear invalidation: stop within 1–2% and target implies >= 3R.
    stop = _safe_float(tc.stop_long if direction == "call" else tc.stop_short if direction == "put" else None)
    target = _safe_float(tc.target_long_3r if direction == "call" else tc.target_short_3r if direction == "put" else None)
    dist_stop_pct = None
    rr = None
    inval_ok = False
    if px > 0 and stop is not None and target is not None:
        dist_stop_pct = abs(px - stop) / px * 100.0
        risk = abs(px - stop)
        reward = abs(target - px)
        rr = (reward / risk) if risk > 1e-9 else None
        inval_ok = (dist_stop_pct <= 2.0) and (rr is not None and rr >= 3.0)
    _mark("invalidation_rr", bool(inval_ok), {"stop": stop, "target_3r": target, "stop_dist_pct": dist_stop_pct, "rr": rr})

    score = len(passed)
    required = 5
    rec = "PROCEED" if (direction in ("call", "put") and score >= required) else "ABORT"
    details["aplus_mode"] = mode
    return APlusSetup(
        as_of_unix=int(tc.as_of_unix or now_unix),
        direction=direction,
        score=int(score),
        required=int(required),
        passed=passed,
        failed=failed,
        details=details,
        recommendation=rec,
    )

