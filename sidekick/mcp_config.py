"""Optional Model Context Protocol (MCP) toolsets wired from environment variables.

Each toolset is tagged with a stable ``mcp_prefix`` (``task`` / ``calendar`` /
``notes``) so the agent's ``before_tool_callback`` can look up per-chat MCP
grants and gate every tool call against them — see :mod:`sidekick.mcp_guard`.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Maps the env-var prefix to the short identifier persisted in
# ``sidekick_chat_mcp_access.mcp_prefix``. Keep these stable.
_PREFIX_SHORT: dict[str, str] = {
    "SIDEKICK_MCP_TASK": "task",
    "SIDEKICK_MCP_CALENDAR": "calendar",
    "SIDEKICK_MCP_NOTES": "notes",
}


class TaggedMcpToolset(McpToolset):
    """``McpToolset`` that tags each discovered tool with a Sidekick MCP prefix.

    Tools surface with a ``_sidekick_mcp_prefix`` attribute so the
    before-tool callback can correlate a tool call back to its env-var origin
    and enforce the corresponding per-chat grant.
    """

    def __init__(self, *args: Any, sidekick_mcp_prefix: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Underscore-prefixed attribute, set after super init so we don't fight
        # the parent's pydantic/dataclass validation.
        object.__setattr__(self, "_sidekick_mcp_prefix", sidekick_mcp_prefix)

    async def get_tools(
        self,
        readonly_context: Optional[ReadonlyContext] = None,
    ) -> List[BaseTool]:
        """Discover MCP tools and tag each with the toolset's Sidekick prefix.

        Args:
            readonly_context (Optional[ReadonlyContext]): Forwarded to ``McpToolset``.

        Returns:
            List[BaseTool]: Discovered tools, each carrying ``_sidekick_mcp_prefix``.
        """
        tools = await super().get_tools(readonly_context)
        for t in tools:
            try:
                object.__setattr__(t, "_sidekick_mcp_prefix", self._sidekick_mcp_prefix)
            except Exception:
                # If the tool class refuses extra attrs, the guard simply
                # treats it as "untagged MCP" and falls back to a deny-by-default.
                pass
        return tools


def mcp_short_prefix_for(env_prefix: str) -> str:
    """Return the short MCP grant id for ``env_prefix`` (e.g. ``task``).

    Args:
        env_prefix (str): Env-var prefix passed to :func:`mcp_toolset_from_env`.

    Returns:
        str: Short id used for grant lookups; falls back to a slug of ``env_prefix``.
    """
    return _PREFIX_SHORT.get(env_prefix) or env_prefix.lower().replace(
        "sidekick_mcp_", ""
    ).replace("_", "-")


def mcp_toolset_from_env(prefix: str) -> Optional[TaggedMcpToolset]:
    """Build an MCP toolset from ``{prefix}_COMMAND`` and ``{prefix}_ARGS``.

    Args:
        prefix (str): Environment variable prefix (e.g. ``SIDEKICK_MCP_TASK``,
            ``SIDEKICK_MCP_CALENDAR``, ``SIDEKICK_MCP_NOTES``).

    Raises:
        ValueError: If ``{prefix}_ARGS`` is not valid JSON or is not a JSON array.

    Returns:
        Optional[TaggedMcpToolset]: Configured toolset, or ``None`` if ``{prefix}_COMMAND`` is unset.
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
    return TaggedMcpToolset(
        connection_params=StdioConnectionParams(server_params=params),
        sidekick_mcp_prefix=mcp_short_prefix_for(prefix),
    )


def mcp_known_short_prefixes() -> tuple[str, ...]:
    """Return all MCP short prefixes that have a configured server right now.

    Returns:
        tuple[str, ...]: Sorted tuple of available short prefixes (e.g. ``("task", "notes")``).
    """
    out: list[str] = []
    for env in _PREFIX_SHORT:
        if os.environ.get(f"{env}_COMMAND", "").strip():
            out.append(_PREFIX_SHORT[env])
    return tuple(sorted(out))
