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
