"""Optional Model Context Protocol (MCP) toolsets wired from environment variables."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters


def mcp_toolset_from_env(prefix: str) -> Optional[McpToolset]:
    """Build an MCP toolset from ``{prefix}_COMMAND`` and ``{prefix}_ARGS``.

    Args:
        prefix (str): Environment variable prefix (e.g. ``SIDEKICK_MCP_TASK``,
            ``SIDEKICK_MCP_CALENDAR``, ``SIDEKICK_MCP_NOTES``).

    Raises:
        ValueError: If ``{prefix}_ARGS`` is not valid JSON.
        ValueError: If ``{prefix}_ARGS`` is not a JSON array.

    Returns:
        Optional[McpToolset]: Configured toolset, or ``None`` if ``{prefix}_COMMAND`` is unset.
    """
    cmd = os.environ.get(f"{prefix}_COMMAND", "").strip()
    if not cmd:
        return None
    raw = os.environ.get(f"{prefix}_ARGS", "[]").strip()
    try:
        args: list[Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{prefix}_ARGS must be a JSON array: {e}") from e
    if not isinstance(args, list):
        raise ValueError(f"{prefix}_ARGS must be a JSON array")
    params = StdioServerParameters(command=cmd, args=[str(a) for a in args])
    return McpToolset(connection_params=StdioConnectionParams(server_params=params))
