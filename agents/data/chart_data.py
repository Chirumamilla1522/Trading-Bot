"""
OHLC bars for UI charts — Alpaca first (if keys), then Alpha Vantage, then Yahoo; optional synthetic for dev.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Short-lived cache: UI polls /bars every ~15s; dedupes bursts when switching tickers or multiple widgets.
_bars_cache_lock = threading.Lock()
_bars_cache: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]], str]] = {}
_BARS_CACHE_TTL_S = float(os.getenv("CHART_BARS_CACHE_TTL_S", "60"))


def _bars_cache_get(key: tuple[str, str, int]) -> tuple[list[dict[str, Any]], str] | None:
    now = time.time()
    with _bars_cache_lock:
        row = _bars_cache.get(key)
        if not row:
            return None
        ts, bars, src = row
        if now - ts > _BARS_CACHE_TTL_S:
            del _bars_cache[key]
            return None
        return list(bars), src


def _bars_cache_put(key: tuple[str, str, int], bars: list[dict[str, Any]], src: str) -> None:
    with _bars_cache_lock:
        _bars_cache[key] = (time.time(), list(bars), src)
        if len(_bars_cache) > 400:
            # Drop oldest ~half when oversized (simple back-pressure)
            for k in list(_bars_cache.keys())[:200]:
                _bars_cache.pop(k, None)


def _is_alpaca_rate_limit_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "too many" in s or "429" in s or "rate limit" in s


def _alpaca_get_stock_bars_retry(client: Any, req: Any, ticker: str) -> Any:
    """Alpaca REST enforces per-minute limits; backoff and retry on 429 / too many requests."""
    max_attempts = 5
    delay = 1.0
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return client.get_stock_bars(req)
        except Exception as e:
            last_exc = e
            if not _is_alpaca_rate_limit_error(e) or attempt >= max_attempts - 1:
                raise
            log.warning(
                "Alpaca rate limit on %s bars (attempt %s/%s); retry in %.1fs",
                ticker,
                attempt + 1,
                max_attempts,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2.0, 16.0)
    assert last_exc is not None
    raise last_exc


def _synthetic_bars(ticker: str, n: int, bar_seconds: int) -> list[dict[str, Any]]:
    """Deterministic pseudo-OHLC for dev / when Alpaca is unavailable."""
    h = int(hashlib.md5(ticker.upper().encode()).hexdigest()[:8], 16)
    rng = random.Random(h)
    base = 20.0 + (h % 800) / 10.0
    now = int(time.time())
    start = now - n * bar_seconds
    out: list[dict[str, Any]] = []
    for i in range(n):
        t = start + i * bar_seconds
        o = max(0.01, base + rng.gauss(0, base * 0.008))
        c = max(0.01, o + rng.gauss(0, base * 0.006))
        hi = max(o, c) + abs(rng.gauss(0, base * 0.004))
        lo = min(o, c) - abs(rng.gauss(0, base * 0.004))
        out.append(
            {
                "time": t,
                "open": round(o, 4),
                "high": round(hi, 4),
                "low": round(lo, 4),
                "close": round(c, 4),
                "volume": float(rng.randint(100_000, 5_000_000)),
            }
        )
        base = c
    return out


# Multi-day / multi-month **daily** ranges (underlying stock history, not a single calendar day).
_RANGE_DAILY_LIMIT: dict[str, int] = {
    "5D": 7,
    "1M": 22,
    "3M": 66,
    "6M": 126,
    "1Y": 252,
}

_ET = ZoneInfo("America/New_York")


def _filter_bars_last_et_day(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep bars whose America/New_York calendar date matches the latest bar (one session view)."""
    if not bars:
        return []
    last_ts = int(bars[-1]["time"])
    last_day: date = datetime.fromtimestamp(last_ts, tz=timezone.utc).astimezone(_ET).date()
    out: list[dict[str, Any]] = []
    for b in bars:
        ts = int(b["time"])
        d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_ET).date()
        if d == last_day:
            out.append(b)
    return out


