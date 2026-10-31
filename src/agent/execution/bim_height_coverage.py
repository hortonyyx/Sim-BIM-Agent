"""Report which actual opening heights retain drawing-backed claim bindings.

This module only projects existing claims onto the immutable source opening
inventory.  It does not inspect images, change geometry, or infer that an empty
facade (or any other scope) agrees with a drawing.
"""
from __future__ import annotations

import copy
import json
import math

from src.agent.execution.bim_claim_state import project
from src.agent.geometry.opening_review import facade_inventory


_IMAGE_BASES = frozenset({"annotation_and_pixels", "pixels", "visual_estimate"})
_ROLE_BATCH_PREFIX = "Atomic role elevation height batch: "


def _role_height_trace(record):
    reason = record.get("claim", {}).get("reason")
    if not isinstance(reason, str) or not reason.startswith(_ROLE_BATCH_PREFIX):
        return []
    try:
        payload = json.loads(reason[len(_ROLE_BATCH_PREFIX):])
        rows = payload["per_opening"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def _absolute_z(opening: dict) -> list[float]:
    try:
        values = sorted({float(vertex[2]) for vertex in opening["vertices"]})
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"source opening {opening.get('id')} has invalid absolute z vertices") from exc
    if len(values) < 2 or not all(math.isfinite(value) for value in values):
        raise ValueError(f"source opening {opening.get('id')} has invalid absolute z vertices")
    return values


def _scope(opening_ids: list[str], rows: dict[str, dict]) -> dict:
    ordered = [rows[identity] for identity in sorted(opening_ids)]
    image_linked = [row["opening_id"] for row in ordered
                    if row["coverage_state"] == "image_evidence_linked"]
    non_image = [row["opening_id"] for row in ordered
                 if row["coverage_state"] == "non_image_evidence_only"]
    unchecked = [row["opening_id"] for row in ordered if not row["image_height_evidence_linked"]]
    return {
        "actual_opening_ids": [row["opening_id"] for row in ordered],
        "image_evidence_linked_opening_ids": image_linked,
        "non_image_evidence_only_opening_ids": non_image,
        "unchecked_height_opening_ids": unchecked,
        "height_coverage": ("no_built_openings" if not ordered else
                            "all_image_evidence_linked" if not unchecked else
                            "partial_image_evidence_linked" if image_linked else
                            "no_image_evidence_linked"),
        "drawing_fidelity": "not_evaluated",
    }


def compact_height_coverage(report: dict) -> dict:
    """Remove detailed binding evidence while retaining actionable scopes."""
    compact = {
        key: copy.deepcopy(report[key]) for key in (
            "schema_version", "candidate", "source_model_sha256", "summary",
            "drawing_fidelity", "delivery_blocked", "note",
        ) if key in report
    }
    compact["openings"] = [{
        key: copy.deepcopy(row[key]) for key in (
            "opening_id", "kind", "floor_ids", "facade", "absolute_z_m",
            "coverage_state", "image_height_evidence_linked",
        ) if key in row
    } for row in report.get("openings", [])]
    compact["floors"] = copy.deepcopy(report.get("floors", []))
    for floor in compact["floors"]:
        for facade in floor.get("facades", []):
            facade.pop("exterior_boundary_ids", None)
    return compact


