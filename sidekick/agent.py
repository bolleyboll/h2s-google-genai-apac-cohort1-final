"""Multi-agent ADK definition: coordinator plus task, schedule, and notes specialists.

    Tools may target Google APIs (Tasks, Calendar, Keep) with database backups, or database-only
    mode when those APIs are disabled via environment. Optional MCP toolsets extend each domain.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import sidekick._google_auth_patch  # noqa: F401 — before google.adk / google.auth

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext
from sqlalchemy import text

from sidekick.chat_access import (
    chat_can_use,
    deny_with_title,
    grant_chat_resource_access,
    is_admin_bypass,
)
from sidekick.db import db_connection
from sidekick.google_credentials import (
    calendar_api_enabled_in_oauth,
    docs_api_enabled_in_oauth,
    tasks_api_enabled_in_oauth,
)
from sidekick.google_docs_tools import (
    google_docs_create_note,
    google_docs_delete_note,
    google_docs_get_note,
    google_docs_list_notes,
    google_docs_update_note,
)
from sidekick.google_product_tools import (
    google_calendar_create_event,
    google_calendar_delete_event,
    google_calendar_list_events,
    google_calendar_update_event,
    google_tasks_create_task,
    google_tasks_delete_task,
    google_tasks_list_tasklists,
    google_tasks_list_tasks,
    google_tasks_update_task,
)
from sidekick.inventory import list_sidekick_inventory
from sidekick.mcp_config import mcp_toolset_from_env
from sidekick.mcp_guard import mcp_access_callback
from sidekick.memory import forget, recall, remember
from sidekick.resource_label import ensure_body_lines_tagged, ensure_title_tagged
from sidekick.time_sanitize import sanitize_schedule_times_to_utc

MODEL = os.environ.get("MODEL", "gemini-2.5-flash")


def _row_to_dict(row) -> dict[str, Any]:
    """Convert a SQLAlchemy result row to a plain dict.

    Args:
        row (Any): Row-like object with ``_mapping`` (for example from ``Result.fetchone()``).

    Returns:
        dict[str, Any]: Column names to values (use ``json.dumps(..., default=str)`` for JSON).
    """
    return dict(row._mapping)


def _owner_sub(tool_context: ToolContext) -> str:
    """Return the ADK user id for database row ownership.

    Args:
        tool_context (ToolContext): Current tool execution context.

    Returns:
        str: ``user_id`` (Google OAuth ``sub`` when using the Flask proxy).
    """
    return tool_context.user_id


def _active_chat_id(tool_context: ToolContext) -> Optional[int]:
    """Read the active chat id from session state, set by the Flask proxy on each run.

    Args:
        tool_context (ToolContext): Current tool execution context.

    Returns:
        Optional[int]: Chat primary key, or None if state hasn't been seeded
        (in which case create operations fall back to the user's Inbox).
    """
    raw = None
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


def list_tasks(limit: int = 20, *, tool_context: ToolContext) -> str:
    """List the user's tasks across every chat (newest first).

    Listing is intentionally not chat-scoped — the user can refer to any of their
    own tasks from any chat. The ``chat_id`` field on each row tells you which
    chat owns the task so you can tell the user where it lives. Editing or
    deleting still respects chat scope (same chat or explicit grant).

    Args:
        limit (int): Maximum rows to return (clamped to 1–200).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of task rows owned by the user.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT t.id, t.title, t.status, t.due_at, t.created_at, "
                "c.title AS home_chat_title "
                "FROM sidekick_tasks t "
                "LEFT JOIN sidekick_chats c ON c.id = t.chat_id "
                "WHERE t.owner_sub = :owner "
                "ORDER BY t.created_at DESC LIMIT :lim"
            ),
            {"owner": owner, "lim": lim},
        )
        rows = [_row_to_dict(row) for row in r]
    return json.dumps(rows, default=str)


def create_task(
    title: str,
    status: str = "open",
    due_at: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create a task bound to the active chat (or the user's Inbox when no chat is set).

    Args:
        title (str): Task title (Sidekick label applied).
        status (str): Task status (default ``open``).
        due_at (Optional[str]): Due instant as ISO-8601, or None.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON object for the inserted row.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    tagged_title = ensure_title_tagged(title)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "INSERT INTO sidekick_tasks (owner_sub, title, status, due_at, chat_id) "
                "VALUES (:owner, :title, :status, :due_at, :cid) "
                "RETURNING id, title, status, due_at, created_at, chat_id"
            ),
            {
                "owner": owner,
                "title": tagged_title,
                "status": status,
                "due_at": due_at,
                "cid": chat_id,
            },
        )
        row = r.fetchone()
    return json.dumps(_row_to_dict(row), default=str)


