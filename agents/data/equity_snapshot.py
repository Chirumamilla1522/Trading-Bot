"""
Cash-equity account snapshot + stock quotes (Alpaca trading + market data APIs).
Syncs both StockPosition (equities) and Position (options) from broker into FirmState.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

_ET = ZoneInfo("America/New_York")


def classify_us_equity_session_et(now_et: datetime) -> str:
    """
    US equities extended-hours aware: pre 04:00–09:30, regular 09:30–16:00,
    post 16:00–20:00 ET Mon–Fri; outside those windows → closed / weekend.
    """
    if now_et.weekday() >= 5:
        return "weekend"
    mins = now_et.hour * 60 + now_et.minute
    if mins < 4 * 60:
        return "closed"
    if mins < 9 * 60 + 30:
        return "pre"
    if mins < 16 * 60:
        return "regular"
    if mins < 20 * 60:
        return "post"
    return "closed"


def session_from_trade_timestamp_iso(trade_ts_iso: str | None) -> str | None:
    """Derive pre/regular/post from Alpaca trade timestamp (RFC3339)."""
    if not trade_ts_iso:
        return None
    try:
        ts = str(trade_ts_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        et = dt.astimezone(_ET)
        return classify_us_equity_session_et(et)
    except Exception:
        return None

from agents.config import (
    ALPACA_API_KEY,
    ALPACA_BASE_URL,
    ALPACA_DATA_URL,
    ALPACA_SECRET_KEY,
    ALPACA_STOCK_DATA_FEED,
    ALPHA_VANTAGE_API_KEY,
)
from agents.state import FirmState, OptionRight, Position, GreeksSnapshot, StockPosition

log = logging.getLogger(__name__)

# ── TTL caches (avoids hammering Alpaca on every UI poll) ────────────────────
_QUOTE_TTL = 1.0         # seconds per ticker — scanner / quote strip can poll ~1 Hz without stale reads
_ACCOUNT_TTL = 30.0      # full account sync interval
_quote_cache: dict[str, tuple[dict, float]] = {}   # ticker → (data, expire_at)
_last_account_sync: float = 0.0


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


# OCC symbol regex: AAPL240119C00180000
_OCC_RE = re.compile(
    r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$",
    re.IGNORECASE,
)


def _parse_occ_symbol(sym: str) -> tuple[str, str, float, OptionRight] | None:
    """
    Parse an OCC option symbol into (underlying, expiry_YYMMDD, strike, right).
    Returns None if the symbol doesn't match the pattern.
    """
    m = _OCC_RE.match(sym.upper().strip())
    if not m:
        return None
    underlying = m.group(1)
    expiry     = m.group(2)           # YYMMDD
    right      = OptionRight.CALL if m.group(3).upper() == "C" else OptionRight.PUT
    strike     = int(m.group(4)) / 1000.0   # 00180000 → 180.000
    return underlying, expiry, strike, right


def _parse_option_position(row: dict) -> Position | None:
    """
    Convert an Alpaca /v2/positions row with asset_class == 'us_option'
    into a Position model. Returns None on any parse error.
    """
    sym = str(row.get("symbol") or "").upper().strip()
    parsed = _parse_occ_symbol(sym)
    if not parsed:
        log.debug("Could not parse OCC symbol: %s", sym)
        return None
    _, expiry, strike, right = parsed

    try:
        qty     = int(float(row.get("qty") or 0))
        avg     = float(row.get("avg_entry_price") or 0)
        mv      = float(row.get("market_value") or 0)
        u_pl    = float(row.get("unrealized_pl") or 0)
        cb      = float(row.get("cost_basis") or abs(qty) * avg * 100)
        # current_price is market_value / (qty * 100) for options
        cur_px  = mv / (qty * 100) if qty != 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    # Build a minimal GreeksSnapshot so the UI can display price info
    greeks = GreeksSnapshot(
        symbol=sym,
        expiry=expiry,
        strike=strike,
        right=right,
        bid=0.0,
        ask=0.0,
    )

    return Position(
        leg_id      = sym,
        symbol      = sym,
        right       = right,
        strike      = strike,
        expiry      = expiry,
        quantity    = qty,
        avg_cost    = avg,
        current_pnl = u_pl,
        greeks      = greeks,
    )


def sync_alpaca_account_into_state(state: FirmState, force: bool = False) -> bool:
    """
    Pull cash, buying power, equity, stock positions, and option positions from Alpaca.
    Updates both state.stock_positions and state.open_positions.

    Args:
        force: if True, bypass the TTL throttle (use after placing an order).

    Returns True if the request succeeded and state was updated.
    """
    global _last_account_sync
    now = time.monotonic()
    if not force and now - _last_account_sync < _ACCOUNT_TTL:
        return False
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False

    try:
        with httpx.Client(timeout=10.0) as client:
            ar = client.get(f"{ALPACA_BASE_URL}/v2/account", headers=_headers())
            ar.raise_for_status()
            acct = ar.json()

            pr = client.get(f"{ALPACA_BASE_URL}/v2/positions", headers=_headers())
            pr.raise_for_status()
            pos_rows = pr.json()
    except Exception as e:
        log.warning("Alpaca account sync failed: %s", e)
        return False

    _last_account_sync = time.monotonic()

    # ── Account balances ──────────────────────────────────────────────────────
    try:
        state.cash_balance   = float(acct.get("cash") or 0)
        state.buying_power   = float(
            acct.get("buying_power") or acct.get("regt_buying_power") or state.cash_balance
        )
        state.account_equity = float(
            acct.get("equity") or acct.get("portfolio_value") or 0
        )
    except (TypeError, ValueError):
        pass

    # ── Positions: split into equities and options ────────────────────────────
    stocks:  list[StockPosition] = []
    options: list[Position]      = []

    for row in pos_rows:
        asset_class = str(row.get("asset_class") or "").lower()

        if asset_class == "us_equity":
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym:
                continue
            try:
                qty  = float(row.get("qty") or 0)
                avg  = float(row.get("avg_entry_price") or 0)
                mv   = float(row.get("market_value") or 0)
                u_pl = float(row.get("unrealized_pl") or 0)
                cb   = float(row.get("cost_basis") or abs(qty) * avg)
            except (TypeError, ValueError):
                continue
            stocks.append(StockPosition(
                ticker       = sym,
                quantity     = qty,
                avg_cost     = avg,
                market_value = mv,
                unrealized_pl = u_pl,
                cost_basis   = cb,
            ))

        elif asset_class in ("us_option", "option"):
            pos = _parse_option_position(row)
            if pos:
                options.append(pos)
            else:
                log.debug("Skipped unparseable option position: %s", row.get("symbol"))

    state.stock_positions = stocks
    state.open_positions  = options

    if state.account_equity > 0:
        state.risk.current_nav = state.account_equity

    log.info(
        "Account sync OK: cash=%.2f equity=%.2f stocks=%d options=%d",
        state.cash_balance, state.account_equity, len(stocks), len(options),
    )
    return True


def _quote_from_yfinance(ticker: str) -> dict[str, Any] | None:
    """yfinance fallback for bid/ask/last/prev_close when Alpaca is unavailable."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info or {}
    except Exception as e:
        log.debug("yfinance quote fallback %s: %s", ticker, e)
        return None

    def _f(key):
        v = getattr(info, key, None)
        if v is None or v != v:  # NaN check
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _info_float(di: dict, *keys: str) -> float | None:
        for k in keys:
            v = di.get(k)
            if v is not None and v == v:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    now_et = datetime.now(_ET)
    sess = classify_us_equity_session_et(now_et)

    full: dict = {}
    try:
        full = tk.info or {}
    except Exception:
        full = {}
    if not isinstance(full, dict):
        full = {}

    # Prefer pre/post/regular fields by clock so extended hours are not masked.
    last = None
    if sess == "pre":
        last = _info_float(
            full,
            "preMarketPrice",
            "regularMarketPrice",
            "currentPrice",
        )
    elif sess == "post":
        last = _info_float(
            full,
            "postMarketPrice",
            "regularMarketPrice",
            "currentPrice",
        )
    elif sess == "regular":
        last = _f("last_price") or _info_float(
            full,
            "regularMarketPrice",
            "currentPrice",
        )
    else:
        last = _info_float(
            full,
            "postMarketPrice",
            "preMarketPrice",
            "regularMarketPrice",
            "currentPrice",
        )
    if last is None:
        last = _f("last_price")
    if last is None:
        last = _info_float(
            full,
            "postMarketPrice",
            "preMarketPrice",
            "regularMarketPrice",
            "currentPrice",
        )

    prev = _f("previous_close")
    if prev is None:
        prev = _info_float(full, "previousClose", "regularMarketPreviousClose")

    if last is None and prev is None:
        return None

    chg = None
    if last and prev:
        try:
            chg = round((last - prev) / prev * 100.0, 3)
        except ZeroDivisionError:
            pass

    return {
        "ticker": ticker,
        "bid":        _f("bid") or None,
        "ask":        _f("ask") or None,
        "last":       last,
        "prev_close": prev,
        "change_pct": chg,
        "source":     "yfinance",
        "session":    sess,
        "trade_time": None,
    }


