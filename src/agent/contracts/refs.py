"""References that preserve the project's existing BIM and evidence identities."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ._base import ContractModel, NonEmptyStr, Sha256


class BuildingObjectRef(ContractModel):
    """Reference an existing proposal/source-model object without rebinding it."""

    kind: Literal["space", "boundary", "opening", "window", "floor"]
    id: NonEmptyStr


class ExistingEvidenceRef(ContractModel):
    """Use IDs already emitted by view_image, claims, or record_inference."""

    scheme: Literal["view_id", "claim", "record_inference"]
    value: NonEmptyStr

    @model_validator(mode="after")
    def validate_existing_id(self) -> "ExistingEvidenceRef":
        patterns = {
            "view_id": r"^view_\d{4,}$",
            "claim": r"^claim_\d{4,}$",
            "record_inference": r"^inference_\d{3,}$",
        }
        import re

        if re.fullmatch(patterns[self.scheme], self.value) is None:
            raise ValueError(f"{self.value!r} is not an existing {self.scheme} identity")
        return self


class CoordinateRelation(ContractModel):
    """Explicit relation from original-image pixels to referenced coordinates."""

    original_space: Literal["original_image_pixels"] = "original_image_pixels"
    referenced_space: Literal["original_image_pixels", "view_pixels", "world_metres"]
    relation: Literal["identity", "crop", "affine", "crop_and_affine"]
    original_to_referenced_affine: tuple[float, float, float, float, float, float] | None = None
    crop_original_pixels: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_relation(self) -> "CoordinateRelation":
        if self.relation == "identity":
            if self.referenced_space != "original_image_pixels":
                raise ValueError("identity coordinates must remain in original image pixels")
            if self.original_to_referenced_affine is not None or self.crop_original_pixels is not None:
                raise ValueError("identity coordinates do not take a crop or affine transform")
        elif self.relation == "crop":
            if self.referenced_space != "view_pixels" or self.crop_original_pixels is None:
                raise ValueError("a crop must locate view pixels in original image pixels")
            left, top, right, bottom = self.crop_original_pixels
            if not (0 <= left < right and 0 <= top < bottom):
                raise ValueError("crop_original_pixels must be a positive [left, top, right, bottom] box")
            if self.original_to_referenced_affine is not None:
                raise ValueError("a crop relation must not also provide an affine transform")
        elif self.relation == "affine":
            if self.original_to_referenced_affine is None:
                raise ValueError("an affine relation requires original_to_referenced_affine")
            a, b, _, d, e, _ = self.original_to_referenced_affine
            if a * e - b * d == 0:
                raise ValueError("coordinate affine transform must be invertible")
            if self.crop_original_pixels is not None:
                raise ValueError("an affine relation must not also provide a crop")
        else:
            if self.referenced_space != "view_pixels":
                raise ValueError("crop_and_affine is reserved for resized image views")
            if self.crop_original_pixels is None or self.original_to_referenced_affine is None:
                raise ValueError("a resized crop requires both its original crop and affine transform")
            left, top, right, bottom = self.crop_original_pixels
            if not (0 <= left < right and 0 <= top < bottom):
                raise ValueError("crop_original_pixels must be a positive [left, top, right, bottom] box")
            a, b, _, d, e, _ = self.original_to_referenced_affine
            if a * e - b * d == 0:
                raise ValueError("coordinate affine transform must be invertible")
        return self


class RunQualifiedEvidenceRef(ContractModel):
    """A portable evidence reference; never rely on a run-local ID alone."""

    run_id: NonEmptyStr
    reference: ExistingEvidenceRef
    original_sha256: Sha256
    coordinate_relation: CoordinateRelation

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.reference.scheme}:{self.reference.value}"
