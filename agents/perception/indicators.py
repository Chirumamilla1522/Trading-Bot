"""Pure-Python OHLC indicators (no numpy)."""
from __future__ import annotations

import math
from typing import Sequence


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    # seed with sma
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    e = seed
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # Wilder smoothing
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    idx = period
    if avg_l == 0:
        out[idx] = 100.0
    else:
        rs = avg_g / avg_l
        out[idx] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        idx = i + 1
        if avg_l == 0:
            out[idx] = 100.0
        else:
            rs = avg_g / avg_l
            out[idx] = 100.0 - (100.0 / (1.0 + rs))
    return out


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    n = len(closes)
    ef = ema(closes, fast)
    es = ema(closes, slow)
    line: list[float | None] = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            line[i] = float(ef[i]) - float(es[i])
    start = next((i for i, v in enumerate(line) if v is not None), n)
    dense = [float(line[i]) for i in range(start, n)]
    sig_dense = ema(dense, signal) if dense else []
    sig: list[float | None] = [None] * n
    hist: list[float | None] = [None] * n
    for j, idx in enumerate(range(start, n)):
        if j < len(sig_dense) and sig_dense[j] is not None:
            sig[idx] = sig_dense[j]
            if line[idx] is not None:
                hist[idx] = float(line[idx]) - float(sig_dense[j])
    return line, sig, hist


def bollinger(
    closes: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ma = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if ma[i] is None:
            continue
        window = closes[i - period + 1 : i + 1]
        if len(window) != period:
            continue
        m = float(ma[i])
        var = sum((x - m) ** 2 for x in window) / period
        sd = math.sqrt(max(var, 0.0))
        upper[i] = m + num_std * sd
        lower[i] = m - num_std * sd
    return upper, ma, lower


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def last_valid(series: list[float | None]) -> float | None:
    for v in reversed(series):
        if v is not None and v == v:
            return float(v)
    return None


def atr_series_sma(bars: list[dict], period: int = 14) -> list[float | None]:
    """Simple rolling mean of true range (good enough for perception vol)."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < 2:
        return out
    trs: list[float] = []
    for i in range(1, n):
        h, lo = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(true_range(h, lo, pc))
    for i in range(period - 1, len(trs)):
        window = trs[i - period + 1 : i + 1]
        if len(window) == period:
            out[i + 1] = sum(window) / period
    return out


def atr_last(bars: list[dict], period: int = 14) -> float | None:
    s = atr_series_sma(bars, period)
    return last_valid(s)
