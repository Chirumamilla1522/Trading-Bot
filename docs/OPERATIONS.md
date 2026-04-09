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

- Agent reasoning log: `logs/xai/reasoning_YYYYMMDD.jsonl`
- Persisted state snapshot: `agents/_firm_state.json`

### Safe reset

- Delete `agents/_firm_state.json` to start fresh, or use the API endpoint that deletes state (if present).

### Secrets hygiene

- Treat `.env` as secret.
- Rotate keys if you share logs or screenshots containing tokens/keys.