def summary_from_bars(bars: list[dict[str, Any]], ticker: str) -> dict[str, Any]:
    """Underlying stock stats from OHLC series (not options)."""
    if not bars:
        return {"ticker": ticker, "has_data": False}
    lo = min(float(b["low"]) for b in bars)
    hi = max(float(b["high"]) for b in bars)
    first_o = float(bars[0]["open"])
    last_c = float(bars[-1]["close"])
    prev_c = float(bars[-2]["close"]) if len(bars) >= 2 else first_o
    period_chg_pct = ((last_c - first_o) / first_o * 100.0) if first_o else 0.0
    day_chg_pct = ((last_c - prev_c) / prev_c * 100.0) if prev_c else 0.0
    vol = sum(float(b.get("volume") or 0) for b in bars)
    return {
        "ticker": ticker,
        "has_data": True,
        "last": last_c,
        "period_open": first_o,
        "period_high": hi,
        "period_low": lo,
        "period_change_pct": round(period_chg_pct, 3),
        "last_bar_change_pct": round(day_chg_pct, 3),
        "volume_total": vol if vol > 0 else None,
        "bars": len(bars),
    }


def fetch_bars(ticker: str, timeframe: str = "1Day", limit: int = 120) -> tuple[list[dict[str, Any]], str]:
    """
    Returns (bars, source) where source is e.g. 'alpaca', 'alphavantage', 'yfinance', 'synthetic', or 'no_data'.

    Each bar: time (unix sec UTC), open, high, low, close, optional volume.

    Timeframes:
      - Intraday: 1D (5m bars, latest US session), 1Min, 5Min, 15Min, 1Hour, 1Day (daily bar size; limit controls depth)
      - Stock history (daily candles): 5D, 1M, 3M, 6M, 1Y → number of **sessions** in range
    """
    t = ticker.upper().strip()
    tf = (timeframe or "1Day").strip()

    # Bar width in seconds (for synthetic spacing only)
    tf_sec = {
        "1D": 300,
        "1Day": 86400,
        "1Hour": 3600,
        "15Min": 900,
        "5Min": 300,
        "1Min": 60,
        **{k: 86400 for k in _RANGE_DAILY_LIMIT},
    }.get(tf, 86400)

    if tf in _RANGE_DAILY_LIMIT:
        n = _RANGE_DAILY_LIMIT[tf]
    else:
        n = max(10, min(500, int(limit)))

    _cache_key = (t, tf, int(limit))
    _cached = _bars_cache_get(_cache_key)
    if _cached is not None:
        return _cached

    def _ret(bars: list[dict[str, Any]], src: str) -> tuple[list[dict[str, Any]], str]:
        _bars_cache_put(_cache_key, bars, src)
        return bars, src

    # Intraday: optional Yahoo (`prepost=True`) so charts show pre/regular/post when
    # Alpaca IEX bars are RTH-centric. Enable with CHART_INTRADAY_USE_YFINANCE=true.
    if (
        tf not in _RANGE_DAILY_LIMIT
        and tf != "1Day"
        and os.getenv("CHART_INTRADAY_USE_YFINANCE", "").lower() in ("1", "true", "yes")
    ):
        yf_bars = _yfinance_bars(t, tf, n)
        if yf_bars:
            if tf == "1D":
                yf_bars = _filter_bars_last_et_day(yf_bars)
            if yf_bars:
                return _ret(yf_bars, "yfinance")

    from agents.config import (
        ALPACA_API_KEY,
        ALPACA_DATA_URL,
        ALPACA_SECRET_KEY,
        ALPACA_STOCK_DATA_FEED,
        ALPHA_VANTAGE_API_KEY,
    )

    def _alpaca_stock_feed():
        from alpaca.data.enums import DataFeed

        return {
            "iex": DataFeed.IEX,
            "sip": DataFeed.SIP,
            "delayed_sip": DataFeed.DELAYED_SIP,
        }.get(ALPACA_STOCK_DATA_FEED, DataFeed.IEX)

    # ── Alpaca (primary; IEX by default — SIP requires paid market-data sub.) ──
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

            _feed = _alpaca_stock_feed()
            client = StockHistoricalDataClient(
                ALPACA_API_KEY,
                ALPACA_SECRET_KEY,
                url_override=ALPACA_DATA_URL,
            )
            tf_map = {
                "1D": TimeFrame(5, TimeFrameUnit.Minute),
                "1Day": TimeFrame.Day,
                "1Hour": TimeFrame.Hour,
                "15Min": TimeFrame(15, TimeFrameUnit.Minute),
                "5Min": TimeFrame(5, TimeFrameUnit.Minute),
                "1Min": TimeFrame.Minute,
                **{k: TimeFrame.Day for k in _RANGE_DAILY_LIMIT},
            }
            alpaca_tf = tf_map.get(tf, TimeFrame.Day)
            # Intraday: give a wide [start, end] so `limit` bars survive nights/weekends;
            # Alpaca returns the newest `limit` bars in range (daily presets use limit-only).
            if tf in _RANGE_DAILY_LIMIT:
                # Supply start+end but NO limit — Alpaca returns oldest-first when
                # limit+start are combined, so we fetch the full window and trim ourselves.
                end_dt = datetime.now(timezone.utc)
                start_dt = end_dt - timedelta(days=max(14, n * 3))  # ~2.5× for weekends
                req = StockBarsRequest(
                    symbol_or_symbols=[t],
                    timeframe=alpaca_tf,
                    start=start_dt,
                    end=end_dt,
                    feed=_feed,
                )
            else:
                end = datetime.now(timezone.utc)
                # Lookback extended so intraday TFs can span multi-day/multi-month ranges
                # when the frontend passes a large limit (e.g. 1Y × 1H = ~1764 bars).
                lookback = {
                    "1D":    timedelta(days=7),
                    "1Min":  timedelta(days=10),
                    "5Min":  timedelta(days=21),
                    "15Min": timedelta(days=90),   # supports up to 3M range
                    "1Hour": timedelta(days=400),  # supports up to 1Y range
                    "1Day":  timedelta(days=450),
                }.get(tf, timedelta(days=30))
                start = end - lookback
                req = StockBarsRequest(
                    symbol_or_symbols=[t],
                    timeframe=alpaca_tf,
                    start=start,
                    end=end,
                    limit=n,
                    feed=_feed,
                )
            bars_obj = _alpaca_get_stock_bars_retry(client, req, t)
            if bars_obj and t in bars_obj.data:
                    rows = bars_obj.data[t]
                    out = []
                    for b in rows:
                        ts = int(b.timestamp.timestamp())
                        row = {
                            "time": ts,
                            "open": float(b.open),
                            "high": float(b.high),
                            "low": float(b.low),
                            "close": float(b.close),
                        }
                        try:
                            row["volume"] = float(b.volume)
                        except Exception:
                            pass
                        out.append(row)
                    if out:
                        if tf == "1D":
                            out = _filter_bars_last_et_day(out)
                        # For daily ranges, Alpaca returns oldest-first across the window;
                        # keep only the most-recent n bars.
                        if tf in _RANGE_DAILY_LIMIT and len(out) > n:
                            out = out[-n:]
                        # Accept only if we got at least half the expected bars
                        min_bars = max(1, n // 2) if tf in _RANGE_DAILY_LIMIT else 1
                        if out and len(out) >= min_bars:
                            return _ret(out, "alpaca")
                        if out:
                            log.warning(
                                "Alpaca returned only %d/%d bars for %s (%s) — trying fallback",
                                len(out), n, t, tf,
                            )
        except Exception as e:
            log.warning("Alpaca bars failed for %s: %s — trying Alpha Vantage/yfinance", t, e)

    # ── Alpha Vantage backup ──────────────────────────────────────────────────
    if ALPHA_VANTAGE_API_KEY:
        try:
            from agents.data.alpha_vantage import fetch_ohlc_bars as av_ohlc

            av_rows = av_ohlc(t, tf, n)
            if av_rows:
                out = list(av_rows)
                if tf == "1D":
                    out = _filter_bars_last_et_day(out)
                if out:
                    return _ret(out, "alphavantage")
        except Exception as e:
            log.warning("Alpha Vantage bars failed for %s: %s — trying yfinance", t, e)

    # ── yfinance fallback ─────────────────────────────────────────────────────
    yf_bars = _yfinance_bars(t, tf, n)
    if yf_bars:
        return _ret(yf_bars, "yfinance")

    # Synthetic OHLC is only for offline/dev — wrong scale vs real stocks if shown by mistake.
    if _chart_synthetic_fallback_allowed():
        syn = _synthetic_bars(t, n, tf_sec)
        if tf == "1D":
            syn = _filter_bars_last_et_day(syn)
        return _ret(syn, "synthetic")
    log.warning(
        "No chart bars for %s (%s); returning empty (set CHART_SYNTHETIC_FALLBACK=true for dev-only fake OHLC)",
        t,
        tf,
    )
    return _ret([], "no_data")


def _chart_synthetic_fallback_allowed() -> bool:
    """Allow deterministic fake bars only when explicitly enabled or no real data keys."""
    import os

    from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPHA_VANTAGE_API_KEY

    raw = os.getenv("CHART_SYNTHETIC_FALLBACK", "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    has_real = (ALPACA_API_KEY and ALPACA_SECRET_KEY) or bool(ALPHA_VANTAGE_API_KEY)
    return not has_real


def _yfinance_bars(ticker: str, timeframe: str, n: int) -> list[dict] | None:
    """
    Pull OHLCV bars from yfinance as a fallback when Alpaca is unavailable.
    Returns None if yfinance is not installed or the fetch fails.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    # Map our timeframe strings to yfinance interval + period/start args
    _YF_INTERVAL: dict[str, str] = {
        "1D":    "5m",
        "1Min":  "1m",
        "5Min":  "5m",
        "15Min": "15m",
        "1Hour": "1h",
        "1Day":  "1d",
        # Daily history ranges → use "1d" interval with explicit period
        "5D":  "1d",
        "1M":  "1d",
        "3M":  "1d",
        "6M":  "1d",
        "1Y":  "1d",
    }
    _YF_PERIOD: dict[str, str] = {
        "5D": "5d",
        "1M": "1mo",
        "3M": "3mo",
        "6M": "6mo",
        "1Y": "1y",
        # Intraday — yfinance caps intraday history differently; use period
        "1D":    "5d",
        "1Min":  "7d",
        "5Min":  "60d",
        "15Min": "60d",
        "1Hour": "730d",
        "1Day":  "2y",
    }

    interval = _YF_INTERVAL.get(timeframe)
    period   = _YF_PERIOD.get(timeframe)
    if not interval or not period:
        return None

    try:
        # Include pre/regular/post session bars for intraday (yfinance)
        _intraday = timeframe not in _RANGE_DAILY_LIMIT and timeframe != "1Day"
        df = yf.Ticker(ticker).history(
            period=period,
            interval=interval,
            auto_adjust=True,
            prepost=_intraday,
        )
        if df is None or df.empty:
            return None
    except Exception as e:
        log.debug("yfinance bars %s/%s: %s", ticker, timeframe, e)
        return None

    rows: list[dict] = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            o  = float(row["Open"])
            h  = float(row["High"])
            lo = float(row["Low"])
            c  = float(row["Close"])
            v  = float(row.get("Volume", 0) or 0)
            if not all(map(lambda x: x == x and x >= 0, [o, h, lo, c])):  # NaN / negative guard
                continue
            entry: dict = {"time": ts, "open": o, "high": h, "low": lo, "close": c}
            if v > 0:
                entry["volume"] = v
            rows.append(entry)
        except Exception:
            continue

    if not rows:
        return None

    # Respect n limit (keep the most recent n bars)
    if len(rows) > n:
        rows = rows[-n:]
    if timeframe == "1D":
        rows = _filter_bars_last_et_day(rows)
    return rows
