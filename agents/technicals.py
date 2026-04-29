"""
Deterministic technical context derived from OHLCV bars.

Goal: provide a compact, prompt-friendly "market terms" bundle (levels, trend, volume,
range events) so LLM agents form a thesis before choosing an options expression.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any
from zoneinfo import ZoneInfo

from agents.state import TechnicalContext, TechnicalLevel, TrianglePattern

_ET = ZoneInfo("America/New_York")


def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except Exception:
        return None


def _ema(values: list[float], span: int) -> list[float]:
    """
    Simple EMA over the full list. Returns list same length as values.
    For early points, EMA starts at the first value (stable, deterministic).
    """
    if not values:
        return []
    if span <= 1:
        return list(values)
    alpha = 2.0 / (span + 1.0)
    out: list[float] = [float(values[0])]
    for x in values[1:]:
        out.append(alpha * float(x) + (1.0 - alpha) * out[-1])
    return out


def _rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder-style RSI, returns last value only."""
    try:
        if len(values) < period + 2:
            return None
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(values)):
            chg = float(values[i]) - float(values[i - 1])
            gains.append(max(0.0, chg))
            losses.append(max(0.0, -chg))
        # initial averages
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss <= 1e-12:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        return None


def _macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None, float | None]:
    """
    Deterministic MACD last values (macd, signal, hist) using EMAs.
    """
    try:
        if len(values) < slow + signal + 5:
            return None, None, None
        e_fast = _ema(values, fast)
        e_slow = _ema(values, slow)
        macd_series = [float(a) - float(b) for a, b in zip(e_fast, e_slow, strict=False)]
        sig_series = _ema(macd_series, signal)
        macd_last = float(macd_series[-1])
        sig_last = float(sig_series[-1])
        hist_last = float(macd_last - sig_last)
        return macd_last, sig_last, hist_last
    except Exception:
        return None, None, None


def _bbands(values: list[float], period: int = 20, k: float = 2.0) -> tuple[float | None, float | None, float | None, float | None]:
    """
    Bollinger bands (mid, upper, lower, bandwidth) for last bar.
    bandwidth = (upper-lower)/mid
    """
    try:
        if len(values) < period + 2:
            return None, None, None, None
        win = [float(x) for x in values[-period:]]
        mid = sum(win) / period
        sd = _std(win)
        if sd is None:
            return None, None, None, None
        upper = mid + float(k) * sd
        lower = mid - float(k) * sd
        bw = ((upper - lower) / mid) if mid else None
        return float(mid), float(upper), float(lower), (float(bw) if bw is not None else None)
    except Exception:
        return None, None, None, None


def _candle_shape(o: float, h: float, l: float, c: float) -> str:
    """
    Simple last-candle classifier (daily):
    - doji: tiny body vs range
    - hammer / shooting_star: long wick on one side, small body on the other
    """
    try:
        rng = float(h) - float(l)
        if rng <= 1e-9:
            return "unknown"
        body = abs(float(c) - float(o))
        body_pct = body / rng
        if body_pct <= 0.10:
            return "doji"
        upper_wick = float(h) - max(float(o), float(c))
        lower_wick = min(float(o), float(c)) - float(l)
        # hammer: long lower wick, small upper wick, modest body
        if lower_wick >= 2.0 * body and upper_wick <= 0.30 * body:
            return "hammer"
        # shooting star: long upper wick, small lower wick
        if upper_wick >= 2.0 * body and lower_wick <= 0.30 * body:
            return "shooting_star"
        return "other"
    except Exception:
        return "unknown"


def _divergence_simple(
    *,
    closes: list[float],
    indicator: list[float],
    lookback: int = 10,
) -> str:
    """
    Very simple divergence heuristic:
    - bearish: price makes new high vs prior lookback, indicator does NOT make new high
    - bullish: price makes new low vs prior lookback, indicator does NOT make new low
    """
    try:
        if len(closes) < lookback + 2 or len(indicator) < lookback + 2:
            return "none"
        px_now = float(closes[-1])
        ind_now = float(indicator[-1])
        px_prev_win = [float(x) for x in closes[-(lookback + 1) : -1]]
        ind_prev_win = [float(x) for x in indicator[-(lookback + 1) : -1]]
        if not px_prev_win or not ind_prev_win:
            return "none"
        px_prev_hi = max(px_prev_win)
        px_prev_lo = min(px_prev_win)
        ind_prev_hi = max(ind_prev_win)
        ind_prev_lo = min(ind_prev_win)
        if px_now > px_prev_hi and ind_now < ind_prev_hi:
            return "bearish"
        if px_now < px_prev_lo and ind_now > ind_prev_lo:
            return "bullish"
        return "none"
    except Exception:
        return "none"


