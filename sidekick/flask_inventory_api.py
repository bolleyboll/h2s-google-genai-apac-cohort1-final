"""Flask JSON routes for tabbed inventory UI: DB-backed lists and PATCH/DELETE with Google or DB paths."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, jsonify, request, session
from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.google_credentials import (
    calendar_api_enabled_in_oauth,
    keep_api_enabled_in_oauth,
    tasks_api_enabled_in_oauth,
)
from sidekick.google_keep_tools import google_keep_delete_note, google_keep_update_note
from sidekick.google_product_tools import (
    google_calendar_delete_event,
    google_calendar_update_event,
    google_tasks_delete_task,
    google_tasks_update_task,
    normalize_stored_task_quick_link,
)
from sidekick.resource_label import ensure_body_lines_tagged, ensure_title_tagged

ui_api_bp = Blueprint("ui_inventory", __name__, url_prefix="/ui-api")

_INVENTORY_LIMIT = 200


def _oauth_configured() -> bool:
    """Return whether Google OAuth client credentials are configured for this process.

    Returns:
        bool: True if both ``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET`` are set.
    """
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    return bool(cid and csec)


def _tool_context(owner_sub: str) -> SimpleNamespace:
    """Build a minimal ADK-like context object for calling Google API tool functions.

    Args:
        owner_sub (str): Google subject id (``sub``) or placeholder when OAuth is off.

    Returns:
        SimpleNamespace: Object with ``user_id`` set to ``owner_sub``.
    """
    return SimpleNamespace(user_id=owner_sub)


def _require_owner() -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    """Resolve the current user id for UI API routes, or an error response tuple.

    Returns:
        Tuple[Optional[str], Optional[Tuple[Any, int]]]: ``(owner_sub, None)`` on success, or
        ``(None, (response, status))`` when OAuth is on but the session is missing ``user_sub``.
    """
    if _oauth_configured():
        sub = session.get("user_sub")
        if not sub:
            return None, (jsonify(error="unauthorized", login="/login/google"), 401)
        return sub, None
    return session.get("user_sub") or "web-ui", None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy row to a plain dict for JSON serialization.

    Args:
        row (Any): Row-like object with ``_mapping``.

    Returns:
        dict[str, Any]: Column names mapped to values.
    """
    return dict(row._mapping)


def _json_body() -> dict[str, Any]:
    """Parse the current Flask request body as a JSON object when possible.

    Returns:
        dict[str, Any]: Parsed object, or an empty dict if missing or invalid.
    """
    if not request.data:
        return {}
    try:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json_response_from_tool(raw: str) -> Tuple[Any, int]:
    """Turn a JSON string from an ADK tool into a Flask ``jsonify`` response and HTTP status.

    Args:
        raw (str): JSON text returned by a Google or DB-backed tool.

    Returns:
        Tuple[Any, int]: ``(flask.Response, status_code)``; 404 for ``not_found``-style errors.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify(error="invalid_tool_response", message=raw[:500]), 500
    if isinstance(obj, dict) and obj.get("error"):
        status = 400
        if obj.get("error") in ("not_found", "not_sidekick_task", "not_sidekick_event", "not_sidekick_note"):
            status = 404
        return jsonify(obj), status
    return jsonify(obj), 200


@ui_api_bp.get("/inventory/tasks")
def inventory_tasks():
    """List task rows for the signed-in user with Google Tasks API enablement flag.

    Returns:
        Response: JSON with ``google_api_enabled`` and ``items`` (HTTP 200), or error JSON.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, status, due_at, created_at, google_task_id, "
                    "google_tasklist_id, google_quick_link FROM sidekick_tasks WHERE owner_sub = :owner "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"owner": owner, "lim": _INVENTORY_LIMIT},
            )
            items = [_row_to_dict(row) for row in r]
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    for it in items:
        it["google_quick_link"] = normalize_stored_task_quick_link(it.get("google_quick_link"))
    payload = {
        "google_api_enabled": tasks_api_enabled_in_oauth(),
        "items": items,
    }
    return Response(
        json.dumps(payload, default=str),
        mimetype="application/json",
    )


