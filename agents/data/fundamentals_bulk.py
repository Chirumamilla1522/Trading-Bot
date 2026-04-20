"""
Bulk-download yfinance fundamentals into SQLite (``fundamentals.sqlite3``).

Also enqueues PostgreSQL warehouse rows when ``WAREHOUSE_POSTGRES_URL`` is set
(via ``upsert_stock_info`` → ``enqueue_fundamentals``).

Usage (from project root):

  python -m agents.data.fundamentals_bulk
  python -m agents.data.fundamentals_bulk --only-missing
  python -m agents.data.fundamentals_bulk --force --max 50
  FUNDAMENTALS_BULK_DELAY_S=0.5 python -m agents.data.fundamentals_bulk --only-missing
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger(__name__)


def sync_fundamentals_universe(
    symbols: list[str] | None = None,
    *,
    delay_s: float = 0.35,
    max_symbols: int | None = None,
    only_missing: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """
    Fetch ``fetch_stock_info`` + ``upsert_stock_info`` for each symbol.

    - ``only_missing``: skip tickers that already have a row in SQLite (unless ``force``).
    - ``force``: re-fetch every ticker (ignores ``only_missing`` skip).
    """
    from agents.data.fundamentals import fetch_stock_info
    from agents.data.fundamentals_db import get_stock_info_cached, upsert_stock_info
    from agents.data.sp500 import SP500_TICKERS

    tickers = list(dict.fromkeys(symbols or SP500_TICKERS))
    if max_symbols is not None and max_symbols > 0:
        tickers = tickers[:max_symbols]

    ok = err = skipped = 0
    for i, t in enumerate(tickers):
        t = t.upper().strip()
        try:
            if not force and only_missing:
                cached, _ = get_stock_info_cached(t)
                if cached:
                    skipped += 1
                    continue
            payload = fetch_stock_info(t)
            upsert_stock_info(t, payload)
            ok += 1
            if (i + 1) % 25 == 0:
                log.info("fundamentals bulk: %d/%d stored", i + 1, len(tickers))
        except Exception as e:
            log.debug("fundamentals bulk %s: %s", t, e)
            err += 1
        time.sleep(max(0.0, delay_s))

    return {"ok": ok, "err": err, "skipped": skipped, "total": len(tickers)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Bulk fundamentals → SQLite (+ optional PG warehouse)")
    p.add_argument(
        "--delay",
        type=float,
        default=float(os.getenv("FUNDAMENTALS_BULK_DELAY_S", "0.35")),
        help="Sleep between tickers (rate limit Yahoo)",
    )
    p.add_argument("--max", type=int, default=0, help="Limit count (0 = all)")
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip symbols already present in fundamentals SQLite",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch all symbols (ignore --only-missing skips)",
    )
    args = p.parse_args(argv)

    mx = args.max if args.max > 0 else None
    stats = sync_fundamentals_universe(
        delay_s=args.delay,
        max_symbols=mx,
        only_missing=args.only_missing and not args.force,
        force=args.force,
    )
    log.info(
        "Done: stored=%d errors=%d skipped=%d total=%d",
        stats["ok"],
        stats["err"],
        stats["skipped"],
        stats["total"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
