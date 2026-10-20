"""Q1 rule selection and audit presentation for saved plan operations.

Only the trusted run manifest may select the historical replay policy. A plan
submitted by a model cannot opt out of the rules for a new operation.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math


CURRENT_RULE = "plan_regularization_v1"
LEGACY_RULE = "legacy"
CROSS_STOREY_FAILURE_POLICIES = {"reject_assembly", "skip_failed_pair"}


def selected_rule(manifest):
    rule = manifest.get("plan_regularization_rule", CURRENT_RULE)
    if rule not in {CURRENT_RULE, LEGACY_RULE}:
        raise ValueError(f"unknown plan_regularization_rule: {rule!r}")
    return rule


def classify_source_precision(report, manifest):
    """Keep mesh contact-partition size advisory, without changing source validity.

    A roof can contact several rooms below it in narrow regions even though all
    solids, coverage and hosts are valid. This is not evidence of a thin room.
    Drawing policies and every other precision rule retain their existing scope.
    """
    if not (manifest.get("mesh_input") and manifest.get("architectural_modelling_policy")):
        return report
    warnings = [row for row in report.get("items", [])
                if row.get("type") == "thin_horizontal_contact"]
    if not warnings:
        return report
    result = copy.deepcopy(report)
    result["items"] = [row for row in result["items"]
                       if row.get("type") != "thin_horizontal_contact"]
    result["warnings"] = [{**row, "severity": "warning",
        "original_fix": row.get("fix"),
        "fix": "A narrow horizontal contact partition alone does not require reshaping. "
               "Revise only if evidence shows a real coverage, connection or geometry defect; "
               "source validity checks still apply."} for row in copy.deepcopy(warnings)]
    result["warning_count"] = len(warnings)
    result["warning_counts"] = dict(Counter(row["type"] for row in warnings))
    result["total"] = len(result["items"])
    result["counts"] = dict(Counter(row["type"] for row in result["items"]))
    result["status"] = "rejected" if result["items"] else "reported"
    result["policy_classification"] = {
        "scope": "native mesh with architectural adoption",
        "advisory_types": ["thin_horizontal_contact"],
        "source_validity": "unchanged; precision warnings do not certify source geometry",
    }
    return result


def selected_grid_step(manifest):
    """Only newly admitted drawing runs declare the current whole-building grid."""
    if "lite_bim_grid_step_m" not in manifest:
        return None
    from src.agent.geometry.lite_bim_regularization import DEFAULT_LITE_GRID_M
    step = manifest["lite_bim_grid_step_m"]
    if (isinstance(step, bool) or not isinstance(step, (int, float))
            or step != DEFAULT_LITE_GRID_M):
        raise ValueError("lite_bim_grid_step_m must be the current 0.1 m drawing policy")
    if selected_rule(manifest) == LEGACY_RULE:
        raise ValueError("a Lite BIM run cannot also select legacy regularization")
    return float(step)


def selected_cross_storey_failure_policy(manifest):
    """Return the trusted run-level policy for an unresolvable wall pair."""
    policy = manifest.get(
        "cross_storey_alignment_failure_policy", "reject_assembly")
    if policy not in CROSS_STOREY_FAILURE_POLICIES:
        raise ValueError(
            "unknown cross_storey_alignment_failure_policy: "
            f"{policy!r}")
    return policy


def apply_cross_storey_skip_policy(precision, regularization):
    """Separate source diagnostics already covered by an audited skipped pair.

    The source-level precision gate uses boundary IDs while plan regularization
    uses partition IDs.  Their shared, deterministic identity is the two floor,
    axis, coordinate and overlapping-span records.  Nothing else is waived.
    """
    if (not isinstance(regularization, dict)
            or regularization.get("cross_storey_failure_policy")
            != "skip_failed_pair"):
        return precision
    skipped = regularization.get("skipped_alignments", [])
    if not skipped:
        return precision

    def same_line(first, second):
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        if (first.get("floor_id"), first.get("axis")) != (
                second.get("floor_id"), second.get("axis")):
            return False
        a, b = first.get("coordinate_m"), second.get("coordinate_m")
        if (not isinstance(a, (int, float)) or isinstance(a, bool)
                or not isinstance(b, (int, float)) or isinstance(b, bool)
                or not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-7)):
            return False
        first_span, second_span = first.get("span_m"), second.get("span_m")
        if (not isinstance(first_span, list) or len(first_span) != 2
                or not isinstance(second_span, list) or len(second_span) != 2):
            return False
        return min(max(first_span), max(second_span)) - max(
            min(first_span), min(second_span)) > 1e-7

    def covered(item):
        lines = item.get("lines") or item.get("align_to_options") or []
        if len(lines) != 2:
            return False
        for record in skipped:
            expected = [record.get("source"), record.get("target")]
            if all(any(same_line(row, candidate) for candidate in lines)
                   for row in expected):
                return record
        return False

    result = copy.deepcopy(precision)
    blocking, accepted = [], []
    for item in result.get("items", []):
        record = covered(item)
        if not isinstance(record, dict):
            blocking.append(item)
        else:
            accepted.append({
                **item,
                "disposition": "covered_by_skipped_cross_storey_alignment",
                "regularization_type": record.get("type"),
            })
    if not accepted:
        return precision
    result["items"] = blocking
    result["skipped_items"] = accepted
    result["total"] = len(blocking)
    result["counts"] = dict(Counter(row.get("type") for row in blocking))
    result["status"] = "pass" if not blocking else "rejected"
    result["meaning"] = (
        "Post-regularisation hard audit. Diagnostics matching an explicitly "
        "skipped cross-storey pair remain listed in skipped_items; every other "
        "item still blocks the save."
    )
    return result


def source_grid_report(source, grid_step_m):
    """Check the same Lite grid on actual source geometry, without editing it."""
    from src.agent.geometry.lite_bim_regularization import quantize_m
    coordinates = []
    for floor in source["floors"]:
        coordinates.extend((f"floors:{floor['id']}.{field}", floor[field])
                           for field in ("z_floor", "height"))
        coordinates.extend((f"floors:{floor['id']}.footprint[{i}][{axis}]", value)
                           for i, point in enumerate(floor["footprint"])
                           for axis, value in enumerate(point))
    # Generic proposals also support rectangles and facade/span windows. Source
    # vertices cover those forms as well as the pixel compiler's polygons/p1/p2.
    coordinates.extend((f"{collection}:{item['id']}.vertices[{i}][{axis}]", value)
                       for collection in ("boundaries", "openings")
                       for item in source[collection]
                       for i, point in enumerate(item["vertices"])
                       for axis, value in enumerate(point))
    violations = [{"path": path, "adopted_m": value}
                  for path, value in coordinates
                  if abs(value - quantize_m(value, grid_step_m)) > 1e-7]
    return {"status": "rejected" if violations else "pass", "grid_step_m": grid_step_m,
            "coordinate_count": len(coordinates), "violations": violations}


def reject_source_proposal(toolkit, proposal, *, plan_assembly=None, parent=None):
    """Prepare a source-only hard-rule refusal before allocating a candidate.

    The caller is Toolkit.build. This helper never edits a source or the input
    proposal. Ordinary malformed inputs still use the exporter's existing error
    path; only a successfully constructed source is checked here.
    """
    grid_step = selected_grid_step(toolkit.manifest)
    if selected_rule(toolkit.manifest) == LEGACY_RULE:
        return None
    from src.agent.execution.source_proposal import _validate_proposal
    from src.agent.correction.schema import FootprintRing
    from src.agent.geometry.source_bim import build_source_bim
    from src.agent.geometry.building_precision import precision_report
    from src.agent.geometry.input_scale import check_geometry_scale
    from src.agent.execution.source_proposal import ensure_corrected_geometry

    try:
        geometry, _, _, enclosure = _validate_proposal(proposal)
        geom = ensure_corrected_geometry(copy.deepcopy(geometry))
        for floor in geom.floors:
            footprint = getattr(floor, "footprint", None)
            if isinstance(footprint, dict):
                floor.footprint = FootprintRing.model_validate(footprint)
        check_geometry_scale(geom.model_dump(mode="json"))
        profile = 'simple_polygon' if geom.schema_version == 'source_polygon_v1' else 'orthogonal_polygon'
        source = build_source_bim(geom, capability_profile=profile,
                                 enclosure_declaration=enclosure)
    except (ValueError, TypeError, KeyError):
        return None
    report = precision_report(source)
    if isinstance(plan_assembly, dict):
        report = apply_cross_storey_skip_policy(
            report, plan_assembly.get("regularization"))
    if parent is not None:
        for regularization in candidate_reports(toolkit, parent):
            report = apply_cross_storey_skip_policy(report, regularization)
    report = classify_source_precision(report, toolkit.manifest)
    grid = None
    if grid_step is not None:
        grid = source_grid_report(source, grid_step)
        if grid["violations"]:
            report["items"].append({
                "type": "source_lite_grid_violation", "grid_step_m": grid_step,
                "coordinate_count": len(grid["violations"]),
                "fix": "Revise the saved pixel plan with revise_plan_bim so code reapplies the run grid, "
                       "or use grid-aligned values for direct BIM edits; source vertices are never silently rounded.",
            })
            report.update(status="rejected", total=len(report["items"]),
                          counts=dict(Counter(row["type"] for row in report["items"])))
    if not report["items"]:
        return None
    folder = toolkit.run / "geometry_rejections"
    folder.mkdir(exist_ok=True)
    attempt = folder / f"attempt_{len(list(folder.glob('attempt_*'))) + 1:03d}"
    attempt.mkdir(exist_ok=False)
    full = {"rule_version": CURRENT_RULE, "status": "rejected", "changes": [],
            "rejections": report["items"], "hard_constraints": report}
    if grid is not None:
        full["compiled_grid"] = grid
    reference = save_report(attempt, full, toolkit.run)
    (attempt / "proposal.json").write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"status": "error", "source_geometry_ready": False,
            "error_stage": "source_hard_constraints",
            "error": ("Source geometry violates the run's 0.1 m Lite BIM grid; repair the listed coordinates before saving."
                      if grid is not None and grid["violations"] else
                      "Source geometry violates the 0.30 m alignment or 0.60 m room-width rules; repair the listed objects before saving."),
            "regularization": reference, "remaining_seconds": toolkit.remaining_seconds()}


def report_sections(report):
    yield report
    for floor_id, floor_report in report.get("floor_reports", {}).items():
        for section in report_sections(floor_report):
            # Assembly may namespace/rename a floor from its original draft.
            yield {**section, "floor_id": floor_id,
                   "changes": [{**row, "floor_id": floor_id}
                               for row in section.get("changes", [])]}


def summary(report):
    sections = list(report_sections(report))
    changes = [row for section in sections for row in section.get("changes", [])]
    skipped = [
        row for section in sections
        for row in section.get("skipped_alignments", [])
    ]
    rejected = [row for section in sections for row in section.get("rejections", [])
                if row.get("type") != "floor_regularization_rejected"]
    attempted = [row for section in sections for row in section.get("attempted_changes", [])]
    if report.get("status") in {"rejected", "error"}:
        # A floor may have succeeded inside a rejected assembly. None of its
        # edits are delivered by that rejected top-level operation.
        attempted.extend(changes)
        changes = []
    distances = []
    for row in changes:
        for key in ("movement_m", "distance_m", "displacement_m", "delta_m"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                distances.append(abs(value))
    grid_steps = [section.get("grid_step_m") or section.get("method", {}).get("grid_step_m")
                  or section.get("compiled_grid", {}).get("grid_step_m")
                  or section.get("lite_bim", {}).get("grid_step_m") for section in sections]
    return {
        "rule_version": report.get("rule_version", CURRENT_RULE),
        **({"axis_rule_applicability": report["hard_constraints"]["axis_rule_applicability"]}
           if report.get("hard_constraints", {}).get("axis_rule_applicability") else {}),
        "grid_step_m": next((step for step in grid_steps if step is not None), None),
        "status": report.get("status", "rejected" if rejected else "passed"),
        "moved_count": len(changes),
        "removed_strip_seed_count": sum(len(row.get("removed_space_seeds", [])) for row in changes),
        "merged_opening_count": sum(len(row.get("removed_opening_ids", [])) for row in changes),
        "attempted_change_count": len(attempted),
        "max_movement_m": max(distances, default=0.0),
        "rejected_count": len(rejected),
        "skipped_alignment_count": len(skipped),
        "cross_storey_failure_policy": report.get(
            "cross_storey_failure_policy", "reject_assembly"),
        "junction_preparation": [
            {"floor_id": section.get("floor_id"),
             **{key: preparation.get(key) for key in (
                 "status", "tolerance_m", "tolerance_pixels", "pixel_decimals",
                 "compile_error", "rejection")},
             "applied_change_count": len(preparation.get("changes", [])),
             "attempted_change_count": len(preparation.get("attempted_changes", []))}
            for section in sections
            if isinstance(preparation := section.get("junction_preparation"), dict)],
        "rejections": [
            {**{key: value for key, value in row.items()
                if key not in {"report", "before", "after", "hard_constraints", "attempted_changes",
                               "before_relationships", "after_relationships", "changed_legitimate_separations"}},
             **({"protected_separation_conflicts": len(row["changed_legitimate_separations"])}
                if row.get("changed_legitimate_separations") else {})}
            for row in rejected],
        "skipped_alignments": [
            {key: value for key, value in row.items()
             if key not in {
                 "attempted_changes", "endpoint_rejections",
                 "before_relationships", "after_relationships",
                 "hard_violations", "hard_violation",
             }}
            for row in skipped],
    }


def save_report(folder, report, run):
    path = folder / "regularization.json"
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(payload)
    result = {"file": path.relative_to(run).as_posix(),
              "sha256": hashlib.sha256(payload).hexdigest(), **summary(report)}
    if folder.parent == run / "plan_drafts":
        # Failed raw drafts need not contain an adopted regularization field.
        # The existing report reader verifies the sidecar's saved binding.
        result["read"] = {"tool": "inspect_plan_draft", "arguments": {
            "draft_id": folder.name, "field": "report:/regularization"}}
    else:
        # Assembly outcomes and source-level refusals have no draft reader.
        # Every such report uses the same existing immutable report contract.
        from scripts.tool_scripts.bim_agent_replies import compact_reply, report_reference
        saved = compact_reply(run, "regularization_report", report, force=True)
        reference = report_reference(saved['details_file'], saved['details_sha256'], '', report)
        result.update({key: reference[key] for key in ('report_file', 'report_sha256', 'read')})
    return result


def compact_plan_input(record):
    compact = copy.deepcopy(record)
    if isinstance(compact.get("regularization"), dict):
        compact["regularization"] = summary(compact["regularization"])
    return compact


def source_reports(source):
    provenance = source.get("generation", {}).get("provenance", {})
    reports = []
    for name in ("plan_input", "plan_assembly"):
        report = provenance.get(name, {}).get("regularization")
        if isinstance(report, dict):
            reports.append(report)
    return reports


def candidate_reports(toolkit, candidate, source=None):
    """Read the first regularisation audit available along a candidate chain."""
    current = candidate
    seen = set()
    while current and current not in seen:
        seen.add(current)
        path = toolkit.candidate_path(current)
        current_source = source if current == candidate and source is not None else json.loads(
            (path / "source_model.json").read_text(encoding="utf-8"))
        reports = source_reports(current_source)
        if reports:
            return reports
        sidecar = path / "regularization_report.json"
        if sidecar.is_file():
            inherited = json.loads(sidecar.read_text(encoding="utf-8"))
            reports = inherited.get("reports", [])
            if reports:
                return reports
        current = current_source.get(
            "generation", {}).get("provenance", {}).get("parent_candidate")
        source = None
    return []


def collect_candidate_report(toolkit, candidate, source):
    """Keep the audit through later height/use edits without changing source BIM."""
    reports = candidate_reports(toolkit, candidate, source)
    if not reports:
        return None
    result = {"rule_version": CURRENT_RULE, "reports": reports,
              "source_model_sha256": source.get("source_model_sha256"),
              "meaning": "Recorded geometric regularisation; original drawing fidelity remains unevaluated."}
    path = toolkit.candidate_path(candidate) / "regularization_report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result
