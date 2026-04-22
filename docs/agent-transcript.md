## Agent transcript (shared context across devices)

This file is an **append-only running log** of high-signal agent sessions so you can
`git pull` on another laptop and immediately restore context.

- **Do not include secrets**: API keys, passwords, tokens, account IDs, private URLs.
- Prefer short summaries: **10–30 lines per session**.
- Include: goals, decisions, what changed, how to verify, and next steps.

---

## Template

### Session
- **Date**: YYYY-MM-DD
- **Title**: <short title>

### Goal
- ...

### Decisions / Defaults
- ...

### Changes made (files)
- `path`: ...

### How to verify
- ...

### Notes / Gotchas
- ...

### Next steps
- ...

---

## 2026-04-22 — Postgres + quote persistence + strategist defaults

### Goal
- Make options strategist default to **one-leg (SINGLE)** when user hasn’t selected structures.
- Persist **every pulled equity quote** to the database.
- Bring up **Postgres** locally and access it over the LAN; verify tables contain data.

### Decisions / Defaults
- **Option structures default**: `SINGLE` if empty/missing; `ALL` only when explicitly selected.
- **Quote persistence**: store quote movements in:
  - Postgres `quote_snapshot` (structured columns)
  - Postgres `market_event` (full JSON payload, `channel='quote'`)
- **Postgres network access**: enabled via `listen_addresses='*'` + LAN `pg_hba.conf` rule.

### Changes made (files)
- `agents/state.py`: `allowed_option_structures` default set to `["SINGLE"]`.
- `agents/agents/strategist.py`: builds regime strategy guide dynamically based on `allowed_option_rights` + `allowed_option_structures`.
- `agents/api_server.py`: removed duplicate Postgres quote enqueue in WS quote push task (quote writes happen in quote fetch path).
- `agents/data/equity_snapshot.py`: on real quote pulls (single + batch), append to `market_event` and enqueue Postgres `quote_snapshot`.
- `ui/index.html`, `ui/src/main.js`: default strategy types to `SINGLE` and treat empty selection as `SINGLE`.
- `agents/data/warehouse/schema.sql`: added additional tables (perception + universe research) for future Postgres-first persistence.

### How to verify
- Postgres connect:
  - `psql "postgresql://<user>@<host>:5432/agentic_trading" -c "select now();"`
- Tables exist:
  - `psql "$WAREHOUSE_POSTGRES_URL" -c "\\dt"`
- Quote rows flowing:
  - `psql "$WAREHOUSE_POSTGRES_URL" -c "select count(*) from quote_snapshot;"`
  - `psql "$WAREHOUSE_POSTGRES_URL" -c "select symbol,last,captured_at from quote_snapshot order by captured_at desc limit 10;"`

### Notes / Gotchas
- Homebrew Postgres config paths (macOS):
  - `postgresql.conf`: `/opt/homebrew/var/postgresql@16/postgresql.conf`
  - `pg_hba.conf`: `/opt/homebrew/var/postgresql@16/pg_hba.conf`
- `pg_hba.conf` errors mean the client IP/subnet is not allowed (or rule ordering is wrong).
- “password authentication failed” means the password in the connection string doesn’t match the role.

### Next steps
- (Optional) expose a small API endpoint to query recent `quote_snapshot` rows by ticker for UI/debug.

