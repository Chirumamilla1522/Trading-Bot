"""
Local OpenAI-compatible HTTP server (/v1/chat/completions).

Primary path when OPENROUTER_ENABLED=false. Typical servers:
  llama-server, MLX-LM OpenAI shim, vLLM, etc.

Load Balancing
--------------
All unique LLM server URLs (from LLAMA_LOCAL_BASE_URL + per-role overrides)
form a pool.  When any agent needs an LLM call, the pool picks the
**least-recently-used healthy** server, so all 4 servers stay busy and
no single server becomes a bottleneck.

Servers that fail a health check are temporarily excluded and re-probed
every HEALTH_RECHECK_S seconds.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

LLAMA_LOCAL_MAX_TOKENS = int(os.getenv("LLAMA_LOCAL_MAX_TOKENS", "2048"))

_KNOWN_AGENT_ROLES: tuple[str, ...] = (
    "options_specialist",
    "sentiment_analyst",
    "strategist",
    "risk_manager",
    "desk_head",
    "bull_researcher",
    "bear_researcher",
    "adversarial_judge",
    "news_processor",
)

_LOCAL_MODEL_ID_CACHE: dict[str, str] = {}
_LOCAL_MODEL_ID_LOCK = threading.Lock()

# ── Load-balancing pool ──────────────────────────────────────────────────────

HEALTH_RECHECK_S = float(os.getenv("LLAMA_LOCAL_HEALTH_RECHECK_S", "60"))
# Health GET /v1/models — LAN / remote hosts may need >3s; some servers omit /models (404) but chat works
LLAMA_PROBE_TIMEOUT_S = float(os.getenv("LLAMA_PROBE_TIMEOUT_S", "8.0"))

class _ServerSlot:
    """One entry in the LLM server pool."""
    __slots__ = ("url", "healthy", "last_used", "last_health_check", "in_flight")
    def __init__(self, url: str):
        self.url: str = url
        self.healthy: bool = True
        self.last_used: float = 0.0
        self.last_health_check: float = 0.0
        self.in_flight: int = 0

class _ServerPool:
    """
    Thread-safe LLM server pool with least-loaded + round-robin selection.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._slots: list[_ServerSlot] = []

    def init_from_env(self) -> list[str]:
        """Discover all unique URLs from env and probe them. Returns healthy URLs."""
        urls = _discover_all_urls()
        with self._lock:
            self._slots = [_ServerSlot(u) for u in urls]
        healthy = self.probe_all()
        return healthy

    def probe_all(self) -> list[str]:
        """Probe every slot and return list of healthy URLs."""
        healthy: list[str] = []
        for slot in self._slots:
            ok = self._probe_one(slot)
            if ok:
                healthy.append(slot.url)
        return healthy

    def _probe_one(self, slot: _ServerSlot) -> bool:
        """
        Quick HTTP check on ``GET .../v1/models``.

        If the server returns 404/405/501 on ``/models`` but the connection succeeded,
        we still treat it as healthy: some stacks only expose ``/v1/chat/completions``.
        """
        try:
            import httpx as _hx

            url = f"{slot.url.rstrip('/')}/models"
            with _hx.Client(timeout=LLAMA_PROBE_TIMEOUT_S) as cli:
                r = cli.get(url)
            code = r.status_code
            if code < 400:
                ok = True
            elif code in (404, 405, 501):
                ok = True
                log.debug(
                    "LLM health: %s returned %s on /models — treating as reachable (chat may still work)",
                    slot.url,
                    code,
                )
            elif code in (502, 503, 504):
                ok = False
            else:
                ok = code < 500
        except Exception as exc:
            log.debug("LLM health probe failed %s: %s", slot.url, exc)
            ok = False
        with self._lock:
            slot.healthy = ok
            slot.last_health_check = time.monotonic()
        return ok

    def acquire(self, agent_role: str | None = None) -> str:
        """
        Pick the best server for the next request.
        Strategy: among healthy servers, pick the one with fewest in_flight
        requests (ties broken by least-recently-used).
        If all are unhealthy, re-probe and try again.
        """
        now = time.monotonic()
        with self._lock:
            # Re-check any unhealthy server that has cooled down
            for s in self._slots:
                if not s.healthy and (now - s.last_health_check) >= HEALTH_RECHECK_S:
                    # Release lock briefly for network call
                    pass  # will probe below

        # Probe stale unhealthy servers outside the lock
        for s in self._slots:
            if not s.healthy and (time.monotonic() - s.last_health_check) >= HEALTH_RECHECK_S:
                self._probe_one(s)

        with self._lock:
            candidates = [s for s in self._slots if s.healthy]
            if not candidates:
                # All down — probe everything once more
                pass

        if not candidates:
            self.probe_all()
            with self._lock:
                candidates = [s for s in self._slots if s.healthy]
            if not candidates:
                # Fall back to default URL even if unhealthy
                return self._slots[0].url if self._slots else _default_base_url()

        with self._lock:
            # Sort by (in_flight ASC, last_used ASC) → least busy, least recent
            candidates.sort(key=lambda s: (s.in_flight, s.last_used))
            chosen = candidates[0]
            chosen.in_flight += 1
            chosen.last_used = time.monotonic()
            return chosen.url

    def release(self, url: str, success: bool = True) -> None:
        """Mark a request as finished. On failure, mark server unhealthy."""
        with self._lock:
            for s in self._slots:
                if s.url == url:
                    s.in_flight = max(0, s.in_flight - 1)
                    if not success:
                        s.healthy = False
                        s.last_health_check = time.monotonic()
                    break

    def mark_unhealthy(self, url: str) -> None:
        with self._lock:
            for s in self._slots:
                if s.url == url:
                    s.healthy = False
                    s.last_health_check = time.monotonic()
                    break

    def status(self) -> list[dict]:
        """For the /llm/status API."""
        with self._lock:
            return [
                {
                    "url": s.url,
                    "healthy": s.healthy,
                    "in_flight": s.in_flight,
                    "idle_s": round(time.monotonic() - s.last_used, 1) if s.last_used else None,
                }
                for s in self._slots
            ]

    @property
    def all_urls(self) -> list[str]:
        with self._lock:
            return [s.url for s in self._slots]

    @property
    def healthy_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._slots if s.healthy)


