"""Google Keep API tools: list, create, update (recreate), and delete; mirror to the database."""

from __future__ import annotations

import json
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import text

from sidekick.db import db_connection
from sidekick.google_credentials import load_credentials_for_google_api
from sidekick.resource_label import ensure_body_lines_tagged, ensure_title_tagged, sidekick_resource_label


def _keep_quick_link(note_name: str) -> str:
    """Build a best-effort end-user link for a Keep note.

    Args:
        note_name (str): Keep resource name (e.g. ``notes/...``).

    Returns:
        str: A Keep web URL (may require the user to be signed in).
    """
    n = (note_name or "").strip()
    if not n:
        return "https://keep.google.com/"
    if n.startswith("notes/") and len(n) > 6:
        return f"https://keep.google.com/#NOTE/{n.split('/', 1)[1]}"
    return "https://keep.google.com/"


def _owner(tool_context: ToolContext) -> str:
    """Return the ADK user id for Google Keep API calls.

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


def _backup_keep_note_to_db(
    owner_sub: str,
    created: dict[str, Any],
    title: str,
    body: Optional[str],
) -> Optional[str]:
    """Insert a backup note row after a successful Google Keep create.

    Args:
        owner_sub (str): Google subject identifier.
        created (dict[str, Any]): API response body from ``notes.create``.
        title (str): Stored title (tagged).
        body (Optional[str]): Stored body (tagged).

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    name = created.get("name")
    if not name:
        return "missing_google_keep_note_name"
    link = _keep_quick_link(str(name))
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO sidekick_notes (owner_sub, title, body, google_keep_note_name, google_quick_link) "
                    "VALUES (:owner, :title, :body, :gk, :link)"
                ),
                {"owner": owner_sub, "title": title, "body": body, "gk": name, "link": link},
            )
    except Exception as e:
        return str(e)
    return None


def _delete_keep_note_from_db(owner_sub: str, note_name: str) -> Optional[str]:
    """Remove backup row for a Keep note resource name.

    Args:
        owner_sub (str): Google subject identifier.
        note_name (str): Keep API note name (``notes/...``).

    Returns:
        Optional[str]: None on success, or an error message string on failure.
    """
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "DELETE FROM sidekick_notes WHERE owner_sub = :owner "
                    "AND google_keep_note_name = :gk"
                ),
                {"owner": owner_sub, "gk": note_name},
            )
    except Exception as e:
        return str(e)
    return None


def _http_error_payload(exc: HttpError) -> dict[str, Any]:
    """Normalize a Google API HTTP error for JSON tool responses.

    Args:
        exc (HttpError): Error raised by googleapiclient.

    Returns:
        dict[str, Any]: Payload with ``error``, ``status``, and ``details`` keys.
    """
    try:
        err = json.loads(exc.content.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError):
        err = {"message": str(exc)}
    return {"error": "google_api_http", "status": exc.status_code, "details": err}


def _keep_body_plain(body: Optional[dict[str, Any]]) -> str:
    """Extract plain text from a Keep API note ``body`` object.

    Args:
        body (Optional[dict[str, Any]]): Keep API body dict (text or list shape).

    Returns:
        str: Concatenated plain text, or empty string.
    """
    if not body:
        return ""
    text_block = body.get("text")
    if isinstance(text_block, dict):
        return (text_block.get("text") or "").strip()
    lst = body.get("list")
    if isinstance(lst, dict):
        items = lst.get("listItems") or []
        parts: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("text")
            if isinstance(t, dict):
                parts.append((t.get("text") or "").strip())
        return "\n".join(parts)
    return ""


def _note_matches_sidekick(note: dict[str, Any]) -> bool:
    """Return whether a Keep note is tagged as Sidekick-owned.

    Args:
        note (dict[str, Any]): Keep API note resource.

    Returns:
        bool: True if the Sidekick label appears in title or serialized body.
    """
    label = sidekick_resource_label()
    title = note.get("title") or ""
    blob = _keep_body_plain(note.get("body"))
    return label in title or label in blob


def _serialize_note(note: dict[str, Any]) -> dict[str, Any]:
    """Shape a Keep API note for compact JSON tool output.

    Args:
        note (dict[str, Any]): Keep API note resource.

    Returns:
        dict[str, Any]: Subset of fields including a short body preview.
    """
    return {
        "name": note.get("name"),
        "title": note.get("title", ""),
        "body_preview": (_keep_body_plain(note.get("body")) or "")[:2000],
        "createTime": note.get("createTime"),
        "updateTime": note.get("updateTime"),
        "trashed": note.get("trashed"),
    }