def _std(xs: list[float]) -> float | None:
    try:
        ys = [float(x) for x in xs if x is not None]
        if len(ys) < 2:
            return None
        m = sum(ys) / len(ys)
        v = sum((y - m) ** 2 for y in ys) / max(1, (len(ys) - 1))
        return math.sqrt(v)
    except Exception:
        return None


@dataclass(frozen=True)
class _Pivot:
    idx: int
    price: float
    kind: str  # "low" | "high"


def _find_pivots(bars: list[dict[str, Any]], left: int = 3, right: int = 3) -> tuple[list[_Pivot], list[_Pivot]]:
    lows: list[_Pivot] = []
    highs: list[_Pivot] = []
    n = len(bars)
    if n < left + right + 3:
        return lows, highs
    lows_arr = [_safe_float(b.get("low")) for b in bars]
    highs_arr = [_safe_float(b.get("high")) for b in bars]
    for i in range(left, n - right):
        lo = lows_arr[i]
        hi = highs_arr[i]
        if lo is None or hi is None:
            continue
        win_lo = [x for x in lows_arr[i - left : i + right + 1] if x is not None]
        win_hi = [x for x in highs_arr[i - left : i + right + 1] if x is not None]
        if win_lo and lo <= min(win_lo):
            lows.append(_Pivot(i, float(lo), "low"))
        if win_hi and hi >= max(win_hi):
            highs.append(_Pivot(i, float(hi), "high"))
    return lows, highs


def _week_key_et(ts_unix: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc).astimezone(_ET).date()
    iso = d.isocalendar()
    return int(iso.year), int(iso.week)


