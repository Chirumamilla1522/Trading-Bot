## Configuration & environment variables

Primary config source: `.env` (loaded by `agents/config.py`).
Template: `config/.env.example`

### LLM configuration

- `OPENROUTER_ENABLED` (default: `false`)
  - `false`: local llama.cpp only
  - `true`: allow OpenRouter routing (and optional fallback logic)

- Local OpenAI-compatible server (llama.cpp, MLX-LM shim, vLLM, etc.):
  - `LLAMA_LOCAL_BASE_URL` (default `http://127.0.0.1:8080/v1`) — fallback when no per-role override
  - `LLAMA_LOCAL_MODEL` (default `local`)
  - `LLAMA_LOCAL_API_KEY` (default `not-needed`)
  - `LLAMA_LOCAL_TIMEOUT_S` (recommend 120–300 for heavier prompts)
  - `LLAMA_LOCAL_PRIMARY` / `LLAMA_LOCAL_FALLBACK`

- **Per-agent routing** (local only; ignored when `OPENROUTER_ENABLED=true`):
  - Full URL: `LLAMA_LOCAL_BASE_URL_<ROLE>` where `<ROLE>` is uppercase with underscores, e.g.
    - `LLAMA_LOCAL_BASE_URL_OPTIONS_SPECIALIST`
    - `LLAMA_LOCAL_BASE_URL_SENTIMENT_ANALYST`
    - `LLAMA_LOCAL_BASE_URL_STRATEGIST`
    - `LLAMA_LOCAL_BASE_URL_RISK_MANAGER`
    - `LLAMA_LOCAL_BASE_URL_DESK_HEAD`
    - `LLAMA_LOCAL_BASE_URL_BULL_RESEARCHER`
    - `LLAMA_LOCAL_BASE_URL_BEAR_RESEARCHER`
    - `LLAMA_LOCAL_BASE_URL_ADVERSARIAL_JUDGE`
  - Or host + ports: `LLAMA_LOCAL_HOST` + `LLAMA_LOCAL_PORT_<SAME_ROLE_KEYS>` (e.g. `LLAMA_LOCAL_PORT_STRATEGIST=8003`)

- OpenRouter (only relevant if enabled):
  - `OPENROUTER_API_KEY`
  - `OPENROUTER_MAX_TOKENS`
  - `OPENROUTER_DATA_COLLECTION` / `OPENROUTER_EXTRA_BODY`
  - 429 retry knobs (see `config/.env.example`)

### Broker / market data

- Alpaca keys:
  - `ALPACA_API_KEY`
  - `ALPACA_SECRET_KEY`
  - `ALPACA_BASE_URL` (paper vs live)
  - `ALPACA_DATA_URL` (default `https://data.alpaca.markets` for stock bars)
  - `ALPACA_STOCK_DATA_FEED` — `iex` (default; avoids “subscription does not permit querying recent SIP data” on free data), or `sip` / `delayed_sip` if your account includes that feed.

- Alpha Vantage (optional):
  - `ALPHA_VANTAGE_API_KEY` — [alphavantage.co](https://www.alphavantage.co/support/#api-key); free tier is **rate-limited** and quotes are often **~15 min delayed** (not a live tape).

- Stock price chart (`/bars`):
  - Tries **Alpaca** → Alpha Vantage (if key) → yfinance → optional synthetic fake OHLC (dev only).

- Live quote strip (`/quote`) — top bar price / bid / ask:
  - Same order: **Alpaca** → Alpha Vantage → Yahoo. The UI polls about every **2s**; the server refreshes cached quotes every **2s** so the display tracks near–real-time **last** (still REST polling, not a WebSocket tick stream).
  - `CHART_SYNTHETIC_FALLBACK`: when `true`, allow synthetic bars even with real data keys; when unset and Alpaca or Alpha Vantage keys exist, synthetic is **off** so the UI never shows misleading fake prices.

### Feature flags

- `ENABLE_NEWS_FEED`
- `ENABLE_SYNTHETIC_NEWS`
- `ENABLE_ADVERSARIAL_DEBATE`
- `ENABLE_SEMANTIC_CACHE`
- `DEBATE_ROUNDS`

### Risk limits

- `MAX_DAILY_DRAWDOWN`
- `MAX_POSITION_PCT`

### Persistence

- `FIRM_STATE_FILE` (optional override for `agents/_firm_state.json` location)
- `XAI_LOG_DIR` (default `logs/xai`)

