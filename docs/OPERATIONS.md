## Operations / runbooks

### Run locally (backend)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r agents/requirements.txt
python3 agents/api_server.py
```

### Run local LLM (llama.cpp)

Example:

```bash
llama-server -m /path/to/model.gguf --host 127.0.0.1 --port 8080
```

If you see timeouts, raise `LLAMA_LOCAL_TIMEOUT_S` and/or reduce prompt sizes.

### Run the UI

```bash
cd ui
npm install
npm run tauri dev
```

### Logs

- **Primary persistence** (SQLite):
  - App DB: `cache/app.sqlite3` (or `APP_DB_PATH` / `AGENTIC_DATA_DIR`)
  - Processed news DB: `cache/news_processed.sqlite3`
  - Portfolio series DB: `cache/portfolio_series.sqlite3`

- **Optional debug mirrors** (off by default):
  - Reasoning JSONL: `logs/xai/reasoning_YYYYMMDD.jsonl` (enable with `XAI_JSONL=1`)
  - Processed news JSONL: `logs/news/processed_YYYYMMDD.jsonl` (enable with `NEWS_JSONL=1`)
  - Market hub JSONL: `logs/market_data/{TICKER}.jsonl` (enable with `MARKET_DATA_JSONL=1`)

### Safe reset

- Delete `cache/app.sqlite3` (and optionally the other `cache/*.sqlite3` files) to start fresh,
  or use the API endpoint that deletes state (if present).

- If you still have a legacy `agents/_firm_state.json`, it is only used for one-time migration.

### Secrets hygiene

- Treat `.env` as secret.
- Rotate keys if you share logs or screenshots containing tokens/keys.

