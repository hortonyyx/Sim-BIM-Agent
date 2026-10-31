from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from loguru import logger

from src.agent._share import ensure_schema_initialized
from src.agent.state import AgentState, SimContext
from src.agent.trace import reset_traces

type InterruptDecision = dict[str, Any]
type InterruptHandler = Callable[[dict[str, Any]], InterruptDecision]
type NodeEventHandler = Callable[[str, dict[str, Any]], None]
type AgentGraph = CompiledStateGraph[AgentState, SimContext, AgentState, AgentState]


def load_intake_from(path: Path):
    """Load an IntakeOutput JSON with IDD-backed schemas initialized."""
    from src.agent.state import IntakeOutput

    ensure_schema_initialized()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return IntakeOutput.model_validate(data)


class InterruptLoopBreakerError(RuntimeError):
    """Raised by ``run_session`` when the same validate-interrupt error set
    recurs past the breaker threshold — a deterministic termination guarantee
    against repair loops that can never make progress.

    A ``validate -> intake -> ... -> validate`` cycle with a persistent error,
    ``MAX_RETRIES=0``, and an intake short-circuit cannot self-terminate: the
    error never disappears, retry is already exhausted, and auto-approval
    always rejects. Rather than rely on the error "eventually going away", the
    breaker counts consecutive identical error-bearing interrupts and aborts.
    """


def _interrupt_errors_signature(payload: Any) -> tuple[str, ...] | None:
    """Stable signature of an interrupt payload's error set.

    Returns ``None`` when the payload carries no errors (a clean interrupt
    awaiting approval, or a non-dict payload) so the clean path never counts
    toward the breaker. Order-independent so list reshuffles do not mask a
    genuinely stuck loop.
    """
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not errors:
        return None
    return tuple(sorted(str(e) for e in errors))


def run_session(
    graph: AgentGraph,
    initial: AgentState,
    context: SimContext,
    config: RunnableConfig,
    on_interrupt: InterruptHandler,
    on_event: NodeEventHandler | None = None,
    *,
    max_consecutive_identical_interrupts: int = 3,
) -> dict[str, Any]:
    """Drive `graph` from `initial` until END, handling validate interrupts.

    Args:
        graph: Compiled agent graph from `build_graph()`.
        initial: Seed AgentState (user_input + optional image_paths).
        context: Runtime context (epw_path, output_dir).
        config: RunnableConfig carrying thread_id for checkpointer.
        on_interrupt: Called at each validate interrupt. Receives the
            interrupt payload `{"summary": ..., "errors": [...], "message": ...}`,
            returns `{"approved": bool, ...}`.
        on_event: Optional debug hook called as `on_event(node_name, update)`
            after every non-interrupt node. `update` is the partial state
            dict that node returned. Default None = silent.
        max_consecutive_identical_interrupts: Circuit-breaker threshold. If a
            validate interrupt carries the SAME error set on more than this
            many consecutive rounds, ``run_session`` raises
            ``InterruptLoopBreakerError`` instead of resuming — a deterministic
            termination guarantee against non-terminating repair loops (see the
            exception docstring). Normal flows interrupt once and resolve; this
            only trips on a genuinely stuck loop.

    Returns:
        Final state dict after END (pulled from the checkpointer).
    """
    reset_traces()
    payload: Any = initial
    identical_run = 0
    last_signature: tuple[str, ...] | None = None
    while True:
        for event in graph.stream(
            payload, config=config, context=context, stream_mode="updates"
        ):
            if on_event is None:
                continue
            for node, update in event.items():
                if node == "__interrupt__":
                    continue
                on_event(node, update)

        snapshot = graph.get_state(config)
        pending = [t.interrupts[0] for t in snapshot.tasks if t.interrupts]
        if not pending:
            return dict(snapshot.values)

        interrupt_value = pending[0].value
        signature = _interrupt_errors_signature(interrupt_value)
        if signature is None:
            # No error set (clean interrupt awaiting approval) — never counts
            # toward the breaker; reset so a later error run starts fresh.
            identical_run = 0
            last_signature = None
        else:
            if signature == last_signature:
                identical_run += 1
            else:
                identical_run = 1
                last_signature = signature
            if identical_run > max_consecutive_identical_interrupts:
                errors = (
                    interrupt_value.get("errors")
                    if isinstance(interrupt_value, dict)
                    else []
                )
                raise InterruptLoopBreakerError(
                    f"validate interrupt repeated the same error set "
                    f"{identical_run} consecutive time(s) "
                    f"(threshold={max_consecutive_identical_interrupts}); "
                    f"aborting a non-terminating repair loop. "
                    f"{len(errors)} error(s):\n"
                    + "\n".join(f"  - {e}" for e in (errors or []))
                )

        decision = on_interrupt(interrupt_value)
        payload = Command(resume=decision)


