"""
Strategist – Regime-Aware Trade Proposal Generator

Creates a concrete TradeProposal by matching the current market regime, IV regime,
and sentiment signal to an appropriate options strategy. Uses pre-computed chain
analytics so the LLM focuses on strategy selection, not contract arithmetic.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.config import (
    INCLUDE_RESEARCHER_ARGUMENTS,
    MAX_RESEARCHER_ARGUMENT_CHARS,
    MODELS,
)
from agents.llm_providers import chat_llm
from agents.llm_retry import invoke_llm, invoke_llm_with_metrics
from agents.features import build_chain_analytics
from agents.data.options_chain_filter import parse_greeks_expiry_str
from agents.data.opra_client import occ_expiry_as_date
from agents.schemas import StrategistOutput, parse_and_validate
from agents.state import (
    AgentDecision, FirmState, OptionRight, OrderSide,
    ReasoningEntry, TradeLeg, TradeProposal,
    StockTradeProposal,
)


def _classify_option_structure(proposal: TradeProposal) -> str:
    """
    Classify a proposal into a small set of leg-pattern structures.

    Notes:
    - Even when desk policy is SINGLE-only, we keep this classifier because:
      - it documents the intended taxonomy (SINGLE / VERTICAL / IRON_CONDOR / CALENDAR)
      - it gives the UI and risk layer a stable "shape" label
      - it prevents future prompt changes from silently producing multi-leg structures
    """
    try:
        legs = proposal.legs or []
        if not legs:
            return "OTHER"
        n = len(legs)
        rights = {l.right for l in legs}
        expiries = {str(l.expiry or "") for l in legs}
        strikes = [float(l.strike) for l in legs]

        if n == 1:
            return "SINGLE"

        if n == 2:
            # Vertical: same expiry + same right, different strike.
            if len(expiries) == 1 and len(rights) == 1:
                if abs(strikes[1] - strikes[0]) > 0:
                    return "VERTICAL"
            # Calendar: same right + same strike, different expiry.
            if len(rights) == 1 and len(expiries) == 2:
                if abs(strikes[1] - strikes[0]) < 1e-9:
                    return "CALENDAR"
            return "OTHER"

        if n == 4 and len(expiries) == 1:
            puts = [l for l in legs if l.right == OptionRight.PUT]
            calls = [l for l in legs if l.right == OptionRight.CALL]
            if len(puts) == 2 and len(calls) == 2:
                return "IRON_CONDOR"
        return "OTHER"
    except Exception:
        return "OTHER"


# Strategy catalog: display label → (structure bucket, leg-rights family)
# Desk policy (2026-04): SINGLE-leg strategies only.
# rights: CALL | PUT
_STRATEGY_DEFS: dict[str, tuple[str, str]] = {
    "Short Put (naked)": ("SINGLE", "PUT"),
    "Short Call (naked)": ("SINGLE", "CALL"),
}

# Full regime matrix: (market_regime, iv_regime, [strategy labels])
_REGIME_MATRIX_ROWS: list[tuple[str, str, list[str]]] = [
    (
        "TRENDING_UP",
        "LOW/NORMAL",
        ["Short Put (naked)"],
    ),
    (
        "TRENDING_UP",
        "ELEVATED",
        ["Short Put (naked)"],
    ),
    (
        "TRENDING_DOWN",
        "ELEVATED",
        ["Short Call (naked)"],
    ),
    (
        "TRENDING_DOWN",
        "EXTREME",
        ["Short Call (naked)", "Short Put (naked)"],
    ),
    (
        "MEAN_REVERTING",
        "ELEVATED",
        ["Short Put (naked)", "Short Call (naked)"],
    ),
    (
        "HIGH_VOL",
        "ELEVATED/EXTREME",
        ["Short Call (naked)", "Short Put (naked)"],
    ),
    (
        "LOW_VOL",
        "LOW",
        ["Short Call (naked)", "Short Put (naked)"],
    ),
]


def _normalize_allowed_structures(structs: list[str] | None) -> set[str]:
    vals = [str(x or "").strip().upper() for x in (structs or []) if str(x or "").strip()]
    if not vals:
        return {"SINGLE"}
    if "ALL" in vals:
        return {"ALL"}
    return set(vals)


# Desk policy: single-leg only, so no "requires ALL structures" guide exceptions.
_GUIDE_REQUIRES_ALL_STRUCTURES = frozenset()


def _strategy_allowed(
    label: str,
    allowed_structs: set[str],
    allowed_rights: str,
) -> bool:
    """Whether this strategy label may appear in the regime guide."""
    spec = _STRATEGY_DEFS.get(label)
    if not spec:
        return False
    sk, rk = spec
    if label in _GUIDE_REQUIRES_ALL_STRUCTURES and "ALL" not in allowed_structs:
        return False
    if "ALL" not in allowed_structs and sk not in allowed_structs:
        return False
    ar = (allowed_rights or "BOTH").strip().upper()
    if ar == "BOTH":
        return True
    if ar not in ("CALL", "PUT"):
        return True
    return rk == ar


def _filter_row_strategies(
    labels: list[str],
    allowed_structs: set[str],
    allowed_rights: str,
) -> list[str]:
    return [lb for lb in labels if _strategy_allowed(lb, allowed_structs, allowed_rights)]


def _build_regime_strategy_guide(
    allowed_option_rights: str | None,
    allowed_option_structures: list[str] | None,
) -> str:
    """
    Regime → strategy table filtered by user preference (option rights + allowed structures).
    """
    # Avoid large ASCII tables (token heavy); provide only the minimum policy guidance.
    ar = (allowed_option_rights or "BOTH").strip().upper()
    if ar not in ("CALL", "PUT", "BOTH"):
        ar = "BOTH"
    allowed_structs = _normalize_allowed_structures(allowed_option_structures)
    allowed_structs_txt = ", ".join(sorted(list(allowed_structs))) if allowed_structs else "SINGLE"
    return "\n".join([
        "REGIME→STRATEGY GUIDE:",
        f"- allowed_option_rights: {ar}",
        f"- allowed_option_structures: {allowed_structs_txt}",
        "- Preferred mapping (when safe):",
        "  - TRENDING_UP → Short Put (naked)",
        "  - TRENDING_DOWN → Short Call (naked)",
        "  - otherwise → HOLD unless clear mean-reversion at a level",
        "- Skew modifier: skew_ratio > 1.25 → puts premium (prefer short put); skew_ratio < 0.85 → calls premium (prefer short call).",
        "- Sentiment modifier: sentiment > 0.4 → lean bullish; sentiment < -0.3 → lean bearish.",
        "",
    ])

SYSTEM_PROMPT_HEAD = """ROLE: Strategist (options or shares)
You are the strategy constructor. Your job is to choose ONE trade expression for THIS ticker RIGHT NOW:
- EITHER an options proposal (SINGLE-leg naked short call/put only), OR
- a stock proposal (BUY/SELL shares)

