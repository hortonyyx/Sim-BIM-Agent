"""Shared helpers for phase-agent nodes.

Kept here (rather than `src/agent/_share.py`) because the scope is
nodes-internal — no other part of the agent package uses these.
"""

from __future__ import annotations

from typing import Any, Final

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from loguru import logger

from src.agent._share import language_directive
from src.agent.react import ReactState
from src.mcp.state import ConfigState

MAX_SELF_REPAIR_ROUNDS: Final = 2
"""Max extra invokes per phase for cross-ref self-repair.

Two rounds is enough for the LLM to see its own error feedback and
react; repeated failures beyond that point usually mean the intake
specs are broken, which the outer validate loop handles better.
"""


def invoke_with_self_repair(
    agent: CompiledStateGraph[Any, Any, Any, Any],
    local_config: ConfigState,
    specs: str,
    *,
    phase: str,
) -> dict[str, Any]:
    """Run a phase ReAct agent and force cross-reference self-repair.

    After each `agent.invoke`, call `local_config.validate_references()`
    in code (not tool — cannot be skipped by the LLM). If errors exist,
    push them back as a HumanMessage and invoke again. Loop up to
    MAX_SELF_REPAIR_ROUNDS.

    Since phase agents only see objects they created + upstream phases'
    outputs (no cross-phase bleed through LangGraph's deep-copy model),
    any error surfaced here is either the LLM referencing a bad name
    (self-repairable) or an upstream resource truly missing (LLM should
    report in summary; outer validate loop handles the recovery).

    Args:
        agent: Compiled ReAct subgraph from `build_react_agent`.
        local_config: The deep-copied ConfigState the phase mutates.
        specs: Natural-language task for the phase (from intake_output).
        phase: Name used in logs ("construction", "surface", ...).

    Returns:
        The final ReAct result dict (shape {"messages": [...]}).
    """
    messages: list[AnyMessage] = [HumanMessage(content=specs)]

    for attempt in range(MAX_SELF_REPAIR_ROUNDS + 1):
        result = agent.invoke(ReactState(messages=messages))
        errors = local_config.validate_references()

        if not errors:
            if attempt > 0:
                logger.info("[{}] self-repair succeeded on round {}", phase, attempt)
            return result

        if attempt == MAX_SELF_REPAIR_ROUNDS:
            logger.warning(
                "[{}] self-repair exhausted after {} rounds, {} errors remain "
                "— escalating to outer validate loop",
                phase,
                MAX_SELF_REPAIR_ROUNDS,
                len(errors),
            )
            return result

        logger.info(
            "[{}] self-repair round {}: {} cross-ref errors",
            phase,
            attempt + 1,
            len(errors),
        )
        feedback = HumanMessage(
            content=(
                "Cross-reference validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + "\n\nFix the objects YOU just created: use `update_<x>` to "
                "rename references, or `delete_<x>` + `create_<x>` to "
                "rebuild. If the broken reference names an upstream "
                "resource (zone / schedule / material / construction / "
                "surface) that truly does not exist, report it in your "
                "final message and do NOT fabricate a replacement — "
                "upstream phases own those objects." + language_directive()
            )
        )
        messages = [*list(result["messages"]), feedback]

    return result
