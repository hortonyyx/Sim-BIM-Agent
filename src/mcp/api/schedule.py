from fastmcp import FastMCP
from pydantic import Field

from src.mcp.api.common import ToolInput, to_payload
from src.mcp.tools import ScheduleCompactTool, ScheduleTypeLimitsTool


class ScheduleTypeLimitsInput(ToolInput):
    """Input schema for creating a new ScheduleTypeLimits object."""

    name: str = Field(
        alias="Name", description="Unique name for the schedule type limits."
    )
    lower_limit_value: float = Field(
        alias="Lower Limit Value",
        description="Minimum allowed value for schedules using this type.",
    )
    upper_limit_value: float = Field(
        alias="Upper Limit Value",
        description="Maximum allowed value for schedules using this type.",
    )
    numeric_type: str = Field(
        alias="Numeric Type",
        description="Numeric type: 'Continuous' or 'Discrete'.",
    )
    unit_type: str = Field(
        alias="Unit Type",
        description="Unit type (e.g. 'Temperature', 'Dimensionless', 'Availability').",
    )


class ScheduleTypeLimitsUpdateInput(ToolInput):
    """Input schema for updating an existing ScheduleTypeLimits object."""

    name: str = Field(
        alias="Name", description="Name of the schedule type limits to update."
    )
    lower_limit_value: float | None = Field(
        default=None,
        alias="Lower Limit Value",
        description="Minimum allowed value for schedules using this type.",
    )
    upper_limit_value: float | None = Field(
        default=None,
        alias="Upper Limit Value",
        description="Maximum allowed value for schedules using this type.",
    )
    numeric_type: str | None = Field(
        default=None,
        alias="Numeric Type",
        description="Numeric type: 'Continuous' or 'Discrete'.",
    )
    unit_type: str | None = Field(
        default=None,
        alias="Unit Type",
        description="Unit type (e.g. 'Temperature', 'Dimensionless', 'Availability').",
    )


class ScheduleCompactInput(ToolInput):
    """Input schema for creating a new Schedule:Compact object."""

    name: str = Field(alias="Name", description="Unique name for the compact schedule.")
    schedule_type_limits_name: str = Field(
        alias="Schedule Type Limits Name",
        description="Name of the ScheduleTypeLimits that governs this schedule.",
    )
    times: list[dict] = Field(
        alias="Data",
        description="Schedule data entries with time periods and values.",
    )


class ScheduleCompactUpdateInput(ToolInput):
    """Input schema for updating an existing Schedule:Compact object."""

    name: str = Field(
        alias="Name", description="Name of the compact schedule to update."
    )
    schedule_type_limits_name: str | None = Field(
        default=None,
        alias="Schedule Type Limits Name",
        description="Name of the ScheduleTypeLimits that governs this schedule.",
    )
    times: list[dict] | None = Field(
        default=None,
        alias="Data",
        description="Schedule data entries with time periods and values.",
    )


