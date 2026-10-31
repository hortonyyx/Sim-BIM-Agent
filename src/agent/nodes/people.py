from langchain_core.messages import AIMessage

from src.agent.llm import create_llm
from src.agent.nodes._share import invoke_with_self_repair
from src.agent.react import build_react_agent
from src.agent.state import AgentState, AgentStateUpdate
from src.agent.tools import make_people_tools
from src.agent.trace import TraceCollector, record_phase_trace

PEOPLE_SYSTEM_PROMPT = """You are an occupancy-load expert for EnergyPlus.
For each specified zone, create a People object via create_people.

Workflow:
1. FIRST call `list_zones` to see the exact zone names.
2. FIRST call `list_schedules` to see the exact Schedule:Compact names
   (you need occupancy fraction + activity level schedules).
3. Create a People object per zone via `create_people`.
4. Call `list_people` once at the end to confirm.

Rules:
- `zone_name`, `number_of_people_schedule_name`, `activity_level_schedule_name`
  MUST all appear verbatim in the list_zones / list_schedules results.
- If a needed zone or schedule is missing, STOP and report; do NOT invent names.
- name convention: '{zone}_People', where zone is the exact deterministic
  zone name from list_zones, e.g. 'Z01_F1_Office_SW_People'.
- Choose number_of_people_calculation_method based on input:
    * 'People' -> supply number_of_people (absolute count)
    * 'People/Area' -> supply people_per_floor_area (people/m^2)
    * 'Area/Person' -> supply floor_area_per_person (m^2/person)
- Typical office density: 10 m^2/person (People/Area ~ 0.1).
- fraction_radiant defaults to 0.3 for seated activity.
"""


def people_agent(state: AgentState) -> AgentStateUpdate:
    local = state.config_state.model_copy(deep=True)
    tools = make_people_tools(local)
    collector = TraceCollector(phase="people")

    agent = build_react_agent(
        llm=create_llm(node_name="people"),
        tools=tools,
        system_prompt=PEOPLE_SYSTEM_PROMPT,
        trace_collector=collector,
    )

    specs = (
        state.intake_output.people_specs if state.intake_output else state.user_input
    )
    result = invoke_with_self_repair(agent, local, specs, phase="people")

    final = [
        m for m in result["messages"] if isinstance(m, AIMessage) and not m.tool_calls
    ]
    summary = final[-1].content if final else "people done"

    record_phase_trace("people", collector.export())
    return AgentStateUpdate(
        config_state=local,
        messages=[AIMessage(content=f"[people] {summary}")],
    )
