"""Batch backfill embeddings for existing sidekick_notes.

Run as a script (local dev) to compute embeddings for notes that lack them.
It writes raw float32 little-endian bytes into `sidekick_notes.embedding` so the
existing in-process scoring can reuse them. If `USE_PGVECTOR` is enabled and
`embedding_vector` exists, the script will also attempt to populate that
column so DB-side similarity queries work.

Usage:
    python -m sidekick.backfill_embeddings --batch 50 --sleep 0.5
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import List

from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.embedding import embed_for_storage

logger = logging.getLogger("sidekick.backfill")
logging.basicConfig(level=logging.INFO)


def _rows_missing(conn, limit: int) -> List[dict]:
    rows = conn.execute(
        text(
            "SELECT id, title, body FROM sidekick_notes WHERE embedding IS NULL ORDER BY created_at ASC LIMIT :lim"
        ),
        {"lim": limit},
    ).all()
    return [dict(r._mapping) for r in rows]


def _has_embedding_vector_column(conn) -> bool:
    r = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sidekick_notes' AND column_name='embedding_vector'"
        )
    ).first()
    return r is not None


def _to_vector_literal(arr_bytes: bytes) -> str:
    # Convert raw float32 little-endian bytes back into a python list and
    # format as pgvector literal: '[x,y,z,...]'
    import numpy as np

    a = np.frombuffer(arr_bytes, dtype=np.float32)
    return "[" + ",".join(str(float(x)) for x in a.tolist()) + "]"


def backfill(batch: int = 100, sleep: float = 0.2) -> None:
    updated = 0
    with db_connection() as conn:
        has_vec = _has_embedding_vector_column(conn)
        while True:
            rows = _rows_missing(conn, batch)
            if not rows:
                logger.info("No more notes to backfill.")
                break
            for r in rows:
                nid = r["id"]
                text_src = (r.get("title") or "") + "\n" + (r.get("body") or "")
                emb = embed_for_storage(text_src)
                if emb is None:
                    logger.warning("embedding unavailable for note id=%s", nid)
                    continue
                try:
                    if has_vec:
                        vec_lit = _to_vector_literal(emb)
                        # update both columns in one statement
                        conn.execute(
                            text(
                                "UPDATE sidekick_notes SET embedding = :emb, embedding_vector = :vec::vector WHERE id = :id"
                            ),
                            {"emb": emb, "vec": vec_lit, "id": nid},
                        )
                    else:
                        conn.execute(
                            text("UPDATE sidekick_notes SET embedding = :emb WHERE id = :id"),
                            {"emb": emb, "id": nid},
                        )
                    updated += 1
                except Exception:
                    logger.exception("Failed to update embedding for note id=%s", nid)
            logger.info("Backfilled %d embeddings so far...", updated)
            time.sleep(sleep)
    logger.info("Backfill complete, total updated=%d", updated)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--sleep", type=float, default=0.2)
    args = p.parse_args()
    backfill(batch=args.batch, sleep=args.sleep)
