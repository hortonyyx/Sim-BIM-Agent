"""One saved-BIM result contract, including reads of pre-contract run archives.

``saved_candidate`` is the final usable source, never a failed attempt or the
transaction's starting candidate. Effects distinguish durable audit writes from
geometry application and retain every committed candidate of a partial batch.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


SAVE_TOOLS = frozenset({"build_bim", "build_plan_bim", "assemble_plan_bim",
    "build_parametric_bim", "revise_bim", "revise_plan_bim", "finish_bim", "claim_transaction"})


def opening_delivery_summary(toolkit, source):
    """Use the same current/stale review projection for inspection and handoff."""
    from src.agent.geometry.bim_delivery import summarize_delivery

    reviews = [json.loads(p.read_bytes()) for p in
               sorted((toolkit.run / "opening_reviews").glob("review_*.json"))]
    calibrations = {(row["image"], row["floor_id"]): row
                    for _, row in toolkit.registered_calibrations()}
    for review in reviews:
        image = review.get("image", {})
        try:
            toolkit.image_path(image.get("name"))
            if image.get("sha256") != toolkit.manifest["images"][image["name"]]["sha256"]:
                raise ValueError("review original image changed")
        except (OSError, ValueError, KeyError, TypeError):
            review["stale_reason"] = "review_original_image_changed"
        location = review.get("location_check", {})
        if location.get("status") != "checked_against_supplied_plan_boxes":
            continue
        current = calibrations.get((review.get("image", {}).get("name"),
                                    review.get("review_scope", {}).get("floor_id")), {})
        if (any(location.get(key) != current.get(key) for key in ("x_anchors", "y_anchors"))
                or review.get("image", {}).get("sha256") != current.get("image_sha256")):
            review["stale_reason"] = "plan_calibration_changed"
    return summarize_delivery(source, reviews)


def candidate_review(toolkit, candidate, source, report):
    """Read current evidence on demand, without selecting or changing a candidate.

    This reuses existing domain projections, not a second acceptance policy.
    Expensive drawing/precision generators are not run. Height location and
    claims share one current claim projection; no historical pass is promoted.
    """
    from scripts.tool_scripts.bim_agent_budget import saved_floor_status
    from scripts.tool_scripts.bim_agent_facade_checks import located_height_report
    from src.agent.execution.bim_claim_state import project
    from src.agent.geometry.source_model import _digest

    source_hash = source.get("source_model_sha256")
    source_current = source_hash == _digest({k: v for k, v in source.items()
                                           if k != "source_model_sha256"})
    result = {"candidate": candidate, "source_model_sha256": source_hash,
              "drawing_fidelity": "not_evaluated", "calibration_independently_verified": False,
              "scope": "Whole-candidate current domain projections and evidence limits, not a delivery gate or drawing verdict. "
                       "Pixel/drawing diagnostics and precision generators are not rerun; this is not all tool history."}

    def collect(name, function):
        try:
            result[name] = function()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            result[name] = {"status": "unavailable", "reason": str(error)}

    result["source"] = {"status": "current" if source_current else "source_hash_mismatch",
        "validation": source.get("validation"),
        "source_geometry_ready": report.get("source_geometry_ready") if
            report.get("source_model_sha256") == source_hash and source_current else None,
        "unbuilt_openings": source.get("unbuilt_openings", []),
        "unsupported": source.get("unsupported", []), "conflicts": source.get("conflicts", [])}
    result["notes"] = {"assumptions": source.get("assumptions", []),
                       "unresolved": source.get("generation", {}).get("unresolved", [])}
    if not source_current:
        result["evidence"] = {"status": "not_projected", "reason": "source_hash_mismatch"}
        return result

    collect("floors", lambda: saved_floor_status(toolkit, candidate))
    if toolkit.manifest.get("drawing_review_policy"):
        from scripts.tool_scripts.bim_agent_process_reviews import process_status, building_facts
        collect("process_review", lambda: process_status(toolkit, candidate))
        collect("building_facts", lambda: building_facts(source))

    def openings():
        summary = opening_delivery_summary(toolkit, source)
        return {key: summary[key] for key in
                ("opening_review_scopes", "current_reviews", "stale_reviews")}
    collect("opening_review", openings)
    collect("space_relations", lambda: toolkit.space_relation_status(source))
    collect("facade_counts", lambda: toolkit.facade_counts(candidate))

    # One projection validates ancestry/actual targets before computing height
    # location. Neither the cached height table nor old applications suffice.
    try:
        current = project(toolkit.claims(), candidate)
        result["claims"] = {"status": "projected_current",
            "state_counts": dict(Counter(row["state"] for row in current["claims"])),
            "adopted_unapplied_count": sum(row["state"] in
                {"pending_application", "partially_satisfied", "changed_since_check"} for row in current["claims"]),
            "claims_with_unresolved_count": sum(bool(row.get("unresolved")) and row["state"] != "retracted"
                                                for row in current["claims"]),
            "claims_with_failed_history_count": sum(bool(row.get("failed_attempts")) for row in current["claims"]),
            "issues": [{key: row[key] for key in
                ("id", "state", "objects", "missing_bindings", "failed_attempts", "unresolved") if key in row}
                for row in current["claims"] if row["state"] != "retracted" and
                (row["state"] not in {"applied_current", "confirmed_unchanged"} or row.get("unresolved"))],
            "retracted_claims": [{key: row[key] for key in ("id", "state", "unresolved") if key in row}
                                 for row in current["claims"] if row["state"] == "retracted"],
            "failed_attempt_history": [{key: row[key] for key in ("id", "state", "failed_attempts")}
                for row in current["claims"] if row.get("failed_attempts")],
            "history_meaning": "Failed attempts are history, not a current failure or required follow-up.",
            "other_branch_count": len(current["other_branch_claim_ids"]),
            "inherited_observations": current["inherited_observations"]}
        collect("height_coverage", lambda: located_height_report(toolkit, candidate, current))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result["claims"] = {"status": "unavailable", "reason": str(error)}
        result["height_coverage"] = {"status": "not_projected", "reason": "current_claim_projection_unavailable"}
    return result


def candidate_review_summary(review):
    """Small status/counts only; complete issue objects remain in the bound reply."""
    summary = {key: review[key] for key in
               ("candidate", "source_model_sha256", "drawing_fidelity", "calibration_independently_verified", "scope")}
    for name, section in review.items():
        if not isinstance(section, dict):
            continue
        row = {key: section[key] for key in
               ("status", "complete_building", "source_geometry_ready", "state_counts",
                "sample_count", "conflict_count", "unassessed_count", "stale_review_count",
                "adopted_unapplied_count", "claims_with_unresolved_count", "claims_with_failed_history_count") if key in section}
        if "reason" in section:
            row["reason"] = str(section["reason"])[:240]
        if name == "process_review":
            row.update({key: section[key] for key in ("ready", "missing_images", "missing_plan_floors") if key in section})
            row["pending"] = [{key: item[key] for key in ("scope", "reason") if key in item}
                              for item in section.get("pending", [])[:16]]
            row["building_status"] = (section.get("building_review") or {}).get("status", "missing")
        if name == "building_facts":
            row["floors"] = section.get("floors", [])[:12]
            row["floor_projection_comparisons"] = section.get("floor_projection_comparisons", [])[:12]
            row["door_components"] = [{"space_count": len(item["space_ids"]),
                "space_ids": item["space_ids"][:12], "has_exterior_door": item["has_exterior_door"]}
                for item in section.get("door_components", [])[:12]]
            row["spaces_without_declared_door"] = section.get("spaces_without_declared_door", [])[:24]
            row["meaning"] = section.get("meaning")
        if name == "source":
            row["validation_status"] = (section.get("validation") or {}).get("status")
        if "summary" in section:
            row["summary"] = section["summary"]
        for key, value in section.items():
            if isinstance(value, list):
                row[key + "_count"] = len(value)
        if name == "opening_review":
            scopes = section.get("opening_review_scopes", [])
            row["scope_status_counts"] = dict(Counter(s["review_status"] for s in scopes))
            row["scopes_with_openings_status_counts"] = dict(Counter(
                s["review_status"] for s in scopes if s["built_count"] > 0))
            row["follow_up_scope_count"] = sum(s["review_status"] == "observations_require_follow_up" for s in scopes)
        if name == "height_coverage":
            rows = section.get("openings", [])
            row["openings_with_issues_count"] = sum(bool(r.get("issues")) for r in rows)
            row["issue_code_counts"] = dict(Counter(code for r in rows for code in r.get("issues", [])))
            row["issue_opening_ids_sample"] = [str(r["opening_id"])[:120] for r in rows
                                               if r.get("issues")][:3]
        if name == "claims":
            row["issue_claim_ids_sample"] = [str(r["id"])[:120] for r in section.get("issues", [])][:3]
            if "history_meaning" in section:
                row["history_meaning"] = section["history_meaning"]
        summary[name] = row
    return summary


def saved_result(result, *, candidate=None, created=(), geometry_applied=False, audit_written=False):
    result["saved_candidate"] = candidate
    result["save_effects"] = dict(created_candidates=list(created),
        geometry_applied=bool(geometry_applied), audit_written=bool(audit_written))
    return result


def _geometry(source):
    """Physical objects only; source roles, notes and provenance are not geometry."""
    fields = {
        "floors": ("id", "footprint", "z_floor", "height", "spanning_space_ids"),
        "spaces": ("id", "floor_id", "polygon", "z_floor", "height", "ceiling_height"),
        "boundaries": ("id", "space_id", "kind", "geometry_type", "vertices", "adjacent_space_ids", "counterpart_ids"),
        "openings": ("id", "kind", "vertices", "space_ids", "host_boundary_id", "exterior", "connectivity"),
        "connections": ("kind", "opening_id", "space_ids", "exterior", "state"),
    }
    result = {collection: sorted((tuple(json.dumps(row.get(k), sort_keys=True) for k in keys)
            for row in source.get(collection, []))) for collection, keys in fields.items()}
    result["opening_hosts"] = source.get("opening_hosts", {})
    result["coordinate_system"] = {k: source.get("coordinate_system", {}).get(k)
                                   for k in ("units", "up_axis", "north_axis")}
    north = result["coordinate_system"]["north_axis"]
    if isinstance(north, dict):
        result["coordinate_system"]["north_axis"] = north.get("value_deg")
    return result


def saved_source_result(run, result, *, parent=None, selection=False):
    candidate = result.get("candidate")
    path = Path(run) / str(candidate) / "source_model.json"
    if not candidate or result.get("source_geometry_ready") is False or not path.is_file():
        return saved_result(result, audit_written=True)
    applied = not selection
    if parent:
        before = Path(run) / parent / "source_model.json"
        if before.is_file():
            applied = _geometry(json.loads(before.read_bytes())) != _geometry(json.loads(path.read_bytes()))
    return saved_result(result, candidate=candidate, created=[] if selection else [candidate],
                        geometry_applied=applied, audit_written=True)


def result_metadata(raw):
    structured = raw.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = raw.get("content", [])
    if not isinstance(content, list):
        return {}
    for block in reversed(content):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            value = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def full_result_metadata(raw, run):
    """Read the hash-bound full result for program consumers, not prompt expansion."""
    import hashlib
    meta = {**result_metadata(raw), **(raw.get("structuredContent") or {})}
    if not meta.get("details_file"):
        return meta
    root = Path(run).resolve()
    path = (root / meta["details_file"]).resolve()
    if path.parent != (root / "tool_reports").resolve() or not path.is_file():
        raise ValueError("full tool result is missing or outside tool_reports")
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != meta.get("details_sha256"):
        raise ValueError("full tool result hash mismatch")
    saved = json.loads(contents)
    if not isinstance(saved, dict):
        raise ValueError("full tool result must be an object")
    return {**meta, **saved}


def read_saved_result(data, *, tool=None, run=None):
    """Read live results directly; one conservative adapter for older archives.

    No consumer maintains its own saving-tool list. Inspection results cannot
    select a candidate merely because they mention one. Failed later entries do
    not discard earlier commits. Legacy geometry application requires evidence.
    """
    if "saved_candidate" in data:
        return saved_result({}, candidate=data["saved_candidate"],
            created=data.get("save_effects", {}).get("created_candidates", []),
            geometry_applied=data.get("save_effects", {}).get("geometry_applied", False),
            audit_written=data.get("save_effects", {}).get("audit_written", False))
    empty = saved_result({})
    if tool not in SAVE_TOOLS:
        return empty
    if tool == "claim_transaction":
        entries = [row for row in data.get("entries", [])
                   if row.get("status") == "applied" and row.get("result_candidate")]
        committed = [row["result_candidate"] for row in entries]
        applied, parent = False, data.get("candidate")
        for row in entries:
            effects = row.get("save_effects")
            if effects is not None:
                applied |= effects.get("geometry_applied", False)
            elif run is not None and parent:
                root = Path(run).resolve()
                paths = [(root / str(c) / "source_model.json").resolve()
                         for c in (parent, row["result_candidate"])]
                if all(p.is_relative_to(root) and p.is_file() for p in paths):
                    applied |= _geometry(json.loads(paths[0].read_bytes())) != _geometry(json.loads(paths[1].read_bytes()))
            parent = row["result_candidate"]
        return saved_result({}, candidate=committed[-1] if committed else None,
            created=committed, geometry_applied=applied, audit_written=bool(data.get("audit_file")))
    candidate = data.get("candidate")
    if not isinstance(candidate, str) or data.get("error") or data.get("status") in {"failed", "error"}:
        return empty
    ready = data.get("source_geometry_ready")
    if ready is None and run is not None:
        root = Path(run).resolve()
        folder = (root / candidate).resolve()
        if folder.parent == root:
            report = folder / "report.json"
            if report.is_file():
                ready = json.loads(report.read_bytes()).get("source_geometry_ready")
            if ready is None and (folder / "source_model.json").is_file():
                ready = True
    if ready is not True:
        # Old returns sometimes named only the attempt; its persisted report
        # still decides domain failure, even when no usable source was saved.
        empty["source_geometry_ready"] = ready
        return empty
    app = data.get("claim_application", {})
    applied = (app.get("status") == "applied" and
        app.get("geometry_before_sha256") != app.get("geometry_after_sha256"))
    if not app and tool not in {"revise_bim", "finish_bim"}:
        applied = True
    return saved_result({}, candidate=candidate, created=[] if tool == "finish_bim" else [candidate],
        geometry_applied=applied, audit_written=True)
