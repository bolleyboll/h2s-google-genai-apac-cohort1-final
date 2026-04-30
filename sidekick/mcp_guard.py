"""Before-tool callback that gates MCP-provided tool calls on a per-chat grant.

The MCP tools come from external server processes; their tool names and
mutation semantics are unknown to Sidekick. To keep MCP within the same
chat-scope guarantees as our first-party tools, every MCP tool call is routed
through this callback. The chat must hold an explicit grant for that MCP
prefix (stored in ``sidekick_chat_mcp_access``) — without it the call is
short-circuited with a structured ``mcp_access_denied`` payload that the UI /
LLM can react to (typically by surfacing a "Grant MCP access" prompt).

Read and write are gated identically: until the user explicitly opts a chat
into a given MCP server, the agent cannot use it from that chat.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from sidekick.db import db_connection

logger = logging.getLogger(__name__)


def _is_mcp_tool(tool: Any) -> bool:
    """Return whether ``tool`` is an MCP-discovered tool.

    Args:
        tool (Any): Tool instance passed to the before-callback.

    Returns:
        bool: True when the tool descends from ADK's MCP tool class.
    """
    try:
        from google.adk.tools.mcp_tool.mcp_tool import McpTool  # type: ignore
    except Exception:
        return False
    return isinstance(tool, McpTool)


def _active_chat_id(tool_context: Any) -> Optional[int]:
    """Read the active chat id from ADK session state.

    Args:
        tool_context (Any): ADK ``ToolContext`` for the current call.

    Returns:
        Optional[int]: Active chat id or None when not seeded.
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


def _grant_exists(chat_id: int, prefix: str) -> bool:
    """Return whether a row exists in ``sidekick_chat_mcp_access`` for the pair.

    Args:
        chat_id (int): Chat primary key.
        prefix (str): MCP short prefix (``task`` / ``calendar`` / ``notes`` / custom).

    Returns:
        bool: True when the chat has been opted into this MCP server.
    """
    try:
        with db_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM sidekick_chat_mcp_access "
                    "WHERE chat_id = :c AND mcp_prefix = :p LIMIT 1"
                ),
                {"c": chat_id, "p": prefix},
            ).first()
        return row is not None
    except Exception:
        logger.exception("MCP grant lookup failed for chat=%s prefix=%s", chat_id, prefix)
        return False


def mcp_access_callback(
    tool: Any, args: dict[str, Any], tool_context: Any
) -> Optional[dict[str, Any]]:
    """ADK ``before_tool_callback`` that enforces per-chat MCP grants.

    Args:
        tool (Any): Tool ADK is about to invoke.
        args (dict[str, Any]): Arguments resolved for this call (unused here).
        tool_context (Any): ADK tool context (for state + user_id).

    Returns:
        Optional[dict[str, Any]]: ``None`` to let ADK run the tool normally;
        an error dict to short-circuit the call with that result instead.
    """
    if not _is_mcp_tool(tool):
        return None

    prefix = getattr(tool, "_sidekick_mcp_prefix", None)
    chat_id = _active_chat_id(tool_context)

    if chat_id is None:
        return {
            "error": "mcp_access_denied",
            "reason": "no_active_chat",
            "mcp_prefix": prefix,
            "tool": getattr(tool, "name", None),
            "message": (
                "MCP tools require an active chat with explicit access. "
                "No active chat was set in session state."
            ),
        }

    if not prefix:
        # Untagged MCP tool — deny by default. Should not happen with TaggedMcpToolset.
        return {
            "error": "mcp_access_denied",
            "reason": "untagged_mcp_tool",
            "tool": getattr(tool, "name", None),
            "message": (
                "An MCP tool reached the agent without a Sidekick MCP prefix. "
                "Refusing to call without explicit grant."
            ),
        }

    if _grant_exists(chat_id, prefix):
        return None

    return {
        "error": "mcp_access_denied",
        "reason": "no_grant",
        "mcp_prefix": prefix,
        "chat_id": chat_id,
        "tool": getattr(tool, "name", None),
        "message": (
            "This chat has not been granted access to the "
            f"'{prefix}' MCP server. Tell the user to enable it for this "
            "chat from the chat settings (MCP access section), then retry."
        ),
    }
