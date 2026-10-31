from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import MaterialSchema


class MaterialTool(BaseTool):
    """Tool for managing EnergyPlus Material objects.

    Handles CRUD operations for all material types (Standard, NoMass,
    AirGap, Glazing). Materials are referenced by constructions, so
    deletion checks for construction dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Material")

    @property
    def storage(self) -> dict[str, MaterialSchema]:
        return {material.name: material for material in self.state.materials}

    def _add_to_storage(self, instance: MaterialSchema) -> None:
        self.state.materials.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.materials = [
            material for material in self.state.materials if material.name != name
        ]

    def _update_storage(self, name: str, instance: MaterialSchema) -> None:
        self.state.materials = [
            material for material in self.state.materials if material.name != name
        ]
        self.state.materials.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> MaterialSchema:
        return MaterialSchema.model_validate(data)

    def _get_name(self, instance: MaterialSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []
        for construction in self.state.constructions:
            if name in construction.layers:
                refs.append(f"Construction:{construction.name}")
        return refs
