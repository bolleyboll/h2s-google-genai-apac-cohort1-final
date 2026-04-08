"""ADK tool that turns natural-language schedule text into UTC RFC3339 via the Gemini API."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


def _genai_client():
    """Construct a Gemini client from environment configuration.

    Raises:
        RuntimeError: If Vertex mode is enabled without ``GOOGLE_CLOUD_PROJECT``.
        RuntimeError: If neither Vertex AI nor ``GOOGLE_API_KEY`` is configured.

    Returns:
        Client: Configured ``google.genai.Client`` instance.
    """
    from google.genai import Client

    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    if use_vertex:
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is required when GOOGLE_GENAI_USE_VERTEXAI is set")
        return Client(vertexai=True, project=project, location=location)
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if key:
        return Client(api_key=key)
    raise RuntimeError(
        "Configure Vertex (GOOGLE_GENAI_USE_VERTEXAI=1 and GOOGLE_CLOUD_PROJECT) or GOOGLE_API_KEY"
    )


def _response_json_dict(resp: Any) -> dict[str, Any]:
    """Extract a JSON object from a ``generate_content`` response.

    Args:
        resp (Any): Model response object with ``parsed`` or ``candidates[].content.parts``.

    Raises:
        json.JSONDecodeError: If concatenated text is not valid JSON.

    Returns:
        dict[str, Any]: Parsed object, or ``{"ok": False, "error": ...}`` when text is empty.
    """
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    chunks: list[str] = []
    for c in getattr(resp, "candidates", None) or []:
        content = getattr(c, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if t:
                chunks.append(t)
    raw = "".join(chunks).strip()
    if not raw:
        return {"ok": False, "error": "empty_model_response"}
    return json.loads(raw)


def sanitize_schedule_times_to_utc(
    natural_language: str,
    default_duration_minutes: int = 60,
    *,
    tool_context: ToolContext,
) -> str:
    """Convert natural-language schedule text to UTC RFC3339 timestamps using Gemini.

    Args:
        natural_language (str): User wording describing when the event occurs.
        default_duration_minutes (int): Default length in minutes if no end time is implied.
        tool_context (ToolContext): ADK tool context (unused; required by ADK).

    Returns:
        str: JSON string with ``ok``, ``start_at_utc`` / ``end_at_utc`` on success, or error fields.
    """
    _ = tool_context
    nl = (natural_language or "").strip()
    if not nl:
        return json.dumps({"ok": False, "error": "natural_language is empty"})

    try:
        from google.genai import types
    except ImportError as e:
        return json.dumps({"ok": False, "error": f"google-genai not available: {e}"})

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    model = os.environ.get("MODEL", "gemini-2.5-flash").strip()
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "start_at_utc": {
                "type": "string",
                "description": "Start instant as RFC3339 in UTC with Z suffix",
            },
            "end_at_utc": {
                "type": "string",
                "description": "End instant as RFC3339 in UTC with Z suffix",
            },
            "error": {"type": "string", "description": "Set when ok is false"},
        },
        "required": ["ok"],
    }
    system = (
        "You convert the user's natural-language schedule text into UTC timestamps.\n"
        f"Current UTC time (use for relative phrases like tomorrow, next Friday): {now}\n"
        f"If the user does not specify an end time, set end to start plus {int(default_duration_minutes)} minutes.\n"
        "Rules:\n"
        "- Respond with JSON only, matching the schema.\n"
        "- start_at_utc and end_at_utc must look like 2026-04-08T14:30:00Z (Z means UTC).\n"
        "- If you cannot infer reasonable times, set ok to false and error to a short explanation.\n"
    )

    try:
        client = _genai_client()
        resp = client.models.generate_content(
            model=model,
            contents=f"User schedule request:\n{nl}",
            config=types.GenerateContentConfig(
                temperature=0,
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        data = _response_json_dict(resp)
    except json.JSONDecodeError as e:
        logger.warning("sanitize_schedule_times JSON decode failed: %s", e)
        return json.dumps({"ok": False, "error": f"model_returned_invalid_json: {e}"})
    except Exception as e:
        logger.exception("sanitize_schedule_times failed")
        return json.dumps({"ok": False, "error": str(e)})

    if not isinstance(data, dict):
        return json.dumps({"ok": False, "error": "model_returned_non_object"})
    if not data.get("ok"):
        return json.dumps(
            {
                "ok": False,
                "error": data.get("error") or "unspecified_model_error",
            }
        )
    start = (data.get("start_at_utc") or "").strip()
    end = (data.get("end_at_utc") or "").strip()
    if not start or not end:
        return json.dumps(
            {"ok": False, "error": "model_ok_but_missing_start_or_end_at_utc"}
        )
    return json.dumps(
        {
            "ok": True,
            "start_at_utc": start,
            "end_at_utc": end,
            "hint": (
                "Pass start_at_utc and end_at_utc to google_calendar_create_event when Calendar "
                "integration is on, or to create_calendar_event when only database schedule tools exist."
            ),
        }
    )