def build_technical_context_from_bars(
    *,
    ticker: str,
    bars: list[dict[str, Any]],
    bars_source: str,
    timeframe: str = "1Day",
) -> TechnicalContext | None:
    if not bars or len(bars) < 30:
        return None

    closes: list[float] = []
    vols: list[float] = []
    times: list[int] = []
    highs: list[float] = []
    lows: list[float] = []

    for b in bars:
        t = int(b.get("time") or 0)
        c = _safe_float(b.get("close"))
        h = _safe_float(b.get("high"))
        lo = _safe_float(b.get("low"))
        v = _safe_float(b.get("volume"))
        if t <= 0 or c is None or h is None or lo is None:
            continue
        times.append(t)
        closes.append(float(c))
        highs.append(float(h))
        lows.append(float(lo))
        vols.append(float(v) if v is not None else 0.0)

    if len(closes) < 30:
        return None

    px_last = float(closes[-1])
    px_prev = float(closes[-2]) if len(closes) >= 2 else px_last
    highs_252 = None
    ath_252_high = None
    dist_to_ath_pct = None
    ema200 = None
    ema200_slope_5d = None
    dist_to_ema200_pct = None
    regime_label = "unknown"

    # EMA10 (short-term "magnet" in strong trends)
    ema10 = None
    dist_to_ema10_pct = None
    try:
        e10 = _ema(closes, 10)
        ema10 = float(e10[-1]) if e10 else None
        if ema10 and ema10 > 0:
            dist_to_ema10_pct = (px_last - ema10) / ema10 * 100.0
    except Exception:
        ema10 = None
        dist_to_ema10_pct = None

    # 252d ATH proximity (rolling 1-year high)
    try:
        window = highs[-252:] if len(highs) >= 252 else highs
        if window:
            ath_252_high = float(max(window))
            if ath_252_high and ath_252_high > 0:
                dist_to_ath_pct = (px_last - ath_252_high) / ath_252_high * 100.0
    except Exception:
        ath_252_high = None
        dist_to_ath_pct = None

    if len(closes) >= 210:
        e200 = _ema(closes, 200)
        ema200 = float(e200[-1])
        try:
            ema200_slope_5d = float(e200[-1] - e200[-6]) / 5.0
        except Exception:
            ema200_slope_5d = None
        if ema200 and ema200 > 0:
            dist_to_ema200_pct = (px_last - ema200) / ema200 * 100.0
            # Regime label (trend vs range) from EMA200 distance + slope.
            # Keep conservative thresholds to avoid whipsaw classification.
            slope = float(ema200_slope_5d or 0.0)
            dist = float(dist_to_ema200_pct or 0.0)
            if abs(dist) < 1.0 or abs(slope) < max(0.0001, abs(ema200) * 0.0002):
                regime_label = "range"
            elif dist > 0 and slope > 0:
                regime_label = "trend_up"
            elif dist < 0 and slope < 0:
                regime_label = "trend_down"
            else:
                # Mixed signals (e.g., above EMA but slope down) -> treat as range/transition.
                regime_label = "range"

    # RSI (momentum context)
    rsi14 = _rsi(closes, 14)
    rsi_state = "unknown"
    if rsi14 is not None:
        if rsi14 >= 70:
            rsi_state = "overbought"
        elif rsi14 <= 30:
            rsi_state = "oversold"
        else:
            rsi_state = "neutral"

    # MACD
    macd, macd_signal, macd_hist = _macd(closes, 12, 26, 9)

    # Bollinger (20)
    bb_mid_20, bb_upper_20, bb_lower_20, bb_bw_20 = _bbands(closes, 20, 2.0)
    bb_squeeze = None
    try:
        # squeeze if bandwidth in the bottom-ish regime: < 4%
        if bb_bw_20 is not None:
            bb_squeeze = bool(float(bb_bw_20) <= 0.04)
    except Exception:
        bb_squeeze = None

    vol_last = float(vols[-1]) if vols else None
    vol_avg20 = None
    vol_ratio20 = None
    vol_avg30 = None
    vol_ratio30 = None
    volume_state = "unknown"
    unusual_volume = None
    volume_confirms_direction = "unknown"
    if len(vols) >= 20:
        recent = vols[-20:]
        s = sum(float(x or 0.0) for x in recent)
        vol_avg20 = (s / 20.0) if s > 0 else None
        if vol_avg20 and vol_avg20 > 0 and vol_last is not None:
            vol_ratio20 = vol_last / vol_avg20
            # Participation label: elevated volume = confirming, low volume = fading
            if vol_ratio20 >= 1.25:
                volume_state = "confirming"
            elif vol_ratio20 <= 0.80:
                volume_state = "fading"
            else:
                volume_state = "neutral"
            unusual_volume = bool(vol_ratio20 >= 1.80)

            # Volume confirms direction: use last close direction with high participation.
            # (Daily bars only; conservative "neither" unless volume is clearly elevated.)
            if vol_ratio20 >= 1.25:
                if px_last > px_prev:
                    volume_confirms_direction = "up"
                elif px_last < px_prev:
                    volume_confirms_direction = "down"
                else:
                    volume_confirms_direction = "neither"
            else:
                volume_confirms_direction = "neither"

    # RVOL proxy over 30d
    try:
        if len(vols) >= 30:
            recent30 = vols[-30:]
            s30 = sum(float(x or 0.0) for x in recent30)
            vol_avg30 = (s30 / 30.0) if s30 > 0 else None
            if vol_avg30 and vol_avg30 > 0 and vol_last is not None:
                vol_ratio30 = vol_last / vol_avg30
    except Exception:
        vol_avg30 = None
        vol_ratio30 = None

    # VCP / contraction proxy (ATH-weekly playbook):
    # - "getting tighter" → daily range variability is small vs the normal daily range
    # Rule we expose for A+ scorecard: std(range, 5d) < avg(range, 20d)
    range_std_5 = range_std_20 = None
    range_avg_20 = None
    vcp_contraction = None
    try:
        ranges = [float(h) - float(l) for h, l in zip(highs, lows, strict=False)]
        if len(ranges) >= 22:
            range_std_5 = _std(ranges[-5:])
            range_std_20 = _std(ranges[-20:])
            try:
                win20 = [float(x) for x in ranges[-20:]]
                range_avg_20 = (sum(win20) / 20.0) if len(win20) >= 20 else None
            except Exception:
                range_avg_20 = None
            if range_std_5 is not None and range_avg_20 is not None and range_avg_20 > 1e-9:
                vcp_contraction = bool(range_std_5 < range_avg_20)
    except Exception:
        range_std_5 = range_std_20 = None
        range_avg_20 = None
        vcp_contraction = None

    # Candle shape + volume-price climax (absorption/exhaustion proxy) on last bar
    candle_shape = "unknown"
    vol_price_climax = None
    try:
        o_last = _safe_float(bars[-1].get("open"))
        h_last = _safe_float(bars[-1].get("high"))
        l_last = _safe_float(bars[-1].get("low"))
        c_last = _safe_float(bars[-1].get("close"))
        if o_last is not None and h_last is not None and l_last is not None and c_last is not None:
            candle_shape = _candle_shape(float(o_last), float(h_last), float(l_last), float(c_last))
            # price "stall" if body is small vs range
            rng = float(h_last) - float(l_last)
            stall = False
            if rng > 1e-9:
                stall = abs(float(c_last) - float(o_last)) <= 0.10 * rng
            # volume surge if RVOL20 >= 2.5 OR RVOL30 >= 2.5
            vr2 = (vol_ratio20 if vol_ratio20 is not None else None)
            vr3 = (vol_ratio30 if vol_ratio30 is not None else None)
            surge = bool((vr2 is not None and vr2 >= 2.5) or (vr3 is not None and vr3 >= 2.5))
            vol_price_climax = bool(surge and stall)
    except Exception:
        candle_shape = "unknown"
        vol_price_climax = None

    # Divergence (very simple, deterministic)
    rsi_divergence = "none"
    macd_divergence = "none"
    try:
        if rsi14 is not None:
            # Build a crude RSI series for divergence check (compute RSI on sliding window).
            # Keep it small to stay deterministic + cheap.
            rsi_series: list[float] = []
            for i in range(max(0, len(closes) - 60), len(closes)):
                r = _rsi(closes[: i + 1], 14)
                if r is not None:
                    rsi_series.append(float(r))
            if len(rsi_series) >= 12:
                rsi_divergence = _divergence_simple(closes=closes[-len(rsi_series):], indicator=rsi_series, lookback=10)
    except Exception:
        rsi_divergence = "none"
    try:
        if macd is not None:
            # MACD series via ema difference (reuse)
            e_fast = _ema(closes, 12)
            e_slow = _ema(closes, 26)
            macd_series = [float(a) - float(b) for a, b in zip(e_fast, e_slow, strict=False)]
            if len(macd_series) >= 14:
                macd_divergence = _divergence_simple(closes=closes, indicator=macd_series, lookback=10)
    except Exception:
        macd_divergence = "none"

    # Inflection summary (single label + tags) derived from the computed flags.
    inflection_tags: list[str] = []
    try:
        if bool(vol_price_climax):
            inflection_tags.append("VOL_PRICE_CLIMAX")
        if candle_shape in ("hammer", "doji", "shooting_star"):
            inflection_tags.append(f"CANDLE_{candle_shape.upper()}")
        if rsi_divergence in ("bullish", "bearish"):
            inflection_tags.append(f"RSI_DIVERGENCE_{rsi_divergence.upper()}")
        if macd_divergence in ("bullish", "bearish"):
            inflection_tags.append(f"MACD_DIVERGENCE_{macd_divergence.upper()}")
        if bool(bb_squeeze):
            inflection_tags.append("BB_SQUEEZE")
    except Exception:
        inflection_tags = []

    # Determine a coarse inflection label.
    # - bullish: reversal/accumulation signals dominate
    # - bearish: exhaustion/distribution signals dominate
    # - volatility: squeeze/coiling without direction
    inflection_point = "none"
    try:
        bull_votes = 0
        bear_votes = 0
        if rsi_divergence == "bullish":
            bull_votes += 1
        if macd_divergence == "bullish":
            bull_votes += 1
        if candle_shape == "hammer":
            bull_votes += 1
        if rsi_divergence == "bearish":
            bear_votes += 1
        if macd_divergence == "bearish":
            bear_votes += 1
        if candle_shape == "shooting_star":
            bear_votes += 1
        if bull_votes > bear_votes and bull_votes >= 1:
            inflection_point = "bullish"
        elif bear_votes > bull_votes and bear_votes >= 1:
            inflection_point = "bearish"
        elif bool(bb_squeeze):
            inflection_point = "volatility"
    except Exception:
        inflection_point = "none"

    # Weekly range (ET calendar weeks)
    prev_week_high = prev_week_low = curr_week_high = curr_week_low = None
    outside_prev_week = None
    outside_week_state = "UNCLEAR"
    try:
        wk_map: dict[tuple[int, int], dict[str, float]] = {}
        for t, hi, lo in zip(times, highs, lows, strict=False):
            k = _week_key_et(t)
            row = wk_map.get(k)
            if not row:
                wk_map[k] = {"high": float(hi), "low": float(lo)}
            else:
                row["high"] = max(float(row["high"]), float(hi))
                row["low"] = min(float(row["low"]), float(lo))
        keys = sorted(wk_map.keys())
        if len(keys) >= 2:
            prev_k = keys[-2]
            curr_k = keys[-1]
            prev = wk_map[prev_k]
            cur = wk_map[curr_k]
            prev_week_high = float(prev["high"])
            prev_week_low = float(prev["low"])
            curr_week_high = float(cur["high"])
            curr_week_low = float(cur["low"])
            outside_prev_week = bool(
                (curr_week_high > prev_week_high) or (curr_week_low < prev_week_low)
            )
            # "confirmed" if outside and last close is also outside on the same side
            if outside_prev_week:
                if prev_week_high is not None and px_last > prev_week_high:
                    outside_week_state = "CONFIRMED"
                elif prev_week_low is not None and px_last < prev_week_low:
                    outside_week_state = "CONFIRMED"
                else:
                    outside_week_state = "REJECTED"
    except Exception:
        pass

    # Support / resistance from pivots (last ~90 bars)
    piv_lows, piv_highs = _find_pivots(bars[-120:], left=3, right=3)
    supports: list[TechnicalLevel] = []
    resistances: list[TechnicalLevel] = []

    def _levels_from_pivots(pivs: list[_Pivot], kind: str) -> list[TechnicalLevel]:
        out: list[TechnicalLevel] = []
        seen: set[int] = set()
        # newest first
        for p in reversed(pivs[-20:]):
            price = float(p.price)
            key = int(round(price * 100))  # 1 cent bucket
            if key in seen:
                continue
            seen.add(key)
            dist = ((price - px_last) / px_last * 100.0) if px_last else 0.0
            last_reaction_unix = None
            try:
                # Map pivot idx (within the sliced bars[-120:]) back to its bar timestamp
                # by using the same slice of parsed `times`.
                _slice_times = times[-min(len(times), 120):]
                if 0 <= p.idx < len(_slice_times):
                    last_reaction_unix = int(_slice_times[p.idx])
            except Exception:
                last_reaction_unix = None
            out.append(
                TechnicalLevel(
                    kind=kind,
                    price=price,
                    source=("pivot_low" if kind == "support" else "pivot_high"),
                    distance_pct=float(dist),
                    confidence=0.55,
                    touches=1,
                    last_reaction_unix=last_reaction_unix,
                )
            )
            if len(out) >= 2:
                break
        return out

    supports = _levels_from_pivots(piv_lows, "support")
    resistances = _levels_from_pivots(piv_highs, "resistance")

    # Add prev-week levels when available (they tend to be meaningful)
    try:
        if prev_week_low is not None and px_last:
            supports.append(
                TechnicalLevel(
                    kind="support",
                    price=float(prev_week_low),
                    source="prev_week_low",
                    distance_pct=((float(prev_week_low) - px_last) / px_last * 100.0),
                    confidence=0.65,
                    touches=0,
                )
            )
        if prev_week_high is not None and px_last:
            resistances.append(
                TechnicalLevel(
                    kind="resistance",
                    price=float(prev_week_high),
                    source="prev_week_high",
                    distance_pct=((float(prev_week_high) - px_last) / px_last * 100.0),
                    confidence=0.65,
                    touches=0,
                )
            )
    except Exception:
        pass

    # Triangle classifier from last pivots (simple, conservative)
    tri = TrianglePattern()
    try:
        last_lows = [p.price for p in piv_lows[-4:]]
        last_highs = [p.price for p in piv_highs[-4:]]
        if len(last_lows) >= 3 and len(last_highs) >= 3:
            lows_up = last_lows[-1] > last_lows[-2] > last_lows[-3]
            highs_dn = last_highs[-1] < last_highs[-2] < last_highs[-3]
            # "flat" within ~0.6% band
            hi_band = (max(last_highs[-3:]) - min(last_highs[-3:])) / max(min(last_highs[-3:]), 1.0)
            lo_band = (max(last_lows[-3:]) - min(last_lows[-3:])) / max(min(last_lows[-3:]), 1.0)
            highs_flat = hi_band <= 0.006
            lows_flat = lo_band <= 0.006

            if lows_up and highs_flat:
                upper = float(mean(last_highs[-3:]))
                lower = float(last_lows[-1])
                height = max(0.0, upper - float(min(last_lows[-3:])))
                tri = TrianglePattern(
                    type="ASCENDING",
                    upper=upper,
                    lower=lower,
                    breakout_rule="daily close > upper",
                    invalidation_rule="daily close < last higher-low",
                    target=(upper + height) if height > 0 else None,
                    confidence=0.55,
                )
            elif highs_dn and lows_flat:
                upper = float(last_highs[-1])
                lower = float(mean(last_lows[-3:]))
                height = max(0.0, float(max(last_highs[-3:])) - lower)
                tri = TrianglePattern(
                    type="DESCENDING",
                    upper=upper,
                    lower=lower,
                    breakout_rule="daily close < lower",
                    invalidation_rule="daily close > last lower-high",
                    target=(lower - height) if height > 0 else None,
                    confidence=0.55,
                )
            elif highs_dn and lows_up:
                upper = float(last_highs[-1])
                lower = float(last_lows[-1])
                height = max(0.0, upper - lower)
                tri = TrianglePattern(
                    type="SYMMETRICAL",
                    upper=upper,
                    lower=lower,
                    breakout_rule="daily close outside upper/lower",
                    invalidation_rule="daily close back inside triangle",
                    target=(upper + height) if height > 0 else None,
                    confidence=0.45,
                )
    except Exception:
        pass

    as_of_unix = int(times[-1]) if times else None

    # Swing stop/target guidance (underlying) derived from nearest levels.
    stop_long = target_long_3r = stop_short = target_short_3r = None
    try:
        # nearest support below px_last
        sups = [lv for lv in (supports or []) if float(getattr(lv, "price", 0.0) or 0.0) < px_last]
        ress = [lv for lv in (resistances or []) if float(getattr(lv, "price", 0.0) or 0.0) > px_last]
        if sups:
            s = max(float(getattr(lv, "price", 0.0) or 0.0) for lv in sups)
            if s > 0 and px_last > s:
                stop_long = s
                target_long_3r = px_last + 3.0 * (px_last - s)
        if ress:
            r = min(float(getattr(lv, "price", 0.0) or 0.0) for lv in ress)
            if r > 0 and r > px_last:
                stop_short = r
                target_short_3r = px_last - 3.0 * (r - px_last)
    except Exception:
        pass

    return TechnicalContext(
        as_of_unix=as_of_unix,
        bars_source=str(bars_source or ""),
        bars_count=len(bars),
        timeframe=timeframe,
        px_last=px_last,
        ema200=ema200,
        ema200_slope_5d=ema200_slope_5d,
        dist_to_ema200_pct=dist_to_ema200_pct,
        regime_label=regime_label,
        rsi14=(round(float(rsi14), 2) if rsi14 is not None else None),
        rsi_state=rsi_state,
        ema10=ema10,
        dist_to_ema10_pct=dist_to_ema10_pct,
        vol_last=vol_last,
        vol_avg20=vol_avg20,
        vol_ratio20=vol_ratio20,
        vol_avg30=vol_avg30,
        vol_ratio30=vol_ratio30,
        volume_state=volume_state,
        unusual_volume=unusual_volume,
        volume_confirms_direction=volume_confirms_direction,
        ath_252_high=ath_252_high,
        dist_to_ath_pct=dist_to_ath_pct,
        range_std_5=range_std_5,
        range_std_20=range_std_20,
        range_avg_20=range_avg_20,
        vcp_contraction=vcp_contraction,
        bb_mid_20=bb_mid_20,
        bb_upper_20=bb_upper_20,
        bb_lower_20=bb_lower_20,
        bb_bandwidth_20=bb_bw_20,
        bb_squeeze=bb_squeeze,
        macd=macd,
        macd_signal=macd_signal,
        macd_hist=macd_hist,
        candle_shape=candle_shape,
        vol_price_climax=vol_price_climax,
        rsi_divergence=rsi_divergence,
        macd_divergence=macd_divergence,
        inflection_point=inflection_point,
        inflection_tags=inflection_tags,
        # iv_rank_30d is filled in ingest_data_node after IV metrics are computed/persisted
        prev_week_high=prev_week_high,
        prev_week_low=prev_week_low,
        curr_week_high=curr_week_high,
        curr_week_low=curr_week_low,
        outside_prev_week=outside_prev_week,
        outside_week_state=outside_week_state,
        supports=supports[:3],
        resistances=resistances[:3],
        triangle=tri,
        stop_long=stop_long,
        target_long_3r=target_long_3r,
        stop_short=stop_short,
        target_short_3r=target_short_3r,
    )


def mean(xs: list[float]) -> float:
    return sum(xs) / max(1, len(xs))

