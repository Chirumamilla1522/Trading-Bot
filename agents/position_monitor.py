"""
Deterministic position monitoring and exit recommendation generation.

Goal: once positions exist in the portfolio, generate close/trim recommendations using
simple risk-first rules (profit-taking, stops, time stops).

This intentionally avoids LLM calls. If you later want discretionary management, we can
add a second-stage LLM "PositionManager" that only runs for ambiguous cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from agents.state import (
    FirmState,
    Recommendation,
    StockTradeProposal,
    TradeLeg,
    TradeProposal,
    OrderSide,
    OptionRight,
    PositionMandate,
)
from agents.data.chart_data import fetch_bars


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _return_pct(unrealized_pl: float, cost_basis: float) -> float:
    try:
        cb = float(cost_basis or 0.0)
        if cb <= 0:
            return 0.0
        return float(unrealized_pl or 0.0) / cb
    except Exception:
        return 0.0


def _has_pending_close_rec(state: FirmState, *, key: str) -> bool:
    k = str(key or "").strip().upper()
    if not k:
        return True
    for r in (state.pending_recommendations or []):
        try:
            if getattr(r, "status", "") != "pending":
                continue
            if str(getattr(r, "strategy_name", "")).upper().startswith("CLOSE"):
                if k in str(getattr(r, "strategy_name", "")).upper():
                    return True
        except Exception:
            continue
    return False


def _ema_last(values: list[float], span: int) -> float | None:
    try:
        if not values:
            return None
        if span <= 1:
            return float(values[-1])
        alpha = 2.0 / (span + 1.0)
        ema = float(values[0])
        for x in values[1:]:
            ema = alpha * float(x) + (1.0 - alpha) * ema
        return float(ema)
    except Exception:
        return None


def _maybe_add(rec_list: list[Recommendation], rec: Recommendation | None) -> None:
    if rec is None:
        return
    rec_list.append(rec)


def build_close_recommendations(state: FirmState) -> list[Recommendation]:
    """
    Inspect open positions and return new close recommendations (does not mutate state).
    Uses PositionMandate when present; falls back to conservative defaults.
    """
    out: list[Recommendation] = []

    # ── Options positions ─────────────────────────────────────────────────────
    for pos in (state.open_positions or []):
        sym = str(getattr(pos, "symbol", "") or "").upper().strip()
        if not sym:
            continue
        if _has_pending_close_rec(state, key=sym):
            continue

        # Determine cost basis (USD) and return
        try:
            qty = int(getattr(pos, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if qty == 0:
            continue
        cost_basis = float(abs(qty)) * float(getattr(pos, "avg_cost", 0.0) or 0.0) * 100.0
        r_pct = _return_pct(float(getattr(pos, "current_pnl", 0.0) or 0.0), cost_basis)

        m: PositionMandate | None = None
        try:
            m = state.position_mandates.get(sym)
        except Exception:
            m = None

        tp = float(getattr(m, "take_profit_pct", 0.75) or 0.75) if m is not None else 0.75
        sl = float(getattr(m, "stop_loss_pct", 0.50) or 0.50) if m is not None else 0.50
        tstop = int(getattr(m, "time_stop_days", 7) or 7) if m is not None else 7
        opened_at = getattr(m, "opened_at", None) if m is not None else None

        reason = None
        if r_pct >= tp:
            reason = f"Profit target hit (+{r_pct:.0%} vs take_profit {tp:.0%})."
        elif r_pct <= -sl:
            reason = f"Stop loss hit ({r_pct:.0%} vs stop_loss {sl:.0%})."
        elif opened_at is not None:
            try:
                age_days = (_utcnow() - opened_at).total_seconds() / 86400.0
                if age_days >= float(tstop) and r_pct < 0.10:
                    reason = f"Time stop ({age_days:.1f}d >= {tstop}d) and not working (return {r_pct:.0%})."
            except Exception:
                pass

        # Weekly options overlays (best-effort): theta veto and trailing EMA stop.
        if not reason and opened_at is not None and m is not None:
            try:
                underlying = str(getattr(m, "underlying", "") or "").upper().strip() or str(state.ticker or "").upper().strip()
                # Use intraday 15m bars for trailing EMA + "no-move" veto.
                tf = str(getattr(m, "trailing_ema_timeframe", "15Min") or "15Min")
                bars, _src = fetch_bars(underlying, tf, 120)
                if bars:
                    last_px = float(bars[-1].get("close") or 0.0)
                else:
                    last_px = 0.0

                # Theta veto: after N hours, if underlying is within +/- band from entry -> exit.
                hv = getattr(m, "theta_veto_hours", None)
                band = getattr(m, "theta_veto_band_pct", None)
                entry_px = getattr(m, "entry_underlying_px", None)
                if reason is None and hv is not None and band is not None and entry_px is not None and entry_px > 0 and last_px > 0:
                    age_hours = (_utcnow() - opened_at).total_seconds() / 3600.0
                    move_pct = abs((last_px - float(entry_px)) / float(entry_px) * 100.0)
                    if age_hours >= float(hv) and move_pct <= float(band):
                        reason = (
                            f"Theta veto: {age_hours:.1f}h since entry and underlying moved only {move_pct:.2f}% "
                            f"(≤ {float(band):.2f}%)."
                        )

                # Trailing EMA stop: only after profit, close if last close below EMA(period) on intraday bars.
                if reason is None and bool(getattr(m, "trailing_ema_enabled", False)):
                    act = float(getattr(m, "trailing_activate_profit_pct", 0.20) or 0.20)
                    if r_pct >= act and bars and len(bars) >= 12:
                        closes = [float(b.get("close") or 0.0) for b in bars if (b.get("close") is not None)]
                        period = int(getattr(m, "trailing_ema_period", 10) or 10)
                        ema = _ema_last(closes[-max(30, period + 3) :], period)
                        if ema is not None and last_px > 0 and last_px < float(ema):
                            reason = (
                                f"Trailing EMA stop: return {r_pct:.0%} (≥ {act:.0%}) and {tf} close "
                                f"{last_px:.2f} < EMA{period} {float(ema):.2f}."
                            )
            except Exception:
                pass

        if not reason:
            continue

        # Close side is the opposite of the current position.
        close_side = OrderSide.BUY if qty < 0 else OrderSide.SELL
        close_qty = abs(qty)

        leg = TradeLeg(
            symbol=sym,
            right=OptionRight.CALL if getattr(pos, "right", OptionRight.CALL) == OptionRight.CALL else OptionRight.PUT,
            strike=float(getattr(pos, "strike", 0.0) or 0.0),
            expiry=str(getattr(pos, "expiry", "") or ""),
            side=close_side,
            qty=int(max(1, close_qty)),
        )
        prop = TradeProposal(
            strategy_name=f"Close {sym}",
            legs=[leg],
            max_risk=0.0,
            target_return=0.0,
            rationale=reason,
            confidence=0.8,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
        )
        _maybe_add(out, Recommendation(
            ticker=str(state.ticker or ""),
            asset_type="option",
            strategy_name=f"CLOSE {sym}",
            proposal=prop,
            bull_conviction=int(getattr(state, "bull_conviction", 0) or 0),
            bear_conviction=int(getattr(state, "bear_conviction", 0) or 0),
            desk_head_reasoning=reason,
            confidence=0.8,
        ))

    # ── Stock positions ───────────────────────────────────────────────────────
    for sp in (state.stock_positions or []):
        tkr = str(getattr(sp, "ticker", "") or "").upper().strip()
        if not tkr:
            continue
        if _has_pending_close_rec(state, key=tkr):
            continue
        qty = float(getattr(sp, "quantity", 0.0) or 0.0)
        if abs(qty) < 1e-9:
            continue

        cb = float(getattr(sp, "cost_basis", 0.0) or 0.0)
        r_pct = _return_pct(float(getattr(sp, "unrealized_pl", 0.0) or 0.0), cb)

        m: PositionMandate | None = None
        try:
            m = state.position_mandates.get(tkr)
        except Exception:
            m = None

        tp = float(getattr(m, "take_profit_pct", 0.20) or 0.20) if m is not None else 0.20
        sl = float(getattr(m, "stop_loss_pct", 0.10) or 0.10) if m is not None else 0.10
        tstop = int(getattr(m, "time_stop_days", 5) or 5) if m is not None else 5
        opened_at = getattr(m, "opened_at", None) if m is not None else None

        reason = None
        if r_pct >= tp:
            reason = f"Profit target hit (+{r_pct:.0%} vs take_profit {tp:.0%})."
        elif r_pct <= -sl:
            reason = f"Stop loss hit ({r_pct:.0%} vs stop_loss {sl:.0%})."
        elif opened_at is not None:
            try:
                age_days = (_utcnow() - opened_at).total_seconds() / 86400.0
                if age_days >= float(tstop) and r_pct < 0.05:
                    reason = f"Time stop ({age_days:.1f}d >= {tstop}d) and not working (return {r_pct:.0%})."
            except Exception:
                pass

        if not reason:
            continue

        close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        close_qty = int(max(1, round(abs(qty))))

        stock_close = StockTradeProposal(
            side=close_side,
            qty=float(close_qty),
            order_type="market",
            limit_price=None,
            rationale=reason,
            confidence=0.8,
            stop_loss_pct=None,
            take_profit_pct=None,
        )
        _maybe_add(out, Recommendation(
            ticker=tkr,
            asset_type="stock",
            strategy_name=f"CLOSE {tkr}",
            stock_proposal=stock_close,
            bull_conviction=int(getattr(state, "bull_conviction", 0) or 0),
            bear_conviction=int(getattr(state, "bear_conviction", 0) or 0),
            desk_head_reasoning=reason,
            confidence=0.8,
        ))

    return out

