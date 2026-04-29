"""
Token counting smoke test.

Purpose
-------
Quickly validate that token usage numbers shown in the UI are sane:
- If the provider reports usage, we show it.
- If local providers omit or misreport usage (common), we estimate tokens from text.

This script does NOT place orders and does not depend on the full agent graph.

Examples
--------
Local-only (uses your normal routing):
  python3 agents/token_smoke_test.py --agent stock_specialist

Force local base URL (bypass routing) for debugging:
  LLAMA_LOCAL_BASE_URL=http://127.0.0.1:8001/v1 python3 agents/token_smoke_test.py --agent strategist

Offline estimation only (no network calls):
  python3 agents/token_smoke_test.py --offline
"""

from __future__ import annotations

import argparse
import json
import time
import sys
import os
from pathlib import Path
from typing import Any

# Ensure the project root is first on sys.path so `import agents.*` resolves to this repo
# even if the user has some other `agents` package installed.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _sample_messages() -> list[dict[str, str]]:
    # Use the real StockSpecialist system prompt + realistic sample context payload
    # so token accounting reflects production-like prompts.
    from agents.agents.stock_specialist import SYSTEM_PROMPT as STOCK_SYSTEM_PROMPT

    sample_context = {
        "ticker": "NVDA",
        "underlying_price": 1003.01,
        "market_regime": "TRENDING_UP",
        "price_change_pct": -6.28,
        "momentum": -0.00401,
        "vol_ratio": 1.34,
        "movement_signal": -0.22,
        "movement_anomaly": False,
        "aggregate_sentiment": 0.43,
        "news_timing_regime": "moderate",
        "market_bias_score": -0.4263,
        "technical_context": {
            "regime_label": "range",
            "ema200": 428.08,
            "dist_to_ema200_pct": 134.3,
            "rsi14": 57.4,
            "volume_state": "neutral",
            "outside_week_state": "UNCLEAR",
            "supports": [
                {"kind": "support", "price": 970.0, "source": "swing_low", "distance_pct": -3.3},
                {"kind": "support", "price": 940.0, "source": "vwap_band", "distance_pct": -6.3},
            ],
            "resistances": [
                {"kind": "resistance", "price": 1020.0, "source": "pivot_high", "distance_pct": 1.7},
                {"kind": "resistance", "price": 1050.0, "source": "round_number", "distance_pct": 4.7},
            ],
            "inflection_point": "volatility",
            "inflection_tags": ["BB_SQUEEZE", "MACD_DIVERGENCE_BEARISH"],
        },
        "portfolio": {
            "current_nav": 100000.0,
            "position_cap_dollars": 2000.0,
            "existing_stock_qty": 0.0,
            "drawdown_pct": 0.021,
        },
    }

    return [
        {"role": "system", "content": STOCK_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(sample_context, indent=2)},
    ]


