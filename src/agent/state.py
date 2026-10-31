from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from src.agent._share import DEFAULT_OUTPUT_DIR, MAX_RETRIES
from src.agent.output_coordinates import (
    OutputCoordinateContract,
    OutputCoordinateValidationContext,
    ZoneFrameNormalizationEntryV1,
)
from src.mcp.state import ConfigState
from src.validator import (
    BuildingSchema,
    HVACSchema,
    ScheduleCollectionSchema,
    SiteLocationSchema,
)


class IntakeOutput(BaseModel):
    """Structured output from intake LLM call.

    `building` and `site_location` are nested schemas populated directly
    by the LLM's structured output, so intake_node does not need to parse
    free text a second time.

    All `*_specs` fields are natural-language task instructions passed to
    the corresponding phase agent.
    """

    building: BuildingSchema = Field(
        description="Building object (name, orientation, terrain, tolerances)"
    )
    site_location: SiteLocationSchema = Field(
        description="Site location (latitude, longitude, time zone, elevation)"
    )
    zone_specs: str = Field(
        description="Zone creation instructions: count, names, dimensions, positions"
    )
    material_specs: str = Field(
        description="Material definitions with thermal properties"
    )
    schedule_specs: str = Field(
        description="Schedule definitions: occupancy, lighting, HVAC operation patterns"
    )
    construction_specs: str = Field(
        description="Construction assembly instructions referencing materials"
    )
    surface_specs: str = Field(
        description="Surface geometry instructions referencing zones and constructions"
    )
    fenestration_specs: str = Field(
        description="Window/door instructions referencing surfaces"
    )
    hvac_specs: str = Field(
        description="HVAC system type, thermostat setpoints, schedule references"
    )
    people_specs: str = Field(
        description="Occupancy: zone assignment, density, activity schedule per zone"
    )
    lights_specs: str = Field(
        description="Lighting: zone assignment, power density, schedule per zone"
    )


@dataclass(frozen=True)
class SimContext:
    """Immutable runtime context, passed via StateGraph context_schema."""

    epw_path: Path
    output_dir: Path = DEFAULT_OUTPUT_DIR
    run_simulate: bool = True  # False -> simulate_node only exports IDF
    # When set, EnergyPlus run artifacts go to output_dir/<ep_run_subdir>/ while
    # the IDF stays in output_dir (standard case layout: EP/ holds IDF, EP/EP_run/
    # the sim). None -> flat (legacy behaviour).
    ep_run_subdir: str | None = None


def _get_identity(item: Any) -> str:
    """Return the unique identity key for a schema item.

    Most schemas use `.name`; IdealLoadsAirSystem uses `.zone_name`
    (one system per zone).
    """
    if hasattr(item, "name"):
        return item.name
    if hasattr(item, "zone_name"):
        return item.zone_name
    if hasattr(item, "variable_name") and hasattr(item, "key_value"):
        return f"{item.key_value}_{item.variable_name}_{item.reporting_frequency}"
    raise ValueError(f"Cannot determine identity for {type(item).__name__}")


def _merge_named_list(old_items: list, new_items: list) -> list:
    """Union merge by identity key. New wins on conflict."""
    merged = {_get_identity(item): item for item in old_items}
    merged.update({_get_identity(item): item for item in new_items})
    return list(merged.values())


def _is_default(value: Any, field_name: str) -> bool:
    """Check whether `value` equals ConfigState's default for `field_name`."""
    info = ConfigState.model_fields[field_name]
    if info.default_factory is not None:
        return value == info.default_factory()
    return value is info.default or value == info.default


def _merge_schedules(
    old: ScheduleCollectionSchema,
    new: ScheduleCollectionSchema,
) -> ScheduleCollectionSchema:
    return ScheduleCollectionSchema.model_validate(
        {
            "schedule_type_limits": _merge_named_list(
                old.schedule_type_limits, new.schedule_type_limits
            ),
            "schedules": _merge_named_list(old.schedules, new.schedules),
        }
    )


def _merge_hvac(old: HVACSchema, new: HVACSchema) -> HVACSchema:
    return HVACSchema.model_validate(
        {
            "thermostats": _merge_named_list(old.thermostats, new.thermostats),
            "ideal_loads_systems": _merge_named_list(
                old.ideal_loads_systems, new.ideal_loads_systems
            ),
        }
    )


