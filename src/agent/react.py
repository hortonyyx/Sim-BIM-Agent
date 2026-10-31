import time
from collections import Counter
from typing import Annotated, Final

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from loguru import logger
from pydantic import BaseModel, Field

from src.agent._share import language_directive
from src.agent.trace import TraceCollector

MAX_LLM_CALL_ATTEMPTS: Final = 3
"""Total attempts (including the first) for a single ReAct-loop LLM call.

2026-08-13, F-26 — bounded retry, not blind.

Downstream 9-subagent tool-calling loops (`build_react_agent`) had zero
resilience: one provider-side failure on any turn propagated uncaught all the
way through `run_downstream_ep`, killing the entire flow with no retry and no
diagnosable evidence (unlike 1_correction/4_mep, whose single-shot raw-OpenAI
calls already retry via `pipeline.py::_call_json_llm`, and unlike the outer
stage-attempt loop, which redraws a whole stage on gate① rejection). This
mirrors that same idea one layer down, for the LLM<->tool transport itself.
The provider may still return a batch despite `parallel_tool_calls=False`.
That flag is a request preference, not permission to discard calls: every
returned id must be carried to `ToolNode` and answered before the next model
turn. This retry only covers residual transient provider/transport failures.
"""

RETRY_BACKOFF_SECONDS: Final = 2.0


class ReactLLMCallError(RuntimeError):
    """Raised when a ReAct-loop LLM call still fails after bounded retries.

    Carries the phase name, attempt count, and a message-pairing summary at
    the point of final failure so a crash leaves diagnosable evidence (which
    node, which round, message-history summary) instead of a bare provider
    error swallowed into a generic top-level `except Exception` — see
    `scripts/tool_scripts/run_stage.py`'s `flow --with-ep` handler, which
    only ever printed `str(e)` before this (2026-08-13, F-26 downstream
    `surface` break investigation).
    """


def _message_pairing_summary(messages: list[AnyMessage], *, tail: int = 20) -> str:
    """Render tool_call <-> ToolMessage pairing state for diagnostic evidence.

    Reports any tool_call_ids opened by an AIMessage and never closed by a
    matching ToolMessage by the end of the list, PLUS a subtler failure mode
    a naive per-key dict scan would hide: an AIMessage emitting >1 tool_calls
    in one turn (the provider ignoring `parallel_tool_calls=False`) and/or
    duplicate ids within/across tool_calls (a duplicate id still looks
    "paired" to a plain dict pop, since the second pop is a silent no-op,
    while genuinely producing more ToolMessages than the provider expects
    against that one id).
    """
    pending: dict[str, int] = {}
    lines: list[str] = []
    all_call_ids: list[str] = []
    tool_msg_ids: list[str] = []
    multi_call_turns: list[tuple[int, list[str]]] = []
    for i, m in enumerate(messages):
        mtype = type(m).__name__
        mid = getattr(m, "id", None)
        if isinstance(m, AIMessage) and m.tool_calls:
            call_ids = [tc.get("id") for tc in m.tool_calls]
            content_len = len(str(m.content))
            finish_reason = m.response_metadata.get("finish_reason")
            lines.append(
                f"[{i}] {mtype} id={mid} content_len={content_len} "
                f"finish_reason={finish_reason!r} n_tool_calls={len(call_ids)} "
                f"tool_calls={call_ids}"
            )
            if len(call_ids) > 1:
                multi_call_turns.append((i, call_ids))
            all_call_ids.extend(call_ids)
            for cid in call_ids:
                pending[cid] = i
        elif isinstance(m, ToolMessage):
            lines.append(
                f"[{i}] {mtype} id={mid} content_len={len(str(m.content))} "
                f"tool_call_id={m.tool_call_id}"
            )
            tool_msg_ids.append(m.tool_call_id)
            pending.pop(m.tool_call_id, None)
        else:
            content_len = (
                len(str(getattr(m, "content", ""))) if hasattr(m, "content") else 0
            )
            lines.append(f"[{i}] {mtype} id={mid} content_len={content_len}")

    call_id_counts = Counter(all_call_ids)
    dup_call_ids = {cid: n for cid, n in call_id_counts.items() if n > 1}
    tool_msg_counts = Counter(tool_msg_ids)
    dup_tool_msg_ids = {cid: n for cid, n in tool_msg_counts.items() if n > 1}
    orphan_tool_msgs = [cid for cid in tool_msg_ids if cid not in call_id_counts]

    header = (
        f"{len(messages)} total messages; unpaired tool_call_ids at end: {list(pending.keys())}\n"
        f"total AIMessage tool_calls emitted: {len(all_call_ids)}; total ToolMessages: {len(tool_msg_ids)}\n"
        f"multi-call turns (parallel_tool_calls should be False): {multi_call_turns}\n"
        f"DUPLICATE tool_call ids within AIMessage.tool_calls: {dup_call_ids}\n"
        f"DUPLICATE tool_call_id across ToolMessages: {dup_tool_msg_ids}\n"
        f"ToolMessages whose tool_call_id matches NO known AIMessage tool_call: {orphan_tool_msgs}"
    )
    tail_lines = lines[-tail:] if len(lines) > tail else lines
    return header + "\ntail:\n  " + "\n  ".join(tail_lines)


