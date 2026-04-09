"""
FirmState Persistence – survive server restarts.

Serialises the relevant parts of FirmState to a JSON file on disk so that
positions, account balances, and the latest reasoning log are restored
when the API server restarts.

Intentionally NOT persisted (reset on restart):
  - latest_greeks / vol_surface  (re-fetched from market data on next tick)
  - news_feed                    (stale news is useless)
  - pending_proposal             (safety: never auto-re-submit on restart)
  - circuit_breaker / kill_switch (always start safe = not tripped)
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime
from typing import Optional

from agents.state import FirmState

log = logging.getLogger(__name__)

# Store next to the agents package; can be overridden via env var
_DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "_firm_state.json"


def _state_path() -> pathlib.Path:
    import os
    p = os.getenv("FIRM_STATE_FILE", "")
    return pathlib.Path(p) if p else _DEFAULT_PATH


# Fields that ARE safe to restore across restarts
_PERSIST_FIELDS = {
    "ticker",
    "underlying_price",
    "open_positions",
    "stock_positions",
    "cash_balance",
    "buying_power",
    "account_equity",
    "risk",
    "aggregate_sentiment",
    "analyst_decision",
    "sentiment_decision",
    "risk_decision",
    "trader_decision",
    "analyst_confidence",
    "sentiment_confidence",
    "risk_confidence",
    "strategy_confidence",
    "iv_atm",
    "iv_skew_ratio",
    "iv_regime",
    "iv_term_structure",
    "sentiment_themes",
    "sentiment_tail_risks",
    "reasoning_log",   # last 100 entries
}


def save_state(state: FirmState, max_log_entries: int = 100) -> bool:
    """
    Serialise a snapshot of FirmState to disk.
    Returns True on success, False on any error.
    """
    path = _state_path()
    try:
        raw = state.model_dump()
        snapshot: dict = {"_saved_at": datetime.utcnow().isoformat()}
        for field in _PERSIST_FIELDS:
            if field in raw:
                snapshot[field] = raw[field]

        # Keep reasoning log bounded
        if "reasoning_log" in snapshot:
            snapshot["reasoning_log"] = snapshot["reasoning_log"][-max_log_entries:]

        path.write_text(json.dumps(snapshot, default=str, indent=2), encoding="utf-8")
        log.debug("State persisted to %s", path)
        return True
    except Exception as e:
        log.warning("State save failed: %s", e)
        return False


def load_state() -> Optional[FirmState]:
    """
    Load a previously saved FirmState snapshot from disk.
    Returns None if the file doesn't exist or is unreadable.
    Dangerous fields (greeks, pending_proposal, kill_switch) are NOT restored.
    """
    path = _state_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("_saved_at", None)

        # Always reset safety flags and volatile fields on load
        raw["circuit_breaker_tripped"] = False
        raw["kill_switch_active"]       = False
        raw["pending_proposal"]         = None
        raw["latest_greeks"]            = []
        raw["vol_surface"]              = None
        raw["news_feed"]                = []
        raw["debate_record"]            = None

        state = FirmState(**raw)
        log.info(
            "Loaded persisted state: ticker=%s equity=%.2f positions=%d stock=%d",
            state.ticker,
            state.account_equity,
            len(state.open_positions),
            len(state.stock_positions),
        )
        return state
    except Exception as e:
        log.warning("State load failed (starting fresh): %s", e)
        return None


def delete_state() -> bool:
    """Delete the persisted state file (e.g. for a clean restart)."""
    path = _state_path()
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        log.warning("State delete failed: %s", e)
        return False