def _parse_alpaca_stock_snapshot(snap: dict, ticker: str) -> dict[str, Any] | None:
    """
    Normalize one Alpaca stock snapshot JSON (single- or batch-endpoint shape)
    into the same dict shape as `fetch_stock_quote`.

    Uses latest trade first (includes extended-hours prints on supported feeds),
    then minute aggregate, then developing daily bar. Sets ``session`` from the
    trade timestamp when available, else from current ET clock.
    """
    t = ticker.upper().strip()
    o: dict[str, Any] = {
        "ticker": t,
        "bid": None,
        "ask": None,
        "last": None,
        "prev_close": None,
        "change_pct": None,
        "source": "alpaca",
        "session": None,
        "trade_time": None,
    }
    q = snap.get("latest_quote") or snap.get("latestQuote") or {}
    if not isinstance(q, dict):
        q = {}
    bp = float(q.get("bp") or 0)
    ap = float(q.get("ap") or 0)
    if bp > 0:
        o["bid"] = bp
    if ap > 0:
        o["ask"] = ap
    tr = snap.get("latest_trade") or snap.get("latestTrade") or {}
    if not isinstance(tr, dict):
        tr = {}
    p = float(tr.get("p") or tr.get("price") or 0)
    if p > 0:
        o["last"] = p
    trade_iso = tr.get("t") or tr.get("timestamp")
    if trade_iso:
        o["trade_time"] = str(trade_iso)[:40]
        sess = session_from_trade_timestamp_iso(str(trade_iso))
        if sess:
            o["session"] = sess
    prev = snap.get("prev_daily_bar") or snap.get("prevDailyBar") or {}
    if not isinstance(prev, dict):
        prev = {}
    pc = float(prev.get("c") or prev.get("close") or 0)
    if pc > 0:
        o["prev_close"] = pc

    # Extended-hours / stale-trade fallbacks (snapshot aggregates move in pre/post)
    if o["last"] is None or o["last"] <= 0:
        mb = snap.get("minuteBar") or snap.get("minute_bar") or {}
        if isinstance(mb, dict):
            mc = float(mb.get("c") or mb.get("close") or 0)
            if mc > 0:
                o["last"] = mc
    if o["last"] is None or o["last"] <= 0:
        db = snap.get("dailyBar") or snap.get("daily_bar") or {}
        if isinstance(db, dict):
            dc = float(db.get("c") or db.get("close") or 0)
            if dc > 0:
                o["last"] = dc

    if o["last"] and o["prev_close"]:
        o["change_pct"] = round(
            (o["last"] - o["prev_close"]) / o["prev_close"] * 100.0, 3
        )
    if o["last"] is None and o.get("bid") and o.get("ask"):
        o["last"] = (o["bid"] + o["ask"]) / 2.0
    if o["last"] is None:
        return None
    if o.get("session") is None:
        o["session"] = classify_us_equity_session_et(datetime.now(_ET))
    return o


