# Installing dependencies

This guide covers **Python** (agents + API) and **Node.js** (web/Tauri UI). Run commands from the **repository root** unless noted.

## Prerequisites

| Tool | Version | Used for |
|------|---------|----------|
| **Python** | 3.10 or newer (3.11+ recommended) | FastAPI, LangGraph, market data, ML |
| **Node.js** | 18 or newer | Vite, Tauri CLI, frontend build |
| **Rust** | stable (via [rustup](https://rustup.rs/)) | Only if you run `npm run tauri dev` / `tauri build` |

Optional:

- **Redis** — semantic / news caching (see `docs/CONFIG.md`).
- **PostgreSQL** — warehouse features when `WAREHOUSE_POSTGRES_URL` is set.

---

## 1. Python packages (backend)

### Create a virtual environment (recommended)

**macOS / Linux**

```bash
cd "/path/to/Trading-Bot"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

**Windows (PowerShell)**

```powershell
cd "C:\path\to\Trading-Bot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### Install requirements

```bash
pip install -r agents/requirements.txt
```

This pulls **LangGraph**, **FastAPI**, **Alpaca**, **PyTorch** (for FinBERT), and other libraries. The first install can take several minutes and several gigabytes (mainly **torch**).

### Lighter install (API-only experimentation)

If you only need the HTTP API and can skip local FinBERT / heavy ML for now, you can install a subset manually (you may need to resolve import errors for unused features):

```bash
pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" pydantic langgraph langchain langchain-openai httpx python-dotenv alpaca-py structlog certifi
```

For full behavior as intended by the repo, use `agents/requirements.txt` as above.

### Verify Python imports

```bash
python -c "import fastapi, langgraph; print('ok')"
```

Run the API (from repo root):

```bash
python3 agents/api_server.py
```

---

## 2. Node.js packages (UI)

```bash
cd ui
npm install
```

This installs Vite, React, Tauri CLI, charts, etc. (`node_modules` is gitignored.)

### Web-only dev (no desktop shell)

```bash
cd ui
npm run dev
```

### Tauri desktop (requires Rust)

```bash
# One-time: install Rust from https://rustup.rs/
cd ui
npm run tauri dev    # development
npm run tauri build  # release binary
```

---

## 3. Environment variables

Secrets are **not** committed. Copy any example env file your team uses (or create `.env` in the repo root) and set at least **Alpaca** (and LLM) keys as described in `README.md` → *Quick start* → *Configuration* and `docs/CONFIG.md`.

---

## Common issues

| Problem | What to try |
|---------|-------------|
| `pip` installs wrong Python | Use `python3 -m pip` or activate `.venv` first. |
| Torch wheel fails on Apple Silicon | Use recent pip; PyTorch publishes `arm64` wheels. Upgrade pip: `pip install -U pip`. |
| `npm install` permission errors | Do not use `sudo npm`; fix npm permissions or use a node version manager (nvm, fnm). |
| Tauri fails to compile | Install Xcode CLI tools (macOS) or MSVC build tools (Windows); ensure `rustc --version` works. |
| Out of disk space | Torch + caches are large; free space or use a machine with more storage. |

For runtime and API issues after install, see `docs/TROUBLESHOOTING.md` and `docs/OPERATIONS.md`.
