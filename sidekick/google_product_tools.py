"""Google Calendar and Google Tasks API tools with optional AlloyDB backup rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.google_credentials import load_credentials_for_google_api
from sidekick.resource_label import (
    calendar_event_has_label,
    ensure_calendar_description,
    ensure_task_notes,
    ensure_title_tagged,
    sidekick_resource_label,
    task_item_has_label,
)


def _owner(tool_context: ToolContext) -> str:
    """Return the ADK user id for Google API calls.

    Args:
        tool_context (ToolContext): Current tool execution context.

    Returns:
        str: ``user_id`` (Google OAuth ``sub`` when using the Flask proxy).
    """
    return tool_context.user_id


def _resolve_creds(owner: str) -> tuple[Optional[Credentials], Optional[str]]:
    """Resolve OAuth credentials or a JSON error string suitable as a tool return value.

    Args:
        owner (str): Google subject identifier.

    Returns:
        tuple[Optional[Credentials], Optional[str]]: ``(creds, None)`` or ``(None, json_error)``.
    """
    creds, err = load_credentials_for_google_api(owner)
    if err:
        return None, json.dumps(err)
    return creds, None


def _gcal_time_to_sql_string(part: Any) -> Optional[str]:
    """Map a Calendar API ``start``/``end`` object to an ISO string for SQL.

    Args:
        part (Any): API ``start`` or ``end`` dict (``dateTime`` or all-day ``date``).

    Returns:
        Optional[str]: RFC3339-like string, or None if ``part`` is not usable.
    """
    if not isinstance(part, dict):
        return None
    dt = part.get("dateTime")
    if dt:
        return str(dt)
    d = part.get("date")
    if d:
        return f"{d}T00:00:00Z"
    return None


def _normalize_gcal_datetime_string(val: Optional[str]) -> Optional[str]:
    """Normalize user/DB datetime strings for Calendar API ``dateTime`` fields.

    Args:
        val (Optional[str]): RFC3339-like string; may use a space between date and time.

    Returns:
        Optional[str]: String suitable for API ``dateTime``, or unchanged all-day ``YYYY-MM-DD``.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return s
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if "T" not in s and len(s) > 10 and s[10:11] == " ":
        return s[:10] + "T" + s[11:].strip()
    return s


def _tasks_quick_link(tasklist_id: str, task_id: str) -> str:
    """Build a best-effort end-user link for Google Tasks.

    Google does not publish a stable, public URL that opens one task by API id in the
    browser; patterns like ``tasks.google.com/list/{list}/{task}`` return 404. The
    supported web surface is Tasks inside Google Calendar.

    Args:
        tasklist_id (str): Google Tasks task list id (unused; kept for callers).
        task_id (str): Google Tasks task id (unused; kept for callers).

    Returns:
        str: URL that opens Google Tasks in Calendar (signed-in user).
    """
    _ = (tasklist_id, task_id)
    return "https://calendar.google.com/calendar/u/0/r/tasks"


def normalize_stored_task_quick_link(url: Optional[str]) -> Optional[str]:
    """Rewrite known-broken Tasks URLs from older backups to a working Calendar Tasks view.

    Args:
        url (Optional[str]): Stored ``google_quick_link`` value.

    Returns:
        Optional[str]: Canonical Tasks-in-Calendar URL when the old pattern is detected.
    """
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if not u:
        return url
    if "tasks.google.com/list/" in u or "tasks.google.com/embed" in u:
        return _tasks_quick_link("", "")
    if u == "https://calendar.google.com/calendar/r/tasks":
        return _tasks_quick_link("", "")
    return url


