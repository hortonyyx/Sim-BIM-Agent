from __future__ import annotations

from pathlib import Path

from loguru import logger

from src.agent.output_coordinates import (
    OutputCoordinateContract,
    apply_output_coordinate_contract,
)
from src.agent.state import AgentState, AgentStateUpdate, IntakeOutput


def _seed_config(
    state: AgentState,
    intake: IntakeOutput,
    contract: OutputCoordinateContract | None = None,
) -> AgentStateUpdate:
    """Write building + site_location (+ the E4 output-coordinate frame when a
    contract is present: GlobalGeometryRules A3/A4/A5 and the authoritative
    Building.North Axis — spec §5.2 call point 1; the Zone list is still empty
    at seed time, so the Zone-frame-zeroing half of the apply function is a
    no-op here) into config_state and return the update."""
    config = state.config_state.model_copy(deep=True)
    config.building = intake.building
    config.site_location = intake.site_location
    if contract is not None:
        config = apply_output_coordinate_contract(config, contract)
    return AgentStateUpdate(
        intake_output=intake, config_state=config, validation_errors=[]
    )


def intake_node(state: AgentState) -> AgentStateUpdate:
    """Produce IntakeOutput and seed config_state. Two dispatch modes:

    1. **Short-circuit** — `state.intake_output` already populated (the
       `--intake-from` flow): skip everything, just seed config_state. When the
       CLI loaded the file through `load_intake_bundle`, the E4 contract is
       already in `state.output_coordinate_contract` and is applied to the
       seeded config here.
    2. **Pipeline** — `state.reading_vector_dir` set (the half-manual flow:
       `--reading-from`): run the staged intake pipeline (vector JSON ->
       IntakeOutput) via `src.agent.legacy.pipeline.run_pipeline_artifacts`,
       image-blind. This is the legacy graph's input path; the pipeline returns the
       output-coordinate bundle alongside the IntakeOutput.

    Current Sim BIM Agent uses source BIM and its own runtime, outside this graph.
    The earlier image-to-IntakeOutput prompt library is retired.
    """
    if state.intake_output is not None and not state.validation_errors:
        config = state.config_state.model_copy(deep=True)
        config.building = state.intake_output.building
        config.site_location = state.intake_output.site_location
        if state.output_coordinate_contract is not None:
            config = apply_output_coordinate_contract(
                config, state.output_coordinate_contract
            )
        logger.info(
            "intake_node: short-circuit (pre-populated IntakeOutput); "
            "building={} site={} coordinate_mode={}",
            state.intake_output.building.name,
            state.intake_output.site_location.name,
            state.output_coordinate_contract.mode
            if state.output_coordinate_contract is not None
            else "none",
        )
        return AgentStateUpdate(config_state=config, validation_errors=[])

    if state.reading_vector_dir:
        # The reading stage already produced vector JSON; run the staged pipeline
        # (1_correction -> deterministic geometry -> 4_mep -> 5_intakeoutput) here.
        # Stay in the pipeline even with validation_errors present (a
        # validate->intake repair): feed the errors in as repair context.
        # Imported lazily so callers/tests that never touch the pipeline don't
        # pull in the OpenAI client.
        from src.agent.legacy.pipeline import run_pipeline_artifacts

        vector_dir = Path(state.reading_vector_dir)
        testdata_text = state.testdata_text or state.user_input
        feedback = "\n".join(f"- {e}" for e in state.validation_errors) or None
        out_dir = Path(state.pipeline_out_dir) if state.pipeline_out_dir else None
        logger.info(
            "intake_node: pipeline from {} (repair={})",
            vector_dir,
            bool(feedback),
        )
        intake, bundle = run_pipeline_artifacts(
            vector_dir, testdata_text, out_dir=out_dir, feedback=feedback
        )
        logger.info(
            "intake_node: pipeline done; building={} site={} coordinate_mode={}",
            intake.building.name,
            intake.site_location.name,
            bundle.output_coordinates.mode if bundle is not None else "none",
        )
        update = _seed_config(
            state,
            intake,
            contract=bundle.output_coordinates if bundle is not None else None,
        )
        update["output_coordinate_contract"] = (
            bundle.output_coordinates if bundle is not None else None
        )
        update["output_coordinate_context"] = (
            bundle.validation_context if bundle is not None else None
        )
        return update

    raise RuntimeError(
        "intake_node: no input. The legacy single-step image->IntakeOutput path "
        "is retired. Provide either a pre-built IntakeOutput (--intake-from) or a "
        "reading-stage vector directory (--reading-from) for the staged pipeline."
    )
