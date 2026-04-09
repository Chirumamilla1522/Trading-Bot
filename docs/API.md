## API reference (FastAPI)

Backend entrypoint: `agents/api_server.py`

Base URL (default): `http://localhost:8000`

### Core endpoints

- `GET /`
  - Service info and endpoint list.

- `GET /state`
  - Returns `FirmState` plus `agent_runtime` (loop health counters/timestamps).

- `WS /ws`
  - Preferred realtime channel. Server pushes state diffs frequently.

- `GET /news`
  - Latest news items from `FirmState.news_feed` (most recent slice).

- `GET /reasoning_log?tail=500`
  - Reads today’s JSONL from `logs/xai/`.
  - `tail` controls max number of rows returned (server-enforced bounds).

- `GET /agent_status`
  - Lightweight runtime status: last success/error, counters, etc.

### Control endpoints

- `POST /set_ticker`
  - Changes the active ticker in `FirmState`.

- `POST /run_cycle`
  - Manually triggers an agent cycle (if not already running).

- `POST /kill_switch`
  - Activates the kill switch and circuit breaker.

### Trading endpoints (high level)

There are endpoints for placing orders and syncing positions (see the endpoint list in `/` and `agents/api_server.py`).

### Notes

- The UI primarily uses `/ws` and falls back to polling `/state`, `/reasoning_log`, `/agent_status`.
- If you see only `SYSTEM/ERROR` lines in reasoning, check `docs/TROUBLESHOOTING.md`.

