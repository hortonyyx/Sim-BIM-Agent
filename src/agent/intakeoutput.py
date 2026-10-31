"""5_intakeoutput — deterministic assembly of the final IntakeOutput.

Combines the deterministic geometry specs (from the kernel serializer) with the
non-geometry specs the 4_MEP LLM stage authors, then runs a deterministic
contract check before the result leaves the project boundary. The check is an
early, deterministic fail for the one cross-stage hazard the split introduces:
the serializer emits construction names (e.g. `Cons_InterFloor`) that 4_MEP must
define — if 4_MEP omits one, the surfaces referencing it would drop and EnergyPlus
would fatal. We catch that here, by name, not at EP.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from src.agent.state import IntakeOutput
from src.validator import BuildingSchema, SiteLocationSchema

if TYPE_CHECKING:
    from src.agent.output_coordinates import OutputCoordinateContract


class MepOutput(BaseModel):
    """The 4_MEP stage's output: everything in IntakeOutput except the three
    geometry spec fields (zone/surface/fenestration), which come from the kernel."""

    building: BuildingSchema = Field(description="Building object")
    site_location: SiteLocationSchema = Field(description="Site location")
    material_specs: str
    construction_specs: str
    schedule_specs: str
    # 2026-08-14 dispatch: hvac_specs is never authored by the LLM — run_mep
    # (src/agent/legacy/pipeline.py) unconditionally overwrites it with a
    # deterministic render, regardless of what (if anything) is supplied
    # here. Defaulted to "" (rather than required) so a model response that
    # drops the key entirely still validates — the override runs either way,
    # this only widens which malformed LLM responses are tolerated.
    hvac_specs: str = ""
    people_specs: str
    lights_specs: str


def assemble_intake_output(
    *,
    zone_specs: str,
    surface_specs: str,
    fenestration_specs: str,
    mep: MepOutput,
    output_coordinates: "OutputCoordinateContract | None" = None,
) -> IntakeOutput:
    """Stitch the deterministic geometry specs and the MEP specs into one
    IntakeOutput. Pure mechanical merge — no field is invented here, with one
    deliberate exception: `building.north_axis`.

    E4-output-contract spec v2 §4.2 — S5 unconditional override. When
    ``output_coordinates`` is given, `Building.North Axis` is UNCONDITIONALLY
    replaced by `output_coordinates.north_axis_deg` (0.0 for `world_legacy`,
    the accepted correction's theta for `relative_north_axis`); MEP's own
    `north_axis` (checked elsewhere to be the 0.0 compatibility placeholder,
    `mep.building_north_axis_placeholder`) never reaches the final IntakeOutput
    and is never compared against theta for a "conflict" — 0 carries no
    evidentiary weight, so `0 != theta` is not a disagreement. `mep` itself is
    never mutated (the caller's object stays exactly what it was); a fresh
    `BuildingSchema` is rebuilt through validation so this cannot be bypassed
    via `model_copy(update=...)`.

    ``output_coordinates=None`` (the default) preserves the exact pre-E4
    behavior byte-for-byte — `building` is copied verbatim from `mep` — so
    every existing legacy caller/test is unaffected.
    """
    building = mep.building
    if output_coordinates is not None:
        if float(mep.building.north_axis) != 0.0:
            raise ValueError(
                "assemble_intake_output: mep.building.north_axis must be the 0.0 "
                f"compatibility placeholder, got {mep.building.north_axis!r} "
                "(S4's mep.building_north_axis_placeholder gate should have caught this)"
            )
        building = BuildingSchema.model_validate(
            {**mep.building.model_dump(by_alias=True), "North Axis": output_coordinates.north_axis_deg}
        )
    intake = IntakeOutput(
        building=building,
        site_location=mep.site_location,
        zone_specs=zone_specs,
        material_specs=mep.material_specs,
        schedule_specs=mep.schedule_specs,
        construction_specs=mep.construction_specs,
        surface_specs=surface_specs,
        fenestration_specs=fenestration_specs,
        hvac_specs=mep.hvac_specs,
        people_specs=mep.people_specs,
        lights_specs=mep.lights_specs,
    )
    if output_coordinates is not None:
        intake = IntakeOutput.model_validate_json(intake.model_dump_json())
        if intake.building.north_axis != output_coordinates.north_axis_deg:
            raise ValueError(
                "assemble_intake_output: post round-trip intake.building.north_axis "
                f"({intake.building.north_axis}) does not equal "
                f"output_coordinates.north_axis_deg ({output_coordinates.north_axis_deg})"
            )
    return intake


def _defines(text: str, name: str) -> bool:
    """Case-sensitive whole-token match (names are case-sensitive in EnergyPlus
    and cross-field references must be literally identical). Underscores are word
    chars, so `Default_Ext_Wall` does NOT match inside `Default_Ext_Wall_2`."""
    return re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text) is not None


def validate_contract(
    intake: IntakeOutput, used_constructions: set[str]
) -> list[str]:
    """Deterministic cross-stage contract check. Returns the list of issues.

    Hard issue (the caller raises on these): a construction the geometry
    serializer referenced is not defined in `construction_specs` — a 4_MEP
    omission that would drop surfaces at EnergyPlus.
    """
    issues: list[str] = []
    for cons in sorted(used_constructions):
        if not _defines(intake.construction_specs, cons):
            issues.append(
                f"construction '{cons}' is referenced by the geometry specs but "
                f"not defined in construction_specs (4_MEP must define it)"
            )
    return issues
