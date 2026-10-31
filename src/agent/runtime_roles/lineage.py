"""Read candidate provenance from durable build receipts, never the newest reader alone."""

from __future__ import annotations

import json
from pathlib import Path


def metadata(result):
    from scripts.tool_scripts.bim_agent_saved_result import result_metadata
    return {**result_metadata(result), **(result.get("structuredContent") or {})}


def latest_candidate(session, fallback):
    # Candidates are immutable and numbered by the toolkit. Failed builds do not
    # supersede the last usable source. This also survives a crash before a role
    # receipt was acknowledged, and includes explicit assembly/local revisions.
    paths = (p for p in session.run_directory.glob("candidate_*/report.json")
             if p.parent.name.removeprefix("candidate_").isdigit())
    for path in sorted(paths,
                       key=lambda p: int(p.parent.name.removeprefix("candidate_")), reverse=True):
        if json.loads(path.read_bytes()).get("source_geometry_ready") is True:
            return path.parent.name
    return fallback


def current_plan_deliveries(session):
    """Return the latest validated plan delivery for each admitted target.

    This deliberately follows the same eligibility and ranking rules as
    ``assembly.select_deliveries``.  Failed retries do not replace an earlier
    usable delivery, while a completed rework removes every ancestor in its
    ``previous_task_id`` chain.
    """
    registry = session.registry
    eligible = {
        key: row
        for key, row in registry.records.items()
        if row.get("role_id") == "plan_reader"
        and row.get("status") == "completed"
        and row.get("artifact")
        and (row.get("validation") or {}).get("validation_passed")
    }
    replaced = set()
    for key in eligible:
        previous, seen = registry.task(key).get("previous_task_id"), set()
        while previous and previous not in seen:
            replaced.add(previous)
            seen.add(previous)
            previous = registry.task(previous).get("previous_task_id")

    selected = {}
    for key in sorted(set(eligible) - replaced):
        row = eligible[key]
        artifact = registry.read(
            key, sha256=row["artifact"]["sha256"], role_id="plan_reader"
        )
        target = row["target"]
        record_path = registry.child(key).task_directory / "reader_record.json"
        rank = (row.get("delivered_at_ns") or record_path.stat().st_mtime_ns, key)
        if target not in selected or rank > selected[target][0]:
            selected[target] = (rank, {
                "task_id": key,
                "target": target,
                "floor_id": artifact["plan"]["floor_id"],
                "artifact_sha256": row["artifact"]["sha256"],
            })
    return {target: value for target, (_, value) in sorted(selected.items())}


def candidate_readers(session, candidate):
    builds = []
    for path in (session.store.directory / "role_operations").glob("*.json"):
        operation = json.loads(path.read_bytes())
        if operation.get("tool") == "build_plan_bim" and "result" in operation:
            builds.append((metadata(operation["result"]), operation["reference"]))

    def draft_readers(draft_file, seen):
        for meta, ref in builds:
            if (meta.get("plan_input") or {}).get("plan_file") == draft_file:
                return {ref["task_id"]}
        # A coordinator's local plan revision preserves the recorded draft chain.
        path = session.run_directory / draft_file
        if path.is_file():
            revision_file = path.parent / "input.json"
            if revision_file.is_file():
                revision = json.loads(revision_file.read_bytes()).get("revision") or {}
                parent = revision.get("parent_draft_id")
                if parent and parent not in seen:
                    return draft_readers(f"plan_drafts/{parent}/plan.json", seen | {parent})
        return set()

    def walk(identity, seen):
        if identity in seen:
            raise ValueError("candidate provenance contains a cycle")
        seen = seen | {identity}
        for meta, ref in builds:
            if meta.get("candidate") == identity:
                return {ref["task_id"]}
        path = session.run_directory / identity / "report.json"
        if not path.is_file():
            return set()
        provenance = json.loads(path.read_bytes()).get("provenance") or {}
        if parent := provenance.get("parent_candidate"):
            session._source(parent)  # Enforce the normal candidate path boundary.
            return walk(parent, seen)
        if plan := provenance.get("plan_input"):
            return draft_readers(plan["plan_file"], set())
        if assembly := provenance.get("plan_assembly"):
            path = (session.run_directory / assembly["file"]).resolve()
            if not path.is_relative_to(session.run_directory.resolve()):
                raise ValueError("assembly provenance escapes the run")
            rows = json.loads(path.read_bytes())["floors"]
            return set().union(*(draft_readers(f"plan_drafts/{row['draft_id']}/plan.json", set()) for row in rows))
        return set()

    return walk(candidate, set())


def guard_replaced_plans(session, candidate):
    used = candidate_readers(session, candidate)
    for task_id, row in session.registry.records.items():
        if row["role_id"] != "plan_reader" or row["status"] != "completed" or not row.get("artifact"):
            continue
        if not (row.get("validation") or {}).get("validation_passed"):
            continue
        previous = session.registry.task(task_id).get("previous_task_id")
        visited = set()
        while previous and previous not in visited:
            visited.add(previous)
            if previous in used:
                session.registry.read(task_id, role_id="plan_reader")
                raise ValueError(f"先用新产物建层：{candidate} 基于已被替代的平面 {previous}；"
                                 f"使用 {task_id}（sha256={row['artifact']['sha256']}）重新调用 "
                                 "assemble_from_readers")
            previous = session.registry.task(previous).get("previous_task_id")


def opening_plan(source):
    """Exact opening XY, kind, host and floor identity; height edits do not alter it."""
    floors = {row["id"]: row["floor_id"] for row in source["spaces"]}
    boundaries = {row["id"]: row for row in source["boundaries"]}
    return {row["id"]: {"kind": row["kind"], "exterior": row.get("exterior"),
        "host": row.get("host_boundary_id"), "spaces": sorted(row.get("space_ids", [])),
        "floors": sorted({floors.get(s) for s in row.get("space_ids", [])}),
        "xy": sorted({tuple(p[:2]) for p in row["vertices"]}),
        "host_xy": sorted({tuple(p[:2]) for p in boundaries.get(row.get("host_boundary_id"), {}).get("vertices", [])})}
        for row in source["openings"]}
