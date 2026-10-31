from pydantic import BaseModel, Field

from src.validator import (
    BuildingSchema,
    GlobalGeometryRulesSchema,
    RunPeriodSchema,
    SimulationControlSchema,
    SiteLocationSchema,
)


class ToolResponse(BaseModel):
    """Standardized response object returned by all MCP tool operations.

    Wraps the result of a tool call with success status, message, and optional data
    payload for consistent API responses.
    """

    success: bool = Field(..., description="Whether the tool call was successful.")
    message: str = Field(..., description="The message from the tool call.")
    data: dict | list | None = Field(
        default=None, description="The data from the tool call."
    )

    def to_mcp_response(self) -> dict:
        """Convert the tool response to an MCP-compatible response dict.

        Returns:
            Dictionary with a 'result' key containing the serialized response.
        """
        return {
            "result": self.model_dump(),
        }


class SchemaValidationError(BaseModel):
    """Represents a single field-level validation error from Pydantic schema validation."""

    field: str = Field(..., description="The field that caused the validation error.")
    message: str = Field(..., description="The message from the validation error.")


class ConfigSummary(BaseModel):
    """Summary snapshot of the current EnergyPlus configuration state.

    Provides a high-level overview including component counts and key
    configuration objects for quick inspection.
    """

    building: BuildingSchema | None = Field(
        default=None, description="The building configuration."
    )
    site_location: SiteLocationSchema | None = Field(
        default=None, description="The site location configuration."
    )
    zones_count: int = Field(
        default=0, description="The number of zones in the configuration."
    )
    materials_count: int = Field(
        default=0, description="The number of materials in the configuration."
    )
    constructions_count: int = Field(
        default=0, description="The number of constructions in the configuration."
    )
    surfaces_count: int = Field(
        default=0, description="The number of surfaces in the configuration."
    )
    fenestrations_count: int = Field(
        default=0, description="The number of fenestrations in the configuration."
    )
    schedules_count: int = Field(
        default=0, description="The number of schedules in the configuration."
    )
    hvac_thermostats_count: int = Field(
        default=0, description="The number of HVAC thermostats in the configuration."
    )
    hvac_ideal_loads_count: int = Field(
        default=0, description="The number of HVAC ideal loads in the configuration."
    )
    simulation_control: SimulationControlSchema | None = Field(
        default=None, description="The simulation control configuration."
    )
    run_period: RunPeriodSchema | None = Field(
        default=None, description="The run period configuration."
    )
    global_geometry_rules: GlobalGeometryRulesSchema | None = Field(
        default=None, description="The global geometry rules configuration."
    )