# Singleton pool instance
server_pool = _ServerPool()


def _default_base_url() -> str:
    return normalize_local_base_url(
        os.getenv("LLAMA_LOCAL_BASE_URL", "http://127.0.0.1:8080/v1"),
    )


def _discover_all_urls() -> list[str]:
    """Collect all unique LLM server URLs from environment."""
    seen: set[str] = set()
    out: list[str] = []

    default = _default_base_url()
    seen.add(default)
    out.append(default)

    for role in _KNOWN_AGENT_ROLES:
        key = role.strip().upper().replace("-", "_")
        explicit = os.getenv(f"LLAMA_LOCAL_BASE_URL_{key}", "").strip()
        if explicit:
            u = normalize_local_base_url(explicit)
            if u not in seen:
                seen.add(u)
                out.append(u)

    host = os.getenv("LLAMA_LOCAL_HOST", "").strip()
    if host:
        for role in _KNOWN_AGENT_ROLES:
            key = role.strip().upper().replace("-", "_")
            port_env = os.getenv(f"LLAMA_LOCAL_PORT_{key}", "").strip()
            if port_env:
                u = normalize_local_base_url(f"http://{host}:{port_env}/v1")
                if u not in seen:
                    seen.add(u)
                    out.append(u)

    return out


def normalize_local_base_url(raw: str) -> str:
    """Ensure base URL ends with ``/v1``."""
    base_url = raw.strip().rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    return base_url


def resolve_local_base_url(agent_role: str | None = None) -> str:
    """
    Pick the best available server from the pool (load-balanced).
    Falls back to env-based static resolution if pool is empty.
    """
    if server_pool.all_urls:
        return server_pool.acquire(agent_role)
    return _default_base_url()


def iter_unique_local_base_urls() -> list[str]:
    """All distinct local base URLs. For startup probes & status display."""
    if server_pool.all_urls:
        return server_pool.all_urls
    return _discover_all_urls()


def _extract_first_model_id(payload: Any) -> str | None:
    """Best-effort extraction of an OpenAI-style model `id`."""
    try:
        if isinstance(payload, dict):
            data = payload.get("data", [])
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    for k in ("id", "model", "name"):
                        v = first.get(k)
                        if v:
                            return str(v)
                return str(first)
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                for k in ("id", "model", "name"):
                    v = first.get(k)
                    if v:
                        return str(v)
            return str(first)
    except Exception:
        return None
    return None


