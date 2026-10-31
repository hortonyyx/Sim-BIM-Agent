from fastmcp import FastMCP
from pydantic import Field, ValidationError

from src.mcp.api.common import ToolInput, to_payload
from src.mcp.interface import ToolResponse
from src.mcp.tools import (
    ConstructionTool,
    FenestrationTool,
    MaterialTool,
    SurfaceTool,
)


class StandardMaterialCreateInput(ToolInput):
    """Input schema for creating a new standard (mass) material."""

    name: str = Field(alias="Name", description="Unique name for the material.")
    material_type: str = Field(
        default="Standard",
        alias="Type",
        description="Material type discriminator, fixed to 'Standard'.",
    )
    roughness: str = Field(
        alias="Roughness",
        description="Surface roughness (e.g. 'Smooth', 'MediumSmooth', 'Rough').",
    )
    thickness: float = Field(
        alias="Thickness", description="Material thickness in meters."
    )
    conductivity: float = Field(
        alias="Conductivity",
        description="Thermal conductivity in W/(m·K).",
    )
    density: float = Field(alias="Density", description="Material density in kg/m³.")
    specific_heat: float = Field(
        alias="Specific_Heat", description="Specific heat capacity in J/(kg·K)."
    )


class StandardMaterialUpdateInput(ToolInput):
    """Input schema for updating an existing standard material."""

    name: str = Field(alias="Name", description="Name of the material to update.")
    material_type: str = Field(
        default="Standard",
        alias="Type",
        description="Material type discriminator, fixed to 'Standard'.",
    )
    roughness: str | None = Field(
        default=None, alias="Roughness", description="Surface roughness category."
    )
    thickness: float | None = Field(
        default=None, alias="Thickness", description="Material thickness in meters."
    )
    conductivity: float | None = Field(
        default=None,
        alias="Conductivity",
        description="Thermal conductivity in W/(m·K).",
    )
    density: float | None = Field(
        default=None, alias="Density", description="Material density in kg/m³."
    )
    specific_heat: float | None = Field(
        default=None,
        alias="Specific_Heat",
        description="Specific heat capacity in J/(kg·K).",
    )


class NoMassMaterialCreateInput(ToolInput):
    """Input schema for creating a new no-mass (resistance-only) material."""

    name: str = Field(alias="Name", description="Unique name for the material.")
    material_type: str = Field(
        default="NoMass",
        alias="Type",
        description="Material type discriminator, fixed to 'NoMass'.",
    )
    roughness: str = Field(alias="Roughness", description="Surface roughness category.")
    thermal_resistance: float = Field(
        alias="Thermal_Resistance",
        description="Thermal resistance (R-value) in m²·K/W.",
    )


class NoMassMaterialUpdateInput(ToolInput):
    """Input schema for updating an existing no-mass material."""

    name: str = Field(alias="Name", description="Name of the material to update.")
    material_type: str = Field(
        default="NoMass",
        alias="Type",
        description="Material type discriminator, fixed to 'NoMass'.",
    )
    roughness: str | None = Field(
        default=None, alias="Roughness", description="Surface roughness category."
    )
    thermal_resistance: float | None = Field(
        default=None,
        alias="Thermal_Resistance",
        description="Thermal resistance (R-value) in m²·K/W.",
    )


class AirGapMaterialCreateInput(ToolInput):
    """Input schema for creating a new air gap material."""

    name: str = Field(alias="Name", description="Unique name for the air gap material.")
    material_type: str = Field(
        default="AirGap",
        alias="Type",
        description="Material type discriminator, fixed to 'AirGap'.",
    )
    thermal_resistance: float = Field(
        alias="Thermal_Resistance",
        description="Thermal resistance of the air gap in m²·K/W.",
    )


class AirGapMaterialUpdateInput(ToolInput):
    """Input schema for updating an existing air gap material."""

    name: str = Field(alias="Name", description="Name of the material to update.")
    material_type: str = Field(
        default="AirGap",
        alias="Type",
        description="Material type discriminator, fixed to 'AirGap'.",
    )
    thermal_resistance: float | None = Field(
        default=None,
        alias="Thermal_Resistance",
        description="Thermal resistance of the air gap in m²·K/W.",
    )


