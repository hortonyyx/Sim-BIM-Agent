from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import ConstructionSchema


class ConstructionTool(BaseTool):
    """Tool for managing EnergyPlus Construction objects.

    Handles CRUD operations for construction assemblies composed of
    material layers. Constructions are referenced by surfaces and
    fenestrations, so deletion checks for these dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Construction")

    @property
    def storage(self) -> dict[str, ConstructionSchema]:
        return {
            construction.name: construction for construction in self.state.constructions
        }

    def _add_to_storage(self, instance: ConstructionSchema) -> None:
        self.state.constructions.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.constructions = [
            construction
            for construction in self.state.constructions
            if construction.name != name
        ]

    def _update_storage(self, name: str, instance: ConstructionSchema) -> None:
        self.state.constructions = [
            construction
            for construction in self.state.constructions
            if construction.name != name
        ]
        self.state.constructions.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> ConstructionSchema:
        return ConstructionSchema.model_validate(data)

    def _get_name(self, instance: ConstructionSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []

        for surface in self.state.surfaces:
            if surface.construction_name == name:
                refs.append(f"Surface:{surface.name}")

        for fenestration in self.state.fenestrations:
            if fenestration.construction_name == name:
                refs.append(f"Fenestration:{fenestration.name}")
        return refs
