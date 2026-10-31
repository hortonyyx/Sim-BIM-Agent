from fastmcp import FastMCP
from pydantic import Field

from src.mcp.api.common import ToolInput, to_payload
from src.mcp.tools import LightTool, PeopleTool


class PeopleCreateInput(ToolInput):
    """Input schema for creating a new People (occupancy) object."""

    name: str = Field(alias="Name", description="Unique name for the people object.")
    zone_or_zonelist_or_space_or_spacelist_name: str = Field(
        alias="Zone or ZoneList or Space or SpaceList Name",
        description="Name of the zone, zone list, space, or space list to assign occupants to.",
    )
    number_of_people_schedule_name: str = Field(
        alias="Number of People Schedule Name",
        description="Name of the schedule controlling occupancy over time.",
    )
    activity_level_schedule_name: str = Field(
        alias="Activity Level Schedule Name",
        description="Name of the schedule defining metabolic activity level in W/person.",
    )
    number_of_people_calculation_method: str = Field(
        default="People",
        alias="Number of People Calculation Method",
        description="Calculation method: 'People', 'People/Area', or 'Area/Person'.",
    )
    number_of_people: float | None = Field(
        default=None,
        alias="Number of People",
        description="Total number of people when method is 'People'.",
    )
    people_per_floor_area: float | None = Field(
        default=None,
        alias="People per Floor Area",
        description="People density in people/m² when method is 'People/Area'.",
    )
    floor_area_per_person: float | None = Field(
        default=None,
        alias="Floor Area per Person",
        description="Floor area per person in m²/person when method is 'Area/Person'.",
    )
    fraction_radiant: float = Field(
        default=0.3,
        alias="Fraction Radiant",
        description="Fraction of sensible heat gain that is radiant (0.0-1.0).",
    )
    sensible_heat_fraction: float | str = Field(
        default="Autocalculate",
        alias="Sensible Heat Fraction",
        description="Fraction of total heat that is sensible, or 'Autocalculate'.",
    )


class PeopleUpdateInput(ToolInput):
    """Input schema for updating an existing People object."""

    name: str = Field(alias="Name", description="Name of the people object to update.")
    zone_or_zonelist_or_space_or_spacelist_name: str | None = Field(
        default=None,
        alias="Zone or ZoneList or Space or SpaceList Name",
        description="Name of the zone, zone list, space, or space list.",
    )
    number_of_people_schedule_name: str | None = Field(
        default=None,
        alias="Number of People Schedule Name",
        description="Name of the occupancy schedule.",
    )
    activity_level_schedule_name: str | None = Field(
        default=None,
        alias="Activity Level Schedule Name",
        description="Name of the activity level schedule.",
    )
    number_of_people_calculation_method: str | None = Field(
        default=None,
        alias="Number of People Calculation Method",
        description="Calculation method: 'People', 'People/Area', or 'Area/Person'.",
    )
    number_of_people: float | None = Field(
        default=None,
        alias="Number of People",
        description="Total number of people.",
    )
    people_per_floor_area: float | None = Field(
        default=None,
        alias="People per Floor Area",
        description="People density in people/m².",
    )
    floor_area_per_person: float | None = Field(
        default=None,
        alias="Floor Area per Person",
        description="Floor area per person in m²/person.",
    )
    fraction_radiant: float | None = Field(
        default=None,
        alias="Fraction Radiant",
        description="Fraction of sensible heat gain that is radiant.",
    )
    sensible_heat_fraction: float | str | None = Field(
        default=None,
        alias="Sensible Heat Fraction",
        description="Fraction of total heat that is sensible, or 'Autocalculate'.",
    )


