"""Google Docs API tools: list, create, update, and delete; mirror to the database.

Notes are stored as plain Google Docs. The drive.file scope limits Drive API access to
files created by this app, so no resource-label tagging is needed in titles or bodies.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import text

from sidekick.chat_access import (
    chat_can_use,
    deny_with_title,
    is_admin_bypass,
    lookup_resource_id_by_google_id,
)
from sidekick.db import db_connection
from sidekick.google_credentials import load_credentials_for_google_api
from sidekick.resource_label import ensure_title_tagged

logger = logging.getLogger(__name__)


def _active_chat_id(tool_context: ToolContext) -> Optional[int]:
    """Read the active chat id from ADK session state seeded by the Flask proxy.

    Args:
        tool_context (ToolContext): Current tool execution context.

    Returns:
        Optional[int]: Active chat id, or None when state has not been seeded.
    """
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


def _docs_quick_link(doc_id: str) -> str:
    d = (doc_id or "").strip()
    if d:
        return f"https://docs.google.com/document/d/{d}/edit"
    return "https://docs.google.com/"


def _owner(tool_context: ToolContext) -> str:
    return tool_context.user_id


def _resolve_creds(owner: str) -> tuple[Optional[Credentials], Optional[str]]:
    creds, err = load_credentials_for_google_api(owner)
    if err:
        logger.error("[docs] credential error for owner=%s: %s", owner, err)
        return None, json.dumps(err)
    if creds:
        logger.info("[docs] creds loaded for owner=%s scopes=%s valid=%s", owner, creds.scopes, creds.valid)
    return creds, None


def _backup_doc_to_db(
    owner_sub: str,
    doc_id: str,
    title: str,
    body: Optional[str],
    chat_id: Optional[int],
) -> Optional[str]:
    link = _docs_quick_link(doc_id)
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO sidekick_notes "
                    "(owner_sub, title, body, google_doc_id, google_quick_link, chat_id) "
                    "VALUES (:owner, :title, :body, :doc_id, :link, :cid)"
                ),
                {
                    "owner": owner_sub,
                    "title": title,
                    "body": body,
                    "doc_id": doc_id,
                    "link": link,
                    "cid": chat_id,
                },
            )
    except Exception as e:
        return str(e)
    return None


def _update_doc_in_db(
    owner_sub: str,
    doc_id: str,
    title: Optional[str],
    body: Optional[str],
) -> Optional[str]:
    sets: list[str] = []
    params: dict[str, Any] = {"owner": owner_sub, "doc_id": doc_id}
    if title is not None:
        sets.append("title = :title")
        params["title"] = title
    if body is not None:
        sets.append("body = :body")
        params["body"] = body
    if not sets:
        return None
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "UPDATE sidekick_notes SET "
                    + ", ".join(sets)
                    + " WHERE owner_sub = :owner AND google_doc_id = :doc_id"
                ),
                params,
            )
    except Exception as e:
        return str(e)
    return None


def _delete_doc_from_db(owner_sub: str, doc_id: str) -> Optional[str]:
    try:
        with db_connection() as conn:
            conn.execute(
                text(
                    "DELETE FROM sidekick_notes WHERE owner_sub = :owner "
                    "AND google_doc_id = :doc_id"
                ),
                {"owner": owner_sub, "doc_id": doc_id},
            )
    except Exception as e:
        return str(e)
    return None


def _http_error_payload(exc: HttpError) -> dict[str, Any]:
    try:
        err = json.loads(exc.content.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError):
        err = {"message": str(exc)}
    payload = {"error": "google_api_http", "status": exc.status_code, "details": err}
    logger.error("[docs] HttpError status=%s details=%s", exc.status_code, err)
    return payload


def google_docs_list_notes(
    max_results: int = 20,
    include_untagged: bool = False,
    page_token: Optional[str] = None,
    query: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """List Google Docs notes for the signed-in user (across all chats).

    Listing is intentionally not chat-scoped — the user can refer to any of their
    own docs from any chat. Editing still requires an explicit grant for docs that
    belong to a different chat.

    Args:
        max_results (int): Maximum notes to return (clamped to 1–50).
        include_untagged (bool): Ignored — drive.file scope already limits to app-created docs.
        page_token (Optional[str]): Drive API pagination token from a prior list call.
        query (Optional[str]): Free-text fragment to match against document titles
            (Drive ``name contains`` filter). Use this to find an older doc by topic.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with ``notes`` and ``nextPageToken``, or error JSON.
    """
    owner = _owner(tool_context)
    logger.info(
        "[docs] list_notes owner=%s max_results=%s query=%r",
        owner, max_results, query,
    )
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    n = max(1, min(int(max_results), 50))
    q = "mimeType='application/vnd.google-apps.document' and trashed=false"
    if query:
        # Escape single quotes per Drive query language (double them).
        safe = str(query).replace("\\", "\\\\").replace("'", "\\'")
        q = f"{q} and name contains '{safe}'"

    collected: list[dict[str, Any]] = []
    token: Optional[str] = page_token
    last_next: Optional[str] = None
    safety_pages = 0

    try:
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        while len(collected) < n and safety_pages < 25:
            safety_pages += 1
            kwargs: dict[str, Any] = {
                "q": q,
                "pageSize": min(50, n * 2),
                "fields": "nextPageToken, files(id, name, createdTime, modifiedTime)",
                "orderBy": "modifiedTime desc",
            }
            if token:
                kwargs["pageToken"] = token
            resp = drive_service.files().list(**kwargs).execute()
            last_next = resp.get("nextPageToken")
            for f in resp.get("files") or []:
                collected.append({
                    "doc_id": f.get("id"),
                    "title": f.get("name", ""),
                    "createdTime": f.get("createdTime"),
                    "modifiedTime": f.get("modifiedTime"),
                    "link": _docs_quick_link(f.get("id", "")),
                })
                if len(collected) >= n:
                    break
            if len(collected) >= n:
                break
            token = last_next
            if not token:
                break
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    return json.dumps({
        "notes": collected[:n],
        "nextPageToken": last_next,
    }, default=str)


def _extract_doc_plain_text(doc: dict[str, Any]) -> str:
    """Flatten a Docs API ``documents.get`` response into plain text.

    Args:
        doc (dict[str, Any]): API document resource.

    Returns:
        str: Concatenated text from every ``textRun`` in body order.
    """
    out: list[str] = []
    for el in (doc.get("body") or {}).get("content") or []:
        para = el.get("paragraph")
        if not para:
            continue
        for piece in para.get("elements") or []:
            run = piece.get("textRun")
            if run and isinstance(run.get("content"), str):
                out.append(run["content"])
    return "".join(out)


def google_docs_get_note(
    doc_id: str,
    *,
    tool_context: ToolContext,
) -> str:
    """Fetch the title and full text of a Google Doc note.

    Reads are not chat-scoped: the signed-in user can read any of their own docs
    from any chat. Editing still requires an explicit grant for docs from other
    chats. The first call after a server restart may need to discover the doc by
    listing first; pass an exact ``doc_id`` here.

    Args:
        doc_id (str): Google Docs document id.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON ``{doc_id, title, link, text, resource_id?, home_chat_title?,
        home_chat_is_orphan?}`` or a JSON error string. ``home_chat_title``
        names the doc's home chat for the agent to refer to in conversation;
        the agent must NEVER expose a numeric chat id to the user.
    """
    owner = _owner(tool_context)
    logger.info("[docs] get_note owner=%s doc_id=%s", owner, doc_id)
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    try:
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        meta = drive_service.files().get(fileId=doc_id, fields="id,name").execute()
        doc = docs_service.documents().get(documentId=doc_id).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    plain = _extract_doc_plain_text(doc).rstrip()
    out: dict[str, Any] = {
        "doc_id": doc_id,
        "title": meta.get("name", ""),
        "link": _docs_quick_link(doc_id),
        "text": plain,
    }
    # Annotate the home chat by TITLE (never by id) so the agent can refer to
    # it naturally and ask the user about granting access without leaking
    # internal identifiers.
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT n.id, n.chat_id, c.title "
                    "FROM sidekick_notes n "
                    "LEFT JOIN sidekick_chats c ON c.id = n.chat_id "
                    "WHERE n.owner_sub = :o AND n.google_doc_id = :gid LIMIT 1"
                ),
                {"o": owner, "gid": doc_id},
            ).first()
            if row is not None:
                out["resource_id"] = int(row[0])
                if row[2]:
                    out["home_chat_title"] = row[2]
                elif row[1] is None:
                    out["home_chat_is_orphan"] = True
    except Exception:
        logger.exception("[docs] failed to look up backup row for doc_id=%s", doc_id)
    return json.dumps(out, default=str)


def _lookup_chat_default_doc_id(chat_id: Optional[int], owner_sub: str) -> Optional[str]:
    """Return the most recent Google Doc id for a chat, or None when the chat has no docs.

    Used to enforce "one notes doc per chat" by default — the next save appends
    to this doc rather than creating a new file.

    Args:
        chat_id (Optional[int]): Active chat id; None disables the lookup.
        owner_sub (str): Authenticated user id.

    Returns:
        Optional[str]: Most recent ``google_doc_id`` bound to the chat, or None.
    """
    if chat_id is None:
        return None
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT google_doc_id FROM sidekick_notes "
                    "WHERE owner_sub = :o AND chat_id = :c "
                    "  AND google_doc_id IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": owner_sub, "c": chat_id},
            ).first()
    except Exception:
        logger.exception("[docs] default-doc lookup failed for chat_id=%s", chat_id)
        return None
    return row[0] if row else None


def _append_to_existing_doc(
    doc_id: str,
    heading: Optional[str],
    body: Optional[str],
    owner: str,
    creds: Any,
) -> dict[str, Any]:
    """Append ``heading`` + ``body`` to the end of an existing Google Doc.

    Args:
        doc_id (str): Target doc.
        heading (Optional[str]): Section heading inserted before the body, blank line above.
        body (Optional[str]): Body text appended after the heading.
        owner (str): Authenticated user id (for DB sync).
        creds (Any): Resolved Google credentials.

    Raises:
        HttpError: Surfaces Drive/Docs API errors so the caller can react (e.g., 404).

    Returns:
        dict[str, Any]: Result dict with ``doc_id``, ``title``, ``link``, ``appended=True``.
    """
    docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

    parts: list[str] = []
    h = (heading or "").strip()
    b = body or ""
    if h:
        parts.append(f"\n\n{h}\n")
    elif b:
        parts.append("\n\n")
    if b:
        parts.append(b if b.endswith("\n") else b + "\n")
    entry = "".join(parts)

    current_doc = docs_service.documents().get(documentId=doc_id).execute()
    content = (current_doc.get("body") or {}).get("content") or []
    end_index = content[-1].get("endIndex", 1) - 1 if content else 1
    if entry:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {"insertText": {"location": {"index": end_index}, "text": entry}}
                ]
            },
        ).execute()

    meta = drive_service.files().get(fileId=doc_id, fields="name").execute()
    title_now = meta.get("name", "")

    # Refresh DB body snapshot so the central "Notes" view stays accurate.
    try:
        latest = docs_service.documents().get(documentId=doc_id).execute()
        plain = _extract_doc_plain_text(latest).rstrip()
        _update_doc_in_db(owner, doc_id, None, plain)
    except HttpError as e:
        logger.warning(
            "[docs] failed to refresh DB body for doc_id=%s after append: %s",
            doc_id, e,
        )

    return {
        "doc_id": doc_id,
        "title": title_now,
        "link": _docs_quick_link(doc_id),
        "appended": True,
    }


def google_docs_create_note(
    title: str,
    body: Optional[str] = None,
    force_new: bool = False,
    *,
    tool_context: ToolContext,
) -> str:
    """Save a note. By default appends to the chat's existing notes doc; creates a fresh doc on first save.

    The chat-scoped behavior keeps each chat anchored to ONE growing notes
    document. Combine multi-line content into a single call — bullets, multiple
    items, etc., should all live in ``body`` for one invocation. Only set
    ``force_new=True`` when the user *explicitly* asks for a separate doc.

    Args:
        title (str): When creating a fresh doc, this becomes the doc title;
            when appending, it's used as a section heading inside the doc.
        body (Optional[str]): Full content for this save (may include bullets,
            multiple paragraphs, etc.).
        force_new (bool): When True, always create a new doc (never append).
            Use only when the user explicitly asks for a separate / different /
            new doc.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with doc metadata and optional warning/error fields. The
        ``appended`` field is True when an existing doc was extended.
    """
    owner = _owner(tool_context)
    chat_id = _active_chat_id(tool_context)
    logger.info(
        "[docs] create_note owner=%s chat_id=%s force_new=%s title=%r",
        owner, chat_id, force_new, title,
    )
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    if not force_new:
        existing_doc_id = _lookup_chat_default_doc_id(chat_id, owner)
        if existing_doc_id:
            try:
                appended = _append_to_existing_doc(
                    existing_doc_id, title, body, owner, creds
                )
                return json.dumps(appended, default=str)
            except HttpError as e:
                # 404 means the chat's previous default doc was deleted upstream.
                # Fall through and create a fresh one to host this save.
                if e.status_code == 404:
                    logger.warning(
                        "[docs] chat default doc_id=%s gone (404) — creating fresh",
                        existing_doc_id,
                    )
                else:
                    return json.dumps(_http_error_payload(e))

    try:
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        tagged_title = ensure_title_tagged(title)
        doc = docs_service.documents().create(body={"title": tagged_title}).execute()
        doc_id: str = doc["documentId"]
        logger.info("[docs] created doc_id=%s", doc_id)
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    body_warning: Optional[dict[str, Any]] = None
    if body:
        try:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]},
            ).execute()
        except HttpError as e:
            body_warning = _http_error_payload(e)
            logger.warning(
                "[docs] body insert failed for doc_id=%s: %s", doc_id, body_warning
            )

    out: dict[str, Any] = {
        "doc_id": doc_id,
        "title": tagged_title,
        "link": _docs_quick_link(doc_id),
    }
    if body_warning:
        out["body_warning"] = body_warning

    backup_err = _backup_doc_to_db(owner, doc_id, tagged_title, body, chat_id)
    if backup_err:
        out["backup_error"] = True
        out["backup_message"] = backup_err
    return json.dumps(out, default=str)


def google_docs_update_note(
    doc_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    *,
    tool_context: ToolContext,
) -> str:
    """Update title and/or body of a Google Doc in place.

    Args:
        doc_id (str): Google Docs document id.
        title (Optional[str]): New title.
        body (Optional[str]): New body text (replaces existing content).
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON with updated doc metadata and optional ``backup_error``, or error JSON.
    """
    owner = _owner(tool_context)
    chat_id = _active_chat_id(tool_context)
    logger.info(
        "[docs] update_note owner=%s chat_id=%s doc_id=%s title=%r body_set=%s",
        owner, chat_id, doc_id, title, body is not None,
    )
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    if title is None and body is None:
        return json.dumps({"error": "no_fields", "message": "Provide title and/or body to update."})

    with db_connection() as conn:
        rid = lookup_resource_id_by_google_id(conn, "note", doc_id, owner)
        if rid is not None and not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "note", rid, owner):
            return deny_with_title(conn, "note", rid, owner, google_id=doc_id)

    current_name: str = ""

    try:
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)

        meta = drive_service.files().get(fileId=doc_id, fields="id,name,trashed").execute()
        current_name = meta.get("name", "")

        if body is not None:
            current_doc = docs_service.documents().get(documentId=doc_id).execute()
            content = (current_doc.get("body") or {}).get("content") or []
            end_index = content[-1].get("endIndex", 1) - 1 if content else 0
            requests: list[dict[str, Any]] = []
            if end_index > 1:
                requests.append({
                    "deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}
                })
            requests.append({"insertText": {"location": {"index": 1}, "text": body}})
            docs_service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()

        if title is not None:
            tagged_title = ensure_title_tagged(title)
            drive_service.files().update(fileId=doc_id, body={"name": tagged_title}).execute()
        else:
            tagged_title = None

    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    backup_err = _update_doc_in_db(owner, doc_id, tagged_title, body)
    out: dict[str, Any] = {
        "doc_id": doc_id,
        "title": tagged_title or current_name,
        "link": _docs_quick_link(doc_id),
    }
    if backup_err:
        out["backup_error"] = True
        out["backup_message"] = backup_err
    return json.dumps(out, default=str)


def google_docs_delete_note(doc_id: str, *, tool_context: ToolContext) -> str:
    """Delete a Google Doc by document id.

    Args:
        doc_id (str): Google Docs document id.
        tool_context (ToolContext): ADK tool context.

    Returns:
        str: JSON confirming deletion or a JSON error string.
    """
    owner = _owner(tool_context)
    chat_id = _active_chat_id(tool_context)
    logger.info(
        "[docs] delete_note owner=%s chat_id=%s doc_id=%s", owner, chat_id, doc_id
    )
    creds, err_json = _resolve_creds(owner)
    if err_json:
        return err_json

    with db_connection() as conn:
        rid = lookup_resource_id_by_google_id(conn, "note", doc_id, owner)
        if rid is not None and not is_admin_bypass(tool_context) and not chat_can_use(conn, chat_id, "note", rid, owner):
            return deny_with_title(conn, "note", rid, owner, google_id=doc_id)

    try:
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        drive_service.files().delete(fileId=doc_id).execute()
    except HttpError as e:
        return json.dumps(_http_error_payload(e))

    sync_err = _delete_doc_from_db(owner, doc_id)
    out: dict[str, Any] = {"deleted": True, "doc_id": doc_id}
    if sync_err:
        out["backup_error"] = True
        out["backup_message"] = sync_err
    return json.dumps(out)
