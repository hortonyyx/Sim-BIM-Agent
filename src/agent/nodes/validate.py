from typing import Literal

from langchain_core.messages import RemoveMessage
from langgraph.types import Command, interrupt

from src.agent.state import AgentState


def _output_coordinate_errors(state: AgentState) -> list[str]:
    """Repeat the pure contract gate before deciding repair/HITL routing.

    This deliberately checks ConfigState only; WorkflowTool repeats the same
    gate after conversion against the live IDF.
    """
    contract = state.output_coordinate_contract
    if contract is None:
        return []
    if state.output_coordinate_context is None:
        return ["output-coordinates[CONTRACT_IDENTITY]: contract has no validation context"]
    from src.validator.output_coordinates import validate_output_coordinate_contract

    return [
        f"output-coordinates[{issue.code}]: {issue.message}"
        for issue in validate_output_coordinate_contract(
            state.config_state, contract, state.output_coordinate_context,
        )
    ]


def validate_node(state: AgentState) -> Command[Literal["simulate", "intake"]]:
    """Validate full config; auto-retry on error up to max_retries; else HITL.

    Return behavior:
    - errors + retries remaining -> goto intake with error feedback
    - clean or retries exhausted  -> interrupt() for human review
        - approved -> goto simulate
        - rejected -> goto intake with human feedback
    """
    errors = state.config_state.validate_references() + _output_coordinate_errors(state)

    if errors and state.retry_count < state.max_retries:
        return Command(
            goto="intake",
            update={
                "validation_errors": errors,
                "retry_count": state.retry_count + 1,
                "messages": [
                    RemoveMessage(id=m.id) for m in state.messages if m.id is not None
                ],
            },
        )

    summary = state.config_state.get_summary()
    decision = interrupt(
        {
            "summary": summary.model_dump(),
            "errors": errors,
            "message": "Review configuration before simulation. "
            "Respond with {'approved': True} or "
            "{'approved': False, 'feedback': '...', 'errors': [...]}.",
        }
    )

    if decision.get("approved"):
        return Command(goto="simulate")

    return Command(
        goto="intake",
        update={
            "user_input": decision.get("feedback", state.user_input),
            "validation_errors": decision.get("errors", []),
            "retry_count": 0,
            "messages": [
                RemoveMessage(id=m.id) for m in state.messages if m.id is not None
            ],
        },
    )