def _backup_calendar_event_to_db(
    owner_sub: str,
    created: dict[str, Any],
    fallback_start: str,
    fallback_end: str,
    notes: Optional[str],
) -> Optional[str]:
    """Insert a backup calendar row after a successful Google Calendar create.

    Args:
        owner_sub (str): Google subject identifier.
        created (dict[str, Any]): API response body from ``events.insert``.
        fallback_start (str): Start time string if API shape lacks parseable start.
        fallback_end (str): End time string if API shape lacks parseable end.
        notes (Optional[str]): Description/notes stored locally.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    start_s = _gcal_time_to_sql_string(created.get("start")) or fallback_start
    end_s = _gcal_time_to_sql_string(created.get("end")) or fallback_end
    gid = created.get("id")
    if not gid:
        return "missing_google_event_id"
    title = (created.get("summary") or "").strip() or "(no title)"
    link = created.get("htmlLink")
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO sidekick_calendar_events "
                    "(owner_sub, title, start_at, end_at, notes, google_event_id, google_quick_link) "
                    "VALUES (:owner, :title, CAST(:start_at AS timestamptz), "
                    "CAST(:end_at AS timestamptz), :notes, :gid, :link)"
                ),
                {
                    "owner": owner_sub,
                    "title": title,
                    "start_at": start_s,
                    "end_at": end_s,
                    "notes": notes,
                    "gid": gid,
                    "link": link,
                },
            )
    except Exception as e:
        return str(e)
    return None


def _google_task_status_to_db(g: Optional[str]) -> str:
    """Map a Google Tasks status string to a database status value.

    Args:
        g (Optional[str]): Google Tasks ``status`` field.

    Returns:
        str: ``completed`` or ``open``.
    """
    if g == "completed":
        return "completed"
    return "open"


def _normalize_tasks_api_due(val: Optional[Any]) -> Optional[str]:
    """Normalize a due string for Google Tasks ``insert``/``patch`` (RFC3339).

    PostgreSQL and some serializers emit a space between date and time; the Tasks
    API rejects that with ``invalidArgument``.

    Args:
        val (Optional[Any]): Due from the client or DB.

    Returns:
        Optional[str]: Value safe to send as ``due``, or None to omit (no change).
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null":
        return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if "T" not in s and len(s) > 10 and s[10:11] == " ":
        s = s[:10] + "T" + s[11:].strip()
    return s


def _task_due_to_sql(due: Optional[str]) -> Optional[str]:
    """Normalize a Google Tasks ``due`` value for PostgreSQL ``timestamptz``.

    Args:
        due (Optional[str]): RFC3339 date or date-time from Tasks API.

    Returns:
        Optional[str]: String suitable for ``CAST(... AS timestamptz)``, or None.
    """
    if not due:
        return None
    s = str(due).strip()
    if "T" in s:
        return s
    return f"{s}T00:00:00Z"


def _backup_google_task_to_db(
    owner_sub: str,
    created: dict[str, Any],
    tasklist_id: str,
) -> Optional[str]:
    """Insert a backup task row after a successful Google Tasks create.

    Args:
        owner_sub (str): Google subject identifier.
        created (dict[str, Any]): API response body from ``tasks.insert``.
        tasklist_id (str): Task list id the task was inserted into.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    gtid = created.get("id")
    if not gtid:
        return "missing_google_task_id"
    title = (created.get("title") or "").strip() or "(no title)"
    status = _google_task_status_to_db(created.get("status"))
    due_at = _task_due_to_sql(created.get("due"))
    link = _tasks_quick_link(tasklist_id, str(gtid))
    try:
        with db_connection() as conn:
            if due_at is None:
                conn.execute(
                    text(
                        "INSERT INTO sidekick_tasks (owner_sub, title, status, due_at, "
                        "google_task_id, google_tasklist_id, google_quick_link) "
                        "VALUES (:owner, :title, :status, NULL, :gtid, :gtlist, :link)"
                    ),
                    {
                        "owner": owner_sub,
                        "title": title,
                        "status": status,
                        "gtid": gtid,
                        "gtlist": tasklist_id,
                        "link": link,
                    },
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO sidekick_tasks (owner_sub, title, status, due_at, "
                        "google_task_id, google_tasklist_id, google_quick_link) "
                        "VALUES (:owner, :title, :status, CAST(:due_at AS timestamptz), "
                        ":gtid, :gtlist, :link)"
                    ),
                    {
                        "owner": owner_sub,
                        "title": title,
                        "status": status,
                        "due_at": due_at,
                        "gtid": gtid,
                        "gtlist": tasklist_id,
                        "link": link,
                    },
                )
    except Exception as e:
        return str(e)
    return None


def _sync_google_task_db_from_api(
    owner_sub: str, task: dict[str, Any], tasklist_id: str
) -> Optional[str]:
    """Update AlloyDB backup row to match a Google Tasks resource after patch.

    Args:
        owner_sub (str): Google subject identifier.
        task (dict[str, Any]): Task resource from the Tasks API (after patch/get).
        tasklist_id (str): Task list id.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    gtid = task.get("id")
    if not gtid:
        return "missing_google_task_id"
    title = (task.get("title") or "").strip() or "(no title)"
    status = _google_task_status_to_db(task.get("status"))
    due_at = _task_due_to_sql(task.get("due"))
    link = _tasks_quick_link(tasklist_id, str(gtid))
    try:
        with db_connection() as conn:
            if due_at is None:
                conn.execute(
                    text(
                        "UPDATE sidekick_tasks SET title = :title, status = :status, "
                        "due_at = NULL, google_tasklist_id = :gtlist, google_quick_link = :link "
                        "WHERE owner_sub = :owner AND google_task_id = :gtid"
                    ),
                    {
                        "owner": owner_sub,
                        "title": title,
                        "status": status,
                        "gtlist": tasklist_id,
                        "gtid": gtid,
                        "link": link,
                    },
                )
            else:
                conn.execute(
                    text(
                        "UPDATE sidekick_tasks SET title = :title, status = :status, "
                        "due_at = CAST(:due_at AS timestamptz), google_tasklist_id = :gtlist, "
                        "google_quick_link = :link "
                        "WHERE owner_sub = :owner AND google_task_id = :gtid"
                    ),
                    {
                        "owner": owner_sub,
                        "title": title,
                        "status": status,
                        "due_at": due_at,
                        "gtlist": tasklist_id,
                        "gtid": gtid,
                        "link": link,
                    },
                )
    except Exception as e:
        return str(e)
    return None