You will be given a JSON context containing:
- ticker, underlying_price, market_regime
- technical_context (if present): regime_label/volume_state + key levels + outside-week/triangle flags
- iv_regime, iv_atm, skew_ratio, term_structure
- aggregate_sentiment, news_timing_regime, market_bias_score, movement_signal, price_change_pct
- near_atm_contracts (OCC symbols), position_cap_dollars, current_nav

Rules:
- You MUST use ONLY the provided JSON context. Do NOT invent symbols, events, earnings, or levels.
- Output must be VALID JSON only (no markdown).
- ONLY use OCC symbols that appear in near_atm_contracts for options.
- Options constraint: SINGLE-leg only, side=SELL only, strategy_name must be "Short Put (naked)" or "Short Call (naked)".
- Obey allowed_option_rights (CALL|PUT|BOTH). If you cannot satisfy it using near_atm_contracts, output HOLD.
"""

SYSTEM_PROMPT_TAIL = """
Output STRICT JSON ONLY. Use exactly ONE of:

HOLD:
{"decision":"HOLD","reason":"...","insufficient_data":false,"bias":"neutral","setup_type":"range_fade","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[]}

PROCEED (OPTION):
{"decision":"PROCEED","insufficient_data":false,"bias":"neutral","setup_type":"range_fade","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[],"proposal":{"strategy_name":"Short Put (naked)","legs":[{"symbol":"<OCC>","right":"PUT","strike":0.0,"expiry":"YYMMDD","side":"SELL","qty":1}],"max_risk":0.0,"target_return":0.0,"stop_loss_pct":0.5,"take_profit_pct":0.75,"rationale":"(2-3 sentences)","confidence":0.0}}

PROCEED (STOCK):
{"decision":"PROCEED","insufficient_data":false,"bias":"neutral","setup_type":"range_fade","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[],"stock_proposal":{"side":"BUY","qty":1.0,"order_type":"market","limit_price":null,"stop_loss_pct":null,"take_profit_pct":null,"rationale":"(2-3 sentences)","confidence":0.0}}
"""


def _build_strategist_system_prompt(state: FirmState) -> str:
    # Desk policy (2026-04): only allow SINGLE-leg option strategies for now.   
    guide = _build_regime_strategy_guide(
        state.allowed_option_rights,
        ["SINGLE"],
    )
    return SYSTEM_PROMPT_HEAD + guide + SYSTEM_PROMPT_TAIL


def strategist_node(state: FirmState) -> FirmState:
    # Gate: "in play" policy.
    # News/sentiment/movement decide whether we should even attempt a trade thesis.
    # Technical context then decides invalidation/fragility; options chain is last (expression).
    if (
        state.analyst_confidence < 0.3
        and state.sentiment_confidence < 0.3
        and abs(state.market_bias_score) < 0.35
        and not state.movement_anomaly
        and str(getattr(state, "news_timing_regime", "none") or "none") in ("none", "stale")
    ):
        state.pending_proposal = None
        state.reasoning_log.append(ReasoningEntry(
            agent="Strategist", action="HOLD",
            reasoning=(
                "Not in play: analyst/sentiment confidence low (<0.3), no strong non-news signal "
                "(market_bias/anomaly), and no fresh/moderate news timing. Skipping proposal generation."
            ),
            inputs={
                "analyst_confidence": state.analyst_confidence,
                "sentiment_confidence": state.sentiment_confidence,
                "market_bias_score": state.market_bias_score,
                "movement_anomaly": state.movement_anomaly,
                "news_timing_regime": state.news_timing_regime,
            },
            outputs={"skipped": True},
        ))
        return state

    # Naked-only safety gate: when technical context implies runaway move risk,
    # avoid forcing naked short options. Allow the system to "wait for confirmation".
    try:
        tc = state.technical_context
        if tc is not None:
            risk_flags: list[str] = []
            rl = str(getattr(tc, "regime_label", "") or "")
            vs = str(getattr(tc, "volume_state", "") or "")
            if rl in ("trend_up", "trend_down") and vs == "confirming":
                risk_flags.append("trend_with_confirming_volume")
            # Outside-week confirmations can happen frequently in strong trends.
            # Only treat it as runaway-move risk when participation is confirming.
            if (
                bool(getattr(tc, "outside_prev_week", False))
                and str(getattr(tc, "outside_week_state", "")).upper() == "CONFIRMED"
                and vs == "confirming"
            ):
                risk_flags.append("outside_week_confirmed")
            tri = getattr(tc, "triangle", None)
            tri_type = str(getattr(tri, "type", "") or "")
            if tri_type and tri_type != "NONE" and vs == "confirming":
                risk_flags.append("triangle_with_confirming_volume")

            # Guardrail: if price is very near a key level and volume is rising, do not assume range/fade.
            # This is a common "steamroll" setup for naked short options.
            try:
                prox_pct = float(__import__("os").getenv("NAKED_LEVEL_PROXIMITY_PCT", "0.8"))
            except Exception:
                prox_pct = 0.8
            prox_pct = max(0.2, min(3.0, prox_pct))
            try:
                if float(getattr(tc, "vol_ratio20", 0.0) or 0.0) >= 1.25:
                    # nearest absolute distance among supports/resistances
                    dists = []
                    for lv in list(getattr(tc, "supports", []) or []) + list(getattr(tc, "resistances", []) or []):
                        try:
                            dists.append(abs(float(getattr(lv, "distance_pct", 999.0) or 999.0)))
                        except Exception:
                            continue
                    if dists and min(dists) <= prox_pct:
                        risk_flags.append(f"near_level_with_rising_volume(<={prox_pct:.1f}%)")
            except Exception:
                pass

            if risk_flags:
                state.pending_proposal = None
                state.strategy_confidence = 0.0
                state.reasoning_log.append(ReasoningEntry(
                    agent="Strategist",
                    action="HOLD",
                    reasoning=(
                        "Naked-only safety: waiting for confirmation due to runaway-move risk flags: "
                        + ", ".join(risk_flags)
                    ),
                    inputs={"risk_flags": risk_flags, "technical_context": tc.model_dump()},
                    outputs={"skipped": True},
                ))
                return state
    except Exception:
        pass

    # A+ deterministic gate (long naked calls/puts).
    #
    # This is intentionally *pre-LLM* and deterministic.
    # When the A+ scorecard passes, we prefer a mechanical, reproducible selection:
    # - pick a liquid contract in a specific DTE window
    # - target a delta bucket (|Δ|≈0.60 default, or weekly ATH mode)
    # - attach explicit stop/take-profit/time-stop so exits do not depend on the LLM remembering
    # If the scorecard passes, propose a single-leg BUY option deterministically:
    # - DTE: 7–14 days
    # - Delta target: ~0.60 (calls) / ~-0.60 (puts)
    try:
        aplus = getattr(state, "aplus_setup", None)
        if aplus and getattr(aplus, "recommendation", "ABORT") == "PROCEED":
            direction = str(getattr(aplus, "direction", "none") or "none").lower()
            if direction in ("call", "put"):
                from datetime import date as _date
                from agents.options_math import (
                    breakeven_at_expiry,
                    expected_value_long_option,
                    pop_long_option,
                )
                from agents.state import TradeLeg, TradeProposal, OptionRight, OrderSide

                # Build candidates from live greeks snapshot (already OCC symbols).
                today = _date.today()
                # Default delta target: |Δ|≈0.60 (swing-ish). For ATH weekly calls, use 0.35–0.45 (gamma zone).
                try:
                    mode = str(getattr(aplus, "details", {}) or {}).lower()
                except Exception:
                    mode = ""
                is_ath_weekly_call = False
                try:
                    is_ath_weekly_call = (
                        str(getattr(aplus, "details", {}).get("aplus_mode", "")).lower() == "ath_weekly_call"
                        and direction == "call"
                    )
                except Exception:
                    is_ath_weekly_call = False

                tgt = 0.40 if is_ath_weekly_call else (0.60 if direction == "call" else -0.60)
                right = OptionRight.CALL if direction == "call" else OptionRight.PUT
                candidates = []
                for g in list(state.latest_greeks or []):
                    try:
                        if g.right != right:
                            continue
                        if g.delta is None:
                            continue
                        # ATH weekly calls: enforce the gamma "weapon" constraint (explosive acceleration).
                        if is_ath_weekly_call:
                            try:
                                if getattr(g, "gamma", None) is None or float(getattr(g, "gamma", 0.0) or 0.0) < 0.05:
                                    continue
                            except Exception:
                                continue
                        exp_d = occ_expiry_as_date(str(g.symbol or ""))
                        if exp_d is None:
                            exp_d = parse_greeks_expiry_str(str(getattr(g, "expiry", "") or ""))
                        if exp_d is None:
                            continue
                        dte = (exp_d - today).days
                        if is_ath_weekly_call:
                            # Weeklies: tighter window. Mon–Wed -> allow 4–9 DTE. Thu/Fri -> allow 7–14 DTE (next week).
                            wd = today.weekday()  # Mon=0 ... Sun=6
                            if wd >= 3:  # Thu/Fri
                                if dte < 7 or dte > 14:
                                    continue
                            else:
                                if dte < 4 or dte > 9:
                                    continue
                        else:
                            if dte < 7 or dte > 14:
                                continue
                        if not (g.bid > 0 and g.ask > 0 and g.ask >= g.bid):
                            continue
                        mid = (float(g.bid) + float(g.ask)) / 2.0
                        spread = float(g.ask) - float(g.bid)
                        if mid <= 0:
                            continue
                        # Liquidity guard: avoid insane spreads.
                        if spread > max(0.25, mid * 0.35):
                            continue
                        # ATH weekly calls: prefer slightly OTM strikes.
                        if is_ath_weekly_call:
                            try:
                                u_px = float(getattr(state, "underlying_price", 0.0) or 0.0)
                            except Exception:
                                u_px = 0.0
                            if u_px > 0:
                                try:
                                    strike = float(getattr(g, "strike", 0.0) or 0.0)
                                except Exception:
                                    strike = 0.0
                                # Slightly OTM: strike >= spot and not too far (<= ~3%)
                                if not (strike >= u_px and strike <= (u_px * 1.03)):
                                    continue
                        candidates.append((abs(float(g.delta) - tgt), dte, spread, mid, g))
                    except Exception:
                        continue
                candidates.sort(key=lambda x: (x[0], x[1], x[2]))
                if candidates:
                    _, dte, spread, mid, g = candidates[0]
                    max_risk = round(float(mid) * 100.0, 2)
                    # Weeklies at ATH: faster profit-taking (30%); otherwise +50%.
                    tp_pct = 0.30 if is_ath_weekly_call else 0.50
                    target_return = round(max_risk * tp_pct, 2)
                    try:
                        u_px = float(getattr(state, "underlying_price", 0.0) or 0.0)
                    except Exception:
                        u_px = 0.0
                    try:
                        iv = float(getattr(g, "iv", None) or 0.0)
                    except Exception:
                        iv = 0.0

                    be = breakeven_at_expiry(right=right.value, strike=float(g.strike), premium=float(mid))
                    pop = pop_long_option(
                        right=right.value,
                        s0=u_px,
                        strike=float(g.strike),
                        premium=float(mid),
                        iv=iv,
                        dte=int(dte),
                        mu=0.0,
                    )
                    ev = expected_value_long_option(
                        right=right.value,
                        s0=u_px,
                        strike=float(g.strike),
                        premium=float(mid),
                        iv=iv,
                        dte=int(dte),
                        mu=0.0,
                    )

                    proposal = TradeProposal(
                        strategy_name=f"Long {direction.upper()} (A+)",
                        legs=[
                            TradeLeg(
                                symbol=str(g.symbol),
                                right=right,
                                strike=float(g.strike),
                                expiry=str(g.expiry),
                                side=OrderSide.BUY,
                                qty=1,
                            )
                        ],
                        max_risk=max_risk,
                        target_return=target_return,
                        stop_loss_pct=0.20,
                        take_profit_pct=tp_pct,
                        dte=int(dte),
                        delta=float(g.delta) if getattr(g, "delta", None) is not None else None,
                        breakeven=float(be) if be is not None else None,
                        pop=float(pop) if pop is not None else None,
                        ev=float(ev) if ev is not None else None,
                        rationale=(
                            f"A+ setup PROCEED ({getattr(aplus,'score',0)}/{getattr(aplus,'required',5)}). "
                            f"Picked {g.symbol} DTE={dte} |Δ|≈{abs(tgt):.2f} (Δ={float(g.delta):+.2f}) "
                            f"mid≈${mid:.2f} spread=${spread:.2f}. "
                            f"{(f'PoP≈{(pop * 100.0):.0f}% ' if isinstance(pop, (float, int)) else '')}"
                            f"{(f'EV≈${float(ev):.0f} ' if isinstance(ev, (float, int)) else '')}"
                            f"{(f'BE≈${float(be):.2f}. ' if isinstance(be, (float, int)) else '')}"
                            f"Exit policy: -20% stop, +{int(tp_pct*100)}% take profit, 48h time stop."
                        ),
                        confidence=max(0.75, float(getattr(state, "analyst_confidence", 0.0) or 0.0)),
                    )
                    state.pending_proposal = proposal
                    state.strategy_confidence = float(proposal.confidence or 0.0)
                    state.reasoning_log.append(ReasoningEntry(
                        agent="Strategist",
                        action="PROCEED",
                        reasoning="A+ deterministic gate: built long naked option proposal (delta/DTE targeted).",
                        inputs={"aplus_setup": aplus.model_dump() if hasattr(aplus, "model_dump") else {}},
                        outputs={"symbol": str(g.symbol), "dte": dte, "mid": round(mid, 2), "delta": float(g.delta)},
                    ))
                    return state
                else:
                    state.reasoning_log.append(ReasoningEntry(
                        agent="Strategist",
                        action="HOLD",
                        reasoning="A+ passed but no liquid 7–14 DTE contract found near |Δ|≈0.60 in latest_greeks.",
                        inputs={"direction": direction},
                        outputs={"candidates": 0},
                    ))
                    return state
    except Exception:
        pass

    llm = chat_llm(
        MODELS.strategist.active,
        agent_role="strategist",
        temperature=0.1,
    )

    # Use pre-computed chain analytics
    analytics = build_chain_analytics(state.latest_greeks, state.underlying_price)

    # Existing position summary (avoid doubling up on same underlying)
    existing_summary = [
        {"symbol": p.symbol, "right": p.right.value, "strike": p.strike,
         "expiry": p.expiry, "qty": p.quantity}
        for p in state.open_positions[:10]
    ]

    # NAV-based sizing guidance
    nav = max(state.risk.current_nav, state.account_equity, 10_000.0)
    position_cap = nav * state.risk.position_cap_pct

    # Desk policy (2026-04): only allow SINGLE-leg option strategies for now.
    # We keep the broader strategy catalog for future expansion, but enforce
    # one-leg proposals deterministically (prompt + post-parse validation).
    effective_allowed_structures: list[str] = ["SINGLE"]

    def _compute_mid(bid: float, ask: float) -> float | None:
        try:
            bid = float(bid)
            ask = float(ask)
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        return round((bid + ask) / 2.0, 2)

    def _estimate_max_loss_usd_from_quotes(proposal: TradeProposal) -> float | None:
        """
        Best-effort max loss estimate in USD using mids from latest_greeks.
        We only use this to detect the common "forgot ×100" unit mistake.
        """
        try:
            gmap = {g.symbol: g for g in (state.latest_greeks or [])}
            legs = proposal.legs or []
            if not legs:
                return None

            # Net premium in USD (+credit, -debit)
            missing = 0
            net_usd = 0.0
            for leg in legs:
                g = gmap.get(leg.symbol)
                if not g:
                    missing += 1
                    continue
                m = _compute_mid(getattr(g, "bid", 0.0), getattr(g, "ask", 0.0))
                if m is None:
                    missing += 1
                    continue
                sign = 1.0 if leg.side == OrderSide.SELL else -1.0
                net_usd += sign * float(m) * float(leg.qty) * 100.0

            if missing:
                return None

            # Debit-style: worst case loss ≈ debit paid.
            if net_usd < 0:
                return round(abs(net_usd), 2)

            # Credit-style heuristics.
            credit_usd = max(0.0, net_usd)
            rights = {l.right for l in legs}
            expiries = {str(l.expiry or "") for l in legs}

            # Vertical spread: 2 legs, same right+expiry, qty 1 (or same qty).
            if len(legs) == 2 and len(rights) == 1 and len(expiries) == 1:
                strikes = sorted([float(l.strike) for l in legs])
                width = abs(strikes[1] - strikes[0])
                return round(max(0.0, width * 100.0 - credit_usd), 2)

            # Iron condor: 2 puts + 2 calls, same expiry.
            puts = [l for l in legs if l.right == OptionRight.PUT]
            calls = [l for l in legs if l.right == OptionRight.CALL]
            if len(puts) == 2 and len(calls) == 2 and len(expiries) == 1:
                put_strikes = sorted([float(l.strike) for l in puts])
                call_strikes = sorted([float(l.strike) for l in calls])
                width_put = abs(put_strikes[1] - put_strikes[0])
                width_call = abs(call_strikes[1] - call_strikes[0])
                width = max(width_put, width_call)
                return round(max(0.0, width * 100.0 - credit_usd), 2)

            # Fallback: for unknown credit structures, use a conservative proxy.
            # This isn't used for trading decisions, only unit-normalization.
            return round(max(0.0, credit_usd), 2)
        except Exception:
            return None

    def _normalize_single_leg_naked_short_risk_targets_from_quotes(proposal: TradeProposal) -> dict | None:
        """
        For SINGLE-leg naked short options, compute trade-level max_risk/target_return
        from live mid quotes + configured stop/take percentages.

        Rationale:
        - LLMs frequently output inconsistent max_risk/target_return (e.g. using NAV cap or forgetting ×100).
        - For naked short calls, expiry payoff max loss is unbounded, so we must NOT pretend it's small.
          Instead we interpret stop-loss as a trade-level "planned max loss" (credit × stop_loss_pct).
        - For naked short puts, expiry payoff max loss (to zero) can be computed, but the desk's
          intended risk is still managed via stop-loss; we keep both: proposal max_risk (stop-based),
          and API pricing_summary shows expiry worst-case separately.
        """
        try:
            legs = proposal.legs or []
            if len(legs) != 1:
                return None
            leg = legs[0]
            if leg.side != OrderSide.SELL:
                return None

            gmap = {g.symbol: g for g in (state.latest_greeks or [])}
            g = gmap.get(leg.symbol)
            if not g:
                return None
            mid = _compute_mid(getattr(g, "bid", 0.0), getattr(g, "ask", 0.0))
            if mid is None or mid <= 0:
                return None

            credit_usd = float(mid) * float(leg.qty or 1) * 100.0
            sl = float(proposal.stop_loss_pct) if proposal.stop_loss_pct is not None else None
            tp = float(proposal.take_profit_pct) if proposal.take_profit_pct is not None else None
            if sl is None or tp is None:
                return None
            # Clamp to sane bounds (prompt contract is 0..1 but LLMs can drift).
            sl = max(0.0, min(1.0, sl))
            tp = max(0.0, min(1.0, tp))

            planned_max_loss = round(max(0.0, credit_usd * sl), 2)
            planned_target = round(max(0.0, credit_usd * tp), 2)
            return {
                "credit_usd": round(credit_usd, 2),
                "planned_max_loss_usd": planned_max_loss,
                "planned_target_usd": planned_target,
                "mid": round(float(mid), 2),
            }
        except Exception:
            return None

    context = {
        "ticker":              state.ticker,
        "underlying_price":    state.underlying_price,
        "market_regime":       state.market_regime.value,
        "technical_context":   (state.technical_context.model_dump() if state.technical_context else None),
        "aplus_setup":         (state.aplus_setup.model_dump() if getattr(state, "aplus_setup", None) else None),
        "allowed_option_rights": (state.allowed_option_rights or "BOTH"),
        "allowed_option_structures": effective_allowed_structures,
        "iv_regime":           state.iv_regime or analytics["iv_metrics"]["iv_regime"],
        "iv_atm":              analytics["iv_metrics"]["atm_iv"],
        "skew_ratio":          analytics["iv_metrics"]["skew_ratio"],
        "term_structure":      analytics["term_structure"],
        "aggregate_sentiment": state.aggregate_sentiment,
        "sentiment_themes":    state.sentiment_themes,
        "analyst_confidence":  round(state.analyst_confidence, 2),
        "risk_metrics": {
            "portfolio_delta":   state.risk.portfolio_delta,
            "portfolio_vega":    state.risk.portfolio_vega,
            "daily_pnl":         state.risk.daily_pnl,
            "drawdown_pct":      f"{state.risk.drawdown_pct:.2%}",
        },
        "nav":                  round(nav, 2),
        "position_cap_dollars": round(position_cap, 2),
        "existing_positions":   existing_summary,
        "near_atm_contracts":   analytics["near_atm_contracts"],
        "highest_iv_contracts": analytics["highest_iv_contracts"],
        "existing_proposal":    state.pending_proposal.model_dump() if state.pending_proposal else None,
        # T3 researcher debate outputs (may be empty on first cycle)
        "movement_signal":      round(state.movement_signal, 4),
        "price_change_pct":     round(state.price_change_pct * 100, 3),
        "bull_researcher": {
            "conviction":  state.bull_conviction,
            "argument": (
                (state.bull_argument or "")[:MAX_RESEARCHER_ARGUMENT_CHARS]
                if INCLUDE_RESEARCHER_ARGUMENTS
                else ""
            ),
        },
        "bear_researcher": {
            "conviction":  state.bear_conviction,
            "argument": (
                (state.bear_argument or "")[:MAX_RESEARCHER_ARGUMENT_CHARS]
                if INCLUDE_RESEARCHER_ARGUMENTS
                else ""
            ),
        },
        "news_timing_regime":    state.news_timing_regime,
        "news_newest_age_minutes": state.news_newest_age_minutes,
        "market_bias_score":     round(state.market_bias_score, 4),
        "tier3_structured_digests": state.tier3_structured_digests[:8],
        "fundamentals_snapshot": {
            "pe_ratio": state.fundamentals.get("pe_ratio"),
            "fwd_pe":   state.fundamentals.get("fwd_pe"),
            "beta":     state.fundamentals.get("beta"),
            "sector":   state.fundamentals.get("sector"),
        },
    }

    messages = [
        SystemMessage(content=_build_strategist_system_prompt(state)),
        # Compact JSON saves tokens materially vs pretty indent.
        HumanMessage(content=json.dumps(context, separators=(",", ":"), ensure_ascii=False)),
    ]

    response, llm_meta = invoke_llm_with_metrics(llm, messages)

    def _has_text(r) -> bool:
        return bool(r and str(getattr(r, "content", "") or "").strip())

    out = parse_and_validate(getattr(response, "content", "") or "", StrategistOutput, "Strategist")

    # Bounded retry: handle empty content and/or parse failures once.
    if not out:
        try:
            response2, llm_meta_retry = invoke_llm_with_metrics(llm, messages)
            if _has_text(response2):
                llm_meta = {"first": llm_meta, "retry": llm_meta_retry}
                response = response2
                out = parse_and_validate(getattr(response, "content", "") or "", StrategistOutput, "Strategist")
        except Exception:
            pass
    if not out:
        # One-shot repair pass: coerce the model into STRICT JSON only.
        repair_sys = (
            "You are a strict JSON repair tool.\n"
            "Return ONLY valid JSON in ONE of these two forms (no markdown, no prose):\n\n"
            'HOLD: {"decision":"HOLD","reason":"...","insufficient_data":false,"bias":"neutral","setup_type":"unknown","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[]}\n\n'
            'PROCEED_OPTION: {"decision":"PROCEED","insufficient_data":false,"bias":"neutral","setup_type":"unknown","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[],"proposal":{"strategy_name":"...","legs":[{"symbol":"<OCC>","right":"CALL|PUT","strike":0.0,"expiry":"YYMMDD","side":"SELL","qty":1}],"max_risk":0.0,"target_return":0.0,"stop_loss_pct":0.5,"take_profit_pct":0.75,"rationale":"...","confidence":0.0}}\n\n'
            'PROCEED_STOCK: {"decision":"PROCEED","insufficient_data":false,"bias":"neutral","setup_type":"unknown","key_levels":[],"confirmation":"","invalidation":"","risk_notes":[],"stock_proposal":{"side":"BUY","qty":1.0,"order_type":"market","limit_price":null,"stop_loss_pct":null,"take_profit_pct":null,"rationale":"...","confidence":0.0}}\n\n'
            "If you are unsure, choose HOLD."
        )
        repair_msgs = [
            SystemMessage(content=repair_sys),
            HumanMessage(content=(response.content or "")[:2600]),
        ]
        llm_repair = chat_llm(
            MODELS.strategist.active,
            agent_role="strategist",
            temperature=0.0,
        )
        resp2, llm_meta_repair = invoke_llm_with_metrics(llm_repair, repair_msgs)
        out = parse_and_validate(resp2.content, StrategistOutput, "Strategist")

    proposal: TradeProposal | None = None
    stock_proposal: StockTradeProposal | None = None
    decision   = AgentDecision.HOLD
    reasoning  = ""
    confidence = 0.0

    if out:
        decision = AgentDecision(out.decision)
        if decision == AgentDecision.PROCEED and getattr(out, "stock_proposal", None):
            sp = out.stock_proposal
            # UX policy: use whole shares for stock proposals.
            try:
                qty_i = int(round(float(sp.qty)))
            except Exception:
                qty_i = 0
            qty_f = float(max(1, qty_i)) if qty_i > 0 else 0.0
            stock_proposal = StockTradeProposal(
                side=OrderSide(sp.side),
                qty=qty_f,
                order_type=str(sp.order_type or "market"),
                limit_price=sp.limit_price,
                rationale=str(sp.rationale or ""),
                confidence=float(sp.confidence or 0.0),
                stop_loss_pct=sp.stop_loss_pct,
                take_profit_pct=sp.take_profit_pct,
            )
            proposal = None
            reasoning = stock_proposal.rationale
            confidence = stock_proposal.confidence
        elif decision == AgentDecision.PROCEED and out.proposal:
            p = out.proposal
            legs = []
            for leg in p.legs:
                try:
                    legs.append(TradeLeg(
                        symbol=leg.symbol,
                        right=OptionRight(leg.right),
                        strike=leg.strike,
                        expiry=leg.expiry,
                        side=OrderSide(leg.side),
                        qty=leg.qty,
                    ))
                except Exception:
                    continue

            if not legs:
                decision  = AgentDecision.HOLD
                reasoning = "Strategist PROCEED but no valid legs parsed."
            else:
                proposal = TradeProposal(
                    strategy_name   = p.strategy_name,
                    legs            = legs,
                    max_risk        = p.max_risk,
                    target_return   = p.target_return,
                    rationale       = p.rationale,
                    confidence      = p.confidence,
                    stop_loss_pct   = p.stop_loss_pct,
                    take_profit_pct = p.take_profit_pct,
                )

                # Enforce allowed rights deterministically (user preference).
                allowed = (state.allowed_option_rights or "BOTH").strip().upper()
                if allowed in ("CALL", "PUT"):
                    bad = [l.symbol for l in (proposal.legs or []) if l.right.value != allowed]
                    if bad:
                        decision = AgentDecision.HOLD
                        reasoning = (
                            f"Rejected proposal: allowed_option_rights={allowed} "
                            f"but proposal contains other rights: {bad[:4]}"
                        )
                        confidence = 0.0
                        proposal = None
                        # Skip multiplier normalization / cap validation.
                        # Continue to final section which clears pending_proposal.
                # Enforce allowed structures deterministically (user preference).
                if proposal is not None:
                    allowed_structs = [s.upper() for s in (effective_allowed_structures or ["SINGLE"])]
                    if "ALL" not in allowed_structs:
                        kind = _classify_option_structure(proposal)
                        if kind not in allowed_structs:
                            decision = AgentDecision.HOLD
                            reasoning = (
                                f"Rejected proposal: structure={kind} not in allowed_option_structures={allowed_structs}."
                            )
                            confidence = 0.0
                            proposal = None

                # Hard gate: only allow single-leg strategies for now (even if the model tries a spread).
                if proposal is not None:
                    if len(proposal.legs or []) != 1:
                        decision = AgentDecision.HOLD
                        reasoning = (
                            f"Rejected proposal: only SINGLE-leg strategies are enabled right now "
                            f"(got {len(proposal.legs or [])} legs)."
                        )
                        confidence = 0.0
                        proposal = None

                # Hard gate: naked short options only (no long calls/puts).
                if proposal is not None:
                    bad = [l.symbol for l in (proposal.legs or []) if l.side != OrderSide.SELL]
                    if bad:
                        decision = AgentDecision.HOLD
                        reasoning = (
                            "Rejected proposal: only naked SELL legs are allowed right now "
                            f"(found non-SELL legs: {bad[:3]})."
                        )
                        confidence = 0.0
                        proposal = None

                # Normalize common unit mistake: model forgets options are quoted per-share (×100 per contract).
                # If the proposal looks off by ~100x relative to live mids, correct it deterministically.
                est_loss = _estimate_max_loss_usd_from_quotes(proposal)
                try:
                    mr = float(proposal.max_risk)
                    tr = float(proposal.target_return)
                except Exception:
                    mr = tr = 0.0
                if est_loss is not None and mr > 0:
                    # Detect if max_risk likely provided in "option price units" instead of dollars.
                    if mr < 25 and est_loss >= 100:
                        scaled = mr * 100.0
                        rel_err = abs(est_loss - scaled) / max(est_loss, 1.0)
                        if rel_err <= 0.25:
                            proposal.max_risk = round(scaled, 2)
                            # target_return should be in dollars too; scale if it appears similarly small.
                            if tr > 0 and tr < 250:
                                proposal.target_return = round(tr * 100.0, 2)
                            state.reasoning_log.append(ReasoningEntry(
                                agent="Strategist",
                                action="NORMALIZED_MULTIPLIER",
                                reasoning=(
                                    "Normalized proposal dollars by ×100 contract multiplier "
                                    f"(estimated_max_loss≈${est_loss:.2f}, reported_max_risk={mr:.2f})."
                                ),
                                inputs={"estimated_max_loss_usd": est_loss, "reported_max_risk": mr},
                                outputs={"normalized_max_risk": proposal.max_risk, "normalized_target_return": proposal.target_return},
                            ))

                # Deterministic override for SINGLE-leg naked short: compute max_risk/target from quotes + stop/take.
                # This prevents impossible outputs like "max_risk $1959" while net credit is $7205.
                try:
                    rr = _normalize_single_leg_naked_short_risk_targets_from_quotes(proposal)
                    if rr is not None:
                        old_mr = float(getattr(proposal, "max_risk", 0.0) or 0.0)
                        old_tr = float(getattr(proposal, "target_return", 0.0) or 0.0)
                        proposal.max_risk = float(rr["planned_max_loss_usd"])
                        proposal.target_return = float(rr["planned_target_usd"])
                        state.reasoning_log.append(ReasoningEntry(
                            agent="Strategist",
                            action="RISK_TARGET_RECOMPUTED_FROM_QUOTES",
                            reasoning=(
                                "Recomputed SINGLE-leg naked short max_risk/target_return from live mid + stop/take. "
                                f"credit≈${rr['credit_usd']:.2f} (mid≈${rr['mid']:.2f}) → "
                                f"max_risk≈${proposal.max_risk:.2f} (SL {proposal.stop_loss_pct:.0%}) / "
                                f"target≈${proposal.target_return:.2f} (TP {proposal.take_profit_pct:.0%})."
                            ),
                            inputs={"old_max_risk": old_mr, "old_target_return": old_tr},
                            outputs={
                                "credit_usd": rr["credit_usd"],
                                "new_max_risk": proposal.max_risk,
                                "new_target_return": proposal.target_return,
                            },
                        ))
                except Exception:
                    pass
                reasoning  = proposal.rationale
                confidence = proposal.confidence
                # Validate max_risk against position cap
                if proposal.max_risk > position_cap * 1.1:
                    decision  = AgentDecision.HOLD
                    reasoning = (f"max_risk ${proposal.max_risk:.0f} exceeds cap "
                                 f"${position_cap:.0f}. Downgrading to HOLD.")
                    proposal  = None
        else:
            reasoning = out.reason or ""
    else:
        decision  = AgentDecision.HOLD
        reasoning = f"Schema validation failed: {response.content[:250]}"
        proposal  = None
        stock_proposal = None

    # Final gate: never allow expired contracts into pending_proposal
    if decision == AgentDecision.PROCEED and proposal is not None:
        today = __import__("datetime").date.today()
        expired: list[dict] = []
        for leg in proposal.legs:
            sym = str(getattr(leg, "symbol", "") or "").strip()
            exp_d = occ_expiry_as_date(sym)
            if exp_d is None:
                exp_d = parse_greeks_expiry_str(str(getattr(leg, "expiry", "") or ""))
            if exp_d is not None and exp_d < today:
                expired.append({"symbol": sym, "expired_on": exp_d.isoformat()})
        if expired:
            decision = AgentDecision.HOLD
            proposal = None
            confidence = 0.0
            reasoning = (
                "Rejected: proposal contains expired option legs. "
                "Regenerate using only future-dated OCC symbols from near_atm_contracts."
            )
            state.reasoning_log.append(ReasoningEntry(
                agent="Strategist",
                action="HOLD",
                reasoning=reasoning,
                inputs={"expired_legs": expired},
                outputs={"rejected": True},
            ))

    # Strategist is the final trade chooser; clear the other instrument so downstream
    # recommend_node does not park the "wrong" one from earlier agents.
    if decision == AgentDecision.PROCEED and stock_proposal is not None:
        state.pending_stock_proposal = stock_proposal
        state.stock_decision = AgentDecision.PROCEED
        state.stock_confidence = confidence
        state.pending_proposal = None
        state.strategy_confidence = 0.0
    else:
        state.pending_proposal   = proposal if decision == AgentDecision.PROCEED else None
        state.strategy_confidence = confidence
        if proposal is not None and decision == AgentDecision.PROCEED:
            state.pending_stock_proposal = None
            state.stock_decision = AgentDecision.HOLD
            state.stock_confidence = 0.0

    state.reasoning_log.append(ReasoningEntry(
        agent="Strategist",
        action=decision.value,
        reasoning=reasoning,
        inputs={
            "ticker":          state.ticker,
            "regime":          state.market_regime.value,
            "iv_regime":       state.iv_regime,
            "skew_ratio":      analytics["iv_metrics"]["skew_ratio"],
            "sentiment":       state.aggregate_sentiment,
            "position_cap":    round(position_cap, 2),
            "technical_context": (state.technical_context.model_dump() if state.technical_context else None),
        },
        outputs={
            "has_proposal":    proposal is not None,
            "has_stock_proposal": stock_proposal is not None,
            "confidence":      confidence,
            "strategy_name":   proposal.strategy_name if proposal else None,
            "legs_count":      len(proposal.legs) if proposal else 0,
            "max_risk":        proposal.max_risk if proposal else None,
            "target_return":   proposal.target_return if proposal else None,
            "llm_call": (llm_meta if "llm_meta" in locals() else None),
            "llm_repair_call": (llm_meta_repair if "llm_meta_repair" in locals() else None),
        },
    ))
    return state
