"""Flask blueprint exposing chat CRUD, history, telemetry, and grants."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, jsonify, request, session
from sqlalchemy import text

from sidekick.crypto import decrypt_text, encrypt_text
from sidekick.db import db_connection, get_chat, new_chat
from sidekick.mcp_config import mcp_known_short_prefixes
from sidekick.run_telemetry import fetch_run_telemetry

logger = logging.getLogger(__name__)

ui_chats_bp = Blueprint("ui_chats", __name__, url_prefix="/ui-api")

VALID_RESOURCE_TYPES = ("note", "task", "calendar_event")


def _oauth_configured() -> bool:
    import os

    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    return bool(cid and csec)


def _require_owner() -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    if _oauth_configured():
        sub = session.get("user_sub")
        if not sub:
            return None, (jsonify(error="unauthorized", login="/login/google"), 401)
        return sub, None
    return session.get("user_sub") or "web-ui", None


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _resource_count_for_chat(conn: Any, chat_id: int, owner: str) -> int:
    own = conn.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM sidekick_notes WHERE owner_sub = :o AND chat_id = :c) "
            "+ (SELECT COUNT(*) FROM sidekick_tasks WHERE owner_sub = :o AND chat_id = :c) "
            "+ (SELECT COUNT(*) FROM sidekick_calendar_events WHERE owner_sub = :o AND chat_id = :c) AS n"
        ),
        {"o": owner, "c": chat_id},
    ).scalar() or 0
    grants = conn.execute(
        text("SELECT COUNT(*) FROM sidekick_chat_resource_access WHERE chat_id = :c"),
        {"c": chat_id},
    ).scalar() or 0
    return int(own) + int(grants)


@ui_chats_bp.get("/chats")
def list_chats():
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, title, agent_session_id, archived_at, created_at, updated_at "
                    "FROM sidekick_chats WHERE owner_sub = :o ORDER BY updated_at DESC"
                ),
                {"o": owner},
            ).all()
            items = [_row(r) for r in rows]
            for it in items:
                it["resource_count"] = _resource_count_for_chat(conn, int(it["id"]), owner)
    except Exception as e:
        logger.exception("list_chats failed")
        return jsonify(error="database", message=str(e)), 500
    return Response(json.dumps({"chats": items}, default=str), mimetype="application/json")


@ui_chats_bp.post("/chats")
def create_chat():
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "New chat").strip() or "New chat"
    try:
        with db_connection() as conn:
            row = new_chat(conn, owner, title=title)
    except Exception as e:
        logger.exception("create_chat failed")
        return jsonify(error="database", message=str(e)), 500
    return Response(json.dumps(row, default=str), mimetype="application/json", status=201)


@ui_chats_bp.patch("/chats/<int:chat_id>")
def patch_chat(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    body = request.get_json(silent=True) or {}
    sets: list[str] = []
    params: dict[str, Any] = {"id": chat_id, "o": owner}
    if "title" in body:
        sets.append("title = :title")
        params["title"] = (str(body["title"]).strip() or "Untitled chat")[:200]
    if "archived" in body:
        sets.append("archived_at = NOW()" if bool(body["archived"]) else "archived_at = NULL")
    if not sets:
        return jsonify(error="no_fields"), 400
    sets.append("updated_at = NOW()")
    sql = (
        "UPDATE sidekick_chats SET "
        + ", ".join(sets)
        + " WHERE id = :id AND owner_sub = :o "
        "RETURNING id, title, agent_session_id, archived_at, created_at, updated_at"
    )
    try:
        with db_connection() as conn:
            r = conn.execute(text(sql), params).first()
    except Exception as e:
        logger.exception("patch_chat failed")
        return jsonify(error="database", message=str(e)), 500
    if r is None:
        return jsonify(error="not_found", id=chat_id), 404
    return Response(json.dumps(_row(r), default=str), mimetype="application/json")


@ui_chats_bp.delete("/chats/<int:chat_id>")
def delete_chat(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            existing = get_chat(conn, chat_id, owner)
            if existing is None:
                return jsonify(error="not_found", id=chat_id), 404
            conn.execute(text("DELETE FROM sidekick_chats WHERE id = :id AND owner_sub = :o"), {"id": chat_id, "o": owner})
    except Exception as e:
        logger.exception("delete_chat failed")
        return jsonify(error="database", message=str(e)), 500
    return jsonify(deleted=True, id=chat_id)


@ui_chats_bp.get("/chats/<int:chat_id>/messages")
def list_messages(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
            rows = conn.execute(
                text(
                    "SELECT id, role, content_ciphertext, created_at "
                    "FROM sidekick_chat_messages WHERE chat_id = :c "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"c": chat_id},
            ).all()
    except Exception as e:
        logger.exception("list_messages failed")
        return jsonify(error="database", message=str(e)), 500
    items = []
    for r in rows:
        text_plain = decrypt_text(r._mapping["content_ciphertext"])
        items.append(
            {
                "id": r._mapping["id"],
                "role": r._mapping["role"],
                "text": text_plain if text_plain is not None else "",
                "decrypt_failed": text_plain is None,
                "created_at": r._mapping["created_at"],
            }
        )
    return Response(json.dumps({"messages": items}, default=str), mimetype="application/json")


@ui_chats_bp.get("/chats/<int:chat_id>/telemetry")
def list_telemetry(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    limit_raw = request.args.get("limit", "10")
    try:
        limit = max(1, min(int(limit_raw), 50))
    except Exception:
        limit = 10
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
    except Exception as e:
        logger.exception("list_telemetry ownership check failed")
        return jsonify(error="database", message=str(e)), 500
    runs = fetch_run_telemetry(chat_id, limit=limit)
    return Response(json.dumps({"runs": runs}, default=str), mimetype="application/json")


def append_chat_message(conn: Any, chat_id: int, role: str, text_plain: str) -> int:
    cipher = encrypt_text(text_plain)
    row = conn.execute(
        text(
            "INSERT INTO sidekick_chat_messages "
            "(chat_id, role, content_ciphertext) "
            "VALUES (:c, :r, :ct) RETURNING id"
        ),
        {"c": chat_id, "r": role, "ct": cipher},
    ).first()
    conn.execute(text("UPDATE sidekick_chats SET updated_at = NOW() WHERE id = :c"), {"c": chat_id})
    return int(row[0])


# --- cross-chat resource grants ----------------------------------------------------------


def _resource_owned_by(conn: Any, resource_type: str, resource_id: int, owner: str) -> Optional[int]:
    table = {
        "note": "sidekick_notes",
        "task": "sidekick_tasks",
        "calendar_event": "sidekick_calendar_events",
    }[resource_type]
    row = conn.execute(text(f"SELECT chat_id FROM {table} WHERE id = :id AND owner_sub = :o"), {"id": resource_id, "o": owner}).first()
    if row is None:
        return None
    return row[0]


@ui_chats_bp.get("/chats/<int:chat_id>/access")
def list_grants(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
            rows = conn.execute(
                text(
                    "SELECT g.id, g.chat_id, g.resource_type, g.resource_id, g.granted_at, g.granted_from_chat_id, src.title AS source_chat_title "
                    "FROM sidekick_chat_resource_access g "
                    "LEFT JOIN sidekick_chats src ON src.id = g.granted_from_chat_id "
                    "WHERE g.chat_id = :c ORDER BY g.granted_at DESC"
                ),
                {"c": chat_id},
            ).all()
    except Exception as e:
        logger.exception("list_grants failed")
        return jsonify(error="database", message=str(e)), 500
    return Response(json.dumps({"grants": [_row(r) for r in rows]}, default=str), mimetype="application/json")


@ui_chats_bp.post("/chats/<int:chat_id>/access")
def create_grant(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    body = request.get_json(silent=True) or {}
    rtype = (body.get("resource_type") or "").strip()
    if rtype not in VALID_RESOURCE_TYPES:
        return jsonify(error="invalid_resource_type"), 400
    try:
        rid = int(body.get("resource_id"))
    except (TypeError, ValueError):
        return jsonify(error="invalid_resource_id"), 400
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="chat_not_found", id=chat_id), 404
            home_chat = _resource_owned_by(conn, rtype, rid, owner)
            if home_chat is None:
                return jsonify(error="resource_not_found"), 404
            if home_chat == chat_id:
                return jsonify(error="resource_already_local"), 400
            r = conn.execute(
                text(
                    "INSERT INTO sidekick_chat_resource_access "
                    "(chat_id, resource_type, resource_id, granted_from_chat_id) "
                    "VALUES (:c, :rt, :ri, :gf) "
                    "ON CONFLICT (chat_id, resource_type, resource_id) DO UPDATE SET granted_at = NOW() "
                    "RETURNING id, chat_id, resource_type, resource_id, granted_at, granted_from_chat_id"
                ),
                {"c": chat_id, "rt": rtype, "ri": rid, "gf": home_chat},
            ).first()
    except Exception as e:
        logger.exception("create_grant failed")
        return jsonify(error="database", message=str(e)), 500
    return Response(json.dumps(_row(r), default=str), mimetype="application/json", status=201)


# --- per-chat MCP server grants -----------------------------------------------------------


@ui_chats_bp.get("/mcp/servers")
def list_mcp_servers():
    return Response(json.dumps({"servers": list(mcp_known_short_prefixes())}, default=str), mimetype="application/json")


@ui_chats_bp.get("/chats/<int:chat_id>/mcp")
def list_chat_mcp_grants(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
            rows = conn.execute(
                text("SELECT mcp_prefix FROM sidekick_chat_mcp_access WHERE chat_id = :c ORDER BY mcp_prefix"),
                {"c": chat_id},
            ).all()
    except Exception as e:
        logger.exception("list_chat_mcp_grants failed")
        return jsonify(error="database", message=str(e)), 500
    return Response(json.dumps({"granted": [r[0] for r in rows]}, default=str), mimetype="application/json")


@ui_chats_bp.post("/chats/<int:chat_id>/mcp")
def grant_chat_mcp(chat_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    body = request.get_json(silent=True) or {}
    prefix = (body.get("mcp_prefix") or "").strip().lower()
    if not prefix:
        return jsonify(error="invalid_mcp_prefix"), 400
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
            conn.execute(
                text(
                    "INSERT INTO sidekick_chat_mcp_access (chat_id, mcp_prefix) VALUES (:c, :p) ON CONFLICT (chat_id, mcp_prefix) DO NOTHING"
                ),
                {"c": chat_id, "p": prefix},
            )
    except Exception as e:
        logger.exception("grant_chat_mcp failed")
        return jsonify(error="database", message=str(e)), 500
    return jsonify(granted=True, mcp_prefix=prefix)


@ui_chats_bp.delete("/chats/<int:chat_id>/mcp/<string:mcp_prefix>")
def revoke_chat_mcp(chat_id: int, mcp_prefix: str):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="not_found", id=chat_id), 404
            conn.execute(text("DELETE FROM sidekick_chat_mcp_access WHERE chat_id = :c AND mcp_prefix = :p"), {"c": chat_id, "p": mcp_prefix.strip().lower()})
    except Exception as e:
        logger.exception("revoke_chat_mcp failed")
        return jsonify(error="database", message=str(e)), 500
    return jsonify(deleted=True, mcp_prefix=mcp_prefix)


@ui_chats_bp.delete("/chats/<int:chat_id>/access/<int:grant_id>")
def revoke_grant(chat_id: int, grant_id: int):
    owner, err = _require_owner()
    if err:
        return err[0], err[1]
    assert owner is not None
    try:
        with db_connection() as conn:
            chat = get_chat(conn, chat_id, owner)
            if chat is None:
                return jsonify(error="chat_not_found"), 404
            r = conn.execute(
                text("DELETE FROM sidekick_chat_resource_access WHERE id = :id AND chat_id = :c RETURNING id"),
                {"id": grant_id, "c": chat_id},
            ).first()
    except Exception as e:
        logger.exception("revoke_grant failed")
        return jsonify(error="database", message=str(e)), 500
    if r is None:
        return jsonify(error="not_found", id=grant_id), 404
    return jsonify(deleted=True, id=grant_id)
