"""Checks, safe dimensional normalization, and saved-result coverage."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from ._base import ContractModel, NonEmptyStr, Sha256, assert_json_value, unique
from .refs import BuildingObjectRef


class ValueSnapshot(ContractModel):
    value: Any
    unit: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "ValueSnapshot":
        assert_json_value(self.value, "check snapshot value")
        return self


class ToleranceDecision(ContractModel):
    origin: Literal["tool_default", "profile_limit", "model_ruling"]
    value: float = Field(ge=0)
    unit: NonEmptyStr
    source_id: NonEmptyStr
    reason: NonEmptyStr


class SuggestedAction(ContractModel):
    kind: Literal["dimension_normalization", "model_review", "no_action"]
    description: NonEmptyStr


class SavedVerification(ContractModel):
    model_version_id: NonEmptyStr
    artifact_sha256: Sha256
    inspected_objects: tuple[BuildingObjectRef, ...] = Field(min_length=1)


class CheckResult(ContractModel):
    check_id: NonEmptyStr
    category: Literal["geometry", "topology", "coverage"]
    status: Literal["passed", "failed", "unresolved"]
    involved_objects: tuple[BuildingObjectRef, ...] = Field(min_length=1)
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    suggested_action: SuggestedAction
    tolerance: ToleranceDecision | None
    before: ValueSnapshot
    after: ValueSnapshot
    saved_verification: SavedVerification | None = None

    @model_validator(mode="after")
    def validate_check(self) -> "CheckResult":
        unique(self.evidence_ids, "check evidence_ids")
        if self.status == "passed" and self.saved_verification is None:
            raise ValueError("a passed check requires artifact-bound saved verification")
        if self.saved_verification is not None:
            involved = {(row.kind, row.id) for row in self.involved_objects}
            inspected = {(row.kind, row.id) for row in self.saved_verification.inspected_objects}
            if not involved <= inspected:
                raise ValueError("saved verification must inspect every involved object")
        return self


class OpeningSemanticState(ContractModel):
    object_kind: Literal["opening", "window"]
    opening_id: NonEmptyStr
    host_ids: tuple[NonEmptyStr, ...] = Field(min_length=1, max_length=2)
    p1: tuple[float, float]
    p2: tuple[float, float]
    z_range: tuple[float, float]

    @model_validator(mode="after")
    def validate_opening(self) -> "OpeningSemanticState":
        if self.p1 == self.p2:
            raise ValueError("an opening must have a non-zero plan span")
        if self.z_range[0] >= self.z_range[1]:
            raise ValueError("an opening must have an increasing z_range")
        if len(set(self.host_ids)) != len(self.host_ids):
            raise ValueError("opening host IDs must be distinct")
        return self


class ConnectivityState(ContractModel):
    connection_id: NonEmptyStr
    object_ids: tuple[NonEmptyStr, NonEmptyStr]


class SemanticSnapshot(ContractModel):
    floor_ids: tuple[NonEmptyStr, ...]
    wall_ids: tuple[NonEmptyStr, ...]
    room_ids: tuple[NonEmptyStr, ...]
    openings: tuple[OpeningSemanticState, ...]
    connectivity: tuple[ConnectivityState, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SemanticSnapshot":
        unique(self.wall_ids, "snapshot wall IDs")
        unique(self.room_ids, "snapshot room IDs")
        unique(self.floor_ids, "snapshot floor IDs")
        unique([row.opening_id for row in self.openings], "snapshot opening IDs")
        unique([row.connection_id for row in self.connectivity], "snapshot connection IDs")
        return self


class DimensionState(ContractModel):
    object_ref: BuildingObjectRef
    field_path: NonEmptyStr
    value_m: float


class SavedModelSnapshot(ContractModel):
    model_version_id: NonEmptyStr
    artifact_sha256: Sha256
    semantics: SemanticSnapshot
    dimensions: tuple[DimensionState, ...]

    @model_validator(mode="after")
    def validate_dimensions(self) -> "SavedModelSnapshot":
        keys = [(row.object_ref.kind, row.object_ref.id, row.field_path) for row in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValueError("saved snapshot dimensions must have unique object/field keys")
        opening_ids = {(row.object_kind, row.opening_id) for row in self.semantics.openings}
        for row in self.dimensions:
            ref = row.object_ref
            exists = (
                (ref.kind == "floor" and ref.id in self.semantics.floor_ids)
                or (ref.kind == "boundary" and ref.id in self.semantics.wall_ids)
                or (ref.kind == "space" and ref.id in self.semantics.room_ids)
                or ((ref.kind, ref.id) in opening_ids)
            )
            if not exists:
                raise ValueError("snapshot dimension must belong to an object in its semantic inventory")
        return self


class FeatureDisposition(ContractModel):
    feature_id: NonEmptyStr
    classification: Literal["drawing_noise", "genuine_narrow", "genuine_step", "genuine_offset"]
    decision: Literal["normalize", "preserve"]
    reason: NonEmptyStr

    @model_validator(mode="after")
    def protect_real_features(self) -> "FeatureDisposition":
        if self.classification != "drawing_noise" and self.decision != "preserve":
            raise ValueError("genuine narrow/step/offset features must be preserved")
        return self


class ExistingTargetLine(ContractModel):
    kind: Literal["existing_model_line"] = "existing_model_line"
    object_ref: BuildingObjectRef
    field_path: NonEmptyStr
    coordinate_m: float
    selection_reason: NonEmptyStr


class DimensionEdit(ContractModel):
    operation: Literal["set_dimension"] = "set_dimension"
    feature_id: NonEmptyStr
    object_ref: BuildingObjectRef
    field_path: NonEmptyStr
    before_m: float
    after_m: float
    target: ExistingTargetLine

    @model_validator(mode="after")
    def validate_edit(self) -> "DimensionEdit":
        if self.before_m == self.after_m:
            raise ValueError("a normalization edit must change a dimension")
        if self.after_m != self.target.coordinate_m:
            raise ValueError("normalization must align to an existing line exactly")
        return self


class NormalizationProposal(ContractModel):
    proposal_id: NonEmptyStr
    check_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    tolerance: ToleranceDecision
    features: tuple[FeatureDisposition, ...] = Field(min_length=1)
    edits: tuple[DimensionEdit, ...]
    before: SavedModelSnapshot
    after: SavedModelSnapshot

    @model_validator(mode="after")
    def validate_normalization(self) -> "NormalizationProposal":
        unique(self.check_ids, "normalization check_ids")
        feature_by_id = {row.feature_id: row for row in self.features}
        if len(feature_by_id) != len(self.features):
            raise ValueError("normalization feature IDs must be unique")
        if self.tolerance.unit != "m":
            raise ValueError("stage-0 normalization tolerance must be expressed in metres")
        for edit in self.edits:
            if edit.feature_id not in feature_by_id:
                raise ValueError("every dimension edit must identify a classified feature")
            if feature_by_id[edit.feature_id].decision != "normalize":
                raise ValueError("a preserved feature cannot receive a dimension edit")
            change_m = abs(Decimal(str(edit.after_m)) - Decimal(str(edit.before_m)))
            tolerance_m = Decimal(str(self.tolerance.value))
            if change_m > tolerance_m:
                raise ValueError("dimension normalization exceeds its declared tolerance")
        if self.before.semantics != self.after.semantics:
            raise ValueError(
                "normalization changed walls, rooms, opening coordinates/hosts, or connectivity"
            )
        def dimension_map(snapshot: SavedModelSnapshot) -> dict[tuple[str, str, str], float]:
            return {
                (row.object_ref.kind, row.object_ref.id, row.field_path): row.value_m
                for row in snapshot.dimensions
            }

        before = dimension_map(self.before)
        after = dimension_map(self.after)
        edited_keys: set[tuple[str, str, str]] = set()
        for edit in self.edits:
            key = (edit.object_ref.kind, edit.object_ref.id, edit.field_path)
            target_key = (edit.target.object_ref.kind, edit.target.object_ref.id, edit.target.field_path)
            if before.get(key) != edit.before_m or after.get(key) != edit.after_m:
                raise ValueError("dimension edit does not match the saved before/after snapshots")
            if before.get(target_key) != edit.target.coordinate_m:
                raise ValueError("normalization target is not an existing line in the before snapshot")
            edited_keys.add(key)
        all_keys = set(before) | set(after)
        if any(before.get(key) != after.get(key) for key in all_keys - edited_keys):
            raise ValueError("normalization changed an undeclared dimension")
        return self


class CoverageIssue(ContractModel):
    issue_id: NonEmptyStr
    statement: NonEmptyStr


class CoverageEntry(ContractModel):
    coverage_id: NonEmptyStr
    requirement_id: NonEmptyStr
    choice_hypothesis_id: NonEmptyStr
    saved_model_version_id: NonEmptyStr
    actual_objects: tuple[BuildingObjectRef, ...] = Field(min_length=1)
    check_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    open_issue_ids: tuple[NonEmptyStr, ...] = ()
    status: Literal["complete", "incomplete"]
    coarse_merge: bool = False

    @model_validator(mode="after")
    def validate_entry(self) -> "CoverageEntry":
        unique(self.check_ids, "coverage check_ids")
        unique(self.open_issue_ids, "coverage open_issue_ids")
        if self.status == "complete" and self.open_issue_ids:
            raise ValueError("coverage with open issues cannot be complete")
        if self.status == "incomplete" and not self.open_issue_ids:
            raise ValueError("incomplete coverage must name its open issues")
        return self
