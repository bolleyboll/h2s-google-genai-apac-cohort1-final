"""Auto-generate a short chat title from its first turn (OpenWebUI-style).

A new chat is created with a placeholder title (``New chat``). After the first
user message + assistant response, this module makes a single Gemini call to
synthesize a 3-6 word title and updates the chat row — but only if the title
is still the default. If the user has already renamed the chat, we leave it
alone.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from sqlalchemy import text

from sidekick.db import db_connection

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New chat"
_MAX_TITLE_CHARS = 80


@lru_cache(maxsize=1)
def _genai_client():
    """Return a cached ``google.genai.Client`` configured from env (Vertex or API key).

    Returns:
        Optional[Any]: Initialized client, or ``None`` if the SDK is unavailable.
    """
    try:
        from google import genai  # type: ignore
    except Exception:
        logger.warning(
            "google.genai SDK not available; chats will not be auto-named."
        )
        return None
    try:
        return genai.Client()
    except Exception:
        logger.exception("Failed to construct google.genai.Client for auto-naming")
        return None


def _model_name() -> str:
    """Return the Gemini model id used for title generation.

    Returns:
        str: Value of ``MODEL`` env var, defaulting to ``gemini-2.5-flash``.
    """
    return os.environ.get("MODEL", "gemini-2.5-flash")


def _generate_title(user_text: str, assistant_text: str) -> Optional[str]:
    """Ask Gemini for a concise chat title from the first turn.

    Args:
        user_text (str): First user message in the chat.
        assistant_text (str): Assistant's first reply (may be empty).

    Returns:
        Optional[str]: A short title (≤ ``_MAX_TITLE_CHARS`` chars), or None on failure.
    """
    client = _genai_client()
    if client is None:
        return None
    user_snip = (user_text or "").strip()[:600]
    asst_snip = (assistant_text or "").strip()[:400]
    if not user_snip:
        return None
    prompt = (
        "Generate a concise 3-6 word title that summarises this chat opening. "
        "Return ONLY the title — no quotes, no punctuation at the end, no preamble, "
        "no newlines.\n\n"
        f"User: {user_snip}\n"
        f"Assistant: {asst_snip}"
    )
    try:
        resp = client.models.generate_content(
            model=_model_name(),
            contents=prompt,
        )
    except Exception:
        logger.exception("Gemini call failed for chat title generation")
        return None
    raw = (getattr(resp, "text", None) or "").strip()
    if not raw:
        return None
    # Take only the first line, strip surrounding quotes / fences / trailing punctuation.
    first = raw.splitlines()[0].strip()
    for ch in ('"', "'", "`"):
        if first.startswith(ch) and first.endswith(ch):
            first = first[1:-1].strip()
    while first and first[-1] in ".!?":
        first = first[:-1].rstrip()
    if not first:
        return None
    return first[:_MAX_TITLE_CHARS]


def maybe_autoname_chat(
    chat_id: int, user_text: str, assistant_text: str
) -> Optional[str]:
    """Rename the chat from its default title using a Gemini-generated summary.

    The update only fires when:

    * the chat's current title is still ``DEFAULT_TITLE`` (so user-renamed
      chats are never overwritten),
    * a non-empty title comes back from the LLM.

    Args:
        chat_id (int): Chat row id to consider for renaming.
        user_text (str): First user message text.
        assistant_text (str): Assistant final reply text.

    Returns:
        Optional[str]: The new title when a rename was applied, else None.
    """
    if not chat_id:
        return None
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT title FROM sidekick_chats WHERE id = :id"
                ),
                {"id": chat_id},
            ).first()
            if row is None or row[0] != DEFAULT_TITLE:
                return None
    except Exception:
        logger.exception("Pre-check failed for auto-naming chat_id=%s", chat_id)
        return None

    title = _generate_title(user_text, assistant_text)
    if not title:
        return None
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "UPDATE sidekick_chats SET title = :t, updated_at = NOW() "
                    "WHERE id = :id AND title = :default "
                    "RETURNING id"
                ),
                {"t": title, "id": chat_id, "default": DEFAULT_TITLE},
            ).first()
    except Exception:
        logger.exception("Title update failed for chat_id=%s", chat_id)
        return None
    return title if r is not None else None
