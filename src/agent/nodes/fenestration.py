from langchain_core.messages import AIMessage

from src.agent.llm import create_llm
from src.agent.nodes._share import invoke_with_self_repair
from src.agent.react import build_react_agent
from src.agent.state import AgentState, AgentStateUpdate
from src.agent.tools import make_fenestration_tools
from src.agent.trace import TraceCollector, record_phase_trace

FENESTRATION_SYSTEM_PROMPT = """You are a window/door geometry expert for EnergyPlus.
Given fenestration specifications, create FenestrationSurface:Detailed
objects (windows, doors, skylights) that lie on existing parent surfaces.

Vertices MUST be a list of dicts with explicit X / Y / Z keys (not a
bare [x, y, z] list). fenestration_specs already lists, for every window,
its COMPLETE vertex polygon as absolute world-coordinate (X, Y, Z) tuples,
already in CCW-from-outside order. Transcribe those vertices verbatim —
do NOT recompute, re-derive, reorder, round, drop, or add vertices, and do
NOT derive vertex coordinates from a window-to-wall ratio (WWR). The only
"work" you do on a vertex is copy it — no arithmetic.

Workflow:
1. FIRST call `list_surfaces` to see parent surface names — you need the
   parent surface name to reference in `building_surface_name`.
2. THEN call `list_constructions` to find glazing/door construction names.
3. Create each fenestration via `create_fenestration`, transcribing
   fenestration_specs' own vertex list for that window exactly (same
   coordinates, same order).
4. Call `list_fenestrations` once at the end to confirm.

Rules:
- `building_surface_name` and `construction_name` MUST appear verbatim
  in the list_surfaces / list_constructions results.
- If a needed surface or construction is missing after list, STOP and
  report; do NOT invent names.
- construction_name should be a Glazing construction for windows/skylights.
- >= 3 vertices, counter-clockwise from OUTSIDE, and MUST lie on the
  parent surface's plane (coplanar — share one coordinate for walls).
  fenestration_specs' own vertices already satisfy this — transcribe them
  as given; do NOT re-derive or re-sort the vertex order yourself.
- surface_type is Window, Door, or GlassDoor.
- Window names are deterministic public names from fenestration_specs:
  '{parent_wall}_Win{k}' (e.g., 'Z01_W1_Win1'). Transcribe them exactly.
"""


def fenestration_agent(state: AgentState) -> AgentStateUpdate:
    local = state.config_state.model_copy(deep=True)
    tools = make_fenestration_tools(local)
    collector = TraceCollector(phase="fenestration")

    agent = build_react_agent(
        llm=create_llm(node_name="fenestration"),
        tools=tools,
        system_prompt=FENESTRATION_SYSTEM_PROMPT,
        trace_collector=collector,
    )

    specs = (
        state.intake_output.fenestration_specs
        if state.intake_output
        else state.user_input
    )
    result = invoke_with_self_repair(agent, local, specs, phase="fenestration")

    final = [
        m for m in result["messages"] if isinstance(m, AIMessage) and not m.tool_calls
    ]
    summary = final[-1].content if final else "fenestration done"

    record_phase_trace("fenestration", collector.export())
    return AgentStateUpdate(
        config_state=local,
        messages=[AIMessage(content=f"[fenestration] {summary}")],
    )
