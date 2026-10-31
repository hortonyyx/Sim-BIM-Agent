from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from src.agent.state import AgentState, AgentStateUpdate, SimContext
from src.mcp.state import ConfigState
from src.mcp.tools.workflow import WorkflowTool
from src.validator import OutputVariableSchema

# Default Output:Variable set. Without at least one entry, EnergyPlus
# runs the full RunPeriod but `eplusout.eso` stays 0 bytes — nothing is
# recorded. Applied only when `config.output_variable` is empty; users
# / LLM can override by populating it themselves.
_DEFAULT_OUTPUT_VARIABLES: tuple[tuple[str, str, str], ...] = (
    ("*", "Zone Mean Air Temperature", "Hourly"),
    ("*", "Zone Air Relative Humidity", "Hourly"),
    ("*", "Zone Ideal Loads Supply Air Total Heating Energy", "Hourly"),
    ("*", "Zone Ideal Loads Supply Air Total Cooling Energy", "Hourly"),
    ("*", "Zone Lights Electricity Energy", "Hourly"),
    ("*", "Zone People Total Heating Energy", "Hourly"),
    ("", "Facility Total HVAC Electricity Demand Rate", "Hourly"),
)


def _ensure_default_output_variables(config: ConfigState) -> None:
    """Populate `config.output_variable` with office-default monitoring set
    if the user / LLM has not specified any."""
    if config.output_variable:
        return
    for key, name, freq in _DEFAULT_OUTPUT_VARIABLES:
        config.output_variable.append(
            OutputVariableSchema.model_validate(
                {
                    "Key Value": key,
                    "Variable Name": name,
                    "Reporting Frequency": freq,
                }
            )
        )


def simulate_node(state: AgentState, runtime: Runtime[SimContext]) -> AgentStateUpdate:
    """Export YAML -> IDF and run EnergyPlus.

    `WorkflowTool.run_simulation` does the full pipeline:
    validate -> export YAML -> convert to IDF -> run eplus.
    """
    ctx = runtime.context

    config = state.config_state.model_copy(deep=True)
    _ensure_default_output_variables(config)

    # E4 (spec §5.2 call point 5 / §8.2): hand the contract + context through
    # EXPLICITLY. WorkflowTool re-validates at the pre-export and post-convert
    # gates; a missing contract means the explicit legacy_unbound policy.
    workflow = WorkflowTool(
        config,
        output_coordinates=state.output_coordinate_contract,
        validation_context=state.output_coordinate_context,
        zone_frame_normalizations=state.zone_frame_normalizations,
    )
    if ctx.run_simulate:
        response = workflow.run_simulation(
            epw_path=str(ctx.epw_path.resolve().absolute()),
            output_dir=str(ctx.output_dir.resolve().absolute()),
            ep_run_subdir=ctx.ep_run_subdir,
        )
    else:
        response = workflow.export_idf_only(
            output_dir=str(ctx.output_dir.resolve().absolute()),
        )

    message = f"[simulate] {response.message}"
    if response.success and isinstance(response.data, dict):
        message += f" idf={response.data.get('idf_path')}"

    return AgentStateUpdate(messages=[AIMessage(content=message)])
