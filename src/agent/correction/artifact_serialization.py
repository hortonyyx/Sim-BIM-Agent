"""Canonical byte serializers for correction attempt artifacts."""
from __future__ import annotations

from pydantic import BaseModel


def serialize_correction_output(model: BaseModel) -> bytes:
    """Return the versioned correction ``output.json`` byte projection."""
    schema_version = str(getattr(model, "schema_version", "1") or "1")
    if schema_version not in {"1", "2", "3"}:
        raise ValueError(f"unsupported correction serializer schema {schema_version!r}")
    if schema_version in {"1", "2"}:
        # Legacy schemas remain permissive on input, but B5-only host identity
        # keys must not leak into their versioned output projection.
        model = model.model_copy(deep=True)
        for window in getattr(model, "windows", ()):
            extras = getattr(window, "__pydantic_extra__", None)
            if isinstance(extras, dict):
                for key in (
                    "facade_segment_id",
                    "host_resolution_sha256",
                    "source_window_id",
                ):
                    extras.pop(key, None)
    return model.model_dump_json(indent=2).encode("utf-8")


def serialize_feature_states(model: BaseModel) -> bytes:
    """Return the sole ``feature_states.json`` byte projection."""
    return model.model_dump_json(indent=2).encode("utf-8")


__all__ = ["serialize_correction_output", "serialize_feature_states"]
