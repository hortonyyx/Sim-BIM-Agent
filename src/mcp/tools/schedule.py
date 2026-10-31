from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import ScheduleCompactSchema, ScheduleTypeLimitsSchema


class ScheduleTypeLimitsTool(BaseTool):
    """Tool for managing EnergyPlus ScheduleTypeLimits objects.

    Handles CRUD operations for schedule type limits that define valid
    ranges and units for schedule values. Referenced by compact schedules,
    so deletion checks for schedule dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "ScheduleTypeLimits")

    @property
    def storage(self) -> dict[str, ScheduleTypeLimitsSchema]:
        return {
            schedule_type_limit.name: schedule_type_limit
            for schedule_type_limit in self.state.schedules.schedule_type_limits
        }

    def _add_to_storage(self, instance: ScheduleTypeLimitsSchema) -> None:
        self.state.schedules.schedule_type_limits.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.schedules.schedule_type_limits = [
            schedule_type_limit
            for schedule_type_limit in self.state.schedules.schedule_type_limits
            if schedule_type_limit.name != name
        ]

    def _update_storage(self, name: str, instance: ScheduleTypeLimitsSchema) -> None:
        self.state.schedules.schedule_type_limits = [
            schedule_type_limit
            for schedule_type_limit in self.state.schedules.schedule_type_limits
            if schedule_type_limit.name != name
        ]
        self.state.schedules.schedule_type_limits.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> ScheduleTypeLimitsSchema:
        return ScheduleTypeLimitsSchema.model_validate(data)

    def _get_name(self, instance: ScheduleTypeLimitsSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []
        for schedule in self.state.schedules.schedules:
            if schedule.schedule_type_limits_name == name:
                refs.append(f"Schedule:Compact:{schedule.name}")
        return refs


class ScheduleCompactTool(BaseTool):
    """Tool for managing EnergyPlus Schedule:Compact objects.

    Handles CRUD operations for compact schedules that define time-varying
    values. Referenced by thermostats, ideal loads systems, people, and
    lights, so deletion checks for all these dependencies.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "Schedule:Compact")

    @property
    def storage(self) -> dict[str, ScheduleCompactSchema]:
        return {schedule.name: schedule for schedule in self.state.schedules.schedules}

    def _add_to_storage(self, instance: ScheduleCompactSchema) -> None:
        self.state.schedules.schedules.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.schedules.schedules = [
            schedule
            for schedule in self.state.schedules.schedules
            if schedule.name != name
        ]

    def _update_storage(self, name: str, instance: ScheduleCompactSchema) -> None:
        self.state.schedules.schedules = [
            schedule
            for schedule in self.state.schedules.schedules
            if schedule.name != name
        ]
        self.state.schedules.schedules.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> ScheduleCompactSchema:
        return ScheduleCompactSchema.model_validate(data)

    def _get_name(self, instance: ScheduleCompactSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        refs = []

        for thermostat in self.state.hvac.thermostats:
            if thermostat.heating_setpoint_schedule_name == name:
                refs.append(f"Thermostat:{thermostat.name}")
            if thermostat.cooling_setpoint_schedule_name == name:
                refs.append(f"Thermostat:{thermostat.name}")

        for ideal_loads_system in self.state.hvac.ideal_loads_systems:
            if ideal_loads_system.system_availability_schedule_name == name:
                refs.append(f"IdealLoadsSystem:{ideal_loads_system.zone_name}")

        for people in self.state.people:
            if people.number_of_people_schedule_name == name:
                refs.append(f"People:{people.name}")
            if people.activity_level_schedule_name == name:
                refs.append(f"People:{people.name}")

        for light in self.state.lights:
            if light.schedule_name == name:
                refs.append(f"Light:{light.name}")

        return refs
