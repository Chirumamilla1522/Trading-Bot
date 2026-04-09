## Architecture

This project has three main layers:

1. **Desktop UI** (Tauri + HTML/JS) in `ui/`
2. **FastAPI backend** in `agents/api_server.py`
3. **LangGraph multi-agent pipeline** in `agents/graph.py` with agent nodes in `agents/agents/`

### Data flow (high level)

- Market/news/background tasks update a shared `FirmState` in memory.
- An agent “cycle” runs the LangGraph pipeline against that `FirmState`.
- Each agent appends a `ReasoningEntry` into `FirmState.reasoning_log`.
- The `xai_log` node persists new entries to disk (`logs/xai/reasoning_YYYYMMDD.jsonl`).
- UI reads state via WebSocket `/ws` (preferred) and REST endpoints (fallback).

### Key files

- **Backend entrypoint**: `agents/api_server.py`
- **Graph wiring**: `agents/graph.py`
- **State schema**: `agents/state.py`
- **State persistence**: `agents/state_persistence.py` → `agents/_firm_state.json`
- **XAI reasoning persistence**: `agents/xai/reasoning_log.py` → `logs/xai/*`

### Persistence model

- `agents/_firm_state.json` is written after cycles (and some operations).
- On server start, `load_state()` restores a safe subset of fields.
- Safety flags (`kill_switch_active`, `circuit_breaker_tripped`) are reset to safe defaults.
- `news_feed` is intentionally **not** restored (stale news).

### LLM routing model

- **Local-first** mode uses an OpenAI-compatible llama.cpp server (`LLAMA_LOCAL_BASE_URL`).
- Optional cloud mode can route via OpenRouter when enabled.
- See `docs/CONFIG.md` and the code in:
  - `agents/llm_providers.py`
  - `agents/llm_local.py`
  - `agents/llm_openrouter.py`
  - `agents/llm_retry.py`

