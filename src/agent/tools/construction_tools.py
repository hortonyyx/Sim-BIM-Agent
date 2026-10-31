from langchain_core.tools import BaseTool, tool

from src.mcp.state import ConfigState
from src.mcp.tools.construction import ConstructionTool
from src.mcp.tools.material import MaterialTool


def make_construction_tools(config: ConfigState) -> list[BaseTool]:
    ct = ConstructionTool(config)

    @tool
    def create_construction(name: str, layers: list[str]) -> str:
        """Create a Construction as an ordered list of material layers.

        Args:
            name: Unique construction name (e.g., 'ExtWall_Brick').
            layers: Material names from outside to inside. All names must
                    already exist in the materials list. >= 1 layer.
        """
        return ct.create({"Name": name, "Layers": layers}).model_dump_json()

    @tool
    def list_constructions() -> str:
        """List all constructions."""
        return ct.list_all().model_dump_json()

    @tool
    def get_construction(name: str) -> str:
        """Read a construction by name."""
        return ct.read(name).model_dump_json()

    @tool
    def delete_construction(name: str) -> str:
        """Delete a construction. Fails if referenced by surfaces/fenestration."""
        return ct.delete(name).model_dump_json()

    @tool
    def list_materials() -> str:
        """Read-only: list all materials available for use as construction layers."""
        return MaterialTool(config).list_all().model_dump_json()

    return [
        create_construction,
        list_constructions,
        get_construction,
        delete_construction,
        list_materials,
    ]
