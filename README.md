# Agentic Trading Terminal

An autonomous options trading system combining:
- a **multi-agent decision pipeline** (LangGraph)
- a **FastAPI backend** (market data, orders, background tasks, WS)
- a **desktop terminal UI** (Tauri + HTML/JS)
- **XAI audit logging** (JSONL reasoning log)

This README is intentionally exhaustive so you can move machines, debug quickly, and know where every major piece lives.

## Docs (split by topic)

- **`INSTALL.md`** — install Python and Node packages (venv, `pip`, `npm`, Tauri)
- `docs/README.md` (index)
- `docs/ARCHITECTURE.md`
- `docs/AGENTS.md`
- `docs/API.md`
- `docs/CONFIG.md`
- `docs/OPERATIONS.md`
- `docs/TROUBLESHOOTING.md`
- `docs/UI.md`

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Tauri Desktop UI                            │
│  Price pill · Options chain · Positions · Order ticket · Blotter│
│  WebSocket (live) ──── falls back to REST polling              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP / WebSocket :8000
┌──────────────────────────▼──────────────────────────────────────┐
│                   FastAPI API Server                             │
│  /state  /ws  /options  /bars  /quote  /order/*  /positions/*  │
│  Background tasks:                                              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  tick_ingestion (15s)    greeks_update (15s)             │   │
│  │  equity_sync (30s)       position_monitor (20s)          │   │
│  │  portfolio_history (20s) ws_broadcast (2s)               │   │
│  │  agent_cycle (60s)       scanner (continuous)            │   │
│  │  scanner_driven_cycle    state_persistence               │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│              LangGraph Multi-Agent Pipeline                      │
│                                                                  │
│  ingest_data ──► options_specialist ──► sentiment_analyst        │
│                         │                       │                │
│                         └────────┬──────────────┘               │
│                                  ▼                               │
│                             strategist                           │
│                                  │                               │
│                             risk_manager                         │
│                                  │                               │
│                    (adversarial_debate if enabled)               │
│                                  │                               │
│                              desk_head                           │
│                                  │                               │
│                         trader (deterministic)                   │
│                                  │                               │
│                               xai_log                            │
└──────────────────────────┬──────────────────────────────────────┘
                           │
        ┌──────────────────┴───────────────────┐
        ▼                                       ▼
  Alpaca Broker API                      Disk / QuestDB
  (paper or live)                     (state, XAI logs, backtest results)
```

---

## Agent pipeline

| Agent | Role | Model (testing) |
|-------|------|----------------|
| `ingest_data` | Builds vol surface, IV metrics (skew, term structure, regime) deterministically | — (no LLM) |
| `options_specialist` | Analyses IV surface for structural opportunities | `gemini-2.0-flash-thinking-exp:free` |
| `sentiment_analyst` | Recency-weighted news scoring with per-headline Redis cache | `llama-4-scout:free` |
| `strategist` | Regime-aware strategy selection; outputs `TradeProposal` with stop/take-profit | `llama-4-maverick:free` |
| `risk_manager` | 5 deterministic hard gates (drawdown, delta, gamma, vega, concentration) + LLM soft assessment | `deepseek-r1:free` |
| `adversarial_debate` | Bull/Bear debate with conviction scoring; judge gets full market context | `llama-4-scout:free` / `qwq-32b:free` |
| `desk_head` | Synthesises all verdicts with explicit signal weights → final PROCEED/HOLD/ABORT | `llama-4-maverick:free` |
| `trader` | **Deterministic** order construction from proposal — no LLM, validates prices against live bid/ask | — (no LLM) |
| `xai_log` | Persists full reasoning chain to disk | — |

### Where the pipeline is defined
- **Graph wiring**: `agents/graph.py`
- **Agent implementations**: `agents/agents/*.py`
- **Shared state schema**: `agents/state.py`
- **XAI persistence**: `agents/xai/reasoning_log.py` (writes `logs/xai/reasoning_YYYYMMDD.jsonl`)

### Decision flow

```
circuit_breaker? ──YES──► early_abort (all ABORT, no LLM calls)
       │NO
       ▼
analyst_confidence < 0.3 AND sentiment_confidence < 0.3? ──► HOLD (strategist skips)
       │
risk hard limits violated? ──► ABORT (no LLM)
       │
strategy_confidence < 0.40? ──► ABORT
       │
debate conviction score imbalanced? ──► verdict feeds desk_head
       │
desk_head synthesises → trader deterministically builds order
```

---

## Features

### Market data
- **Alpaca** options chain (greeks, IV, bid/ask) via `AlpacaDelayedFeed`
- **yfinance** fallback for stock quotes, fundamentals, peers
- **SP500 scanner**: continuous background scan of ~400 tickers, sorted by IV / P-C ratio / OI
- **Scanner-driven cycles**: auto-triggers agent cycle on tickers with IV > 85th percentile + P/C > 1.3

### Risk management
| Check | Threshold | Where |
|-------|-----------|-------|
| Daily drawdown | 5% (configurable) | Deterministic, pre-LLM |
| Portfolio delta | ±0.10 | Deterministic |
| Portfolio gamma | $500/pt | Deterministic |
| Portfolio vega | $1,000/1% IV | Deterministic |
| Position count | 10 open spreads | Deterministic |
| Strategy confidence | < 0.40 | Deterministic |
| Max position size | 2% NAV | Deterministic |

### Positions & P&L
- Positions synced from Alpaca every 30 s (stocks **and** options)
- Immediate force-sync after every order placement (2 s delay, then 9 s confirmation)
- **Live option P&L** recomputed every 15 s by matching open positions against latest greeks chain
- **Portfolio Greeks** (delta/gamma/vega/theta) recomputed from live positions every 15 s
- **Stop-loss / take-profit monitor** runs every 20 s; auto-submits market close order on breach

### Persistence
- `FirmState` saved to `agents/_firm_state.json` after every agent cycle
- Loaded on startup so positions and metrics survive server restarts
- Safety fields always reset on load: `circuit_breaker`, `kill_switch`, `pending_proposal`
- `POST /state/reset` deletes the file for a clean start

### “What persists” vs “what does not”
From `agents/state_persistence.py`:
- **Persisted**: ticker, positions, balances, risk metrics, IV metrics, last decisions/confidences, and the last ~100 `reasoning_log` entries.
- **Not persisted by design**: `news_feed` (stale), greeks/vol surface (re-fetched), `pending_proposal` (safety), `kill_switch` / circuit breaker (always reset safe).

### UI
- **WebSocket** (`/ws`) pushes state diffs every 2 s; indicator in topbar shows `● LIVE` vs `● POLL`
- Falls back to HTTP polling automatically on disconnect
- **Live price pill**: real-time ticker, price (flash on change), dollar + % change
- **Drag-to-resize** column dividers, persisted in `localStorage`
- **Collapsible panels** for all major sections
- **Toast notifications** for order fills, errors, and system events
- **Options chain toolbar**: filter C/P, sort by any column, strike range filter
- **Order ticket**: stock (market/limit) and single-leg option orders

---

## Quick start

### Prerequisites
You’ll typically run:
- **Python 3.10+** (recommended) for the agents/API
- **Node 18+** for the UI (Tauri/Vite)
- Optional: **Redis** for semantic cache
- Optional: **llama.cpp** `llama-server` for local LLM
- Optional: **PostgreSQL** if you want persistence across machines (set `WAREHOUSE_POSTGRES_URL`)

**Step-by-step installs (venv, pip, npm, Rust/Tauri):** see **`INSTALL.md`** in the repo root.

### Configuration
Copy `.env.example` and fill in your keys:
```bash
cp config/.env.example .env
```

Key variables:
```env
OPENROUTER_ENABLED=false             # local-only by default
OPENROUTER_API_KEY=sk-or-...          # only needed if OPENROUTER_ENABLED=true
ALPACA_API_KEY=PKxxx                  # Required for market data & orders
ALPACA_SECRET_KEY=xxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # paper trading
TRADING_ENV=testing                   # testing | production
ENABLE_ADVERSARIAL_DEBATE=true
DEBATE_ROUNDS=2
MAX_DAILY_DRAWDOWN=0.05
MAX_POSITION_PCT=0.02

# Optional PostgreSQL (recommended if you move machines / want durable shared storage)
WAREHOUSE_POSTGRES_URL=postgresql://user:pass@host:5432/agentic_trading
WAREHOUSE_AUTO_SCHEMA=1

# Local LLM (llama.cpp server)
LLAMA_LOCAL_BASE_URL=http://127.0.0.1:8080/v1
LLAMA_LOCAL_TIMEOUT_S=120
```

### Local LLM setup (llama.cpp)
This project expects an **OpenAI-compatible** `/v1/chat/completions` server.

Example (llama.cpp):

```bash
llama-server -m /path/to/model.gguf --host 127.0.0.1 --port 8080
```

### Run the API server
```bash
python agents/api_server.py
# or
cd agents && uvicorn api_server:app --host 0.0.0.0 --port 8000
```

### Run the desktop UI (Tauri)
```bash
cd ui
npm install
npm run tauri dev      # development
npm run tauri build    # production binary
```

### Run a backtest
```bash
# 6-month historical backtest on SPY
python -m agents.backtest.paper_trader \
  --mode backtest --ticker SPY \
  --start 2024-01-01 --end 2024-06-30 \
  --capital 100000

# Paper trade live
python -m agents.backtest.paper_trader --mode paper --ticker AAPL
```

Backtest results are saved to `agents/backtest/results/<ticker>_<timestamp>.json`.

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info + endpoint list |
| GET | `/state` | Full FirmState + agent runtime |
| WS | `/ws` | Real-time state push (2 s diffs) |
| GET | `/news` | Latest 50 news items |
| GET | `/reasoning_log` | Today's XAI audit log |

Tip: the UI uses WebSocket (`/ws`) for live state and falls back to polling.

---

## Repo map (where to look)

### Backend (Python)
- `agents/api_server.py`: FastAPI app + background tasks + endpoints + WS
- `agents/graph.py`: LangGraph pipeline (agent order and conditional edges)
- `agents/state.py`: Pydantic v2 state schema (`FirmState`, `ReasoningEntry`, `NewsItem`, positions, risk)
- `agents/state_persistence.py`: saves/loads `agents/_firm_state.json`
- `agents/xai/reasoning_log.py`: persists reasoning JSONL under `logs/xai/`

### Agents (decision makers)
- `agents/agents/options_specialist.py`
- `agents/agents/sentiment_analyst.py`
- `agents/agents/strategist.py`
- `agents/agents/risk_manager.py`
- `agents/agents/adversarial_debate.py`
- `agents/agents/desk_head.py`
- `agents/agents/trader.py` (deterministic, no LLM)

### Data ingestion / market adapters
- `agents/data/news_feed.py`: unified news stream (Benzinga + yfinance + optional synthetic)
- `agents/execution/`: broker adapters + EMS
- `agents/features.py`: deterministic analytics used by agents (IV metrics, chain analytics, etc.)

### UI (Tauri / web)
- `ui/src/index.html`: layout
- `ui/src/main.js`: UI logic (polling + WS + rendering)
- `ui/src/charts.js`: chart helpers
- `ui/src/terminal.css`: styling

### Rust core (if enabled)
- `core/src/`: shared memory / ring buffer utilities

---

## LLM routing (local vs OpenRouter)

The code supports two backends:
- **Local llama.cpp** (default) via `agents/llm_local.py`
- **OpenRouter** (optional) via `agents/llm_openrouter.py`

Routing entrypoint:
- `agents/llm_providers.py` → `chat_llm(...)`

Resilience / retry wrapper:
- `agents/llm_retry.py` → `invoke_llm(...)`

When `OPENROUTER_ENABLED=false`, the system is **local-only**: any LLM timeout becomes a `SYSTEM/ERROR` cycle failure entry in the XAI log.

---

## Troubleshooting

### Reasoning log shows only SYSTEM/ERROR
That means the graph is failing mid-cycle (commonly a local LLM timeout). Check:
- `LLAMA_LOCAL_TIMEOUT_S` is high enough (try 120–300)
- llama-server is running at `LLAMA_LOCAL_BASE_URL`

### News updates but reasoning doesn’t
News ingestion fills `FirmState.news_feed` continuously, but reasoning entries are written when a cycle runs and persists to JSONL.

---

## Moving to another laptop (including Cursor chat history)
Use the transfer bundle scripts and guide:
- `TRANSFER_TO_NEW_LAPTOP.md`
- `scripts/export_cursor_bundle.sh`
- `scripts/zip_transfer_bundle.sh`

The export includes:
- persisted bot state (`agents/_firm_state.json`)
- XAI logs (`logs/xai/`)
- Cursor project metadata + **agent chat transcripts** (from `~/.cursor/projects/.../agent-transcripts`)

| GET | `/agent_status` | Agent loop health |
| GET | `/scanner` | S&P 500 scanner results (`?sort=iv\|pc\|oi\|ticker`) |
| GET | `/scanner/tickers` | Full tracked ticker list |
| GET | `/options/{ticker}` | Full options chain |
| GET | `/bars/{ticker}` | OHLC bars (`?timeframe=5D\|1M\|3M\|6M\|1Y`) |
| GET | `/quote/{ticker}` | Live stock quote |
| GET | `/stock_info/{ticker}` | Fundamentals, peers, dependencies |
| GET | `/portfolio_series` | NAV / Greeks time series |
| POST | `/order/stock` | Place stock order |
| POST | `/order/option` | Place option order |
| GET | `/orders` | Recent broker orders |
| DELETE | `/order/{id}` | Cancel order |
| POST | `/positions/refresh` | Force-sync positions from broker |
| POST | `/set_ticker` | Change active ticker |
| POST | `/run_cycle` | Manually trigger agent cycle |
| POST | `/kill_switch` | Halt all trading |
| POST | `/state/reset` | Delete persisted state file |

---

## Project structure

```
Trading Bot/
├── agents/
│   ├── api_server.py          # FastAPI app + all background tasks
│   ├── graph.py               # LangGraph pipeline definition
│   ├── state.py               # Pydantic FirmState (shared across all agents)
│   ├── features.py            # Deterministic analytics: IV skew, term structure, portfolio Greeks
│   ├── schemas.py             # Pydantic output validation schemas per agent
│   ├── config.py              # Model routing, API keys, feature flags
│   ├── state_persistence.py   # FirmState save/load to JSON
│   ├── llm_retry.py           # OpenRouter retry + local llama.cpp fallback
│   ├── parse_llm_json.py      # Strip fences, extract first JSON object
│   ├── agents/
│   │   ├── options_specialist.py
│   │   ├── sentiment_analyst.py
│   │   ├── strategist.py
│   │   ├── risk_manager.py
│   │   ├── adversarial_debate.py
│   │   ├── desk_head.py
│   │   └── trader.py
│   ├── data/
│   │   ├── opra_client.py      # Alpaca options chain feed
│   │   ├── equity_snapshot.py  # Account + position sync (stocks & options)
│   │   ├── chart_data.py       # OHLC bars (Alpaca + yfinance fallback)
│   │   ├── fundamentals.py     # yfinance fundamentals, peers, dependencies
│   │   ├── news_feed.py        # Benzinga / synthetic news ingestion
│   │   └── sp500.py            # S&P 500 scanner (continuous background)
│   ├── execution/
│   │   ├── ems.py              # Execution Management System (Alpaca / IBKR / Lime)
│   │   └── fix_client.py       # FIX 4.2 client stub (Lime)
│   ├── backtest/
│   │   ├── paper_trader.py     # Full backtest + paper trading framework
│   │   └── results/            # JSON backtest reports
│   └── xai/
│       └── reasoning_log.py    # Persist / read XAI audit trail
├── ui/
│   ├── index.html              # Tauri webview UI
│   └── src/
│       ├── main.js             # Frontend controller (WebSocket, charts, order ticket)
│       ├── charts.js           # TradingView lightweight-charts wrappers
│       └── terminal.css        # Dark-theme terminal styling
├── core/                       # Rust shared-memory bridge (optional, for ultra-low latency)
│   └── Cargo.toml
├── config/
│   └── .env.example
└── .env                        # Your local credentials (not committed)
```

---

## Local LLM (optional)

Set `LLAMA_LOCAL_PRIMARY=true` (default) to route all LLM calls to a local `llama-server` first, falling back to OpenRouter only if it fails:

```bash
llama-server -m qwen2.5-7b-instruct.gguf --host 127.0.0.1 --port 8080
```

Set `LLAMA_LOCAL_PRIMARY=false` for cloud-first with local fallback.

---

## Extending

**Add a new agent**: create `agents/agents/my_agent.py` with a `my_agent_node(state: FirmState) -> FirmState` function, add a matching output schema in `agents/schemas.py`, then wire it into `agents/graph.py`.

**Switch broker**: implement the `BrokerAdapter` interface in `agents/execution/ems.py` and set `BROKER=youradapter` in `.env`.

**Custom risk limits**: override `MAX_DAILY_DRAWDOWN` and `MAX_POSITION_PCT` in `.env`, or modify the hard-coded thresholds at the top of `agents/agents/risk_manager.py`.
