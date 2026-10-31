from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import (
    HVACTemplateThermostatSchema,
    HVACTemplateZoneIdealLoadsAirSystemSchema,
)


class ThermostatTool(BaseTool):
    """Tool for managing EnergyPlus HVACTemplate:Thermostat objects.

    Handles CRUD operations for HVAC thermostats that define heating
    and cooling setpoint schedules. Referenced by ideal loads systems,
    so deletion checks for these dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Thermostat")

    @property
    def storage(self) -> dict[str, HVACTemplateThermostatSchema]:
        return {
            thermostat.name: thermostat for thermostat in self.state.hvac.thermostats
        }

    def _add_to_storage(self, instance: HVACTemplateThermostatSchema) -> None:
        self.state.hvac.thermostats.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.hvac.thermostats = [
            thermostat
            for thermostat in self.state.hvac.thermostats
            if thermostat.name != name
        ]

    def _update_storage(
        self, name: str, instance: HVACTemplateThermostatSchema
    ) -> None:
        self.state.hvac.thermostats = [
            thermostat
            for thermostat in self.state.hvac.thermostats
            if thermostat.name != name
        ]
        self.state.hvac.thermostats.append(instance)

    def _validate_and_create(
        self, data: dict[str, Any]
    ) -> HVACTemplateThermostatSchema:
        return HVACTemplateThermostatSchema.model_validate(data)

    def _get_name(self, instance: HVACTemplateThermostatSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []
        for ils in self.state.hvac.ideal_loads_systems:
            if ils.template_thermostat_name == name:
                refs.append(f"IdealLoadsSystem:{ils.zone_name}")
        return refs


class IdealLoadsSystemTool(BaseTool):
    """Tool for managing EnergyPlus HVACTemplate:Zone:IdealLoadsAirSystem objects.

    Handles CRUD operations for ideal loads air systems, keyed by zone name.
    These are leaf HVAC components with no downstream references.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "IdealLoadsSystem")

    @property
    def storage(self) -> dict[str, HVACTemplateZoneIdealLoadsAirSystemSchema]:
        return {ils.zone_name: ils for ils in self.state.hvac.ideal_loads_systems}

    def _add_to_storage(
        self, instance: HVACTemplateZoneIdealLoadsAirSystemSchema
    ) -> None:
        self.state.hvac.ideal_loads_systems.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.hvac.ideal_loads_systems = [
            ils for ils in self.state.hvac.ideal_loads_systems if ils.zone_name != name
        ]

    def _update_storage(
        self, name: str, instance: HVACTemplateZoneIdealLoadsAirSystemSchema
    ) -> None:
        self.state.hvac.ideal_loads_systems = [
            ils for ils in self.state.hvac.ideal_loads_systems if ils.zone_name != name
        ]
        self.state.hvac.ideal_loads_systems.append(instance)

    def _validate_and_create(
        self, data: dict[str, Any]
    ) -> HVACTemplateZoneIdealLoadsAirSystemSchema:
        return HVACTemplateZoneIdealLoadsAirSystemSchema.model_validate(data)

    def _get_name(self, instance: HVACTemplateZoneIdealLoadsAirSystemSchema) -> str:
        return instance.zone_name

    def _check_references(self, name: str) -> list[str]:
        return []
