from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger


class TraceCollector:
    """Collect tool-call traces via ToolNode.wrap_tool_call.

    Usage:
        collector = TraceCollector()
        tool_node = ToolNode(tools, wrap_tool_call=collector.wrap)

    Each phase agent should instantiate its own collector to avoid
    cross-phase contamination under parallel execution.
    """

    def __init__(self, phase: str = "unknown") -> None:
        self.phase = phase
        self.traces: list[dict[str, Any]] = []

    def wrap(
        self,
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept tool execution, record trace, pass through result."""
        tool_name = request.tool_call.get("name", "<unknown>")
        entry: dict[str, Any] = {
            "phase": self.phase,
            "tool_name": tool_name,
            "tool_args": request.tool_call.get("args", {}),
        }

        result = execute(request)

        if isinstance(result, ToolMessage):
            content = str(result.content)
            entry["result"] = content
            entry["success"] = "error" not in content.lower()
        else:
            entry["result"] = str(result)
            entry["success"] = True

        self.traces.append(entry)
        logger.debug(
            "Tool trace[{}]: {} -> {}", self.phase, tool_name, entry["success"]
        )
        return result

    def export(self) -> list[dict[str, Any]]:
        """Return a copy of all collected traces."""
        return list(self.traces)

    def clear(self) -> None:
        self.traces.clear()


_trace_store: dict[str, list[dict[str, Any]]] = {}


def record_phase_trace(phase: str, entries: list[dict[str, Any]]) -> None:
    """Append one phase's trace entries to the session-scoped store."""
    _trace_store.setdefault(phase, []).extend(entries)


def export_traces() -> dict[str, list[dict[str, Any]]]:
    """Snapshot the current session's traces, phase -> entries."""
    return {phase: list(entries) for phase, entries in _trace_store.items()}


def reset_traces() -> None:
    """Clear the trace store. Called at the start of every `run_session`."""
    _trace_store.clear()
