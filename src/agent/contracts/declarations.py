"""Thin building declarations around the existing BIM tool inputs."""
from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import Field, model_validator

from ._base import ContractModel, NonEmptyStr, assert_json_value, unique
from .refs import BuildingObjectRef


class DeclarationScope(ContractModel):
    kind: Literal["floor", "wing", "object_group", "cross_floor_space"]
    ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "DeclarationScope":
        unique(self.ids, "declaration scope IDs")
        if self.kind in {"floor", "wing", "object_group"} and len(self.ids) != 1:
            raise ValueError(f"{self.kind} scope requires exactly one ID")
        if self.kind == "cross_floor_space" and len(self.ids) < 2:
            raise ValueError("cross_floor_space scope must name the space and at least one floor")
        return self


class ExistingToolCall(ContractModel):
    tool: Literal[
        "build_plan_bim",
        "assemble_plan_bim",
        "build_parametric_bim",
        "revise_bim",
    ]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self) -> "ExistingToolCall":
        expected = {
            "build_plan_bim": {"image", "plan_json"},
            "assemble_plan_bim": {"floors_json"},
            "build_parametric_bim": {"plan_json"},
            "revise_bim": {"candidate", "operations_json"},
        }[self.tool]
        if set(self.payload) != expected:
            raise ValueError(f"{self.tool} payload must contain exactly {sorted(expected)}")
        if any(not isinstance(value, str) or not value.strip() for value in self.payload.values()):
            raise ValueError("existing BIM tool payload values must be non-empty strings")
        if self.tool == "assemble_plan_bim":
            self._validate_assembly_rows(self.payload["floors_json"])
        assert_json_value(self.payload, "tool payload")
        return self

    @staticmethod
    def _validate_assembly_rows(floors_json: str) -> None:
        """Expose the existing assembly tool's architectural decisions.

        Floor identity and absolute elevation are model decisions.  Validating
        them here keeps an assembly declaration from being represented as an
        opaque deterministic post-processing step.
        """

        try:
            rows = json.loads(floors_json)
        except json.JSONDecodeError as exc:
            raise ValueError("assemble_plan_bim floors_json must be valid JSON") from exc
        if not isinstance(rows, list) or not 2 <= len(rows) <= 32:
            raise ValueError("assemble_plan_bim requires 2-32 floor declarations")
        expected = {"draft_id", "expected_plan_sha256", "floor_id", "z_floor", "evidence"}
        floor_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise ValueError(
                    "each assembly floor must contain draft_id, expected_plan_sha256, "
                    "floor_id, z_floor, and evidence"
                )
            for field in ("draft_id", "floor_id", "evidence"):
                if not isinstance(row[field], str) or not row[field].strip():
                    raise ValueError(f"assembly floor {field} must be a non-empty string")
            if row["floor_id"] in floor_ids:
                raise ValueError("assembly floor_id values must be unique")
            floor_ids.add(row["floor_id"])
            if (
                not isinstance(row["expected_plan_sha256"], str)
                or len(row["expected_plan_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in row["expected_plan_sha256"])
            ):
                raise ValueError("assembly expected_plan_sha256 must be lowercase SHA-256")
            if (
                isinstance(row["z_floor"], bool)
                or not isinstance(row["z_floor"], (int, float))
                or not math.isfinite(row["z_floor"])
            ):
                raise ValueError("assembly z_floor must be a finite number")


class FieldPartition(ContractModel):
    model_declared: tuple[NonEmptyStr, ...] = Field(min_length=1)
    code_expanded: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_partition(self) -> "FieldPartition":
        unique(self.model_declared, "model-declared field paths")
        unique(self.code_expanded, "code-expanded field paths")
        overlap = set(self.model_declared) & set(self.code_expanded)
        if overlap:
            raise ValueError(f"field ownership overlaps: {sorted(overlap)}")
        if any(not path.startswith("/") for path in self.model_declared + self.code_expanded):
            raise ValueError("field ownership entries must be absolute JSON-style paths")
        return self


class RepetitionInstance(ContractModel):
    instance_id: NonEmptyStr
    scope: DeclarationScope
    exceptions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_exceptions(self) -> "RepetitionInstance":
        if any(not path.startswith("/") for path in self.exceptions):
            raise ValueError("repetition exception keys must be absolute JSON-style paths")
        assert_json_value(self.exceptions, "repetition exceptions")
        return self


class RepetitionRule(ContractModel):
    repeat_by: Literal["floor", "wing", "object_group"]
    instances: tuple[RepetitionInstance, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_instances(self) -> "RepetitionRule":
        unique([row.instance_id for row in self.instances], "repetition instance IDs")
        return self


class InferenceHypothesis(ContractModel):
    hypothesis_id: NonEmptyStr
    kind: Literal["functional_requirement", "spatial_relation", "design_choice"]
    statement: NonEmptyStr
    object_refs: tuple[BuildingObjectRef, ...] = ()
    requirement_ids: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    hypothesis: Literal[True] = True

    @model_validator(mode="after")
    def validate_hypothesis(self) -> "InferenceHypothesis":
        unique(self.requirement_ids, "hypothesis requirement_ids")
        unique(self.evidence_ids, "hypothesis evidence_ids")
        if self.kind == "design_choice" and not self.requirement_ids:
            raise ValueError("a design-choice hypothesis must answer a user requirement")
        if self.kind == "spatial_relation" and len(self.object_refs) < 2:
            raise ValueError("a spatial-relation hypothesis must name at least two objects")
        return self


class BuildingDeclaration(ContractModel):
    declaration_id: NonEmptyStr
    scope: DeclarationScope
    tool_call: ExistingToolCall
    field_partition: FieldPartition
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    requirement_ids: tuple[NonEmptyStr, ...] = ()
    hypothesis_ids: tuple[NonEmptyStr, ...] = ()
    repetition: RepetitionRule | None = None
    declared_object_refs: tuple[BuildingObjectRef, ...] = ()

    @model_validator(mode="after")
    def validate_ids(self) -> "BuildingDeclaration":
        unique(self.evidence_ids, "declaration evidence_ids")
        unique(self.requirement_ids, "declaration requirement_ids")
        unique(self.hypothesis_ids, "declaration hypothesis_ids")
        if self.tool_call.tool == "assemble_plan_bim":
            required = {"/assembly/floors/*/floor_id", "/assembly/floors/*/z_floor"}
            missing = required - set(self.field_partition.model_declared)
            if missing:
                raise ValueError(
                    "an assembly declaration must assign floor identity and absolute "
                    f"elevation to the model: {sorted(missing)}"
                )
        return self


class BuildingModelVersion(ContractModel):
    model_version_id: NonEmptyStr
    source_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> "BuildingModelVersion":
        unique(self.declaration_ids, "model-version declaration_ids")
        return self
