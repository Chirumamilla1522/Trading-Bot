## Data flow: from APIs to recommendations

This document explains the end‑to‑end flow of data in **Atlas — Agentic Trading**:
how we pull market/news data, how it becomes structured state, and how Tier‑3 produces a
recommendation (advisory) or execution (autopilot).

If you want node-by-node contracts, read `docs/AGENTS.md`. If you want overall architecture,
read `docs/ARCHITECTURE.md`.

---

### High-level picture

```mermaid
flowchart LR
  UI[UI\nui/index.html + ui/src/main.js] -->|REST| API[FastAPI\nagents/api_server.py]
  UI -->|WS /ws/market| API

  API -->|background loops| DATA[Market + News ingestion]
  DATA -->|writes| STATE[FirmState (in-memory)\nagents/state.py]
  API -->|trigger| G[LangGraph Tier-3\nagents/graph.py]
  G -->|mutates| STATE

  STATE -->|persist| SQLITE[(SQLite\ncache/app.sqlite3)]
  API -->|optional tracking| MLF[MLflow\nmlflow server]

  API -->|advisory| RECS[Recommendations\npending approvals]
  API -->|autopilot| EMS[Broker / EMS\nAlpaca paper]
```

Key idea:
- **Background loops continuously enrich `FirmState`.**
- **Tier‑3 runs a LangGraph pass when triggered and appends an audit trail + recommendation.**

---

### Sources of truth

- **Runtime truth**: in‑memory `FirmState` owned by `agents/api_server.py`
- **Durable state**: SQLite `cache/app.sqlite3` (`kv` snapshot + append‑only XAI log)
- **UI truth for “selected ticker”**: UI `activeTicker` (prevents background drift)

---

### Step 1 — Pulling market data (quotes, bars, option chain)

#### Price movements (Tier‑1 “MovementTracker”)

There are **two** related “price” paths in the system:

- **Quotes (UI / execution)**: last/bid/ask + day % for a symbol right now.
- **Movement signals (agent context)**: a lightweight interpretation of intraday structure:
  percent change, EMA momentum, volume expansion, and a composite movement score.

Movement signals are produced by the Tier‑1 MovementTracker and written into `FirmState`,
so every Tier‑3 agent can reference the same “tape-like” summary without re-pulling bars.

**Files**
- MovementTracker logic: `agents/agents/movement_tracker.py`
- Tier loop that writes fields: `agents/tiers.py` (`_movement_tracker_loop`)
- Non-news bias shaping: `agents/desk_context.py` (`update_market_bias_score`)

**What gets written to `FirmState`**
- `price_change_pct`: \((last - prev_close) / prev_close\)
- `momentum`: \((EMA9 - EMA21) / EMA21\)
- `vol_ratio`: current volume / 10-day average volume
- `movement_signal`: composite score in \([-1, +1]\)
- `movement_anomaly`: True when any component exceeds thresholds
- `market_bias_score`: non-news directional bias derived from the movement fields

**Snippet: Tier‑1 writes movement fields (simplified)**

```python
# agents/tiers.py
signals = run_movement_tracker(firm_state.ticker, firm_state.underlying_price or None)
firm_state.price_change_pct = signals["price_change_pct"]
firm_state.momentum         = signals["momentum"]
firm_state.vol_ratio        = signals["vol_ratio"]
firm_state.movement_signal  = signals["movement_signal"]
firm_state.movement_anomaly = signals["anomaly"]
update_market_bias_score(firm_state)  # agents/desk_context.py
```

#### Quotes

- UI calls:
  - `GET /quote/{ticker}` for the active symbol
  - `GET /scanner/quotes` for the scanner’s 1 Hz quote-only refresh
  - `GET /quotes/benchmarks` for indices/benchmarks

- Backend uses `agents/data/equity_snapshot.py`:
  - Tries Alpaca for normal tickers
  - Falls back to Alpha Vantage / yfinance when needed
  - For caret indices like `^GSPC/^IXIC/^DJI`, Alpaca is not valid → **yfinance fallback**

**Code pointers**
- **API endpoints**: `agents/api_server.py`
  - `GET /quote/{ticker}` (active ticker quote)
  - `GET /scanner` + `GET /scanner/quotes` (scanner rows + quote-only refresh)
  - `GET /quotes/benchmarks` (benchmarks + index section)
