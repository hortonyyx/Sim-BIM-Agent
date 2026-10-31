"""Read-only projections of immutable reader deliveries for model rework context.

These projections are never persisted as artifacts or used to validate/rebase
edits. The session retains the complete registry artifact for those operations.
"""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from typing import Any

from src.agent_runtime.store import json_bytes


def _reference(value: Any, pointer: str) -> dict[str, Any]:
    raw = json_bytes(value)
    return {"json_pointer": pointer, "canonical_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_bytes": len(raw)}


def _summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"value_type": type(value).__name__}
    result = {key: value[key] for key in ("status", "grid_step_m", "schema", "schema_version", "rule_version")
              if key in value and isinstance(value[key], (str, int, float, bool))}
    summary = value.get("summary")
    if isinstance(summary, Mapping):
        result["counts"] = {key: count for key, count in summary.items()
                            if isinstance(count, (int, float)) and not isinstance(count, bool)}
    for field in ("items", "changes", "readings", "rejections", "openings"):
        if isinstance(value.get(field), list):
            result[field + "_count"] = len(value[field])
    lite = value.get("lite_bim")
    if isinstance(lite, Mapping):
        result["lite_bim"] = {key: lite[key] for key in ("status", "grid_step_m") if key in lite}
        if isinstance(lite.get("summary"), Mapping):
            result["lite_bim"]["counts"] = {key: count for key, count in lite["summary"].items()
                                              if isinstance(count, (int, float)) and not isinstance(count, bool)}
    return result


def previous_artifact_context(
    artifact: Mapping[str, Any] | None, *, role_id: str | None = None,
    artifact_reference: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Keep editable readings/evidence, replacing generated audits with references.

    ``artifact_reference`` is the already-verified registry reference, usually
    ``task['previous_artifact']``. Its stored hash is not renamed or recomputed;
    our separately named canonical hash documents this projection's source.
    ``context_audit`` is read-only prompt metadata, never a submission field.
    """
    if artifact is None:
        return None
    if not isinstance(artifact, Mapping):
        raise TypeError("previous artifact must be an object or None")
    if role_id not in (None, "plan_reader", "elevation_reader"):
        raise ValueError("previous artifact context supports plan/elevation readers only")
    omitted = []

    def omit(value, pointer):
        omitted.append({**_reference(value, pointer), "summary": _summary(value)})

    projected: dict[str, Any] = {}
    for key, value in artifact.items():
        if key in {"regularization", "ink_alignment", "reading_alignment", "artifact_sha256"}:
            omit(value, "/" + key)
        elif key == "plan" and isinstance(value, Mapping):
            plan = {}
            for field, original in value.items():
                if field in {"regularization", "reading_alignment"}:
                    omit(original, "/plan/" + field)
                elif field == "regularization_inputs" and isinstance(original, Mapping):
                    inputs = {}
                    for name, section in original.items():
                        if name == "reading_alignment":
                            omit(section, "/plan/regularization_inputs/reading_alignment")
                        else:
                            inputs[name] = copy.deepcopy(section)
                    if inputs:
                        plan[field] = inputs
                else:
                    plan[field] = copy.deepcopy(original)
            projected[key] = plan
        else:
            projected[key] = copy.deepcopy(value)

    full = _reference(artifact, "")
    audit = {
        "schema": "reader_previous_artifact_context_v1",
        "read_only": True,
        "instruction": (
            "context_audit is a read-only context summary, not a submit field. "
            "Edit only the requested geometry/readings using the tool schema. "
            "The complete original artifact and audits remain immutable on the server; "
            "omitting this summary cannot remove or change them."
        ),
        "canonical_artifact_sha256": full["canonical_sha256"],
        "canonical_artifact_bytes": full["canonical_bytes"],
        "omitted_audits": omitted,
    }
    if artifact_reference is not None:
        audit["registry_reference"] = copy.deepcopy(dict(artifact_reference))
    if role_id == "elevation_reader" or (role_id is None and "orientation" in artifact and "plan" not in artifact):
        report = artifact.get("regularization", {})
        changes = report.get("changes", []) if isinstance(report, Mapping) else []
        fields = ("item_kind", "item_id", "field", "original_m", "adopted_m")
        audit["adoption_examples"] = [
            {field: copy.deepcopy(row[field]) for field in fields if field in row}
            for row in changes[:12] if isinstance(row, Mapping)
        ]
        audit["adoption_examples_remaining"] = max(0, len(changes) - 12)
    projected["context_audit"] = audit
    return projected
