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

from sidekick.db import db_connection
from sidekick.google_credentials import (
    calendar_api_enabled_in_oauth,
    keep_api_enabled_in_oauth,
    tasks_api_enabled_in_oauth,
)
from sidekick.google_keep_tools import (
    google_keep_create_note,
    google_keep_delete_note,
    google_keep_list_notes,
    google_keep_update_note,
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


def list_tasks(limit: int = 20, *, tool_context: ToolContext) -> str:
    """List recent tasks for the current user from the database (newest first).

    Args:
        limit (int): Maximum rows to return (clamped to 1–200).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of task rows.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT id, title, status, due_at, created_at "
                "FROM sidekick_tasks WHERE owner_sub = :owner "
                "ORDER BY created_at DESC LIMIT :lim"
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
    """Create a task row for the current user in the database.

    Args:
        title (str): Task title (Sidekick label applied).
        status (str): Task status (default ``open``).
        due_at (Optional[str]): Due instant as ISO-8601, or None.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON object for the inserted row.
    """
    owner = _owner_sub(tool_context)
    tagged_title = ensure_title_tagged(title)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "INSERT INTO sidekick_tasks (owner_sub, title, status, due_at) "
                "VALUES (:owner, :title, :status, :due_at) "
                "RETURNING id, title, status, due_at, created_at"
            ),
            {"owner": owner, "title": tagged_title, "status": status, "due_at": due_at},
        )
        row = r.fetchone()
    return json.dumps(_row_to_dict(row), default=str)


def update_task_status(
    task_id: int, status: str, *, tool_context: ToolContext
) -> str:
    """Update a task's status if it belongs to the current user.

    Args:
        task_id (int): Primary key of the task.
        status (str): New status value.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row on success, or ``{"error": "not_found"}`` JSON if not owned/found.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "UPDATE sidekick_tasks SET status = :status "
                "WHERE id = :id AND owner_sub = :owner "
                "RETURNING id, title, status, due_at, created_at"
            ),
            {"id": task_id, "status": status, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": task_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_task(task_id: int, *, tool_context: ToolContext) -> str:
    """Delete a task row if it belongs to the current user.

    Args:
        task_id (int): Primary key of the task.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete or ``not_found`` error.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
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
    """List stored calendar events for the current user from the database.

    Args:
        limit (int): Maximum rows (clamped to 1–200).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of event rows.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT id, title, start_at, end_at, notes, created_at "
                "FROM sidekick_calendar_events WHERE owner_sub = :owner "
                "ORDER BY start_at DESC LIMIT :lim"
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
    """Create a calendar event row for the current user in the database.

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
    tagged_title = ensure_title_tagged(title)
    tagged_notes = ensure_body_lines_tagged(notes)
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "INSERT INTO sidekick_calendar_events "
                    "(owner_sub, title, start_at, end_at, notes) "
                    "VALUES (:owner, :title, CAST(:start_at AS timestamptz), "
                    "CAST(:end_at AS timestamptz), :notes) "
                    "RETURNING id, title, start_at, end_at, notes, created_at"
                ),
                {
                    "owner": owner,
                    "title": tagged_title,
                    "start_at": start_at,
                    "end_at": end_at,
                    "notes": tagged_notes,
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
        "RETURNING id, title, start_at, end_at, notes, created_at"
    )
    with db_connection() as conn:
        r = conn.execute(text(sql), params)
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": event_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_calendar_event(event_id: int, *, tool_context: ToolContext) -> str:
    """Delete a stored calendar event if it belongs to the current user.

    Args:
        event_id (int): Primary key of the event.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete or ``not_found`` error.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
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


def list_notes(limit: int = 20, *, tool_context: ToolContext) -> str:
    """List notes for the current user from the database (newest first).

    Args:
        limit (int): Maximum rows (clamped to 1–200).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON array of note rows.
    """
    owner = _owner_sub(tool_context)
    lim = max(1, min(int(limit), 200))
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT id, title, body, created_at FROM sidekick_notes "
                "WHERE owner_sub = :owner ORDER BY created_at DESC LIMIT :lim"
            ),
            {"owner": owner, "lim": lim},
        )
        rows = [_row_to_dict(row) for row in r]
    return json.dumps(rows, default=str)


def create_note(
    title: str,
    body: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create a note row for the current user in the database.

    Args:
        title (str): Note title (Sidekick label applied).
        body (Optional[str]): Optional body text.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON object for the inserted row.
    """
    owner = _owner_sub(tool_context)
    tagged_title = ensure_title_tagged(title)
    tagged_body = ensure_body_lines_tagged(body)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "INSERT INTO sidekick_notes (owner_sub, title, body) "
                "VALUES (:owner, :title, :body) "
                "RETURNING id, title, body, created_at"
            ),
            {"owner": owner, "title": tagged_title, "body": tagged_body},
        )
        row = r.fetchone()
    return json.dumps(_row_to_dict(row), default=str)


def get_note(note_id: int, *, tool_context: ToolContext) -> str:
    """Fetch one note by id if it belongs to the current user.

    Args:
        note_id (int): Primary key of the note.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON row or ``not_found`` error.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT id, title, body, created_at FROM sidekick_notes "
                "WHERE id = :id AND owner_sub = :owner"
            ),
            {"id": note_id, "owner": owner},
        )
        row = r.fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "id": note_id})
    return json.dumps(_row_to_dict(row), default=str)


