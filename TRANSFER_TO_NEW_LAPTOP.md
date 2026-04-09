## Goal
Move this project to another laptop and keep:
- the **code**,
- the bot’s **runtime continuity** (persisted state + XAI reasoning logs),
- and Cursor’s **project “knowledge”** (rules + agent chat transcripts) so Cursor can reference past work.

This repo is **not a git repo** right now, so the most reliable approach is to **export a bundle** and restore it on the new machine.

---

## What counts as “state” in this project

- **Persisted FirmState snapshot**: `agents/_firm_state.json`
  - Written by `agents/state_persistence.py`
  - Restored on API server startup (see `agents/api_server.py` lifespan)
- **XAI reasoning logs**: `logs/xai/reasoning_YYYYMMDD.jsonl`
  - Used by the UI reasoning panel (`GET /reasoning_log`)

Note: `news_feed` is intentionally **not** persisted (it’s reset on restart).

---

## Export (old laptop)

From the project root, run:

```bash
bash scripts/export_cursor_bundle.sh
```

This creates a `transfer_bundle/` directory containing:
- `project/` (the codebase, excluding heavy build artifacts)
- `runtime_state/agents/_firm_state.json` (if present)
- `runtime_state/logs/xai/` (if present)
- `cursor_project/` (Cursor project metadata + **agent chat transcripts**, if found)

### Does it include “this chat”?
Yes — Cursor stores chats as transcript files under the project’s `agent-transcripts/` directory.
The export copies the entire `agent-transcripts` folder, so this chat is included.

Then zip it and copy it to the new laptop (AirDrop/USB/etc).

---

## Restore (new laptop)

1. Unzip the bundle somewhere (e.g. `~/Projects/Trading Bot/`).
2. Copy `project/` to your desired location (or keep it where it is).
3. Copy runtime state into the project:
   - `runtime_state/agents/_firm_state.json` → `<project>/agents/_firm_state.json`
   - `runtime_state/logs/xai/` → `<project>/logs/xai/`
4. Restore Cursor knowledge:
   - Copy `cursor_project/` into your new laptop’s Cursor projects folder.
   - If Cursor doesn’t pick it up automatically, open the project folder in Cursor; the transcripts can still be used as a reference archive.

---

## Recreate runtime dependencies (new laptop)

### Python agents
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r agents/requirements.txt
```

### Local LLM (llama.cpp server)
Run an OpenAI-compatible server (example):
```bash
llama-server -m /path/to/model.gguf --host 127.0.0.1 --port 8080
```

### Services (optional)
- **Redis** (only if semantic cache enabled)

### Environment
Copy `.env` securely (do not share publicly). Then start:
```bash
python3 agents/api_server.py
```

---

## Security note
Your `.env` contains secrets. Move it via a secure method and rotate keys if you suspect it was exposed.