def resolve_local_model_id(base_url: str) -> str:
    """
    Resolve the `model` field for local requests.

    If `LLAMA_LOCAL_MODEL` is set to a real model string, we use it.
    If it's the sentinel/default value (\"local\"), we auto-probe `/models`
    for the correct id and cache it per base_url.
    """
    desired = os.getenv("LLAMA_LOCAL_MODEL", "local").strip()
    desired_lower = desired.lower()
    if desired and desired_lower not in ("local", "auto"):
        return desired

    # Auto mode (desired is "local" or empty): probe/cached by base_url
    with _LOCAL_MODEL_ID_LOCK:
        cached = _LOCAL_MODEL_ID_CACHE.get(base_url)
    if cached:
        return cached

    model_id = desired if desired else "local"
    try:
        import httpx as _hx

        # base_url ends with /v1, so /models becomes /v1/models
        url = f"{base_url.rstrip('/')}/models"
        with _hx.Client(timeout=3.0) as cli:
            r = cli.get(url)
        if r.status_code < 400:
            model_id = _extract_first_model_id(r.json()) or model_id
    except Exception:
        # Keep fallback model_id (most servers ignore `model`, but MLX doesn't in
        # your current setup—so if probing fails, you may still need to set
        # LLAMA_LOCAL_MODEL explicitly.)
        pass

    with _LOCAL_MODEL_ID_LOCK:
        _LOCAL_MODEL_ID_CACHE[base_url] = model_id
    return model_id


def local_chat_llm(
    *,
    agent_role: str | None = None,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    **kwargs: Any,
):
    """
    Build ChatOpenAI pointed at the least-busy healthy local server.
    The pool automatically load-balances across all available servers.
    """
    from langchain_openai import ChatOpenAI

    base_url = resolve_local_base_url(agent_role)
    model = resolve_local_model_id(base_url)
    key = os.getenv("LLAMA_LOCAL_API_KEY", "not-needed").strip() or "not-needed"
    timeout_s = float(os.getenv("LLAMA_LOCAL_TIMEOUT_S", "300"))
    mt = max_tokens if max_tokens is not None else LLAMA_LOCAL_MAX_TOKENS
    llm = ChatOpenAI(
        model=model,
        temperature=temperature,
        max_tokens=mt,
        base_url=base_url,
        api_key=key,
        timeout=timeout_s,
        **kwargs,
    )
    setattr(llm, "_trading_agent_role", agent_role)
    setattr(llm, "_pool_base_url", base_url)
    return llm


def llama_local_chat_llm(llm: Any) -> Any:
    """
    Build a ChatOpenAI for local inference, copying temperature / max_tokens from a stub.
    Uses the pool to pick the least-busy healthy server.
    """
    from langchain_openai import ChatOpenAI

    role = getattr(llm, "_trading_agent_role", None)
    base_url = resolve_local_base_url(role if isinstance(role, str) else None)
    model = resolve_local_model_id(base_url)
    key = os.getenv("LLAMA_LOCAL_API_KEY", "not-needed").strip() or "not-needed"
    timeout_s = float(os.getenv("LLAMA_LOCAL_TIMEOUT_S", "300"))
    temp = float(getattr(llm, "temperature", 0.1))
    max_tok = getattr(llm, "max_tokens", None)
    if max_tok is None:
        max_tok = LLAMA_LOCAL_MAX_TOKENS
    out = ChatOpenAI(
        model=model,
        temperature=temp,
        max_tokens=max_tok,
        base_url=base_url,
        api_key=key,
        timeout=timeout_s,
    )
    if isinstance(role, str):
        setattr(out, "_trading_agent_role", role)
    setattr(out, "_pool_base_url", base_url)
    return out


def local_llama_fallback_enabled() -> bool:
    """When OpenRouter is tried first, also use llama.cpp after OpenRouter fails."""
    return os.getenv("LLAMA_LOCAL_FALLBACK", "true").strip().lower() in (
        "1", "true", "yes",
    )


def llama_local_primary_enabled() -> bool:
    """Try local llama.cpp before OpenRouter when cloud is enabled."""
    return os.getenv("LLAMA_LOCAL_PRIMARY", "true").strip().lower() in (
        "1", "true", "yes",
    )
