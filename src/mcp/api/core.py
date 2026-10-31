from fastmcp import FastMCP
from pydantic import Field

from src.mcp.api.common import (
    ToolInput,
    convert_vertices_to_mcp_format,
    to_payload,
    validate_floor_vertices,
)
from src.mcp.interface import ToolResponse
from src.mcp.tools import BuildingTool, LocationTool, SurfaceTool, ZoneTool


class BuildingCreateInput(ToolInput):
    """Input schema for creating a new Building object."""

    name: str = Field(alias="Name", description="Unique name for the building.")
    north_axis: float = Field(
        alias="North Axis",
        description="Building north axis angle in degrees from true north.",
    )
    terrain: str = Field(
        alias="Terrain",
        description="Terrain type for wind and solar calculations (e.g. Suburbs, City, Ocean).",
    )


class BuildingUpdateInput(ToolInput):
    """Input schema for updating an existing Building object."""

    name: str = Field(alias="Name", description="Name of the building to update.")
    north_axis: float = Field(
        alias="North Axis",
        description="Building north axis angle in degrees from true north.",
    )
    terrain: str = Field(
        alias="Terrain",
        description="Terrain type for wind and solar calculations.",
    )


class LocationCreateInput(ToolInput):
    """Input schema for creating a new Site:Location object."""

    name: str = Field(alias="Name", description="Unique name for the site location.")
    latitude: float = Field(
        alias="Latitude", description="Site latitude in degrees (-90 to 90)."
    )
    longitude: float = Field(
        alias="Longitude", description="Site longitude in degrees (-180 to 180)."
    )
    time_zone: float = Field(
        alias="Time Zone", description="UTC time zone offset in hours (-12 to 14)."
    )
    elevation: float = Field(
        alias="Elevation", description="Site elevation above sea level in meters."
    )


class LocationUpdateInput(ToolInput):
    """Input schema for updating an existing Site:Location object."""

    name: str = Field(alias="Name", description="Name of the location to update.")
    latitude: float = Field(
        alias="Latitude", description="Site latitude in degrees (-90 to 90)."
    )
    longitude: float = Field(
        alias="Longitude", description="Site longitude in degrees (-180 to 180)."
    )
    time_zone: float = Field(
        alias="Time Zone", description="UTC time zone offset in hours (-12 to 14)."
    )
    elevation: float = Field(
        alias="Elevation", description="Site elevation above sea level in meters."
    )


class FloorVertexInput(ToolInput):
    """Bottom-face vertex input model"""

    x: float = Field(..., alias="X", description="X coordinates")
    y: float = Field(..., alias="Y", description="Y coordinates")
    z: float = Field(
        ...,
        alias="Z",
        description="Z coordinates(All points on the same base should be identical)",
    )


class ZoneCreateInput(ToolInput):
    """Input schema for creating a new Zone object."""

    name: str = Field(alias="Name", description="Unique name for the thermal zone.")
    x_origin: float = Field(
        default=0.0, alias="X Origin", description="Zone origin X coordinate in meters."
    )
    y_origin: float = Field(
        default=0.0, alias="Y Origin", description="Zone origin Y coordinate in meters."
    )
    z_origin: float = Field(
        default=0.0, alias="Z Origin", description="Zone origin Z coordinate in meters."
    )
    direction_of_relative_north: float | None = Field(
        default=0.0,
        alias="Direction of Relative North",
        description="Zone north axis angle in degrees relative to building north.",
    )
    multiplier: int = Field(
        default=1,
        alias="Multiplier",
        description="Zone multiplier for identical zones.",
    )
    ceiling_height: float | str = Field(
        default="autocalculate",
        alias="Ceiling Height",
        description="Zone ceiling height in meters, or 'autocalculate'.",
    )
    volume: float | str = Field(
        default="autocalculate",
        alias="Volume",
        description="Zone volume in cubic meters, or 'autocalculate'.",
    )
    floor_area: float | str = Field(
        default="autocalculate",
        alias="Floor Area",
        description="Zone floor area in square meters, or 'autocalculate'.",
    )
    floor_vertices: list[FloorVertexInput] | None = Field(
        default=None,
        alias="Floor Vertices",
        description="List of bottom vertices, arranged in counterclockwise order",
    )


