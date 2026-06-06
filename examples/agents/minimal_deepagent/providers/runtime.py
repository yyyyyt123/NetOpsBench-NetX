"""Runtime helpers for the minimal_deepagent example."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from langchain_mcp_adapters.sessions import create_session
from langchain_mcp_adapters.tools import load_mcp_tools


async def _connect_mcp_tools(
    exit_stack: AsyncExitStack,
    server_config: dict[str, dict[str, Any]],
) -> list:
    """Open MCP sessions and return the loaded tool list."""
    tools: list = []
    prefix = len(server_config) > 1
    for name, connection in server_config.items():
        session = await exit_stack.enter_async_context(create_session(connection))
        await session.initialize()
        tools.extend(await load_mcp_tools(session, server_name=name, tool_name_prefix=prefix))
    return tools
