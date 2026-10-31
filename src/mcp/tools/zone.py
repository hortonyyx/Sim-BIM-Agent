from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import ZoneSchema


class ZoneTool(BaseTool):
    """Tool for managing EnergyPlus Zone objects.

    Handles CRUD operations for thermal zones. Zones are referenced by
    surfaces and HVAC ideal loads systems, so deletion checks for
    these dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Zone")

    @property
    def storage(self) -> dict[str, ZoneSchema]:
        return {zone.name: zone for zone in self.state.zones}

    def _add_to_storage(self, instance: ZoneSchema) -> None:
        self.state.zones.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.zones = [zone for zone in self.state.zones if zone.name != name]

    def _update_storage(self, name: str, instance: ZoneSchema) -> None:
        self.state.zones = [zone for zone in self.state.zones if zone.name != name]
        self.state.zones.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> ZoneSchema:
        return ZoneSchema.model_validate(data)

    def _get_name(self, instance: ZoneSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []

        for surface in self.state.surfaces:
            if surface.zone_name == name:
                refs.append(f"Surface:{surface.name}")

        for ils in (
            self.state.hvac.ideal_loads_systems
            if self.state.hvac and self.state.hvac.ideal_loads_systems
            else []
        ):
            if ils.zone_name == name:
                refs.append(f"IdealLoadsSystem:{ils.zone_name}")

        return refs
