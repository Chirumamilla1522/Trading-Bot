"""Phase 1 — Fundamental snapshot from cached yfinance payload."""
from __future__ import annotations

import logging
from typing import Any

from agents.data.fundamentals import fetch_stock_info
from agents.data.fundamentals_db import get_stock_info_cached

from agents.perception.schemas import FundamentalReport

log = logging.getLogger(__name__)


def _note_from_payload(p: dict[str, Any]) -> str:
    pe = p.get("pe_ratio")
    peg = p.get("peg_ratio")
    parts: list[str] = []
    if pe is not None:
        parts.append(f"P/E {pe:.1f}" if isinstance(pe, (int, float)) else f"P/E {pe}")
    if peg is not None:
        try:
            parts.append(f"PEG {float(peg):.2f}")
        except (TypeError, ValueError):
            pass
    margin = p.get("profit_margin")
    if isinstance(margin, (int, float)) and margin is not None:
        parts.append(f"net margin ~{margin*100:.1f}%")
    return "; ".join(parts) if parts else ""


def build_fundamental_report(ticker: str, *, use_cache: bool = True) -> FundamentalReport:
    t = ticker.upper().strip()
    payload: dict[str, Any] | None = None
    if use_cache:
        cached, _ts = get_stock_info_cached(t)
        if cached:
            payload = cached
    if payload is None:
        try:
            payload = fetch_stock_info(t)
        except Exception as e:
            log.debug("fundamental fetch %s: %s", t, e)
            return FundamentalReport(ticker=t, valuation_note="fetch_failed", confidence=0.0)

    pe = payload.get("pe_ratio")
    fpe = payload.get("forward_pe")
    peg = payload.get("peg_ratio")
    mc = payload.get("market_cap")
    note = _note_from_payload(payload)
    # crude valuation tag
    tag = ""
    try:
        if pe is not None and float(pe) > 35:
            tag = "stretched multiples"
        elif pe is not None and float(pe) < 12:
            tag = "low multiple vs market"
    except (TypeError, ValueError):
        pass

    return FundamentalReport(
        ticker=t,
        name=str(payload.get("name") or t),
        sector=str(payload.get("sector") or ""),
        pe_ratio=float(pe) if pe is not None else None,
        forward_pe=float(fpe) if fpe is not None else None,
        peg_ratio=float(peg) if peg is not None else None,
        market_cap=float(mc) if mc is not None else None,
        revenue_growth=float(payload["revenue_growth"])
        if payload.get("revenue_growth") is not None
        else None,
        gross_margin=float(payload["gross_margin"])
        if payload.get("gross_margin") is not None
        else None,
        profit_margin=float(payload["profit_margin"])
        if payload.get("profit_margin") is not None
        else None,
        return_on_equity=float(payload["return_on_equity"])
        if payload.get("return_on_equity") is not None
        else None,
        dividend_yield=float(payload["dividend_yield"])
        if payload.get("dividend_yield") is not None
        else None,
        valuation_note=(note + ("; " if note and tag else "") + tag).strip("; "),
        confidence=0.75 if payload.get("data_source") == "yfinance" else 0.4,
        raw_keys_present=sorted([k for k, v in payload.items() if v is not None])[:40],
    )