def height_coverage(store, candidate, current_state=None):
    """Return floor/facade coverage of retained absolute-height bindings.

    A height is image-evidence-linked only when an adopted claim still has a
    retained ``z`` binding to that exact opening and the claim has a located
    image basis.
    Retained inference/declared bindings remain visible as useful provenance,
    but deliberately leave the opening in the unchecked drawing-height scope.
    """
    proposal, source = store.candidate(candidate)
    state = current_state if current_state is not None else project(store, candidate)
    if not isinstance(state, dict) or state.get("candidate") != candidate:
        raise ValueError("current_state must be the projected state for candidate")

    facade_index = facade_inventory(source)
    openings = {}
    for opening in source.get("openings", []):
        identity = opening.get("id") if isinstance(opening, dict) else None
        if not isinstance(identity, str) or not identity:
            raise ValueError("source opening requires a nonempty id")
        if identity in openings:
            raise ValueError(f"duplicate source opening id: {identity}")
        openings[identity] = opening

    target_kinds = {}
    for kind in ("window", "opening"):
        for row in proposal.get("geometry", {}).get(kind + "s", []):
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                target_kinds[row["id"]] = kind

    claim_records = {row["id"]: row for row in store.status()["claims"]}
    from src.agent.runtime_roles.height_evidence import (
        delivered_reader_height_index,
        verified_reader_height_reference,
    )
    delivered_reader_index = delivered_reader_height_index(store.run, source, candidate)
    links = {identity: [] for identity in openings}
    for claim_state in state.get("claims", []):
        decision = claim_state.get("decision") or {}
        if decision.get("disposition") != "adopted":
            continue
        record = claim_records.get(claim_state.get("id"))
        if record is None:
            continue
        claim = record["claim"]
        basis = claim["basis"]
        trace = _role_height_trace(record)
        for binding in claim_state.get("retained_bindings", []):
            if binding.get("parameter") != "z":
                continue
            for target in binding.get("result_targets", binding.get("targets", [])):
                if not isinstance(target, (list, tuple)) or len(target) != 2:
                    continue
                kind, identity = target
                opening = openings.get(identity)
                if opening is None or target_kinds.get(identity) != kind:
                    continue
                trace_rows = [row for row in trace if isinstance(row, dict)
                              and row.get("value") == binding.get("value")
                              and row.get("object") == {"kind": kind, "id": identity}]
                trace_row = trace_rows[0] if len(trace_rows) == 1 else None
                per_object_basis = trace_row.get("basis") if trace_row else basis
                source_ref = trace_row.get("source") if trace_row else None
                value = record.get("resolved_values", {}).get(binding.get("value"))
                verified_reader = None
                if (trace_row is not None and isinstance(source_ref, dict)
                        and isinstance(value, list)):
                    verified_reader = verified_reader_height_reference(
                        store.run, trace_row.get("reader_evidence"),
                        target={"kind": kind, "id": identity}, value=value, source=source_ref,
                    )
                    if verified_reader is None:
                        key = (kind, identity, tuple(value), source_ref.get("image"),
                               tuple(source_ref.get("box", ())))
                        verified_reader = delivered_reader_index.get(key)
                evidence_class = (
                    "image" if verified_reader is not None
                    else "image" if per_object_basis in _IMAGE_BASES and claim.get("sources")
                    else "unlocated_image" if per_object_basis in _IMAGE_BASES
                    else "non_image"
                )
                links[identity].append({
                    "claim_id": claim_state["id"],
                    "binding_record": binding.get("record"),
                    "binding_kind": binding.get("kind"),
                    "value": binding.get("value"),
                    "parameter": "z",
                    "basis": per_object_basis,
                    "evidence_class": evidence_class,
                    "source_images": sorted({row["image"] for row in record.get("sources", [])}),
                    "verified_reader_evidence": verified_reader,
                })

    classifications = facade_index["opening_classifications"]
    opening_rows = {}
    for identity, opening in openings.items():
        classification = classifications[identity]
        retained = sorted(links[identity], key=lambda row: (
            row["claim_id"], row["binding_record"] or "", row["value"] or ""))
        image_evidence = [row for row in retained if row["evidence_class"] == "image"]
        non_image_evidence = [row for row in retained if row["evidence_class"] == "non_image"]
        unlocated_image_evidence = [row for row in retained if row["evidence_class"] == "unlocated_image"]
        if image_evidence:
            coverage_state = "image_evidence_linked"
        elif non_image_evidence:
            coverage_state = "non_image_evidence_only"
        else:
            coverage_state = "unchecked"
        opening_rows[identity] = {
            "opening_id": identity,
            "kind": classification["kind"],
            "floor_ids": classification.get("floor_ids", [classification["floor_id"]]),
            "facade": classification.get("facade"),
            "absolute_z_m": _absolute_z(opening),
            "coverage_state": coverage_state,
            "image_height_evidence_linked": bool(image_evidence),
            "retained_image_evidence": image_evidence,
            "retained_non_image_evidence": non_image_evidence,
            "retained_unlocated_image_evidence": unlocated_image_evidence,
        }

    floors = []
    for floor in facade_index["floors"]:
        facade_rows = []
        facade_openings = set()
        for facade in floor["facades"]:
            facade_openings.update(facade["opening_ids"])
            facade_rows.append({
                "facade": facade["facade"],
                "exterior_boundary_ids": facade["exterior_boundary_ids"],
                **_scope(facade["opening_ids"], opening_rows),
            })
        all_floor_openings = sorted(identity for identity, row in opening_rows.items()
                                    if floor["floor_id"] in row["floor_ids"])
        non_facade_ids = sorted(set(all_floor_openings) - facade_openings)
        floors.append({
            "floor_id": floor["floor_id"],
            "facades": facade_rows,
            "non_facade": _scope(non_facade_ids, opening_rows),
            "all_openings": _scope(all_floor_openings, opening_rows),
            "unsupported_exterior_boundaries": floor["unsupported_exterior_boundaries"],
        })

    ordered_openings = [opening_rows[identity] for identity in sorted(opening_rows)]
    summary = {
        "total_count": len(ordered_openings),
        "image_linked_count": sum(bool(row["retained_image_evidence"]) for row in ordered_openings),
        "non_image_linked_count": sum(bool(row["retained_non_image_evidence"]) for row in ordered_openings),
        "unchecked_height_opening_ids": [row["opening_id"] for row in ordered_openings
                                         if not row["image_height_evidence_linked"]],
    }
    return {
        "schema_version": "opening_height_coverage_v1",
        "candidate": candidate,
        "source_model_sha256": facade_index["source_model_sha256"],
        "summary": summary,
        "openings": ordered_openings,
        "floors": floors,
        "drawing_fidelity": "not_evaluated",
        "delivery_blocked": False,
        "note": ("Coverage means a current adopted z binding, not proof that the drawing or BIM is correct. "
                 "Inference and declared values do not count as located-image height evidence."),
    }
