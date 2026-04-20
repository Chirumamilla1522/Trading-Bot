"""
FirmState persistence — survives API server restarts.

As of v0.2.x, FirmState is stored in SQLite (see ``agents/data/app_db.py``) instead of
rewriting large JSON snapshots on every update (which caused latency spikes).

Migration:
- If ``agents/_firm_state.json`` exists (or ``FIRM_STATE_FILE`` points to a JSON file),
  it is imported once into SQLite and then left as-is (you may delete it).
"""
from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Optional

from agents.state import FirmState

log = logging.getLogger(__name__)

_DEFAULT_JSON_PATH = pathlib.Path(__file__).resolve().parent / "_firm_state.json"


def _legacy_json_path() -> pathlib.Path:
    """
    Legacy JSON snapshot location. If FIRM_STATE_FILE is set and ends with .json,
    treat it as legacy and import from there.
    """
    p = os.getenv("FIRM_STATE_FILE", "").strip()
    if p and p.lower().endswith(".json"):
        return pathlib.Path(p)
    return _DEFAULT_JSON_PATH


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)).strip()))
    except Exception:
        return default


_MAX_REC_DEFAULT = 500


def _max_persisted_recommendations() -> int:
    try:
        return max(50, int(os.getenv("MAX_PERSISTED_RECOMMENDATIONS", str(_MAX_REC_DEFAULT))))
    except ValueError:
        return _MAX_REC_DEFAULT


def _max_persisted_greeks() -> int:
    """Max ``latest_greeks`` rows kept in the JSON snapshot (tail of list)."""
    try:
        return max(100, min(100_000, _env_int("MAX_PERSISTED_GREEKS", 5000)))
    except Exception:
        return 5000


def _max_persisted_news() -> int:
    try:
        return max(20, min(5_000, _env_int("MAX_PERSISTED_NEWS", 250)))
    except Exception:
        return 250


def _max_tier3_digests() -> int:
    try:
        return max(10, min(2_000, _env_int("MAX_PERSISTED_TIER3_DIGESTS", 400)))
    except Exception:
        return 400


def _reset_volatile_on_load() -> bool:
    v = os.getenv("FIRM_STATE_RESET_VOLATILE_ON_LOAD", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _apply_legacy_volatile_reset(raw: dict[str, Any]) -> None:
    raw["circuit_breaker_tripped"] = False
    raw["kill_switch_active"] = False
    raw["pending_proposal"] = None
    raw["latest_greeks"] = []
    raw["vol_surface"] = None
    raw["news_feed"] = []
    raw["debate_record"] = None


def _apply_save_caps(raw: dict[str, Any], max_log_entries: int) -> None:
    if "reasoning_log" in raw and isinstance(raw["reasoning_log"], list):
        if len(raw["reasoning_log"]) > max_log_entries:
            raw["reasoning_log"] = raw["reasoning_log"][-max_log_entries:]

    cap = _max_persisted_recommendations()
    if "pending_recommendations" in raw and isinstance(raw["pending_recommendations"], list):
        recs = raw["pending_recommendations"]
        if len(recs) > cap:
            raw["pending_recommendations"] = recs[-cap:]

    mg = _max_persisted_greeks()
    if "latest_greeks" in raw and isinstance(raw["latest_greeks"], list):
        if len(raw["latest_greeks"]) > mg:
            raw["latest_greeks"] = raw["latest_greeks"][-mg:]

    mn = _max_persisted_news()
    if "news_feed" in raw and isinstance(raw["news_feed"], list):
        if len(raw["news_feed"]) > mn:
            raw["news_feed"] = raw["news_feed"][-mn:]

    mt = _max_tier3_digests()
    if "tier3_structured_digests" in raw and isinstance(raw["tier3_structured_digests"], list):
        if len(raw["tier3_structured_digests"]) > mt:
            raw["tier3_structured_digests"] = raw["tier3_structured_digests"][-mt:]


def save_state(state: FirmState, max_log_entries: int = 500) -> bool:
    """
    Persist FirmState into SQLite.
    ``max_log_entries`` bounds ``reasoning_log`` (default 500).
    """
    try:
        from agents.data.app_db import kv_put

        raw = state.model_dump(mode="json")
        _apply_save_caps(raw, max_log_entries=max_log_entries)
        kv_put("firm_state", raw)
        return True
    except Exception as e:
        log.warning("State save failed: %s", e)
        return False


def load_state() -> Optional[FirmState]:
    """
    Load FirmState from SQLite.
    If a legacy JSON snapshot exists and SQLite is empty, import it once.
    """
    try:
        from agents.data.app_db import kv_get, kv_put
        raw = kv_get("firm_state")
        if raw is None:
            # One-time migration from legacy JSON snapshot if present.
            path = _legacy_json_path()
            if path.exists():
                try:
                    import json as _json

                    legacy = _json.loads(path.read_text(encoding="utf-8"))
                    legacy.pop("_saved_at", None)
                    kv_put("firm_state", legacy)
                    raw = legacy
                    log.info("Migrated legacy FirmState JSON → SQLite (%s)", path)
                except Exception as e:
                    log.warning("Legacy JSON migration failed: %s", e)
                    return None
            else:
                return None

        if not isinstance(raw, dict):
            return None

        if _reset_volatile_on_load():
            _apply_legacy_volatile_reset(raw)

        state = FirmState.model_validate(raw)
        return state
    except Exception as e:
        log.warning("State load failed (starting fresh): %s", e)
        return None


def delete_state() -> bool:
    """Delete persisted FirmState from SQLite (and legacy JSON if present)."""
    try:
        from agents.data.app_db import kv_put

        kv_put("firm_state", {})
        # Best-effort legacy cleanup
        p = _legacy_json_path()
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("State delete failed: %s", e)
        return False
