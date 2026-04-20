"""
Filter option snapshots before they feed the LangGraph / desk agents.

Excludes contracts that are:
  - farther than ``AGENT_OPTIONS_MAX_DTE_DAYS`` calendar days to expiry
    (default ~2 months / 60 days), and
  - outside the strike window for the option type (default band
    ``AGENT_OPTIONS_STRIKE_BAND_PCT`` = 0.5, i.e. 50%):
    * **Calls** — inclusive strikes from **current (spot) price** through
      **spot × (1 + band)** (default: spot → 150% of spot, “up to 50% more”).
    * **Puts** — inclusive strikes from **spot × (1 − band)** through **spot**
      (default: 50% of spot → spot, “current down to 50% less”).
    * If ``right`` is missing but ``symbol`` is a parsable OCC/OSI id, call/put is
      inferred from the symbol tail; otherwise symmetric
      ``[spot * (1 - band), spot * (1 + band)]``.

UI/API chain endpoints use the same ``strike_bounds_for_contract`` helper.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.state import GreeksSnapshot

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)).strip()))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def agent_options_max_dte_days() -> int:
    """Max inclusive days-to-expiry kept for agents (default ~2 calendar months)."""
    d = _env_int("AGENT_OPTIONS_MAX_DTE_DAYS", 60)
    return max(7, min(365, d))


def agent_options_strike_band_pct() -> float:
    """Call wing above spot and put wing below spot (default 0.5 → +50% / −50%)."""
    p = _env_float("AGENT_OPTIONS_STRIKE_BAND_PCT", 0.50)
    return max(0.05, min(0.95, p))


def _option_is_call(right: Any) -> bool | None:
    """Return True/False for call/put, or None if unknown."""
    if right is None:
        return None
    if hasattr(right, "value"):
        u = str(getattr(right, "value", "")).strip().upper()
    else:
        u = str(right).strip().upper()
    if u in ("CALL", "C"):
        return True
    if u in ("PUT", "P"):
        return False
    return None


def strike_bounds_for_contract(
    right: Any,
    spot: float,
    band: float,
    *,
    occ_symbol: str | None = None,
) -> tuple[float | None, float | None]:
    """
    Inclusive (low, high) strike bounds vs underlying *spot*.

    * CALL → ``[spot, spot * (1 + band)]`` — spot through “50% more” when band=0.5.
    * PUT  → ``[spot * (1 - band), spot]`` — “50% less” through spot when band=0.5.
    * Unknown ``right`` → try ``occ_symbol`` (OCC tail); else symmetric band.
    """
    if spot <= 0:
        return None, None
    side = _option_is_call(right)
    if side is None and occ_symbol:
        try:
            from agents.data.opra_client import parse_osi_occ_symbol

            parsed = parse_osi_occ_symbol(occ_symbol)
            if parsed:
                _, oright, _ = parsed
                side = _option_is_call(oright)
        except Exception:
            pass
    if side is True:
        return spot, spot * (1.0 + band)
    if side is False:
        return spot * (1.0 - band), spot
    low = (1.0 - band) * spot
    high = (1.0 + band) * spot
    return low, high


def parse_greeks_expiry_str(expiry: str) -> date | None:
    """Parse ``GreeksSnapshot.expiry`` to a calendar date.

    Accepted:
    - YYMMDD (e.g. 260620)
    - YYYYMMDD (e.g. 20260620)
    - YYYY-MM-DD (e.g. 2026-06-20)  ← can appear in persisted state / external APIs
    """
    s = (expiry or "").strip()
    if not s:
        return None
    try:
        # ISO date (from persisted state or some APIs)
        if "-" in s:
            # tolerate datetime suffixes
            return date.fromisoformat(s.split("T", 1)[0])
        if len(s) == 6:
            return date(int("20" + s[:2]), int(s[2:4]), int(s[4:6]))
        if len(s) == 8:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except Exception:
        return None
    return None


def filter_greeks_for_agents(
    snaps: list[Any],
    underlying: float | None,
) -> list[Any]:
    """
    Return a shallow copy of ``snaps`` with far-dated / far-strike contracts removed.

    ``snaps`` may be ``GreeksSnapshot`` instances or duck-typed objects with
    ``expiry``, ``strike`` attributes.
    """
    if not snaps:
        return []

    max_dte = agent_options_max_dte_days()
    band = agent_options_strike_band_pct()
    today = date.today()
    spot = float(underlying) if underlying and float(underlying) > 0 else 0.0

    out: list[Any] = []
    for g in snaps:
        try:
            exp = parse_greeks_expiry_str(str(getattr(g, "expiry", "") or ""))
            if exp is not None:
                dte = (exp - today).days
                if dte < 0 or dte > max_dte:
                    continue
            strike = float(getattr(g, "strike", 0.0) or 0.0)
            sym = str(getattr(g, "symbol", "") or "").strip()
            low, high = strike_bounds_for_contract(
                getattr(g, "right", None), spot, band, occ_symbol=sym or None
            )
            if low is not None and high is not None and strike > 0:
                if strike < low or strike > high:
                    continue
            out.append(g)
        except Exception:
            continue

    if len(out) < len(snaps):
        log.debug(
            "options_chain_filter: %d → %d (max_dte=%d band=%.0f%% spot=%.2f call/put wings)",
            len(snaps),
            len(out),
            max_dte,
            band * 100.0,
            spot,
        )
    if not out and snaps:
        log.warning(
            "options_chain_filter: all %d contracts filtered; agents get empty chain "
            "(max_dte=%d band=%.0f%% spot=%.2f call/put wings)",
            len(snaps),
            max_dte,
            band * 100.0,
            spot,
        )
    return out
