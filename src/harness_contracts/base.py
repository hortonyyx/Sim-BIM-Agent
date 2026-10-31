"""Small, domain-neutral primitives shared by harness contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    """Base class for immutable wire contracts with closed fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


class KnownTimestamp(ContractModel):
    kind: Literal["known"] = "known"
    value: datetime

    @field_validator("value")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must include a timezone")
        return value


class HistoricalTimestampMissing(ContractModel):
    kind: Literal["historical_missing"] = "historical_missing"
    reason: NonEmptyStr


EventTimestamp = Annotated[
    KnownTimestamp | HistoricalTimestampMissing,
    Field(discriminator="kind"),
]


class RootTask(ContractModel):
    kind: Literal["root"] = "root"


class KnownParentTask(ContractModel):
    kind: Literal["known"] = "known"
    task_id: NonEmptyStr


class HistoricalParentMissing(ContractModel):
    kind: Literal["historical_missing"] = "historical_missing"
    reason: NonEmptyStr


ParentTaskRef = Annotated[
    RootTask | KnownParentTask | HistoricalParentMissing,
    Field(discriminator="kind"),
]
