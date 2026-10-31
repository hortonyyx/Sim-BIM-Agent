from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import FenestrationSurfaceSchema


class FenestrationTool(BaseTool):
    """Tool for managing EnergyPlus FenestrationSurface:Detailed objects.

    Handles CRUD operations for fenestration surfaces such as windows,
    doors, and skylights. Fenestrations are leaf components with no
    downstream references.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "FenestrationSurface")

    @property
    def storage(self) -> dict[str, FenestrationSurfaceSchema]:
        return {fen.name: fen for fen in self.state.fenestrations}

    def _add_to_storage(self, instance: FenestrationSurfaceSchema) -> None:
        self.state.fenestrations.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.fenestrations = [
            fen for fen in self.state.fenestrations if fen.name != name
        ]

    def _update_storage(self, name: str, instance: FenestrationSurfaceSchema) -> None:
        self.state.fenestrations = [
            fen for fen in self.state.fenestrations if fen.name != name
        ]
        self.state.fenestrations.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> FenestrationSurfaceSchema:
        return FenestrationSurfaceSchema.model_validate(data)

    def _get_name(self, instance: FenestrationSurfaceSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        return []
