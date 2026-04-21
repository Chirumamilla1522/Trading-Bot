"""
SQLite store for AI-processed news in an LLM-friendly format.

Goal: make retrieval cheap (indexed by ticker/time) and prompts small (digest text).

Default location: ``cache/news_processed.sqlite3`` under repo root.
Override:
  - NEWS_PROCESSED_DB_PATH=/absolute/or/relative/path.sqlite3
  - AGENTIC_DATA_DIR=~/.agentic_trading (uses <dir>/news_processed.sqlite3)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log_lock = threading.Lock()
_init_lock = threading.Lock()
_inited = False


def _pg_enabled() -> bool:
    try:
        from agents.data.warehouse import postgres as wh

        return wh.is_postgres_enabled()
    except Exception:
        return False


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configured_path() -> Path:
    explicit = (os.getenv("NEWS_PROCESSED_DB_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    agentic = (os.getenv("AGENTIC_DATA_DIR") or "").strip()
    if agentic:
        return Path(agentic).expanduser() / "news_processed.sqlite3"
    return Path("cache/news_processed.sqlite3")


def _path() -> Path:
    p = _configured_path()
    if not p.is_absolute():
        p = _root() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect_raw() -> sqlite3.Connection:
    con = sqlite3.connect(str(_path()), timeout=5.0, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


def _ensure() -> None:
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        if _pg_enabled():
            try:
                from agents.data.warehouse import postgres as wh

                wh.ensure_schema()
            except Exception:
                pass
            _inited = True
            return
        con = _connect_raw()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_article (
                  id TEXT PRIMARY KEY,
                  headline TEXT NOT NULL,
                  source TEXT,
                  url TEXT,
                  summary TEXT,
                  published_at TEXT NOT NULL,
                  fetched_at TEXT NOT NULL,
                  category TEXT,
                  sentiment REAL,
                  confidence REAL,
                  impact_magnitude INTEGER,
                  llm_digest TEXT,
                  themes_json TEXT,
                  tail_risks_json TEXT,
                  original_tickers_json TEXT,
                  affected_tickers_json TEXT,
                  llm_model TEXT,
                  processing_time_ms INTEGER
                );
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_article_ticker (
                  article_id TEXT NOT NULL,
                  ticker TEXT NOT NULL,
                  role TEXT NOT NULL,   -- "original" | "affected"
                  PRIMARY KEY (article_id, ticker, role)
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_pa_pub ON processed_article(published_at DESC);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pat_ticker ON processed_article_ticker(ticker);")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_pat_ticker_article ON processed_article_ticker(ticker, article_id);"
            )
            # Per-ticker daily rollups (cheap “meta-context” for 8–30 day window).
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS ticker_news_rollup_day (
                  ticker TEXT NOT NULL,
                  day_utc TEXT NOT NULL,               -- YYYY-MM-DD
                  article_count INTEGER NOT NULL,
                  avg_sentiment REAL NOT NULL,
                  avg_impact REAL NOT NULL,
                  top_themes_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (ticker, day_utc)
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_rollup_ticker_day ON ticker_news_rollup_day(ticker, day_utc DESC);")
            con.commit()
        finally:
            con.close()
        _inited = True


def upsert_processed_article(payload: dict[str, Any]) -> None:
    """
    Insert/update one processed article row.
    `payload` is typically ProcessedArticle.model_dump(mode="json").
    """
    _ensure()
    aid = str(payload.get("id") or "").strip()
    if not aid:
        return
    # Normalize time strings.
    pub = payload.get("published_at") or ""
    fet = payload.get("fetched_at") or ""
    if isinstance(pub, datetime):
        pub = pub.astimezone(timezone.utc).isoformat()
    if isinstance(fet, datetime):
        fet = fet.astimezone(timezone.utc).isoformat()

    tickers_orig = payload.get("original_tickers") or []
    tickers_aff = []
    for af in payload.get("affected_tickers") or []:
        t = (af.get("ticker") or "").upper().strip()
        if t and t not in tickers_aff:
            tickers_aff.append(t)

    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    # Ensure all instruments exist (for FK constraints).
                    for sym in set([str(x).upper().strip() for x in (tickers_orig or [])] + tickers_aff):
                        if sym:
                            cur.execute(
                                "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                                (sym,),
                            )
                    # Article row.
                    cur.execute(
                        """
                        INSERT INTO processed_article(
                          id, headline, source, url, summary,
                          published_at, fetched_at,
                          category, sentiment, confidence, impact_magnitude,
                          llm_digest, themes_json, tail_risks_json,
                          original_tickers_json, affected_tickers_json,
                          llm_model, processing_time_ms
                        ) VALUES (
                          %s,%s,%s,%s,%s,
                          %s,%s,
                          %s,%s,%s,%s,
                          %s,%s::jsonb,%s::jsonb,
                          %s::jsonb,%s::jsonb,
                          %s,%s
                        )
                        ON CONFLICT(id) DO UPDATE SET
                          headline=excluded.headline,
                          source=excluded.source,
                          url=excluded.url,
                          summary=excluded.summary,
                          published_at=excluded.published_at,
                          fetched_at=excluded.fetched_at,
                          category=excluded.category,
                          sentiment=excluded.sentiment,
                          confidence=excluded.confidence,
                          impact_magnitude=excluded.impact_magnitude,
                          llm_digest=excluded.llm_digest,
                          themes_json=excluded.themes_json,
                          tail_risks_json=excluded.tail_risks_json,
                          original_tickers_json=excluded.original_tickers_json,
                          affected_tickers_json=excluded.affected_tickers_json,
                          llm_model=excluded.llm_model,
                          processing_time_ms=excluded.processing_time_ms
                        """,
                        (
                            aid,
                            payload.get("headline") or "",
                            payload.get("source") or "",
                            payload.get("url") or "",
                            payload.get("summary") or "",
                            str(pub),
                            str(fet),
                            payload.get("category") or "general",
                            float(payload.get("sentiment") or 0.0),
                            float(payload.get("confidence") or 0.0),
                            int(payload.get("impact_magnitude") or 1),
                            payload.get("llm_digest") or "",
                            json.dumps(payload.get("themes") or [], ensure_ascii=False),
                            json.dumps(payload.get("tail_risks") or [], ensure_ascii=False),
                            json.dumps(tickers_orig or [], ensure_ascii=False),
                            json.dumps(payload.get("affected_tickers") or [], ensure_ascii=False),
                            payload.get("llm_model") or "",
                            int(payload.get("processing_time_ms") or 0),
                        ),
                    )
                    # Mapping rows (idempotent).
                    for t in [str(x).upper().strip() for x in (tickers_orig or []) if str(x).strip()]:
                        cur.execute(
                            """
                            INSERT INTO processed_article_ticker(article_id, symbol, role)
                            VALUES(%s,%s,%s)
                            ON CONFLICT(article_id, symbol, role) DO NOTHING
                            """,
                            (aid, t, "original"),
                        )
                    for t in tickers_aff:
                        cur.execute(
                            """
                            INSERT INTO processed_article_ticker(article_id, symbol, role)
                            VALUES(%s,%s,%s)
                            ON CONFLICT(article_id, symbol, role) DO NOTHING
                            """,
                            (aid, t, "affected"),
                        )
            return
        except Exception:
            # fall back to sqlite path
            pass

    con = _connect_raw()
    try:
        con.execute(
            """
            INSERT INTO processed_article(
              id, headline, source, url, summary,
              published_at, fetched_at,
              category, sentiment, confidence, impact_magnitude,
              llm_digest, themes_json, tail_risks_json,
              original_tickers_json, affected_tickers_json,
              llm_model, processing_time_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              headline=excluded.headline,
              source=excluded.source,
              url=excluded.url,
              summary=excluded.summary,
              published_at=excluded.published_at,
              fetched_at=excluded.fetched_at,
              category=excluded.category,
              sentiment=excluded.sentiment,
              confidence=excluded.confidence,
              impact_magnitude=excluded.impact_magnitude,
              llm_digest=excluded.llm_digest,
              themes_json=excluded.themes_json,
              tail_risks_json=excluded.tail_risks_json,
              original_tickers_json=excluded.original_tickers_json,
              affected_tickers_json=excluded.affected_tickers_json,
              llm_model=excluded.llm_model,
              processing_time_ms=excluded.processing_time_ms
            """,
            (
                aid,
                payload.get("headline") or "",
                payload.get("source") or "",
                payload.get("url") or "",
                payload.get("summary") or "",
                str(pub),
                str(fet),
                payload.get("category") or "general",
                float(payload.get("sentiment") or 0.0),
                float(payload.get("confidence") or 0.0),
                int(payload.get("impact_magnitude") or 1),
                payload.get("llm_digest") or "",
                json.dumps(payload.get("themes") or [], ensure_ascii=False),
                json.dumps(payload.get("tail_risks") or [], ensure_ascii=False),
                json.dumps(tickers_orig or [], ensure_ascii=False),
                json.dumps(payload.get("affected_tickers") or [], ensure_ascii=False),
                payload.get("llm_model") or "",
                int(payload.get("processing_time_ms") or 0),
            ),
        )

        # Maintain ticker mapping rows (idempotent).
        for t in [str(x).upper().strip() for x in (tickers_orig or []) if str(x).strip()]:
            con.execute(
                "INSERT OR IGNORE INTO processed_article_ticker(article_id, ticker, role) VALUES(?,?,?)",
                (aid, t, "original"),
            )
        for t in tickers_aff:
            con.execute(
                "INSERT OR IGNORE INTO processed_article_ticker(article_id, ticker, role) VALUES(?,?,?)",
                (aid, t, "affected"),
            )
        con.commit()
    finally:
        con.close()


def purge_older_than(days: int = 90) -> int:
    """Delete processed articles older than `days` (and orphaned mappings). Returns deleted article count."""
    _ensure()
    days = max(1, int(days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_s = cutoff.isoformat()
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM processed_article WHERE published_at < %s;", (cutoff_s,))
                    ids = [r[0] for r in cur.fetchall()]
                    if not ids:
                        return 0
                    cur.execute("DELETE FROM processed_article WHERE published_at < %s;", (cutoff_s,))
            return len(ids)
        except Exception:
            return 0
    con = _connect_raw()
    try:
        # Find ids to delete (so we can clean mapping table).
        ids = [r[0] for r in con.execute("SELECT id FROM processed_article WHERE published_at < ?", (cutoff_s,)).fetchall()]
        if not ids:
            return 0
        # Delete mappings first.
        for aid in ids:
            con.execute("DELETE FROM processed_article_ticker WHERE article_id = ?", (aid,))
        # Delete articles.
        con.execute("DELETE FROM processed_article WHERE published_at < ?", (cutoff_s,))
        con.commit()
        return len(ids)
    finally:
        con.close()


def rebuild_rollups_for_ticker(ticker: str, lookback_days: int = 90) -> int:
    """
    Recompute daily rollups for `ticker` for the last `lookback_days`.
    Returns number of day rows upserted.
    """
    _ensure()
    t = ticker.upper().strip()
    lookback_days = max(1, int(lookback_days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_s = cutoff.isoformat()
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT a.published_at, a.sentiment, a.impact_magnitude, a.themes_json
                        FROM processed_article_ticker m
                        JOIN processed_article a ON a.id = m.article_id
                        WHERE m.symbol = %s
                          AND a.published_at >= %s
                        ORDER BY a.published_at ASC
                        """,
                        (t, cutoff_s),
                    )
                    rows = cur.fetchall()
            if not rows:
                return 0

            by_day: dict[str, list[tuple[float, float, list[str]]]] = {}
            for pub_dt, sent, imp_mag, themes_json in rows:
                try:
                    day = str(pub_dt)[:10]
                except Exception:
                    continue
                try:
                    s = float(sent or 0.0)
                except Exception:
                    s = 0.0
                try:
                    imp = float(imp_mag or 1)
                except Exception:
                    imp = 1.0
                try:
                    th = themes_json if isinstance(themes_json, list) else json.loads(themes_json) if themes_json else []
                    if not isinstance(th, list):
                        th = []
                    th = [str(x).strip() for x in th if str(x).strip()]
                except Exception:
                    th = []
                by_day.setdefault(day, []).append((s, imp, th))

            now_s = datetime.now(timezone.utc).isoformat()
            upserted = 0
            from agents.data.warehouse import postgres as wh2

            with wh2.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO instrument(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
                        (t,),
                    )
                    for day, vals in by_day.items():
                        n = len(vals)
                        if n <= 0:
                            continue
                        avg_s = sum(v[0] for v in vals) / n
                        avg_imp = sum(v[1] for v in vals) / n
                        freq: dict[str, int] = {}
                        for _s, _i, th in vals:
                            for tag in th[:6]:
                                freq[tag] = freq.get(tag, 0) + 1
                        top = [k for k, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:6]]
                        cur.execute(
                            """
                            INSERT INTO ticker_news_rollup_day(symbol, day_utc, article_count, avg_sentiment, avg_impact, top_themes_json, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                            ON CONFLICT(symbol, day_utc) DO UPDATE SET
                              article_count=EXCLUDED.article_count,
                              avg_sentiment=EXCLUDED.avg_sentiment,
                              avg_impact=EXCLUDED.avg_impact,
                              top_themes_json=EXCLUDED.top_themes_json,
                              updated_at=EXCLUDED.updated_at
                            """,
                            (t, day, int(n), float(avg_s), float(avg_imp), json.dumps(top, ensure_ascii=False), now_s),
                        )
                        upserted += 1
            return upserted
        except Exception:
            return 0

    con = _connect_raw()
    try:
        rows = con.execute(
            """
            SELECT a.published_at, a.sentiment, a.impact_magnitude, a.themes_json
            FROM processed_article_ticker m
            JOIN processed_article a ON a.id = m.article_id
            WHERE m.ticker = ?
              AND a.published_at >= ?
            ORDER BY a.published_at ASC
            """,
            (t, cutoff_s),
        ).fetchall()
        if not rows:
            return 0

        by_day: dict[str, list[tuple[float, float, list[str]]]] = {}
        for pub_s, sent, imp_mag, themes_json in rows:
            try:
                day = str(pub_s)[:10]
            except Exception:
                continue
            try:
                s = float(sent or 0.0)
            except Exception:
                s = 0.0
            try:
                imp = float(imp_mag or 1)
            except Exception:
                imp = 1.0
            try:
                th = json.loads(themes_json) if themes_json else []
                if not isinstance(th, list):
                    th = []
                th = [str(x).strip() for x in th if str(x).strip()]
            except Exception:
                th = []
            by_day.setdefault(day, []).append((s, imp, th))

        now_s = datetime.now(timezone.utc).isoformat()
        upserted = 0
        for day, vals in by_day.items():
            n = len(vals)
            if n <= 0:
                continue
            avg_s = sum(v[0] for v in vals) / n
            avg_imp = sum(v[1] for v in vals) / n
            # Top themes: frequency.
            freq: dict[str, int] = {}
            for _s, _i, th in vals:
                for tag in th[:6]:
                    freq[tag] = freq.get(tag, 0) + 1
            top = [k for k, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:6]]
            con.execute(
                """
                INSERT INTO ticker_news_rollup_day(ticker, day_utc, article_count, avg_sentiment, avg_impact, top_themes_json, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(ticker, day_utc) DO UPDATE SET
                  article_count=excluded.article_count,
                  avg_sentiment=excluded.avg_sentiment,
                  avg_impact=excluded.avg_impact,
                  top_themes_json=excluded.top_themes_json,
                  updated_at=excluded.updated_at
                """,
                (t, day, int(n), float(avg_s), float(avg_imp), json.dumps(top, ensure_ascii=False), now_s),
            )
            upserted += 1
        con.commit()
        return upserted
    finally:
        con.close()