class GlazingMaterialCreateInput(ToolInput):
    """Input schema for creating a new simple glazing material."""

    name: str = Field(alias="Name", description="Unique name for the glazing material.")
    material_type: str = Field(
        default="Glazing",
        alias="Type",
        description="Material type discriminator, fixed to 'Glazing'.",
    )
    u_factor: float = Field(
        alias="U-Factor",
        description="Overall U-factor of the glazing system in W/(m²·K).",
    )
    solar_heat_gain_coefficient: float = Field(
        alias="Solar_Heat_Gain_Coefficient",
        description="Solar heat gain coefficient (SHGC), dimensionless (0.0-1.0).",
    )
    visible_transmittance: float = Field(
        alias="Visible_Transmittance",
        description="Visible light transmittance, dimensionless (0.0-1.0).",
    )


class GlazingMaterialUpdateInput(ToolInput):
    """Input schema for updating an existing glazing material."""

    name: str = Field(alias="Name", description="Name of the material to update.")
    material_type: str = Field(
        default="Glazing",
        alias="Type",
        description="Material type discriminator, fixed to 'Glazing'.",
    )
    u_factor: float | None = Field(
        default=None,
        alias="U-Factor",
        description="Overall U-factor in W/(m²·K).",
    )
    solar_heat_gain_coefficient: float | None = Field(
        default=None,
        alias="Solar_Heat_Gain_Coefficient",
        description="Solar heat gain coefficient (0.0-1.0).",
    )
    visible_transmittance: float | None = Field(
        default=None,
        alias="Visible_Transmittance",
        description="Visible light transmittance (0.0-1.0).",
    )


class ConstructionCreateInput(ToolInput):
    """Input schema for creating a new Construction assembly."""

    name: str = Field(alias="Name", description="Unique name for the construction.")
    layers: list[str] = Field(
        alias="Layers",
        description="Ordered list of material names from outside to inside.",
    )


class ConstructionUpdateInput(ToolInput):
    """Input schema for updating an existing Construction assembly."""

    name: str = Field(alias="Name", description="Name of the construction to update.")
    layers: list[str] | None = Field(
        default=None,
        alias="Layers",
        description="Ordered list of material names from outside to inside.",
    )


class SurfaceCreateInput(ToolInput):
    """Input schema for creating a new BuildingSurface:Detailed object."""

    name: str = Field(alias="Name", description="Unique name for the surface.")
    surface_type: str = Field(
        alias="Surface Type",
        description="Surface type: 'Wall', 'Floor', 'Roof', or 'Ceiling'.",
    )
    construction_name: str = Field(
        alias="Construction Name",
        description="Name of the construction assembly for this surface.",
    )
    zone_name: str = Field(
        alias="Zone Name",
        description="Name of the zone this surface belongs to.",
    )
    outside_boundary_condition: str = Field(
        alias="Outside Boundary Condition",
        description="Boundary condition: 'Outdoors', 'Ground', 'Surface', 'Adiabatic', etc.",
    )
    sun_exposure: str = Field(
        alias="Sun Exposure",
        description="Sun exposure: 'SunExposed' or 'NoSun'.",
    )
    wind_exposure: str = Field(
        alias="Wind Exposure",
        description="Wind exposure: 'WindExposed' or 'NoWind'.",
    )
    vertices: list[dict] = Field(
        alias="Vertices",
        description="List of vertex coordinate dicts with 'x', 'y', 'z' keys in meters.",
    )
    outside_boundary_condition_object: str | None = Field(
        default=None,
        alias="Outside Boundary Condition Object",
        description="Name of the adjacent surface when boundary condition is 'Surface'.",
    )
    space_name: str | None = Field(
        default=None,
        alias="Space Name",
        description="Optional space name within the zone.",
    )
    view_factor_to_ground: float | str = Field(
        default="autocalculate",
        alias="View Factor to Ground",
        description="View factor to ground (0.0-1.0) or 'autocalculate'.",
    )
    number_of_vertices: int | str = Field(
        default="autocalculate",
        alias="Number of Vertices",
        description="Number of vertices or 'autocalculate'.",
    )


