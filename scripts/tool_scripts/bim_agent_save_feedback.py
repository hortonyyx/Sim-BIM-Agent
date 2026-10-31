"""Current saved mesh facts supplied with a save, without rerunning validation.

Exact diffs reuse the inference audit. Continuity, openings and source findings
are projections of the hash-verified source, not a new acceptance procedure.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import hashlib
import json

from scripts.tool_scripts.bim_agent_inference import (
    _source, audit_inference_candidate, door_width_summary,
)
from scripts.tool_scripts.bim_agent_review_scope import compact_review_scope, review_scope


_LIMIT = 4
_SCOPE = ("Facts from the current saved source only; not drawing fidelity, "
          "architectural truth, continuity completeness, or delivery acceptance. "
          "Existing validation is reused, not rerun.")


def _sample(ids):
    rows = sorted(set(ids))
    return {"count": len(rows), "ids": rows[:_LIMIT], "omitted_count": max(0, len(rows) - _LIMIT)}


def _compact_diff(comparison):
    if not comparison:
        return {"status": "no_parent", "meaning": "Initial saved source; no previous entity comparison."}
    entities = comparison["entities"]
    return {
        "status": "compared",
        "previous_candidate": comparison["previous_candidate"],
        "previous_source_model_sha256": comparison["previous_source_model_sha256"],
        "entity_scope": entities["entity_scope"],
        "entities": {kind: {name.removesuffix("_ids"): _sample(ids)
                            for name, ids in rows.items()}
                     for kind, rows in entities.items() if kind != "entity_scope"},
    }


def _bound_summary(reply):
    """Keep decision facts directly visible when the containing build is large."""
    def size():
        return len(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
    if size() <= 2500:
        return reply
    reply["scope"] = "Current-source facts only; not drawing fidelity, architecture, continuity completeness or delivery acceptance."
    doors = reply.get("door_geometry")
    if doors is not None:
        doors["smallest_measured_doors"] = [
            {key: row[key] for key in ("id", "width_m") if key in row}
            for row in doors["smallest_measured_doors"][:1]]
        doors["omitted_measured_count"] = doors["measured_count"] - len(doors["smallest_measured_doors"])
        doors.pop("meaning", None)  # Scope and the full bound facts retain the interpretation.
        doors.pop("door_count", None)  # Exactly measured_count + unmeasured_count.
    cores = reply["continuous_space_facts"]
    cores.pop("volumes", None)
    cores["omitted_volume_count"] = cores["declared_spanning_volume_count"]
    affected = reply["affected_opening_hosts"]
    affected.pop("openings", None)
    affected.pop("removed_opening_ids", None)
    affected["omitted_opening_count"] = affected["count"]
    entities = reply["entity_diff"].get("entities", {})
    samples = []
    for kind, changes in entities.items():
        for change, group in changes.items():
            if change != "preserved":
                samples.extend({"collection": kind, "change": change, "id": identity}
                               for identity in group["ids"])
            changes[change] = {"count": group["count"]}
    if samples:
        reply["entity_diff"]["changed_id_sample"] = samples[:2]
    # Interpretations remain in the top-level scope and complete bound report.
    for section in (cores["saved_vertical_continuity_review"], reply["saved_source_check"]):
        section.pop("meaning", None)
    if size() > 2500:
        # Keep material-change actions ahead of exact-diff samples, which can
        # include harmless floating-point recomputation or provenance updates.
        reply["entity_diff"].pop("changed_id_sample", None)
        for changes in reply["entity_diff"].get("entities", {}).values():
            changes.pop("preserved", None)
        for group in reply.get("review_scope", {}).get("affected", {}).values():
            group["ids"] = group["ids"][:1]
            group["omitted_count"] = group["count"] - len(group["ids"])
    if size() > 2500:
        reply["entity_diff"].pop("entities", None)
        reply["entity_diff"]["entity_counts_location"] = "facts_file:/entity_diff/entities"
    if size() > 2500:
        reply["entity_diff"].pop("entity_scope", None)
        reply["entity_diff"].pop("previous_source_model_sha256", None)
        reply.pop("source_file", None)
        reply.pop("source_file_sha256", None)  # Retained in hash-bound facts_file.
    return reply


def _host_fact(opening, source, boundaries):
    identity = opening["id"]
    declared = source.get("opening_hosts", {}).get(identity)
    host_ids = declared if isinstance(declared, list) else [opening.get("host_boundary_id")]
    host_ids = [value for value in host_ids if isinstance(value, str)]
    return {
        "id": identity, "kind": opening.get("kind"),
        "space_ids": opening.get("space_ids", []),
        "host_boundary_id": opening.get("host_boundary_id"),
        "source_host_boundary_ids": host_ids,
        "host_mapping_present": isinstance(declared, list),
        "missing_host_boundary_ids": [identity for identity in host_ids if identity not in boundaries],
        "host_space_ids": sorted({boundaries[identity]["space_id"] for identity in host_ids
                                  if identity in boundaries and boundaries[identity].get("space_id")}),
    }


def _affected_openings(source, comparison, material_scope=None):
    """Include unchanged apertures whose host or adjacent source space changed."""
    openings = {row["id"]: row for row in source["openings"]}
    boundaries = {row["id"]: row for row in source["boundaries"]}
    if comparison:
        entities = comparison["entities"]
        modified = {kind: set(rows["changed_ids"]) | set(rows["added_ids"]) | set(rows["removed_ids"])
                    for kind, rows in entities.items() if kind != "entity_scope"}
        direct = modified["openings"] | modified["windows"]
        affected = {
            identity for identity, row in openings.items()
            if identity in direct
            or modified["spaces"].intersection(row.get("space_ids", []))
            or modified["boundaries"].intersection(source.get("opening_hosts", {}).get(
                identity, [row.get("host_boundary_id")]))
        }
        if material_scope is not None:
            affected = set(material_scope["affected"].get("openings", [])) | set(
                material_scope["affected"].get("windows", []))
            affected.intersection_update(openings)
        removed = (set(entities["openings"]["removed_ids"]) | set(entities["windows"]["removed_ids"])) - openings.keys()
        scope = "Materially changed openings and those affected by source host/space changes; provenance and numerical noise excluded."
    else:
        affected, removed = set(openings), set()
        scope = "All current openings because no valid parent comparison is available."
    rows = [_host_fact(openings[identity], source, boundaries) for identity in sorted(affected)]
    return {"scope": scope, "count": len(rows), "openings": rows,
            "removed_opening_ids": sorted(removed),
            "missing_host_count": sum(bool(row["missing_host_boundary_ids"]) for row in rows),
            "missing_host_mapping_count": sum(not row["host_mapping_present"] for row in rows),
            "meaning": "Host identities only; existing source validation owns geometric host validity."}


def _core_facts(source, audit):
    """Describe declared spanning prisms and their actual saved door records."""
    spaces = {row["id"]: row for row in source["spaces"]}
    spanning = defaultdict(list)
    for floor in source.get("floors", []):
        for space_id in floor.get("spanning_space_ids", []):
            spanning[space_id].append(str(floor["id"]))
    cores, doors = [], {row["id"]: row for row in audit["doors"]}
    all_door_ids = set()
    for space_id, floor_ids in sorted(spanning.items()):
        space = spaces.get(space_id)
        if space is None:
            cores.append({"space_id": space_id, "status": "missing_source_space",
                          "declared_floor_ids": sorted(floor_ids), "door_connections": []})
            continue
        connected = []
        for opening in source["openings"]:
            if (opening.get("kind") != "door" or opening.get("exterior")
                    or space_id not in opening.get("space_ids", [])
                    or len(opening.get("space_ids", [])) < 2):
                continue
            other_ids = sorted(set(opening["space_ids"]) - {space_id})
            adjacent_floors = sorted({str(spaces[identity]["floor_id"]) for identity in other_ids
                                      if identity in spaces})
            measured = doors[opening["id"]]
            connected.append({
                "opening_id": opening["id"], "other_space_ids": other_ids,
                "adjacent_floor_ids": adjacent_floors,
                **{key: measured[key] for key in ("bottom_z_m", "top_z_m", "height_m", "width_m")
                   if key in measured},
                "connection_record_count": len(measured["connection_records"]),
                "host_boundary_id": opening.get("host_boundary_id"),
            })
            all_door_ids.add(opening["id"])
        cores.append({
            "space_id": space_id, "role": space.get("role"),
            "status": "saved_single_volume",
            "source_floor_id": space.get("floor_id"),
            "z_floor_m": space.get("z_floor"), "height_m": space.get("height"),
            "declared_floor_ids": sorted(floor_ids), "door_connections": connected,
        })
    review = source.get("vertical_continuity_review")
    review = review if isinstance(review, dict) else {}
    return {
        "scope": "Explicit floor spanning_space_ids and actual doors attached to those source spaces; "
                 "adjacent floors alone do not establish that each door serves the intended storey.",
        "declared_spanning_volume_count": len(cores), "volumes": cores,
        "actual_door_connection_count": len(all_door_ids),
        "saved_vertical_continuity_review": {
            "status": review.get("status", "unavailable"),
            "resolved_interface_count": len(review.get("resolved_interfaces", [])),
            "unresolved_stack_count": len(review.get("unresolved_stacks", [])) if review else None,
            "invalid_declaration_count": len(review.get("invalid_declarations", [])) if review else None,
            "source_json_pointer": "/vertical_continuity_review",
            "meaning": "Previously computed check on this saved source; no continuity check rerun.",
        },
    }


def attach_save_facts(toolkit, result, parent=None, feedback_parent=None, feedback_parent_error=None):
    """Attach bounded facts; feedback errors never turn a saved candidate into a failed build."""
    if not toolkit.manifest.get("mesh_input") or not result.get("candidate"):
        return result
    candidate = result["candidate"]
    try:
        source, path, file_hash = _source(toolkit, candidate)
        if result.get("source_model_sha256") != source["source_model_sha256"]:
            raise ValueError("saved result and current source hash do not match")
        comparison_error = feedback_parent_error
        previous_source = previous_path = previous_file_hash = None
        comparison_origin = "source_parent" if parent is not None else "none"
        if feedback_parent_error:
            comparison_origin = "unavailable_feedback_ancestor"
        if feedback_parent is not None:
            # Match the existing plan-feedback ancestry without making it the
            # geometry parent or trusting a stale draft's candidate name alone.
            comparison_origin = "bound_feedback_ancestor"
            parent = feedback_parent.get("candidate")
            try:
                folder = toolkit.candidate_path(parent)
                for name, field in (("source_model.json", "source_file_sha256"),
                                    ("proposal.json", "proposal_sha256"),
                                    ("parametric_plan.json", "plan_sha256")):
                    if hashlib.sha256((folder / name).read_bytes()).hexdigest() != feedback_parent[field]:
                        raise ValueError(f"feedback ancestor {name} binding changed")
            except (OSError, ValueError, KeyError, TypeError) as error:
                comparison_error = str(error)
        if parent is not None:
            try:
                previous_source, previous_path, previous_file_hash = _source(toolkit, parent)
            except (OSError, ValueError, KeyError, TypeError) as error:
                comparison_error = str(error)
        summary = audit_inference_candidate(toolkit, candidate, None if comparison_error else parent)
        audit_path = toolkit.run / summary["audit_file"]
        audit = json.loads(audit_path.read_bytes())
        if (audit["source_model_sha256"] != source["source_model_sha256"]
                or audit["source_file_sha256"] != file_hash):
            raise ValueError("source changed while collecting saved facts")
        comparison = audit.get("comparison")
        if comparison and (
                previous_source is None
                or comparison["previous_source_model_sha256"] != previous_source["source_model_sha256"]
                or hashlib.sha256(previous_path.read_bytes()).hexdigest() != previous_file_hash):
            comparison_error = "comparison baseline changed while collecting saved facts"
            comparison = None
        diff = _compact_diff(comparison)
        if comparison_error:
            diff = {"status": "unavailable", "previous_candidate": parent, "reason": comparison_error}
        diff["comparison_origin"] = comparison_origin
        material_scope = review_scope(
            previous_source if comparison else None, source,
            previous_candidate=parent, unavailable_reason=comparison_error)
        affected = _affected_openings(source, comparison, material_scope if comparison else None)
        cores = _core_facts(source, audit)
        validation = source.get("validation", {})
        findings = validation.get("findings", [])
        checks = {
            "status": validation.get("status", "unavailable"),
            "finding_count": len(findings),
            "finding_counts_by_severity": dict(Counter(str(row.get("severity")) for row in findings)),
            "source_json_pointer": "/validation",
            "meaning": "Saved source consistency findings, not a new check or acceptance verdict.",
        }
        complete = {
            "schema_version": "mesh_save_facts_v1", "status": "available", "candidate": candidate,
            "source_model_sha256": source["source_model_sha256"], "source_file_sha256": file_hash,
            "source_file": path.relative_to(toolkit.run).as_posix(), "scope": _SCOPE,
            "audit_file": summary["audit_file"], "counts": summary["counts"], "entity_diff": diff,
            "continuous_space_facts": cores, "affected_opening_hosts": affected,
            "saved_source_check": checks, "review_scope": material_scope,
            "door_geometry": {**door_width_summary(audit["doors"]),
                              "all_doors_pointer": "/doors"},
        }
        full_path = toolkit.candidate_path(candidate) / "save_facts.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != file_hash:
            raise ValueError("source changed before saving current facts")
        full_path.write_text(json.dumps(complete, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        compact = copy.deepcopy(complete)
        compact["review_scope"] = compact_review_scope(material_scope)
        compact["facts_file"] = full_path.relative_to(toolkit.run).as_posix()
        compact["facts_file_sha256"] = hashlib.sha256(full_path.read_bytes()).hexdigest()
        compact["continuous_space_facts"] = {
            key: cores[key] for key in ("declared_spanning_volume_count", "actual_door_connection_count",
                                       "saved_vertical_continuity_review")}
        compact["continuous_space_facts"]["volumes"] = [
            {key: row[key] for key in ("space_id", "role", "status", "z_floor_m", "height_m") if key in row}
            | {"declared_floors": _sample(row["declared_floor_ids"]),
               "actual_door_connection_count": len(row["door_connections"])}
            for row in cores["volumes"][:_LIMIT]]
        compact["continuous_space_facts"]["omitted_volume_count"] = max(0, len(cores["volumes"]) - _LIMIT)
        compact["affected_opening_hosts"] = {
            **{key: affected[key] for key in ("count", "missing_host_count", "missing_host_mapping_count")},
            "openings": affected["openings"][:_LIMIT],
            "omitted_opening_count": max(0, affected["count"] - _LIMIT),
            "removed_opening_ids": affected["removed_opening_ids"][:_LIMIT],
            "removed_opening_count": len(affected["removed_opening_ids"]),
        }
        from scripts.tool_scripts.bim_agent_replies import compact_reply
        result["save_facts"] = compact_reply(
            toolkit.run, "mesh_save_facts", _bound_summary(compact),
            full_result={**complete, "inference_audit": audit}, force=True)
        # Exact file/hash references already locate the complete record.
        result["save_facts"].pop("details_read", None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result["save_facts"] = {
            "schema_version": "mesh_save_facts_v1", "status": "unavailable", "candidate": candidate,
            "reason": str(error), "scope": _SCOPE,
            "meaning": "Optional facts could not be collected; saved candidate and existing quality checks are retained.",
        }
    return result
