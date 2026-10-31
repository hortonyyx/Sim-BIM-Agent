from typing import Any

from src.mcp.interface import ToolResponse
from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import BuildingSchema


def _requested_north_axis(data: dict[str, Any]) -> float | None:
    """Read either API spelling without inferring any coordinate mode."""
    for key in ("North Axis", "north_axis"):
        if key in data and data[key] is not None:
            return float(data[key])
    return None


class BuildingTool(BaseTool):
    """Tool for managing EnergyPlus Building objects.

    Handles CRUD operations for the building configuration, which is a
    singleton object (only one building per configuration).
    """

    def __init__(self, state: ConfigState, *, output_coordinates=None):
        super().__init__(state, "Building")
        self.output_coordinates = output_coordinates

    def update(self, name: str, data: dict[str, Any]):
        """Reject direct North-Axis ownership writes before they mutate state.

        An E4 run may only retain the already-derived contract value.  A
        standalone tool has no accepted correction identity, so it may not
        write a non-zero placeholder that EP would ignore in World mode.
        """
        requested = _requested_north_axis(data)
        if requested is not None:
            expected = (
                float(self.output_coordinates.north_axis_deg)
                if self.output_coordinates is not None else 0.0
            )
            if requested != expected:
                return ToolResponse(
                    success=False,
                    message=(
                        "Building.North Axis is owned by the output-coordinate contract "
                        f"(expected {expected!r}); direct update to {requested!r} rejected"
                    ),
                )
        return super().update(name, data)

    @property
    def storage(self) -> dict[str, BuildingSchema]:
        if self.state.building:
            return {self.state.building.name: self.state.building}
        return {}

    def _add_to_storage(self, instance: BuildingSchema) -> None:
        self.state.building = instance

    def _remove_from_storage(self, name: str) -> None:
        if self.state.building and self.state.building.name == name:
            self.state.building = None
        else:
            raise ValueError(f"Building with name {name} not found.")

    def _update_storage(self, name: str, instance: BuildingSchema) -> None:
        if self.state.building and self.state.building.name == name:
            self.state.building = instance
        else:
            raise ValueError(f"Building with name {name} not found.")

    def _validate_and_create(self, data: dict[str, Any]) -> BuildingSchema:
        return BuildingSchema.model_validate(data)

    def _get_name(self, instance: BuildingSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        return []