@ui_api_bp.get("/inventory/calendar")
def inventory_calendar():
    """List calendar event rows for the signed-in user with Calendar API enablement flag.

    Returns:
        Response: JSON with ``google_api_enabled`` and ``items`` (HTTP 200), or error JSON.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, start_at, end_at, notes, created_at, google_event_id "
                    ", google_quick_link FROM sidekick_calendar_events WHERE owner_sub = :owner "
                    "ORDER BY start_at DESC LIMIT :lim"
                ),
                {"owner": owner, "lim": _INVENTORY_LIMIT},
            )
            items = [_row_to_dict(row) for row in r]
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    payload = {
        "google_api_enabled": calendar_api_enabled_in_oauth(),
        "items": items,
    }
    return Response(
        json.dumps(payload, default=str),
        mimetype="application/json",
    )


@ui_api_bp.get("/inventory/notes")
def inventory_notes():
    """List note rows for the signed-in user with Keep API enablement flag.

    Returns:
        Response: JSON with ``google_api_enabled`` and ``items`` (HTTP 200), or error JSON.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "SELECT id, title, body, created_at, google_keep_note_name, google_quick_link "
                    "FROM sidekick_notes WHERE owner_sub = :owner "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"owner": owner, "lim": _INVENTORY_LIMIT},
            )
            items = [_row_to_dict(row) for row in r]
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    payload = {
        "google_api_enabled": keep_api_enabled_in_oauth(),
        "items": items,
    }
    return Response(
        json.dumps(payload, default=str),
        mimetype="application/json",
    )


# --- Google-backed mutations ---


@ui_api_bp.patch("/google/tasks/<google_task_id>")
def patch_google_task(google_task_id: str):
    """Patch a Google Task via ``google_tasks_update_task`` when Tasks API is enabled.

    Args:
        google_task_id (str): Google Tasks task id from the URL.

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not tasks_api_enabled_in_oauth():
        return jsonify(error="google_tasks_disabled"), 400
    body = _json_body()
    tasklist_id = request.args.get("tasklist_id") or body.get("tasklist_id") or "@default"
    raw = google_tasks_update_task(
        task_id=google_task_id,
        tasklist_id=tasklist_id,
        title=body.get("title"),
        notes=body.get("notes"),
        due_rfc3339=body.get("due_rfc3339"),
        status=body.get("status"),
        tool_context=_tool_context(owner),
    )
    return _json_response_from_tool(raw)


@ui_api_bp.delete("/google/tasks/<google_task_id>")
def delete_google_task(google_task_id: str):
    """Delete a Google Task via ``google_tasks_delete_task`` when Tasks API is enabled.

    Args:
        google_task_id (str): Google Tasks task id from the URL.

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not tasks_api_enabled_in_oauth():
        return jsonify(error="google_tasks_disabled"), 400
    tasklist_id = request.args.get("tasklist_id") or "@default"
    raw = google_tasks_delete_task(
        task_id=google_task_id,
        tasklist_id=tasklist_id,
        tool_context=_tool_context(owner),
    )
    return _json_response_from_tool(raw)


@ui_api_bp.patch("/google/calendar/<event_id>")
def patch_google_calendar(event_id: str):
    """Patch a Google Calendar event via ``google_calendar_update_event`` when Calendar is enabled.

    Args:
        event_id (str): Google Calendar event id from the URL.

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not calendar_api_enabled_in_oauth():
        return jsonify(error="google_calendar_disabled"), 400
    body = _json_body()
    raw = google_calendar_update_event(
        event_id=event_id,
        summary=body.get("summary"),
        start_at=body.get("start_at"),
        end_at=body.get("end_at"),
        description=body.get("description"),
        tool_context=_tool_context(owner),
    )
    return _json_response_from_tool(raw)


@ui_api_bp.delete("/google/calendar/<event_id>")
def delete_google_calendar(event_id: str):
    """Delete a Google Calendar event via ``google_calendar_delete_event`` when Calendar is enabled.

    Args:
        event_id (str): Google Calendar event id from the URL.

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not calendar_api_enabled_in_oauth():
        return jsonify(error="google_calendar_disabled"), 400
    raw = google_calendar_delete_event(event_id, tool_context=_tool_context(owner))
    return _json_response_from_tool(raw)


