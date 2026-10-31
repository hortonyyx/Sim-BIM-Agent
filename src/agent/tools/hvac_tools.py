from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.mcp.tools.hvac import IdealLoadsSystemTool, ThermostatTool
from src.mcp.tools.schedule import ScheduleCompactTool
from src.mcp.tools.zone import ZoneTool


def make_hvac_tools(config: ConfigState) -> list[BaseTool]:
    tt = ThermostatTool(config)
    ilst = IdealLoadsSystemTool(config)

    @tool
    def create_thermostat(
        name: str,
        heating_setpoint_schedule_name: str,
        cooling_setpoint_schedule_name: str,
    ) -> str:
        """Create an HVACTemplate:Thermostat.

        Args:
            name: Unique thermostat name.
            heating_setpoint_schedule_name: Existing Schedule:Compact for heating setpoints (C).
            cooling_setpoint_schedule_name: Existing Schedule:Compact for cooling setpoints (C).
        """
        return tt.create(
            {
                "Name": name,
                "Heating Setpoint Schedule Name": heating_setpoint_schedule_name,
                "Cooling Setpoint Schedule Name": cooling_setpoint_schedule_name,
            }
        ).model_dump_json()

    @tool
    def create_ideal_loads_system(
        zone_name: str,
        template_thermostat_name: str,
        system_availability_schedule_name: str | None = None,
    ) -> str:
        """Create an HVACTemplate:Zone:IdealLoadsAirSystem (one per zone).

        Args:
            zone_name: Existing Zone name. Acts as the identity key (no separate Name).
            template_thermostat_name: Existing HVACTemplate:Thermostat name.
            system_availability_schedule_name: Optional availability Schedule:Compact.
        """
        return ilst.create(
            {
                "Zone Name": zone_name,
                "Template Thermostat Name": template_thermostat_name,
                "System Availability Schedule Name": system_availability_schedule_name,
            }
        ).model_dump_json()

    @tool
    def list_thermostats() -> str:
        """List all thermostats."""
        return tt.list_all().model_dump_json()

    @tool
    def list_ideal_loads_systems() -> str:
        """List all IdealLoadsAirSystem entries (keyed by zone_name)."""
        return ilst.list_all().model_dump_json()

    @tool
    def delete_thermostat(name: str) -> str:
        """Delete a thermostat. Fails if referenced by an IdealLoadsSystem."""
        return tt.delete(name).model_dump_json()

    @tool
    def delete_ideal_loads_system(zone_name: str) -> str:
        """Delete an IdealLoadsSystem by its zone_name."""
        return ilst.delete(zone_name).model_dump_json()

    @tool
    def list_zones() -> str:
        """Read-only: list zones an IdealLoadsAirSystem can be attached to."""
        return ZoneTool(config).list_all().model_dump_json()

    @tool
    def list_schedules() -> str:
        """Read-only: list Schedule:Compact objects (setpoint / availability references)."""
        return ScheduleCompactTool(config).list_all().model_dump_json()

    return [
        create_thermostat,
        create_ideal_loads_system,
        list_thermostats,
        list_ideal_loads_systems,
        delete_thermostat,
        delete_ideal_loads_system,
        list_zones,
        list_schedules,
    ]
