"""
DB Explorer — schema + data introspection for dashboards.

Supports:
- SQLite files used by the app (cache/*.sqlite3, cache/perception.sqlite3, logs/research/universe.db)
- Optional PostgreSQL warehouse (WAREHOUSE_POSTGRES_URL) when enabled

All functions are read-only and safe for UI dashboards.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


DbSourceKind = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class DbSource:
    key: str
    kind: DbSourceKind
    label: str
    # sqlite: absolute path; postgres: DSN (redacted in response)
    target: str


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _abs(p: Path) -> Path:
    if p.is_absolute():
        return p
    return (_root() / p).resolve()


def _sqlite_sources() -> list[DbSource]:
    root = _root()
    out: list[DbSource] = []
    out.append(DbSource("sqlite_daily_bars", "sqlite", "SQLite: daily_bars", str(_abs(Path("cache/daily_bars.sqlite3")))))
    out.append(DbSource("sqlite_fundamentals", "sqlite", "SQLite: fundamentals", str(_abs(Path("cache/fundamentals.sqlite3")))))
    out.append(DbSource("sqlite_news_processed", "sqlite", "SQLite: news_processed", str(_abs(Path("cache/news_processed.sqlite3")))))
    out.append(DbSource("sqlite_perception", "sqlite", "SQLite: perception", str(_abs(Path("cache/perception.sqlite3")))))
    out.append(DbSource("sqlite_research_universe", "sqlite", "SQLite: research universe", str((root / "logs" / "research" / "universe.db").resolve())))
    return out


def _postgres_source() -> DbSource | None:
    dsn = (os.getenv("WAREHOUSE_POSTGRES_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return None
    return DbSource("postgres_warehouse", "postgres", "Postgres: warehouse", dsn)


def list_sources() -> list[DbSource]:
    out = _sqlite_sources()
    pg = _postgres_source()
    if pg:
        out.append(pg)
    return out


def _sqlite_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=2.5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # read-only pragmas (best-effort)
    try:
        con.execute("PRAGMA query_only = ON;")
    except Exception:
        pass
    return con


def _sqlite_table_list(con: sqlite3.Connection) -> list[str]:
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r[0] for r in cur.fetchall()]


def _sqlite_table_info(con: sqlite3.Connection, table: str) -> dict[str, Any]:
    cols = []
    for row in con.execute(f"PRAGMA table_info({table})").fetchall():
        cols.append(
            {
                "cid": row["cid"],
                "name": row["name"],
                "type": row["type"],
                "notnull": bool(row["notnull"]),
                "dflt_value": row["dflt_value"],
                "pk": bool(row["pk"]),
            }
        )
    fks = []
    try:
        for row in con.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            fks.append(
                {
                    "id": row["id"],
                    "seq": row["seq"],
                    "table": row["table"],
                    "from": row["from"],
                    "to": row["to"],
                    "on_update": row["on_update"],
                    "on_delete": row["on_delete"],
                    "match": row["match"],
                }
            )
    except Exception:
        pass
    idx = []
    try:
        for row in con.execute(f"PRAGMA index_list({table})").fetchall():
            idx.append(
                {
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                    "origin": row["origin"],
                    "partial": bool(row.get("partial", 0)) if isinstance(row, sqlite3.Row) else False,
                }
            )
    except Exception:
        pass
    return {"columns": cols, "foreign_keys": fks, "indexes": idx}


def _sqlite_count(con: sqlite3.Connection, table: str) -> int:
    try:
        row = con.execute(f"SELECT COUNT(1) AS n FROM {table}").fetchone()
        return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])
    except Exception:
        return -1


def _sqlite_rows(con: sqlite3.Connection, table: str, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    lim = max(1, min(500, int(limit)))
    off = max(0, int(offset))
    cur = con.execute(f"SELECT * FROM {table} LIMIT ? OFFSET ?", (lim, off))
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({k: r[k] for k in r.keys()})
    return out


def _sqlite_relationship_graph(con: sqlite3.Connection) -> dict[str, Any]:
    # edges: table(from_col) -> ref_table(to_col)
    nodes = _sqlite_table_list(con)
    edges: list[dict[str, Any]] = []
    for t in nodes:
        try:
            for fk in con.execute(f"PRAGMA foreign_key_list({t})").fetchall():
                edges.append(
                    {
                        "from_table": t,
                        "from_col": fk["from"],
                        "to_table": fk["table"],
                        "to_col": fk["to"],
                        "on_delete": fk["on_delete"],
                        "on_update": fk["on_update"],
                    }
                )
        except Exception:
            continue
    return {"nodes": nodes, "edges": edges}


def _postgres_redact_dsn(dsn: str) -> str:
    # simple redact for UI display (do not leak creds)
    if "://" not in dsn:
        return "postgres://***"
    scheme, rest = dsn.split("://", 1)
    if "@" in rest:
        _, host = rest.split("@", 1)
        return f"{scheme}://***@{host}"
    return f"{scheme}://***"


def _pg_connect():
    import psycopg

    dsn = (os.getenv("WAREHOUSE_POSTGRES_URL") or os.getenv("DATABASE_URL") or "").strip()
    return psycopg.connect(dsn, autocommit=True)


def _pg_tables(cur) -> list[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type='BASE TABLE'
        ORDER BY table_name
        """
    )
    return [r[0] for r in cur.fetchall()]


