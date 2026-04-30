"""Simple background worker to run periodic memory/backfill tasks.

This is intentionally lightweight: it exposes a `run_once()` function that the
app's process manager or a cron-like scheduler can call. It currently calls
`backfill_embeddings.backfill()` to keep `sidekick_notes` populated with
embeddings, and can be extended to extract facts and call `remember(...)`.

Usage (development):
    python -m sidekick.memory_worker --once

In production you might run it as a separate process or schedule it with
systemd/cron/Cloud Tasks.
"""
from __future__ import annotations

import argparse
import logging

from sidekick import backfill_embeddings
from sidekick import fact_extractor

logger = logging.getLogger("sidekick.memory_worker")
logging.basicConfig(level=logging.INFO)


def run_once(batch: int = 50, sleep: float = 0.2) -> None:
    logger.info("Starting memory worker: backfill embeddings (batch=%s)", batch)
    backfill_embeddings.backfill(batch=batch, sleep=sleep)
    logger.info("Backfill complete; running automated fact extraction for users")
    # Discover active users from sidekick_chats and run extraction per owner.
    try:
        from sidekick.db import get_engine
        from sqlalchemy import text

        eng = get_engine()
        with eng.begin() as conn:
            rows = conn.execute(text("SELECT DISTINCT owner_sub FROM sidekick_chats")).all()
            owners = [r[0] for r in rows if r and r[0]]
    except Exception:
        logger.exception("failed to list owners for extraction")
        owners = []

    total_inserted = 0
    for o in owners:
        try:
            n = fact_extractor.run_extraction_for_owner(o, max_items=50)
            total_inserted += n
            logger.info("Inserted %d facts for owner=%s", n, o)
        except Exception:
            logger.exception("extraction failed for owner=%s", o)

    logger.info("Memory worker run_once complete: total facts inserted=%d", total_inserted)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        run_once(batch=args.batch, sleep=args.sleep)
    else:
        # For safety don't run an infinite loop by default; users can wrap this
        # file in supervisord or systemd to schedule regular runs.
        run_once(batch=args.batch, sleep=args.sleep)
