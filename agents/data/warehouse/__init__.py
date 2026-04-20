"""
Durable warehouse layer (PostgreSQL + optional QuestDB).

- **SQLite** (`cache/*.sqlite3`): default L1 for UI — lowest latency, same process.
- **PostgreSQL** (`WAREHOUSE_POSTGRES_URL`): append/upsert everything we pull; analytics & backup.
- **QuestDB** (optional): tick / high-frequency — use HTTP ILP or TCP; see ``questdb_line.py``.

Writes are **non-blocking** (background queue) so clicks stay fast.
"""
from agents.data.warehouse.postgres import (
    ensure_schema,
    is_postgres_enabled,
    start_warehouse_writer,
    stop_warehouse_writer,
)

__all__ = [
    "ensure_schema",
    "is_postgres_enabled",
    "start_warehouse_writer",
    "stop_warehouse_writer",
]
