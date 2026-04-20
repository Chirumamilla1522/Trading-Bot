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
  - Reads from SQLite XAI log (`cache/app.sqlite3` table `xai_log`).
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

- The UI primarily uses REST polling (`/state`, `/reasoning_log`, `/agent_status`) and uses `/ws/market` for market hub updates.
- If you see only `SYSTEM/ERROR` lines in reasoning, check `docs/TROUBLESHOOTING.md`.

### Options endpoints (filtering rules)

- `GET /options/{ticker}`
  - Returns the UI options chain for the ticker.
  - Applies:
    - **expiry filter** (expiry must be >= today; and DTE <= `OPTIONS_MAX_DTE_DAYS` / agent default)
    - **strike filter** using asymmetric windows (calls spot→+band, puts −band→spot)
  - Spot is resolved from `GET /quote/{ticker}` first, so the strike window applies consistently across tickers.

### Recommendations endpoints (expiry guardrails)

- `GET /recommendations`
  - Returns persisted recommendations enriched with current quote snapshots (when available).
  - Legs include `expired` + `occ_expiry` when the OCC symbol is expired.
- `POST /recommendations/{rec_id}/approve`
  - Refuses to execute recommendations with expired legs or missing quotes.