def google_keep_list_notes(
    max_results: int = 20,
    include_untagged: bool = False,
    page_token: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """List Google Keep notes for the signed-in user.

    Args:
        max_results (int): Maximum notes to return (clamped to 1–50).
        include_untagged (bool): If False, only notes carrying the Sidekick label.
        page_token (Optional[str]): Keep API pagination token from a prior list call.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with ``notes``, ``nextPageToken``, and label metadata, or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    n = max(1, min(int(max_results), 50))
    label = sidekick_resource_label()
    collected: list[dict[str, Any]] = []
    token: Optional[str] = page_token
    last_next: Optional[str] = None
    safety_pages = 0

    try:
        service = build("keep", "v1", credentials=creds, cache_discovery=False)
        while len(collected) < n and safety_pages < 25:
            safety_pages += 1
            kwargs: dict[str, Any] = {"pageSize": 50}
            if token:
                kwargs["pageToken"] = token
            resp = service.notes().list(**kwargs).execute()
            last_next = resp.get("nextPageToken")
            for note in resp.get("notes") or []:
                if include_untagged or _note_matches_sidekick(note):
                    collected.append(_serialize_note(note))
                    if len(collected) >= n:
                        break
            if len(collected) >= n:
                break
            token = last_next
            if not token:
                break
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    out = {
        "notes": collected[:n],
        "sidekick_resource_label": label,
        "nextPageToken": last_next,
    }
    return json.dumps(out, default=str)


def google_keep_create_note(
    title: str,
    body: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Create a Google Keep note with Sidekick tagging on title and body.

    Args:
        title (str): Note title.
        body (Optional[str]): Note body text.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with created note metadata and optional ``backup_error`` fields.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    tagged_title = ensure_title_tagged(title)
    tagged_body = ensure_body_lines_tagged(body)
    note_body: dict[str, Any] = {"text": {"text": tagged_body}}

    try:
        service = build("keep", "v1", credentials=creds, cache_discovery=False)
        created = (
            service.notes()
            .create(body={"title": tagged_title, "body": note_body})
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    out: dict[str, Any] = {
        "name": created.get("name"),
        "title": created.get("title"),
        "sidekick_resource_label": sidekick_resource_label(),
        "createTime": created.get("createTime"),
    }
    backup_err = _backup_keep_note_to_db(owner, created, tagged_title, tagged_body)
    if backup_err:
        out["backup_error"] = True
        out["backup_message"] = backup_err
    return json.dumps(out, default=str)


def google_keep_update_note(
    note_name: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Update a Sidekick-tagged Keep note.

    The Keep API has no patch method; this creates a new note and deletes the old one.

    Args:
        note_name (str): Existing Keep note resource name (e.g. ``notes/...``).
        title (Optional[str]): New title (Sidekick label applied when set).
        body (Optional[str]): New body (Sidekick label applied when set).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with the new note ``name`` and optional ``backup_error``, or error JSON.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    if title is None and body is None:
        return json.dumps(
            {
                "error": "no_fields",
                "message": "Provide title and/or body to update.",
            }
        )

    try:
        service = build("keep", "v1", credentials=creds, cache_discovery=False)
        current = service.notes().get(name=note_name).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    if not _note_matches_sidekick(current):
        return json.dumps(
            {
                "error": "not_sidekick_note",
                "message": "Refusing to modify a note without the Sidekick label.",
            }
        )

    prev_title = (current.get("title") or "").strip()
    prev_plain = _keep_body_plain(current.get("body"))
    new_title = ensure_title_tagged(title) if title is not None else prev_title
    new_body_plain = body if body is not None else prev_plain
    tagged_body = ensure_body_lines_tagged(new_body_plain)
    note_body: dict[str, Any] = {"text": {"text": tagged_body}}

    try:
        created = (
            service.notes()
            .create(body={"title": new_title, "body": note_body})
            .execute()
        )
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    try:
        service.notes().delete(name=note_name).execute()
    except HttpError as e:
        err = _http_error_payload(e)
        err["error"] = "keep_delete_old_failed"
        err["new_note_name"] = created.get("name")
        err["replaced"] = note_name
        return json.dumps(err)

    db_err_old = _delete_keep_note_from_db(owner, note_name)
    backup_err = _backup_keep_note_to_db(owner, created, new_title, tagged_body)
    out: dict[str, Any] = {
        "name": created.get("name"),
        "replaced": note_name,
        "title": created.get("title"),
        "sidekick_resource_label": sidekick_resource_label(),
        "createTime": created.get("createTime"),
    }
    if db_err_old or backup_err:
        out["backup_error"] = True
        out["backup_message"] = db_err_old or backup_err
    return json.dumps(out, default=str)


def google_keep_delete_note(note_name: str, *, tool_context: ToolContext) -> str:
    """Delete a Sidekick-tagged Google Keep note by resource name.

    Args:
        note_name (str): Keep note resource name (e.g. ``notes/...``).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming deletion or a JSON error string.
    """
    owner = _owner(tool_context)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        service = build("keep", "v1", credentials=creds, cache_discovery=False)
        current = service.notes().get(name=note_name).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    if not _note_matches_sidekick(current):
        return json.dumps(
            {
                "error": "not_sidekick_note",
                "message": "Refusing to delete a note without the Sidekick label.",
            }
        )

    try:
        service.notes().delete(name=note_name).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    sync_err = _delete_keep_note_from_db(owner, note_name)
    out: dict[str, Any] = {"deleted": True, "name": note_name}
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out)
