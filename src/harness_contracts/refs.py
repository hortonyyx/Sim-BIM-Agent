"""Content-addressed references used by the harness core."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import ContractModel, NonEmptyStr, Sha256


class HashedBlobRef(ContractModel):
    kind: Literal["sha256"] = "sha256"
    uri: NonEmptyStr
    media_type: NonEmptyStr
    sha256: Sha256


class HistoricalBlobHashMissing(ContractModel):
    kind: Literal["historical_hash_missing"] = "historical_hash_missing"
    uri: NonEmptyStr
    media_type: NonEmptyStr
    reason: NonEmptyStr


BlobRef = Annotated[
    HashedBlobRef | HistoricalBlobHashMissing,
    Field(discriminator="kind"),
]


class SourceRef(ContractModel):
    """Reference to either immutable bytes or another recorded event."""

    source_id: NonEmptyStr
    source_kind: Literal[
        "user",
        "runtime",
        "tool",
        "history",
        "external_coordinator",
        "generated",
    ]
    locator: NonEmptyStr
    blob: BlobRef | None = None
    event_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_resolvable_target(self) -> SourceRef:
        if self.blob is None and self.event_id is None:
            raise ValueError("a source reference needs a blob or event_id")
        return self


class ImageTransmission(ContractModel):
    original: BlobRef
    sent: HashedBlobRef
    request_reference: NonEmptyStr
