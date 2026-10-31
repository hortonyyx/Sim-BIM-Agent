from src.agent.state import AgentState, AgentStateUpdate


def _output_coordinate_errors(
    state: AgentState, *, include_vertex_drift: bool = True
) -> list[str]:
    """Run the E4 contract gate at graph repair boundaries.

    The final Workflow gate remains authoritative for the converted IDF, but
    an in-memory drift must not survive a parallel merge/checkpoint/retry only
    to be discovered at export time (E4 §5.2 call point 3, BO-CR8).

    include_vertex_drift: the vertex-by-vertex drift comparison is a TERMINAL
        check (needs the full geometry in ConfigState). ``cross_ref_foundations``
        passes False because surfaces do not exist yet at that boundary;
        ``cross_ref_complete`` keeps the default True so the full gate still
        runs once geometry is complete. See
        ``validate_output_coordinate_contract`` for the contract guarantee.
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
            include_vertex_drift=include_vertex_drift,
        )
    ]


def cross_ref_foundations_node(state: AgentState) -> AgentStateUpdate:
    """Cross-ref check after phase 1 (zone + material + schedule).

    Most checks are moot at this stage (no constructions, surfaces, HVAC yet),
    but any early violation of shared identity still surfaces here.

    The E4 vertex-drift comparison is deliberately skipped here: it compares the
    terminal geometry snapshot against ConfigState.surfaces/fenestrations, which
    do not exist until the construction -> surface -> fenestration chain runs.
    Running it now would report every snapshot record as "missing from
    ConfigState" — a timing false-positive, not a real drift. The full gate
    (including vertex-drift) runs at cross_ref_complete and validate once the
    geometry exists, so no real drift is dropped (E4 §5.2 call points 3 & 4).
    """
    return AgentStateUpdate(
        validation_errors=state.config_state.validate_references()
        + _output_coordinate_errors(state, include_vertex_drift=False)
    )


def cross_ref_complete_node(state: AgentState) -> AgentStateUpdate:
    """Full cross-ref check after phase 3 (hvac + people + lights)."""
    return AgentStateUpdate(
        validation_errors=state.config_state.validate_references() + _output_coordinate_errors(state)
    )