- **Quote resolution**: `agents/data/equity_snapshot.py`
  - `fetch_stock_quote(...)` (single symbol)
  - `fetch_stock_quotes_batch(...)` (batch for scanner/benchmarks)

**Snippet: batch quote fetch + index fallback (caret symbols)**

```python
# agents/data/equity_snapshot.py (fetch_stock_quotes_batch)
index_need = [u for u in need if u.startswith("^")]
alpaca_need = [u for u in need if not u.startswith("^")]

# ... Alpaca batch snapshots over alpaca_need ...

# yfinance fallback for indices and missing symbols
for u in (index_need + [x for x in alpaca_need if x not in out]):
    yf = _quote_from_yfinance(u)
    if yf:
        out[u] = yf
```

#### Bars (price history for charts)

- UI calls `GET /bars/{ticker}?timeframe=...`
- Backend serves bars from cached/persisted stores (SQLite/Postgres depending on config)

**Code pointers**
- UI: `ui/src/main.js` (`loadTickerBars(...)`)
- API: `agents/api_server.py` (`GET /bars/{ticker}`)

#### Option chain (for optionable tickers only)

- UI calls `GET /options/{ticker}` on-demand when the user switches tickers
- Backend filters contracts (expiry >= today, DTE cap, strike band) before returning

Note:
- Indices like `^GSPC/^IXIC/^DJI` are **quotes-only** in this app; they do not have an Alpaca options chain.

**Code pointers**
- UI: `ui/src/main.js`
  - `switchTicker(...)`
  - `fetchOptionsChain(...)` (skips caret indices)
- API: `agents/api_server.py` (`GET /options/{ticker}`)
- Filtering helpers: `agents/data/options_chain_filter.py`

---

### Step 2 — Pulling news and making it structured

#### News ingestion

Background task in `agents/api_server.py`:
- `_news_task()` consumes `unified_news_stream(...)` from `agents/data/news_feed.py`
- Each `NewsItem` is appended to `firm_state.news_feed` and also pushed to the in-memory
  `NewsPriorityQueue` (for Tier‑2/Tier‑3 consumers).

**Code pointers**
- Background task: `agents/api_server.py` (`_news_task`)
- Stream + parsing: `agents/data/news_feed.py` (`unified_news_stream`)
- Backlog queue: `agents/data/news_priority_queue.py` (`NewsPriorityQueue`)

**Snippet: news stream consumption**

```python
# agents/api_server.py (inside _news_task)
async for item in unified_news_stream(_current_tickers, _portfolio_tickers):
    firm_state.news_feed.append(item)
    get_queue().push(item)  # NewsPriorityQueue
```

#### Where tickers on news come from

In `agents/data/news_feed.py` each `NewsItem` can have:
- **`tickers`**: provided by the source API (Benzinga “stocks” list / Yahoo “relatedTickers”)
- **`mentioned_tickers`**: extracted by our regex/entity extraction from headline + summary

**Code pointers**
- Benzinga parsing: `agents/data/news_feed.py` (`_fetch_benzinga_tier`, `_fetch_benzinga_general`)
- Yahoo parsing: `agents/data/news_feed.py` (`_fetch_yf_tier`, `_parse_yf_article`)
- Mention extraction: `agents/data/news_feed.py` (`_extract_ticker_mentions`)

#### Universe filter

This repo supports a restricted “desk universe” (e.g. SPX/Nasdaq/Dow + a shortlist of single names).
News ingestion applies a filter so the UI and downstream agents only see headlines related to the
configured universe tickers.

**Code pointers**
- Restriction knobs: `.env` (`SCANNER_TICKERS`, `BENCHMARK_TICKERS`, `RESTRICT_UNIVERSE`)
- Defaults + alias mapping: `agents/data/sp500.py`
- News filter: `agents/data/news_feed.py` (`_universe_intersects(...)`)
- API backstop filter: `agents/api_server.py` (`GET /state`, `GET /news`)

---

### Step 3 — Tier model (T1 / T2 / T3)

#### Tier 1 (always on, lightweight)

- Movement tracker / sentiment monitor update small signals continuously.
- These signals can be used as triggers for Tier‑3.

