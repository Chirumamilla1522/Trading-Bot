"""
Extract structured JSON from LLM chat responses.

Models often wrap JSON in markdown fences (```json ... ```) or add preamble text,
which breaks json.loads on the raw string.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return t


def _first_json_object(text: str) -> str:
    """Return first top-level JSON object substring (respects strings)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced JSON braces")


def parse_llm_json(content: str | Any) -> dict[str, Any]:
    """
    Parse a JSON object from LLM output (plain JSON, fenced, or with leading prose).
    """
    if content is None:
        raise ValueError("empty LLM content")
    text = content if isinstance(content, str) else str(content)
    text = _strip_markdown_fences(text)
    text = text.strip()
    # Fast path: strict JSON only.
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("expected JSON object")
        return obj
    except Exception:
        pass

    # Robust path: extract first object and allow trailing junk ("extra data").
    snippet = _first_json_object(text)
    dec = json.JSONDecoder()
    obj, _end = dec.raw_decode(snippet)
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")
    return obj