def _delete_google_task_from_db(owner_sub: str, google_task_id: str) -> Optional[str]:
    """Remove backup row for a Google task id.

    Args:
        owner_sub (str): Google subject identifier.
        google_task_id (str): Google Tasks task id.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "DELETE FROM sidekick_tasks WHERE owner_sub = :owner "
                    "AND google_task_id = :gtid"
                ),
                {"owner": owner_sub, "gtid": google_task_id},
            )
    except Exception as e:
        return str(e)
    return None


def _sync_calendar_event_db_from_api(
    owner_sub: str, ev: dict[str, Any], description: Optional[str] = None
) -> Optional[str]:
    """Update AlloyDB backup row to match a Calendar event after patch.

    Args:
        owner_sub (str): Google subject identifier.
        ev (dict[str, Any]): Event resource from the Calendar API.
        description (Optional[str]): Stored description if not taken from ``ev``.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    gid = ev.get("id")
    if not gid:
        return "missing_google_event_id"
    start_s = _gcal_time_to_sql_string(ev.get("start"))
    end_s = _gcal_time_to_sql_string(ev.get("end"))
    if not start_s or not end_s:
        return "missing_event_times"
    title = (ev.get("summary") or "").strip() or "(no title)"
    desc = description if description is not None else (ev.get("description") or "")
    link = ev.get("htmlLink")
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "UPDATE sidekick_calendar_events SET title = :title, "
                    "start_at = CAST(:start_at AS timestamptz), "
                    "end_at = CAST(:end_at AS timestamptz), notes = :notes, "
                    "google_quick_link = :link "
                    "WHERE owner_sub = :owner AND google_event_id = :gid"
                ),
                {
                    "owner": owner_sub,
                    "title": title,
                    "start_at": start_s,
                    "end_at": end_s,
                    "notes": desc,
                    "gid": gid,
                    "link": link,
                },
            )
    except Exception as e:
        return str(e)
    return None


