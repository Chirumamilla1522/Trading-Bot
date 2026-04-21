"""
Stock Specialist – Underlying (cash equity) trade filter / idea generator

Purpose:
- Produce a *stock* (shares) recommendation in parallel with the options pipeline
  so the desk can issue advisory recommendations for both.

This agent is deliberately conservative: if key context is missing, it outputs HOLD.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.config import MODELS
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm
from agents.schemas import StockSpecialistOutput, parse_and_validate
from agents.state import AgentDecision, FirmState, OrderSide, ReasoningEntry, StockTradeProposal


SYSTEM_PROMPT = """ROLE: StockSpecialist (cash equity)
You are the stock (shares) specialist at a multi-asset desk. You do NOT trade options here.
Your job is to decide whether the underlying stock/ETF itself offers a clean trade.

You will be given a JSON context containing:
- ticker, underlying_price, market_regime
- price_change_pct, momentum, vol_ratio, movement_signal, movement_anomaly
- aggregate_sentiment, news_timing_regime, market_bias_score
- portfolio context: current_nav, position_cap_dollars, existing_stock_qty

Rules:
- You MUST use ONLY the provided JSON context. Do NOT invent earnings, levels, or catalysts.
- If underlying_price <= 0 or current_nav <= 0, output HOLD with low confidence.
- Prefer HOLD unless there is alignment across (regime + movement_signal + bias/sentiment).
- If you propose a trade (PROCEED):
  - Choose side BUY or SELL.
  - Choose qty as a small fraction of NAV, respecting position_cap_dollars.
  - Use order_type market unless you have a strong reason for limit.
  - limit_price must be omitted/null unless order_type=limit.
  - Provide stop_loss_pct and take_profit_pct as guidance (0..1), or null if unsure.

Output STRICT JSON:
{
  "decision": "PROCEED" | "HOLD" | "ABORT",
  "side": "BUY" | "SELL",
  "qty": <float>,
  "order_type": "market" | "limit",
  "limit_price": <float or null>,
  "stop_loss_pct": <0..1 or null>,
  "take_profit_pct": <0..1 or null>,
  "confidence": 0.0-1.0,
  "reasoning": "<3-5 sentences citing concrete fields from context>"
}
"""


def stock_specialist_node(state: FirmState) -> FirmState:
    _t0 = __import__("time").time()
    llm = chat_llm(
        MODELS.options_specialist.active,
        agent_role="stock_specialist",
        temperature=0.05,
        max_tokens=650,
    )

    # Existing stock position qty (if any)
    existing_qty = 0.0
    try:
        for p in state.stock_positions or []:
            if (p.ticker or "").upper().strip() == (state.ticker or "").upper().strip():
                existing_qty = float(p.quantity or 0.0)
                break
    except Exception:
        existing_qty = 0.0

    nav = max(float(state.risk.current_nav or 0.0), float(state.account_equity or 0.0), 0.0)
    position_cap = float(nav) * float(state.risk.position_cap_pct or 0.02) if nav > 0 else 0.0

    context = {
        "ticker": state.ticker,
        "underlying_price": float(state.underlying_price or 0.0),
        "market_regime": getattr(state.market_regime, "value", str(state.market_regime)),
        "price_change_pct": float(state.price_change_pct or 0.0) * 100.0,
        "momentum": float(state.momentum or 0.0),
        "vol_ratio": float(state.vol_ratio or 0.0),
        "movement_signal": float(state.movement_signal or 0.0),
        "movement_anomaly": bool(state.movement_anomaly),
        "aggregate_sentiment": float(state.aggregate_sentiment or 0.0),
        "news_timing_regime": str(state.news_timing_regime or "none"),
        "market_bias_score": float(state.market_bias_score or 0.0),
        "portfolio": {
            "current_nav": float(nav or 0.0),
            "position_cap_dollars": float(position_cap or 0.0),
            "existing_stock_qty": float(existing_qty or 0.0),
            "drawdown_pct": float(state.risk.drawdown_pct or 0.0),
        },
    }

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, indent=2)),
    ]

    response = invoke_llm(llm, messages)
    out = parse_and_validate(response.content, StockSpecialistOutput, "StockSpecialist")
    if not out:
        # Safe fallback
        state.stock_decision = AgentDecision.HOLD
        state.stock_confidence = 0.0
        state.pending_stock_proposal = None
        state.reasoning_log.append(ReasoningEntry(
            agent="StockSpecialist",
            action="HOLD",
            reasoning=(response.content or "")[:400],
            inputs={"ticker": state.ticker, "underlying_price": state.underlying_price},
            outputs={"parse_failed": True},
        ))
        return state

    decision = AgentDecision(out.decision)
    state.stock_decision = decision
    state.stock_confidence = float(out.confidence or 0.0)

    stock_prop: StockTradeProposal | None = None
    if decision == AgentDecision.PROCEED:
        # Basic safety: ensure qty and cap
        px = float(state.underlying_price or 0.0)
        qty = float(out.qty or 0.0)
        if px > 0 and qty > 0 and position_cap > 0:
            notional = qty * px
            if notional > position_cap:
                # Clamp quantity down to cap.
                qty = max(0.0, position_cap / px)
        else:
            qty = 0.0
        if qty > 0:
            stock_prop = StockTradeProposal(
                side=OrderSide.BUY if out.side == "BUY" else OrderSide.SELL,
                qty=qty,
                order_type=str(out.order_type),
                limit_price=out.limit_price if str(out.order_type) == "limit" else None,
                rationale=str(out.reasoning or "")[:1800],
                confidence=float(out.confidence or 0.0),
                stop_loss_pct=out.stop_loss_pct,
                take_profit_pct=out.take_profit_pct,
            )

    state.pending_stock_proposal = stock_prop
    state.reasoning_log.append(ReasoningEntry(
        agent="StockSpecialist",
        action=decision.value,
        reasoning=str(out.reasoning or ""),
        inputs={
            "market_regime": context.get("market_regime"),
            "movement_signal": context.get("movement_signal"),
            "market_bias_score": context.get("market_bias_score"),
            "aggregate_sentiment": context.get("aggregate_sentiment"),
        },
        outputs={
            "confidence": state.stock_confidence,
            "side": out.side,
            "qty": float(stock_prop.qty) if stock_prop else 0.0,
            "order_type": out.order_type,
        },
    ))
    try:
        from agents.tracking.mlflow_tracing import log_agent_step
        log_agent_step(
            "stock_specialist",
            inputs={"ticker": state.ticker, "underlying_price": float(state.underlying_price or 0.0)},
            outputs={"decision": decision.value, "confidence": float(state.stock_confidence or 0.0)},
            duration_s=max(0.0, __import__("time").time() - _t0),
        )
    except Exception:
        pass
    return state

