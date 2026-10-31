"""Build one atomic claim entry for a role-reader elevation batch.

The ordinary ``claim_transaction`` contract deliberately remains unchanged.
This adapter folds the already validated per-opening entries produced by the
role runtime into one compound claim and one operation list.  Consequently the
existing transaction performs one in-memory revision and creates at most one
candidate for the complete facade batch.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any


_BASIS = {
    "annotation_and_pixels",
    "pixels",
    "visual_estimate",
    "inference",
    "declared",
}
_EVIDENCE_TYPES = {
    "annotation",
    "pixels",
    "annotation_and_pixels",
    "visual_estimate",
    "assumption",
    "declared",
}


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return dict(value)


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{path} must be an array")
    return list(value)


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    return value


def _aggregate_basis(bases: list[str]) -> str:
    """Never promote a weaker or mixed reading into stronger evidence."""

    if len(set(bases)) == 1:
        return bases[0]
    # Purely observational mixtures can use their weakest member without
    # promoting any opening. Declared and inferred values are not observations,
    # so a mixture containing either uses the conservative inference carrier.
    if any(basis in {"inference", "declared"} for basis in bases):
        return "inference"
    observational = ["visual_estimate", "pixels", "annotation_and_pixels"]
    return min(bases, key=observational.index)


def build_role_height_batch_entry(
    entries: Sequence[Mapping[str, Any]],
    *,
    evidence_types: Sequence[str],
) -> dict[str, Any]:
    """Fold validated one-opening apply entries into one atomic apply entry.

    The compound claim keeps one value and explicit ``value_targets`` mapping
    per opening.  Sources remain separate boxes in the same order; the persisted
    reason records the exact value/object/source-index/evidence mapping so a
    later audit never has to infer which union crop supported which opening.
    """

    rows = _sequence(entries, "entries")
    kinds = _sequence(evidence_types, "evidence_types")
    if not rows:
        raise ValueError("entries must contain at least one height application")
    if len(rows) != len(kinds):
        raise ValueError("evidence_types must contain one item per height entry")

    objects: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    targets: dict[str, list[dict[str, str]]] = {}
    operations: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    unresolved: list[str] = []
    bases: list[str] = []
    observation_modes: list[str] = []
    seen_objects: set[tuple[str, str]] = set()

    for index, (raw_entry, evidence_type) in enumerate(zip(rows, kinds, strict=True)):
        path = f"entries[{index}]"
        entry = _mapping(raw_entry, path)
        if set(entry) not in (
            {"claim", "action", "reason", "operations"},
            {"claim", "action", "reason", "operations", "reader_evidence"},
        ):
            raise ValueError(f"{path} must be one complete per-opening apply entry")
        if entry["action"] != "apply":
            raise ValueError(f"{path}.action must be 'apply'")
        if evidence_type not in _EVIDENCE_TYPES:
            raise ValueError(
                f"evidence_types[{index}] must be one of {sorted(_EVIDENCE_TYPES)}"
            )

        claim = _mapping(entry["claim"], f"{path}.claim")
        claim_objects = _sequence(claim.get("objects"), f"{path}.claim.objects")
        claim_sources = _sequence(claim.get("sources"), f"{path}.claim.sources")
        claim_values = _mapping(claim.get("values"), f"{path}.claim.values")
        claim_operations = _sequence(entry["operations"], f"{path}.operations")
        if len(claim_objects) != 1 or len(claim_sources) != 1 or len(claim_operations) != 1:
            raise ValueError(
                f"{path} must contain exactly one object, one source box and one operation"
            )
        if set(claim_values) != {"height"}:
            raise ValueError(f"{path}.claim.values must contain only 'height'")

        object_ref = _mapping(claim_objects[0], f"{path}.claim.objects[0]")
        if set(object_ref) != {"kind", "id"} or object_ref["kind"] not in {"window", "opening"}:
            raise ValueError(f"{path}.claim.objects[0] must be a window/opening kind and id")
        identity = (object_ref["kind"], _nonempty_string(object_ref["id"], f"{path}.claim.objects[0].id"))
        if identity in seen_objects:
            raise ValueError(f"duplicate height target {identity!r}")
        seen_objects.add(identity)

        source = _mapping(claim_sources[0], f"{path}.claim.sources[0]")
        if set(source) != {"image", "box"}:
            raise ValueError(f"{path}.claim.sources[0] must contain its image and exact bbox")
        _nonempty_string(source["image"], f"{path}.claim.sources[0].image")
        box = _sequence(source["box"], f"{path}.claim.sources[0].box")
        if len(box) != 4:
            raise ValueError(f"{path}.claim.sources[0].box must have four coordinates")

        basis = claim.get("basis")
        if basis not in _BASIS:
            raise ValueError(f"{path}.claim.basis must be a supported claim basis")
        mode = claim.get("observation_mode", "direct")
        if mode not in {"direct", "candidate_review"}:
            raise ValueError(f"{path}.claim.observation_mode is invalid")
        reason = _nonempty_string(entry["reason"], f"{path}.reason")

        operation = _mapping(claim_operations[0], f"{path}.operations[0]")
        expected_op = "update_window" if identity[0] == "window" else "update_opening"
        if operation.get("op") != expected_op or operation.get("id") != identity[1]:
            raise ValueError(f"{path}.operations[0] does not target its declared object")
        changes = _mapping(operation.get("changes"), f"{path}.operations[0].changes")
        if changes != {"z": {"claim": "$claim", "value": "height"}}:
            raise ValueError(f"{path}.operations[0] must bind z to $claim height")

        value_name = f"height_{index + 1:04d}"
        source_index = len(sources)
        objects.append(copy.deepcopy(object_ref))
        sources.append(copy.deepcopy(source))
        values[value_name] = copy.deepcopy(claim_values["height"])
        targets[value_name] = [copy.deepcopy(object_ref)]
        operations.append(
            {
                **copy.deepcopy(operation),
                "changes": {"z": {"claim": "$claim", "value": value_name}},
            }
        )
        trace_row = {
            "value": value_name,
            "object": copy.deepcopy(object_ref),
            "source_index": source_index,
            "source": copy.deepcopy(source),
            "evidence_type": evidence_type,
            "basis": basis,
            "observation_mode": mode,
            "reason": reason,
        }
        if "reader_evidence" in entry:
            reference = _mapping(entry["reader_evidence"], f"{path}.reader_evidence")
            required = {"evidence_id", "task_id", "artifact_sha256", "artifact_opening_id"}
            if set(reference) != required:
                raise ValueError(
                    f"{path}.reader_evidence must contain exactly {sorted(required)}"
                )
            for key in required:
                _nonempty_string(reference[key], f"{path}.reader_evidence.{key}")
            trace_row["reader_evidence"] = copy.deepcopy(reference)
        trace.append(trace_row)
        for item in _sequence(claim.get("unresolved", []), f"{path}.claim.unresolved"):
            unresolved.append(f"{identity[0]}:{identity[1]}: {_nonempty_string(item, f'{path}.claim.unresolved')}")
        bases.append(basis)
        observation_modes.append(mode)

    candidate = _nonempty_string(
        _mapping(rows[0], "entries[0]")["claim"].get("candidate"),
        "entries[0].claim.candidate",
    )
    for index, raw_entry in enumerate(rows[1:], 1):
        other = _mapping(raw_entry, f"entries[{index}]")["claim"].get("candidate")
        if other != candidate:
            raise ValueError("all height entries must target the same candidate")

    carrier_basis = _aggregate_basis(bases)
    if carrier_basis == "inference" and any(basis != "inference" for basis in bases):
        unresolved.append(
            "Compound carrier uses inference because per-opening evidence bases differ; "
            "see the persisted per_opening mapping for each original basis."
        )
    batch_reason = "Atomic role elevation height batch: " + json.dumps(
        {"per_opening": trace},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    claim = {
        "candidate": candidate,
        "objects": objects,
        "basis": carrier_basis,
        "reason": batch_reason,
        "sources": sources,
        "values": values,
        "value_targets": targets,
        "observation_mode": (
            "candidate_review"
            if carrier_basis in {"inference", "declared"}
            or "candidate_review" in observation_modes
            else "direct"
        ),
        "unresolved": unresolved,
    }
    entry = {
        "claim": claim,
        "action": "apply",
        "reason": batch_reason,
        "operations": operations,
    }
    return {
        "entry": entry,
        "entries": [entry],
        "entries_json": json.dumps([entry], ensure_ascii=False, separators=(",", ":")),
        "per_opening": trace,
    }


__all__ = ["build_role_height_batch_entry"]