def get_tiered_llm_context(
    ticker: str,
    *,
    now: datetime | None = None,
    recent_hours: int = 72,
    days_detail: int = 7,
    days_rollup: int = 30,
    limit_recent: int = 8,
    limit_week: int = 10,
) -> dict[str, Any]:
    """
    Decaying lookback strategy:
      - last `recent_hours`: primary context (digests)
      - 2..`days_detail`: secondary context (digests)
      - 8..`days_rollup`: meta context (daily rollups only)
    Returns structured dict suitable for prompt injection.
    """
    _ensure()
    t = ticker.upper().strip()
    now_dt = now or datetime.now(timezone.utc)
    recent_cut = now_dt - timedelta(hours=max(1, int(recent_hours)))
    week_cut = now_dt - timedelta(days=max(1, int(days_detail)))
    roll_cut = now_dt - timedelta(days=max(1, int(days_rollup)))

    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT a.llm_digest
                        FROM processed_article_ticker m
                        JOIN processed_article a ON a.id = m.article_id
                        WHERE m.symbol = %s
                          AND a.published_at >= %s
                          AND a.llm_digest IS NOT NULL AND a.llm_digest != ''
                        ORDER BY a.published_at DESC
                        LIMIT %s
                        """,
                        (t, recent_cut.isoformat(), int(limit_recent)),
                    )
                    recent = cur.fetchall()
                    cur.execute(
                        """
                        SELECT a.llm_digest
                        FROM processed_article_ticker m
                        JOIN processed_article a ON a.id = m.article_id
                        WHERE m.symbol = %s
                          AND a.published_at >= %s
                          AND a.published_at < %s
                          AND a.llm_digest IS NOT NULL AND a.llm_digest != ''
                        ORDER BY a.published_at DESC
                        LIMIT %s
                        """,
                        (t, week_cut.isoformat(), recent_cut.isoformat(), int(limit_week)),
                    )
                    week = cur.fetchall()
                    cur.execute(
                        """
                        SELECT day_utc, article_count, avg_sentiment, avg_impact, top_themes_json
                        FROM ticker_news_rollup_day
                        WHERE symbol = %s
                          AND day_utc >= %s
                          AND day_utc < %s
                        ORDER BY day_utc DESC
                        """,
                        (t, roll_cut.date().isoformat(), week_cut.date().isoformat()),
                    )
                    roll = cur.fetchall()

            roll_lines: list[str] = []
            for day, n, avg_s, avg_imp, top_th in roll:
                try:
                    themes = top_th if isinstance(top_th, list) else json.loads(top_th) if top_th else []
                    if not isinstance(themes, list):
                        themes = []
                except Exception:
                    themes = []
                th = ",".join([str(x) for x in themes[:5] if str(x)])
                roll_lines.append(
                    f\"{day} n={int(n)} avg_sent={float(avg_s):+.2f} avg_imp={float(avg_imp):.2f}\"
                    + (f\" th:{th}\" if th else \"\")
                )

            return {
                \"ticker\": t,
                \"window\": {
                    \"recent_hours\": int(recent_hours),
                    \"days_detail\": int(days_detail),
                    \"days_rollup\": int(days_rollup),
                },
                \"recent\": [r[0] for r in recent if r and r[0]],
                \"week\": [r[0] for r in week if r and r[0]],
                \"rollup\": roll_lines,
            }
        except Exception:
            pass

    con = _connect_raw()
    try:
        # Recent digests (0..recent_cut)
        recent = con.execute(
            """
            SELECT a.llm_digest
            FROM processed_article_ticker m
            JOIN processed_article a ON a.id = m.article_id
            WHERE m.ticker = ?
              AND a.published_at >= ?
              AND a.llm_digest IS NOT NULL AND a.llm_digest != ''
            ORDER BY a.published_at DESC
            LIMIT ?
            """,
            (t, recent_cut.isoformat(), int(limit_recent)),
        ).fetchall()

        # Week digests (week_cut..recent_cut)
        week = con.execute(
            """
            SELECT a.llm_digest
            FROM processed_article_ticker m
            JOIN processed_article a ON a.id = m.article_id
            WHERE m.ticker = ?
              AND a.published_at >= ?
              AND a.published_at < ?
              AND a.llm_digest IS NOT NULL AND a.llm_digest != ''
            ORDER BY a.published_at DESC
            LIMIT ?
            """,
            (t, week_cut.isoformat(), recent_cut.isoformat(), int(limit_week)),
        ).fetchall()

        # Rollups (roll_cut..week_cut) — one line per day.
        roll = con.execute(
            """
            SELECT day_utc, article_count, avg_sentiment, avg_impact, top_themes_json
            FROM ticker_news_rollup_day
            WHERE ticker = ?
              AND day_utc >= ?
              AND day_utc < ?
            ORDER BY day_utc DESC
            """,
            (t, roll_cut.date().isoformat(), week_cut.date().isoformat()),
        ).fetchall()

        roll_lines: list[str] = []
        for day, n, avg_s, avg_imp, top_th in roll:
            try:
                themes = json.loads(top_th) if top_th else []
                if not isinstance(themes, list):
                    themes = []
            except Exception:
                themes = []
            th = ",".join([str(x) for x in themes[:5] if str(x)])
            roll_lines.append(
                f"{day} n={int(n)} avg_sent={float(avg_s):+.2f} avg_imp={float(avg_imp):.2f}"
                + (f" th:{th}" if th else "")
            )

        return {
            "ticker": t,
            "window": {
                "recent_hours": int(recent_hours),
                "days_detail": int(days_detail),
                "days_rollup": int(days_rollup),
            },
            "recent": [r[0] for r in recent if r and r[0]],
            "week": [r[0] for r in week if r and r[0]],
            "rollup": roll_lines,
        }
    finally:
        con.close()


def get_structured_articles_for_monitor(
    ticker: str,
    *,
    hours: float = 1.0,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """
    Recent processed articles affecting `ticker` within the last `hours` window.
    Used by Tier-1 SentimentMonitor LLM synthesis (structured fields, not raw keywords).
    """
    _ensure()
    t = ticker.upper().strip()
    hours = max(0.25, float(hours))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_s = cutoff.isoformat()
    lim = max(1, int(limit))

    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT a.id, a.published_at, a.headline, a.category, a.sentiment,
                               a.confidence, a.impact_magnitude, a.llm_digest, a.themes_json,
                               m.role
                        FROM processed_article_ticker m
                        JOIN processed_article a ON a.id = m.article_id
                        WHERE m.symbol = %s
                          AND a.published_at >= %s
                        ORDER BY a.published_at DESC
                        LIMIT %s
                        """,
                        (t, cutoff_s, lim),
                    )
                    rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                (
                    aid,
                    pub_s,
                    headline,
                    category,
                    sentiment,
                    confidence,
                    impact_magnitude,
                    llm_digest,
                    themes_json,
                    role,
                ) = row
                try:
                    themes = themes_json if isinstance(themes_json, list) else json.loads(themes_json) if themes_json else []
                    if not isinstance(themes, list):
                        themes = []
                except Exception:
                    themes = []
                out.append(
                    {
                        "id": str(aid),
                        "published_at": str(pub_s),
                        "headline": headline or "",
                        "category": category or "general",
                        "sentiment": float(sentiment or 0.0),
                        "confidence": float(confidence or 0.0),
                        "impact_magnitude": int(impact_magnitude or 1),
                        "llm_digest": llm_digest or "",
                        "themes": themes,
                        "ticker_role": str(role or ""),
                    }
                )
            return out
        except Exception:
            return []

    con = _connect_raw()
    try:
        rows = con.execute(
            """
            SELECT a.id, a.published_at, a.headline, a.category, a.sentiment,
                   a.confidence, a.impact_magnitude, a.llm_digest, a.themes_json,
                   m.role
            FROM processed_article_ticker m
            JOIN processed_article a ON a.id = m.article_id
            WHERE m.ticker = ?
              AND a.published_at >= ?
            ORDER BY a.published_at DESC
            LIMIT ?
            """,
            (t, cutoff_s, lim),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            (
                aid,
                pub_s,
                headline,
                category,
                sentiment,
                confidence,
                impact_magnitude,
                llm_digest,
                themes_json,
                role,
            ) = row
            try:
                themes = json.loads(themes_json) if themes_json else []
                if not isinstance(themes, list):
                    themes = []
            except Exception:
                themes = []
            out.append(
                {
                    "id": str(aid),
                    "published_at": str(pub_s),
                    "headline": headline or "",
                    "category": category or "general",
                    "sentiment": float(sentiment or 0.0),
                    "confidence": float(confidence or 0.0),
                    "impact_magnitude": int(impact_magnitude or 1),
                    "llm_digest": llm_digest or "",
                    "themes": themes,
                    "ticker_role": str(role or ""),
                }
            )
        return out
    finally:
        con.close()


def get_llm_digests_for_ticker(ticker: str, limit: int = 12) -> list[str]:
    """Return most recent digests mentioning/affecting `ticker`."""
    _ensure()
    t = ticker.upper().strip()
    if _pg_enabled():
        try:
            from agents.data.warehouse import postgres as wh

            with wh.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT a.llm_digest
                        FROM processed_article_ticker m
                        JOIN processed_article a ON a.id = m.article_id
                        WHERE m.symbol = %s
                          AND a.llm_digest IS NOT NULL
                          AND a.llm_digest != ''
                        ORDER BY a.published_at DESC
                        LIMIT %s
                        """,
                        (t, int(limit)),
                    )
                    rows = cur.fetchall()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    con = _connect_raw()
    try:
        rows = con.execute(
            """
            SELECT a.llm_digest
            FROM processed_article_ticker m
            JOIN processed_article a ON a.id = m.article_id
            WHERE m.ticker = ?
              AND a.llm_digest IS NOT NULL
              AND a.llm_digest != ''
            ORDER BY a.published_at DESC
            LIMIT ?
            """,
            (t, int(limit)),
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    finally:
        con.close()

