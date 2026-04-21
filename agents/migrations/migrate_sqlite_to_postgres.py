"""
One-time migration: copy local SQLite stores into PostgreSQL (warehouse schema).

Usage:
  export WAREHOUSE_POSTGRES_URL="postgresql://user:pass@host:5432/dbname"
  python -m agents.migrations.migrate_sqlite_to_postgres

This script is safe to run multiple times; it uses UPSERT / ON CONFLICT patterns.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sqlite_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs).expanduser()
    if not p.is_absolute():
        p = _root() / p
    return p


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _utc_iso_from_ts_unix(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def main() -> int:
    from agents.data.warehouse import postgres as wh

    if not wh.is_postgres_enabled():
        raise SystemExit("WAREHOUSE_POSTGRES_URL (or DATABASE_URL) must be set and psycopg installed.")

    wh.ensure_schema()

    # Default SQLite locations (relative to repo root) unless user overrides via env vars.
    app_db = os.getenv("APP_DB_PATH", "cache/app.sqlite3")
    bars_db = os.getenv("BARS_CACHE_DB_PATH", "cache/daily_bars.sqlite3")
    fund_db = os.getenv("FUNDAMENTALS_DB_PATH", "cache/fundamentals.sqlite3")
    news_db = os.getenv("NEWS_PROCESSED_DB_PATH", "cache/news_processed.sqlite3")
    port_db = os.getenv("PORTFOLIO_HISTORY_DB_PATH", "cache/portfolio_series.sqlite3")

    with wh.connect() as conn:
        with conn.cursor() as cur:
            # ── app.sqlite3 (kv + xai + market_event) ───────────────────────
            p = _sqlite_path(app_db)
            if p.exists():
                s = _connect_sqlite(p)
                try:
                    # kv
                    try:
                        for k, v_json, updated_at_unix in s.execute("SELECT k, v_json, updated_at_unix FROM kv;").fetchall():
                            cur.execute(
                                """
                                INSERT INTO app_kv(k, v_json, updated_at)
                                VALUES (%s, %s::jsonb, %s)
                                ON CONFLICT(k) DO UPDATE SET
                                  v_json = EXCLUDED.v_json,
                                  updated_at = EXCLUDED.updated_at
                                """,
                                (k, v_json, _utc_iso_from_ts_unix(updated_at_unix)),
                            )
                    except Exception:
                        pass

                    # xai_log
                    try:
                        rows = s.execute(
                            "SELECT ts_iso, ticker, agent, action, reasoning, inputs_json, outputs_json, trade_id FROM xai_log;"
                        ).fetchall()
                        for ts_iso, ticker, agent, action, reasoning, inputs_json, outputs_json, trade_id in rows:
                            sym = str(ticker or "").upper().strip() or "SPY"
                            cur.execute(
                                "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                                (sym,),
                            )
                            cur.execute(
                                """
                                INSERT INTO xai_log(ts_iso, symbol, agent, action, reasoning, inputs_json, outputs_json, trade_id)
                                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                                """,
                                (
                                    ts_iso,
                                    sym,
                                    agent,
                                    action,
                                    reasoning,
                                    inputs_json or "{}",
                                    outputs_json or "{}",
                                    trade_id,
                                ),
                            )
                    except Exception:
                        pass

                    # market_event
                    try:
                        rows = s.execute("SELECT ts_unix, ticker, channel, payload_json FROM market_event;").fetchall()
                        for ts_unix, ticker, channel, payload_json in rows:
                            sym = str(ticker or "").upper().strip() or "SPY"
                            cur.execute(
                                "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                                (sym,),
                            )
                            cur.execute(
                                """
                                INSERT INTO market_event(ts_unix, symbol, channel, payload_json)
                                VALUES (%s, %s, %s, %s::jsonb)
                                """,
                                (float(ts_unix or 0.0), sym, channel or "", payload_json or "{}"),
                            )
                    except Exception:
                        pass
                finally:
                    s.close()

            # ── daily_bars.sqlite3 → ohlc_1d (via enqueue already supported) ─
            # Use the existing daily cache table if present.
            p = _sqlite_path(bars_db)
            if p.exists():
                s = _connect_sqlite(p)
                try:
                    rows = s.execute("SELECT ticker, bars_json FROM ticker_daily;").fetchall()
                    for ticker, bars_json in rows:
                        sym = str(ticker or "").upper().strip() or "SPY"
                        cur.execute(
                            "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                            (sym,),
                        )
                        try:
                            bars = json.loads(bars_json or "[]")
                        except Exception:
                            bars = []
                        # Insert day rows
                        for b in bars:
                            try:
                                ts = int(b["time"])
                                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                                cur.execute(
                                    """
                                    INSERT INTO ohlc_1d(symbol, bar_date, open, high, low, close, volume, source)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                    ON CONFLICT(symbol, bar_date) DO UPDATE SET
                                      open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                                      volume=EXCLUDED.volume, source=EXCLUDED.source, ingested_at=now()
                                    """,
                                    (
                                        sym,
                                        d,
                                        float(b["open"]),
                                        float(b["high"]),
                                        float(b["low"]),
                                        float(b["close"]),
                                        float(b["volume"]) if b.get("volume") is not None else None,
                                        "sqlite_migrate",
                                    ),
                                )
                            except Exception:
                                continue
                finally:
                    s.close()

            # ── fundamentals.sqlite3 → fundamentals_latest ───────────────────
            p = _sqlite_path(fund_db)
            if p.exists():
                s = _connect_sqlite(p)
                try:
                    rows = s.execute("SELECT ticker, payload_json, fetched_at_unix FROM stock_info;").fetchall()
                    import hashlib

                    for ticker, payload_json, fetched_at_unix in rows:
                        sym = str(ticker or "").upper().strip() or "SPY"
                        cur.execute(
                            "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                            (sym,),
                        )
                        try:
                            payload = json.loads(payload_json or "{}")
                        except Exception:
                            payload = {}
                        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
                        ft = datetime.fromtimestamp(int(fetched_at_unix or 0), tz=timezone.utc)
                        cur.execute(
                            """
                            INSERT INTO fundamentals_latest(symbol, fetched_at, payload, payload_hash)
                            VALUES (%s, %s, %s::jsonb, %s)
                            ON CONFLICT(symbol) DO UPDATE SET
                              fetched_at=EXCLUDED.fetched_at,
                              payload=EXCLUDED.payload,
                              payload_hash=EXCLUDED.payload_hash
                            """,
                            (sym, ft, raw, h),
                        )
                finally:
                    s.close()

            # ── news_processed.sqlite3 → processed_article* ──────────────────
            p = _sqlite_path(news_db)
            if p.exists():
                s = _connect_sqlite(p)
                try:
                    rows = s.execute(
                        """
                        SELECT id, headline, source, url, summary, published_at, fetched_at,
                               category, sentiment, confidence, impact_magnitude, llm_digest,
                               themes_json, tail_risks_json, original_tickers_json, affected_tickers_json,
                               llm_model, processing_time_ms
                        FROM processed_article;
                        """
                    ).fetchall()
                    for r in rows:
                        (
                            aid,
                            headline,
                            source,
                            url,
                            summary,
                            published_at,
                            fetched_at,
                            category,
                            sentiment,
                            confidence,
                            impact_magnitude,
                            llm_digest,
                            themes_json,
                            tail_risks_json,
                            original_tickers_json,
                            affected_tickers_json,
                            llm_model,
                            processing_time_ms,
                        ) = r
                        cur.execute(
                            """
                            INSERT INTO processed_article(
                              id, headline, source, url, summary, published_at, fetched_at,
                              category, sentiment, confidence, impact_magnitude,
                              llm_digest, themes_json, tail_risks_json, original_tickers_json, affected_tickers_json,
                              llm_model, processing_time_ms
                            ) VALUES (
                              %s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s,
                              %s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
                              %s,%s
                            )
                            ON CONFLICT(id) DO NOTHING
                            """,
                            (
                                aid,
                                headline or "",
                                source or "",
                                url or "",
                                summary or "",
                                published_at,
                                fetched_at,
                                category or "general",
                                float(sentiment or 0.0),
                                float(confidence or 0.0),
                                int(impact_magnitude or 1),
                                llm_digest or "",
                                themes_json or "[]",
                                tail_risks_json or "[]",
                                original_tickers_json or "[]",
                                affected_tickers_json or "[]",
                                llm_model or "",
                                int(processing_time_ms or 0),
                            ),
                        )
                    # mappings
                    rows = s.execute("SELECT article_id, ticker, role FROM processed_article_ticker;").fetchall()
                    for aid, ticker, role in rows:
                        sym = str(ticker or "").upper().strip()
                        if not sym:
                            continue
                        cur.execute(
                            "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                            (sym,),
                        )
                        cur.execute(
                            """
                            INSERT INTO processed_article_ticker(article_id, symbol, role)
                            VALUES(%s,%s,%s)
                            ON CONFLICT(article_id, symbol, role) DO NOTHING
                            """,
                            (aid, sym, role or ""),
                        )
                    # rollups
                    rows = s.execute(
                        "SELECT ticker, day_utc, article_count, avg_sentiment, avg_impact, top_themes_json, updated_at FROM ticker_news_rollup_day;"
                    ).fetchall()
                    for ticker, day_utc, n, avg_s, avg_imp, top, updated_at in rows:
                        sym = str(ticker or "").upper().strip()
                        if not sym:
                            continue
                        cur.execute(
                            "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                            (sym,),
                        )
                        cur.execute(
                            """
                            INSERT INTO ticker_news_rollup_day(symbol, day_utc, article_count, avg_sentiment, avg_impact, top_themes_json, updated_at)
                            VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)
                            ON CONFLICT(symbol, day_utc) DO UPDATE SET
                              article_count=EXCLUDED.article_count,
                              avg_sentiment=EXCLUDED.avg_sentiment,
                              avg_impact=EXCLUDED.avg_impact,
                              top_themes_json=EXCLUDED.top_themes_json,
                              updated_at=EXCLUDED.updated_at
                            """,
                            (sym, day_utc, int(n or 0), float(avg_s or 0.0), float(avg_imp or 0.0), top or "[]", updated_at),
                        )
                finally:
                    s.close()

            # ── portfolio_series.sqlite3 → portfolio_point ───────────────────
            p = _sqlite_path(port_db)
            if p.exists():
                s = _connect_sqlite(p)
                try:
                    rows = s.execute("SELECT ts, equity, delta, vega, daily_pnl, drawdown_pct FROM portfolio_point;").fetchall()
                    for ts, equity, delta, vega, daily_pnl, drawdown_pct in rows:
                        cur.execute(
                            """
                            INSERT INTO portfolio_point(ts, equity, delta, vega, daily_pnl, drawdown_pct)
                            VALUES(%s,%s,%s,%s,%s,%s)
                            """,
                            (float(ts), float(equity), float(delta), float(vega), float(daily_pnl), float(drawdown_pct)),
                        )
                finally:
                    s.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