class SurfaceUpdateInput(ToolInput):
    """Input schema for updating an existing BuildingSurface:Detailed object."""

    name: str = Field(alias="Name", description="Name of the surface to update.")
    surface_type: str | None = Field(
        default=None,
        alias="Surface Type",
        description="Surface type: 'Wall', 'Floor', 'Roof', or 'Ceiling'.",
    )
    construction_name: str | None = Field(
        default=None,
        alias="Construction Name",
        description="Name of the construction assembly.",
    )
    zone_name: str | None = Field(
        default=None,
        alias="Zone Name",
        description="Name of the zone this surface belongs to.",
    )
    space_name: str | None = Field(
        default=None,
        alias="Space Name",
        description="Optional space name within the zone.",
    )
    outside_boundary_condition: str | None = Field(
        default=None,
        alias="Outside Boundary Condition",
        description="Boundary condition type.",
    )
    outside_boundary_condition_object: str | None = Field(
        default=None,
        alias="Outside Boundary Condition Object",
        description="Adjacent surface name for 'Surface' boundary condition.",
    )
    sun_exposure: str | None = Field(
        default=None,
        alias="Sun Exposure",
        description="Sun exposure setting.",
    )
    wind_exposure: str | None = Field(
        default=None,
        alias="Wind Exposure",
        description="Wind exposure setting.",
    )
    view_factor_to_ground: float | str | None = Field(
        default=None,
        alias="View Factor to Ground",
        description="View factor to ground or 'autocalculate'.",
    )
    vertices: list[dict] | None = Field(
        default=None,
        alias="Vertices",
        description="List of vertex coordinate dicts.",
    )


class FenestrationCreateInput(ToolInput):
    """Input schema for creating a new FenestrationSurface:Detailed object."""

    name: str = Field(
        alias="Name", description="Unique name for the fenestration surface."
    )
    surface_type: str = Field(
        alias="Surface Type",
        description="Fenestration type: 'Window', 'Door', 'GlassDoor', or 'TubularDaylightDome'.",
    )
    construction_name: str = Field(
        alias="Construction Name",
        description="Name of the glazing/door construction assembly.",
    )
    building_surface_name: str = Field(
        alias="Building Surface Name",
        description="Name of the host building surface (wall or roof).",
    )
    vertices: list[dict] = Field(
        alias="Vertices",
        description="List of vertex coordinate dicts with 'x', 'y', 'z' keys in meters.",
    )
    outside_boundary_condition_object: str | None = Field(
        default=None,
        alias="Outside Boundary Condition Object",
        description="Adjacent fenestration name for interior windows.",
    )
    view_factor_to_ground: float | str = Field(
        default="autocalculate",
        alias="View Factor to Ground",
        description="View factor to ground (0.0-1.0) or 'autocalculate'.",
    )
    frame_and_divider_name: str | None = Field(
        default=None,
        alias="Frame and Divider Name",
        description="Optional window frame and divider object name.",
    )
    multiplier: int = Field(
        default=1,
        alias="Multiplier",
        description="Number of identical fenestration surfaces.",
    )
    number_of_vertices: int | str = Field(
        default="autocalculate",
        alias="Number of Vertices",
        description="Number of vertices or 'autocalculate'.",
    )


class FenestrationUpdateInput(ToolInput):
    """Input schema for updating an existing FenestrationSurface:Detailed object."""

    name: str = Field(
        alias="Name", description="Name of the fenestration surface to update."
    )
    surface_type: str | None = Field(
        default=None,
        alias="Surface Type",
        description="Fenestration type.",
    )
    construction_name: str | None = Field(
        default=None,
        alias="Construction Name",
        description="Name of the construction assembly.",
    )
    building_surface_name: str | None = Field(
        default=None,
        alias="Building Surface Name",
        description="Name of the host building surface.",
    )
    outside_boundary_condition_object: str | None = Field(
        default=None,
        alias="Outside Boundary Condition Object",
        description="Adjacent fenestration name.",
    )
    view_factor_to_ground: float | str | None = Field(
        default=None,
        alias="View Factor to Ground",
        description="View factor to ground or 'autocalculate'.",
    )
    frame_and_divider_name: str | None = Field(
        default=None,
        alias="Frame and Divider Name",
        description="Window frame and divider object name.",
    )
    multiplier: int | None = Field(
        default=None,
        alias="Multiplier",
        description="Number of identical fenestration surfaces.",
    )
    number_of_vertices: int | str | None = Field(
        default=None,
        alias="Number of Vertices",
        description="Number of vertices or 'autocalculate'.",
    )
    vertices: list[dict] | None = Field(
        default=None,
        alias="Vertices",
        description="List of vertex coordinate dicts.",
    )


