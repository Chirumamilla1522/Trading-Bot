"""
Movement Tracker — Tier-1 Always-On Signal Generator

No LLM calls. Reads price history from yfinance (or firm_state price tape)
and computes:

  price_change_pct  — % move from previous session close
  momentum          — EMA(9) minus EMA(21), normalised to ±1
  vol_ratio         — last-bar volume vs 10-day average volume
  movement_signal   — composite signal in [-1.0 bearish … +1.0 bullish]
  movement_anomaly  — True when any single signal breaks threshold

Runs as a lightweight asyncio loop inside tiers.py (every 30 s).
No state written here — tiers.py writes directly to firm_state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
PRICE_MOVE_THRESHOLD = 0.005   # 0.5 %  → anomaly flag
MOMENTUM_THRESHOLD   = 0.003   # EMA-cross magnitude → anomaly flag
VOL_SPIKE_THRESHOLD  = 1.80    # 1.8× avg daily vol → anomaly flag


# ── EMA helper ─────────────────────────────────────────────────────────────────

def _ema(series: list[float], period: int) -> float:
    """Exponential moving average — works on short series too."""
    if not series:
        return 0.0
    if len(series) < period:
        return sum(series) / len(series)
    k = 2.0 / (period + 1)
    val = sum(series[:period]) / period
    for p in series[period:]:
        val = p * k + val * (1.0 - k)
    return val


# ── Core computation ───────────────────────────────────────────────────────────

def compute_movement_signals(
    prices: list[float],
    volumes: list[float],
    prev_close: float,
    avg_volume_10d: float,
) -> dict:
    """
    Compute movement signals from a price/volume history list.

    Args:
        prices:        List of recent close prices, oldest first.
        volumes:       Matching volume list.
        prev_close:    Previous session closing price (for % change).
        avg_volume_10d: 10-day average daily volume (for vol_ratio).

    Returns:
        Dict with keys: price_change_pct, momentum, vol_ratio,
                        movement_signal, anomaly.
    """
    _empty = {
        "price_change_pct": 0.0,
        "momentum":         0.0,
        "vol_ratio":        1.0,
        "movement_signal":  0.0,
        "anomaly":          False,
    }

    if not prices or len(prices) < 2:
        return _empty

    current = prices[-1]

    # Percent change from prev close
    price_change_pct = (current - prev_close) / prev_close if prev_close > 0 else 0.0

    # EMA momentum (EMA9 - EMA21, normalised by price)
    ema9  = _ema(prices, 9)
    ema21 = _ema(prices, 21)
    momentum = (ema9 - ema21) / ema21 if ema21 > 0 else 0.0

    # Volume ratio
    recent_vol = volumes[-1] if volumes else 0.0
    vol_ratio  = recent_vol / avg_volume_10d if avg_volume_10d > 0 else 1.0

    # Composite signal [-1, +1]
    # Each component clipped to [-1, 1] then weighted
    price_contrib = max(-1.0, min(1.0, price_change_pct / 0.02))   # ±2 % → ±1
    mom_contrib   = max(-1.0, min(1.0, momentum        / 0.01))    # ±1 % → ±1
    vol_amplifier = min(2.0, vol_ratio) / 2.0                      # 0 – 1

    raw = (price_contrib * 0.55 + mom_contrib * 0.45) * (0.6 + vol_amplifier * 0.4)
    movement_signal = round(max(-1.0, min(1.0, raw)), 4)

    anomaly = (
        abs(price_change_pct) > PRICE_MOVE_THRESHOLD
        or abs(momentum)       > MOMENTUM_THRESHOLD
        or vol_ratio           > VOL_SPIKE_THRESHOLD
    )

    return {
        "price_change_pct": round(price_change_pct, 5),
        "momentum":         round(momentum, 5),
        "vol_ratio":        round(vol_ratio, 3),
        "movement_signal":  movement_signal,
        "anomaly":          anomaly,
    }


# ── yfinance data fetcher ──────────────────────────────────────────────────────

def fetch_price_data(ticker: str) -> Optional[dict]:
    """
    Pull recent intraday price data from yfinance for signal computation.

    Returns dict with keys:
        prices, volumes, prev_close, avg_volume_10d
    or None on failure.
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)

        # 5-min bars for today + yesterday (for momentum)
        intraday = tk.history(period="2d", interval="5m", auto_adjust=True)
        if intraday.empty:
            return None

        prices  = [float(p) for p in intraday["Close"].tolist()]
        volumes = [float(v) for v in intraday["Volume"].tolist()]

        # Previous session close — last price of previous trading day
        daily = tk.history(period="5d", interval="1d", auto_adjust=True)
        if len(daily) >= 2:
            prev_close = float(daily["Close"].iloc[-2])
        else:
            prev_close = prices[0] if prices else 0.0

        # 10-day average volume
        avg_volume_10d = (
            float(daily["Volume"].tail(10).mean())
            if not daily.empty and "Volume" in daily.columns
            else (float(sum(volumes)) / len(volumes) if volumes else 1.0)
        )

        return {
            "prices":        prices,
            "volumes":       volumes,
            "prev_close":    prev_close,
            "avg_volume_10d": avg_volume_10d,
        }

    except Exception as exc:
        log.debug("movement_tracker fetch_price_data(%s): %s", ticker, exc)
        return None


# ── Public entry point ─────────────────────────────────────────────────────────

def run_movement_tracker(ticker: str, current_price: Optional[float] = None) -> dict:
    """
    Fetch price data and return movement signals for the given ticker.

    If current_price is provided (e.g. from Alpaca WS trade), it is appended
    to the yfinance price list so the signal reflects the very latest tick.
    """
    data = fetch_price_data(ticker)
    if data is None:
        return {
            "price_change_pct": 0.0,
            "momentum":         0.0,
            "vol_ratio":        1.0,
            "movement_signal":  0.0,
            "anomaly":          False,
        }

    prices  = data["prices"]
    volumes = data["volumes"]

    # Splice in live price if available
    if current_price and current_price > 0:
        prices.append(current_price)
        volumes.append(volumes[-1] if volumes else 0.0)

    return compute_movement_signals(
        prices         = prices,
        volumes        = volumes,
        prev_close     = data["prev_close"],
        avg_volume_10d = data["avg_volume_10d"],
    )
