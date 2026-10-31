"""Building evidence, user requirements, inheritance, and value calculations."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ._base import ContractModel, NonEmptyStr, unique
from .refs import BuildingObjectRef, RunQualifiedEvidenceRef


class UserRequirement(ContractModel):
    """A user instruction or preference, deliberately separate from building facts."""

    requirement_id: NonEmptyStr
    kind: Literal["functional", "simplification", "fidelity", "preference", "constraint"]
    statement: NonEmptyStr
    target_objects: tuple[BuildingObjectRef, ...] = ()
    source: Literal["user"] = "user"


class EvidenceItem(ContractModel):
    evidence_id: NonEmptyStr
    kind: Literal["observation", "inference", "simplification", "assumption"]
    statement: NonEmptyStr
    source_refs: tuple[RunQualifiedEvidenceRef, ...] = ()
    requirement_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_basis(self) -> "EvidenceItem":
        unique(self.requirement_ids, "evidence requirement_ids")
        if self.kind == "observation" and not self.source_refs:
            raise ValueError("observations require located source_refs")
        if self.kind == "inference" and not any(
            ref.reference.scheme == "record_inference" for ref in self.source_refs
        ):
            raise ValueError("inference evidence must reuse a record_inference identity")
        return self


class EvidenceTemplate(ContractModel):
    template_id: NonEmptyStr
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> "EvidenceTemplate":
        unique(self.evidence_ids, "template evidence_ids")
        return self


class EvidenceGroup(ContractModel):
    group_id: NonEmptyStr
    members: tuple[BuildingObjectRef, ...] = Field(min_length=1)
    template_ids: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_ids(self) -> "EvidenceGroup":
        unique(self.template_ids, "group template_ids")
        unique(self.evidence_ids, "group evidence_ids")
        if not self.template_ids and not self.evidence_ids:
            raise ValueError("an evidence group must inherit a template or evidence item")
        return self


class ObjectEvidenceBinding(ContractModel):
    object_ref: BuildingObjectRef
    group_ids: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_ids(self) -> "ObjectEvidenceBinding":
        unique(self.group_ids, "binding group_ids")
        unique(self.evidence_ids, "binding evidence_ids")
        if not self.group_ids and not self.evidence_ids:
            raise ValueError("an object evidence binding cannot be empty")
        return self


class EvidenceConflict(ContractModel):
    conflict_id: NonEmptyStr
    evidence_ids: tuple[NonEmptyStr, NonEmptyStr]
    status: Literal["resolved", "unresolved"]
    resolution: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "EvidenceConflict":
        if self.evidence_ids[0] == self.evidence_ids[1]:
            raise ValueError("an evidence conflict must preserve two different sides")
        if self.status == "resolved" and self.resolution is None:
            raise ValueError("a resolved evidence conflict requires its resolution")
        if self.status == "unresolved" and self.resolution is not None:
            raise ValueError("an unresolved evidence conflict cannot pretend to have a resolution")
        return self


class BuildingEvidenceLedger(ContractModel):
    items: tuple[EvidenceItem, ...]
    templates: tuple[EvidenceTemplate, ...] = ()
    groups: tuple[EvidenceGroup, ...] = ()
    bindings: tuple[ObjectEvidenceBinding, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()

    @model_validator(mode="after")
    def validate_ledger(self) -> "BuildingEvidenceLedger":
        item_ids = [row.evidence_id for row in self.items]
        template_ids = [row.template_id for row in self.templates]
        group_ids = [row.group_id for row in self.groups]
        unique(item_ids, "evidence IDs")
        unique(template_ids, "evidence template IDs")
        unique(group_ids, "evidence group IDs")
        unique([row.conflict_id for row in self.conflicts], "evidence conflict IDs")
        known_items, known_templates, known_groups = set(item_ids), set(template_ids), set(group_ids)
        for row in self.templates:
            if missing := set(row.evidence_ids) - known_items:
                raise ValueError(f"template references unknown evidence: {sorted(missing)}")
        for row in self.groups:
            if missing := set(row.evidence_ids) - known_items:
                raise ValueError(f"group references unknown evidence: {sorted(missing)}")
            if missing := set(row.template_ids) - known_templates:
                raise ValueError(f"group references unknown templates: {sorted(missing)}")
        member_pairs = {(member.kind, member.id, group.group_id) for group in self.groups for member in group.members}
        for row in self.bindings:
            if missing := set(row.evidence_ids) - known_items:
                raise ValueError(f"binding references unknown evidence: {sorted(missing)}")
            if missing := set(row.group_ids) - known_groups:
                raise ValueError(f"binding references unknown groups: {sorted(missing)}")
            for group_id in row.group_ids:
                if (row.object_ref.kind, row.object_ref.id, group_id) not in member_pairs:
                    raise ValueError("an object can only bind a group that lists it as a member")
        for row in self.conflicts:
            if missing := set(row.evidence_ids) - known_items:
                raise ValueError(f"conflict references unknown evidence: {sorted(missing)}")
        return self

    def expanded_object_evidence(self) -> dict[str, tuple[str, ...]]:
        """Expand template/group inheritance to a canonical object-to-evidence map."""

        templates = {row.template_id: row.evidence_ids for row in self.templates}
        groups: dict[str, set[str]] = {}
        for row in self.groups:
            values = set(row.evidence_ids)
            for template_id in row.template_ids:
                values.update(templates[template_id])
            groups[row.group_id] = values
        expanded: dict[str, set[str]] = {}
        for row in self.bindings:
            key = f"{row.object_ref.kind}:{row.object_ref.id}"
            values = expanded.setdefault(key, set())
            values.update(row.evidence_ids)
            for group_id in row.group_ids:
                values.update(groups[group_id])
        return {key: tuple(sorted(values)) for key, values in sorted(expanded.items())}


class Quantity(ContractModel):
    value: float
    unit: Literal["px", "mm", "cm", "m"]


class CalculationStep(ContractModel):
    stage: Literal["raw", "converted", "normalized", "saved"]
    input: Quantity | None
    output: Quantity
    reason: NonEmptyStr


class CalculationChain(ContractModel):
    calculation_id: NonEmptyStr
    object_ref: BuildingObjectRef
    field_path: NonEmptyStr
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    pixel_scale_m_per_px: float | None = Field(default=None, gt=0)
    steps: tuple[CalculationStep, CalculationStep, CalculationStep, CalculationStep]

    @model_validator(mode="after")
    def validate_chain(self) -> "CalculationChain":
        if tuple(row.stage for row in self.steps) != ("raw", "converted", "normalized", "saved"):
            raise ValueError("calculation stages must be raw -> converted -> normalized -> saved")
        if self.steps[0].input is not None:
            raise ValueError("the raw calculation step must not have an input")
        for before, after in zip(self.steps, self.steps[1:]):
            if after.input != before.output:
                raise ValueError("each calculation input must equal the previous output exactly")
        raw, converted, normalized, saved = self.steps
        if converted.output.unit != "m":
            raise ValueError("the converted calculation value must be in metres")
        factors = {"mm": 0.001, "cm": 0.01, "m": 1.0}
        if raw.output.unit == "px":
            if self.pixel_scale_m_per_px is None:
                raise ValueError("pixel conversion requires an explicit pixel_scale_m_per_px")
            expected = raw.output.value * self.pixel_scale_m_per_px
        else:
            if self.pixel_scale_m_per_px is not None:
                raise ValueError("pixel_scale_m_per_px is only valid for raw pixel values")
            expected = raw.output.value * factors[raw.output.unit]
        import math

        if not math.isclose(converted.output.value, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("raw-to-converted value does not match the declared unit/scale")
        if normalized.output.unit != "m" or saved.output.unit != "m":
            raise ValueError("normalized and saved calculation values must be in metres")
        if saved.output != normalized.output:
            raise ValueError("the saved value must equal the normalized value exactly")
        unique(self.evidence_ids, "calculation evidence_ids")
        return self
