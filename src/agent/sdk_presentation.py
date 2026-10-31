"""Display-only projections; full tool data, receipts and images remain authoritative."""
from __future__ import annotations

import copy
from pathlib import Path


def _receipt_pointer(pointer):
    return {"full_receipt_pointer": "/result/structuredContent" + pointer}


def _display_domain_details(value, pointer=""):
    """Fold known audit bookkeeping, never rejection or diagnostic evidence.

    The normal domain tool has already chosen its checks and projections. This
    only reduces the printed representation; it does not read or write reports.
    """
    if isinstance(value, list):
        return [_display_domain_details(row, f"{pointer}/{i}") for i, row in enumerate(value)]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        at = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
        if (value.get("schema_version") == "architectural_regularization_v1"
                and key in {"changes", "attempted_changes"} and isinstance(item, list) and item):
            result[key] = {"count": len(item), **_receipt_pointer(at)}
        elif key in {"rejections", "diagnostics", "warnings", "errors", "error_details", "blockers"}:
            # Keep all decisive locations, worst vertices, supports and limits,
            # including the last finding; there is no output character budget.
            result[key] = item
        elif key == "read" and isinstance(item, dict) and set(item) == {"tool", "arguments"} and item.get("tool") == "read_candidate_items" and (
                item.get("arguments") == {"candidate": "", "collection": "report",
                    "report_file": value.get("report_file"), "json_pointer": value.get("json_pointer")}
                and {"report_file", "report_sha256", "json_pointer"} <= value.keys()):
            # Exact redundant reader instructions; retain the path/hash/pointer.
            continue
        else:
            result[key] = _display_domain_details(item, at)
    return result


def _display_inputs(data, run):
    """Keep input navigation and scope; move copied declarations to their file."""
    result = copy.deepcopy(data)
    implementation = result.get("implementation_sha256")
    if isinstance(implementation, dict):
        result["implementation_sha256"] = {
            "file_count": len(implementation), **_receipt_pointer("/implementation_sha256")}
    images = result.get("images", {})
    for name, metadata in (images.items() if isinstance(images, dict) else ()):
        if isinstance(metadata, dict):
            metadata["path"] = str(run / "images" / name)
    for key in ("mesh_input", "building_input", "plan_recovery"):
        item = result.get(key)
        if isinstance(item, dict) and isinstance(item.get("frozen_path"), str):
            item["read_path"] = str(run / item["frozen_path"])
    building = result.get("building_input")
    if isinstance(building, dict) and isinstance(building.get("frozen_path"), str) and "declaration" in building:
        building["declaration"] = {"read_path": building["read_path"],
            **_receipt_pointer("/building_input/declaration")}
    return result


_BUILD_TOOLS = frozenset({"build_bim", "build_plan_bim", "build_parametric_bim",
                          "patch_parametric_bim", "revise_bim", "revise_plan_bim",
                          "assemble_plan_bim"})