def register_envelope_tools(
    mcp: FastMCP,
    material_tool: MaterialTool,
    construction_tool: ConstructionTool,
    surface_tool: SurfaceTool,
    fenestration_tool: FenestrationTool,
) -> None:
    """Register building envelope tools (Material, Construction, Surface, Fenestration) with the MCP server.

    Args:
        mcp: FastMCP server instance.
        material_tool: MaterialTool instance for material operations.
        construction_tool: ConstructionTool instance for construction operations.
        surface_tool: SurfaceTool instance for surface operations.
        fenestration_tool: FenestrationTool instance for fenestration operations.
    """

    @mcp.tool
    def create_standard_material(
        name: str,
        roughness: str,
        thickness: float,
        conductivity: float,
        density: float,
        specific_heat: float,
    ) -> dict:
        """Create a new standard (mass) material.

        Args:
            name: Unique name for the material.
            roughness: Surface roughness category.
            thickness: Thickness in meters.
            conductivity: Thermal conductivity in W/(m·K).
            density: Density in kg/m³.
            specific_heat: Specific heat in J/(kg·K).

        Returns:
            MCP response with the created material data.
        """
        payload = to_payload(
            StandardMaterialCreateInput.model_validate(
                {
                    "name": name,
                    "roughness": roughness,
                    "thickness": thickness,
                    "conductivity": conductivity,
                    "density": density,
                    "specific_heat": specific_heat,
                }
            )
        )
        return material_tool.create(payload).to_mcp_response()

    @mcp.tool
    def create_no_mass_material(
        name: str,
        roughness: str,
        thermal_resistance: float,
    ) -> dict:
        """Create a new no-mass (resistance-only) material.

        Args:
            name: Unique name for the material.
            roughness: Surface roughness category.
            thermal_resistance: Thermal resistance (R-value) in m²·K/W.

        Returns:
            MCP response with the created material data.
        """
        payload = to_payload(
            NoMassMaterialCreateInput.model_validate(
                {
                    "name": name,
                    "roughness": roughness,
                    "thermal_resistance": thermal_resistance,
                }
            )
        )
        return material_tool.create(payload).to_mcp_response()

    @mcp.tool
    def create_air_gap_material(
        name: str,
        thermal_resistance: float,
    ) -> dict:
        """Create a new air gap material.

        Args:
            name: Unique name for the material.
            thermal_resistance: Air gap thermal resistance in m²·K/W.

        Returns:
            MCP response with the created material data.
        """
        payload = to_payload(
            AirGapMaterialCreateInput.model_validate(
                {
                    "name": name,
                    "thermal_resistance": thermal_resistance,
                }
            )
        )
        return material_tool.create(payload).to_mcp_response()

    @mcp.tool
    def create_glazing_material(
        name: str,
        u_factor: float,
        solar_heat_gain_coefficient: float,
        visible_transmittance: float,
    ) -> dict:
        """Create a new simple glazing material.

        Args:
            name: Unique name for the glazing material.
            u_factor: Overall U-factor in W/(m²·K).
            solar_heat_gain_coefficient: SHGC value (0.0-1.0).
            visible_transmittance: Visible transmittance (0.0-1.0).

        Returns:
            MCP response with the created material data.
        """
        payload = to_payload(
            GlazingMaterialCreateInput.model_validate(
                {
                    "name": name,
                    "u_factor": u_factor,
                    "solar_heat_gain_coefficient": solar_heat_gain_coefficient,
                    "visible_transmittance": visible_transmittance,
                }
            )
        )
        return material_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_material(name: str) -> dict:
        """Retrieve an existing material by name.

        Args:
            name: Name of the material to retrieve.

        Returns:
            MCP response with the material data.
        """
        return material_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_standard_material(
        name: str,
        roughness: str | None = None,
        thickness: float | None = None,
        conductivity: float | None = None,
        density: float | None = None,
        specific_heat: float | None = None,
    ) -> dict:
        """Update an existing standard material.

        Args:
            name: Name of the material to update.
            roughness: New surface roughness.
            thickness: New thickness in meters.
            conductivity: New conductivity in W/(m·K).
            density: New density in kg/m³.
            specific_heat: New specific heat in J/(kg·K).

        Returns:
            MCP response with the updated material data.
        """
        payload = to_payload(
            StandardMaterialUpdateInput.model_validate(
                {
                    "name": name,
                    "roughness": roughness,
                    "thickness": thickness,
                    "conductivity": conductivity,
                    "density": density,
                    "specific_heat": specific_heat,
                }
            )
        )
        return material_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def update_no_mass_material(
        name: str,
        roughness: str | None = None,
        thermal_resistance: float | None = None,
    ) -> dict:
        """Update an existing no-mass material.

        Args:
            name: Name of the material to update.
            roughness: New surface roughness.
            thermal_resistance: New R-value in m²·K/W.

        Returns:
            MCP response with the updated material data.
        """
        payload = to_payload(
            NoMassMaterialUpdateInput.model_validate(
                {
                    "name": name,
                    "roughness": roughness,
                    "thermal_resistance": thermal_resistance,
                }
            )
        )
        return material_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def update_air_gap_material(
        name: str,
        thermal_resistance: float | None = None,
    ) -> dict:
        """Update an existing air gap material.

        Args:
            name: Name of the material to update.
            thermal_resistance: New air gap R-value in m²·K/W.

        Returns:
            MCP response with the updated material data.
        """
        payload = to_payload(
            AirGapMaterialUpdateInput.model_validate(
                {
                    "name": name,
                    "thermal_resistance": thermal_resistance,
                }
            )
        )
        return material_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def update_glazing_material(
        name: str,
        u_factor: float | None = None,
        solar_heat_gain_coefficient: float | None = None,
        visible_transmittance: float | None = None,
    ) -> dict:
        """Update an existing glazing material.

        Args:
            name: Name of the material to update.
            u_factor: New U-factor in W/(m²·K).
            solar_heat_gain_coefficient: New SHGC value.
            visible_transmittance: New visible transmittance.

        Returns:
            MCP response with the updated material data.
        """
        payload = to_payload(
            GlazingMaterialUpdateInput.model_validate(
                {
                    "name": name,
                    "u_factor": u_factor,
                    "solar_heat_gain_coefficient": solar_heat_gain_coefficient,
                    "visible_transmittance": visible_transmittance,
                }
            )
        )
        return material_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_material(name: str) -> dict:
        """Delete a material by name.

        Args:
            name: Name of the material to delete.

        Returns:
            MCP response with deletion result.
        """
        return material_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_materials() -> dict:
        """List all materials in the configuration.

        Returns:
            MCP response with a list of all materials.
        """
        return material_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_construction(
        name: str,
        layers: list[str],
    ) -> dict:
        """Create a new construction assembly.

        Args:
            name: Unique name for the construction.
            layers: Ordered list of material names from outside to inside.

        Returns:
            MCP response with the created construction data.
        """
        payload = to_payload(
            ConstructionCreateInput.model_validate(
                {
                    "name": name,
                    "layers": layers,
                }
            )
        )
        return construction_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_construction(name: str) -> dict:
        """Retrieve an existing construction by name.

        Args:
            name: Name of the construction to retrieve.

        Returns:
            MCP response with the construction data.
        """
        return construction_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_construction(
        name: str,
        layers: list[str] | None = None,
    ) -> dict:
        """Update an existing construction assembly.

        Args:
            name: Name of the construction to update.
            layers: New ordered list of material names.

        Returns:
            MCP response with the updated construction data.
        """
        payload = to_payload(
            ConstructionUpdateInput.model_validate(
                {
                    "name": name,
                    "layers": layers,
                }
            )
        )
        return construction_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_construction(name: str) -> dict:
        """Delete a construction by name.

        Args:
            name: Name of the construction to delete.

        Returns:
            MCP response with deletion result.
        """
        return construction_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_constructions() -> dict:
        """List all constructions in the configuration.

        Returns:
            MCP response with a list of all constructions.
        """
        return construction_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_surface(
        name: str,
        surface_type: str,
        construction_name: str,
        zone_name: str,
        outside_boundary_condition: str,
        sun_exposure: str,
        wind_exposure: str,
        vertices: list[dict],
        outside_boundary_condition_object: str | None = None,
        space_name: str | None = None,
        view_factor_to_ground: float | str = "autocalculate",
        number_of_vertices: int | str = "autocalculate",
    ) -> dict:
        """Create a new building surface.

        Args:
            name: Unique name for the surface.
            surface_type: Type: 'Wall', 'Floor', 'Roof', or 'Ceiling'.
            construction_name: Construction assembly name.
            zone_name: Zone this surface belongs to.
            outside_boundary_condition: Boundary condition type.
            sun_exposure: 'SunExposed' or 'NoSun'.
            wind_exposure: 'WindExposed' or 'NoWind'.
            vertices: Vertex coordinates as list of dicts.
            outside_boundary_condition_object: Adjacent surface name.
            space_name: Optional space name.
            view_factor_to_ground: View factor or 'autocalculate'.
            number_of_vertices: Vertex count or 'autocalculate'.

        Returns:
            MCP response with the created surface data.
        """
        payload = to_payload(
            SurfaceCreateInput.model_validate(
                {
                    "name": name,
                    "surface_type": surface_type,
                    "construction_name": construction_name,
                    "zone_name": zone_name,
                    "outside_boundary_condition": outside_boundary_condition,
                    "sun_exposure": sun_exposure,
                    "wind_exposure": wind_exposure,
                    "vertices": vertices,
                    "outside_boundary_condition_object": outside_boundary_condition_object,
                    "space_name": space_name,
                    "view_factor_to_ground": view_factor_to_ground,
                    "number_of_vertices": number_of_vertices,
                }
            )
        )
        return surface_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_surface(name: str) -> dict:
        """Retrieve an existing surface by name.

        Args:
            name: Name of the surface to retrieve.

        Returns:
            MCP response with the surface data.
        """
        return surface_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_surface(
        name: str,
        surface_type: str | None = None,
        construction_name: str | None = None,
        zone_name: str | None = None,
        space_name: str | None = None,
        outside_boundary_condition: str | None = None,
        outside_boundary_condition_object: str | None = None,
        sun_exposure: str | None = None,
        wind_exposure: str | None = None,
        view_factor_to_ground: float | str | None = None,
        vertices: list[dict] | None = None,
    ) -> dict:
        """Update an existing building surface.

        Args:
            name: Name of the surface to update.
            surface_type: New surface type.
            construction_name: New construction name.
            zone_name: New zone name.
            space_name: New space name.
            outside_boundary_condition: New boundary condition.
            outside_boundary_condition_object: New adjacent surface name.
            sun_exposure: New sun exposure setting.
            wind_exposure: New wind exposure setting.
            view_factor_to_ground: New view factor or 'autocalculate'.
            vertices: New vertex coordinates.

        Returns:
            MCP response with the updated surface data.
        """
        payload = to_payload(
            SurfaceUpdateInput.model_validate(
                {
                    "name": name,
                    "surface_type": surface_type,
                    "construction_name": construction_name,
                    "zone_name": zone_name,
                    "space_name": space_name,
                    "outside_boundary_condition": outside_boundary_condition,
                    "outside_boundary_condition_object": outside_boundary_condition_object,
                    "sun_exposure": sun_exposure,
                    "wind_exposure": wind_exposure,
                    "view_factor_to_ground": view_factor_to_ground,
                    "vertices": vertices,
                }
            )
        )
        return surface_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_surface(name: str) -> dict:
        """Delete a surface by name.

        Args:
            name: Name of the surface to delete.

        Returns:
            MCP response with deletion result.
        """
        return surface_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_surfaces() -> dict:
        """List all surfaces in the configuration.

        Returns:
            MCP response with a list of all surfaces.
        """
        return surface_tool.list_all().to_mcp_response()

    @mcp.tool
    def create_fenestration_surface(
        name: str,
        surface_type: str,
        construction_name: str,
        building_surface_name: str,
        vertices: list[dict],
        outside_boundary_condition_object: str | None = None,
        view_factor_to_ground: float | str = "autocalculate",
        frame_and_divider_name: str | None = None,
        multiplier: int = 1,
        number_of_vertices: int | str = "autocalculate",
    ) -> dict:
        """Create a new fenestration surface (window, door, etc.).

        Args:
            name: Unique name for the fenestration.
            surface_type: Type: 'Window', 'Door', 'GlassDoor', etc.
            construction_name: Glazing/door construction name.
            building_surface_name: Host building surface name.
            vertices: Vertex coordinates as list of dicts.
            outside_boundary_condition_object: Adjacent fenestration name.
            view_factor_to_ground: View factor or 'autocalculate'.
            frame_and_divider_name: Frame and divider object name.
            multiplier: Number of identical fenestrations.
            number_of_vertices: Vertex count or 'autocalculate'.

        Returns:
            MCP response with the created fenestration data.
        """
        payload = to_payload(
            FenestrationCreateInput.model_validate(
                {
                    "name": name,
                    "surface_type": surface_type,
                    "construction_name": construction_name,
                    "building_surface_name": building_surface_name,
                    "vertices": vertices,
                    "outside_boundary_condition_object": outside_boundary_condition_object,
                    "view_factor_to_ground": view_factor_to_ground,
                    "frame_and_divider_name": frame_and_divider_name,
                    "multiplier": multiplier,
                    "number_of_vertices": number_of_vertices,
                }
            )
        )
        return fenestration_tool.create(payload).to_mcp_response()

    @mcp.tool
    def get_fenestration_surface(name: str) -> dict:
        """Retrieve an existing fenestration surface by name.

        Args:
            name: Name of the fenestration to retrieve.

        Returns:
            MCP response with the fenestration data.
        """
        return fenestration_tool.read(name).to_mcp_response()

    @mcp.tool
    def update_fenestration_surface(
        name: str,
        surface_type: str | None = None,
        construction_name: str | None = None,
        building_surface_name: str | None = None,
        outside_boundary_condition_object: str | None = None,
        view_factor_to_ground: float | str | None = None,
        frame_and_divider_name: str | None = None,
        multiplier: int | None = None,
        number_of_vertices: int | str | None = None,
        vertices: list[dict] | None = None,
    ) -> dict:
        """Update an existing fenestration surface.

        Args:
            name: Name of the fenestration to update.
            surface_type: New fenestration type.
            construction_name: New construction name.
            building_surface_name: New host surface name.
            outside_boundary_condition_object: New adjacent fenestration.
            view_factor_to_ground: New view factor or 'autocalculate'.
            frame_and_divider_name: New frame and divider name.
            multiplier: New multiplier.
            number_of_vertices: New vertex count or 'autocalculate'.
            vertices: New vertex coordinates.

        Returns:
            MCP response with the updated fenestration data.
        """
        payload = to_payload(
            FenestrationUpdateInput.model_validate(
                {
                    "name": name,
                    "surface_type": surface_type,
                    "construction_name": construction_name,
                    "building_surface_name": building_surface_name,
                    "outside_boundary_condition_object": outside_boundary_condition_object,
                    "view_factor_to_ground": view_factor_to_ground,
                    "frame_and_divider_name": frame_and_divider_name,
                    "multiplier": multiplier,
                    "number_of_vertices": number_of_vertices,
                    "vertices": vertices,
                }
            )
        )
        return fenestration_tool.update(name, payload).to_mcp_response()

    @mcp.tool
    def delete_fenestration_surface(name: str) -> dict:
        """Delete a fenestration surface by name.

        Args:
            name: Name of the fenestration to delete.

        Returns:
            MCP response with deletion result.
        """
        return fenestration_tool.delete(name).to_mcp_response()

    @mcp.tool
    def list_fenestration_surfaces() -> dict:
        """List all fenestration surfaces in the configuration.

        Returns:
            MCP response with a list of all fenestrations.
        """
        return fenestration_tool.list_all().to_mcp_response()

    @mcp.tool
    def update_surfaces_batch(updates: list[dict]) -> dict:
        """Apply multiple surface updates in a single call.

        Use this instead of looping `update_surface` when you have many
        boundary-condition / construction touch-ups to perform (e.g. after
        creating zones, configuring all auto-generated walls, floors, ceilings).
        Each item is validated and applied independently; failures do not stop
        the batch.

        Args:
            updates: List of dicts. Each dict must contain `name` (the surface
                to update) plus any fields accepted by `update_surface`
                (`outside_boundary_condition`, `sun_exposure`, `wind_exposure`,
                `construction_name`, `surface_type`, `zone_name`, `space_name`,
                `outside_boundary_condition_object`, `view_factor_to_ground`,
                `vertices`). Fields omitted in an item are left unchanged.

        Returns:
            MCP response with `{"count": N, "succeeded": [name, ...],
            "failed": [{"name": ..., "error": ...}, ...]}`. `success` is
            True only when `failed` is empty.
        """
        succeeded: list[str] = []
        failed: list[dict] = []

        for i, raw in enumerate(updates):
            if not isinstance(raw, dict):
                failed.append({"name": f"<item_{i}>", "error": "item must be a dict"})
                continue
            name = raw.get("name") or raw.get("Name")
            if not name:
                failed.append({"name": f"<item_{i}>", "error": "missing 'name' field"})
                continue
            try:
                payload = to_payload(SurfaceUpdateInput.model_validate(raw))
            except ValidationError as e:
                first = e.errors()[0]
                failed.append(
                    {
                        "name": name,
                        "error": f"validation: {'.'.join(str(x) for x in first['loc'])}: {first['msg']}",
                    }
                )
                continue
            r = surface_tool.update(name, payload)
            if r.success:
                succeeded.append(name)
            else:
                failed.append({"name": name, "error": r.message})

        return ToolResponse(
            success=not failed,
            message=f"Batch update_surface: {len(succeeded)} succeeded, {len(failed)} failed.",
            data={
                "count": len(updates),
                "succeeded": succeeded,
                "failed": failed,
            },
        ).to_mcp_response()

    @mcp.tool
    def create_fenestration_surfaces_batch(items: list[dict]) -> dict:
        """Create multiple fenestration surfaces (windows/doors) in one call.

        Use this instead of looping `create_fenestration_surface` when
        emitting all rows of a Fenestration Table at once. Each item is
        validated and applied independently; failures do not stop the batch.

        Args:
            items: List of dicts. Each dict must contain the fields accepted
                by `create_fenestration_surface`: `name`, `surface_type`,
                `construction_name`, `building_surface_name`, `vertices`, and
                optionally `outside_boundary_condition_object`,
                `view_factor_to_ground`, `frame_and_divider_name`,
                `multiplier`, `number_of_vertices`.

        Returns:
            MCP response with `{"count": N, "succeeded": [name, ...],
            "failed": [{"name": ..., "error": ...}, ...]}`. `success` is
            True only when `failed` is empty.
        """
        succeeded: list[str] = []
        failed: list[dict] = []

        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                failed.append({"name": f"<item_{i}>", "error": "item must be a dict"})
                continue
            name = raw.get("name") or raw.get("Name") or f"<item_{i}>"
            try:
                payload = to_payload(FenestrationCreateInput.model_validate(raw))
            except ValidationError as e:
                first = e.errors()[0]
                failed.append(
                    {
                        "name": name,
                        "error": f"validation: {'.'.join(str(x) for x in first['loc'])}: {first['msg']}",
                    }
                )
                continue
            r = fenestration_tool.create(payload)
            if r.success:
                succeeded.append(name)
            else:
                failed.append({"name": name, "error": r.message})

        return ToolResponse(
            success=not failed,
            message=f"Batch create_fenestration_surface: {len(succeeded)} succeeded, {len(failed)} failed.",
            data={
                "count": len(items),
                "succeeded": succeeded,
                "failed": failed,
            },
        ).to_mcp_response()
