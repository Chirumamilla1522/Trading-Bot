"""
Explainable AI (XAI) – Reasoning Log Persistence
Every autonomous trade action is recorded in human-readable format for:
  1. Compliance auditing (2026 SEC AI Governance Rule / EU AI Act Art. 13)
  2. Post-trade analysis and strategy improvement
  3. Real-time display in the terminal UI

Storage options:
  - SQLite (default; durable + queryable)
  - QuestDB (TimeSeries SQL) for high-frequency analytics
  - PostgreSQL for long-term archival
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from agents.state import FirmState, ReasoningEntry

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_xai_log_dir() -> Path:
    raw = os.getenv("XAI_LOG_DIR", "logs/xai").strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    return _PROJECT_ROOT / p


LOG_DIR = _resolve_xai_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

AGENT_LOG_SUBDIR = "agents"


def _agent_slug_for_file(agent: str) -> str:
    """Safe single-segment filename stem from agent id (e.g. DeskHead, News Processor)."""
    s = (agent or "agent").strip() or "agent"
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", ".", "_"):
            out.append("_")
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return (slug[:80] if slug else "agent")


def persist_reasoning_log(state: FirmState) -> None:
    """Append all new reasoning entries to SQLite (and optionally JSONL when enabled)."""
    write_jsonl = os.getenv("XAI_JSONL", "0").strip().lower() in ("1", "true", "yes", "on")
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = LOG_DIR / f"reasoning_{date_str}.jsonl"

    new_entries = [e for e in state.reasoning_log
                   if not getattr(e, "_persisted", False)]

    if not new_entries:
        return

    agent_dir = LOG_DIR / AGENT_LOG_SUBDIR
    if write_jsonl:
        agent_dir.mkdir(parents=True, exist_ok=True)

    main_lines: list[str] = []
    per_agent_lines: dict[Path, list[str]] = {}
    for entry in new_entries:
        record = {
            # Always serialize as timezone-aware ISO-8601.
            # UI renders this in America/New_York (ET), but storage remains UTC.
            "timestamp": (
                entry.timestamp
                if getattr(entry.timestamp, "tzinfo", None) is not None
                else entry.timestamp.replace(tzinfo=timezone.utc)
            ).isoformat(),
            "agent":     entry.agent,
            "action":    entry.action,
            "reasoning": entry.reasoning,
            "inputs":    entry.inputs,
            "outputs":   entry.outputs,
            "trade_id":  entry.trade_id,
            "ticker":    state.ticker,
        }
        try:
            from agents.data.app_db import append_xai_row

            append_xai_row(record)
        except Exception:
            pass

        if write_jsonl:
            import json as _json
            line = _json.dumps(record) + "\n"
            main_lines.append(line)
            slug = _agent_slug_for_file(entry.agent or "agent")
            p = agent_dir / f"{slug}_{date_str}.jsonl"
            per_agent_lines.setdefault(p, []).append(line)
        entry._persisted = True  # type: ignore[attr-defined]

    if write_jsonl:
        with open(log_file, "a", encoding="utf-8") as f:
            f.writelines(main_lines)
        for path, lines in per_agent_lines.items():
            with open(path, "a", encoding="utf-8") as af:
                af.writelines(lines)
        log.info("XAI: persisted %d entries to %s", len(new_entries), log_file)
    else:
        log.debug("XAI: persisted %d entries to SQLite", len(new_entries))


def log_cycle_failure(reasoning: str, *, ticker: str | None = None) -> None:
    """
    When the LangGraph run throws before `xai_log`, nothing is persisted.
    Append a SYSTEM/ERROR line so the terminal reasoning panel and JSONL stay useful.
    """
    ts = datetime.now(timezone.utc).isoformat()
    record = {
        "timestamp": ts,
        "agent": "SYSTEM",
        "action": "ERROR",
        "reasoning": reasoning,
        "inputs": {},
        "outputs": {},
        "trade_id": None,
        "ticker": ticker or "",
    }
    try:
        from agents.data.app_db import append_xai_row

        append_xai_row(record)
    except Exception:
        pass

    # Optional JSONL mirror
    if os.getenv("XAI_JSONL", "0").strip().lower() in ("1", "true", "yes", "on"):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = LOG_DIR / f"reasoning_{date_str}.jsonl"
        import json as _json
        line = _json.dumps(record) + "\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
        agent_dir = LOG_DIR / AGENT_LOG_SUBDIR
        agent_dir.mkdir(parents=True, exist_ok=True)
        slug = _agent_slug_for_file("SYSTEM")
        with open(agent_dir / f"{slug}_{date_str}.jsonl", "a", encoding="utf-8") as f:
            f.write(line)


def format_human_readable(entry: ReasoningEntry) -> str:
    """Returns a natural language audit string suitable for UI display."""
    return (
        f"[{entry.timestamp.strftime('%H:%M:%S')}] "
        f"{entry.agent} → {entry.action}: {entry.reasoning}"
    )


def get_today_log(
    *,
    tail: int | None = None,
    agent: str | None = None,
) -> list[dict]:
    """
    Read today's log for the UI reasoning panel (file order: oldest first).

    When ``agent`` is set, only rows whose ``agent`` field equals that string
    (exact match, after strip) are returned. Prefer reading the combined daily
    file so filters stay consistent with ``tail``.
    """
    try:
        from agents.data.app_db import read_xai_rows

        return read_xai_rows(tail=tail, agent=agent)
    except Exception:
        # Fallback to legacy JSONL if SQLite isn't available yet
        import json as _json
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = LOG_DIR / f"reasoning_{date_str}.jsonl"
        if not log_file.exists():
            return []
        want = (agent or "").strip()
        entries: list[dict] = []
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                try:
                    row = _json.loads(line)
                except Exception:
                    continue
                if want and (row.get("agent") or "").strip() != want:
                    continue
                entries.append(row)
        if tail is not None and tail > 0 and len(entries) > tail:
            return entries[-tail:]
        return entries
