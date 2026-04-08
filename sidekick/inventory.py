"""Single ADK tool to list Sidekick-tagged tasks, calendar events, and notes across data sources."""

from __future__ import annotations

import json
from typing import Any

from google.adk.tools.tool_context import ToolContext
from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.google_credentials import (
    calendar_api_enabled_in_oauth,
    keep_api_enabled_in_oauth,
    tasks_api_enabled_in_oauth,
)
from sidekick.google_keep_tools import google_keep_list_notes
from sidekick.google_product_tools import (
    google_calendar_list_events,
    google_tasks_list_tasks,
)

_INVENTORY_TIME_MIN_UTC = "1970-01-01T00:00:00Z"


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy result row to a plain dict.

    Args:
        row (Any): Row-like object with ``_mapping``.

    Returns:
        dict[str, Any]: Column names to values.
    """
    return dict(row._mapping)


def _db_list_tasks(owner_sub: str, lim: int) -> list[dict[str, Any]] | dict[str, Any]:
    """List recent tasks from the database for one user.

    Args:
        owner_sub (str): Google subject / ADK user id.
        lim (int): Maximum rows.

    Returns:
        list[dict[str, Any]] | dict[str, Any]: Task rows, or a single error dict on failure.
    """
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, status, due_at, created_at "
                    "FROM sidekick_tasks WHERE owner_sub = :owner "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"owner": owner_sub, "lim": lim},
            )
            return [_row_to_dict(row) for row in r]
    except Exception as e:
        return {"error": "database_tasks", "message": str(e)}


def _db_list_calendar(owner_sub: str, lim: int) -> list[dict[str, Any]] | dict[str, Any]:
    """List recent calendar events from the database for one user.

    Args:
        owner_sub (str): Google subject / ADK user id.
        lim (int): Maximum rows.

    Returns:
        list[dict[str, Any]] | dict[str, Any]: Event rows, or a single error dict on failure.
    """
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, start_at, end_at, notes, created_at, google_event_id "
                    "FROM sidekick_calendar_events WHERE owner_sub = :owner "
                    "ORDER BY start_at DESC LIMIT :lim"
                ),
                {"owner": owner_sub, "lim": lim},
            )
            return [_row_to_dict(row) for row in r]
    except Exception as e:
        return {"error": "database_calendar", "message": str(e)}


def _db_list_notes(owner_sub: str, lim: int) -> list[dict[str, Any]] | dict[str, Any]:
    """List recent notes from the database for one user.

    Args:
        owner_sub (str): Google subject / ADK user id.
        lim (int): Maximum rows.

    Returns:
        list[dict[str, Any]] | dict[str, Any]: Note rows, or a single error dict on failure.
    """
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, body, created_at, google_keep_note_name "
                    "FROM sidekick_notes WHERE owner_sub = :owner "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"owner": owner_sub, "lim": lim},
            )
            return [_row_to_dict(row) for row in r]
    except Exception as e:
        return {"error": "database_notes", "message": str(e)}


def _unwrap_google_list(raw: str, list_key: str) -> list[dict[str, Any]] | dict[str, Any]:
    """Parse JSON from a Google API tool and extract a list field.

    Args:
        raw (str): JSON text returned by another tool.
        list_key (str): Key whose value should be a list (e.g. ``tasks``, ``events``, ``notes``).

    Returns:
        list[dict[str, Any]] | dict[str, Any]: The list, or an error-shaped dict.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid_json", "message": raw[:400]}

    if not isinstance(obj, dict):
        return {"error": "unexpected_shape", "message": type(obj).__name__}

    data = obj.get(list_key)
    if isinstance(data, list):
        return data

    if obj.get("error") is not None:
        err: dict[str, Any] = {"error": obj["error"]}
        for k in ("message", "hint", "status", "details"):
            if k in obj:
                err[k] = obj[k]
        return err

    return {"error": "unexpected_response", "keys": list(obj.keys())}


def list_sidekick_inventory(
    limit_per_domain: int = 30,
    *,
    tool_context: ToolContext,
) -> str:
    """Return a combined inventory of Sidekick-tagged tasks, events, and notes.

    Args:
        limit_per_domain (int): Max items per section (clamped to 1–50).
        tool_context (ToolContext): ADK tool context (provides user id).

    Returns:
        str: JSON with ``sources`` and per-domain data from Google APIs or the database.
    """
    lim = max(1, min(int(limit_per_domain), 50))
    n_tasks = min(lim, 100)
    owner = tool_context.user_id

    sources: dict[str, str] = {}
    out: dict[str, Any] = {"sources": sources}

    if tasks_api_enabled_in_oauth():
        sources["tasks"] = "google"
        raw = google_tasks_list_tasks(
            tasklist_id="@default",
            max_results=n_tasks,
            only_sidekick=True,
            tool_context=tool_context,
        )
        out["tasks"] = _unwrap_google_list(raw, "tasks")
    else:
        sources["tasks"] = "database"
        out["tasks"] = _db_list_tasks(owner, lim)

    if calendar_api_enabled_in_oauth():
        sources["calendar_events"] = "google"
        raw = google_calendar_list_events(
            max_results=lim,
            time_min=_INVENTORY_TIME_MIN_UTC,
            only_sidekick=True,
            tool_context=tool_context,
        )
        out["calendar_events"] = _unwrap_google_list(raw, "events")
    else:
        sources["calendar_events"] = "database"
        out["calendar_events"] = _db_list_calendar(owner, lim)

    if keep_api_enabled_in_oauth():
        sources["notes"] = "google"
        raw = google_keep_list_notes(
            max_results=lim,
            include_untagged=False,
            tool_context=tool_context,
        )
        out["notes"] = _unwrap_google_list(raw, "notes")
    else:
        sources["notes"] = "database"
        out["notes"] = _db_list_notes(owner, lim)

    return json.dumps(out, default=str)
