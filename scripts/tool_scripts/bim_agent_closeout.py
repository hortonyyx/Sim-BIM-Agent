"""Compact current mesh closeout facts, not an additional acceptance policy.

Geometry findings come from the saved source. Model notes remain unknowns and
the existing mesh review owns evidence freshness; neither certifies fidelity.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re

from src.agent.geometry.source_model import _digest


def _text(value, limit=160):
    return str(value).encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _sample(item):
    if not isinstance(item, dict):
        return {"detail": _text(item)}
    result = {}
    for key in ("id", "space_id", "space_ids", "floor_id", "opening_id",
                "boundary_id", "host_boundary_id", "reason", "code"):
        if key in item:
            value = item[key]
            result[key] = ([_text(v, 64) for v in value[:3]] if isinstance(value, list)
                           else _text(value))
            if len(result) == 3:
                break
    return result or {"detail": _text(item)}


def source_blockers(source):
    """Retain source errors, including validation findings absent from notes."""
    validation = source.get("validation")
    findings = validation.get("findings") if isinstance(validation, dict) else None
    severe = ([row for row in findings if isinstance(row, dict) and row.get("severity") == "severe"]
              if isinstance(findings, list) else [])
    result = {"source_validation": severe}
    for field in ("conflicts", "unbuilt_openings", "unsupported"):
        value = source.get(field, [])
        result[field] = value if isinstance(value, list) else [{"reason": f"malformed {field}"}]
    return result


def closeout_summary(candidate, source, report, *, review=None):
    """Pure projection from supplied saved values; no writes or model inference."""
    source = source if isinstance(source, dict) else {}
    report = report if isinstance(report, dict) else {}
    digest = source.get("source_model_sha256")
    source_current = bool(digest) and digest == _digest(
        {key: value for key, value in source.items() if key != "source_model_sha256"})
    report_current = source_current and report.get("source_model_sha256") == digest
    validation = source.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    findings = validation.get("findings")
    validation_known = (validation.get("status") in {"pass", "warning", "severe"}
                        and isinstance(findings, list)
                        and all(isinstance(row, dict) and row.get("severity") in
                                {"pass", "warning", "severe", "info"} for row in findings))
    blockers = source_blockers(source)
    groups = defaultdict(list)
    for collection, items in blockers.items():
        for item in items:
            code = item.get("code") if isinstance(item, dict) else None
            groups[(collection, str(code or collection))].append(item)
    ordered = sorted(groups.items())
    total = sum(len(items) for items in blockers.values())
    unavailable = []
    if not source_current:
        unavailable.append("source_missing_or_hash_mismatch")
    if not report_current:
        unavailable.append("report_missing_or_source_hash_mismatch")
    if not validation_known:
        unavailable.append("source_validation_unavailable_or_malformed")
    if validation.get("status") == "severe" and not blockers["source_validation"]:
        unavailable.append("severe_validation_without_findings")
    if report_current and report.get("source_geometry_ready") is not True and not total:
        unavailable.append("source_geometry_not_ready")
    machine = {
        "status": "unavailable" if unavailable else "blocked" if total else "clear",
        "source_validation_status": validation.get("status"),
        "finding_count": total,
        "counts": {name: len(items) for name, items in blockers.items()},
        "groups": [{"collection": collection, "code": _text(code, 120), "count": len(items),
                    "examples": [_sample(item) for item in items[:2]]}
                   for (collection, code), items in ordered[:8]],
        "omitted_group_count": max(0, len(ordered) - 8),
        "source_file": f"{candidate}/source_model.json",
        "report_file": f"{candidate}/report.json",
        "meaning": "Saved consistency findings only; groups can describe the same defect. Clear is not architectural acceptance.",
    }
    if unavailable:
        machine["unavailable_reasons"] = unavailable
    generation = source.get("generation")
    generation = generation if isinstance(generation, dict) else {}
    notes = report.get("unresolved", generation.get("unresolved", []))
    notes = notes if isinstance(notes, list) else [notes]
    review = review if isinstance(review, dict) else {}
    current_review = (review.get("candidate") == candidate and review.get("status") == "current"
                      and review.get("complete_building") is True and source_current and report_current)
    review_state = {
        "status": review.get("status", "not_rechecked") if review.get("candidate") in {None, candidate}
                  else "different_candidate",
        "current_whole_building_declaration": current_review,
        "fidelity": "not_evaluated",
        "meaning": "Saved delivery declaration state, not whether the model has already looked at the geometry.",
    }
    for key in ("coverage", "review_file", "record_sha256"):
        if key in review:
            review_state[key] = review[key]
    if review.get("reason"):
        review_state["reason"] = _text(review["reason"], 240)
    if machine["status"] == "unavailable":
        action = "Inspect the current saved source/report to resolve unavailable checks before delivery."
    elif total:
        action = ("Repair the identified objects and affected relationships locally; recheck affected results. "
                  "Do not reopen overall form unless a real defect requires it.")
    elif current_review:
        action = ("If requested scope and necessary functionality are satisfied and no known substantive issue remains, "
                  "use normal finish_bim. Existing pending-claim and delivery checks still apply.")
    else:
        action = ("If current shape and necessary functionality satisfy the request, review current overlay evidence "
                  "as needed and carry the truthful current mesh_review with normal finish_bim. "
                  "No separate registration round is required; do not refine form merely to clear notes.")
    return {
        "schema": "mesh_closeout_v1", "candidate": candidate, "source_model_sha256": digest,
        # The existing computed-constraint projection removes audit hash fields;
        # this explicit version identifier remains visible in current state.
        "source_version": f"sha256:{digest}" if digest else None,
        "machine_check": machine,
        "retained_notes": {
            "status": "reported_unknown_not_accepted", "count": len(notes),
            "examples": [_text(note, 180) for note in notes[:3]],
            "source_file": f"{candidate}/report.json", "json_pointer": "/unresolved",
            "meaning": "Retain and judge against user scope. A note is neither an automatic repair task nor an accepted omission.",
        },
        "mesh_review": review_state, "next_action": action,
        "details_read": {"tool": "inspect_candidate", "arguments": {"candidate": candidate, "include_geometry": False}},
    }


def mesh_closeout(toolkit, candidate, *, source=None, report=None, review=None):
    """Read current mesh closeout; reuse a review just validated by the caller.

    Missing review files need no mesh/image hashing. Existing saved declarations
    are validated through the original checker, never trusted from their text.
    """
    if not toolkit.manifest.get("mesh_input"):
        return None
    if not isinstance(candidate, str) or not re.fullmatch(r"candidate_\d+|seed", candidate):
        raise ValueError("mesh closeout requires an admitted candidate ID")
    run = Path(toolkit.run).resolve()
    folder = run / candidate
    if folder.resolve() != folder:
        raise ValueError("mesh closeout candidate escapes its admitted path")

    def read(name):
        path = folder / name
        if path.resolve() != path:
            raise ValueError("mesh closeout artifact escapes its admitted path")
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return {}

    if source is None:
        source = read("source_model.json")
    if report is None:
        report = read("report.json")
    if review is None:
        if (folder / "mesh_review.json").exists():
            from scripts.tool_scripts.bim_agent_mesh_review import mesh_review_status
            review = mesh_review_status(toolkit, candidate)
        else:
            review = {"candidate": candidate, "status": "missing", "complete_building": False}
    return closeout_summary(candidate, source, report, review=review)