_NAMED_LIST_FIELDS: Final = (
    "zones",
    "materials",
    "constructions",
    "surfaces",
    "fenestrations",
    "people",
    "lights",
    "output_variable",
)

_SINGLETON_FIELDS: Final = (
    "building",
    "site_location",
    "simulation_control",
    "global_geometry_rules",
    "run_period",
    "output_variable_dictionary",
    "output_diagnostics",
    "output_table_summary_reports",
    "output_control_table_style",
)


def merge_config_state(old: ConfigState, new: ConfigState) -> ConfigState:
    """Field-level union merge for parallel-safe state updates.

    Three strategies:
    1. Named list fields -> union by identity key; new wins on conflict
    2. Nested containers (schedules, hvac) -> recursive merge
    3. Singleton objects -> non-default wins, new preferred
    """
    data: dict[str, Any] = {}

    for field_name in _NAMED_LIST_FIELDS:
        data[field_name] = _merge_named_list(
            getattr(old, field_name), getattr(new, field_name)
        )

    data["schedules"] = _merge_schedules(old.schedules, new.schedules)
    data["hvac"] = _merge_hvac(old.hvac, new.hvac)

    for field_name in _SINGLETON_FIELDS:
        new_val = getattr(new, field_name)
        old_val = getattr(old, field_name)
        data[field_name] = new_val if not _is_default(new_val, field_name) else old_val

    return ConfigState.model_validate(data)


class AgentState(BaseModel):
    """Top-level graph state.

    `messages` holds only intake conversation and one-line phase summaries.
    Phase agent tool-calling history lives in TraceCollector and is
    extracted separately for fine-tuning.
    """

    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    user_input: str = ""
    image_paths: list[str] = Field(default_factory=list)
    reading_vector_dir: str | None = None
    """Directory of reading-stage vector JSONs (+ reading_summary.md). When set
    (and `intake_output` is not pre-filled), intake_node runs the staged intake
    pipeline (vector JSON -> IntakeOutput). Populated by run_full_pipeline
    `--reading-from`."""
    testdata_text: str | None = None
    """Raw testdata_prompt.json content for the pipeline call. The prompt labels
    this block as JSON, so it must be the raw file, NOT the human-readable
    `user_input` summary. Populated by run_full_pipeline."""
    pipeline_out_dir: str | None = None
    """Where intake_node's pipeline writes its stage-numbered artifact subdirs
    (1_correction / 2_modelling / 3_split_pairing / 4_mep / 5_intakeoutput).
    Populated by run_full_pipeline (= <case>/ for the organized layout)."""

    config_state: Annotated[ConfigState, merge_config_state] = Field(
        default_factory=ConfigState
    )
    intake_output: IntakeOutput | None = None

    output_coordinate_contract: OutputCoordinateContract | None = None
    """E4 output-coordinate contract (World legacy vs Relative+theta), carried
    verbatim from `intake_node` through to `simulate_node`/export. NOT part of
    `ConfigState` (so `merge_config_state` never touches it) and NOT part of
    `IntakeOutput`'s 11-field wire — it is internal dispatch metadata only.
    Any node that has this set must not mutate it; a re-run of correction
    invalidates and re-derives it, it is never patched in place."""
    output_coordinate_context: OutputCoordinateValidationContext | None = None
    """Hash-chain verification context paired with `output_coordinate_contract`
    (raw accepted-correction bytes + raw snapshot bytes) — carried so the
    pre-export/post-convert gates can re-verify without re-reading disk state
    that may have moved."""
    zone_frame_normalizations: tuple[ZoneFrameNormalizationEntryV1, ...] = ()
    """Immutable §7.4 zone-frame normalizations made by the zone node.

    This is runtime/export evidence only: it is deliberately outside both the
    eleven-field IntakeOutput wire and ConfigState's merge reducer.
    """

    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = MAX_RETRIES


class AgentStateUpdate(TypedDict, total=False):
    """Partial update returned by graph nodes."""

    messages: Sequence[AnyMessage]
    user_input: str
    image_paths: list[str]
    reading_vector_dir: str | None
    testdata_text: str | None
    pipeline_out_dir: str | None
    config_state: ConfigState
    intake_output: IntakeOutput | None
    output_coordinate_contract: OutputCoordinateContract | None
    output_coordinate_context: OutputCoordinateValidationContext | None
    zone_frame_normalizations: tuple[ZoneFrameNormalizationEntryV1, ...]
    validation_errors: list[str]
    retry_count: int
