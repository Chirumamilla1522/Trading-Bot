# Agent transcript (shared, repo-synced)

This file is a lightweight, human-written log of important agent work so you can
sync context between laptops via git.

Guidelines:
- Keep entries short (10–30 lines).
- Do NOT paste secrets (API keys, passwords, account ids, full `.env` values).
- Prefer: what changed + where + why + next steps.

---

## 2026-04-21/22 — Postgres + strategist defaults + quote persistence

### Goals
- Default option **structure** to **SINGLE (1-leg)** unless user selects otherwise.
- Make quote “price movements” persist every time we pull a fresh quote.
- Move/standardize persistence onto Postgres (warehouse) where configured.
- Expose Postgres over LAN for multi-device access; verify data is present in tables.

### Key decisions / defaults
- **Option structures default**: `SINGLE` if user selection is empty/missing.
- **Postgres is primary persistence** when `WAREHOUSE_POSTGRES_URL` is set (and `psycopg` available).
- Keep price movements in Postgres tables (`quote_snapshot` and `market_event`).

### Code changes (high-signal)
- **Strategist default 1-leg**:
  - `agents/state.py`: `allowed_option_structures` default → `["SINGLE"]`.
  - `agents/agents/strategist.py`: missing/empty structures treated as `["SINGLE"]`; regime guide filtered by user rights/structures.
  - `agents/api_server.py` + `ui/*`: UI default selects SINGLE; empty payload → SINGLE.
- **Persist quote pulls**:
  - `agents/data/equity_snapshot.py`: on fresh quote pulls (single + batch), append:
    - `market_event` (channel=`quote`, payload=full quote dict)
    - `quote_snapshot` via warehouse enqueue (when enabled)
  - `agents/api_server.py`: removed duplicate quote enqueue in `_ws_quote_push_task` to avoid double-writes.
- **Warehouse schema extension**:
  - `agents/data/warehouse/schema.sql`: added Postgres tables for perception + universe research caches.

### Ops / verification notes
- Postgres LAN enable requires:
  - `postgresql.conf`: `listen_addresses='*'`
  - `pg_hba.conf`: allow LAN CIDR (e.g. `192.168.86.0/24`) with `scram-sha-256`
  - restart: `brew services restart postgresql@16`
- Verified data presence via `psql`:
  - `quote_snapshot` rows present (thousands)
  - `ohlc_1d` daily bars present

### Next steps
- If desired: add API endpoint to query recent quote history per ticker (for UI/debug).