def _delete_calendar_event_from_db(owner_sub: str, google_event_id: str) -> Optional[str]:
    """Remove backup row for a Google Calendar event id.

    Args:
        owner_sub (str): Google subject identifier.
        google_event_id (str): Google Calendar event id.

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "DELETE FROM sidekick_calendar_events WHERE owner_sub = :owner "
                    "AND google_event_id = :gid"
                ),
                {"owner": owner_sub, "gid": google_event_id},
            )
    except Exception as e:
        return str(e)
    return None


def _http_error_payload(exc: HttpError) -> dict[str, Any]:
    """Normalize a Google API HTTP error for JSON tool responses.

    Args:
        exc (HttpError): Error raised by googleapiclient.

    Returns:
        dict[str, Any]: Payload with ``error``, ``message``, ``status``, and ``details`` keys.
    """
    try:
        err = json.loads(exc.content.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError):
        err = {"message": str(exc)}
    msg = _google_api_error_user_message(err)
    if not msg:
        msg = f"Google API HTTP {exc.status_code}"
    return {
        "error": "google_api_http",
        "message": msg,
        "status": exc.status_code,
        "details": err,
    }


def _google_api_error_user_message(parsed: Any) -> str:
    """Extract a short user-facing message from a Google API error JSON body.

    Args:
        parsed (Any): Parsed JSON body from a failed API call (usually a dict).

    Returns:
        str: First useful ``message`` field found, or an empty string if none.
    """
    if not isinstance(parsed, dict):
        return ""
    er = parsed.get("error")
    if isinstance(er, dict):
        if er.get("message"):
            return str(er["message"])
        for item in er.get("errors") or []:
            if isinstance(item, dict) and item.get("message"):
                return str(item["message"])
    if parsed.get("message"):
        return str(parsed["message"])
    return ""


def google_calendar_list_events(
    max_results: int = 10,
    time_min: Optional[str] = None,
    only_sidekick: bool = False,
    *,
    tool_context: ToolContext,
) -> str:
    """List events from the user's primary Google Calendar.

    Args:
        max_results (int): Maximum events (clamped to 1–50).
        time_min (Optional[str]): RFC3339 UTC lower bound; default is current UTC time.
        only_sidekick (bool): If True, keep only events tagged by Sidekick.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON string of events and label metadata, or a JSON error string.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    n = max(1, min(int(max_results), 50))
    if time_min:
        tmin = time_min
    else:
        tmin = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=tmin,
                maxResults=n,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))
    except Exception as e:
        return json.dumps({"error": "calendar_list_failed", "message": str(e)})

    items = []
    for ev in resp.get("items") or []:
        if only_sidekick and not calendar_event_has_label(ev):
            continue
        start = ev.get("start", {})
        end = ev.get("end", {})
        items.append(
            {
                "id": ev.get("id"),
                "summary": ev.get("summary", ""),
                "description": (ev.get("description") or "")[:2000],
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "htmlLink": ev.get("htmlLink"),
            }
        )
    return json.dumps(
        {"events": items, "sidekick_resource_label": sidekick_resource_label()}
    )


def google_calendar_create_event(
    summary: str,
    start_at: str,
    end_at: str,
    description: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create an event on the user's primary Google Calendar.

    Args:
        summary (str): Event title (Sidekick label applied).
        start_at (str): Start as RFC3339 ``dateTime`` (with offset or ``Z``).
        end_at (str): End as RFC3339 ``dateTime``.
        description (Optional[str]): Event description (Sidekick label applied).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with created event fields and optional ``backup_error`` fields.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    tagged_summary = ensure_title_tagged(summary)
    desc_stored = ensure_calendar_description(description)
    body: dict[str, Any] = {
        "summary": tagged_summary,
        "start": {"dateTime": start_at},
        "end": {"dateTime": end_at},
        "description": desc_stored,
        "extendedProperties": {
            "private": {"sidekick_label": sidekick_resource_label()},
        },
    }

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        created = service.events().insert(calendarId="primary", body=body).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))
    except Exception as e:
        return json.dumps(
            {
                "error": "calendar_create_failed",
                "message": str(e),
                "hint": "Try sanitize_schedule_times_to_utc with the user's time text, then retry with the UTC strings.",
            }
        )

    out: dict[str, Any] = {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "start": created.get("start"),
        "end": created.get("end"),
        "htmlLink": created.get("htmlLink"),
    }
    backup_err = _backup_calendar_event_to_db(
        owner, created, start_at, end_at, desc_stored
    )
    if backup_err:
        out["backup_error"] = True
        out["backup_message"] = backup_err
    return json.dumps(out, default=str)