def delete_note(note_id: int, *, tool_context: ToolContext) -> str:
    """Delete a note if it belongs to the current user.

    Args:
        note_id (int): Primary key of the note.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming delete or ``not_found`` error.
    """
    owner = _owner_sub(tool_context)
    with db_connection() as conn:
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
    return tools


def _notes_tools():
    """Build the tool list for the notes specialist agent.

    Returns:
        list[Any]: Google Keep API callables or database note CRUD, plus optional MCP toolset.
    """
    if keep_api_enabled_in_oauth():
        tools: list = [
            google_keep_list_notes,
            google_keep_create_note,
            google_keep_update_note,
            google_keep_delete_note,
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
        "when a backup row exists. "
        "When only database tools are available (Google Tasks API disabled), use those for full CRUD. "
        "If MCP tools are present, use them when the user asks for another task system. "
        "If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only. "
        "Keep replies concise."
    ),
    tools=_task_tools(),
)

schedule_specialist = LlmAgent(
    model=MODEL,
    name="ScheduleSpecialist",
    description="Google Calendar with AlloyDB backup, or database-only events when Calendar API is disabled.",
    instruction=(
        "You handle schedules and calendar-style requests. "
        "When google_calendar_* tools are available, use them by default: list, create, update, and delete "
        "events on the user's primary calendar; only change or remove events tagged as Sidekick—list with "
        "only_sidekick=true when needed. Mirrored rows in AlloyDB are updated or removed after Google changes. "
        "When only database schedule tools are available, use list/create/update/delete on sidekick_calendar_events. "
        "When times are vague, call sanitize_schedule_times_to_utc first; pass start_at_utc and "
        "end_at_utc into google_calendar_create_event or google_calendar_update_event (or DB equivalents). "
        "Use MCP calendar tools when configured and relevant."
        " If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only."
    ),
    tools=_schedule_tools(),
)

notes_specialist = LlmAgent(
    model=MODEL,
    name="NotesSpecialist",
    description="Google Keep with AlloyDB backup, or database-only notes when Keep is disabled.",
    instruction=(
        "You handle notes and reference information. "
        "When google_keep_* tools are available, use them by default: notes go to Google Keep; list, create, "
        "update (recreates the note—new resource name), and delete. Only update or delete notes that carry "
        "the Sidekick label; list with include_untagged=false to see Sidekick notes. AlloyDB backup rows "
        "are updated on create/update/delete. "
        "When only database note tools are available, use those for list/create/get/delete. "
        "Use MCP tools when available for other external sources."
        " If you are being asked to interpret inventory or suggest next actions, do NOT "
        "create/update/delete anything unless the user explicitly asked you to; provide "
        "recommendations only."
    ),
    tools=_notes_tools(),
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
        "- **NotesSpecialist** — Google Keep / notes, memos, saving text, reference info, "
        "jotting things down, note titles and bodies (not calendar blocks, not task checklists).\n"
        "- **TaskSpecialist** — Google Tasks / to-dos, action items, checklists, tasks to complete.\n"
        "If the user names two domains (e.g. add a task and a calendar event), delegate twice "
        "or in sequence. If intent is ambiguous, ask one brief clarifying question instead of "
        "guessing tasks.\n"
        "When the user asks to list everything, a full inventory, or all items they created with "
        "Sidekick across tasks, calendar, and notes, call list_sidekick_inventory first and "
        "summarize the JSON for them in plain language. "
        "Then ALWAYS do an inventory interpretation round: transfer to TaskSpecialist to interpret "
        "the tasks section, then transfer to ScheduleSpecialist to interpret the calendar_events "
        "section, then transfer to NotesSpecialist to interpret the notes section. After those "
        "three specialist turns, synthesize a unified next-actions list for the user. "
        "IMPORTANT: During this inventory interpretation round, do not create/update/delete anything "
        "unless the user explicitly asked for changes; provide suggestions only and ask a single "
        "confirmation question before applying any proposed modifications. "
        "Otherwise delegate with transfer_to_agent to exactly one specialist per sub-request. "
        "Specialists prefer Google-backed tools when those tools appear in their tool list "
        "(AlloyDB holds backup copies of Google creates). "
        "After specialists complete sub-parts, synthesize a clear answer for the user."
    ),
    tools=[list_sidekick_inventory],
    sub_agents=[schedule_specialist, notes_specialist, task_specialist],
)
