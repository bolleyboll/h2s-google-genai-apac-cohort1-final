"""Normalize and persist live agent run telemetry.

The Flask proxy already sees the full `/api/run` / `/api/run_sse` response.
This module turns that raw ADK event stream into a structured timeline the UI
can render, and stores it in Postgres so the right rail can poll it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text

from sidekick.db import db_connection

logger = logging.getLogger(__name__)


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


def decode_run_events(raw: bytes, content_type: str = "") -> list[dict[str, Any]]:
    """Decode a run response into a list of ADK event dicts.

    Supports plain JSON arrays and a conservative SSE parser that looks for
    `data:` frames containing JSON.
    """
    if not raw:
        return []
    text_body = raw.decode("utf-8", errors="replace")
    ct = (content_type or "").lower()

    if "text/event-stream" in ct or "data:" in text_body:
        events: list[dict[str, Any]] = []
        for block in re.split(r"\n\n+", text_body.strip()):
            data_lines: list[str] = []
            for line in block.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            if not payload or payload == "[DONE]":
                continue
            decoded = _safe_json_loads(payload)
            if isinstance(decoded, list):
                for item in decoded:
                    if isinstance(item, dict):
                        events.append(item)
            elif isinstance(decoded, dict):
                events.append(decoded)
        if events:
            return events

    decoded = _safe_json_loads(text_body)
    if isinstance(decoded, list):
        return [ev for ev in decoded if isinstance(ev, dict)]
    if isinstance(decoded, dict) and isinstance(decoded.get("events"), list):
        return [ev for ev in decoded["events"] if isinstance(ev, dict)]
    return []


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return str(value)


def normalize_run_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ADK events into timeline rows for the telemetry pane."""
    timeline: list[dict[str, Any]] = []
    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        author = str(ev.get("author") or ev.get("agent") or "unknown")
        invocation_id = ev.get("invocation_id") or ev.get("id") or ""
        content = ev.get("content") or {}
        parts = content.get("parts") or []
        if not isinstance(parts, list):
            parts = []
        base = {
            "index": len(timeline),
            "event_index": idx,
            "author": author,
            "invocation_id": invocation_id,
            "collapsed": True,
            "raw_event": _jsonable(ev),
        }

        for p_index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            part_base = {**base, "part_index": p_index}
            if isinstance(part.get("text"), str) and part.get("text"):
                timeline.append(
                    {
                        **part_base,
                        "kind": "text",
                        "title": "assistant text" if author != "user" else "user text",
                        "summary": part["text"],
                        "payload": {"text": part["text"]},
                    }
                )
            if isinstance(part.get("functionCall"), dict):
                fc = part["functionCall"]
                timeline.append(
                    {
                        **part_base,
                        "kind": "tool-call",
                        "title": f"call {fc.get('name') or 'tool'}",
                        "summary": f"{fc.get('name') or 'tool'}",
                        "tool_name": fc.get("name"),
                        "payload": {
                            "name": fc.get("name"),
                            "args": fc.get("args") if "args" in fc else fc.get("arguments") if "arguments" in fc else fc,
                        },
                    }
                )
            if isinstance(part.get("functionResponse"), dict):
                fr = part["functionResponse"]
                response = fr.get("response") if "response" in fr else fr
                timeline.append(
                    {
                        **part_base,
                        "kind": "tool-result",
                        "title": f"return {fr.get('name') or 'tool'}",
                        "summary": f"{fr.get('name') or 'tool'}",
                        "tool_name": fr.get("name"),
                        "payload": {
                            "name": fr.get("name"),
                            "response": response,
                        },
                    }
                )
        # Preserve events with no parts so the pane can still show dispatches.
        if not parts:
            timeline.append(
                {
                    **base,
                    "kind": "event",
                    "title": author,
                    "summary": ev.get("type") or ev.get("event_type") or author,
                    "payload": _jsonable(ev),
                }
            )
    return timeline


def persist_run_telemetry(
    *,
    chat_id: int,
    session_id: str,
    run_path: str,
    request_payload: dict[str, Any] | None,
    response_payload: dict[str, Any] | None,
    raw_events: bytes,
    content_type: str,
    assistant_text: str,
    user_text: str,
    status_code: int,
    duration_ms: int,
) -> None:
    """Best-effort persistence of one `/api/run` run."""
    try:
        events = decode_run_events(raw_events, content_type=content_type)
        timeline = normalize_run_events(events)
        row = {
            "chat_id": chat_id,
            "session_id": session_id,
            "run_path": run_path,
            "request_payload_json": json.dumps(_jsonable(request_payload or {}), default=str),
            "response_payload_json": json.dumps(_jsonable(response_payload or {}), default=str),
            "raw_events_json": json.dumps(_jsonable(events), default=str),
            "timeline_json": json.dumps(
                {
                    "steps": timeline,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "event_count": len(events),
                    "assistant_text": assistant_text or "",
                    "user_text": user_text or "",
                },
                default=str,
            ),
            "assistant_text": assistant_text or "",
            "user_text": user_text or "",
            "status_code": status_code,
            "duration_ms": duration_ms,
            "event_count": len(events),
        }
        with db_connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO sidekick_run_telemetry "
                    "(chat_id, session_id, run_path, request_payload_json, response_payload_json, "
                    " raw_events_json, timeline_json, assistant_text, user_text, status_code, duration_ms, event_count) "
                    "VALUES (:chat_id, :session_id, :run_path, :request_payload_json, :response_payload_json, "
                    " :raw_events_json, :timeline_json, :assistant_text, :user_text, :status_code, :duration_ms, :event_count)"
                ),
                row,
            )
    except Exception:
        logger.exception("persist_run_telemetry failed")


def fetch_run_telemetry(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return the latest telemetry runs for a chat, newest first."""
    try:
        with db_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, chat_id, session_id, run_path, assistant_text, user_text, "
                    " status_code, duration_ms, event_count, created_at, timeline_json "
                    "FROM sidekick_run_telemetry WHERE chat_id = :c "
                    "ORDER BY created_at DESC, id DESC LIMIT :lim"
                ),
                {"c": chat_id, "lim": limit},
            ).all()
    except Exception:
        logger.exception("fetch_run_telemetry failed for chat_id=%s", chat_id)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r._mapping)
        timeline = item.pop("timeline_json", "{}")
        try:
            item["timeline"] = json.loads(timeline)
        except Exception:
            item["timeline"] = {"steps": [], "raw": timeline}
        out.append(item)
    return out