def _pg_columns(cur, table: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    cols = []
    for name, dt, nullable, dflt in cur.fetchall():
        cols.append(
            {
                "name": name,
                "type": dt,
                "notnull": (str(nullable).upper() != "YES"),
                "dflt_value": dflt,
            }
        )
    return cols


def _pg_fks(cur, table: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
          tc.constraint_name,
          kcu.column_name,
          ccu.table_name AS foreign_table_name,
          ccu.column_name AS foreign_column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = %s
        """,
        (table,),
    )
    out = []
    for cname, col, ft, fcol in cur.fetchall():
        out.append({"name": cname, "from": col, "to_table": ft, "to_col": fcol})
    return out


def _pg_count(cur, table: str) -> int:
    try:
        cur.execute(f"SELECT COUNT(1) FROM {table}")
        return int(cur.fetchone()[0])
    except Exception:
        return -1


def _pg_rows(cur, table: str, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    lim = max(1, min(500, int(limit)))
    off = max(0, int(offset))
    cur.execute(f"SELECT * FROM {table} LIMIT %s OFFSET %s", (lim, off))
    cols = [d.name for d in cur.description]
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        out.append({cols[i]: row[i] for i in range(len(cols))})
    return out


def _pg_relationship_graph(cur) -> dict[str, Any]:
    nodes = _pg_tables(cur)
    edges: list[dict[str, Any]] = []
    for t in nodes:
        for fk in _pg_fks(cur, t):
            edges.append(
                {
                    "from_table": t,
                    "from_col": fk["from"],
                    "to_table": fk["to_table"],
                    "to_col": fk["to_col"],
                    "name": fk["name"],
                }
            )
    return {"nodes": nodes, "edges": edges}


def resolve_source(key: str) -> DbSource:
    for s in list_sources():
        if s.key == key:
            return s
    raise KeyError(key)


def list_tables(source_key: str) -> dict[str, Any]:
    src = resolve_source(source_key)
    if src.kind == "sqlite":
        p = Path(src.target)
        exists = p.exists()
        if not exists:
            return {"source": src.key, "kind": src.kind, "exists": False, "path": str(p), "tables": []}
        con = _sqlite_connect(str(p))
        try:
            tables = _sqlite_table_list(con)
            return {"source": src.key, "kind": src.kind, "exists": True, "path": str(p), "tables": tables}
        finally:
            con.close()

    # postgres
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            tables = _pg_tables(cur)
            return {"source": src.key, "kind": src.kind, "dsn": _postgres_redact_dsn(src.target), "tables": tables}


def table_schema(source_key: str, table: str) -> dict[str, Any]:
    src = resolve_source(source_key)
    if src.kind == "sqlite":
        p = Path(src.target)
        con = _sqlite_connect(str(p))
        try:
            info = _sqlite_table_info(con, table)
            return {"source": src.key, "kind": src.kind, "table": table, **info}
        finally:
            con.close()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            return {
                "source": src.key,
                "kind": src.kind,
                "table": table,
                "columns": _pg_columns(cur, table),
                "foreign_keys": _pg_fks(cur, table),
            }


def table_rows(source_key: str, table: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    src = resolve_source(source_key)
    if src.kind == "sqlite":
        p = Path(src.target)
        con = _sqlite_connect(str(p))
        try:
            n = _sqlite_count(con, table)
            rows = _sqlite_rows(con, table, limit=limit, offset=offset)
            return {"source": src.key, "kind": src.kind, "table": table, "count": n, "limit": limit, "offset": offset, "rows": rows}
        finally:
            con.close()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            n = _pg_count(cur, table)
            rows = _pg_rows(cur, table, limit=limit, offset=offset)
            return {"source": src.key, "kind": src.kind, "table": table, "count": n, "limit": limit, "offset": offset, "rows": rows}


def relationship_graph(source_key: str) -> dict[str, Any]:
    src = resolve_source(source_key)
    if src.kind == "sqlite":
        p = Path(src.target)
        con = _sqlite_connect(str(p))
        try:
            g = _sqlite_relationship_graph(con)
            return {"source": src.key, "kind": src.kind, "path": str(p), **g}
        finally:
            con.close()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            g = _pg_relationship_graph(cur)
            return {"source": src.key, "kind": src.kind, "dsn": _postgres_redact_dsn(src.target), **g}

