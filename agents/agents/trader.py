"""
Trader Agent – Deterministic Order Construction & Submission

Converts the approved TradeProposal into a broker-ready order payload
WITHOUT any LLM call. The LLM is error-prone for arithmetic; the order
schema is fixed so deterministic code is safer and faster.
"""
from __future__ import annotations

import logging

from agents.state import AgentDecision, FirmState, ReasoningEntry

log = logging.getLogger(__name__)


def _compute_mid(bid: float, ask: float) -> float | None:
    """Mid price clamped to sensible precision."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return round((bid + ask) / 2, 2)


def _validate_and_build_legs(proposal, greeks_by_symbol: dict) -> tuple[list[dict], list[str]]:
    """
    Map TradeLeg objects → broker order legs.
    Returns (legs_payload, warnings).
    """
    legs: list[dict] = []
    warnings: list[str] = []

    for leg in proposal.legs:
        sym = leg.symbol
        greek = greeks_by_symbol.get(sym)

        if greek:
            mid = _compute_mid(greek.bid, greek.ask)
            bid, ask = greek.bid, greek.ask
        else:
            mid = None
            bid = ask = 0.0
            warnings.append(f"No live quote for {sym}; limit price will be None")

        if mid is not None and mid < 0.01:
            warnings.append(f"{sym} mid ${mid:.3f} is suspiciously cheap — verify")

        legs.append({
            "symbol":      sym,
            "side":        leg.side.value,          # "BUY" | "SELL"
            "qty":         leg.qty,
            "order_type":  "LMT",                   # always LMT for options
            "limit_price": mid,
            "bid":         round(bid, 2),
            "ask":         round(ask, 2),
        })

    return legs, warnings


def trader_node(state: FirmState) -> FirmState:
    if state.trader_decision != AgentDecision.PROCEED:
        state.reasoning_log.append(ReasoningEntry(
            agent="Trader", action="NO_ACTION",
            reasoning=f"Desk Head verdict was {state.trader_decision.value}. No order submitted.",
            inputs={}, outputs={},
        ))
        return state

    if not state.pending_proposal:
        state.reasoning_log.append(ReasoningEntry(
            agent="Trader", action="NO_ACTION",
            reasoning="Desk Head PROCEED but no pending_proposal present.",
            inputs={}, outputs={"missing": "pending_proposal"},
        ))
        return state

    proposal = state.pending_proposal

    # Build symbol → greek lookup (first match wins)
    greeks_by_symbol = {g.symbol: g for g in state.latest_greeks}

    legs_payload, warnings = _validate_and_build_legs(proposal, greeks_by_symbol)

    # Reject if any leg has no price (can't submit blind market order for options)
    legs_without_price = [l["symbol"] for l in legs_payload if l["limit_price"] is None]
    if legs_without_price:
        state.reasoning_log.append(ReasoningEntry(
            agent="Trader", action="ORDER_REJECTED",
            reasoning=(
                f"Cannot submit order: missing limit price for legs {legs_without_price}. "
                "Ensure latest_greeks contains current bid/ask for all proposal legs."
            ),
            inputs={"missing_legs": legs_without_price},
            outputs={},
        ))
        return state

    order_payload = {
        "order_type":    "MULTI_LEG",
        "strategy":      proposal.strategy_name,
        "ticker":        state.ticker,
        "legs":          legs_payload,
        "tif":           "DAY",
        "max_risk":      proposal.max_risk,
        "target_return": proposal.target_return,
        "stop_loss_pct": proposal.stop_loss_pct,
        "take_profit_pct": proposal.take_profit_pct,
        "notes": proposal.rationale[:200] if proposal.rationale else "",
        "warnings":      warnings,
    }

    log.info(
        "Trader: submitting %s order for %s (%d legs, max_risk=$%.0f)",
        proposal.strategy_name, state.ticker, len(legs_payload), proposal.max_risk,
    )

    state.reasoning_log.append(ReasoningEntry(
        agent="Trader", action="ORDER_SUBMITTED",
        reasoning=(
            f"Deterministic order built for '{proposal.strategy_name}' on {state.ticker}. "
            f"{len(legs_payload)} legs. Max risk: ${proposal.max_risk:.0f}. "
            + (f"Warnings: {warnings}" if warnings else "No warnings.")
        ),
        inputs={"strategy": proposal.strategy_name, "legs_count": len(legs_payload)},
        outputs={"order_payload": order_payload},
    ))

    # Submit via EMS
    try:
        from agents.execution.ems import ExecutionManagementSystem
        ems = ExecutionManagementSystem()
        ems.submit(order_payload, state)
    except Exception as e:
        log.error("EMS submission failed: %s", e)
        state.reasoning_log.append(ReasoningEntry(
            agent="Trader", action="EMS_ERROR",
            reasoning=f"EMS.submit raised: {type(e).__name__}: {e}",
            inputs={}, outputs={},
        ))

    return state