@ui_api_bp.patch("/google/notes/<path:note_name>")
def patch_google_note(note_name: str):
    """Update a Keep note via ``google_keep_update_note`` when Keep API is enabled.

    Args:
        note_name (str): Keep API resource name (for example ``notes/...``).

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not keep_api_enabled_in_oauth():
        return jsonify(error="google_keep_disabled"), 400
    body = _json_body()
    raw = google_keep_update_note(
        note_name=note_name,
        title=body.get("title"),
        body=body.get("body"),
        tool_context=_tool_context(owner),
    )
    return _json_response_from_tool(raw)


@ui_api_bp.delete("/google/notes/<path:note_name>")
def delete_google_note(note_name: str):
    """Delete a Keep note via ``google_keep_delete_note`` when Keep API is enabled.

    Args:
        note_name (str): Keep API resource name (for example ``notes/...``).

    Returns:
        Tuple[Any, int]: JSON response and status from ``_json_response_from_tool``.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if not keep_api_enabled_in_oauth():
        return jsonify(error="google_keep_disabled"), 400
    raw = google_keep_delete_note(note_name, tool_context=_tool_context(owner))
    return _json_response_from_tool(raw)


# --- DB-only mutations ---


@ui_api_bp.patch("/db/tasks/<int:task_id>")
def patch_db_task(task_id: int):
    """Patch a ``sidekick_tasks`` row when Google Tasks API is disabled (database-only mode).

    Args:
        task_id (int): Primary key of the task row.

    Returns:
        Response | Tuple[Any, int]: Updated row JSON, or error JSON with appropriate status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if tasks_api_enabled_in_oauth():
        return jsonify(error="use_google_task_endpoint"), 400
    body = _json_body()
    sets: list[str] = []
    params: dict[str, Any] = {"id": task_id, "owner": owner}
    if "title" in body:
        sets.append("title = :title")
        params["title"] = ensure_title_tagged(str(body["title"]))
    if "status" in body:
        sets.append("status = :status")
        params["status"] = str(body["status"])
    if "due_at" in body:
        if body["due_at"] is None or body["due_at"] == "":
            sets.append("due_at = NULL")
        else:
            sets.append("due_at = CAST(:due_at AS timestamptz)")
            params["due_at"] = str(body["due_at"])
    if not sets:
        return jsonify(error="no_fields", message="Provide title, status, and/or due_at."), 400
    sql = (
        "UPDATE sidekick_tasks SET "
        + ", ".join(sets)
        + " WHERE id = :id AND owner_sub = :owner "
        "RETURNING id, title, status, due_at, created_at, google_task_id, google_tasklist_id, google_quick_link"
    )
    try:
        with db_connection() as conn:
            r = conn.execute(text(sql), params)
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=task_id), 404
    out = _row_to_dict(row)
    out["google_quick_link"] = normalize_stored_task_quick_link(out.get("google_quick_link"))
    return Response(
        json.dumps(out, default=str),
        mimetype="application/json",
    )


@ui_api_bp.delete("/db/tasks/<int:task_id>")
def delete_db_task(task_id: int):
    """Delete a ``sidekick_tasks`` row when Google Tasks API is disabled.

    Args:
        task_id (int): Primary key of the task row.

    Returns:
        Tuple[Any, int]: ``jsonify`` payload and HTTP status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if tasks_api_enabled_in_oauth():
        return jsonify(error="use_google_task_endpoint"), 400
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "DELETE FROM sidekick_tasks WHERE id = :id AND owner_sub = :owner "
                    "RETURNING id"
                ),
                {"id": task_id, "owner": owner},
            )
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=task_id), 404
    return jsonify(deleted=True, id=task_id), 200


