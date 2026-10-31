from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import SiteLocationSchema


class LocationTool(BaseTool):
    """Tool for managing the EnergyPlus Site:Location object.

    Handles CRUD operations for site location settings including
    latitude, longitude, time zone, and elevation. This is a
    singleton object (only one location per configuration).
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Location")

    @property
    def storage(self) -> dict[str, SiteLocationSchema]:
        if self.state.site_location:
            return {self.state.site_location.name: self.state.site_location}
        return {}

    def _add_to_storage(self, instance: SiteLocationSchema) -> None:
        self.state.site_location = instance

    def _remove_from_storage(self, name: str) -> None:
        if self.state.site_location and self.state.site_location.name == name:
            self.state.site_location = None
        else:
            raise ValueError(f"Site location with name {name} not found.")

    def _update_storage(self, name: str, instance: SiteLocationSchema) -> None:
        if self.state.site_location and self.state.site_location.name == name:
            self.state.site_location = instance
        else:
            raise ValueError(f"Site location with name {name} not found.")

    def _validate_and_create(self, data: dict[str, Any]) -> SiteLocationSchema:
        return SiteLocationSchema.model_validate(data)

    def _get_name(self, instance: SiteLocationSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        return []