def _preserve_all_tool_calls(response: AIMessage, *, phase: str) -> AIMessage:
    """Keep every provider-emitted tool call so `ToolNode` can answer each id.

    `parallel_tool_calls=False` is advisory: deepseek-v4-pro can return a
    batch anyway. A live protocol probe on 2026-08-13 confirmed the provider
    accepts all 28 matching ToolMessages. Mutating the assistant message to
    retain only one is neither needed nor a valid implementation of the
    preference; it throws away requested work and obscures diagnostics.
    """
    if len(response.tool_calls) > 1:
        logger.warning(
            "[react:{}] provider returned {} tool_calls in one turn despite "
            "parallel_tool_calls=False; preserving all ids for ToolNode: {}",
            phase,
            len(response.tool_calls),
            [tc.get("name") for tc in response.tool_calls],
        )
    return response


class ReactState(BaseModel):
    """Internal state for phase-level ReAct subgraph.

    Kept separate from AgentState: messages here are NOT propagated
    to the outer graph. They exist only for the LLM <-> Tool loop.
    """

    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)


def build_react_agent(
    llm: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    trace_collector: TraceCollector | None = None,
) -> CompiledStateGraph:
    """Build a 3-node ReAct subgraph.

    Topology: llm -> [tools_condition] -> tools -> llm -> ... -> END.
    `parallel_tool_calls=False` is enforced so each tool call can be
    requested sequentially — important because tool calls mutate the shared
    (local-copy) ConfigState. The provider may ignore that preference; in
    that case `llm_node` preserves all returned calls so `ToolNode` responds
    to every id, and wraps provider failures in a bounded retry that leaves
    diagnostic evidence (see `ReactLLMCallError`).

    No checkpointer, no interrupts inside the subgraph.
    """
    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
    # Project-wide language directive appended once; per-phase prompts
    # stay free of language boilerplate.
    effective_prompt = system_prompt + language_directive()
    phase = trace_collector.phase if trace_collector else "unknown"

    def llm_node(state: ReactState) -> dict:
        messages = [SystemMessage(content=effective_prompt), *state.messages]
        last_exc: Exception | None = None
        for attempt in range(1, MAX_LLM_CALL_ATTEMPTS + 1):
            try:
                response = llm_with_tools.invoke(messages)
            except Exception as exc:  # noqa: BLE001 — transport/provider errors, bounded-retried
                last_exc = exc
                logger.warning(
                    "[react:{}] llm call attempt {}/{} failed: {}: {}",
                    phase,
                    attempt,
                    MAX_LLM_CALL_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                if attempt < MAX_LLM_CALL_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                summary = _message_pairing_summary(messages)
                logger.error(
                    "[react:{}] llm call exhausted {} attempt(s); message "
                    "pairing at failure time:\n{}",
                    phase,
                    MAX_LLM_CALL_ATTEMPTS,
                    summary,
                )
                raise ReactLLMCallError(
                    f"[react:{phase}] LLM call failed after {MAX_LLM_CALL_ATTEMPTS} "
                    f"attempt(s): {type(last_exc).__name__}: {last_exc}\n{summary}"
                ) from last_exc
            else:
                response = _preserve_all_tool_calls(response, phase=phase)
                return {"messages": [response]}
        # Unreachable: the loop above always either returns or raises.
        raise AssertionError("unreachable")  # pragma: no cover

    tool_node = ToolNode(
        tools,
        handle_tool_errors=True,
        wrap_tool_call=trace_collector.wrap if trace_collector else None,
    )

    builder = StateGraph(ReactState)
    builder.add_node("llm", llm_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "llm")
    builder.add_conditional_edges("llm", tools_condition, ["tools", END])
    builder.add_edge("tools", "llm")

    return builder.compile()
