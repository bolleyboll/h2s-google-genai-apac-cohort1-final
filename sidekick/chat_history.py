"""Decrypted chat history reads + payload helpers for ADK session replay.

The ``main`` proxy uses these to seed a fresh ADK session with the chat's prior
turns so the agent has continuity across server restarts and device switches.
The encrypted source of truth lives in ``sidekick_chat_messages`` — here we
decrypt, cap, and shape the rows into the JSON ADK expects on session create.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import text

from sidekick.crypto import decrypt_text
from sidekick.db import db_connection

logger = logging.getLogger(__name__)

# Authoritative names so the agent's `before_model_callback` history matches
# what ADK would have produced live. ``content.role`` follows Gemini's "user" /
# "model" convention; ``author`` tags who appended the event ("user" or an
# agent name from sidekick.agent).
_AGENT_AUTHOR = "SidekickCoordinator"

# Soft caps so a long-running chat doesn't blow up the prompt or the request body.
DEFAULT_MAX_CHARS = 12000
DEFAULT_MAX_TURNS = 40


def fetch_recent_decrypted_history(
    chat_id: int,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict[str, Any]]:
    """Return decrypted prior turns for a chat in chronological order.

    Trims the oldest messages first when either the per-message text-byte cap
    or the turn-count cap is exceeded so the most recent context is preserved.
    Rows that fail to decrypt (key rotation, corruption) are silently dropped.

    Args:
        chat_id (int): Chat primary key.
        max_chars (int): Hard cap on the total characters returned across all
            messages combined.
        max_turns (int): Hard cap on the number of messages returned.

    Returns:
        list[dict[str, Any]]: ``[{"role": "user"|"assistant", "text": str}, ...]``
        ordered oldest-first. Empty when there's no prior history.
    """
    if not chat_id:
        return []
    try:
        with db_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT role, content_ciphertext, created_at "
                    "FROM sidekick_chat_messages "
                    "WHERE chat_id = :c "
                    "ORDER BY created_at DESC, id DESC LIMIT :lim"
                ),
                {"c": chat_id, "lim": max_turns * 2},
            ).all()
    except Exception:
        logger.exception("history fetch failed for chat_id=%s", chat_id)
        return []

    # Walk newest-first so we can stop as soon as caps are reached, then
    # reverse for chronological output.
    collected: list[dict[str, Any]] = []
    used = 0
    for r in rows:
        plain = decrypt_text(r._mapping["content_ciphertext"])
        if plain is None:
            continue
        role = r._mapping["role"]
        if role not in ("user", "assistant"):
            continue
        if used + len(plain) > max_chars and collected:
            break
        collected.append({"role": role, "text": plain})
        used += len(plain)
        if len(collected) >= max_turns:
            break
    collected.reverse()
    return collected


def history_as_adk_events(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert decrypted history into the JSON shape ADK accepts on session create.

    ADK's ``CreateSessionRequest.events`` is a list of ``google.adk.events.Event``;
    over the wire these map to JSON dicts. ``author`` is ``user`` or an agent
    name; ``content.role`` follows Gemini's ``user`` / ``model`` split.

    Args:
        history (list[dict[str, Any]]): Output of :func:`fetch_recent_decrypted_history`.

    Returns:
        list[dict[str, Any]]: ADK Event JSON objects ready for the request body.
    """
    out: list[dict[str, Any]] = []
    for m in history:
        is_user = m.get("role") == "user"
        out.append(
            {
                "author": "user" if is_user else _AGENT_AUTHOR,
                "invocation_id": "rehydrate-" + secrets.token_hex(6),
                "content": {
                    "role": "user" if is_user else "model",
                    "parts": [{"text": m.get("text") or ""}],
                },
            }
        )
    return out
