from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ValidationError

from src.mcp.interface import SchemaValidationError, ToolResponse
from src.mcp.state import ConfigState
from src.utils.logging import get_logger

logger = get_logger(__name__)


class BaseTool(ABC):
    """Abstract base class for EnergyPlus component CRUD tools.

    Provides generic create, read, update, delete, and list operations
    with validation and reference checking. Subclasses must implement
    storage access and component-specific logic.

    Args:
        state: Shared configuration state instance.
        component_name: Display name for the EnergyPlus component type.
    """

    def __init__(self, state: ConfigState, component_name: str):
        self.state = state
        self.component_name = component_name

    @property
    @abstractmethod
    def storage(self) -> dict[str, BaseModel]: ...

    @abstractmethod
    def _validate_and_create(self, data: dict[str, Any]) -> BaseModel:
        """Validate input data and create a new schema instance.

        Args:
            data: Dictionary of field values to validate.

        Returns:
            Validated Pydantic model instance.
        """
        ...

    @abstractmethod
    def _get_name(self, instance: Any) -> str:
        """Extract the unique name identifier from a component instance.

        Args:
            instance: Component schema instance.

        Returns:
            Unique name string for the component.
        """
        ...

    @abstractmethod
    def _check_references(self, name: str) -> list[str]:
        """Check if other components reference the named component.

        Args:
            name: Name of the component to check references for.

        Returns:
            List of referencing component identifiers (e.g. 'Surface:Wall1').
        """
        ...

    @abstractmethod
    def _add_to_storage(self, instance: Any) -> None:
        """Add a new component instance to the configuration state.

        Args:
            instance: Validated component schema instance to store.
        """
        ...

    @abstractmethod
    def _remove_from_storage(self, name: str) -> None:
        """Remove a component from the configuration state by name.

        Args:
            name: Name of the component to remove.
        """
        ...

    @abstractmethod
    def _update_storage(self, name: str, instance: Any) -> None:
        """Replace an existing component in storage with an updated instance.

        Args:
            name: Current name of the component to update.
            instance: New validated component schema instance.
        """
        ...

    def create(self, data: dict[str, Any]) -> ToolResponse:
        """Create a new component in the configuration.

        Validates input data, checks for name conflicts, and adds the
        component to storage.

        Args:
            data: Dictionary of field values for the new component.

        Returns:
            ToolResponse with success status and created component data.
        """
        name = data.get("Name", data.get("name", "<unknown>"))
        try:
            instance = self._validate_and_create(data)
            name = self._get_name(instance)

            if name in self.storage:
                return ToolResponse(
                    success=False,
                    message=f"Component '{self.component_name}':'{name}' already exists.",
                )

            self._add_to_storage(instance)
            logger.info(
                "Component '{}':'{}' created successfully.",
                self.component_name,
                name,
            )

            return ToolResponse(
                success=True,
                message=f"Component '{self.component_name}':'{name}' created successfully.",
                data={"name": name},
            )

        except ValidationError as e:
            errors = [
                SchemaValidationError(
                    field=".".join(str(loc) for loc in err["loc"]),
                    message=err["msg"],
                )
                for err in e.errors()
            ]
            return ToolResponse(
                success=False,
                message=f"Validation error for component '{self.component_name}':'{name}'.",
                data={"errors": [err.model_dump() for err in errors]},
            )

        except Exception as e:
            logger.exception(
                "Error creating component '{}':'{}'.", self.component_name, name
            )
            return ToolResponse(
                success=False,
                message=f"Error creating component '{self.component_name}':'{name}': {e!s}",
            )

    def read(self, name: str) -> ToolResponse:
        """Read a component from the configuration by name.

        Args:
            name: Unique name of the component to retrieve.

        Returns:
            ToolResponse with the component data if found.
        """
        if name not in self.storage:
            return ToolResponse(
                success=False,
                message=f"Component '{self.component_name}':'{name}' not found.",
            )

        instance = self.storage[name]
        return ToolResponse(
            success=True,
            message=f"Component '{self.component_name}':'{name}' read successfully.",
            data=instance.model_dump(by_alias=True),
        )

    def update(self, name: str, data: dict[str, Any]) -> ToolResponse:
        """Update an existing component with new field values.

        Merges the provided data with existing values and re-validates.
        Handles name changes by removing the old entry and adding a new one.

        Args:
            name: Current name of the component to update.
            data: Dictionary of field values to update (None values are skipped).

        Returns:
            ToolResponse with success status and updated component data.
        """
        if name not in self.storage:
            return ToolResponse(
                success=False,
                message=f"Component '{self.component_name}':'{name}' not found.",
            )

        try:
            existing = self.storage[name]
            existing_data = existing.model_dump(by_alias=True)
            for k, v in data.items():
                if v is not None:
                    existing_data[k] = v
            updated = existing.model_validate(existing_data)

            new_name = self._get_name(updated)
            if new_name != name:
                self._remove_from_storage(name)
                self._add_to_storage(updated)
                logger.info("Updated {}: {} -> {}", self.component_name, name, new_name)
            else:
                self._update_storage(name, updated)
                logger.info("Updated {}: {}", self.component_name, name)

            return ToolResponse(
                success=True,
                message=f"Component '{self.component_name}':'{name}' updated successfully.",
                data={"name": new_name},
            )

        except ValidationError as e:
            errors = [
                SchemaValidationError(
                    field=".".join(str(loc) for loc in err["loc"]),
                    message=err["msg"],
                )
                for err in e.errors()
            ]
            return ToolResponse(
                success=False,
                message=f"Validation error for component '{self.component_name}':'{name}'.",
                data={"errors": [err.model_dump() for err in errors]},
            )

        except Exception as e:
            logger.exception(
                "Error updating component '{}':'{}'.", self.component_name, name
            )
            return ToolResponse(
                success=False,
                message=f"Error updating component '{self.component_name}':'{name}': {e!s}",
            )

    def delete(self, name: str) -> ToolResponse:
        """Delete a component from the configuration by name.

        Checks for references from other components before deletion
        to maintain referential integrity.

        Args:
            name: Name of the component to delete.

        Returns:
            ToolResponse with success status. Fails if component is referenced.
        """
        if name not in self.storage:
            return ToolResponse(
                success=False,
                message=f"Component '{self.component_name}':'{name}' not found.",
            )

        refs = self._check_references(name)
        if refs:
            return ToolResponse(
                success=False,
                message=f"Component '{self.component_name}':'{name}' is referenced by other components.",
                data={"references": refs},
            )

        self._remove_from_storage(name)
        logger.info("Deleted {}':'{}'", self.component_name, name)
        return ToolResponse(
            success=True,
            message=f"Component '{self.component_name}':'{name}' deleted successfully.",
        )

    def list_all(self) -> ToolResponse:
        """List all components of this type in the configuration.

        Returns:
            ToolResponse with the list of component names. Use `read(name)` to
            fetch the full object when full attributes are needed.
        """
        names = [self._get_name(item) for item in self.storage.values()]

        return ToolResponse(
            success=True,
            message=f"Listed {len(names)} {self.component_name}s.",
            data=names,
        )
