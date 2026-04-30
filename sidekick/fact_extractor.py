"""Automatic fact extractor: synthesise candidate long-term facts and persist them.

This module provides a light-weight extractor that attempts to call the Google
GenAI text model to extract up to a few concise third-person facts from a block
of text. If the SDK isn't available it falls back to a simple heuristic that
selects first-person sentences and rewrites them to third-person.

The extractor avoids duplicates by computing embeddings for candidate facts and
skipping insertion when a very-similar memory already exists for the user.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List

from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.embedding import embed_for_storage, cosine_top_k
from sidekick.crypto import encrypt_text

logger = logging.getLogger("sidekick.fact_extractor")
logging.basicConfig(level=logging.INFO)


def _call_genai_extract(text: str, max_facts: int = 3) -> List[str]:
    try:
        from google import genai  # type: ignore
    except Exception:
        return []
    try:
        client = genai.Client()
        model = os.environ.get("FACT_EXTRACT_MODEL", os.environ.get("MODEL", "gpt-4o-mini"))
        prompt = (
            "Extract up to {max_facts} concise, non-sensitive facts about the user "
            "from the following text. Return a JSON array of strings only. "
            "Each fact should be a single sentence in third person (e.g. 'User's home city is Jakarta').\n\nText:\n".format(max_facts=max_facts)
            + text
        )
        # Best-effort call shape; if the installed SDK uses a different call
        # signature this will raise and we fall back to heuristics.
        resp = client.generate(model=model, prompt=prompt)
        raw = getattr(resp, "text", None) or getattr(resp, "content", None) or str(resp)
        # Try to parse JSON from the response
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x]
        except Exception:
            # Try to find a JSON array substring
            m = re.search(r"\[.*\]", raw, re.S)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if x]
                except Exception:
                    pass
        # As a fallback, split lines
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return lines[:max_facts]
    except Exception:
        logger.exception("genai extraction failed")
        return []


def _heuristic_extract(text: str, max_facts: int = 3) -> List[str]:
    # Simple heuristic: split into sentences and select those containing
    # first-person tokens, then rewrite "I/my/me" -> "User/ user's".
    sents = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    facts: List[str] = []
    for s in sents:
        if len(facts) >= max_facts:
            break
        if re.search(r"\bI\b|\bI'm\b|\bmy\b|\bmine\b|\bwe\b|\bour\b", s, re.I):
            f = s.strip()
            f = re.sub(r"\bI\b", "User", f)
            f = re.sub(r"\bI'm\b", "User is", f)
            f = re.sub(r"\bmy\b", "user's", f)
            f = re.sub(r"\bmine\b", "user's", f)
            f = re.sub(r"\bwe\b", "User and their team", f)
            f = re.sub(r"\bour\b", "user's", f)
            # Ensure it ends with a period.
            if not f.endswith(('.', '!', '?')):
                f = f + '.'
            facts.append(f)
    return facts[:max_facts]


def extract_facts_from_text(text: str, max_facts: int = 3) -> List[str]:
    # Prefer LLM extraction when available; fall back to heuristics.
    facts = _call_genai_extract(text, max_facts=max_facts)
    if facts:
        return facts
    return _heuristic_extract(text, max_facts=max_facts)


def persist_fact_if_new(owner_sub: str, fact: str, importance: int = 2, dup_threshold: float = 0.92) -> bool:
    """Persist `fact` into `sidekick_memory` for `owner_sub` unless a similar
    memory already exists. Returns True if inserted.
    """
    if not fact or not owner_sub:
        return False
    emb = embed_for_storage(fact)
    if emb is None:
        logger.warning("embedding unavailable; skipping fact persistence")
        return False
    try:
        with db_connection() as conn:
            # fetch existing embeddings for owner
            rows = conn.execute(
                text("SELECT id, embedding FROM sidekick_memory WHERE owner_sub = :o"),
                {"o": owner_sub},
            ).all()
            candidates = [(dict(r._mapping)["embedding"], dict(r._mapping)) for r in rows]
            hits = cosine_top_k(emb, candidates, k=1, min_score=dup_threshold)
            if hits:
                logger.info("Skipping similar existing memory for owner=%s", owner_sub)
                return False
            cipher = encrypt_text(fact)
            conn.execute(
                text(
                    "INSERT INTO sidekick_memory (owner_sub, text_ciphertext, embedding, source_kind, importance) VALUES (:o, :ct, :emb, 'auto', :imp)"
                ),
                {"o": owner_sub, "ct": cipher, "emb": emb, "imp": int(importance)},
            )
            logger.info("Inserted new memory for owner=%s", owner_sub)
            return True
    except Exception:
        logger.exception("persist_fact_if_new failed")
        return False


def run_extraction_for_owner(owner_sub: str, max_items: int = 50) -> int:
    """Scan recent notes and chat messages for `owner_sub`, extract facts, and
    persist new ones. Returns the number of facts inserted.
    """
    inserted = 0
    try:
        with db_connection() as conn:
            # Grab recent notes and chat messages text to run extraction over.
            notes = conn.execute(
                text("SELECT title, body FROM sidekick_notes WHERE owner_sub = :o ORDER BY created_at DESC LIMIT :lim"),
                {"o": owner_sub, "lim": max_items},
            ).all()
            chats = conn.execute(
                text(
                    "SELECT m.content_ciphertext FROM sidekick_chat_messages m JOIN sidekick_chats c ON m.chat_id = c.id WHERE c.owner_sub = :o ORDER BY m.created_at DESC LIMIT :lim"
                ),
                {"o": owner_sub, "lim": max_items},
            ).all()
            texts: List[str] = []
            for n in notes:
                row = dict(n._mapping)
                t = (row.get("title") or "") + "\n" + (row.get("body") or "")
                if t.strip():
                    texts.append(t)
            # Decrypt chat messages
            from sidekick.crypto import decrypt_text

            for m in chats:
                row = dict(m._mapping)
                plain = decrypt_text(row.get("content_ciphertext"))
                if plain:
                    texts.append(plain)
    except Exception:
        logger.exception("failed to fetch owner content for extraction")
        return 0

    for txt in texts:
        facts = extract_facts_from_text(txt, max_facts=3)
        for f in facts:
            if persist_fact_if_new(owner_sub, f):
                inserted += 1
    return inserted