class ZoneUpdateInput(ToolInput):
    """Input schema for updating an existing Zone object."""

    name: str = Field(alias="Name", description="Name of the zone to update.")
    x_origin: float | None = Field(
        default=None,
        alias="X Origin",
        description="Zone origin X coordinate in meters.",
    )
    y_origin: float | None = Field(
        default=None,
        alias="Y Origin",
        description="Zone origin Y coordinate in meters.",
    )
    z_origin: float | None = Field(
        default=None,
        alias="Z Origin",
        description="Zone origin Z coordinate in meters.",
    )
    direction_of_relative_north: float | None = Field(
        default=None,
        alias="Direction of Relative North",
        description="Zone north axis angle in degrees relative to building north.",
    )
    multiplier: int | None = Field(
        default=None,
        alias="Multiplier",
        description="Zone multiplier for identical zones.",
    )
    ceiling_height: float | str | None = Field(
        default=None,
        alias="Ceiling Height",
        description="Zone ceiling height in meters, or 'autocalculate'.",
    )
    volume: float | str | None = Field(
        default=None,
        alias="Volume",
        description="Zone volume in cubic meters, or 'autocalculate'.",
    )
    floor_area: float | str | None = Field(
        default=None,
        alias="Floor Area",
        description="Zone floor area in square meters, or 'autocalculate'.",
    )


def _create_zone_surfaces(
    surface_tool: SurfaceTool,
    zone_name: str,
    vertices: list[dict],
    height: float,
) -> tuple[list[str], list[dict]]:
    """Create wall, floor and ceiling surfaces for a zone.

    Returns:
        (created_surface_names, failed_surface_info)
    """
    created: list[str] = []
    failed: list[dict] = []
    n = len(vertices)
    z_floor = vertices[0]["Z"]
    z_ceiling = z_floor + height

    for i in range(n):
        v1 = vertices[i]
        v2 = vertices[(i + 1) % n]
        wall_verts = [
            {"X": v1["X"], "Y": v1["Y"], "Z": z_floor},
            {"X": v2["X"], "Y": v2["Y"], "Z": z_floor},
            {"X": v2["X"], "Y": v2["Y"], "Z": z_ceiling},
            {"X": v1["X"], "Y": v1["Y"], "Z": z_ceiling},
        ]
        surface_name = f"{zone_name}_Wall_{i + 1}"
        resp = surface_tool.create(
            {
                "Name": surface_name,
                "Surface Type": "Wall",
                "Construction Name": "Default_Construction",
                "Zone Name": zone_name,
                "Outside Boundary Condition": "Outdoors",
                "Sun Exposure": "SunExposed",
                "Wind Exposure": "WindExposed",
                "Vertices": wall_verts,
            }
        )
        if resp.success:
            created.append(surface_name)
        else:
            failed.append({"name": surface_name, "error": resp.message})

    floor_name = f"{zone_name}_Floor"
    floor_resp = surface_tool.create(
        {
            "Name": floor_name,
            "Surface Type": "Floor",
            "Construction Name": "Default_Construction",
            "Zone Name": zone_name,
            "Outside Boundary Condition": "Ground",
            "Sun Exposure": "NoSun",
            "Wind Exposure": "NoWind",
            "Vertices": vertices[::-1],
        }
    )
    if floor_resp.success:
        created.append(floor_name)
    else:
        failed.append({"name": floor_name, "error": floor_resp.message})

    ceiling_name = f"{zone_name}_Ceiling"
    ceiling_verts = [{"X": v["X"], "Y": v["Y"], "Z": z_ceiling} for v in vertices]
    ceiling_resp = surface_tool.create(
        {
            "Name": ceiling_name,
            "Surface Type": "Ceiling",
            "Construction Name": "Default_Construction",
            "Zone Name": zone_name,
            "Outside Boundary Condition": "Adiabatic",
            "Sun Exposure": "NoSun",
            "Wind Exposure": "NoWind",
            "Vertices": ceiling_verts,
        }
    )
    if ceiling_resp.success:
        created.append(ceiling_name)
    else:
        failed.append({"name": ceiling_name, "error": ceiling_resp.message})

    # Rollback all created surfaces if any failed
    if failed:
        for surface_name in created:
            surface_tool.delete(surface_name)
        created.clear()

    return created, failed