def run_downstream_ep(
    *,
    initial_state: AgentState,
    epw: Path,
    output_dir: Path,
    ep_run_subdir: str | None,
    run_simulate: bool,
    thread_id: str,
    on_event: NodeEventHandler | None = None,
) -> dict[str, Any]:
    """Run the downstream graph + optional EnergyPlus simulation.

    Callers own config resolution and layout decisions. This helper owns only
    graph/session/SimContext wiring so CLI and flow use the same EP execution
    path.
    """
    from src.agent.legacy.graph import build_graph

    graph = build_graph()
    context = SimContext(
        epw_path=Path(epw),
        output_dir=Path(output_dir),
        run_simulate=run_simulate,
        ep_run_subdir=ep_run_subdir,
    )
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    return run_session(
        graph,
        initial_state,
        context,
        cfg,
        on_interrupt=auto_approval,
        on_event=on_event,
    )


def interactive_approval(payload: dict[str, Any]) -> InterruptDecision:
    """Console approval prompt for the validate interrupt.

    Prints a one-line ConfigSummary + any cross-ref errors, then reads
    y / yes to approve or any other text as rejection feedback.
    """

    summary = payload["summary"]
    errors = payload["errors"]

    logger.info(
        "validate interrupt: zones={} materials={} surfaces={} fenestrations={}",
        summary["zones_count"],
        summary["materials_count"],
        summary["surfaces_count"],
        summary["fenestrations_count"],
    )
    if errors:
        for e in errors:
            logger.warning("cross-ref error: {}", e)
    else:
        logger.info("no cross-reference errors")

    answer = input("Approve? [y/N or feedback text] > ").strip()
    if answer.lower() in ["y", "yes"]:
        return {"approved": True}
    else:
        return {"approved": False, "feedback": answer}


def auto_approval(payload: dict[str, Any]) -> InterruptDecision:
    """Auto-approve the validate interrupt.

    Args:
        payload: Payload from the validate interrupt.

    Returns:
        InterruptDecision with "approved": True.

    On rejection the error set is logged at WARNING (count + every message) so
    a stuck repair loop is visible on stdout. Historically this decision was
    fully silent, which let a non-terminating ``validate -> intake -> ... ->
    validate`` loop run ~400 paid LLM rounds before anyone noticed. The
    returned decision dict is deliberately left without an ``"errors"`` key:
    wiring errors back into ``validate_node``'s rejected branch would only flip
    the loop into a different failure mode (a hard crash at intake, per the
    F-11 investigation option A), not fix it. Deterministic loop termination
    is the circuit breaker's job (``InterruptLoopBreakerError``), not this
    function's.
    """
    errors = payload["errors"]
    if not errors:
        return {"approved": True}
    logger.warning(
        "validate interrupt AUTO-REJECTED: {} cross-ref error(s):\n{}",
        len(errors),
        "\n".join(f"  - {e}" for e in errors),
    )
    return {
        "approved": False,
        "feedback": "please address errors: " + "; ".join(errors),
    }


def print_final_messages(state: dict[str, Any], n: int = 5) -> None:
    for m in state.get("messages", [])[-n:]:
        content = m.content if hasattr(m, "content") else str(m)
        logger.info("final {}: {}", getattr(m, "type", "msg"), str(content)[:200])