def update_task_status(
    task_id: int, status: str, *, tool_context: ToolContext
) -> str:
    """Update a task's status if the active chat may operate on it.

    Args:
        task_id (int): Primary key of the task.
        status (str): New status value.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row on success, ``not_found``, or ``cross_chat_access_denied``.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    with db_connection() as conn:
        if not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "task", task_id, owner):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sidekick_tasks "
                    "WHERE id = :id AND owner_sub = :o"
                ),
                {"id": task_id, "o": owner},
            ).first()
            if exists is None:
                return json.dumps({"error": "not_found", "id": task_id})
            return deny_with_title(conn, "task", task_id, owner)
        r = conn.execute(
            text(
                "UPDATE sidekick_tasks SET status = :status "
                "WHERE id = :id AND owner_sub = :owner "
                "RETURNING id, title, status, due_at, created_at, chat_id"
            ),
            {"id": task_id, "status": status, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": task_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_task(task_id: int, *, tool_context: ToolContext) -> str:
    """Delete a task if the active chat may operate on it.

    Args:
        task_id (int): Primary key of the task.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete, ``not_found``, or ``cross_chat_access_denied``.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    with db_connection() as conn:
        if not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "task", task_id, owner):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sidekick_tasks WHERE id = :id AND owner_sub = :o"
                ),
                {"id": task_id, "o": owner},
            ).first()
            if exists is None:
                return json.dumps({"error": "not_found", "id": task_id})
            return deny_with_title(conn, "task", task_id, owner)
        r = conn.execute(
            text(
                "DELETE FROM sidekick_tasks WHERE id = :id AND owner_sub = :owner "
                "RETURNING id"
            ),
            {"id": task_id, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": task_id})
    return json.dumps({"deleted": True, "id": task_id})


def list_calendar_events(limit: int = 20, *, tool_context: ToolContext) -> str:
    """List the user's calendar events across every chat (newest first).

    Listing is intentionally not chat-scoped — see :func:`list_tasks` for the
    rationale. ``chat_id`` on each row identifies the home chat.

    Args:
        limit (int): Maximum rows (clamped to 1–200).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of event rows owned by the user.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT e.id, e.title, e.start_at, e.end_at, e.notes, e.created_at, "
                "c.title AS home_chat_title "
                "FROM sidekick_calendar_events e "
                "LEFT JOIN sidekick_chats c ON c.id = e.chat_id "
                "WHERE e.owner_sub = :owner "
                "ORDER BY e.start_at DESC LIMIT :lim"
            ),
            {"owner": owner, "lim": lim},
        )
        rows = [_row_to_dict(row) for row in r]
    return json.dumps(rows, default=str)


def create_calendar_event(
    title: str,
    start_at: str,
    end_at: Optional[str] = None,
    notes: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create a calendar event bound to the active chat (or Inbox when none).

    Args:
        title (str): Event title (Sidekick label applied).
        start_at (str): Start time as ISO-8601 understood by PostgreSQL ``timestamptz``.
        end_at (Optional[str]): End time, or None.
        notes (Optional[str]): Optional notes (Sidekick label applied to body).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row on success, or JSON error with hint on failure.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    tagged_title = ensure_title_tagged(title)
    tagged_notes = ensure_body_lines_tagged(notes)
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "INSERT INTO sidekick_calendar_events "
                    "(owner_sub, title, start_at, end_at, notes, chat_id) "
                    "VALUES (:owner, :title, CAST(:start_at AS timestamptz), "
                    "CAST(:end_at AS timestamptz), :notes, :cid) "
                    "RETURNING id, title, start_at, end_at, notes, created_at, chat_id"
                ),
                {
                    "owner": owner,
                    "title": tagged_title,
                    "start_at": start_at,
                    "end_at": end_at,
                    "notes": tagged_notes,
                    "cid": chat_id,
                },
            )
            row = r.fetchone()
    except Exception as e:
        return json.dumps(
            {
                "error": "create_calendar_event_failed",
                "message": str(e),
                "hint": (
                    "Call sanitize_schedule_times_to_utc with the user's time wording, "
                    "then use the returned start_at_utc and end_at_utc strings."
                ),
            }
        )
    return json.dumps(_row_to_dict(row), default=str)


def update_calendar_event(
    event_id: int,
    title: Optional[str] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    notes: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Update a calendar event row if it belongs to the current user (database-only mode).

    Args:
        event_id (int): Primary key of the event.
        title (Optional[str]): New title (Sidekick label applied when set).
        start_at (Optional[str]): New start as ISO-8601 for ``timestamptz``.
        end_at (Optional[str]): New end as ISO-8601 for ``timestamptz``.
        notes (Optional[str]): New notes (Sidekick label applied when set).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row on success, or JSON error if nothing to update or not found.
    """
    if title is None and start_at is None and end_at is None and notes is None:
        return json.dumps(
            {"error": "no_fields", "message": "Provide at least one field to update."}
        )
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    sets: list[str] = []
    params: dict[str, Any] = {"id": event_id, "owner": owner}
    if title is not None:
        sets.append("title = :title")
        params["title"] = ensure_title_tagged(title)
    if start_at is not None:
        sets.append("start_at = CAST(:start_at AS timestamptz)")
        params["start_at"] = start_at
    if end_at is not None:
        sets.append("end_at = CAST(:end_at AS timestamptz)")
        params["end_at"] = end_at
    if notes is not None:
        sets.append("notes = :notes")
        params["notes"] = ensure_body_lines_tagged(notes)
    sql = (
        "UPDATE sidekick_calendar_events SET "
        + ", ".join(sets)
        + " WHERE id = :id AND owner_sub = :owner "
        "RETURNING id, title, start_at, end_at, notes, created_at, chat_id"
    )
    with db_connection() as conn:
        if not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "calendar_event", event_id, owner):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sidekick_calendar_events "
                    "WHERE id = :id AND owner_sub = :o"
                ),
                {"id": event_id, "o": owner},
            ).first()
            if exists is None:
                return json.dumps({"error": "not_found", "id": event_id})
            return deny_with_title(conn, "calendar_event", event_id, owner)
        r = conn.execute(text(sql), params)
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": event_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_calendar_event(event_id: int, *, tool_context: ToolContext) -> str:
    """Delete a stored calendar event if the active chat may operate on it.

    Args:
        event_id (int): Primary key of the event.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete, ``not_found``, or ``cross_chat_access_denied``.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    with db_connection() as conn:
        if not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "calendar_event", event_id, owner):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sidekick_calendar_events "
                    "WHERE id = :id AND owner_sub = :o"
                ),
                {"id": event_id, "o": owner},
            ).first()
            if exists is None:
                return json.dumps({"error": "not_found", "id": event_id})
            return deny_with_title(conn, "calendar_event", event_id, owner)
        r = conn.execute(
            text(
                "DELETE FROM sidekick_calendar_events "
                "WHERE id = :id AND owner_sub = :owner RETURNING id"
            ),
            {"id": event_id, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": event_id})
    return json.dumps({"deleted": True, "id": event_id})


def list_notes(
    limit: int = 20,
    query: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """List the user's notes across every chat (newest first).

    Args:
        limit (int): Maximum rows (clamped to 1–200).
        query (Optional[str]): Case-insensitive substring matched against title or body.
            Use this when the user asks for a specific older note ("the Redis doc").
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of note rows owned by the user.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        if query:
            r = conn.execute(
                text(
                    "SELECT n.id, n.title, n.body, n.created_at, "
                    "c.title AS home_chat_title "
                    "FROM sidekick_notes n "
                    "LEFT JOIN sidekick_chats c ON c.id = n.chat_id "
                    "WHERE n.owner_sub = :owner "
                    "  AND (n.title ILIKE :q OR n.body ILIKE :q) "
                    "ORDER BY n.created_at DESC LIMIT :lim"
                ),
                {"owner": owner, "lim": lim, "q": f"%{query}%"},
            )
        else:
            r = conn.execute(
                text(
                    "SELECT n.id, n.title, n.body, n.created_at, "
                    "c.title AS home_chat_title "
                    "FROM sidekick_notes n "
                    "LEFT JOIN sidekick_chats c ON c.id = n.chat_id "
                    "WHERE n.owner_sub = :owner "
                    "ORDER BY n.created_at DESC LIMIT :lim"
                ),
                {"owner": owner, "lim": lim},
            )
        rows = [_row_to_dict(row) for row in r]
    return json.dumps(rows, default=str)


