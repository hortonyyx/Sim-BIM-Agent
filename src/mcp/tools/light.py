from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import LightSchema


class LightTool(BaseTool):
    """Tool for managing EnergyPlus Lights objects.

    Handles CRUD operations for internal lighting loads including
    power levels, schedules, and heat gain fractions.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Light")

    @property
    def storage(self) -> dict[str, LightSchema]:
        return {light.name: light for light in self.state.lights}

    def _add_to_storage(self, instance: LightSchema) -> None:
        self.state.lights.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.lights = [light for light in self.state.lights if light.name != name]

    def _update_storage(self, name: str, instance: LightSchema) -> None:
        self.state.lights = [light for light in self.state.lights if light.name != name]
        self.state.lights.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> LightSchema:
        return LightSchema.model_validate(data)

    def _get_name(self, instance: LightSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        return []
