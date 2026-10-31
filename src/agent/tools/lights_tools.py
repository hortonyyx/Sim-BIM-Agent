from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.mcp.tools.light import LightTool
from src.mcp.tools.schedule import ScheduleCompactTool
from src.mcp.tools.zone import ZoneTool


def make_lights_tools(config: ConfigState) -> list[BaseTool]:
    lt = LightTool(config)

    @tool
    def create_light(
        name: str,
        zone_name: str,
        schedule_name: str,
        design_level_calculation_method: str = "Watts/Area",
        lighting_level: float = 0.0,
        watts_per_floor_area: float = 0.0,
        watts_per_person: float = 0.0,
        fraction_radiant: float = 0.0,
        fraction_visible: float = 0.0,
    ) -> str:
        """Create a Lights (lighting load) object.

        Args:
            name: Unique lights object name.
            zone_name: Existing Zone name.
            schedule_name: Existing Schedule:Compact (Fraction).
            design_level_calculation_method: LightingLevel / Watts/Area / Watts/Person.
            lighting_level: Absolute watts (when method=LightingLevel).
            watts_per_floor_area: W/m^2 (when method=Watts/Area).
            watts_per_person: W/person (when method=Watts/Person).
            fraction_radiant: Radiant fraction (0-1).
            fraction_visible: Visible light fraction (0-1).
        """
        return lt.create(
            {
                "Name": name,
                "Zone or ZoneList or Space or SpaceList Name": zone_name,
                "Schedule Name": schedule_name,
                "Design Level Calculation Method": design_level_calculation_method,
                "Lighting Level": lighting_level,
                "Watts per Floor Area": watts_per_floor_area,
                "Watts per Person": watts_per_person,
                "Fraction Radiant": fraction_radiant,
                "Fraction Visible": fraction_visible,
            }
        ).model_dump_json()

    @tool
    def list_lights() -> str:
        """List all Lights objects."""
        return lt.list_all().model_dump_json()

    @tool
    def delete_light(name: str) -> str:
        """Delete a Lights object."""
        return lt.delete(name).model_dump_json()

    @tool
    def list_zones() -> str:
        """Read-only: list zones a Lights load can be assigned to."""
        return ZoneTool(config).list_all().model_dump_json()

    @tool
    def list_schedules() -> str:
        """Read-only: list Schedule:Compact (for schedule_name reference)."""
        return ScheduleCompactTool(config).list_all().model_dump_json()

    return [create_light, list_lights, delete_light, list_zones, list_schedules]