def register_schedule_tools(
    mcp: FastMCP,
    schedule_type_limits_tool: ScheduleTypeLimitsTool,
    schedule_compact_tool: ScheduleCompactTool,
) -> None:
    """Register schedule tools (ScheduleTypeLimits, Schedule:Compact) with the MCP server.

    Args:
        mcp: FastMCP server instance.
        schedule_type_limits_tool: ScheduleTypeLimitsTool instance.
        schedule_compact_tool: ScheduleCompactTool instance.
    """

    @mcp.tool
    def create_schedule_type_limits(
        name: str,
        lower_limit_value: float,
        upper_limit_value: float,
        numeric_type: str,
        unit_type: str,
    ) -> dict:
        """Create a new ScheduleTypeLimits object.

        Args:
            name: Unique name for the schedule type limits.
            lower_limit_value: Minimum allowed schedule value.
            upper_limit_value: Maximum allowed schedule value.
            numeric_type: 'Continuous' or 'Discrete'.
            unit_type: Unit type (e.g. 'Temperature', 'Dimensionless').

        Returns:
            MCP response with the created schedule type limits data.
        """
        payload = to_payload(
            ScheduleTypeLimitsInput.model_validate(
                {
                    "name": name,
                    "lower_limit_value": lower_limit_value,
                    "upper_limit_value": upper_limit_value,
                    "numeric_type": numeric_type,
                    "unit_type": unit_type,
                }
            )
        )
        return schedule_type_limits_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_schedule_type_limits(name: str) -> dict:
        """Retrieve an existing ScheduleTypeLimits by name.

        Args:
            name: Name of the schedule type limits to retrieve.

        Returns:
            MCP response with the schedule type limits data.
        """
        return schedule_type_limits_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_schedule_type_limits(
        name: str,
        lower_limit_value: float,
        upper_limit_value: float,
        numeric_type: str,
        unit_type: str,
    ) -> dict:
        """Update an existing ScheduleTypeLimits object.

        Args:
            name: Name of the schedule type limits to update.
            lower_limit_value: New minimum allowed value.
            upper_limit_value: New maximum allowed value.
            numeric_type: New numeric type.
            unit_type: New unit type.

        Returns:
            MCP response with the updated schedule type limits data.
        """
        payload = to_payload(
            ScheduleTypeLimitsUpdateInput.model_validate(
                {
                    "name": name,
                    "lower_limit_value": lower_limit_value,
                    "upper_limit_value": upper_limit_value,
                    "numeric_type": numeric_type,
                    "unit_type": unit_type,
                }
            )
        )
        return schedule_type_limits_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_schedule_type_limits(name: str) -> dict:
        """Delete a ScheduleTypeLimits object by name.

        Args:
            name: Name of the schedule type limits to delete.

        Returns:
            MCP response with deletion result.
        """
        return schedule_type_limits_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_schedule_type_limits() -> dict:
        """List all ScheduleTypeLimits objects in the configuration.

        Returns:
            MCP response with a list of all schedule type limits.
        """
        return schedule_type_limits_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_schedule_compact(
        name: str,
        schedule_type_limits_name: str,
        times: list[dict],
    ) -> dict:
        """Create a new Schedule:Compact object.

        Args:
            name: Unique name for the compact schedule.
            schedule_type_limits_name: Name of the governing schedule type limits.
            times: Schedule data entries with time periods and values.

        Returns:
            MCP response with the created compact schedule data.
        """
        payload = to_payload(
            ScheduleCompactInput.model_validate(
                {
                    "name": name,
                    "schedule_type_limits_name": schedule_type_limits_name,
                    "times": times,
                }
            )
        )
        return schedule_compact_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_schedule_compact(name: str) -> dict:
        """Retrieve an existing Schedule:Compact by name.

        Args:
            name: Name of the compact schedule to retrieve.

        Returns:
            MCP response with the compact schedule data.
        """
        return schedule_compact_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_schedule_compact(
        name: str,
        schedule_type_limits_name: str,
        times: list[dict],
    ) -> dict:
        """Update an existing Schedule:Compact object.

        Args:
            name: Name of the compact schedule to update.
            schedule_type_limits_name: New schedule type limits name.
            times: New schedule data entries.

        Returns:
            MCP response with the updated compact schedule data.
        """
        payload = to_payload(
            ScheduleCompactUpdateInput.model_validate(
                {
                    "name": name,
                    "schedule_type_limits_name": schedule_type_limits_name,
                    "times": times,
                }
            )
        )
        return schedule_compact_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_schedule_compact(name: str) -> dict:
        """Delete a Schedule:Compact object by name.

        Args:
            name: Name of the compact schedule to delete.

        Returns:
            MCP response with deletion result.
        """
        return schedule_compact_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_schedule_compacts() -> dict:
        """List all Schedule:Compact objects in the configuration.

        Returns:
            MCP response with a list of all compact schedules.
        """
        return schedule_compact_tool.list_all().to_mcp_response()
