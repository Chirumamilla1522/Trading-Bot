#!/usr/bin/env python3
"""
Sample prompt lab for the trading agents.

This is a small, safe harness for:
  - building representative prompts for each LLM agent role
  - estimating prompt/completion tokens
  - estimating cost when you pass per-1M token rates
  - optionally calling the configured LLM backend and validating strict JSON

Examples:
  python3 agents/sample_agent_prompts.py
  python3 agents/sample_agent_prompts.py --agent strategist --json
  python3 agents/sample_agent_prompts.py --live --agent sentiment_analyst
  python3 agents/sample_agent_prompts.py --input-cost-per-1m 0.07 --output-cost-per-1m 0.28
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import agents.config  # noqa: F401 - loads .env


@dataclass(frozen=True)
class PromptCase:
    role: str
    system: str
    user: str
    schema_name: str


def _compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _sample_chain() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "AAPL260515P00195000",
            "right": "PUT",
            "strike": 195.0,
            "expiry": "260515",
            "dte": 17,
            "bid": 2.35,
            "ask": 2.48,
            "delta": -0.31,
            "iv": 0.34,
            "open_interest": 8310,
        },
        {
            "symbol": "AAPL260515C00215000",
            "right": "CALL",
            "strike": 215.0,
            "expiry": "260515",
            "dte": 17,
            "bid": 1.82,
            "ask": 1.94,
            "delta": 0.28,
            "iv": 0.31,
            "open_interest": 6460,
        },
        {
            "symbol": "AAPL260619P00190000",
            "right": "PUT",
            "strike": 190.0,
            "expiry": "260619",
            "dte": 52,
            "bid": 3.9,
            "ask": 4.1,
            "delta": -0.26,
            "iv": 0.36,
            "open_interest": 11920,
        },
    ]


def _base_context() -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "underlying_price": 205.32,
        "market_regime": "MEAN_REVERTING",
        "technical_context": {
            "regime_label": "range",
            "volume_state": "normal",
            "support": 198.4,
            "resistance": 213.7,
            "outside_prev_week": False,
            "triangle_state": "none",
        },
        "chain_analytics": {
            "iv_metrics": {
                "atm_iv": 0.33,
                "skew_ratio": 1.18,
                "iv_regime": "ELEVATED",
                "term_structure": "CONTANGO",
            },
            "term_structure": "CONTANGO",
            "near_atm_contracts": _sample_chain(),
        },
        "risk_metrics": {
            "drawdown_pct": 0.006,
            "max_drawdown_pct": 0.05,
            "portfolio_delta": -18.0,
            "portfolio_gamma": 34.0,
            "portfolio_vega": 210.0,
            "current_nav": 100000.0,
            "position_cap_pct": 0.02,
        },
        "open_positions_count": 2,
        "aggregate_sentiment": 0.22,
        "news_timing_regime": "moderate",
        "market_bias_score": 0.18,
        "movement_signal": "flat",
        "price_change_pct": -0.35,
        "position_cap_dollars": 2000.0,
        "current_nav": 100000.0,
        "allowed_option_rights": "BOTH",
    }


def _prompt_cases() -> dict[str, PromptCase]:
    from agents.agents.desk_head import SYSTEM_PROMPT as DESK_HEAD_PROMPT
    from agents.agents.options_specialist import SYSTEM_PROMPT as OPTIONS_PROMPT
    from agents.agents.risk_manager import (
        _DELTA_LIMIT_PCT_NAV,
        _GAMMA_LIMIT,
        _VEGA_LIMIT,
        SYSTEM_PROMPT as RISK_PROMPT,
    )
    from agents.agents.sentiment_analyst import SYSTEM_PROMPT as SENTIMENT_PROMPT
    from agents.agents.strategist import (
        SYSTEM_PROMPT_HEAD,
        SYSTEM_PROMPT_TAIL,
        _build_regime_strategy_guide,
    )
    from agents.config import MAX_DAILY_DRAWDOWN, MAX_POSITION_PCT

    ctx = _base_context()
    proposal = {
        "strategy_name": "Short Put (naked)",
        "legs": [
            {
                "symbol": "AAPL260515P00195000",
                "right": "PUT",
                "strike": 195.0,
                "expiry": "260515",
                "side": "SELL",
                "qty": 1,
            }
        ],
        "max_risk": 19500.0,
        "target_return": 241.5,
        "stop_loss_pct": 0.5,
        "take_profit_pct": 0.75,
        "rationale": "Elevated IV and range support favor selling a lower-delta put.",
        "confidence": 0.62,
    }

    sentiment_context = {
        "ticker": "AAPL",
        "price": 205.32,
        "desk_sentiment_monitor": {"score": 0.18, "confidence": 0.54, "source": "tier2_structured_news"},
        "headlines": [
            {
                "title": "Apple services revenue growth offsets softer hardware demand",
                "published_at": "2026-04-28T13:40:00Z",
                "recency_weight": 0.92,
                "source": "sample",
            },
            {
                "title": "Analysts flag China smartphone competition as a margin risk",
                "published_at": "2026-04-28T12:55:00Z",
                "recency_weight": 0.72,
                "source": "sample",
            },
            {
                "title": "Broad market volatility eases after yields stabilize",
                "published_at": "2026-04-28T11:30:00Z",
                "recency_weight": 0.44,
                "source": "sample",
            },
        ],
    }

    risk_context = dict(ctx)
    risk_context["pending_proposal"] = proposal
    risk_context["iv_regime"] = "ELEVATED"
    risk_context["iv_skew_ratio"] = 1.18

    desk_context = {
        "ticker": "AAPL",
        "underlying_price": 205.32,
        "market_regime": "MEAN_REVERTING",
        "iv_regime": "ELEVATED",
        "iv_atm": "33.0%",
        "skew_ratio": 1.18,
        "aggregate_sentiment": 0.22,
        "key_themes": ["services strength", "margin risk"],
        "tail_risks": ["China competition"],
        "sub_agent_verdicts": {
            "options_specialist": {"decision": "PROCEED", "confidence": 0.66},
            "sentiment_analyst": {"decision": "HOLD", "confidence": 0.58},
            "risk_manager": {"decision": "PROCEED", "confidence": 0.7},
        },
        "debate": {"verdict": "HOLD", "summary": "Bull case is IV premium; bear case is stale mixed news."},
        "pending_proposal": {
            "strategy_name": proposal["strategy_name"],
            "max_risk": proposal["max_risk"],
            "target_return": proposal["target_return"],
            "confidence": proposal["confidence"],
            "legs_count": 1,
        },
        "portfolio": {"current_nav": 100000.0, "drawdown_pct": 0.006, "position_cap_pct": 0.02},
    }

    strategist_prompt = (
        SYSTEM_PROMPT_HEAD
        + _build_regime_strategy_guide(ctx["allowed_option_rights"], ["SINGLE"])
        + SYSTEM_PROMPT_TAIL
    )
    risk_prompt = RISK_PROMPT.format(
        max_dd=f"{MAX_DAILY_DRAWDOWN * 100:.0f}",
        max_pos=f"{MAX_POSITION_PCT * 100:.0f}",
        delta_limit_pct=_DELTA_LIMIT_PCT_NAV,
        gamma_limit=_GAMMA_LIMIT,
        vega_limit=_VEGA_LIMIT,
    )

    return {
        "options_specialist": PromptCase(
            "options_specialist",
            OPTIONS_PROMPT,
            _compact_json(ctx),
            "OptionsSpecialistOutput",
        ),
        "sentiment_analyst": PromptCase(
            "sentiment_analyst",
            SENTIMENT_PROMPT.format(ticker="AAPL", price=205.32),
            _compact_json(sentiment_context),
            "SentimentAnalystOutput",
        ),
        "strategist": PromptCase(
            "strategist",
            strategist_prompt,
            _compact_json(ctx),
            "StrategistOutput",
        ),
        "risk_manager": PromptCase(
            "risk_manager",
            risk_prompt,
            _compact_json(risk_context),
            "RiskManagerOutput",
        ),
        "desk_head": PromptCase(
            "desk_head",
            DESK_HEAD_PROMPT,
            _compact_json(desk_context),
            "DeskHeadOutput",
        ),
    }


def _messages_text(case: PromptCase) -> str:
    return f"[system]\n{case.system}\n\n[user]\n{case.user}"


def _estimate(case: PromptCase, completion_tokens: int, input_rate: float, output_rate: float) -> dict[str, Any]:
    from agents.llm_retry import _rough_token_estimate

    prompt_tokens = _rough_token_estimate(_messages_text(case))
    estimated_cost = (prompt_tokens / 1_000_000 * input_rate) + (completion_tokens / 1_000_000 * output_rate)
    return {
        "agent": case.role,
        "prompt_chars": len(_messages_text(case)),
        "prompt_tokens_est": prompt_tokens,
        "completion_tokens_assumed": completion_tokens,
        "total_tokens_est": prompt_tokens + completion_tokens,
        "estimated_cost_usd": round(estimated_cost, 8) if input_rate or output_rate else None,
    }


def _resolve_model_slug(agent: str) -> str:
    from agents.config import MODELS

    attr = "desk_head" if agent == "desk_head" else agent
    if hasattr(MODELS, attr):
        return getattr(MODELS, attr).active
    return MODELS.strategist.active


def _schema_class(schema_name: str):
    import agents.schemas as schemas

    return getattr(schemas, schema_name)


def _validate(agent_name: str, schema_name: str, raw: str) -> dict[str, Any]:
    from agents.schemas import parse_and_validate

    parsed = parse_and_validate(raw, _schema_class(schema_name), agent_name)
    if parsed is None:
        return {"valid": False, "parsed": None}
    return {"valid": True, "parsed": parsed.model_dump()}


def _run_live(case: PromptCase) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.llm_providers import chat_llm
    from agents.llm_retry import _rough_token_estimate
    from agents.llm_retry import invoke_llm_with_metrics

    llm = chat_llm(_resolve_model_slug(case.role), agent_role=case.role, temperature=0.0)
    messages = [SystemMessage(content=case.system), HumanMessage(content=case.user)]
    t0 = time.perf_counter()
    response, meta = invoke_llm_with_metrics(llm, messages)
    duration_s = round(time.perf_counter() - t0, 4)
    text = str(getattr(response, "content", "") or "")
    validation = _validate(case.role, case.schema_name, text)
    response_tokens_est = _rough_token_estimate(text)
    return {
        "agent": case.role,
        "model_slug": _resolve_model_slug(case.role),
        "duration_s": duration_s,
        "usage": {
            "backend": meta.get("backend"),
            "model": meta.get("model"),
            "prompt_tokens": meta.get("prompt_tokens"),
            "completion_tokens": meta.get("completion_tokens"),
            "total_tokens": meta.get("total_tokens"),
        },
        "response_chars": len(text),
        "response_tokens_est": response_tokens_est,
        "response_preview": text.strip().replace("\n", " ")[:500],
        "schema_validation": validation,
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["agent", "prompt_tokens_est", "completion_tokens_assumed", "total_tokens_est", "estimated_cost_usd"]
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in headers}
    print("  ".join(h.ljust(widths[h]) for h in headers))
    print("  ".join("-" * widths[h] for h in headers))
    for r in rows:
        print("  ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and test sample prompts for trading agents.")
    parser.add_argument(
        "--agent",
        choices=["all", "options_specialist", "sentiment_analyst", "strategist", "risk_manager", "desk_head"],
        default="all",
        help="Agent prompt to test.",
    )
    parser.add_argument("--live", action="store_true", help="Call the configured LLM backend and validate JSON.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--show-prompts", action="store_true", help="Include prompt text in JSON output.")
    parser.add_argument("--assumed-completion-tokens", type=int, default=300, help="Offline completion token assumption.")
    parser.add_argument("--input-cost-per-1m", type=float, default=0.0, help="Input token price in USD per 1M tokens.")
    parser.add_argument("--output-cost-per-1m", type=float, default=0.0, help="Output token price in USD per 1M tokens.")
    args = parser.parse_args()

    cases = _prompt_cases()
    selected = list(cases.values()) if args.agent == "all" else [cases[args.agent]]

    estimates = [
        _estimate(
            case,
            max(0, int(args.assumed_completion_tokens)),
            max(0.0, float(args.input_cost_per_1m)),
            max(0.0, float(args.output_cost_per_1m)),
        )
        for case in selected
    ]

    output: dict[str, Any] = {"mode": "live" if args.live else "offline", "estimates": estimates}
    if args.show_prompts:
        output["prompts"] = {
            case.role: {"system": case.system, "user": json.loads(case.user)}
            for case in selected
        }
    if args.live:
        output["live_results"] = [_run_live(case) for case in selected]

    if args.json or args.live or args.show_prompts:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _print_table(estimates)
        print("\nLive LLM calls are off. Add --live to test responses and schema validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
