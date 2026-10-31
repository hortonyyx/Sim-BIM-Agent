from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.mcp.tools.material import MaterialTool


def make_material_tools(config: ConfigState) -> list[BaseTool]:
    mt = MaterialTool(config)

    @tool
    def create_standard_material(
        name: str,
        roughness: str,
        thickness: float,
        conductivity: float,
        density: float,
        specific_heat: float,
    ) -> str:
        """Create a Standard material (solid layer with thermal mass).

        Args:
            name: Unique material name.
            roughness: One of VeryRough / Rough / MediumRough / MediumSmooth / Smooth / VerySmooth.
            thickness: Meters, > 0.
            conductivity: W/(m*K), > 0.
            density: kg/m^3, > 0.
            specific_heat: J/(kg*K), > 0.
        """
        return mt.create(
            {
                "Name": name,
                "Type": "Standard",
                "Roughness": roughness,
                "Thickness": thickness,
                "Conductivity": conductivity,
                "Density": density,
                "Specific_Heat": specific_heat,
            }
        ).model_dump_json()

    @tool
    def create_nomass_material(
        name: str,
        roughness: str,
        thermal_resistance: float,
    ) -> str:
        """Create a NoMass material (R-value only).

        Args:
            name: Unique material name.
            roughness: Same options as create_standard_material.
            thermal_resistance: R-value, m^2*K/W, > 0.
        """
        return mt.create(
            {
                "Name": name,
                "Type": "NoMass",
                "Roughness": roughness,
                "Thermal_Resistance": thermal_resistance,
            }
        ).model_dump_json()

    @tool
    def create_airgap_material(name: str, thermal_resistance: float) -> str:
        """Create an AirGap material (air cavity resistance)."""
        return mt.create(
            {
                "Name": name,
                "Type": "AirGap",
                "Thermal_Resistance": thermal_resistance,
            }
        ).model_dump_json()

    @tool
    def create_glazing_material(
        name: str,
        u_factor: float,
        solar_heat_gain_coefficient: float,
        visible_transmittance: float | None = None,
    ) -> str:
        """Create a Glazing material (simplified window).

        Args:
            name: Unique material name.
            u_factor: Overall U-value, W/(m^2*K), > 0.
            solar_heat_gain_coefficient: SHGC, 0-1.
            visible_transmittance: Optional VT, 0-1.
        """
        return mt.create(
            {
                "Name": name,
                "Type": "Glazing",
                "U-Factor": u_factor,
                "Solar_Heat_Gain_Coefficient": solar_heat_gain_coefficient,
                "Visible_Transmittance": visible_transmittance,
            }
        ).model_dump_json()

    @tool
    def list_materials() -> str:
        """List all materials."""
        return mt.list_all().model_dump_json()

    @tool
    def get_material(name: str) -> str:
        """Read a material by name."""
        return mt.read(name).model_dump_json()

    @tool
    def delete_material(name: str) -> str:
        """Delete a material. Fails if referenced by a construction."""
        return mt.delete(name).model_dump_json()

    return [
        create_standard_material,
        create_nomass_material,
        create_airgap_material,
        create_glazing_material,
        list_materials,
        get_material,
        delete_material,
    ]