def register_core_tools(
    mcp: FastMCP,
    building_tool: BuildingTool,
    location_tool: LocationTool,
    zone_tool: ZoneTool,
    surface_tool: SurfaceTool,
) -> None:
    """Register core EnergyPlus tools (Building, Location, Zone) with the MCP server.

    Args:
        mcp: FastMCP server instance.
        building_tool: BuildingTool instance for building operations.
        location_tool: LocationTool instance for location operations.
        zone_tool: ZoneTool instance for zone operations.
    """

    @mcp.tool
    def create_building(
        name: str,
        north_axis: float,
        terrain: str,
    ) -> dict:
        """Create a new EnergyPlus Building object.

        Args:
            name: Unique name for the building.
            north_axis: Building north axis angle in degrees.
            terrain: Terrain type (e.g. Suburbs, City, Ocean).

        Returns:
            MCP response with the created building data.
        """
        payload = to_payload(
            BuildingCreateInput.model_validate(
                {
                    "name": name,
                    "north_axis": north_axis,
                    "terrain": terrain,
                }
            )
        )
        return building_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_building(name: str) -> dict:
        """Retrieve an existing Building object by name.

        Args:
            name: Name of the building to retrieve.

        Returns:
            MCP response with the building data.
        """
        return building_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_building(name: str, north_axis: float, terrain: str) -> dict:
        """Update an existing Building object.

        Args:
            name: Name of the building to update.
            north_axis: New north axis angle in degrees.
            terrain: New terrain type.

        Returns:
            MCP response with the updated building data.
        """
        payload = to_payload(
            BuildingUpdateInput.model_validate(
                {
                    "name": name,
                    "north_axis": north_axis,
                    "terrain": terrain,
                }
            )
        )
        return building_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_building(name: str) -> dict:
        """Delete a Building object by name.

        Args:
            name: Name of the building to delete.

        Returns:
            MCP response with deletion result.
        """
        return building_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_buildings() -> dict:
        """List all Building objects in the configuration.

        Returns:
            MCP response with a list of all buildings.
        """
        return building_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_location(
        name: str,
        latitude: float,
        longitude: float,
        time_zone: float,
        elevation: float,
    ) -> dict:
        """Create a new Site:Location object.

        Args:
            name: Unique name for the site location.
            latitude: Site latitude in degrees.
            longitude: Site longitude in degrees.
            time_zone: UTC time zone offset in hours.
            elevation: Site elevation in meters.

        Returns:
            MCP response with the created location data.
        """
        payload = to_payload(
            LocationCreateInput.model_validate(
                {
                    "name": name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "time_zone": time_zone,
                    "elevation": elevation,
                }
            )
        )
        return location_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_location(name: str) -> dict:
        """Retrieve an existing Site:Location object by name.

        Args:
            name: Name of the location to retrieve.

        Returns:
            MCP response with the location data.
        """
        return location_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_location(
        name: str,
        latitude: float,
        longitude: float,
        time_zone: float,
        elevation: float,
    ) -> dict:
        """Update an existing Site:Location object.

        Args:
            name: Name of the location to update.
            latitude: New latitude in degrees.
            longitude: New longitude in degrees.
            time_zone: New UTC time zone offset.
            elevation: New elevation in meters.

        Returns:
            MCP response with the updated location data.
        """
        payload = to_payload(
            LocationUpdateInput.model_validate(
                {
                    "name": name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "time_zone": time_zone,
                    "elevation": elevation,
                }
            )
        )
        return location_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_location(name: str) -> dict:
        """Delete a Site:Location object by name.

        Args:
            name: Name of the location to delete.

        Returns:
            MCP response with deletion result.
        """
        return location_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_locations() -> dict:
        """List all Site:Location objects in the configuration.

        Returns:
            MCP response with a list of all locations.
        """
        return location_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_zone(
        name: str,
        floor_vertices: list[dict],  # [{"X": 0, "Y": 0, "Z": 0}, ...]
        x_origin: float = 0.0,
        y_origin: float = 0.0,
        z_origin: float = 0.0,
        direction_of_relative_north: float | None = 0.0,
        multiplier: int = 1,
        ceiling_height: float | str = "autocalculate",
        volume: float | str = "autocalculate",
        floor_area: float | str = "autocalculate",
    ) -> dict:
        """Create a new thermal Zone object.

        Args:
            name: Unique name for the zone.
            x_origin: Zone origin X coordinate in meters.
            y_origin: Zone origin Y coordinate in meters.
            z_origin: Zone origin Z coordinate in meters.
            direction_of_relative_north: North axis angle in degrees.
            multiplier: Zone multiplier for identical zones.
            ceiling_height: Ceiling height in meters or 'autocalculate'.
            volume: Volume in cubic meters or 'autocalculate'.
            floor_area: Floor area in square meters or 'autocalculate'.

        Returns:
            MCP response with the created zone data.
        """
        # Validate inputs before creating zone
        try:
            vertices = convert_vertices_to_mcp_format(floor_vertices)
            is_valid, error = validate_floor_vertices(vertices)
        except (TypeError, ValueError) as e:
            return ToolResponse(
                success=False,
                message=f"Invalid floor_vertices: {e}",
            ).to_mcp_response()

        if not is_valid and error is not None:
            return ToolResponse(
                success=False,
                message=f"Vertex validation failed: {error.message}",
                data={"validation_error": error.model_dump()},
            ).to_mcp_response()

        if ceiling_height == "autocalculate":
            return ToolResponse(
                success=False,
                message="When using the floor_vertices parameter, a specific ceiling_height value must be specified",
            ).to_mcp_response()
        try:
            height = float(ceiling_height)
        except (TypeError, ValueError):
            return ToolResponse(
                success=False,
                message="ceiling_height must be a numeric value when floor_vertices is provided",
            ).to_mcp_response()
        if height <= 0:
            return ToolResponse(
                success=False,
                message="ceiling_height must be greater than 0 when floor_vertices is provided",
            ).to_mcp_response()

        # All validations passed, now create zone
        payload = to_payload(
            ZoneCreateInput.model_validate(
                {
                    "name": name,
                    "x_origin": x_origin,
                    "y_origin": y_origin,
                    "z_origin": z_origin,
                    "direction_of_relative_north": direction_of_relative_north,
                    "multiplier": multiplier,
                    "ceiling_height": ceiling_height,
                    "volume": volume,
                    "floor_area": floor_area,
                }
            )
        )
        zone_response = zone_tool.create(payload)

        if not zone_response.success:
            return zone_response.to_mcp_response()

        created_surfaces, failed_surfaces = _create_zone_surfaces(
            surface_tool,
            name,
            vertices,
            height,
        )

        if failed_surfaces:
            zone_del_resp = zone_tool.delete(name)
            rollback_msg = (
                f"Zone '{name}' creation rolled back due to surface failures."
            )
            if not zone_del_resp.success:
                rollback_msg += (
                    f" Warning: Zone deletion failed: {zone_del_resp.message}"
                )
            return ToolResponse(
                success=False,
                message=rollback_msg,
                data={"surfaces_failed": failed_surfaces},
            ).to_mcp_response()

        return ToolResponse(
            success=True,
            message=f"Zone '{name}' created successfully with {len(created_surfaces)} surfaces.",
            data={
                "name": name,
                "surfaces_created": created_surfaces,
            },
        ).to_mcp_response()

    @mcp.tool
    def get_zone(name: str) -> dict:
        """Retrieve an existing Zone object by name.

        Args:
            name: Name of the zone to retrieve.

        Returns:
            MCP response with the zone data.
        """
        return zone_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_zone(
        name: str,
        x_origin: float | None = None,
        y_origin: float | None = None,
        z_origin: float | None = None,
        direction_of_relative_north: float | None = None,
        multiplier: int | None = None,
        ceiling_height: float | str | None = None,
        volume: float | str | None = None,
        floor_area: float | str | None = None,
    ) -> dict:
        """Update an existing Zone object.

        Args:
            name: Name of the zone to update.
            x_origin: New X origin in meters.
            y_origin: New Y origin in meters.
            z_origin: New Z origin in meters.
            direction_of_relative_north: New north axis angle in degrees.
            multiplier: New zone multiplier.
            ceiling_height: New ceiling height or 'autocalculate'.
            volume: New volume or 'autocalculate'.
            floor_area: New floor area or 'autocalculate'.

        Returns:
            MCP response with the updated zone data.
        """
        payload = to_payload(
            ZoneUpdateInput.model_validate(
                {
                    "name": name,
                    "x_origin": x_origin,
                    "y_origin": y_origin,
                    "z_origin": z_origin,
                    "direction_of_relative_north": direction_of_relative_north,
                    "multiplier": multiplier,
                    "ceiling_height": ceiling_height,
                    "volume": volume,
                    "floor_area": floor_area,
                }
            )
        )
        return zone_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_zone(name: str) -> dict:
        """Delete a Zone object by name.

        Args:
            name: Name of the zone to delete.

        Returns:
            MCP response with deletion result.
        """
        return zone_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_zones() -> dict:
        """List all Zone objects in the configuration.

        Returns:
            MCP response with a list of all zones.
        """
        return zone_tool.list_all().to_mcp_response()