class LightCreateInput(ToolInput):
    """Input schema for creating a new Lights object."""

    name: str = Field(alias="Name", description="Unique name for the lights object.")
    zone_or_zone_list_or_space_or_space_list_name: str = Field(
        alias="Zone or ZoneList or Space or SpaceList Name",
        description="Name of the zone, zone list, space, or space list for lighting.",
    )
    schedule_name: str = Field(
        alias="Schedule Name",
        description="Name of the schedule controlling lighting operation.",
    )
    design_level_calculation_method: str = Field(
        default="LightingLevel",
        alias="Design Level Calculation Method",
        description="Calculation method: 'LightingLevel', 'Watts/Area', or 'Watts/Person'.",
    )
    lighting_level: float | None = Field(
        default=None,
        alias="Lighting Level",
        description="Total lighting power in Watts when method is 'LightingLevel'.",
    )
    watts_per_floor_area: float | None = Field(
        default=None,
        alias="Watts per Floor Area",
        description="Lighting power density in W/m² when method is 'Watts/Area'.",
    )
    watts_per_person: float | None = Field(
        default=None,
        alias="Watts per Person",
        description="Lighting power per person in W/person when method is 'Watts/Person'.",
    )
    return_air_fraction: float = Field(
        default=0.0,
        alias="Return Air Fraction",
        description="Fraction of heat gain going to return air (0.0-1.0).",
    )
    fraction_radiant: float = Field(
        default=0.0,
        alias="Fraction Radiant",
        description="Fraction of heat gain that is radiant (0.0-1.0).",
    )
    fraction_visible: float = Field(
        default=0.0,
        alias="Fraction Visible",
        description="Fraction of heat gain that is visible (0.0-1.0).",
    )
    fraction_replaceable: float = Field(
        default=1.0,
        alias="Fraction Replaceable",
        description="Fraction of lighting that can be replaced by daylighting (0.0-1.0).",
    )
    end_use_subcategory: str = Field(
        default="General",
        alias="End Use Subcategory",
        description="End-use subcategory for output reporting.",
    )


class LightUpdateInput(ToolInput):
    """Input schema for updating an existing Lights object."""

    name: str = Field(alias="Name", description="Name of the lights object to update.")
    zone_or_zone_list_or_space_or_space_list_name: str | None = Field(
        default=None,
        alias="Zone or ZoneList or Space or SpaceList Name",
        description="Name of the zone, zone list, space, or space list.",
    )
    schedule_name: str | None = Field(
        default=None,
        alias="Schedule Name",
        description="Name of the lighting schedule.",
    )
    design_level_calculation_method: str | None = Field(
        default=None,
        alias="Design Level Calculation Method",
        description="Calculation method: 'LightingLevel', 'Watts/Area', or 'Watts/Person'.",
    )
    lighting_level: float | None = Field(
        default=None,
        alias="Lighting Level",
        description="Total lighting power in Watts.",
    )
    watts_per_floor_area: float | None = Field(
        default=None,
        alias="Watts per Floor Area",
        description="Lighting power density in W/m².",
    )
    watts_per_person: float | None = Field(
        default=None,
        alias="Watts per Person",
        description="Lighting power per person in W/person.",
    )
    return_air_fraction: float | None = Field(
        default=None,
        alias="Return Air Fraction",
        description="Fraction of heat gain going to return air.",
    )
    fraction_radiant: float | None = Field(
        default=None,
        alias="Fraction Radiant",
        description="Fraction of heat gain that is radiant.",
    )
    fraction_visible: float | None = Field(
        default=None,
        alias="Fraction Visible",
        description="Fraction of heat gain that is visible.",
    )
    fraction_replaceable: float | None = Field(
        default=None,
        alias="Fraction Replaceable",
        description="Fraction replaceable by daylighting.",
    )
    end_use_subcategory: str | None = Field(
        default=None,
        alias="End Use Subcategory",
        description="End-use subcategory for output reporting.",
    )