@ui_api_bp.patch("/db/calendar/<int:event_id>")
def patch_db_calendar(event_id: int):
    """Patch a ``sidekick_calendar_events`` row when Google Calendar API is disabled.

    Args:
        event_id (int): Primary key of the calendar event row.

    Returns:
        Response | Tuple[Any, int]: Updated row JSON, or error JSON with appropriate status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if calendar_api_enabled_in_oauth():
        return jsonify(error="use_google_calendar_endpoint"), 400
    body = _json_body()
    if not any(k in body for k in ("title", "start_at", "end_at", "notes")):
        return jsonify(error="no_fields", message="Provide title, start_at, end_at, and/or notes."), 400
    sets: list[str] = []
    params: dict[str, Any] = {"id": event_id, "owner": owner}
    if "title" in body:
        sets.append("title = :title")
        params["title"] = ensure_title_tagged(str(body["title"]))
    if "start_at" in body:
        sets.append("start_at = CAST(:start_at AS timestamptz)")
        params["start_at"] = str(body["start_at"])
    if "end_at" in body:
        sets.append("end_at = CAST(:end_at AS timestamptz)")
        params["end_at"] = str(body["end_at"])
    if "notes" in body:
        sets.append("notes = :notes")
        params["notes"] = ensure_body_lines_tagged(body.get("notes"))
    sql = (
        "UPDATE sidekick_calendar_events SET "
        + ", ".join(sets)
        + " WHERE id = :id AND owner_sub = :owner "
        "RETURNING id, title, start_at, end_at, notes, created_at, google_event_id, google_quick_link"
    )
    try:
        with db_connection() as conn:
            r = conn.execute(text(sql), params)
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=event_id), 404
    return Response(
        json.dumps(_row_to_dict(row), default=str),
        mimetype="application/json",
    )


@ui_api_bp.delete("/db/calendar/<int:event_id>")
def delete_db_calendar(event_id: int):
    """Delete a ``sidekick_calendar_events`` row when Google Calendar API is disabled.

    Args:
        event_id (int): Primary key of the calendar event row.

    Returns:
        Tuple[Any, int]: ``jsonify`` payload and HTTP status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if calendar_api_enabled_in_oauth():
        return jsonify(error="use_google_calendar_endpoint"), 400
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "DELETE FROM sidekick_calendar_events WHERE id = :id AND owner_sub = :owner "
                    "RETURNING id"
                ),
                {"id": event_id, "owner": owner},
            )
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=event_id), 404
    return jsonify(deleted=True, id=event_id), 200


@ui_api_bp.patch("/db/notes/<int:note_id>")
def patch_db_note(note_id: int):
    """Patch a ``sidekick_notes`` row when Google Keep API is disabled.

    Args:
        note_id (int): Primary key of the note row.

    Returns:
        Response | Tuple[Any, int]: Updated row JSON, or error JSON with appropriate status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if keep_api_enabled_in_oauth():
        return jsonify(error="use_google_note_endpoint"), 400
    body = _json_body()
    if "title" not in body and "body" not in body:
        return jsonify(error="no_fields", message="Provide title and/or body."), 400
    sets: list[str] = []
    params: dict[str, Any] = {"id": note_id, "owner": owner}
    if "title" in body:
        sets.append("title = :title")
        params["title"] = ensure_title_tagged(str(body["title"]))
    if "body" in body:
        sets.append("body = :body")
        b = body.get("body")
        params["body"] = ensure_body_lines_tagged(b if b is None else str(b))
    sql = (
        "UPDATE sidekick_notes SET "
        + ", ".join(sets)
        + " WHERE id = :id AND owner_sub = :owner "
        "RETURNING id, title, body, created_at, google_keep_note_name, google_quick_link"
    )
    try:
        with db_connection() as conn:
            r = conn.execute(text(sql), params)
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=note_id), 404
    return Response(
        json.dumps(_row_to_dict(row), default=str),
        mimetype="application/json",
    )


@ui_api_bp.delete("/db/notes/<int:note_id>")
def delete_db_note(note_id: int):
    """Delete a ``sidekick_notes`` row when Google Keep API is disabled.

    Args:
        note_id (int): Primary key of the note row.

    Returns:
        Tuple[Any, int]: ``jsonify`` payload and HTTP status.
    """
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    if keep_api_enabled_in_oauth():
        return jsonify(error="use_google_note_endpoint"), 400
    try:
        with db_connection() as conn:
            r = conn.execute(
                text(
                    "DELETE FROM sidekick_notes WHERE id = :id AND owner_sub = :owner "
                    "RETURNING id"
                ),
                {"id": note_id, "owner": owner},
            )
            row = r.fetchone()
    except Exception as e:
        return jsonify(error="database", message=str(e)), 500
    if row is None:
        return jsonify(error="not_found", id=note_id), 404
    return jsonify(deleted=True, id=note_id), 200