#### Structured news sentiment (Tier‑1 “SentimentMonitor”)

SentimentMonitor is Tier‑1, but it **does not read raw headlines directly**. Instead it consumes
Tier‑2 *processed* articles (structured fields) and outputs one desk-level score.

**Files**
- Monitor logic: `agents/sentiment_monitor_llm.py`
- Tier loop wiring: `agents/tiers.py` (`_sentiment_monitor_loop`)
- News timing regime: `agents/desk_context.py` (`update_news_timing_from_feed`)

**Inputs (structured rows)**
From SQLite (`agents/data/news_processed_db.py`) plus an in-memory buffer in `agents/data/news_processor.py`:
- `sentiment`, `confidence`
- `impact_magnitude` (1–5)
- `category`, `themes`, `llm_digest`
- `published_at` (recency weight)

**Outputs written to `FirmState`**
- `sentiment_monitor_score` (desk score \([-1,+1]\))
- `sentiment_monitor_confidence`
- `sentiment_monitor_reasoning`
- `sentiment_monitor_source` (`llm_structured` or deterministic fallback)
- also updates `news_timing_regime` (`fresh/moderate/stale/none`)

**Snippet: deterministic structured blend (fallback)**

```python
# agents/sentiment_monitor_llm.py
w = confidence * (impact_magnitude / 5.0) * recency_weight(published_at)
desk_sentiment = sum(sentiment * w) / sum(w)
```

#### Tier 2 (periodic enrichment)

- NewsProcessor drains `NewsPriorityQueue` (FinBERT/heuristic priority), runs LLM enrichment,
  and persists processed outputs in SQLite.
- Other refreshers periodically update fundamentals / snapshots.

#### Tier 3 (triggered LangGraph)

Tier‑3 is one LangGraph pass (`agents/graph.py`) that produces:
- a final decision (`trader_decision`)
- optionally a `pending_proposal`
- optionally a recommendation (advisory mode)
- an append-only reasoning trail

---

### Step 4 — Tier‑3 graph execution (decision → recommendation)

Graph wiring lives in `agents/graph.py`. The typical order is:

1. `ingest_data` (deterministic features: IV surface/regime + structured digests)
2. `options_specialist` (LLM: IV/skew/term structure)
3. `sentiment_analyst` (LLM: headline synthesis)
4. `bull_researcher` (LLM: bull case + conviction)
5. `bear_researcher` (LLM: bear case + conviction)
6. `strategist` (LLM: produces a concrete `TradeProposal`)
7. `risk_manager` (hard limits + risk checks)
8. optional `adversarial_debate` (extra judge round)
9. `desk_head` (final supervisor decision)
10. `recommend` (advisory) or `trader` (autopilot)
11. `xai_log` (persist reasoning)

Mode behavior:
- **advisory**: produces a `Recommendation` item for user approval
- **autopilot**: routes to `trader` to submit orders via EMS/broker

AdversarialDebate policy:
- **advisory**: skipped (faster; user approves)
- **autopilot**: included (extra safety gate)

#### How movements + news get “passed through” all agents

Everything flows through the shared `FirmState` (`agents/state.py`). The system avoids
passing huge blobs between agents by using:

- **small numeric fields** for movement/structure signals
- **curated lists** for news (raw feed) and Tier‑2 digests (structured summaries)

Here is how the key signals propagate.

**A) Price/structure path**

1. Tier‑1 MovementTracker writes:
   - `price_change_pct`, `momentum`, `vol_ratio`, `movement_signal`, `movement_anomaly`
2. `agents/desk_context.update_market_bias_score(...)` writes:
   - `market_bias_score`
3. Tier‑3 `ingest_data` reads those fields and writes:
   - `market_regime`, IV analytics, and updates spot price
4. Downstream agents read the same shared fields:
   - `bull_researcher` / `bear_researcher`: uses `price_change_pct`, `momentum`, `vol_ratio`, `market_regime`, `market_bias_score`
   - `strategist`: uses regime + volatility analytics + confidence context
   - `desk_head`: uses the final combined context to decide PROCEED/HOLD/ABORT

**B) News path**

1. News ingestion (`agents/data/news_feed.py`) yields `NewsItem` rows:
   - with `tickers` (source-provided) and `mentioned_tickers` (our extraction)
