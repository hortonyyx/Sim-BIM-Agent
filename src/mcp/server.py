from pathlib import Path

from fastmcp import FastMCP

from src.mcp.api import (
    register_core_tools,
    register_envelope_tools,
    register_hvac_tools,
    register_load_tools,
    register_resources,
    register_schedule_tools,
    register_workflow_tools,
)
from src.mcp.state import ConfigState
from src.mcp.tools import (
    BuildingTool,
    ConstructionTool,
    FenestrationTool,
    IdealLoadsSystemTool,
    LightTool,
    LocationTool,
    MaterialTool,
    PeopleTool,
    ScheduleCompactTool,
    ScheduleTypeLimitsTool,
    SurfaceTool,
    ThermostatTool,
    WorkflowTool,
    ZoneTool,
)
from src.validator.data_model import BaseSchema

_SCHEMA_INITIALIZED = False


def _ensure_schema_initialized() -> None:
    """Ensure the EnergyPlus IDD schema is loaded for validation."""
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    BaseSchema.set_idf(Path("./data/dependencies/Energy+.idd"))
    _SCHEMA_INITIALIZED = True


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all EnergyPlus tools.

    Initializes the schema, creates a shared ConfigState, instantiates all
    tool classes, and registers them with the FastMCP server.

    Returns:
        Configured FastMCP server instance ready to run.
    """
    _ensure_schema_initialized()

    mcp = FastMCP(
        name="EnergyPlus Agent",
        version="0.1.0",
        instructions="EnergyPlus Agent is a tool for building energy simulation.",
    )

    state = ConfigState()

    zone_tool = ZoneTool(state)
    building_tool = BuildingTool(state)
    location_tool = LocationTool(state)
    workflow_tool = WorkflowTool(state)
    material_tool = MaterialTool(state)
    construction_tool = ConstructionTool(state)
    surface_tool = SurfaceTool(state)
    fenestration_tool = FenestrationTool(state)
    schedule_type_limits_tool = ScheduleTypeLimitsTool(state)
    schedule_compact_tool = ScheduleCompactTool(state)
    thermostat_tool = ThermostatTool(state)
    ideal_loads_system_tool = IdealLoadsSystemTool(state)
    people_tool = PeopleTool(state)
    light_tool = LightTool(state)

    register_core_tools(
        mcp=mcp,
        building_tool=building_tool,
        location_tool=location_tool,
        zone_tool=zone_tool,
        surface_tool=surface_tool,
    )
    register_schedule_tools(
        mcp=mcp,
        schedule_type_limits_tool=schedule_type_limits_tool,
        schedule_compact_tool=schedule_compact_tool,
    )
    register_envelope_tools(
        mcp=mcp,
        material_tool=material_tool,
        construction_tool=construction_tool,
        surface_tool=surface_tool,
        fenestration_tool=fenestration_tool,
    )
    register_hvac_tools(
        mcp=mcp,
        thermostat_tool=thermostat_tool,
        ideal_loads_system_tool=ideal_loads_system_tool,
    )
    register_load_tools(
        mcp=mcp,
        people_tool=people_tool,
        light_tool=light_tool,
    )
    register_workflow_tools(mcp=mcp, workflow_tool=workflow_tool)
    register_resources(mcp=mcp, state=state)

    return mcp


mcp = create_mcp_server()


if __name__ == "__main__":
    mcp.run()
