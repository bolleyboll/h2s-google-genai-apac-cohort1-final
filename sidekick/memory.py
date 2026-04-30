"""Long-term memory: encrypted facts about the user, retrieved by vector similarity.

Three ADK tools the agent can call:

* :func:`remember` — persist a durable fact (e.g. "user's standup is 10 am Mon/Wed/Fri").
* :func:`recall` — semantic search over the user's stored memories.
* :func:`forget` — remove a memory by id.

Plus :func:`top_relevant_memories`, used by ``main.py`` to inject the most-relevant
memories into each ``/run`` so the agent gets them passively even when it doesn't
explicitly call ``recall``. Memory text is encrypted at rest with the same Fernet
key as chat history; embeddings are stored as raw little-endian float32 bytes
(we don't bother with ``pgvector`` — for any single user we fetch all rows and
score in-process, which is fast for thousands of vectors).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from sqlalchemy import text

from sidekick.crypto import decrypt_text, encrypt_text
from sidekick.db import db_connection
from sidekick.embedding import (
    cosine_top_k,
    embed_for_query,
    embed_for_storage,
)
from sidekick.embedding import decode_vec

logger = logging.getLogger(__name__)


def _owner(tool_context: ToolContext) -> str:
    return tool_context.user_id


def _active_chat_id(tool_context: ToolContext) -> Optional[int]:
    try:
        raw = tool_context.state.get("active_chat_id")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def remember(
    text_value: str,
    importance: int = 2,
    *,
    tool_context: ToolContext,
) -> str:
    """Persist a durable fact about the user so future turns can recall it.

    Call this when the user states something the assistant should remember
    long-term: schedules, preferences, ongoing projects, recurring people,
    goals, constraints. Don't memorise every passing detail — only facts the
    user would expect you to recall in later sessions.

    Args:
        text_value (str): The fact, written as a single sentence in third person
            (e.g. "User's standup is 10 am Mon/Wed/Fri", "User is migrating
            their cache layer to Redis with Aman").
        importance (int): 1 (mildly useful) to 5 (critical). Default 2.
            Higher-importance memories are surfaced first when ties happen.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON ``{"saved": True, "id": <int>}`` on success, or ``{"error": ...}``.
    """
    owner = _owner(tool_context)
    chat_id = _active_chat_id(tool_context)
    s = (text_value or "").strip()
    if not s:
        return json.dumps({"error": "empty_text"})
    imp = max(1, min(int(importance or 2), 5))
    blob = embed_for_storage(s)
    if blob is None:
        return json.dumps(
            {
                "error": "embedding_unavailable",
                "message": "Embedding model not reachable; memory not saved.",
            }
        )
    cipher = encrypt_text(s)
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO sidekick_memory "
                    "(owner_sub, text_ciphertext, embedding, source_kind, "
                    " source_chat_id, importance) "
                    "VALUES (:o, :ct, :emb, 'chat', :cid, :imp) "
                    "RETURNING id"
                ),
                {
                    "o": owner,
                    "ct": cipher,
                    "emb": blob,
                    "cid": chat_id,
                    "imp": imp,
                },
            ).first()
    except Exception as e:
        logger.exception("remember failed")
        return json.dumps({"error": "database", "message": str(e)})
    return json.dumps({"saved": True, "id": int(row[0]), "importance": imp})


def recall(
    query: str,
    limit: int = 5,
    *,
    tool_context: ToolContext,
) -> str:
    """Search the user's saved memories by semantic similarity to ``query``.

    Use this when you suspect there's a stored fact that would help with the
    current turn but it isn't in the auto-injected context (or you want more
    than the default top hits). The returned memories are decrypted in-process
    and ranked by cosine similarity.

    Args:
        query (str): What you're looking for (a phrase or short question).
        limit (int): Max memories to return (clamped to 1–25). Default 5.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON ``{"memories": [{id, text, importance, score, created_at}, ...]}``
        ordered by descending score, or ``{"memories": []}`` when nothing
        relevant exists.
    """
    owner = _owner(tool_context)
    q = (query or "").strip()
    if not q:
        return json.dumps({"memories": []})
    k = max(1, min(int(limit or 5), 25))
    q_blob = embed_for_query(q)
    if q_blob is None:
        return json.dumps(
            {"error": "embedding_unavailable", "memories": []}
        )
    rows = _fetch_owner_memories(owner)
    candidates = [(r["embedding"], r) for r in rows]
    hits = cosine_top_k(q_blob, candidates, k=k, min_score=0.45)
    out = []
    for score, r in hits:
        plain = decrypt_text(r["text_ciphertext"])
        if plain is None:
            continue
        out.append(
            {
                "id": r["id"],
                "text": plain,
                "importance": r["importance"],
                "score": round(score, 3),
                "created_at": r["created_at"],
            }
        )
    if hits:
        _bump_use(owner, [r["id"] for _, r in hits])
    return json.dumps({"memories": out}, default=str)


def forget(memory_id: int, *, tool_context: ToolContext) -> str:
    """Delete a memory by id (only if it belongs to the current user).

    Args:
        memory_id (int): Memory primary key returned by :func:`remember` or :func:`recall`.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON ``{"deleted": True, "id": ...}`` or ``{"error": "not_found"}``.
    """
    owner = _owner(tool_context)
    try:
        mid = int(memory_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid_id"})
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "DELETE FROM sidekick_memory "
                    "WHERE id = :id AND owner_sub = :o RETURNING id"
                ),
                {"id": mid, "o": owner},
            ).first()
    except Exception as e:
        logger.exception("forget failed")
        return json.dumps({"error": "database", "message": str(e)})
    if r is None:
        return json.dumps({"error": "not_found", "id": mid})
    return json.dumps({"deleted": True, "id": mid})


def top_relevant_memories(
    owner_sub: str,
    query_text: str,
    *,
    k: int = 4,
    min_score: float = 0.55,
) -> list[dict[str, Any]]:
    """Return the user's most-relevant memories for ``query_text`` (proxy-side helper).

    Used by ``main._inject_memory_preamble`` so the agent passively sees relevant
    memories on every turn without having to call ``recall`` itself.

    Args:
        owner_sub (str): Authenticated user id.
        query_text (str): The user's incoming message to use as the query.
        k (int): Maximum memories to return.
        min_score (float): Score floor (slightly higher than the agent-tool
            default so passive injection only surfaces strong matches).

    Returns:
        list[dict[str, Any]]: ``[{text, importance, score}, ...]`` newest-first
        among ties; empty when nothing matches or embeddings are disabled.
    """
    s = (query_text or "").strip()
    if not s or not owner_sub:
        return []
    q_blob = embed_for_query(s)
    if q_blob is None:
        return []
    # First, fetch stored memories and score in-process (existing behaviour).
    rows = _fetch_owner_memories(owner_sub)
    out: list[dict[str, Any]] = []
    if rows:
        hits = cosine_top_k(q_blob, [(r["embedding"], r) for r in rows], k=k, min_score=min_score)
        for score, r in hits:
            plain = decrypt_text(r["text_ciphertext"])
            if plain is None:
                continue
            out.append({
                "text": plain,
                "importance": r["importance"],
                "score": round(score, 3),
            })
        if hits:
            _bump_use(owner_sub, [r["id"] for _, r in hits])

    # Optionally, include relevant notes (RAG) when pgvector is enabled and
    # `sidekick_notes.embedding`/`embedding_vector` are populated. We attempt a
    # best-effort DB-side search, falling back to in-process scoring using the
    # stored embedding bytes if available.
    try:
        if os.environ.get("USE_PGVECTOR", "").lower() in ("1", "true", "yes"):
            notes = _query_notes_by_vector(owner_sub, q_blob, k)
        else:
            notes = []
    except Exception:
        notes = []

    # Score any returned notes (or fallback candidates) and append to results.
    for score, row in notes:
        # score may be None when the DB returned rows; ensure we use a numeric score
        s = round(score, 3) if score is not None else None
        text_parts = []
        if row.get("title"):
            text_parts.append(row["title"])
        if row.get("body"):
            text_parts.append(row["body"])
        combined = "\n".join(text_parts).strip()
        if not combined:
            continue
        out.append({"text": combined, "importance": 1, "score": s})

    return out


def _query_notes_by_vector(owner_sub: str, q_blob: bytes, k: int) -> list[tuple[Optional[float], dict]]:
    """Best-effort query of `sidekick_notes` using pgvector if available.

    Returns a list of (score, row_dict). When DB-side scoring isn't possible we
    fall back to fetching candidate rows with embeddings and score them in
    process using the stored embedding bytes.
    """
    if not q_blob:
        return []
    try:
        with db_connection() as conn:
            # If embedding_vector exists, build a vector literal and use the
            # pgvector distance operator to sort nearest neighbours.
            has_vec = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sidekick_notes' AND column_name='embedding_vector'"
                )
            ).first()
            if has_vec:
                # materialise query vector into a literal e.g. [0.1,0.2,...]
                import numpy as np

                qarr = np.frombuffer(q_blob, dtype=np.float32)
                vec_lit = "[" + ",".join(str(float(x)) for x in qarr.tolist()) + "]"
                rows = conn.execute(
                    text(
                        f"SELECT id, title, body, embedding FROM sidekick_notes WHERE owner_sub = :o AND embedding_vector IS NOT NULL ORDER BY embedding_vector <-> '{vec_lit}'::vector LIMIT :k"
                    ),
                    {"o": owner_sub, "k": k},
                ).all()
                out = []
                for r in rows:
                    row = dict(r._mapping)
                    # Compute cosine score from stored bytes for a familiar scale.
                    score = None
                    if row.get("embedding"):
                        try:
                            v = decode_vec(row["embedding"])
                            q = np.frombuffer(q_blob, dtype=np.float32)
                            if v.shape == q.shape:
                                score = float(float(np.dot(q, v)))
                        except Exception:
                            score = None
                    out.append((score, row))
                return out
            # Fallback: pull candidate notes with non-null embedding and score in-process
            rows = conn.execute(
                text(
                    "SELECT id, title, body, embedding FROM sidekick_notes WHERE owner_sub = :o AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 5000"
                ),
                {"o": owner_sub},
            ).all()
            candidates = [(dict(r._mapping)["embedding"], dict(r._mapping)) for r in rows]
    except Exception:
        return []
    # Score in-process
    hits = cosine_top_k(q_blob, candidates, k=k, min_score=0.45)
    out = []
    for score, r in hits:
        out.append((score, r))
    return out


# ----- internal helpers ---------------------------------------------------------


def _fetch_owner_memories(owner_sub: str) -> list[dict[str, Any]]:
    """Pull all of ``owner_sub``'s memory rows for in-process scoring."""
    try:
        with db_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, text_ciphertext, embedding, importance, created_at "
                    "FROM sidekick_memory WHERE owner_sub = :o "
                    "ORDER BY created_at DESC LIMIT 5000"
                ),
                {"o": owner_sub},
            ).all()
    except Exception:
        logger.exception("memory fetch failed for owner=%s", owner_sub)
        return []
    return [dict(r._mapping) for r in rows]


def _bump_use(owner_sub: str, ids: list[int]) -> None:
    """Update ``use_count`` and ``last_used_at`` for memories we just surfaced.

    Best-effort — never raises.
    """
    if not ids:
        return
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "UPDATE sidekick_memory "
                    "SET use_count = use_count + 1, last_used_at = NOW() "
                    "WHERE owner_sub = :o AND id = ANY(:ids)"
                ),
                {"o": owner_sub, "ids": ids},
            )
    except Exception:
        logger.exception("bump_use failed")