def create_note(
    title: str,
    body: Optional[str] = None,
    force_new: bool = False,
    *,
    tool_context: ToolContext,
) -> str:
    """Save a note. Appends to the chat's most recent note by default; creates a fresh row otherwise.

    Args:
        title (str): Heading for this save. When a chat already has a note,
            this is appended as a section heading inside the existing body.
            When creating a fresh row, this becomes the note title.
        body (Optional[str]): Full content for this save (may include bullets,
            multiple lines, etc.). Combine multi-item saves into a single call.
        force_new (bool): When True, always insert a new note row instead of
            appending. Use only when the user explicitly asks for a separate note.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON of the affected row (with ``appended=True`` when extending).
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    tagged_title = ensure_title_tagged(title)
    tagged_body = ensure_body_lines_tagged(body)
    with db_connection() as conn:
        if not force_new and chat_id is not None:
            existing = conn.execute(
                text(
                    "SELECT id, body FROM sidekick_notes "
                    "WHERE owner_sub = :o AND chat_id = :c "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": owner, "c": chat_id},
            ).first()
            if existing is not None:
                old_body = (existing[1] or "").rstrip()
                addition_parts = []
                if tagged_title:
                    addition_parts.append(tagged_title)
                if tagged_body:
                    addition_parts.append(tagged_body)
                addition = "\n".join(addition_parts).strip()
                new_body = (old_body + "\n\n" + addition).strip() if addition else old_body
                r = conn.execute(
                    text(
                        "UPDATE sidekick_notes SET body = :b "
                        "WHERE id = :id AND owner_sub = :o "
                        "RETURNING id, title, body, created_at, chat_id"
                    ),
                    {"b": new_body, "id": existing[0], "o": owner},
                )
                row = r.fetchone()
                if row is not None:
                    out = _row_to_dict(row)
                    out["appended"] = True
                    return json.dumps(out, default=str)
        r = conn.execute(
            text(
                "INSERT INTO sidekick_notes (owner_sub, title, body, chat_id) "
                "VALUES (:owner, :title, :body, :cid) "
                "RETURNING id, title, body, created_at, chat_id"
            ),
            {"owner": owner, "title": tagged_title, "body": tagged_body, "cid": chat_id},
        )
        row = r.fetchone()
    return json.dumps(_row_to_dict(row), default=str)


def get_note(note_id: int, *, tool_context: ToolContext) -> str:
    """Fetch one note by id from any of the user's chats (read-only, cross-chat).

    Reads do not require a chat-scope match — only ownership. If the agent
    needs to *edit* the returned note and the active chat does not own it,
    ``update_note``/``delete_note`` will surface a ``cross_chat_access_denied``
    error and the user can grant access from the central view.

    Args:
        note_id (int): Primary key of the note.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row including ``chat_id``, or ``not_found``.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT n.id, n.title, n.body, n.created_at, "
                "c.title AS home_chat_title "
                "FROM sidekick_notes n "
                "LEFT JOIN sidekick_chats c ON c.id = n.chat_id "
                "WHERE n.id = :id AND n.owner_sub = :owner"
            ),
            {"id": note_id, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": note_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_note(note_id: int, *, tool_context: ToolContext) -> str:
    """Delete a note if the active chat may operate on it.

    Args:
        note_id (int): Primary key of the note.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete, ``not_found``, or ``cross_chat_access_denied``.
    """
    owner = _owner_sub(tool_context)
    chat_id = _active_chat_id(tool_context)
    with db_connection() as conn:
        if not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "note", note_id, owner):
            exists = conn.execute(
                text("SELECT 1 FROM sidekick_notes WHERE id = :id AND owner_sub = :o"),
                {"id": note_id, "o": owner},
            ).first()
            if exists is None:
                return json.dumps({"error": "not_found", "id": note_id})
            return deny_with_title(conn, "note", note_id, owner)
        r = conn.execute(
            text(
                "DELETE FROM sidekick_notes WHERE id = :id AND owner_sub = :owner "
                "RETURNING id"
            ),
            {"id": note_id, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": note_id})
    return json.dumps({"deleted": True, "id": note_id})


def _task_tools():
    """Build the tool list for the task specialist agent.

    Returns:
        list[Any]: Google Tasks API callables, or database task CRUD, plus optional MCP toolset.
    """
    if tasks_api_enabled_in_oauth():
        tools: list = [
            google_tasks_list_tasklists,
            google_tasks_list_tasks,
            google_tasks_create_task,
            google_tasks_update_task,
            google_tasks_delete_task,
        ]
    else:
        tools = [
            list_tasks,
            create_task,
            update_task_status,
            delete_task,
        ]
    mcp = mcp_toolset_from_env("SIDEKICK_MCP_TASK")
    if mcp:
        tools.append(mcp)
    tools.append(list_sidekick_inventory)
    tools.append(grant_chat_resource_access)
    tools.extend([remember, recall, forget])
    return tools


def _schedule_tools():
    """Build the tool list for the schedule specialist agent.

    Returns:
        list[Any]: Time sanitization, then Calendar API or database calendar tools, plus optional MCP.
    """
    tools: list = [sanitize_schedule_times_to_utc]
    if calendar_api_enabled_in_oauth():
        tools.extend(
            [
                google_calendar_list_events,
                google_calendar_create_event,
                google_calendar_update_event,
                google_calendar_delete_event,
            ]
        )
    else:
        tools.extend(
            [
                list_calendar_events,
                create_calendar_event,
                update_calendar_event,
                delete_calendar_event,
            ]
        )
    mcp = mcp_toolset_from_env("SIDEKICK_MCP_CALENDAR")
    if mcp:
        tools.append(mcp)
    tools.append(list_sidekick_inventory)
    tools.append(grant_chat_resource_access)
    tools.extend([remember, recall, forget])
    return tools


def _notes_tools():
    """Build the tool list for the notes specialist agent.

    Returns:
        list[Any]: Google Docs API callables or database note CRUD, plus optional MCP toolset.
    """
    if docs_api_enabled_in_oauth():
        tools: list = [
            google_docs_list_notes,
            google_docs_get_note,
            google_docs_create_note,
            google_docs_update_note,
            google_docs_delete_note,
        ]
    else:
        tools = [
            list_notes,
            create_note,
            get_note,
            delete_note,
        ]
    mcp = mcp_toolset_from_env("SIDEKICK_MCP_NOTES")
    if mcp:
        tools.append(mcp)
    tools.append(list_sidekick_inventory)
    tools.append(grant_chat_resource_access)
    tools.extend([remember, recall, forget])
    return tools


task_specialist = LlmAgent(
    model=MODEL,
    name="TaskSpecialist",
    description="Google Tasks with AlloyDB backup, or database-only tasks when Tasks API is disabled.",
    instruction=(
        "You handle task management only. "
        "When google_tasks_* tools are available, they are the default: list, create, update, and delete "
        "tasks in Google Tasks. Only modify or delete tasks that carry the Sidekick label—list with "
        "only_sidekick=true first when ids are unknown. Each create/update/delete is mirrored to AlloyDB "
        "when a backup row exists.\n"
        "READING is cross-chat: the user can refer to any of their tasks from any chat — never claim "
        "older tasks are inaccessible. List/get freely.\n"
        "WRITING is chat-scoped: create binds the new task to the active chat; update/delete on a "
        "task owned by a different chat returns 'cross_chat_access_denied'. When that happens:\n"
        "  - NEVER quote chat IDs, Chat IDs, or any numeric ids. Use only the `home_chat_title` "
        "    field from the error (or the `home_chat_title` on rows from list_tasks).\n"
        "  - Propose granting access yourself: \"This task lives in the chat "
        "    \\\"<home_chat_title>\\\". Want me to give this chat access so I can update it?\"\n"
        "  - If the user agrees, call grant_chat_resource_access(resource_type='task', "
        "    resource_id=<resource_id>) and immediately retry the update/delete.\n"
        "  - Only fall back to suggesting the central 'All resources' UI if the user declines.\n"
        "  Do NOT silently fail.\n"
        "When only database tools are available (Google Tasks API disabled), use those for full CRUD. "
        "If MCP tools are present, use them when the user asks for another task system. "
        "MCP TOOLS ARE GATED PER CHAT: each MCP server requires an explicit chat-level "
        "grant. If a tool call returns an error like 'mcp_access_denied' (often with "
        "reason='no_grant' and an mcp_prefix such as 'task'), tell the user the chat "
        "doesn't have access to that MCP server and instruct them to enable it from the "
        "MCP access section of the chat settings, then retry. NEVER claim the failure was "
        "due to a missing tool — surface the access-denied reason. "
        "If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only. "
        "**Multi-domain and full-inventory handoff:** If the coordinator already ran "
        "list_sidekick_inventory for a full inventory, always end your turn with "
        "transfer_to_agent('ScheduleSpecialist') only (no other text). Else, if the user's "
        "latest message also needs calendar/scheduling beyond what you handled, end with "
        "transfer_to_agent('ScheduleSpecialist') only; else if it also needs notes/docs, "
        "transfer_to_agent('NotesSpecialist') only; else reply normally (single-domain). "
        "Plain text before another specialist would block the coordinator from chaining in "
        "the same user turn—use transfer-only when handing off. "
        "MEMORY: the user message may be prefixed with a '[CONTEXT — Memories…]' block — "
        "treat it as background, never quote it back. Call remember(\"…\") for durable "
        "task-related facts (recurring deadlines, who owns what, ongoing project names). "
        "Call recall(query) for explicit lookups. Call forget(id) when the user disowns a fact. "
        "Keep replies concise."
    ),
    tools=_task_tools(),
    before_tool_callback=mcp_access_callback,
)

schedule_specialist = LlmAgent(
    model=MODEL,
    name="ScheduleSpecialist",
    description="Google Calendar with AlloyDB backup, or database-only events when Calendar API is disabled.",
    instruction=(
        "You handle schedules and calendar-style requests. "
        "When google_calendar_* tools are available, use them by default: list, create, update, and delete "
        "events on the user's primary calendar; only change or remove events tagged as Sidekick—list with "
        "only_sidekick=true when needed. Mirrored rows in AlloyDB are updated or removed after Google changes.\n"
        "READING is cross-chat: the user can refer to any of their events from any chat — never claim an "
        "older event is inaccessible. List/get freely.\n"
        "WRITING is chat-scoped: create binds the new event to the active chat; update/delete on "
        "an event owned by a different chat returns 'cross_chat_access_denied'. When that happens:\n"
        "  - NEVER quote chat IDs, Chat IDs, or any numeric ids. Refer to the home chat by its "
        "    `home_chat_title` only.\n"
        "  - Propose granting access yourself: \"This event lives in the chat "
        "    \\\"<home_chat_title>\\\". Want me to give this chat access so I can edit it?\"\n"
        "  - If the user agrees, call grant_chat_resource_access(resource_type='calendar_event', "
        "    resource_id=<resource_id>) and immediately retry the update/delete.\n"
        "  - Only fall back to suggesting the central 'All resources' UI if the user declines.\n"
        "  Do NOT silently fail.\n"
        "When only database schedule tools are available, use list/create/update/delete on sidekick_calendar_events. "
        "When times are vague, call sanitize_schedule_times_to_utc first; pass start_at_utc and "
        "end_at_utc into google_calendar_create_event or google_calendar_update_event (or DB equivalents). "
        "Use MCP calendar tools when configured and relevant. MCP tools require a "
        "per-chat grant — if a call returns 'mcp_access_denied', tell the user the chat "
        "needs MCP access enabled in the chat settings before retrying.\n"
        "If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only.\n"
        "**Multi-domain and full-inventory handoff:** If this is the full-inventory round "
        "(list_sidekick_inventory was used), always end with transfer_to_agent('NotesSpecialist') "
        "only. Else, if the user's latest message also needs notes/docs beyond what you handled, "
        "end with transfer_to_agent('NotesSpecialist') only; elif the same message combines "
        "calendar work with tasks (or other domains) and notes are not requested, end with "
        "transfer_to_agent('SidekickCoordinator') only so the coordinator can merge the reply; "
        "else reply normally. "
        "MEMORY: the user message may be prefixed with a '[CONTEXT — Memories…]' block — "
        "treat it as background, never quote it back. Call remember(\"…\") for durable "
        "schedule facts (recurring meetings, time-zone preferences, regular blocks). "
        "Call recall(query) when you need older context. Call forget(id) when a recurring "
        "event is dropped."
    ),
    tools=_schedule_tools(),
    before_tool_callback=mcp_access_callback,
)

notes_specialist = LlmAgent(
    model=MODEL,
    name="NotesSpecialist",
    description="Google Docs with AlloyDB backup, or database-only notes when Docs is disabled.",
    instruction=(
        "You handle notes and reference information. "
        "When google_docs_* tools are available, use them by default: notes are stored as Google Docs.\n"
        "ONE DOC PER CHAT (default): a chat session has ONE growing notes document. "
        "google_docs_create_note automatically appends to the chat's existing doc, or creates a "
        "fresh one on the first save in that chat. NEVER create one doc per bullet or per item.\n"
        "ONE TOOL CALL PER USER MESSAGE: when the user gives you several items in one message "
        "(bullet list, comma-separated lines, multiple paragraphs), make exactly ONE call to "
        "google_docs_create_note with all of that content combined into the `body` argument. "
        "Example — user says 'save: Quantum is fun / Schrodinger / Redis is weird' → one call: "
        "google_docs_create_note(title='Mixed notes — Apr 29', body='- Quantum is fun\\n"
        "- Schrodinger\\n- Redis is weird'). Do NOT make three separate calls.\n"
        "WHEN TO SET force_new=True: only when the user EXPLICITLY says they want a separate "
        "doc — phrases like 'save this as a new/separate/different doc', 'create a fresh "
        "document for this', 'don't add to the existing one'. Otherwise leave force_new=False "
        "(the default) so the chat stays anchored to one doc.\n"
        "READING (no permission needed): the user can refer to ANY of their own notes from any chat. "
        "When the user asks about an older or differently-titled note (e.g. 'the Redis doc', "
        "'the meeting notes from last week'), do NOT say you can't access it. Instead:\n"
        "  1. Call google_docs_list_notes with the `query` argument set to a substring of the topic "
        "     (e.g. query='Redis') to find matching docs across all chats. If you get nothing, retry "
        "     with a shorter or different fragment, or call google_docs_list_notes without a query "
        "     to browse recent docs.\n"
        "  2. Once you have the doc_id, call google_docs_get_note(doc_id) to fetch the title and full "
        "     plaintext body. Summarize or quote it back to the user. The response includes a "
        "     `chat_id` field telling you which chat owns the doc.\n"
        "WRITING (chat-scoped): update/delete are bound to the active chat. If "
        "google_docs_update_note or google_docs_delete_note returns a 'cross_chat_access_denied' "
        "error:\n"
        "  - NEVER tell the user a chat ID, Chat ID, numeric identifier, or any internal database "
        "    id. Refer to chats by the `home_chat_title` field only. If `home_chat_is_orphan` is "
        "    true, say the note isn't bound to any chat (it lives only in the central view).\n"
        "  - PROPOSE granting access. Say something like: \"This note lives in the chat "
        "    \\\"<home_chat_title>\\\". Want me to give this chat access so I can edit it?\" Do "
        "    NOT redirect the user to the All-resources UI unless they decline.\n"
        "  - If the user agrees in their next message, call grant_chat_resource_access("
        "    resource_type='note', resource_id=<resource_id from the error or from get_note>) and "
        "    then immediately retry the original update_note / delete_note call. Confirm what you "
        "    did in plain language.\n"
        "  - If the user declines, leave the note alone. As a fallback, you can mention that they "
        "    can switch to the home chat by name, or use the central 'All resources' Share button.\n"
        "  Do NOT silently fail.\n"
        "TITLES: derive from actual content/topic — never generic titles like 'Notes' or 'Note'. "
        "Always append today's date (e.g. 'Apr 29'). Example: 'Physics & Tech — Apr 29'. "
        "When appending to an existing chat doc, the title argument is used as a section heading "
        "inside that doc — keep it concise and descriptive of the new content being added.\n"
        "When only database note tools are available, use those — list_notes also accepts a `query` "
        "substring, get_note(note_id) reads any of the user's notes regardless of chat, and "
        "create_note uses the same append-by-default semantics with a force_new flag.\n"
        "Use MCP tools when available for other external sources. Each MCP server "
        "is gated per chat: if a call returns 'mcp_access_denied', tell the user the "
        "chat needs MCP access enabled in the chat settings before retrying.\n"
        "If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only.\n"
        "**Full-inventory and multi-domain handoff:** If this is the inventory interpretation "
        "round after list_sidekick_inventory, end with transfer_to_agent('SidekickCoordinator') "
        "only so the coordinator synthesizes. Else, if the user's latest message also required "
        "tasks or calendar work in the same turn (already handled before you), end with "
        "transfer_to_agent('SidekickCoordinator') only so one merged answer is produced; "
        "otherwise reply normally for note-only requests. "
        "MEMORY: the user message may be prefixed with a '[CONTEXT — Memories…]' block — "
        "treat it as background, never quote it back. Call remember(\"…\") for durable "
        "note-related facts (topics the user keeps a running doc on, naming conventions "
        "they prefer, ongoing reference projects). Call recall(query) before searching "
        "Drive when the user phrases a question in their own terms. Call forget(id) when "
        "a topic is no longer relevant."
    ),
    tools=_notes_tools(),
    before_tool_callback=mcp_access_callback,
)

root_agent = LlmAgent(
    model=MODEL,
    name="SidekickCoordinator",
    description=(
        "Routes each request to the right specialist: calendar (ScheduleSpecialist), "
        "notes/Keep (NotesSpecialist), or tasks (TaskSpecialist)—no default preference."
    ),
    instruction=(
        "You are the primary assistant. Break multi-step requests into steps. "
        "Routing rule: pick the specialist from the user's intent. Do **not** default to "
        "TaskSpecialist when the message is really about calendar or notes.\n"
        "- **ScheduleSpecialist** — calendar, events, meetings, appointments, scheduling, "
        "time ranges, reminders that belong on a calendar, availability.\n"
        "- **NotesSpecialist** — Google Docs / notes, memos, saving text, reference info, "
        "jotting things down, note titles and bodies (not calendar blocks, not task checklists).\n"
        "- **TaskSpecialist** — Google Tasks / to-dos, action items, checklists, tasks to complete.\n"
        "**Multiple domains in one user message (ADK constraint):** You only get one "
        "transfer_to_agent from yourself per user turn before a specialist replies with text—"
        "after that, the framework stops your loop. So you must not plan to call three "
        "transfers yourself. Instead: (1) If several domains apply, transfer once to the "
        "**first** specialist in pipeline order **TaskSpecialist → ScheduleSpecialist → "
        "NotesSpecialist** whose domain the user needs (skip specialists for domains they "
        "did not ask for). They hand off with transfer_to_agent along that chain. "
        "(2) If intent is ambiguous, ask one brief clarifying question instead of guessing.\n"
        "When the user asks to list everything, a full inventory, or all items they created with "
        "Sidekick across tasks, calendar, and notes, call list_sidekick_inventory first and "
        "summarize the JSON for them in plain language. "
        "Then transfer_to_agent('TaskSpecialist') **once** to start the inventory interpretation "
        "round—TaskSpecialist chains to ScheduleSpecialist, then NotesSpecialist, then back to "
        "you; after that chain completes, synthesize a unified next-actions list for the user. "
        "IMPORTANT: During this inventory interpretation round, do not create/update/delete anything "
        "unless the user explicitly asked for changes; provide suggestions only and ask a single "
        "confirmation question before applying any proposed modifications. "
        "For a single-domain request, delegate with transfer_to_agent to that specialist once. "
        "Specialists prefer Google-backed tools when those tools appear in their tool list "
        "(AlloyDB holds backup copies of Google creates). "
        "After specialists complete sub-parts, synthesize a clear answer for the user.\n"
        "MEMORY (long-term): you have remember(text, importance), recall(query, limit), and "
        "forget(memory_id). At the start of every turn the user message may be prefixed with a "
        "'[CONTEXT — Memories about this user…]' block; treat that as background you already "
        "know — never quote it back, never recite ids, just use it naturally. When the user "
        "states a durable fact about themselves (a recurring schedule, a teammate's name, a "
        "preference, an ongoing project), call remember() in third person, e.g. "
        "remember(\"User's standup is 10 am Mon/Wed/Fri\"). When you suspect a relevant fact "
        "exists but isn't in the auto-injected context, call recall(query). When the user says "
        "something like 'forget that' or 'I no longer X', call forget(memory_id). Don't memorise "
        "fleeting details — only durable facts the user would expect you to recall later."
    ),
    tools=[list_sidekick_inventory, remember, recall, forget],
    before_tool_callback=mcp_access_callback,
    sub_agents=[schedule_specialist, notes_specialist, task_specialist],
)