def _has_diagnostic(value):
    """Never fold a known bookkeeping field that now carries diagnostic data.

    This is deliberately conservative presentation, not a quality predicate.
    Unknown fields outside the explicitly named bookkeeping fields stay intact.
    """
    if isinstance(value, list):
        return any(_has_diagnostic(row) for row in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if key in {"schema", "schema_version"} and item not in (1, "1", "architectural_regularization_v1"):
            return True
        if item and any(word in key.lower() for word in (
                "error", "warn", "reject", "finding", "issue", "unknown", "unresolved",
                "unsupported", "unassess", "not_assess", "block", "diagnostic", "limit")):
            return True
        if key == "status" and (not isinstance(item, str) or item not in {"pass", "current", "adopted", "completed"}):
            return True
        if _has_diagnostic(item):
            return True
    return False


def _fold_fields(value, fields, pointer):
    if not isinstance(value, dict) or "details" in value:
        return value
    result = dict(value)
    folded = [key for key in fields if key in result and not _has_diagnostic(result[key])]
    for key in folded:
        result.pop(key)
    if folded:
        # One pointer covers the record, including every omitted exact value.
        result["details"] = _receipt_pointer(pointer)
    return result


def _regularization_identity(value, pointer):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return value
    return _fold_fields(value, ("policy", "modelling", "original_sha256", "adopted_sha256",
        "proposal_sha256", "adopted", "original", "effective", "audit"), pointer)


def _display_saved_candidate(data):
    """Fold only known repetition; all check sections/new fields remain visible."""
    result = _display_domain_details(data)
    if "architectural_regularization" in result:
        result["architectural_regularization"] = _regularization_identity(
            result["architectural_regularization"], "/architectural_regularization")
    provenance = result.get("provenance")
    if isinstance(provenance, dict):
        if "architectural_regularization" in provenance:
            provenance["architectural_regularization"] = _regularization_identity(
                provenance["architectural_regularization"], "/provenance/architectural_regularization")
        result["provenance"] = _fold_fields(provenance,
            ("input_manifest_sha256", "mode", "generator"), "/provenance")
    parametric = result.get("parametric")
    if isinstance(parametric, dict) and parametric.get("schema_version") == 1:
        if "architectural_regularization" in parametric:
            parametric["architectural_regularization"] = _regularization_identity(
                parametric["architectural_regularization"], "/parametric/architectural_regularization")
        result["parametric"] = _fold_fields(parametric,
            ("draft_sha256", "plan_sha256", "proposal_sha256", "source_file_sha256", "base", "patch"), "/parametric")
    for key in ("timings", "build_timings"):
        if isinstance(result.get(key), dict) and all(
                name.endswith("_seconds") and isinstance(value, (int, float))
                for name, value in result[key].items()):
            result[key] = _receipt_pointer("/" + key)
    route = result.get("edit_route")
    if isinstance(route, dict):
        result["edit_route"] = _fold_fields(route,
            ("generic_contract", "patch_contract"), "/edit_route")
    views = result.get("source_plan_views")
    if isinstance(views, list):
        result["source_plan_views"] = [_fold_fields(row,
            ("space_names", "space_ids", "opening_ids", "naming_scheme"),
            f"/source_plan_views/{i}") for i, row in enumerate(views)]
    selection = result.get("source_plan_selection")
    if isinstance(selection, dict) and selection.get("policy") == "changed_geometry_and_equivalent_diagnostic_representatives":
        result["source_plan_selection"] = _fold_fields(selection,
            ("policy", "read_any_floor"), "/source_plan_selection")
    _shorten_known_context(result)
    _deduplicate_identity(result, data)
    facade = result.get("facade_counts")
    if isinstance(facade, dict) and facade.get("schema_version") == "facade_count_report_v1":
        for scope in facade.get("unsupported_exterior_boundaries", []):
            if isinstance(scope, dict) and isinstance(scope.get("boundaries"), list):
                scope["boundaries"] = _common_rows(scope["boundaries"])
    return result


def _common_rows(rows):
    """Lossless common-field factoring, with no omitted row or sampled location."""
    if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
        return rows
    common = {key: value for key, value in rows[0].items()
              if all(key in row and row[key] == value for row in rows[1:])}
    if not common:
        return rows
    return {"common": common,
            "rows": [{key: value for key, value in row.items() if key not in common} for row in rows]}


# Exact existing explanatory text only. A future extension or caller-authored
# diagnostic in these fields stays verbatim rather than being classified away.
_CONTEXT = {
    "input_view_status/note": (
        "Only this run's hash-bound direct original returns from view_image or paired elevation views are counted. Other image tools, workers and prior runs are outside this scope. A returned image is not proof of examination, complete coverage, correct interpretation or applied heights. Use supplied relevant elevations to check each floor's opening heights; do not transfer a typical height to an unexamined opening family.",
        "Returned views are not proof of inspection; other tools are outside this log."),
    "room_use_review/interpretation": (
        "Counts cover structured role_evidence only, not semantic correctness. Legacy source_refs may contain other evidence. Unknown remains a fallback when no defensible listed use fits; it is not a preferred response to ambiguity. No-issue status does not establish that roles are correct.",
        "Structured evidence counts only; not role correctness."),
    "room_use_review/use_decision/meaning": (
        "Only explicit unknown roles or inconsistent role declarations identify a use question. Missing structured evidence alone does not identify wrong use or require an edit.",
        "Missing evidence alone does not require an edit."),
    "height_coverage/note": (
        "Current values, evidence binding and source location, not drawing correctness. A region containing several openings needs individual evidence. Report only.",
        "Evidence/location only; not drawing correctness."),
    "facade_counts/note": (
        "Compare observer-reported whole-floor/facade totals with actual source openings. Uncounted facades, zero totals, conflicting evidence and unresolved scopes stay explicit. Equal counts do not prove position or drawing fidelity. Report only.",
        "Counts alone do not prove positions or fidelity."),
    "source_plan_selection/meaning": (
        "Only automatic plan feedback is selected; geometry checks and source are complete. Changed floors describe plan-visible geometry, not every 3D boundary attribute. Role/evidence/note-only edits are textual. Unshown floor plans remain available on demand; representative geometry is not original-input fidelity approval.",
        "Plan selection only; no input fidelity approval."),
    "building_precision/axis_rule_applicability/basis": (
        "Plan strip/width rules require complete axis-orthogonal footprints; inferred schema labels cannot opt out. Nonorthogonal-space boundaries and contacts are excluded from axis matching; their minimum room width is not evaluated. Vertical gaps, source coverage, true spatial overlaps and opening hosts retain their independent checks.",
        "Nonorthogonal plan/width rules not assessed; independent source/overlap/host checks retained."),
}


def _shorten_known_context(result):
    for path, (original, brief) in _CONTEXT.items():
        parts = path.split("/")
        record = result
        for key in parts[:-1]:
            record = record.get(key) if isinstance(record, dict) else None
        if isinstance(record, dict) and record.get(parts[-1]) == original:
            pointer = _receipt_pointer("/" + "/".join(parts[:-1]))
            if "details" in record and record["details"] != pointer:
                continue
            record[parts[-1]] = brief
            record["details"] = pointer


def _deduplicate_identity(result, root):
    """Known report roots only; never edit findings, new fields or tool arguments."""
    schemas = {"source_geometry_self_consistency": None, "source_validation": None,
        "room_use_review": None, "height_coverage": "opening_heights_v2",
        "facade_counts": "facade_count_report_v1", "building_precision": "building_precision_v2",
        "source_plan_selection": None}
    records = [result[key] for key, schema in schemas.items()
        if isinstance(result.get(key), dict)
        and result[key].get("schema_version", result[key].get("schema")) == schema]
    if isinstance(result.get("source_plan_views"), list):
        records.extend(result["source_plan_views"])
    for record in records:
        if isinstance(record, dict):
            for key in ("candidate", "source_model_sha256"):
                if key in record and key in root and record[key] == root[key]:
                    del record[key]


def _display_overlay(data):
    result = copy.deepcopy(data)
    for key, coordinate_key in (("projected_spaces", "pixel_polygon"),
                                 ("projected_openings", "pixel_endpoints")):
        rows = result.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        normal_keys = {"id", coordinate_key, "out_of_image"}
        if key == "projected_openings":
            normal_keys |= {"kind", "space_ids"}
        # Preserve every exceptional row with full location, including new fields.
        exceptions = [row for row in rows if not isinstance(row, dict)
                      or row.get("out_of_image") is not False
                      or set(row) - normal_keys]
        result[key] = {"count": len(rows), "exceptions": exceptions,
                       **_receipt_pointer("/" + key)}
    return result


def _display_data(data, tool_name, receipt_file, *, is_error=False):
    if not isinstance(data, dict):
        return copy.deepcopy(data)
    if tool_name == "inputs":
        return _display_inputs(data, Path(receipt_file).parent.parent.parent)
    if not is_error and tool_name == "overlay_candidate" and data.get("mode") == "source_image_overlay":
        return _display_overlay(data)
    if (not is_error and tool_name in _BUILD_TOOLS and isinstance(data.get("candidate"), str)
            and data.get("mode") == "agent_geometry_proposal"
            and data.get("source_geometry_ready") is True
            and isinstance(data.get("source_validation"), dict)
            and data["source_validation"].get("status") == "pass"):
        return _display_saved_candidate(copy.deepcopy(data))
    from scripts.tool_scripts.bim_agent_replies import COMPACT_TOOLS

    if tool_name not in COMPACT_TOOLS:
        return copy.deepcopy(data)
    # These tools already apply the domain projection before the SDK sees them.
    # Reapplying it to bounded objects would erase their different schema. Keep
    # that projection, with only the known audit/reader presentation folded.
    return _display_domain_details(copy.deepcopy(data))


