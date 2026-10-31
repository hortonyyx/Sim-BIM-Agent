"""Building-specific evidence packets and localized results for generic roles."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ._base import ContractModel, NonEmptyStr, unique
from .declarations import BuildingDeclaration
from .refs import BuildingObjectRef, RunQualifiedEvidenceRef


class EvidencePackage(ContractModel):
    package_id: NonEmptyStr
    task_id: NonEmptyStr
    role_id: NonEmptyStr
    question: NonEmptyStr
    known_evidence_ids: tuple[NonEmptyStr, ...]
    image_refs: tuple[RunQualifiedEvidenceRef, ...] = Field(min_length=1)
    source_model_version_id: NonEmptyStr
    return_schema: Literal["localized_evidence_result_v1"] = "localized_evidence_result_v1"
    budget_reservation_id: NonEmptyStr

    @model_validator(mode="after")
    def validate_package(self) -> "EvidencePackage":
        unique(self.known_evidence_ids, "package known_evidence_ids")
        if any(row.reference.scheme != "view_id" for row in self.image_refs):
            raise ValueError("evidence-package images must reuse view_id references")
        return self


class PixelLocation(ContractModel):
    image_ref: RunQualifiedEvidenceRef
    box_original_pixels: tuple[float, float, float, float]

    @model_validator(mode="after")
    def validate_box(self) -> "PixelLocation":
        left, top, right, bottom = self.box_original_pixels
        if not (0 <= left < right and 0 <= top < bottom):
            raise ValueError("localized evidence needs a positive original-pixel box")
        if self.image_ref.reference.scheme != "view_id":
            raise ValueError("localized image evidence must reuse a view_id")
        return self


class DirectObservation(ContractModel):
    observation_id: NonEmptyStr
    statement: NonEmptyStr
    location: PixelLocation


class LocalInterpretation(ContractModel):
    interpretation_id: NonEmptyStr
    statement: NonEmptyStr
    based_on_observation_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class LocalUncertainty(ContractModel):
    uncertainty_id: NonEmptyStr
    statement: NonEmptyStr
    related_observation_ids: tuple[NonEmptyStr, ...] = ()


class LocalizedEvidenceResult(ContractModel):
    package_id: NonEmptyStr
    task_id: NonEmptyStr
    based_on_source_model_version_id: NonEmptyStr
    directly_seen: tuple[DirectObservation, ...]
    interpretations: tuple[LocalInterpretation, ...] = ()
    uncertain: tuple[LocalUncertainty, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> "LocalizedEvidenceResult":
        seen = [row.observation_id for row in self.directly_seen]
        unique(seen, "localized observation IDs")
        unique([row.interpretation_id for row in self.interpretations], "localized interpretation IDs")
        unique([row.uncertainty_id for row in self.uncertain], "localized uncertainty IDs")
        known = set(seen)
        for row in self.interpretations:
            if missing := set(row.based_on_observation_ids) - known:
                raise ValueError(f"interpretation cites unseen observations: {sorted(missing)}")
        for row in self.uncertain:
            if missing := set(row.related_observation_ids) - known:
                raise ValueError(f"uncertainty cites unseen observations: {sorted(missing)}")
        return self


def assert_result_applicable(
    package: EvidencePackage,
    result: LocalizedEvidenceResult,
    current_source_model_version_id: str,
) -> None:
    """Coordinator preflight: refuse mismatched or stale localized advice."""

    if result.package_id != package.package_id or result.task_id != package.task_id:
        raise ValueError("localized result does not belong to this evidence package")
    if result.based_on_source_model_version_id != package.source_model_version_id:
        raise ValueError("localized result was produced against the wrong source version")
    if current_source_model_version_id != package.source_model_version_id:
        raise ValueError("stale source model version; localized result must not be applied")
    supplied_images = set(package.image_refs)
    if any(row.location.image_ref not in supplied_images for row in result.directly_seen):
        raise ValueError("localized result cites an image that was not supplied in its evidence package")


class FloorDraftContract(ContractModel):
    """Shape for the later floor drafter; stage 0 does not authorize applying it."""

    maturity: Literal["draft_only"] = "draft_only"
    floor_id: NonEmptyStr
    based_on_source_model_version_id: NonEmptyStr
    proposed_declaration: BuildingDeclaration
    open_questions: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_floor_draft(self) -> "FloorDraftContract":
        scope = self.proposed_declaration.scope
        if scope.kind != "floor" or scope.ids != (self.floor_id,):
            raise ValueError("a floor draft must contain one declaration scoped to that floor")
        return self
