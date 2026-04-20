## End-to-end dry run (news → agents → recommendation)

This walkthrough is a **dry run** (no broker order is submitted). It is written as a reproducible runbook:
you can follow it with curl calls and understand what the agents *must* do at each stage.

It uses an example ticker `NFLX` (any ticker works).

> Note: Some values below are illustrative. The *shape* of inputs/outputs and the guardrails are accurate.

---

### 0) Starting state (before any cycle)

The backend keeps an in-memory `FirmState` (schema: `agents/state.py`) and persists it to
SQLite (`cache/app.sqlite3` → `kv(k='firm_state')`).

For this dry run, focus on the following state surfaces:

| Category | Fields you will inspect |
|---|---|
| Market | `ticker`, `underlying_price`, `latest_greeks` |
| News | `news_feed`, `aggregate_sentiment`, `sentiment_themes`, `sentiment_tail_risks` |
| Proposal/recs | `pending_proposal`, `pending_recommendations` |
| Control | `trading_mode`, `kill_switch_active`, `circuit_breaker_tripped` |
| Audit | `reasoning_log` (and persisted `xai_log`) |

### 0.1) Pre-flight checks (recommended)

Before you expect a healthy cycle:

- `GET /quote/NFLX` returns a non-null `last` (spot is needed for strike windows).
- `GET /options/NFLX` returns strikes within the window:
  - calls: `[spot, spot*(1+band)]` (default band=0.5)
  - puts:  `[spot*(1-band), spot]`
- `GET /llm/status` shows the active backend (local vs cloud, if enabled).

### 0.2) “One command” dry-run checklist (manual)

If you want to validate the full loop without UI:

1. Set ticker: `POST /set_ticker` with `{"ticker":"NFLX"}`
2. Trigger: `POST /run_cycle`
3. Inspect:
   - `GET /state`
   - `GET /recommendations`
   - `GET /reasoning_log?tail=200`

---

### 1) Pull news (Tier-2 collector)

**Source**: `agents/data/news_feed.py` (unified stream) and background loop in `agents/api_server.py`.

**Input**
- current `FirmState.ticker`

**Output**
- appends items to `FirmState.news_feed`:
  - headline, source, published_at
  - optional url/summary/ticker tags

Example `news_feed` entry:

```json
{
  "headline": "Netflix beats earnings, raises guidance",
  "source": "Reuters",
  "published_at": "2026-04-17T13:05:00+00:00",
  "tickers": ["NFLX"]
}
```

---

### 2) Process news (Tier-2 AI enrichment)

**Processor**: `agents/data/news_processor.py`

**Input**
- recent raw items in `FirmState.news_feed`

**Output**
- stores enriched articles in SQLite: `cache/news_processed.sqlite3`
- updates derived state fields used by agents:
  - `FirmState.news_impact_map` (rollups)
  - `FirmState.sentiment_monitor_*` (monitor synthesis, when enabled)
  - `FirmState.tier3_structured_digests` (compact prompt-ready digests)

Optional debug mirror (off by default):
- `NEWS_JSONL=1` writes `logs/news/processed_YYYYMMDD.jsonl`

---

### 3) Ingest options + guardrails (deterministic)

**Node**: `ingest_data` in `agents/graph.py` (implementation in `agents/tiers.py` / ingest helpers)

**Input**
- `FirmState.ticker`
- market-data providers (Alpaca / Databento / etc.)

**Output**
- sets `FirmState.underlying_price`
- updates `FirmState.latest_greeks` (options snapshot list)

**Critical filtering (expiry + strike windows)**:

The agent chain is filtered by `agents/data/options_chain_filter.py`:

- expiry must be **>= today**
- DTE must be <= `AGENT_OPTIONS_MAX_DTE_DAYS` (default 60)
- strike windows (band = `AGENT_OPTIONS_STRIKE_BAND_PCT`, default 0.5):
  - calls: `[spot, spot*(1+band)]`
  - puts:  `[spot*(1-band), spot]`

This prevents stale/expired OCC symbols (e.g. `NFLX210521...`) from ever reaching the LLMs.

**Important: expiry parsing formats**

Expiry parsing accepts:
- `YYMMDD` (e.g. `260515`)
- `YYYYMMDD` (e.g. `20260515`)
- `YYYY-MM-DD` (e.g. `2026-05-15`) — may appear in persisted state or external APIs

---

### 4) Options specialist (LLM)

**Agent**: `agents/agents/options_specialist.py`

**Input (subset)**
- `underlying_price`
- IV analytics computed from `latest_greeks` (ATM IV, skew, term structure)
- movement/momentum fields

**Output**
- writes:
  - `FirmState.analyst_decision`
  - `FirmState.analyst_confidence`
- appends `ReasoningEntry(agent="OptionsSpecialist", action="PROCEED|HOLD|ABORT", ...)`

Example (shape):

