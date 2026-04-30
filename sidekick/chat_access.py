"""Helpers shared by the agent tools and the UI blueprint for chat-scoped permissions.

A resource (note / task / calendar event) is reachable from chat ``C`` when:

* the resource's ``chat_id`` equals ``C`` (it lives in that chat), or
* a row exists in ``sidekick_chat_resource_access`` granting ``C`` access.

When the resource has ``chat_id IS NULL`` (it was detached from a deleted chat),
it lives only in the central view; chats can use it only via an explicit grant.

The agent never sees numeric chat ids — only chat titles. ``deny_with_title``
builds a denial payload that names the home chat by title, and
``grant_chat_resource_access`` is an ADK tool the agent can call (after asking
the user) to add an entry to ``sidekick_chat_resource_access`` so the next
retry of update/delete will succeed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from sqlalchemy import text

from sidekick.db import db_connection

logger = logging.getLogger(__name__)


RESOURCE_TABLES: dict[str, str] = {
    "note": "sidekick_notes",
    "task": "sidekick_tasks",
    "calendar_event": "sidekick_calendar_events",
}


def is_admin_bypass(tool_context: Any) -> bool:
    """Return True when the caller has the admin-bypass flag in session state.

    The Flask UI endpoints set this flag on the synthetic tool context they pass
    when invoking agent tool functions directly. The user is interacting through
    the authoritative UI (already authenticated and viewing the resource), so
    chat scoping must not block edits — chat scope is a guard against silent
    cross-chat actions by the agent, not the user.

    Args:
        tool_context (Any): ADK ``ToolContext`` or any object with a ``state``
            attribute supporting ``.get()``.

    Returns:
        bool: True if ``state["_sidekick_admin_bypass"]`` is truthy.
    """
    try:
        return bool(tool_context.state.get("_sidekick_admin_bypass"))
    except Exception:
        return False

GOOGLE_ID_COLUMNS: dict[str, str] = {
    "note": "google_doc_id",
    "task": "google_task_id",
    "calendar_event": "google_event_id",
}


def chat_can_use(
    conn: Any,
    chat_id: Optional[int],
    resource_type: str,
    resource_id: int,
    owner_sub: str,
) -> bool:
    """Return whether the active chat may operate on ``(resource_type, resource_id)``.

    Args:
        conn (Any): Active SQLAlchemy connection.
        chat_id (Optional[int]): Active chat id from session state. ``None`` means
            "no chat context" — only resources in the central scope (granted) can be touched,
            which we treat as denied for tools.
        resource_type (str): One of ``note``, ``task``, ``calendar_event``.
        resource_id (int): Sidekick row id.
        owner_sub (str): Authenticated user id (resource must belong to them).

    Returns:
        bool: True if the chat owns the resource or has an active grant.
    """
    if chat_id is None or resource_type not in RESOURCE_TABLES:
        return False
    table = RESOURCE_TABLES[resource_type]
    row = conn.execute(
        text(
            f"SELECT chat_id FROM {table} "
            f"WHERE id = :id AND owner_sub = :o"
        ),
        {"id": resource_id, "o": owner_sub},
    ).first()
    if row is None:
        return False
    if row[0] == chat_id:
        return True
    grant = conn.execute(
        text(
            "SELECT 1 FROM sidekick_chat_resource_access "
            "WHERE chat_id = :c AND resource_type = :rt AND resource_id = :ri"
        ),
        {"c": chat_id, "rt": resource_type, "ri": resource_id},
    ).first()
    return grant is not None


def lookup_resource_id_by_google_id(
    conn: Any,
    resource_type: str,
    google_id: str,
    owner_sub: str,
) -> Optional[int]:
    """Look up the Sidekick row id for a Google-backed resource.

    Args:
        conn (Any): Active SQLAlchemy connection.
        resource_type (str): One of ``note``, ``task``, ``calendar_event``.
        google_id (str): Google API id (doc id / task id / event id).
        owner_sub (str): Authenticated user id.

    Returns:
        Optional[int]: Sidekick row id, or None if no backup row exists.
    """
    table = RESOURCE_TABLES[resource_type]
    col = GOOGLE_ID_COLUMNS[resource_type]
    row = conn.execute(
        text(
            f"SELECT id FROM {table} "
            f"WHERE {col} = :gid AND owner_sub = :o LIMIT 1"
        ),
        {"gid": google_id, "o": owner_sub},
    ).first()
    return int(row[0]) if row is not None else None


def lookup_resource_home_chat_title(
    conn: Any,
    resource_type: str,
    resource_id: int,
    owner_sub: str,
) -> tuple[Optional[str], bool]:
    """Look up the title of the chat that owns ``(resource_type, resource_id)``.

    Args:
        conn (Any): Active SQLAlchemy connection.
        resource_type (str): One of ``note``, ``task``, ``calendar_event``.
        resource_id (int): Sidekick row id.
        owner_sub (str): Authenticated user id.

    Returns:
        tuple[Optional[str], bool]: ``(home_chat_title, is_orphan)``. ``title`` is
        ``None`` either because the resource has ``chat_id IS NULL`` (an orphan
        from a deleted chat — ``is_orphan=True``) or because no resource row
        matched. ``is_orphan`` is ``True`` only in the first case.
    """
    if resource_type not in RESOURCE_TABLES:
        return None, False
    table = RESOURCE_TABLES[resource_type]
    row = conn.execute(
        text(
            f"SELECT r.chat_id, c.title FROM {table} r "
            f"LEFT JOIN sidekick_chats c ON c.id = r.chat_id "
            f"WHERE r.id = :id AND r.owner_sub = :o LIMIT 1"
        ),
        {"id": resource_id, "o": owner_sub},
    ).first()
    if row is None:
        return None, False
    return (row[1] if row[1] else None), row[0] is None


def access_denied_payload(
    *,
    resource_type: str,
    resource_id: Optional[int],
    google_id: Optional[str] = None,
    home_chat_title: Optional[str] = None,
    home_chat_is_orphan: bool = False,
) -> str:
    """Build the structured JSON returned when a chat may not act on a resource.

    The message is written for the agent (LLM) so it knows to ask the user to
    grant access — it must NEVER quote numeric chat ids back at the user.
    """
    type_human = resource_type.replace("_", " ")
    if home_chat_title:
        where = f'the chat "{home_chat_title}"'
    elif home_chat_is_orphan:
        where = "no chat (it lives only in the central view)"
    else:
        where = "a different chat"
    payload: dict[str, Any] = {
        "error": "cross_chat_access_denied",
        "resource_type": resource_type,
        "next_step": "ask_user_to_grant",
        "message": (
            f"This {type_human} belongs to {where}. Tell the user where it lives "
            "(refer to the chat by title only — never expose chat IDs or numeric "
            "identifiers) and ask whether you should grant this chat permission "
            "to edit it. If the user agrees, call grant_chat_resource_access("
            f'resource_type="{resource_type}", resource_id={resource_id}) '
            "and then retry the original tool call."
        ),
    }
    if home_chat_title:
        payload["home_chat_title"] = home_chat_title
    if home_chat_is_orphan:
        payload["home_chat_is_orphan"] = True
    if resource_id is not None:
        payload["resource_id"] = resource_id
    if google_id is not None:
        payload["google_id"] = google_id
    return json.dumps(payload)


def deny_with_title(
    conn: Any,
    resource_type: str,
    resource_id: int,
    owner_sub: str,
    *,
    google_id: Optional[str] = None,
) -> str:
    """One-call helper: look up the home chat title and build a denial payload.

    Use this at every ``chat_can_use`` call site so the agent never has to
    handle numeric chat ids at all.

    Args:
        conn (Any): Active SQLAlchemy connection.
        resource_type (str): Resource type tag.
        resource_id (int): Sidekick row id.
        owner_sub (str): Authenticated user id.
        google_id (Optional[str]): External id (doc/task/event) when relevant.

    Returns:
        str: JSON denial payload with ``home_chat_title``.
    """
    title, orphan = lookup_resource_home_chat_title(
        conn, resource_type, resource_id, owner_sub
    )
    return access_denied_payload(
        resource_type=resource_type,
        resource_id=resource_id,
        google_id=google_id,
        home_chat_title=title,
        home_chat_is_orphan=orphan,
    )


def _active_chat_id_from(tool_context: Any) -> Optional[int]:
    """Read ``active_chat_id`` from session state safely (None if missing/invalid)."""
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


def grant_chat_resource_access(
    resource_type: str,
    resource_id: int,
    *,
    tool_context: ToolContext,
) -> str:
    """Grant the active chat permission to read/write a resource from a different chat.

    Call this only after the user explicitly agrees (the immediately-prior user
    message confirmed sharing). The grant is per-resource and is recorded in
    ``sidekick_chat_resource_access``; the next ``update`` / ``delete`` call
    on the same resource from the active chat will succeed.

    Args:
        resource_type (str): One of ``"note"``, ``"task"``, ``"calendar_event"``.
        resource_id (int): Sidekick row id of the resource (the ``resource_id``
            field returned by a prior ``cross_chat_access_denied`` error or by
            ``google_docs_get_note``).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON ``{"granted": true, "resource_type", "resource_id"}`` on
        success, or a structured error payload (``invalid_resource_type``,
        ``invalid_resource_id``, ``no_active_chat``, ``resource_not_found``).
    """
    owner = tool_context.user_id
    chat_id = _active_chat_id_from(tool_context)
    if chat_id is None:
        return json.dumps(
            {"error": "no_active_chat", "message": "No active chat context."}
        )
    if resource_type not in RESOURCE_TABLES:
        return json.dumps(
            {"error": "invalid_resource_type", "resource_type": resource_type}
        )
    try:
        rid = int(resource_id)
    except (TypeError, ValueError):
        return json.dumps(
            {"error": "invalid_resource_id", "resource_id": resource_id}
        )
    table = RESOURCE_TABLES[resource_type]
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    f"SELECT chat_id FROM {table} "
                    f"WHERE id = :id AND owner_sub = :o"
                ),
                {"id": rid, "o": owner},
            ).first()
            if row is None:
                return json.dumps(
                    {"error": "resource_not_found", "resource_id": rid}
                )
            home_chat = row[0]
            if home_chat == chat_id:
                return json.dumps(
                    {
                        "granted": True,
                        "already_local": True,
                        "resource_type": resource_type,
                        "resource_id": rid,
                    }
                )
            conn.execute(
                text(
                    "INSERT INTO sidekick_chat_resource_access "
                    "(chat_id, resource_type, resource_id, granted_from_chat_id) "
                    "VALUES (:c, :rt, :ri, :gf) "
                    "ON CONFLICT (chat_id, resource_type, resource_id) DO NOTHING"
                ),
                {"c": chat_id, "rt": resource_type, "ri": rid, "gf": home_chat},
            )
    except Exception as e:
        logger.exception("grant_chat_resource_access failed")
        return json.dumps({"error": "database", "message": str(e)})
    return json.dumps(
        {"granted": True, "resource_type": resource_type, "resource_id": rid}
    )
