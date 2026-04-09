"""
Alpha Vantage stock OHLC and quotes (https://www.alphavantage.co/documentation/).

Free tier: ~5 calls/min, ~500/day — chart + quote polling should stay within limits.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from agents.config import ALPHA_VANTAGE_API_KEY

log = logging.getLogger(__name__)

AV_BASE = "https://www.alphavantage.co/query"
_ET = ZoneInfo("America/New_York")

_RANGE_DAILY_TF = frozenset({"5D", "1M", "3M", "6M", "1Y"})


def _av_get(params: dict[str, str]) -> dict[str, Any] | None:
    if not ALPHA_VANTAGE_API_KEY:
        return None
    q = {**params, "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.get(AV_BASE, params=q)
            if r.status_code != 200:
                log.warning("Alpha Vantage HTTP %s", r.status_code)
                return None
            data = r.json()
    except Exception as e:
        log.warning("Alpha Vantage request failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("Error Message"):
        log.debug("Alpha Vantage: %s", str(data.get("Error Message"))[:300])
        return None
    if data.get("Note"):
        # Rate-limit / premium nudge
        log.debug("Alpha Vantage Note: %s", str(data.get("Note"))[:300])
        return None
    return data


def _parse_intraday_ts(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
    return int(dt.timestamp())


def _parse_daily_ts(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_ET)
    return int(dt.timestamp())


def _row_ohlc(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    o = float(row["1. open"])
    h = float(row["2. high"])
    lo = float(row["3. low"])
    c = float(row["4. close"])
    v = float(row.get("5. volume", 0) or 0)
    return o, h, lo, c, v


def _time_series_key_for_interval(interval: str) -> str:
    return f"Time Series ({interval})"


def _fetch_intraday(symbol: str, interval: str, n: int) -> list[dict[str, Any]] | None:
    """INTRADAY: outputsize=full returns up to ~30 days of bars (interval-dependent)."""
    data = _av_get(
        {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol,
            "interval": interval,
            "outputsize": "full",
            "datatype": "json",
        }
    )
    if not data:
        return None
    ts_key = _time_series_key_for_interval(interval)
    series = data.get(ts_key)
    if not series or not isinstance(series, dict):
        return None
    rows: list[dict[str, Any]] = []
    for tstr, row in series.items():
        try:
            o, h, lo, c, v = _row_ohlc(row)
            ts = _parse_intraday_ts(tstr)
            entry: dict[str, Any] = {
                "time": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
            }
            if v > 0:
                entry["volume"] = v
            rows.append(entry)
        except Exception:
            continue
    rows.sort(key=lambda x: int(x["time"]))
    if len(rows) > n:
        rows = rows[-n:]
    return rows if rows else None


def _fetch_daily(symbol: str, n: int) -> list[dict[str, Any]] | None:
    """Daily bars: compact ≈100 points; full = 20+ years."""
    outputsize = "full" if n > 100 else "compact"
    data = _av_get(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": outputsize,
            "datatype": "json",
        }
    )
    if not data:
        return None
    series = data.get("Time Series (Daily)")
    if not series or not isinstance(series, dict):
        return None
    rows: list[dict[str, Any]] = []
    for dstr, row in series.items():
        try:
            o, h, lo, c, v = _row_ohlc(row)
            ts = _parse_daily_ts(dstr)
            entry: dict[str, Any] = {
                "time": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
            }
            if v > 0:
                entry["volume"] = v
            rows.append(entry)
        except Exception:
            continue
    rows.sort(key=lambda x: int(x["time"]))
    if len(rows) > n:
        rows = rows[-n:]
    return rows if rows else None


def fetch_ohlc_bars(ticker: str, timeframe: str, n: int) -> list[dict[str, Any]] | None:
    """
    Map terminal timeframe strings to Alpha Vantage functions.
    Returns newest n bars (ascending), or None on failure.
    """
    sym = ticker.upper().strip()
    tf = (timeframe or "1Day").strip()

    if tf in _RANGE_DAILY_TF:
        return _fetch_daily(sym, n)

    if tf == "1Day":
        return _fetch_daily(sym, n)

    av_interval = {
        "1Min": "1min",
        "5Min": "5min",
        "15Min": "15min",
        "1Hour": "60min",
        "1D": "5min",
    }.get(tf)
    if av_interval:
        return _fetch_intraday(sym, av_interval, n)

    return None


def _parse_change_pct(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"([-+]?\d+\.?\d*)", str(s).replace("%", ""))
    if not m:
        return None
    try:
        return round(float(m.group(1)), 3)
    except ValueError:
        return None


def fetch_global_quote(ticker: str) -> dict[str, Any] | None:
    """
    GLOBAL_QUOTE — last price, previous close, change %.
    Bid/ask are not provided; left for other providers.
    """
    from agents.data.equity_snapshot import classify_us_equity_session_et

    t = ticker.upper().strip()
    data = _av_get({"function": "GLOBAL_QUOTE", "symbol": t, "datatype": "json"})
    if not data:
        return None
    gq = data.get("Global Quote")
    if not gq or not isinstance(gq, dict):
        return None
    try:
        last = gq.get("05. price")
        prev = gq.get("08. previous close")
        chg = _parse_change_pct(gq.get("10. change percent"))
        lp = float(last) if last not in (None, "") else None
        pc = float(prev) if prev not in (None, "") else None
        if lp is None and pc is None:
            return None
        return {
            "ticker": t,
            "bid": None,
            "ask": None,
            "last": lp,
            "prev_close": pc,
            "change_pct": chg,
            "source": "alphavantage",
            "session": classify_us_equity_session_et(datetime.now(_ET)),
            "trade_time": None,
        }
    except (TypeError, ValueError):
        return None