def google_calendar_update_event(
    event_id: str,
    summary: Optional[str] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    description: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Patch a primary-calendar event that is tagged as Sidekick-owned.

    Args:
        event_id (str): Google Calendar event id.
        summary (Optional[str]): New title (Sidekick label applied when set).
        start_at (Optional[str]): New start RFC3339 ``dateTime`` (or date for all-day).
        end_at (Optional[str]): New end RFC3339 ``dateTime`` (or date for all-day).
        description (Optional[str]): New description (Sidekick label applied when set).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with updated event fields and optional ``backup_error``, or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        ev = (
            service.events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))
    except Exception as e:
        return json.dumps({"error": "calendar_get_failed", "message": str(e)})

    if not calendar_event_has_label(ev):
        return json.dumps(
            {
                "error": "not_sidekick_event",
                "message": "Refusing to modify an event without the Sidekick label.",
            }
        )

    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = ensure_title_tagged(summary)
    if description is not None:
        body["description"] = ensure_calendar_description(description)

    if start_at is not None or end_at is not None:
        st = ev.get("start") or {}
        en = ev.get("end") or {}
        use_date = bool(st.get("date")) and not st.get("dateTime")
        rs = start_at
        re = end_at
        if rs is None:
            rs = st.get("dateTime") or st.get("date")
        if re is None:
            re = en.get("dateTime") or en.get("date")
        if use_date:
            body["start"] = {"date": (rs or "")[:10]}
            body["end"] = {"date": (re or "")[:10]}
        else:
            rs = _normalize_gcal_datetime_string(rs) if rs is not None else rs
            re = _normalize_gcal_datetime_string(re) if re is not None else re
            body["start"] = {"dateTime": rs}
            body["end"] = {"dateTime": re}
            tz = st.get("timeZone") or en.get("timeZone")
            if tz:
                body["start"]["timeZone"] = tz
                body["end"]["timeZone"] = tz

    if not body:
        return json.dumps(
            {"error": "no_fields", "message": "Provide at least one field to update."}
        )

    patch_kwargs: dict[str, Any] = {
        "calendarId": "primary",
        "eventId": event_id,
        "body": body,
    }
    if ev.get("conferenceData"):
        patch_kwargs["conferenceDataVersion"] = 1

    try:
        updated = service.events().patch(**patch_kwargs).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))
    except Exception as e:
        return json.dumps({"error": "calendar_patch_failed", "message": str(e)})

    desc_stored = (
        ensure_calendar_description(description)
        if description is not None
        else (updated.get("description") or "")
    )
    out: dict[str, Any] = {
        "id": updated.get("id"),
        "summary": updated.get("summary"),
        "start": updated.get("start"),
        "end": updated.get("end"),
        "htmlLink": updated.get("htmlLink"),
    }
    sync_err = _sync_calendar_event_db_from_api(owner, updated, desc_stored)
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out, default=str)


def google_calendar_delete_event(event_id: str, *, tool_context: ToolContext) -> str:
    """Delete a primary-calendar event that is tagged as Sidekick-owned.

    Args:
        event_id (str): Google Calendar event id.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming deletion or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        ev = (
            service.events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))
    except Exception as e:
        return json.dumps({"error": "calendar_get_failed", "message": str(e)})

    if not calendar_event_has_label(ev):
        return json.dumps(
            {
                "error": "not_sidekick_event",
                "message": "Refusing to delete an event without the Sidekick label.",
            }
        )

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    sync_err = _delete_calendar_event_from_db(owner, event_id)
    out: dict[str, Any] = {"deleted": True, "id": event_id}
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out)


def google_tasks_list_tasklists(*, tool_context: ToolContext) -> str:
    """List the signed-in user's Google Task lists.

    Args:
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with ``tasklists`` (id and title), or a JSON error string.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        service = build("tasks", "v1", credentials=creds, cache_discovery=False)
        resp = service.tasklists().list(maxResults=50).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    lists = [
        {"id": x.get("id"), "title": x.get("title", "")}
        for x in (resp.get("items") or [])
    ]
    return json.dumps({"tasklists": lists})