def fetch_stock_quotes_batch(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """
    Batch snapshot for many tickers (e.g. S&P 500 scanner). Uses Alpaca
    GET /v2/stocks/snapshots; fills `_quote_cache` per symbol.

    Reuses entries still inside `_QUOTE_TTL` so callers can poll every ~1s without
    hitting the network every time.

    Returns: upper-ticker -> quote dict (same keys as `fetch_stock_quote`).
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        u = s.upper().strip()
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    out: dict[str, dict[str, Any]] = {}
    if not uniq:
        return out
    now = time.monotonic()
    need: list[str] = []
    for u in uniq:
        cached, exp = _quote_cache.get(u, ({}, 0.0))
        if cached and now < exp and cached.get("last") is not None:
            out[u] = cached
        else:
            need.append(u)
    if not need:
        return out
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        return out
    _feed = (ALPACA_STOCK_DATA_FEED or "iex").lower()
    chunk_size = 80
    for i in range(0, len(need), chunk_size):
        chunk = need[i : i + chunk_size]
        sym_param = ",".join(chunk)
        try:
            with httpx.Client(timeout=25.0) as client:
                url = f"{ALPACA_DATA_URL}/v2/stocks/snapshots"
                r = client.get(
                    url,
                    headers=_headers(),
                    params={"symbols": sym_param, "feed": _feed},
                )
                if r.status_code == 429:
                    log.debug("Alpaca batch snapshots rate-limited")
                    continue
                if r.status_code != 200:
                    log.debug("Alpaca batch snapshots HTTP %s", r.status_code)
                    continue
                body = r.json()
                raw = body.get("snapshots") if isinstance(body, dict) else None
                snaps = raw if isinstance(raw, dict) else body
                if not isinstance(snaps, dict):
                    continue
                for sym in chunk:
                    snap = snaps.get(sym) or snaps.get(sym.upper())
                    if not isinstance(snap, dict):
                        continue
                    parsed = _parse_alpaca_stock_snapshot(snap, sym)
                    if not parsed:
                        continue
                    key = sym.upper().strip()
                    out[key] = parsed
                    _quote_cache[key] = (parsed, time.monotonic() + _QUOTE_TTL)
        except Exception as e:
            log.debug("Alpaca batch snapshots: %s", e)
    return out


def fetch_stock_quote(ticker: str) -> dict[str, Any]:
    """
    NBBO-style snapshot for the chart header (price pill + quote strip).

    **Alpaca is tried before Alpha Vantage** so bid/ask/last update near real-time
    (including many extended-hours prints on the IEX feed). Alpha Vantage
    GLOBAL_QUOTE is typically **delayed ~15 minutes** on the free tier — fine as
    a backup for charts-driven workflows, but poor for a “live” tape.

    Priority: (1) cache → (2) Alpaca (feed: ALPACA_STOCK_DATA_FEED, default iex) →
    (3) Alpha Vantage → (4) yfinance.
    """
    t = ticker.upper().strip()
    now = time.monotonic()

    cached, expires = _quote_cache.get(t, ({}, 0.0))
    if cached and now < expires:
        return cached

    _empty: dict[str, Any] = {
        "ticker": t,
        "bid": None,
        "ask": None,
        "last": None,
        "prev_close": None,
        "change_pct": None,
        "source": "none",
        "session": None,
        "trade_time": None,
    }

    out: dict[str, Any] | None = None
    _feed = (ALPACA_STOCK_DATA_FEED or "iex").lower()

    # ── 1. Alpaca snapshot (best available “live” last/bid/ask for the strip) ─
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            with httpx.Client(timeout=8.0) as client:
                url = f"{ALPACA_DATA_URL}/v2/stocks/{t}/snapshot"
                r = client.get(url, headers=_headers(), params={"feed": _feed})
                if r.status_code == 429:
                    log.debug("Alpaca rate limit for %s — falling back", t)
                elif r.status_code == 200:
                    snap = r.json()
                    o = _parse_alpaca_stock_snapshot(snap, t)
                    if o is not None:
                        out = o
        except Exception as e:
            log.debug("Alpaca snapshot for %s: %s — falling back", t, e)

    # ── 2. Alpha Vantage (delayed on free tier — backup if Alpaca empty) ─────
    if out is None and ALPHA_VANTAGE_API_KEY:
        try:
            from agents.data.alpha_vantage import fetch_global_quote as av_quote

            av = av_quote(t)
            if av and av.get("last") is not None:
                out = {**_empty, **av}
        except Exception as e:
            log.debug("Alpha Vantage quote for %s: %s — falling back", t, e)

    # ── 3. yfinance fallback ──────────────────────────────────────────────────
    if out is None:
        yf_data = _quote_from_yfinance(t)
        if yf_data:
            out = yf_data

    result = out if out is not None else (cached if cached else _empty)
    _quote_cache[t] = (result, time.monotonic() + _QUOTE_TTL)
    return result