def register_load_tools(
    mcp: FastMCP,
    people_tool: PeopleTool,
    light_tool: LightTool,
) -> None:
    """Register internal load tools (People, Lights) with the MCP server.

    Args:
        mcp: FastMCP server instance.
        people_tool: PeopleTool instance for occupancy operations.
        light_tool: LightTool instance for lighting operations.
    """

    @mcp.tool
    def create_people(
        name: str,
        zone_or_zonelist_or_space_or_spacelist_name: str,
        number_of_people_schedule_name: str,
        activity_level_schedule_name: str,
        number_of_people_calculation_method: str = "People",
        number_of_people: float | None = None,
        people_per_floor_area: float | None = None,
        floor_area_per_person: float | None = None,
        fraction_radiant: float = 0.3,
        sensible_heat_fraction: float | str = "Autocalculate",
    ) -> dict:
        """Create a new People (occupancy) object.

        Args:
            name: Unique name for the people object.
            zone_or_zonelist_or_space_or_spacelist_name: Zone or space assignment.
            number_of_people_schedule_name: Occupancy schedule name.
            activity_level_schedule_name: Activity level schedule name.
            number_of_people_calculation_method: Calculation method.
            number_of_people: Total number of people.
            people_per_floor_area: People density in people/m².
            floor_area_per_person: Area per person in m²/person.
            fraction_radiant: Radiant heat fraction (0.0-1.0).
            sensible_heat_fraction: Sensible heat fraction or 'Autocalculate'.

        Returns:
            MCP response with the created people data.
        """
        payload = to_payload(
            PeopleCreateInput.model_validate(
                {
                    "name": name,
                    "zone_or_zonelist_or_space_or_spacelist_name": zone_or_zonelist_or_space_or_spacelist_name,
                    "number_of_people_schedule_name": number_of_people_schedule_name,
                    "activity_level_schedule_name": activity_level_schedule_name,
                    "number_of_people_calculation_method": number_of_people_calculation_method,
                    "number_of_people": number_of_people,
                    "people_per_floor_area": people_per_floor_area,
                    "floor_area_per_person": floor_area_per_person,
                    "fraction_radiant": fraction_radiant,
                    "sensible_heat_fraction": sensible_heat_fraction,
                }
            )
        )
        return people_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_people(name: str) -> dict:
        """Retrieve an existing People object by name.

        Args:
            name: Name of the people object to retrieve.

        Returns:
            MCP response with the people data.
        """
        return people_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_people(
        name: str,
        zone_or_zonelist_or_space_or_spacelist_name: str | None = None,
        number_of_people_schedule_name: str | None = None,
        activity_level_schedule_name: str | None = None,
        number_of_people_calculation_method: str | None = None,
        number_of_people: float | None = None,
        people_per_floor_area: float | None = None,
        floor_area_per_person: float | None = None,
        fraction_radiant: float | None = None,
        sensible_heat_fraction: float | str | None = None,
    ) -> dict:
        """Update an existing People object.

        Args:
            name: Name of the people object to update.
            zone_or_zonelist_or_space_or_spacelist_name: New zone/space assignment.
            number_of_people_schedule_name: New occupancy schedule.
            activity_level_schedule_name: New activity schedule.
            number_of_people_calculation_method: New calculation method.
            number_of_people: New total number of people.
            people_per_floor_area: New people density.
            floor_area_per_person: New area per person.
            fraction_radiant: New radiant fraction.
            sensible_heat_fraction: New sensible heat fraction.

        Returns:
            MCP response with the updated people data.
        """
        payload = to_payload(
            PeopleUpdateInput.model_validate(
                {
                    "name": name,
                    "zone_or_zonelist_or_space_or_spacelist_name": zone_or_zonelist_or_space_or_spacelist_name,
                    "number_of_people_schedule_name": number_of_people_schedule_name,
                    "activity_level_schedule_name": activity_level_schedule_name,
                    "number_of_people_calculation_method": number_of_people_calculation_method,
                    "number_of_people": number_of_people,
                    "people_per_floor_area": people_per_floor_area,
                    "floor_area_per_person": floor_area_per_person,
                    "fraction_radiant": fraction_radiant,
                    "sensible_heat_fraction": sensible_heat_fraction,
                }
            )
        )
        return people_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_people(name: str) -> dict:
        """Delete a People object by name.

        Args:
            name: Name of the people object to delete.

        Returns:
            MCP response with deletion result.
        """
        return people_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_people() -> dict:
        """List all People objects in the configuration.

        Returns:
            MCP response with a list of all people objects.
        """
        return people_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_light(
        name: str,
        zone_or_zone_list_or_space_or_space_list_name: str,
        schedule_name: str,
        design_level_calculation_method: str = "LightingLevel",
        lighting_level: float | None = None,
        watts_per_floor_area: float | None = None,
        watts_per_person: float | None = None,
        return_air_fraction: float = 0.0,
        fraction_radiant: float = 0.0,
        fraction_visible: float = 0.0,
        fraction_replaceable: float = 1.0,
        end_use_subcategory: str = "General",
    ) -> dict:
        """Create a new Lights object.

        Args:
            name: Unique name for the lights object.
            zone_or_zone_list_or_space_or_space_list_name: Zone or space assignment.
            schedule_name: Lighting schedule name.
            design_level_calculation_method: Calculation method.
            lighting_level: Total lighting power in Watts.
            watts_per_floor_area: Power density in W/m².
            watts_per_person: Power per person in W/person.
            return_air_fraction: Return air heat fraction.
            fraction_radiant: Radiant heat fraction.
            fraction_visible: Visible heat fraction.
            fraction_replaceable: Daylighting replaceable fraction.
            end_use_subcategory: End-use subcategory label.

        Returns:
            MCP response with the created lights data.
        """
        payload = to_payload(
            LightCreateInput.model_validate(
                {
                    "name": name,
                    "zone_or_zone_list_or_space_or_space_list_name": zone_or_zone_list_or_space_or_space_list_name,
                    "schedule_name": schedule_name,
                    "design_level_calculation_method": design_level_calculation_method,
                    "lighting_level": lighting_level,
                    "watts_per_floor_area": watts_per_floor_area,
                    "watts_per_person": watts_per_person,
                    "return_air_fraction": return_air_fraction,
                    "fraction_radiant": fraction_radiant,
                    "fraction_visible": fraction_visible,
                    "fraction_replaceable": fraction_replaceable,
                    "end_use_subcategory": end_use_subcategory,
                }
            )
        )
        return light_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_light(name: str) -> dict:
        """Retrieve an existing Lights object by name.

        Args:
            name: Name of the lights object to retrieve.

        Returns:
            MCP response with the lights data.
        """
        return light_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_light(
        name: str,
        zone_or_zone_list_or_space_or_space_list_name: str | None = None,
        schedule_name: str | None = None,
        design_level_calculation_method: str | None = None,
        lighting_level: float | None = None,
        watts_per_floor_area: float | None = None,
        watts_per_person: float | None = None,
        return_air_fraction: float | None = None,
        fraction_radiant: float | None = None,
        fraction_visible: float | None = None,
        fraction_replaceable: float | None = None,
        end_use_subcategory: str | None = None,
    ) -> dict:
        """Update an existing Lights object.

        Args:
            name: Name of the lights object to update.
            zone_or_zone_list_or_space_or_space_list_name: New zone/space assignment.
            schedule_name: New lighting schedule.
            design_level_calculation_method: New calculation method.
            lighting_level: New total power in Watts.
            watts_per_floor_area: New power density in W/m².
            watts_per_person: New power per person.
            return_air_fraction: New return air fraction.
            fraction_radiant: New radiant fraction.
            fraction_visible: New visible fraction.
            fraction_replaceable: New replaceable fraction.
            end_use_subcategory: New end-use subcategory.

        Returns:
            MCP response with the updated lights data.
        """
        payload = to_payload(
            LightUpdateInput.model_validate(
                {
                    "name": name,
                    "zone_or_zone_list_or_space_or_space_list_name": zone_or_zone_list_or_space_or_space_list_name,
                    "schedule_name": schedule_name,
                    "design_level_calculation_method": design_level_calculation_method,
                    "lighting_level": lighting_level,
                    "watts_per_floor_area": watts_per_floor_area,
                    "watts_per_person": watts_per_person,
                    "return_air_fraction": return_air_fraction,
                    "fraction_radiant": fraction_radiant,
                    "fraction_visible": fraction_visible,
                    "fraction_replaceable": fraction_replaceable,
                    "end_use_subcategory": end_use_subcategory,
                }
            )
        )
        return light_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_light(name: str) -> dict:
        """Delete a Lights object by name.

        Args:
            name: Name of the lights object to delete.

        Returns:
            MCP response with deletion result.
        """
        return light_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_lights() -> dict:
        """List all Lights objects in the configuration.

        Returns:
            MCP response with a list of all lights objects.
        """
        return light_tool.list_all().to_mcp_response()