def _to_langchain_messages(msgs: list[dict[str, str]]) -> list[Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    out: list[Any] = []
    for m in msgs:
        r = (m.get("role") or "").lower().strip()
        c = str(m.get("content") or "")
        if r == "system":
            out.append(SystemMessage(content=c))
        else:
            out.append(HumanMessage(content=c))
    return out


def _offline_estimate() -> int:
    from agents.llm_retry import _messages_to_plaintext, _rough_token_estimate

    prompt = _messages_to_plaintext(_to_langchain_messages(_sample_messages()))
    return _rough_token_estimate(prompt)


def _token_breakdown(msgs: list[dict[str, str]], completion_text: str = "") -> dict[str, Any]:
    from agents.llm_retry import _messages_to_plaintext, _rough_token_estimate

    _hf_tok_cache: dict[str, Any] = {"tok": None, "model": None, "err": None}

    def _get_hf_tokenizer():
        if _hf_tok_cache["tok"] is not None:
            return _hf_tok_cache["tok"]
        if _hf_tok_cache["err"] is not None:
            return None
        model_name = os.getenv("TOKEN_SMOKE_TEST_TOKENIZER_MODEL", "meta-llama/Llama-3-8b")
        try:
            from transformers import AutoTokenizer  # type: ignore

            tok = AutoTokenizer.from_pretrained(model_name)
            _hf_tok_cache["tok"] = tok
            _hf_tok_cache["model"] = model_name
            return tok
        except Exception as e:
            _hf_tok_cache["err"] = str(e)
            return None

    def _tokenize_exact(text: str) -> dict[str, Any]:
        if not text:
            return {"token_count": 0, "token_pieces": [], "method": "empty"}
        # Preferred: HuggingFace tokenizer pieces (what user asked for).
        tok = _get_hf_tokenizer()
        if tok is not None:
            try:
                pieces = tok.tokenize(text)
                return {
                    "token_count": int(len(pieces)),
                    "token_pieces": pieces,
                    "method": f"transformers:{_hf_tok_cache.get('model') or 'unknown'}",
                }
            except Exception:
                pass
        try:
            import tiktoken  # type: ignore

            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                enc = tiktoken.encoding_for_model("gpt-4o-mini")
            ids = enc.encode(text)
            # Best-effort "pieces" from ids; not semantic words, but readable token strings.
            pieces: list[str] = []
            for tid in ids:
                try:
                    pieces.append(enc.decode([tid]))
                except Exception:
                    pieces.append(f"<{tid}>")
            return {"token_count": int(len(ids)), "token_pieces": pieces, "method": "tiktoken:cl100k_base"}
        except Exception:
            # Keep it explicit when exact tokenizer is unavailable.
            est = _rough_token_estimate(text)
            return {"token_count": int(est), "token_pieces": [], "method": "approx:chars_div_4"}

    per_message: list[dict[str, Any]] = []
    content_only_total = 0
    for i, m in enumerate(msgs):
        role = str(m.get("role") or "user")
        content = str(m.get("content") or "")
        tok = _tokenize_exact(content)
        content_only_total += int(tok["token_count"])
        per_message.append(
            {
                "i": i,
                "role": role,
                "chars": len(content),
                "content_token_count": int(tok["token_count"]),
                "content_token_method": tok["method"],
                "content_token_pieces": tok["token_pieces"],
                "content_preview": content.replace("\n", " ")[:120],
            }
        )

    prompt_text = _messages_to_plaintext(_to_langchain_messages(msgs))
    prompt_tok = _tokenize_exact(prompt_text)
    completion_tok = _tokenize_exact(completion_text) if completion_text else {"token_count": 0, "token_pieces": [], "method": "empty"}
    prompt_est = int(prompt_tok["token_count"])
    completion_est = int(completion_tok["token_count"])
    return {
        "prompt": {
            "flattened_chars": len(prompt_text),
            "flattened_token_count": prompt_est,
            "flattened_token_method": prompt_tok["method"],
            "flattened_token_pieces": prompt_tok["token_pieces"],
            "flattened_preview": prompt_text.replace("\n", " ")[:180],
            "content_only_total_tokens": int(content_only_total),
            "messages": per_message,
        },
        "completion": {
            "chars": len(completion_text),
            "token_count": completion_est,
            "token_method": completion_tok["method"],
            "token_pieces": completion_tok["token_pieces"],
            "preview": completion_text.replace("\n", " ")[:180],
        },
        "content_only_total_tokens": int(content_only_total + completion_est),
        "serialized_total_tokens": int(prompt_est + completion_est),
    }


def _resolve_model_slug(agent: str) -> str:
    from agents.config import MODELS

    a = agent.strip().lower()
    if a == "desk_head":
        return MODELS.desk_head.active
    if a == "options_specialist":
        return MODELS.options_specialist.active
    if a == "sentiment_analyst":
        return MODELS.sentiment_analyst.active
    if a == "risk_manager":
        return MODELS.risk_manager.active
    if a == "bull_researcher":
        return MODELS.bull_researcher.active
    if a == "bear_researcher":
        return MODELS.bear_researcher.active
    if a == "strategist":
        return MODELS.strategist.active
    if a == "stock_specialist":
        # Reuse strategist route if stock specialist isn't configured separately in MODELS.
        return MODELS.strategist.active
    return MODELS.strategist.active


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke test for token counting (reported vs estimated).")
    p.add_argument(
        "--agent",
        default="stock_specialist",
        help="agent role label for routing/attribution (e.g. stock_specialist, strategist, sentiment_analyst)",
    )
    p.add_argument("--offline", action="store_true", help="Only run offline token estimation (no LLM call).")
    p.add_argument("--n", type=int, default=1, help="Number of calls to run (integration mode).")
    args = p.parse_args()

    if args.offline:
        msgs = _sample_messages()
        est = _offline_estimate()
        breakdown = _token_breakdown(msgs, completion_text="")
        print(
            json.dumps(
                {
                    "mode": "offline",
                    "estimated_prompt_tokens": est,
                    "token_breakdown": breakdown,
                },
                indent=2,
            )
        )
        return 0

    from agents.llm_providers import chat_llm
    from agents.llm_retry import invoke_llm_with_metrics

    agent_role = str(args.agent).strip().lower()
    model_slug = _resolve_model_slug(agent_role)
    raw_msgs = _sample_messages()
    msgs = _to_langchain_messages(raw_msgs)

    llm = chat_llm(model_slug, agent_role=agent_role, temperature=0.1)

    results: list[dict[str, Any]] = []
    for i in range(max(1, int(args.n))):
        t0 = time.time()
        resp, meta = invoke_llm_with_metrics(llm, msgs)
        dt = max(0.0, time.time() - t0)
        text = ""
        try:
            c = getattr(resp, "content", None)
            text = c if isinstance(c, str) else str(c or "")
        except Exception:
            text = ""
        results.append(
            {
                "i": i,
                "backend": meta.get("backend"),
                "model": meta.get("model"),
                "duration_s": round(dt, 4),
                "prompt_tokens": meta.get("prompt_tokens"),
                "completion_tokens": meta.get("completion_tokens"),
                "total_tokens": meta.get("total_tokens"),
                "response_chars": len(text),
                "response_preview": text.strip().replace("\n", " ")[:180],
                "token_breakdown": _token_breakdown(raw_msgs, completion_text=text),
            }
        )

    print(json.dumps({"agent_role": agent_role, "model_slug": model_slug, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

