from fastmcp import FastMCP
from pydantic import Field

from src.mcp.api.common import ToolInput, to_payload
from src.mcp.tools import IdealLoadsSystemTool, ThermostatTool


class ThermostatCreateInput(ToolInput):
    """Input schema for creating a new HVACTemplate:Thermostat object."""

    name: str = Field(alias="Name", description="Unique name for the thermostat.")
    heating_setpoint_schedule_name: str = Field(
        alias="Heating Setpoint Schedule Name",
        description="Name of the schedule defining heating setpoint temperatures.",
    )
    cooling_setpoint_schedule_name: str = Field(
        alias="Cooling Setpoint Schedule Name",
        description="Name of the schedule defining cooling setpoint temperatures.",
    )


class ThermostatUpdateInput(ToolInput):
    """Input schema for updating an existing HVACTemplate:Thermostat object."""

    name: str = Field(alias="Name", description="Name of the thermostat to update.")
    heating_setpoint_schedule_name: str | None = Field(
        default=None,
        alias="Heating Setpoint Schedule Name",
        description="Name of the schedule defining heating setpoint temperatures.",
    )
    cooling_setpoint_schedule_name: str | None = Field(
        default=None,
        alias="Cooling Setpoint Schedule Name",
        description="Name of the schedule defining cooling setpoint temperatures.",
    )


class IdealLoadsSystemCreateInput(ToolInput):
    """Input schema for creating a new HVACTemplate:Zone:IdealLoadsAirSystem object."""

    zone_name: str = Field(
        alias="Zone Name",
        description="Name of the zone served by this ideal loads system.",
    )
    template_thermostat_name: str = Field(
        alias="Template Thermostat Name",
        description="Name of the thermostat template controlling this system.",
    )
    system_availability_schedule_name: str | None = Field(
        default=None,
        alias="System Availability Schedule Name",
        description="Name of the schedule controlling system availability.",
    )


class IdealLoadsSystemUpdateInput(ToolInput):
    """Input schema for updating an existing HVACTemplate:Zone:IdealLoadsAirSystem object."""

    zone_name: str = Field(
        alias="Zone Name",
        description="Name of the zone served by this ideal loads system.",
    )
    template_thermostat_name: str | None = Field(
        default=None,
        alias="Template Thermostat Name",
        description="Name of the thermostat template controlling this system.",
    )
    system_availability_schedule_name: str | None = Field(
        default=None,
        alias="System Availability Schedule Name",
        description="Name of the schedule controlling system availability.",
    )


def register_hvac_tools(
    mcp: FastMCP,
    thermostat_tool: ThermostatTool,
    ideal_loads_system_tool: IdealLoadsSystemTool,
) -> None:
    """Register HVAC-related tools (Thermostat, IdealLoadsSystem) with the MCP server.

    Args:
        mcp: FastMCP server instance.
        thermostat_tool: ThermostatTool instance for thermostat operations.
        ideal_loads_system_tool: IdealLoadsSystemTool instance for ideal loads operations.
    """

    @mcp.tool
    def create_hvac_thermostat(
        name: str,
        heating_setpoint_schedule_name: str,
        cooling_setpoint_schedule_name: str,
    ) -> dict:
        """Create a new HVAC thermostat.

        Args:
            name: Unique name for the thermostat.
            heating_setpoint_schedule_name: Heating setpoint schedule name.
            cooling_setpoint_schedule_name: Cooling setpoint schedule name.

        Returns:
            MCP response with the created thermostat data.
        """
        payload = to_payload(
            ThermostatCreateInput.model_validate(
                {
                    "name": name,
                    "heating_setpoint_schedule_name": heating_setpoint_schedule_name,
                    "cooling_setpoint_schedule_name": cooling_setpoint_schedule_name,
                }
            )
        )
        return thermostat_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_hvac_thermostat(name: str) -> dict:
        """Retrieve an existing HVAC thermostat by name.

        Args:
            name: Name of the thermostat to retrieve.

        Returns:
            MCP response with the thermostat data.
        """
        return thermostat_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_hvac_thermostat(
        name: str,
        heating_setpoint_schedule_name: str | None = None,
        cooling_setpoint_schedule_name: str | None = None,
    ) -> dict:
        """Update an existing HVAC thermostat.

        Args:
            name: Name of the thermostat to update.
            heating_setpoint_schedule_name: New heating setpoint schedule name.
            cooling_setpoint_schedule_name: New cooling setpoint schedule name.

        Returns:
            MCP response with the updated thermostat data.
        """
        payload = to_payload(
            ThermostatUpdateInput.model_validate(
                {
                    "name": name,
                    "heating_setpoint_schedule_name": heating_setpoint_schedule_name,
                    "cooling_setpoint_schedule_name": cooling_setpoint_schedule_name,
                }
            )
        )
        return thermostat_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_hvac_thermostat(name: str) -> dict:
        """Delete an HVAC thermostat by name.

        Args:
            name: Name of the thermostat to delete.

        Returns:
            MCP response with deletion result.
        """
        return thermostat_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_hvac_thermostats() -> dict:
        """List all HVAC thermostats in the configuration.

        Returns:
            MCP response with a list of all thermostats.
        """
        return thermostat_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_hvac_ideal_loads_system(
        zone_name: str,
        template_thermostat_name: str,
        system_availability_schedule_name: str | None = None,
    ) -> dict:
        """Create a new HVAC ideal loads air system for a zone.

        Args:
            zone_name: Name of the zone to serve.
            template_thermostat_name: Name of the thermostat template.
            system_availability_schedule_name: Optional availability schedule name.

        Returns:
            MCP response with the created ideal loads system data.
        """
        payload = to_payload(
            IdealLoadsSystemCreateInput.model_validate(
                {
                    "zone_name": zone_name,
                    "template_thermostat_name": template_thermostat_name,
                    "system_availability_schedule_name": system_availability_schedule_name,
                }
            )
        )
        return ideal_loads_system_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_hvac_ideal_loads_system(zone_name: str) -> dict:
        """Retrieve an existing ideal loads system by zone name.

        Args:
            zone_name: Name of the zone to look up.

        Returns:
            MCP response with the ideal loads system data.
        """
        return ideal_loads_system_tool.read(zone_name).to_mcp_response()

    @mcp.tool
    def update_hvac_ideal_loads_system(
        zone_name: str,
        template_thermostat_name: str | None = None,
        system_availability_schedule_name: str | None = None,
    ) -> dict:
        """Update an existing ideal loads system.

        Args:
            zone_name: Name of the zone to update.
            template_thermostat_name: New thermostat template name.
            system_availability_schedule_name: New availability schedule name.

        Returns:
            MCP response with the updated ideal loads system data.
        """
        payload = to_payload(
            IdealLoadsSystemUpdateInput.model_validate(
                {
                    "zone_name": zone_name,
                    "template_thermostat_name": template_thermostat_name,
                    "system_availability_schedule_name": system_availability_schedule_name,
                }
            )
        )
        return ideal_loads_system_tool.update(zone_name, payload).to_mcp_response()

    @mcp.tool
    def delete_hvac_ideal_loads_system(zone_name: str) -> dict:
        """Delete an ideal loads system by zone name.

        Args:
            zone_name: Name of the zone whose system to delete.

        Returns:
            MCP response with deletion result.
        """
        return ideal_loads_system_tool.delete(zone_name).to_mcp_response()

    @mcp.tool
    def list_hvac_ideal_loads_systems() -> dict:
        """List all ideal loads systems in the configuration.

        Returns:
            MCP response with a list of all ideal loads systems.
        """
        return ideal_loads_system_tool.list_all().to_mcp_response()
