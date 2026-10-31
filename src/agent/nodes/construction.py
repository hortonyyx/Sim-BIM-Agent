from langchain_core.messages import AIMessage

from src.agent.llm import create_llm
from src.agent.nodes._share import invoke_with_self_repair
from src.agent.react import build_react_agent
from src.agent.state import AgentState, AgentStateUpdate
from src.agent.tools import make_construction_tools
from src.agent.trace import TraceCollector, record_phase_trace

CONSTRUCTION_SYSTEM_PROMPT = """You are a construction-assembly expert for EnergyPlus.
Given construction specifications, create all required Construction objects.

Workflow:
1. FIRST call `list_materials` to discover which materials are already
   defined and their full properties (thickness, conductivity, U-Factor
   for glazing, etc.). DO NOT skip this step — the materials phase uses
   names that may differ from what the intake spec suggested.
2. Pick the correct layer composition for each construction using the
   material names returned by list_materials, verbatim.
3. Call `create_construction` for each construction in the spec.
4. Call `list_constructions` once at the end to confirm.

Rules:
- Layer names passed to `create_construction` MUST appear verbatim in
  the list_materials result (exact case, underscores, dashes, numbers).
- If a needed material is missing from list_materials, STOP and report
  the gap; do NOT invent names or call create with a broken reference.
- Each Construction is an ordered list of layers from OUTSIDE to INSIDE.
- Use separate constructions per surface type when thermal properties differ
  (e.g., 'ExtWall_Office', 'IntWall_Office', 'Roof_Office', 'Floor_Office',
  'Window_Office').
- For fenestration, the construction's only layer is the glazing material.
"""


def construction_agent(state: AgentState) -> AgentStateUpdate:
    local = state.config_state.model_copy(deep=True)
    tools = make_construction_tools(local)
    collector = TraceCollector(phase="construction")

    agent = build_react_agent(
        llm=create_llm(node_name="construction"),
        tools=tools,
        system_prompt=CONSTRUCTION_SYSTEM_PROMPT,
        trace_collector=collector,
    )

    specs = (
        state.intake_output.construction_specs
        if state.intake_output
        else state.user_input
    )
    result = invoke_with_self_repair(agent, local, specs, phase="construction")

    final = [
        m for m in result["messages"] if isinstance(m, AIMessage) and not m.tool_calls
    ]
    summary = final[-1].content if final else "construction done"

    record_phase_trace("construction", collector.export())
    return AgentStateUpdate(
        config_state=local,
        messages=[AIMessage(content=f"[construction] {summary}")],
    )