```json
{
  "agent": "OptionsSpecialist",
  "action": "PROCEED",
  "reasoning": "ATM IV is elevated…",
  "inputs": {"atm_iv": 0.41, "iv_regime": "ELEVATED", "skew_ratio": 1.12},
  "outputs": {"confidence": 0.68, "opportunity": "Put credit spread 21-45d"},
  "timestamp": "2026-04-17T05:01:30.460415"
}
```

### 4.1) OptionsSpecialist prompt (reference)

Source: `agents/agents/options_specialist.py` `SYSTEM_PROMPT`.

The agent is constrained to strict JSON and must cite context fields like `atm_iv`, `skew_ratio`, etc.

---

### 5) Sentiment analyst (LLM)

**Agent**: `agents/agents/sentiment_analyst.py`

**Input**
- `news_feed` (recent)
- processed news rollups/digests (when present)

**Output**
- writes:
  - `aggregate_sentiment`
  - `sentiment_themes`, `sentiment_tail_risks`
  - `sentiment_decision`, `sentiment_confidence`
- appends a `ReasoningEntry`

Example (shape):

```json
{
  "agent": "SentimentAnalyst",
  "action": "HOLD",
  "reasoning": "Headlines mixed; weighted sentiment low…",
  "inputs": {"headline_count": 8, "lookback_hours": 6},
  "outputs": {"aggregate_sentiment": 0.08, "weighted_sentiment": 0.11, "confidence": 0.44}
}
```

### 5.1) SentimentAnalyst prompt (reference)

Source: `agents/agents/sentiment_analyst.py` `SYSTEM_PROMPT`.

The agent is constrained to strict JSON and must ground the output in provided headlines.

---

### 6) Strategist (LLM → structured proposal)

**Agent**: `agents/agents/strategist.py`

**Input**
- deterministic chain analytics output:
  - `near_atm_contracts` (list of allowed OCC symbols)
  - IV metrics
- risk sizing:
  - NAV, position cap dollars
- sentiment + movement context

**Output**
- either:
  - sets `pending_proposal = None` (HOLD)
  - or sets `pending_proposal = TradeProposal(...)` (PROCEED)

**Hard expiry guard**:
Even if a bad symbol slips in, Strategist performs a final validation:
- if any leg expiry < today → proposal is rejected and downgraded to HOLD

Example `pending_proposal` (shape):

```json
{
  "strategy_name": "Bull Call Spread",
  "legs": [
    {"symbol":"NFLX260515C00100000","right":"CALL","strike":100,"expiry":"260515","side":"BUY","qty":1},
    {"symbol":"NFLX260515C00110000","right":"CALL","strike":110,"expiry":"260515","side":"SELL","qty":1}
  ],
  "max_risk": 250,
  "target_return": 338,
  "stop_loss_pct": 0.5,
  "take_profit_pct": 0.75,
  "rationale": "…",
  "confidence": 0.62
}
```

### 6.1) Strategist prompt (reference)

Source: `agents/agents/strategist.py` `SYSTEM_PROMPT`.

Critical constraints:
- Only use OCC symbols from `near_atm_contracts`
- Output must be one strict JSON object (HOLD or PROCEED schema)

---

### 7) Risk manager + Desk head (LLM decision layers)

**Risk manager**: validates proposal vs risk limits. Can HOLD/ABORT.

**Desk head**: final synthesis. Writes:
- `FirmState.trader_decision` = `PROCEED` or `HOLD` or `ABORT`

---

### 8) Recommendation (advisory mode)

When `FirmState.trading_mode == "advisory"` and DeskHead sets `PROCEED`,
`recommend_node` parks a `Recommendation` into `FirmState.pending_recommendations`.

**Guardrail**:
- recommendations with expired legs are not added

API:
- `GET /recommendations` returns the list and adds a `pricing` block for the UI:
  - per-leg bid/ask/mid when quotes exist in `latest_greeks`
  - `missing_quotes` list when not
  - `expired`/`occ_expiry` flags for leg symbols that are expired

Example `GET /recommendations` item (shape):

```json
{
  "id": "a1b2c3d4e5f6",
  "ticker": "NFLX",
  "strategy_name": "Bull Call Spread",
  "status": "pending",
  "proposal": {"strategy_name":"…","legs":[{"symbol":"NFLX260515C00100000","side":"BUY","qty":1}],"max_risk":250,"target_return":338},
  "pricing": {
    "legs": [{"symbol":"NFLX260515C00100000","bid": 3.2, "ask": 3.4, "mid": 3.3, "expired": false}],
    "missing_quotes": []
  }
}
```

---

### 9) XAI persistence

Every agent adds a `ReasoningEntry`.

Persistence:
- default: SQLite `cache/app.sqlite3` table `xai_log`
- optional debug mirror: `XAI_JSONL=1` writes JSONL under `logs/xai/`

### 10) MLflow interactive flow (optional)

If `MLFLOW_TRACKING_URI` is set and MLflow is running:

- parent run = one cycle (ticker/trigger/trading_mode tags)
- child runs = one per agent step
- each child run logs artifacts:
  - `inputs.json`
  - `outputs.json`

This is the fastest way to visually inspect what each agent saw and produced.

