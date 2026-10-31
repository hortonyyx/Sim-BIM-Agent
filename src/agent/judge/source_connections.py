"""Compare declared connections across source-model revisions with stable IDs.

This checks preserved evidence, not whether an unrecorded doorway exists in a
drawing. Opening IDs and source-boundary IDs must already be aligned; geometric
room matching remains a separate comparison.
"""
from __future__ import annotations

from src.agent.correction.schema import SourceOpening


def compare_connections(reference: dict | None, candidate: dict) -> dict:
    report = {"status": "not_evaluated", "findings": [],
              "not_evaluated": ["unrecorded drawing openings", "door operating state without reference evidence"]}
    if reference is None or (isinstance(reference, dict) and "openings" not in reference):
        report["findings"].append({"code": "connection_reference_unavailable", "severity": "info"})
        return report

    def parse(model, side):
        if not isinstance(model, dict) or not isinstance(model.get("openings"), list):
            raise ValueError(f"{side} requires an explicit openings list")
        result = {}
        for raw in model["openings"]:
            opening = SourceOpening.model_validate(raw)
            if opening.kind == "window":
                continue
            if opening.id in result:
                raise ValueError(f"duplicate {side} opening id: {opening.id}")
            if (len(opening.space_ids) != (1 if opening.exterior else 2)
                    or len(set(opening.space_ids)) != len(opening.space_ids)):
                raise ValueError(f"invalid {side} connected spaces: {opening.id}")
            if opening.kind == "open" and opening.connectivity != "open":
                raise ValueError(f"unfilled {side} opening cannot be closed: {opening.id}")
            result[opening.id] = opening
        return result

    try:
        refs = parse(reference, "reference")
    except (ValueError, TypeError) as exc:
        report["findings"].append({"code": "connection_reference_invalid", "severity": "info", "message": str(exc)})
        return report
    try:
        cands = parse(candidate, "candidate")
    except (ValueError, TypeError) as exc:
        report["status"] = "severe"
        report["findings"].append({"code": "connection_candidate_invalid", "severity": "severe", "message": str(exc)})
        return report
    report["status"] = "pass"

    def fail(code, oid, **evidence):
        report["status"] = "severe"
        report["findings"].append({"code": code, "severity": "severe", "opening_id": oid, **evidence})

    for oid in sorted(refs.keys() - cands.keys()):
        fail("connection_missing", oid, reference_space_ids=refs[oid].space_ids)
    for oid in sorted(cands.keys() - refs.keys()):
        fail("connection_added", oid, candidate_space_ids=cands[oid].space_ids)
    for oid in sorted(refs.keys() & cands.keys()):
        ref, cand = refs[oid], cands[oid]
        if sorted(ref.space_ids) != sorted(cand.space_ids) or ref.exterior != cand.exterior:
            fail("connection_wrong_spaces", oid, reference_space_ids=ref.space_ids,
                 candidate_space_ids=cand.space_ids, reference_exterior=ref.exterior, candidate_exterior=cand.exterior)
        if ref.host_boundary_id != cand.host_boundary_id:
            fail("connection_wrong_host", oid, reference_host=ref.host_boundary_id, candidate_host=cand.host_boundary_id)
        if ref.kind != cand.kind:
            fail("connection_kind_changed", oid, reference_kind=ref.kind, candidate_kind=cand.kind)
        if ref.connectivity != cand.connectivity:
            if ref.connectivity == "unknown":
                report["findings"].append({"code": "connection_state_unverified", "severity": "info", "opening_id": oid})
                if report["status"] == "pass":
                    report["status"] = "not_evaluated"
            else:
                fail("connection_state_changed", oid, reference_state=ref.connectivity, candidate_state=cand.connectivity)
    return report
