"""Deterministic Lite BIM grid adoption for elevation-reader values."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from src.agent.geometry.lite_bim_regularization import (
    DEFAULT_LITE_GRID_M,
    quantize_m,
)


ELEVATION_REGULARIZATION_SCHEMA = "elevation_lite_regularization_v1"


def _reading_specs(artifact: Mapping[str, Any]):
    for row in artifact.get("elevations", ()):
        yield "elevation", row, "value_m"
    for row in artifact.get("openings", ()):
        for field in ("width_m", "sill_m", "head_m"):
            yield "opening", row, field


def regularize_elevation_artifact(
    artifact: Mapping[str, Any], *, grid_step_m: float = DEFAULT_LITE_GRID_M
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an elevation artifact whose adopted dimensions lie on one metre grid.

    Every submitted or ink-aligned reading remains in the report beside the
    adopted value.  The operation never fills missing readings and rejects a
    grid choice that would collapse an opening.
    """

    if isinstance(grid_step_m, bool) or not isinstance(grid_step_m, (int, float)):
        raise ValueError("grid_step_m must be a positive finite number")
    step = float(grid_step_m)
    if not math.isfinite(step) or step <= 0:
        raise ValueError("grid_step_m must be a positive finite number")

    result = copy.deepcopy(dict(artifact))
    result.pop("regularization", None)
    result.pop("artifact_sha256", None)
    readings = []
    for item_kind, row, field in _reading_specs(result):
        original = row.get(field)
        if isinstance(original, bool) or not isinstance(original, (int, float)):
            raise ValueError(
                f"{item_kind} {row.get('id')!r} has no observed numeric {field}; "
                "Lite regularization cannot replace it with a default"
            )
        original = float(original)
        adopted = float(quantize_m(original, grid_step_m=step))
        row[field] = adopted
        readings.append(
            {
                "item_kind": item_kind,
                "item_id": str(row.get("id")),
                "field": field,
                "evidence_type": str(row.get("evidence_type")),
                "original_m": original,
                "adopted_m": adopted,
                "shift_m": adopted - original,
            }
        )

    for row in result.get("openings", ()):
        if row["width_m"] <= 0:
            raise ValueError(
                f"opening {row.get('id')!r} width collapses on the {step:g} m Lite grid"
            )
        if row["head_m"] <= row["sill_m"]:
            raise ValueError(
                f"opening {row.get('id')!r} sill/head collapse or invert on the "
                f"{step:g} m Lite grid"
            )

    report = {
        "schema": ELEVATION_REGULARIZATION_SCHEMA,
        "status": "pass",
        "grid_step_m": step,
        "policy": "absolute levels and opening width/sill/head adopt the Lite grid",
        "readings": readings,
        "changes": [row for row in readings if row["shift_m"] != 0.0],
    }
    result["regularization"] = copy.deepcopy(report)
    return result, report


def validate_elevation_regularization(
    artifact: Mapping[str, Any], report_value: Any
) -> dict[str, Any]:
    """Validate replay metadata against the artifact's adopted values."""

    if not isinstance(report_value, Mapping):
        raise ValueError("regularization must be an object")
    report = copy.deepcopy(dict(report_value))
    allowed = {"schema", "status", "grid_step_m", "policy", "readings", "changes"}
    if extras := sorted(set(report) - allowed):
        raise ValueError(f"regularization has unknown fields: {extras}")
    if report.get("schema") != ELEVATION_REGULARIZATION_SCHEMA:
        raise ValueError("regularization has an unsupported schema")
    if report.get("status") != "pass":
        raise ValueError("regularization.status must be 'pass'")
    step = report.get("grid_step_m")
    if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step) or step <= 0:
        raise ValueError("regularization.grid_step_m must be a positive finite number")
    readings = report.get("readings")
    if not isinstance(readings, list):
        raise ValueError("regularization.readings must be an array")

    expected = {
        (kind, str(row.get("id")), field): (
            float(row[field]), str(row.get("evidence_type"))
        )
        for kind, row, field in _reading_specs(artifact)
    }
    found: dict[tuple[str, str, str], float] = {}
    for index, raw in enumerate(readings):
        if not isinstance(raw, Mapping):
            raise ValueError(f"regularization.readings[{index}] must be an object")
        required = {
            "item_kind", "item_id", "field", "evidence_type",
            "original_m", "adopted_m", "shift_m",
        }
        if set(raw) != required:
            raise ValueError(
                f"regularization.readings[{index}] fields do not match the schema"
            )
        key = (str(raw["item_kind"]), str(raw["item_id"]), str(raw["field"]))
        if key not in expected or key in found:
            raise ValueError(f"regularization.readings[{index}] references an unknown or duplicate field")
        original = raw["original_m"]
        adopted = raw["adopted_m"]
        shift = raw["shift_m"]
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (original, adopted, shift)
        ):
            raise ValueError(f"regularization.readings[{index}] values must be finite numbers")
        quantized = float(quantize_m(float(original), grid_step_m=float(step)))
        if not math.isclose(float(adopted), quantized, abs_tol=1e-9):
            raise ValueError(f"regularization.readings[{index}].adopted_m is not on its grid")
        expected_adopted, expected_evidence_type = expected[key]
        if str(raw["evidence_type"]) != expected_evidence_type:
            raise ValueError(
                f"regularization.readings[{index}].evidence_type does not match its object"
            )
        if not math.isclose(float(adopted), expected_adopted, abs_tol=1e-9):
            raise ValueError(f"regularization.readings[{index}] does not match the artifact value")
        if not math.isclose(float(shift), float(adopted) - float(original), abs_tol=1e-9):
            raise ValueError(f"regularization.readings[{index}].shift_m is inconsistent")
        found[key] = float(adopted)
    if set(found) != set(expected):
        raise ValueError("regularization.readings does not cover every adopted elevation value")

    changes = report.get("changes")
    expected_changes = [row for row in readings if row["shift_m"] != 0.0]
    if changes != expected_changes:
        raise ValueError("regularization.changes must list exactly the changed readings")
    return report


__all__ = [
    "ELEVATION_REGULARIZATION_SCHEMA",
    "regularize_elevation_artifact",
    "validate_elevation_regularization",
]
