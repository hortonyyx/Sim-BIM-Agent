"""Small, domain-neutral stdio MCP client.

The client owns transport mechanics only. Callers supply the executable and
arguments, then a domain layer applies tool policy, role grants, and recovery
rules to the JSON-safe results returned here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class McpTransportError(RuntimeError):
    """Raised when an MCP transport cannot provide a complete safe result."""


class McpToolClient:
    """Connect to one stdio MCP server and return JSON-compatible values.

    ``run_directory`` gives transport temporary files an explicit owner. It is
    not interpreted as a server argument; a building adapter, database adapter,
    or any other caller decides what command-line arguments its server needs.
    """

    def __init__(
        self,
        *,
        command: str | Path,
        args: Sequence[str] = (),
        run_directory: Path,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        command_text = os.fspath(command)
        if not command_text:
            raise ValueError("MCP command must not be empty")
        if isinstance(args, (str, bytes)) or any(not isinstance(arg, str) for arg in args):
            raise TypeError("MCP args must be a sequence of strings")
        self.command = command_text
        self.args = tuple(args)
        self.run_directory = Path(run_directory).resolve()
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.env = dict(env or {})
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> McpToolClient:
        if not self.run_directory.is_dir():
            raise FileNotFoundError(f"MCP run directory does not exist: {self.run_directory}")
        if self._stack is not None:
            raise McpTransportError("MCP client is already entering or connected")
        temp_directory = self.run_directory / ".harness_tmp"
        temp_directory.mkdir(exist_ok=True)
        process_env = {
            # Small geometry work; a default OpenBLAS pool reserves ~0.5 GB per
            # server on a 16-thread machine. An explicit setting still wins.
            "OPENBLAS_NUM_THREADS": "1",
            **os.environ,
            **self.env,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(temp_directory),
        }
        parameters = StdioServerParameters(
            command=self.command,
            args=list(self.args),
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=process_env,
        )
        stack = AsyncExitStack()
        self._stack = stack
        try:
            reader, writer = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(reader, writer))
            await session.initialize()
        except BaseException:
            # stdio_client owns a subprocess/task group. Close it in the same
            # task that entered it before exposing the initialization failure.
            self._stack = None
            self._session = None
            await stack.aclose()
            raise
        self._session = session
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    def _connected(self) -> ClientSession:
        if self._session is None:
            raise McpTransportError("MCP client is not connected; use it inside 'async with'")
        return self._session

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return every original tool definition, following MCP pagination.

        A repeated cursor or duplicate tool name is a protocol error. Silently
        accepting either could expose only part of a role whitelist or make the
        frozen schema hash depend on server ordering quirks.
        """

        session = self._connected()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_names: set[str] = set()
        tools: list[dict[str, Any]] = []
        while True:
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                record = tool.model_dump(mode="json", exclude_none=True)
                name = record.get("name")
                if not isinstance(name, str) or not name:
                    raise McpTransportError("MCP tools/list returned a tool without a valid name")
                if name in seen_names:
                    raise McpTransportError(f"MCP tools/list returned duplicate tool name: {name}")
                seen_names.add(name)
                tools.append(record)
            next_cursor = result.nextCursor
            if next_cursor is None:
                return tools
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpTransportError("MCP tools/list returned an invalid pagination cursor")
            if next_cursor in seen_cursors:
                raise McpTransportError(f"MCP tools/list repeated pagination cursor: {next_cursor}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and preserve its raw MCP result envelope."""

        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be a JSON object")
        result = await self._connected().call_tool(name, arguments)
        reply: dict[str, Any] = {
            "content": [block.model_dump(mode="json", exclude_none=True) for block in result.content],
            "isError": bool(result.isError),
        }
        if result.structuredContent is not None:
            reply["structuredContent"] = result.structuredContent
        # Fail early if a future MCP library returns a non-JSON value. Event
        # storage must never silently stringify an opaque result.
        json.dumps(reply, ensure_ascii=False, allow_nan=False)
        return reply