def google_tasks_list_tasks(
    tasklist_id: str = "@default",
    max_results: int = 20,
    only_sidekick: bool = False,
    *,
    tool_context: ToolContext,
) -> str:
    """List tasks in a Google Task list.

    Args:
        tasklist_id (str): Task list id (default ``@default``).
        max_results (int): Maximum tasks (clamped to 1–100).
        only_sidekick (bool): If True, keep only tasks tagged by Sidekick.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with ``tasks`` and label metadata, or a JSON error string.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    n = max(1, min(int(max_results), 100))
    try:
        service = build("tasks", "v1", credentials=creds, cache_discovery=False)
        resp = (
            service.tasks()
            .list(tasklist=tasklist_id, maxResults=n, showCompleted=False)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    items = []
    for t in resp.get("items") or []:
        if only_sidekick and not task_item_has_label(t):
            continue
        items.append(
            {
                "id": t.get("id"),
                "title": t.get("title", ""),
                "notes": (t.get("notes") or "")[:2000],
                "status": t.get("status"),
                "due": t.get("due"),
            }
        )
    return json.dumps(
        {"tasks": items, "sidekick_resource_label": sidekick_resource_label()}
    )


def google_tasks_create_task(
    title: str,
    tasklist_id: str = "@default",
    notes: Optional[str] = None,
    due_rfc3339: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create a task in Google Tasks.

    Args:
        title (str): Task title (Sidekick label applied).
        tasklist_id (str): Target list id (default ``@default``).
        notes (Optional[str]): Task notes (Sidekick label applied).
        due_rfc3339 (Optional[str]): Due date/time in RFC3339, if any.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with created task fields and optional ``backup_error`` fields.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    body: dict[str, Any] = {
        "title": ensure_title_tagged(title),
        "notes": ensure_task_notes(notes),
    }
    due_norm = _normalize_tasks_api_due(due_rfc3339)
    if due_norm:
        body["due"] = due_norm

    try:
        service = build("tasks", "v1", credentials=creds, cache_discovery=False)
        created = service.tasks().insert(tasklist=tasklist_id, body=body).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    out: dict[str, Any] = {
        "id": created.get("id"),
        "title": created.get("title"),
        "notes": created.get("notes"),
        "due": created.get("due"),
        "status": created.get("status"),
        "tasklist_id": tasklist_id,
    }
    backup_err = _backup_google_task_to_db(owner, created, tasklist_id)
    if backup_err:
        out["backup_error"] = True
        out["backup_message"] = backup_err
    return json.dumps(out, default=str)


def google_tasks_update_task(
    task_id: str,
    tasklist_id: str = "@default",
    title: Optional[str] = None,
    notes: Optional[str] = None,
    due_rfc3339: Optional[str] = None,
    status: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Patch a Google Task that carries the Sidekick label (title or notes).

    Args:
        task_id (str): Google Tasks task id.
        tasklist_id (str): Task list id (default ``@default``).
        title (Optional[str]): New title (Sidekick label applied when set).
        notes (Optional[str]): New notes (Sidekick label applied when set).
        due_rfc3339 (Optional[str]): New due date/time in RFC3339, if changing.
        status (Optional[str]): ``needsAction`` or ``completed`` when changing status.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with updated task fields and optional ``backup_error``, or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    if status is not None and status not in ("needsAction", "completed"):
        return json.dumps(
            {
                "error": "invalid_status",
                "message": "status must be needsAction or completed when set.",
            }
        )

    try:
        service = build("tasks", "v1", credentials=creds, cache_discovery=False)
        existing = (
            service.tasks()
            .get(tasklist=tasklist_id, task=task_id)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    if not task_item_has_label(existing):
        return json.dumps(
            {
                "error": "not_sidekick_task",
                "message": "Refusing to modify a task without the Sidekick label.",
            }
        )

    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = ensure_title_tagged(title)
    if notes is not None:
        body["notes"] = ensure_task_notes(notes)
    if due_rfc3339 is not None:
        due_norm = _normalize_tasks_api_due(due_rfc3339)
        if due_norm is not None:
            body["due"] = due_norm
    if status is not None:
        body["status"] = status

    if not body:
        return json.dumps(
            {"error": "no_fields", "message": "Provide at least one field to update."}
        )

    try:
        updated = (
            service.tasks()
            .patch(tasklist=tasklist_id, task=task_id, body=body)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    out: dict[str, Any] = {
        "id": updated.get("id"),
        "title": updated.get("title"),
        "notes": updated.get("notes"),
        "due": updated.get("due"),
        "status": updated.get("status"),
        "tasklist_id": tasklist_id,
    }
    sync_err = _sync_google_task_db_from_api(owner, updated, tasklist_id)
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out, default=str)


def google_tasks_delete_task(
    task_id: str,
    tasklist_id: str = "@default",
    *,
    tool_context: ToolContext,
) -> str:
    """Delete a Google Task that carries the Sidekick label.

    Args:
        task_id (str): Google Tasks task id.
        tasklist_id (str): Task list id (default ``@default``).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming deletion or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        service = build("tasks", "v1", credentials=creds, cache_discovery=False)
        existing = (
            service.tasks()
            .get(tasklist=tasklist_id, task=task_id)
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    if not task_item_has_label(existing):
        return json.dumps(
            {
                "error": "not_sidekick_task",
                "message": "Refusing to delete a task without the Sidekick label.",
            }
        )

    try:
        service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    sync_err = _delete_google_task_from_db(owner, task_id)
    out: dict[str, Any] = {
        "deleted": True,
        "id": task_id,
        "tasklist_id": tasklist_id,
    }
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out)