2. API server `_news_task` appends to:
   - `firm_state.news_feed`
   - and pushes to `NewsPriorityQueue` (backlog for processors)
3. Tier‑2 NewsProcessor consumes the queue and persists **structured rows**:
   - sentiment/confidence/impact/digests/themes (SQLite)
4. Tier‑1 SentimentMonitor reads the structured rows and writes:
   - `sentiment_monitor_score` (+ reasoning/confidence)
   - and updates `news_timing_regime` via `desk_context`
5. Tier‑3:
   - `ingest_data` attaches `tier3_structured_digests` (compact “LLM-ready” digests)
   - `sentiment_analyst` reads:
     - recent items (window + top‑K) and `sentiment_monitor_score`
     - writes `aggregate_sentiment`, `sentiment_themes`, `sentiment_tail_risks`
6. The rest of the Tier‑3 agents consume the synthesized outputs (not the raw feeds):
   - `bull_researcher` / `bear_researcher` use `aggregate_sentiment`, themes, timing regime
   - `strategist` uses the full context to propose a trade

In short:
- **MovementTracker → market_bias_score → ingest_data/regime → strategist/desk_head**
- **NewsFeed → NewsPriorityQueue → NewsProcessor(SQLite) → SentimentMonitor → SentimentAnalyst → strategist/desk_head**

**Code pointers**
- Graph wiring + routing: `agents/graph.py`
  - `build_graph()`
  - routing fns: `should_run_pipeline`, `should_debate`, `should_trade`
- Agents:
  - `agents/agents/options_specialist.py`
  - `agents/agents/sentiment_analyst.py`
  - `agents/agents/strategist.py`
  - `agents/agents/risk_manager.py`
  - `agents/agents/adversarial_debate.py`
  - `agents/agents/desk_head.py`
  - `agents/agents/trader.py`

**Snippet: mode routing (advisory vs autopilot)**

```python
# agents/graph.py
def should_trade(state: FirmState):
    if state.trading_mode == "autopilot":
        return "trader"
    return "recommend"
```

---

### Step 5 — How the UI sees the result

The UI renders from:

- `GET /state`
  - includes `FirmState` snapshot
  - includes `agent_runtime` status
  - includes filtered `news_feed` for fallback rendering

- `GET /recommendations`
  - advisory approval queue (approve/dismiss)

- `GET /reasoning_log`
  - tail of reasoning rows for “Agent Activity”

UI also uses WebSocket `/ws/market` for incremental updates (quotes, urgent news deltas).

**Code pointers**
- UI polling/WS: `ui/src/main.js`
  - `pollState`, `pollNewsFeed`, `pollReasoningLog`, `pollRecommendations`
  - websocket handler (`_connectWS`), delta merges
- API:
  - `agents/api_server.py` (`GET /state`, `GET /news`, `GET /reasoning_log`, `GET /recommendations`)

---

### Step 6 — Observability / audit (why you can trust the output)

- **XAI reasoning log**:
  - in-memory `FirmState.reasoning_log`
  - persisted via `xai_log` node (SQLite + JSONL optional)

- **MLflow (optional)**:
  - enabled via `MLFLOW_TRACKING_URI`
  - parent run per Tier‑3 cycle
  - child runs per agent step
  - child runs per LLM call (`kind=llm_call`) with `prompt.json` + `response.json`

**Code pointers**
- MLflow helpers: `agents/tracking/mlflow_tracing.py`
  - `start_cycle_run`, `end_cycle_run`, `log_agent_step`, `log_llm_call`
- LLM wrapper (instrumented): `agents/llm_retry.py` (`invoke_llm`)

---

### Practical debugging checklist

- “UI looks stale”:
  - check `GET /state` and `GET /recommendations`
  - confirm `/ws/market` is connected or REST polls are succeeding

- “Indices not showing / invalid symbol”:
  - caret indices (`^GSPC/^IXIC/^DJI`) are quotes-only and must be fetched via yfinance fallback
  - option chain endpoints (`/options/{ticker}`) won’t work for caret indices

- “News shows unrelated tickers”:
  - confirm `SCANNER_TICKERS` / `BENCHMARK_TICKERS` are set
  - restart backend to clear old in-memory `news_feed`

