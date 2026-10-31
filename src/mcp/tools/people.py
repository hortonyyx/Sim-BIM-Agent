from typing import Any

from src.mcp.state import ConfigState
from src.mcp.tools.base import BaseTool
from src.validator.data_model import PeopleSchema


class PeopleTool(BaseTool):
    """Tool for managing EnergyPlus People (occupancy) objects.

    Handles CRUD operations for internal people/occupancy loads including
    occupancy counts, schedules, and heat gain fractions.
    """

    def __init__(self, state: ConfigState):
        super().__init__(state, "People")

    @property
    def storage(self) -> dict[str, PeopleSchema]:
        return {people.name: people for people in self.state.people}

    def _add_to_storage(self, instance: PeopleSchema) -> None:
        self.state.people.append(instance)

    def _remove_from_storage(self, name: str) -> None:
        self.state.people = [
            people for people in self.state.people if people.name != name
        ]

    def _update_storage(self, name: str, instance: PeopleSchema) -> None:
        self.state.people = [
            people for people in self.state.people if people.name != name
        ]
        self.state.people.append(instance)

    def _validate_and_create(self, data: dict[str, Any]) -> PeopleSchema:
        return PeopleSchema.model_validate(data)

    def _get_name(self, instance: PeopleSchema) -> str:
        return instance.name

    def _check_references(self, name: str) -> list[str]:
        return []
