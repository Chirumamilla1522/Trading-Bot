#!/usr/bin/env python3
"""
Benchmark OpenAI-compatible LLM backends (local llama.cpp / vLLM / MLX and OpenRouter).

Run from project root::

    python3 agents/benchmark_llms.py --help

Examples::

    # One local server (model from LLAMA_LOCAL_MODEL or probe /models with \"auto\")
    python3 agents/benchmark_llms.py -t local|http://127.0.0.1:8080/v1|auto

    # Local + two OpenRouter open models (needs OPENROUTER_API_KEY in .env)
    python3 agents/benchmark_llms.py \\
      -t local|http://127.0.0.1:8080/v1|qwen2.5-7b-instruct \\
      -t openrouter|deepseek/deepseek-chat-v4-0329 \\
      -t openrouter|meta-llama/llama-3.1-8b-instruct --repeat 3 --json -

Environment: loads ``.env`` from the repo root (same as ``agents/config.py``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Path bootstrap — same pattern as agents/api_server.py
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=_project_root / ".env", override=False)
except ImportError:
    pass


DEFAULT_USER_PROMPT = """You are participating in an API latency benchmark.
Write exactly 6 short bullet points (one line each) about risk limits for directional options trades.
Plain text only, no preamble."""


def _rough_token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_usage(response: Any) -> dict[str, int | None]:
    from agents.llm_retry import _extract_token_usage

    u = _extract_token_usage(response) if response is not None else {}
    out: dict[str, int | None] = {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "total_tokens": u.get("total_tokens"),
    }
    return out


def _build_local_client(*, base_url: str, model: str, max_tokens: int, temperature: float):
    from langchain_openai import ChatOpenAI

    from agents.llm_local import normalize_local_base_url, resolve_local_model_id

    bu = normalize_local_base_url(base_url)
    mid = model.strip().lower()
    if mid in ("auto", ""):
        mid = resolve_local_model_id(bu)
    key = os.getenv("LLAMA_LOCAL_API_KEY", "not-needed").strip() or "not-needed"
    timeout_s = float(os.getenv("LLAMA_LOCAL_TIMEOUT_S", "300"))
    return ChatOpenAI(
        model=mid,
        temperature=temperature,
        max_tokens=max_tokens,
        base_url=bu,
        api_key=key,
        timeout=timeout_s,
    )


def _build_openrouter_client(*, model: str, max_tokens: int, temperature: float):
    from agents.llm_openrouter import openrouter_chat_llm

    return openrouter_chat_llm(model, temperature=temperature, max_tokens=max_tokens)


@dataclass
class BenchResult:
    backend: str
    label: str
    success: bool
    error: str | None
    duration_s: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    completion_tokens_est: int | None
    tokens_per_s: float | None
    response_chars: int


def _run_one(
    *,
    backend: str,
    label: str,
    llm: Any,
    user_prompt: str,
) -> BenchResult:
    from langchain_core.messages import HumanMessage

    t0 = time.perf_counter()
    err: str | None = None
    resp = None
    try:
        resp = llm.invoke([HumanMessage(content=user_prompt)])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    dt = max(1e-9, time.perf_counter() - t0)

    text = ""
    if resp is not None:
        try:
            c = getattr(resp, "content", None)
            text = c if isinstance(c, str) else str(c or "")
        except Exception:
            text = ""

    usage = _extract_usage(resp)
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    tt = usage.get("total_tokens")
    ct_i = ct if isinstance(ct, int) else None

    est: int | None = None
    if ct_i is None and text:
        est = _rough_token_estimate(text)
        ct_i = est

    tps: float | None = None
    if ct_i is not None and ct_i > 0:
        tps = ct_i / dt

    return BenchResult(
        backend=backend,
        label=label,
        success=err is None,
        error=err,
        duration_s=round(dt, 4),
        prompt_tokens=int(pt) if isinstance(pt, int) else None,
        completion_tokens=usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
        total_tokens=int(tt) if isinstance(tt, int) else None,
        completion_tokens_est=est if usage.get("completion_tokens") is None and est else None,
        tokens_per_s=round(tps, 2) if tps is not None else None,
        response_chars=len(text),
    )


def _parse_target(spec: str) -> tuple[str, str, str]:
    """
    Formats:
      local|BASE_URL|model_id   model_id may be ``auto``
      openrouter|MODEL_SLUG     (MODEL_SLUG everything after first |)
    """
    raw = spec.strip()
    # Shorthand: a plain URL is assumed to be a local OpenAI-compatible base_url.
    # Example: "http://192.168.86.49:8001/v1" → ("local", url, "auto")
    if raw.lower().startswith(("http://", "https://")):
        return "local", raw, "auto"
    if raw.lower().startswith("openrouter"):
        parts = raw.split("|", 2)
        if len(parts) < 2:
            raise ValueError(f"openrouter target needs model: {spec!r}")
        slug = parts[1].strip() if len(parts) == 2 else parts[2].strip()
        if not slug:
            raise ValueError(f"empty OpenRouter model slug: {spec!r}")
        return "openrouter", "-", slug
    parts = raw.split("|")
    if len(parts) == 2 and parts[0].strip().lower() == "local":
        return "local", parts[1].strip(), "auto"
    if len(parts) != 3:
        raise ValueError(
            f"Expected local|BASE_URL|model or openrouter|model, got: {spec!r}"
        )
    bk, url, mid = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
    if bk != "local":
        raise ValueError(f"Unknown backend in {spec!r} (use local|... or openrouter|...)")
    return "local", url, mid


def _preset_openrouter_opensource() -> list[str]:
    """Portable slugs often available on OpenRouter — adjust to your subscription."""
    models = (
        "deepseek/deepseek-chat-v4-0329",
        "deepseek/deepseek-v4-flash",
        "meta-llama/llama-3.1-8b-instruct",
        "mistralai/mistral-7b-instruct",
        "google/gemma-2-9b-it",
    )
    return [f"openrouter|{m}" for m in models]


def main() -> int:
    p = argparse.ArgumentParser(
        description="Benchmark latency and tokens for local + OpenRouter OpenAI-compatible APIs."
    )
    p.add_argument(
        "-t",
        "--target",
        dest="targets",
        action="append",
        default=[],
        metavar="SPEC",
        help="local|http://host:port/v1|MODEL or openrouter|org/model-slug "
        '(repeat flag; MODEL may be "auto" for probe /v1/models)',
    )
    p.add_argument(
        "--preset",
        choices=("openrouter-opensource",),
        default=None,
        help="Predefined target list (common OSS slugs on OpenRouter; edit _preset_openrouter_opensource()).",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="UTF-8 file with the user prompt (default: built-in benchmark prompt).",
    )
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Runs per target (median metrics when repeat > 1).",
    )
    p.add_argument(
        "--warmup",
        type=int,
        default=0,
        help="Extra discarded runs per target before measuring.",
    )
    p.add_argument(
        "--json",
        metavar="PATH",
        help='Write NDJSON rows to PATH (use "-" for stdout).',
    )
    args = p.parse_args()

    targets = list(args.targets or [])
    if args.preset == "openrouter-opensource":
        targets.extend(_preset_openrouter_opensource())

    if not targets:
        p.error("Specify at least one -t SPEC or --preset")

    if args.prompt_file is not None:
        user_prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    else:
        user_prompt = DEFAULT_USER_PROMPT

    rows_out: list[dict[str, Any]] = []

    for spec in targets:
        try:
            backend, url, mid = _parse_target(spec)
        except ValueError as e:
            print(f"SKIP {spec}: {e}", file=sys.stderr)
            continue

        if backend == "openrouter" and not (os.getenv("OPENROUTER_API_KEY", "").strip()):
            print(
                f"SKIP {spec!r}: OPENROUTER_API_KEY not set in environment",
                file=sys.stderr,
            )
            continue

        label = spec
        llm_local = (
            _build_openrouter_client(model=mid, max_tokens=args.max_tokens, temperature=args.temperature)
            if backend == "openrouter"
            else _build_local_client(
                base_url=url, model=mid, max_tokens=args.max_tokens, temperature=args.temperature
            )
        )

        for _ in range(max(0, args.warmup)):
            _run_one(backend=backend, label=label, llm=llm_local, user_prompt=user_prompt)

        runs: list[BenchResult] = []
        for _ in range(max(1, args.repeat)):
            runs.append(
                _run_one(backend=backend, label=label, llm=llm_local, user_prompt=user_prompt)
            )

        def _median(xs: list[float | None]) -> float | None:
            nums = [x for x in xs if isinstance(x, (int, float))]
            if not nums:
                return None
            nums_sorted = sorted(nums)
            m = len(nums_sorted) // 2
            if len(nums_sorted) % 2:
                return float(nums_sorted[m])
            return (nums_sorted[m - 1] + nums_sorted[m]) / 2.0

        med_dur = _median([r.duration_s for r in runs])
        med_tps = _median([r.tokens_per_s for r in runs])

        last = runs[-1]
        print(
            f"{backend:11}  dur_s={last.duration_s:<8} "
            f"median_dur={med_dur!s:<8} "
            f"prompt_tok={last.prompt_tokens!s:<6} compl_tok={last.completion_tokens!s:<6} "
            f"est_compl={last.completion_tokens_est!s:<6} "
            f"tps={last.tokens_per_s!s:<10} median_tps={med_tps!s:<10} chars={last.response_chars}"
        )
        print(f"  label: {label}")
        if not last.success:
            print(f"  ERROR: {last.error}")
        print()

        summary = {
            "spec": spec,
            "backend": backend,
            "success": last.success,
            "error": last.error,
            "duration_s_last": last.duration_s,
            "duration_s_median": med_dur,
            "prompt_tokens": last.prompt_tokens,
            "completion_tokens": last.completion_tokens,
            "completion_tokens_est": last.completion_tokens_est,
            "tokens_per_s_last": last.tokens_per_s,
            "tokens_per_s_median": med_tps,
            "repeat": len(runs),
            "response_chars": last.response_chars,
        }
        rows_out.append(summary)

    if args.json:
        out = sys.stdout if args.json == "-" else open(args.json, "w", encoding="utf-8")
        try:
            for row in rows_out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        finally:
            if out is not sys.stdout:
                out.close()

    if not rows_out:
        return 1
    return 0 if all(r.get("success") for r in rows_out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
