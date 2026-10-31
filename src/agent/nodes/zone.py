from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

from src.agent.llm import create_llm
from src.agent.output_coordinates import zero_zone_frames_with_audit
from src.agent.react import ReactState, build_react_agent
from src.agent.state import AgentState, AgentStateUpdate
from src.agent.tools import make_zone_tools
from src.agent.trace import TraceCollector, record_phase_trace

ZONE_SYSTEM_PROMPT = """You are a thermal zone creation expert for EnergyPlus.
Given zone specifications, create all required zones using create_zone tool.

Rules:
- Zone names are deterministic public names from zone_specs:
  'Z{NN}_F{floor}_{Role}_{Quadrant}' (e.g., 'Z01_F1_Office_SW').
  Transcribe them exactly; do not derive names from source cell ids.
- Set z_origin to the floor's lower elevation: ground floor = 0,
  floor 2 = first-floor height (e.g., 3.0), etc.
- direction_of_relative_north is 0 unless the description specifies a rotation.
- multiplier is 1 unless the description explicitly duplicates a typical floor.
- After creating all zones, call list_zones once to verify, then stop with
  a one-line summary of zone count and names.
"""

# E4-output-contract spec v2 §6.2 item 2: the Relative-contract prompt.
# Selected ONLY by contract.zone_origin_policy (explicit dispatch) — never by
# inspecting theta or GGR values. Every frame field must be 0: the floor's
# elevation is already carried by the surface vertices' absolute z values, and
# Building.North Axis (owned upstream) carries the building's true-north angle.
ZONE_SYSTEM_PROMPT_RELATIVE = """You are a thermal zone creation expert for EnergyPlus.
Given zone specifications, create all required zones using create_zone tool.

Rules:
- Zone names are deterministic public names from zone_specs:
  'Z{NN}_F{floor}_{Role}_{Quadrant}' (e.g., 'Z01_F1_Office_SW').
  Transcribe them exactly; do not derive names from source cell ids.
- x_origin, y_origin, z_origin and direction_of_relative_north MUST all be 0
  for every zone (or simply omit them). The floor's elevation is already in
  the surface vertices — do NOT copy it into z_origin. Zone rotation is not
  used; the building's orientation is owned by Building.North Axis upstream.
- multiplier is 1 unless the description explicitly duplicates a typical floor.
- After creating all zones, call list_zones once to verify, then stop with
  a one-line summary of zone count and names.
"""


def zone_agent(state: AgentState) -> AgentStateUpdate:
    local = state.config_state.model_copy(deep=True)
    contract = state.output_coordinate_contract
    tools = make_zone_tools(local, contract=contract)
    collector = TraceCollector(phase="zone")

    system_prompt = (
        ZONE_SYSTEM_PROMPT_RELATIVE
        if contract is not None and contract.zone_origin_policy == "all_zero"
        else ZONE_SYSTEM_PROMPT
    )
    agent = build_react_agent(
        llm=create_llm(node_name="zone"),
        tools=tools,
        system_prompt=system_prompt,
        trace_collector=collector,
    )

    specs = state.intake_output.zone_specs if state.intake_output else state.user_input
    result = agent.invoke(ReactState(messages=[HumanMessage(content=specs)]))

    final = [
        m for m in result["messages"] if isinstance(m, AIMessage) and not m.tool_calls
    ]
    summary = final[-1].content if final else "zone done"

    record_phase_trace("zone", collector.export())

    # E4-output-contract spec v2 §6.2 item 4: unconditional tail-of-stage
    # normalizer. Under an all_zero policy this overwrites ANY x/y/z/direction
    # the LLM wrote (even if `create_zone`/`update_zone` already rejected
    # nonzero writes — belt-and-suspenders against any bypass) and records a
    # deterministic before/after audit; under preserve_legacy it is a no-op.
    normalizations = ()
    if contract is not None:
        local, normalizations = zero_zone_frames_with_audit(local, contract)
        if normalizations:
            logger.info(
                "zone_agent: normalized {} zone frame(s) to (0,0,0,0) under contract mode={}",
                len(normalizations), contract.mode,
            )

    return AgentStateUpdate(
        config_state=local,
        zone_frame_normalizations=normalizations,
        messages=[AIMessage(content=f"[zone] {summary}")],
    )
