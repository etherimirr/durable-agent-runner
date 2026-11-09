"""How the graph reaches its tools.

`DirectToolClient` calls the Python functions in-process. `McpToolClient`
calls the same tools through an MCP session, which is how any other MCP
client (an IDE, another agent) would use them. The graph does not know
which one it has.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from .tools import TOOL_NAMES, ToolError, Tools


class ToolClient(Protocol):
    async def call(self, name: str, **args: Any) -> dict[str, Any]: ...


class DirectToolClient:
    def __init__(self, tools: Tools):
        self.tools = tools

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool {name!r}")
        return await asyncio.to_thread(getattr(self.tools, name), **args)


class McpToolClient:
    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        async with self._lock:
            result = await self.client.call_tool(name, args)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise ToolError(text)
        data = json.loads(text)
        if isinstance(data, dict) and set(data) == {"error"}:
            raise ToolError(data["error"])
        return data
