"""
Explainable AI (XAI) – Reasoning Log Persistence
Every autonomous trade action is recorded in human-readable format for:
  1. Compliance auditing (2026 SEC AI Governance Rule / EU AI Act Art. 13)
  2. Post-trade analysis and strategy improvement
  3. Real-time display in the terminal UI

Storage options:
  - JSON Lines file (always on, zero dependencies)
  - QuestDB (TimeSeries SQL) for high-frequency analytics
  - PostgreSQL for long-term archival
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
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


def persist_reasoning_log(state: FirmState) -> None:
    """Append all new reasoning entries to the daily JSONL file."""
    date_str  = datetime.utcnow().strftime("%Y%m%d")
    log_file  = LOG_DIR / f"reasoning_{date_str}.jsonl"

    new_entries = [e for e in state.reasoning_log
                   if not getattr(e, "_persisted", False)]

    if not new_entries:
        return

    with open(log_file, "a", encoding="utf-8") as f:
        for entry in new_entries:
            record = {
                "timestamp": entry.timestamp.isoformat(),
                "agent":     entry.agent,
                "action":    entry.action,
                "reasoning": entry.reasoning,
                "inputs":    entry.inputs,
                "outputs":   entry.outputs,
                "trade_id":  entry.trade_id,
                "ticker":    state.ticker,
            }
            f.write(json.dumps(record) + "\n")
            entry._persisted = True  # type: ignore[attr-defined]

    log.info("XAI: persisted %d entries to %s", len(new_entries), log_file)


def log_cycle_failure(reasoning: str, *, ticker: str | None = None) -> None:
    """
    When the LangGraph run throws before `xai_log`, nothing is persisted.
    Append a SYSTEM/ERROR line so the terminal reasoning panel and JSONL stay useful.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    log_file = LOG_DIR / f"reasoning_{date_str}.jsonl"
    ts = datetime.utcnow().isoformat()
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
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    log.info("XAI: logged cycle failure to %s", log_file)


def format_human_readable(entry: ReasoningEntry) -> str:
    """Returns a natural language audit string suitable for UI display."""
    return (
        f"[{entry.timestamp.strftime('%H:%M:%S')}] "
        f"{entry.agent} → {entry.action}: {entry.reasoning}"
    )


def get_today_log(*, tail: int | None = None) -> list[dict]:
    """Read today's log for the UI reasoning panel (newest rows last in the list)."""
    date_str = datetime.utcnow().strftime("%Y%m%d")
    log_file = LOG_DIR / f"reasoning_{date_str}.jsonl"
    if not log_file.exists():
        return []
    entries: list[dict] = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if tail is not None and tail > 0 and len(entries) > tail:
        return entries[-tail:]
    return entries
