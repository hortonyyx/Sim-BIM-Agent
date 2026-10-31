"""Small strict primitives shared by the building contract modules."""
from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    """Immutable, strict and closed wire model used by all stage-0 contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


def assert_json_value(value: Any, label: str) -> Any:
    """Reject values that cannot have one deterministic JSON representation."""

    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be finite JSON data") from error
    return value


def unique(values: tuple[str, ...] | list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
