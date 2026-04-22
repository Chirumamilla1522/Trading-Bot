-- Agentic Trading — PostgreSQL warehouse (historical + snapshots)
-- Apply with: psql "$WAREHOUSE_POSTGRES_URL" -f schema.sql
-- Or call warehouse.postgres.ensure_schema() from Python.

-- ── Reference symbols ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS instrument (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    asset_type  TEXT DEFAULT 'equity',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Daily OHLC (Yahoo / Alpaca / merged) — one row per symbol per UTC date ───
CREATE TABLE IF NOT EXISTS ohlc_1d (
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    bar_date    DATE NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION,
    source      TEXT NOT NULL DEFAULT 'unknown',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_date)
);
CREATE INDEX IF NOT EXISTS idx_ohlc_1d_symbol_bar ON ohlc_1d (symbol, bar_date DESC);

-- ── Intraday bars (optional; high volume → consider QuestDB for tick/1m) ─────
CREATE TABLE IF NOT EXISTS ohlc_intraday (
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    bar_ts      TIMESTAMPTZ NOT NULL,
    interval_sec INTEGER NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION,
    source      TEXT NOT NULL DEFAULT 'unknown',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_ts, interval_sec)
);
CREATE INDEX IF NOT EXISTS idx_ohlc_intraday_sym_ts ON ohlc_intraday (symbol, bar_ts DESC);

-- ── Quote snapshots (NBBO / last — not tick tape; use QuestDB for ticks) ─────
CREATE TABLE IF NOT EXISTS quote_snapshot (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION,
    last        DOUBLE PRECISION,
    prev_close  DOUBLE PRECISION,
    change_pct  DOUBLE PRECISION,
    source      TEXT,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quote_snapshot_sym_time ON quote_snapshot (symbol, captured_at DESC);

-- ── Fundamentals / stock_info JSON (full API payload) ────────────────────────
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    fetched_at  TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL,
    payload_hash TEXT,
    PRIMARY KEY (symbol, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_fundamentals_symbol_time ON fundamentals_snapshot (symbol, fetched_at DESC);

-- ── Latest fundamentals pointer (fast lookup) ───────────────────────────────
CREATE TABLE IF NOT EXISTS fundamentals_latest (
    symbol      TEXT PRIMARY KEY REFERENCES instrument(symbol) ON DELETE CASCADE,
    fetched_at  TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL,
    payload_hash TEXT NOT NULL
);

-- ── News articles (dedupe by content hash) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS news_article (
    id_hash     TEXT PRIMARY KEY,
    headline    TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    source      TEXT,
    payload     JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_article (published_at DESC);

-- ── Raw fetch audit (optional debugging / replay) ───────────────────────────
CREATE TABLE IF NOT EXISTS fetch_log (
    id          BIGSERIAL PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    symbol      TEXT,
    ok          BOOLEAN NOT NULL,
    latency_ms  INTEGER,
    meta        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_created ON fetch_log (created_at DESC);

-- ── App persistence (replaces local SQLite when configured) ──────────────────
-- Key/value snapshot store (FirmState, small runtime state).
CREATE TABLE IF NOT EXISTS app_kv (
    k           TEXT PRIMARY KEY,
    v_json      JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- XAI audit log (agent reasoning). Append-only.
CREATE TABLE IF NOT EXISTS xai_log (
    id          BIGSERIAL PRIMARY KEY,
    ts_iso      TEXT NOT NULL,
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,
    reasoning   TEXT NOT NULL,
    inputs_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    outputs_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    trade_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_xai_ts ON xai_log (ts_iso);
CREATE INDEX IF NOT EXISTS idx_xai_agent ON xai_log (agent);
CREATE INDEX IF NOT EXISTS idx_xai_symbol_ts ON xai_log (symbol, ts_iso);

-- Market events (generic JSON payload, used for audit/diagnostics).
CREATE TABLE IF NOT EXISTS market_event (
    id          BIGSERIAL PRIMARY KEY,
    ts_unix     DOUBLE PRECISION NOT NULL,
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_me_symbol_ts ON market_event (symbol, ts_unix DESC);

-- ── Processed news store (LLM-friendly) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS processed_article (
    id TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    source TEXT,
    url TEXT,
    summary TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    category TEXT,
    sentiment DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    impact_magnitude INTEGER,
    llm_digest TEXT,
    themes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    tail_risks_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    original_tickers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    affected_tickers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    llm_model TEXT,
    processing_time_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pa_pub ON processed_article (published_at DESC);

CREATE TABLE IF NOT EXISTS processed_article_ticker (
    article_id TEXT NOT NULL REFERENCES processed_article(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (article_id, symbol, role)
);
CREATE INDEX IF NOT EXISTS idx_pat_symbol ON processed_article_ticker (symbol);
CREATE INDEX IF NOT EXISTS idx_pat_symbol_article ON processed_article_ticker (symbol, article_id);

CREATE TABLE IF NOT EXISTS ticker_news_rollup_day (
    symbol TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    day_utc DATE NOT NULL,
    article_count INTEGER NOT NULL,
    avg_sentiment DOUBLE PRECISION NOT NULL,
    avg_impact DOUBLE PRECISION NOT NULL,
    top_themes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, day_utc)
);
CREATE INDEX IF NOT EXISTS idx_rollup_symbol_day ON ticker_news_rollup_day (symbol, day_utc DESC);

-- ── Portfolio time series (for /portfolio_series) ───────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_point (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION NOT NULL,
    equity DOUBLE PRECISION NOT NULL,
    delta DOUBLE PRECISION NOT NULL,
    vega DOUBLE PRECISION NOT NULL,
    daily_pnl DOUBLE PRECISION NOT NULL,
    drawdown_pct DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_point_ts ON portfolio_point (ts DESC);

-- ── Perception bundles (Phase 0–2) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS perception_cycle (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL,
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perception_symbol_time ON perception_cycle (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_perception_trace ON perception_cycle (trace_id);

-- ── Universe research cache (briefs + graph edges) ──────────────────────────
CREATE TABLE IF NOT EXISTS ticker_research (
    symbol           TEXT PRIMARY KEY REFERENCES instrument(symbol) ON DELETE CASCADE,
    brief            JSONB NOT NULL,
    signal_hash      TEXT NOT NULL DEFAULT '',
    updated_at       TIMESTAMPTZ NOT NULL,
    valid_until      TIMESTAMPTZ,
    dirty            BOOLEAN NOT NULL DEFAULT FALSE,
    dirty_reasons    JSONB NOT NULL DEFAULT '[]'::jsonb,
    priority_score   DOUBLE PRECISION NOT NULL DEFAULT 0,
    portfolio_weight DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tr_dirty ON ticker_research (dirty, priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_tr_updated ON ticker_research (updated_at DESC);

CREATE TABLE IF NOT EXISTS ticker_edges (
    src     TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    dst     TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    kind    TEXT NOT NULL DEFAULT 'news_impact',
    weight  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON ticker_edges (src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON ticker_edges (dst);

CREATE TABLE IF NOT EXISTS research_eval (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL REFERENCES instrument(symbol) ON DELETE CASCADE,
    event       TEXT NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_research_eval_symbol_time ON research_eval (symbol, created_at DESC);
