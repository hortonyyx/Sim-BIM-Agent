"""One deterministic, resumable assembly from immutable reader deliveries."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from src.agent_runtime.store import json_bytes

from .height_writes import apply_heights, saved_application
from .feedback import assembly_reply
from .levels import LEVEL_TOLERANCE_M, decision, resolve_levels
from .lineage import candidate_readers, latest_candidate, metadata


def _hash(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def select_deliveries(session, task_ids=None):
    registry = session.registry
    eligible = {key: row for key, row in registry.records.items()
                if row["status"] == "completed" and row.get("artifact")
                and (row.get("validation") or {}).get("validation_passed")}
    replaced = set()
    for key in eligible:
        previous, seen = registry.task(key).get("previous_task_id"), set()
        while previous and previous not in seen:
            replaced.add(previous)
            seen.add(previous)
            previous = registry.task(previous).get("previous_task_id")
    if task_ids is not None:
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise ValueError("task_ids must be a nonempty list of distinct delivered task IDs")
        if set(task_ids) - set(eligible) or set(task_ids) & replaced:
            raise ValueError("select current validated deliveries; failed, missing and replaced tasks cannot be assembled")
        keys = task_ids
    else:
        keys = sorted(set(eligible) - replaced)
    selected = {}
    for key in keys:
        artifact = registry.read(key)
        role = eligible[key]["role_id"]
        target = artifact["plan"]["floor_id"] if role == "plan_reader" else artifact["orientation"]
        identity = role, target
        path = registry.child(key).task_directory / "reader_record.json"
        row = eligible[key]
        rank = (row.get("delivered_at_ns") or path.stat().st_mtime_ns, key)
        if identity in selected and task_ids is not None:
            raise ValueError("choose one plan per floor and one elevation per facade")
        if identity not in selected or rank > selected[identity][0]:
            selected[identity] = rank, key, artifact
    plans, elevations, references = {}, {}, []
    for (role, target), (_, key, artifact) in sorted(selected.items()):
        references.append({"task_id": key, "role_id": role, "target": target,
                           "sha256": eligible[key]["artifact"]["sha256"]})
        if role == "plan_reader":
            plans[target] = (key, artifact)
        else:
            elevations[key] = artifact
    if not plans:
        raise ValueError("no validated plan delivery; dispatch a plan reader first")
    return plans, elevations, references


def _issues_from_match(value):
    result, issues = value["result"], []
    for kind in ("elevation_only", "source_only", "conflicts"):
        for item in result[kind]:
            issues.append(decision(kind, "The elevation and plan do not yet establish a safe height match.",
                "Inspect both original views and the listed IDs; re-dispatch the affected reader with specific rework_targets, or correct the current candidate and reassemble.",
                task_id=value["task_id"], orientation=result["orientation"], detail=item))
    return issues


def _review_issues(review):
    if not review:
        return []
    if review["status"] == "blocked":
        return [decision("assembly_lineage", "Candidate uses stale or unverified plan deliveries.",
            "Call assemble_from_readers with current deliveries; this cannot be accepted as partial delivery.",
            review_id=review["review_id"], detail=row) for row in review.get("blockers", [])]
    if review["status"] != "needs_review":
        return []
    return [decision("assembly_change", "Assembly differs from the accepted reader trial.",
            "Inspect this change; call review_role_assembly with a reason for every change before further writes.",
            review_id=review["review_id"], detail=row) for row in review["changes"]]


def _descends(session, candidate, ancestor):
    # Local plan revisions trace parent_draft_id instead of parent_candidate.
    # The verified reader lineage also covers those ordinary editing tools.
    readers = candidate_readers(session, ancestor)
    if readers and candidate_readers(session, candidate) == readers:
        return True
    seen = set()
    while candidate and candidate not in seen:
        if candidate == ancestor:
            return True
        seen.add(candidate)
        report = json.loads((session.run_directory / candidate / "report.json").read_bytes())
        candidate = (report.get("provenance") or {}).get("parent_candidate")
    return False


async def carry_room_uses(session, candidate, plans):
    """Preserve the reader's type as an explicitly inferred, image-backed use.

    The seed format cannot distinguish a printed use label from interpretation;
    do not promote arbitrary evidence prose to an observed label. Existing use
    evidence (including deliberate local revisions) is never overwritten.
    """
    source = session._source(candidate)
    operations = []
    references = []
    for space in source["spaces"]:
        if space.get("role_evidence") or space.get("role", "unknown") == "unknown":
            continue
        floor = space["floor_id"]
        if floor not in plans:
            raise ValueError(f"room use has no selected plan: {space['id']}")
        task_id, artifact = plans[floor]
        image = session.registry.records[task_id]["image"]
        seed_id = space["id"].removeprefix(floor + ":") if len(plans) > 1 else space["id"]
        seed = next((row for row in artifact["plan"].get("space_seeds", []) if row["id"] == seed_id), None)
        if seed is None:
            raise ValueError(f"room use cannot be traced to a reader seed: {space['id']}")
        evidence = [row for row in artifact.get("evidence", []) if row.get("item") == "plan.space_seeds:" + seed_id]
        refs = [f"image:{image}", f"reader:{task_id}/plan.space_seeds:{seed_id}", *seed.get("source_refs", [])]
        refs.extend(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in evidence)
        reason = f"Plan reader assigned {space['role']} at seed {seed_id}; use inferred from {image}, not a verified printed use label."
        operations.append({"op": "set_space_role", "space_id": space["id"], "role": space["role"],
                           "basis": "inferred", "assumptions": [reason], "reason": reason,
                           "source_refs": list(dict.fromkeys(refs))})
        references.append({"space_id": space["id"], "task_id": task_id,
                           "artifact_sha256": session.registry.records[task_id]["artifact"]["sha256"]})
    if not operations:
        return candidate
    result = await session._once("room_uses:" + _hash({"candidate": candidate, "operations": operations}),
        "revise_bim", {"candidate": candidate, "operations_json": json.dumps(operations, ensure_ascii=False)},
        reference={"reader_room_uses": references})
    meta = metadata(result)
    if not meta.get("source_geometry_ready") or not meta.get("candidate"):
        raise ValueError("Reader room-use evidence could not be saved; inspect the retained room_uses receipt")
    return meta["candidate"]


async def assemble_from_readers(session, *, task_ids=None, level_overrides=()):
    """Caller holds the write lock. Every mutation has a durable _once receipt."""
    plans, elevations, references = select_deliveries(session, task_ids)
    resolutions, issues = resolve_levels(session.registry,
        {floor: artifact["plan"] for floor, (_, artifact) in plans.items()}, elevations, level_overrides)
    inputs = {"deliveries": references, "levels": resolutions}
    identity = _hash(inputs)
    path = "role_assemblies/" + identity + ".json"
    saved_path = session.store.directory / path
    state = json.loads(saved_path.read_bytes()) if saved_path.is_file() else {"inputs": inputs}
    if state["inputs"] != inputs:
        raise ValueError("assembly input receipt changed")

    # Readers' own unresolved notes are reported with the delivery, not raised as
    # decisions: run3 alone had 13, and listing them as pending would keep every
    # assembly "needs_decisions" and send the coordinator re-checking each note.
    notes = [{"task_id": ref["task_id"], "note": item} for ref in references
             for item in session.registry.read(ref["task_id"], sha256=ref["sha256"]).get("unresolved", [])]
    for facade in sorted({"North", "South", "East", "West"} - {v["orientation"] for v in elevations.values()}):
        issues.append(decision("missing_elevation", "No selected delivery for this facade; its heights remain provisional.",
            "Dispatch an elevation reader if this original view is available; otherwise retain an explicit height assumption.", orientation=facade))

    def save():
        session.store.write_json(path, state)

    def finish(candidate, *, review=None, matches=(), write=None, status=None):
        from .session import envelope
        pending = [*issues, *_review_issues(review)]
        positions = session.positions.summary()
        response = {"status": status or ("needs_decisions" if pending or positions["delivery_blocking"] else "completed"),
            "candidate": candidate, "source_geometry_ready": candidate is not None,
            "assembly_id": identity, "deliveries": references, "levels": resolutions,
            "level_tolerance_m": LEVEL_TOLERANCE_M,
            "facades": [{"task_id": v["task_id"], "orientation": v["result"]["orientation"],
                "matched": len(v["result"]["matches"]), "source_only": len(v["result"]["source_only"]),
                "elevation_only": len(v["result"]["elevation_only"]), "conflicts": len(v["result"]["conflicts"]),
                "horizontal_fits": v["result"].get("horizontal_fits", [])} for v in matches],
            "height_write": write, "assembly_review": None if not review else {
                key: review[key] for key in ("review_id", "status", "checked_floors", "changes", "blockers", "delivery_scope")
                if key in review},
            "decisions": pending, "position_review": positions, "reader_notes": notes,
            "saved_candidates": len(list(session.run_directory.glob("candidate_*/report.json")))}
        state["response"] = response
        state["complete"] = status != "assembly_review_required" and candidate is not None
        state["candidate"] = candidate
        if candidate:
            from scripts.tool_scripts.bim_agent_process_reviews import building_facts
            response["architectural_facts"] = building_facts(session._source(candidate))
            state["position_revision"] = session.positions.revision()
            session.positions.bind(candidate, references, assembly_id=identity,
                position_revision=state["position_revision"] if state["complete"] else None)
            state["source_sha256"] = hashlib.sha256(
                (session.run_directory / candidate / "source_model.json").read_bytes()).hexdigest()
        save()
        return envelope(assembly_reply(response, saved_path, receipt_file=path))

    candidate = state.get("candidate")
    if state.get("complete"):
        session._source(candidate)
        if hashlib.sha256((session.run_directory / candidate / "source_model.json").read_bytes()).hexdigest() != state["source_sha256"]:
            raise ValueError("completed assembly source changed; saved candidates are immutable")
        session.positions.bind(candidate, references, assembly_id=identity,
            position_revision=state.get("position_revision"))
        latest = latest_candidate(session, candidate)
        if latest == candidate or not _descends(session, latest, candidate):
            from .session import envelope
            session.positions.refresh(candidate)
            if state.get("position_revision") == session.positions.revision():
                state["response"]["position_review"] = session.positions.summary()
                state["response"]["status"] = ("needs_decisions" if state["response"]["decisions"]
                    or state["response"]["position_review"]["delivery_blocking"] else "completed")
                save()
                return envelope(assembly_reply(state["response"], saved_path, receipt_file=path))
            latest = candidate
        # A deliberate candidate-only revision is the new base. Never rebuild
        # unchanged reader plans over the coordinator's local correction.
        state.update(candidate=latest, complete=False, local_revision=True)
        state.pop("height_result", None)
        candidate = latest
    save()
    current = session.assembly.current()
    if current and current["status"] == "needs_review":
        return finish(current["candidate"], review=current, status="assembly_review_required")

    if any(row[field]["value_m"] is None for row in resolutions.values() for field in ("z_floor", "ceiling_height")):
        return finish(None, status="needs_levels")
    if not state.get("local_revision"):
        builds = []
        for floor in resolutions:
            task_id, _ = plans[floor]
            ref = session.registry.records[task_id]["artifact"]
            result = await session._build_from_artifact(task_id, ref["sha256"], resolved_levels=resolutions[floor], elevation_readings=elevations)
            meta = metadata(result)
            notes.extend({"task_id": task_id, "assembly_materialization": row} for row in meta.get("materialization", []))
            if not meta.get("source_geometry_ready") or not meta.get("candidate"):
                issues.append(decision("build_failed", "The selected floor could not compile safely.",
                    "Inspect the saved draft/error and re-dispatch this plan reader with the specific defect.",
                    task_id=task_id, floor_id=floor, detail=meta))
                return finish(None, status="build_failed")
            builds.append((floor, meta))
        candidate = builds[0][1]["candidate"]
        if len(builds) > 1:
            floors = []
            for floor, meta in builds:
                bound = meta["plan_input"]
                floors.append({"draft_id": PurePosixPath(bound["plan_file"]).parent.name, "expected_plan_sha256": bound["plan_sha256"],
                    "floor_id": floor, "z_floor": resolutions[floor]["z_floor"]["value_m"],
                    "evidence": json.dumps(resolutions[floor], ensure_ascii=False, sort_keys=True)})
            result = await session._once("assembly:" + _hash(floors), "assemble_plan_bim",
                {"floors_json": json.dumps(floors, ensure_ascii=False)}, reference={"floors": floors})
            meta = metadata(result)
            if not meta.get("source_geometry_ready") or not meta.get("candidate"):
                issues.append(decision("assembly_failed", "Floor assembly did not produce safe geometry.",
                    "Inspect the assembly error and correct the affected floor delivery.", detail=meta))
                return finish(None, status="build_failed")
            candidate = meta["candidate"]
        state["candidate"] = candidate
        save()

    review = session.assembly.check(candidate, require_all=True)
    if review and review["status"] in {"needs_review", "blocked"}:
        return finish(candidate, review=review, status=("assembly_delivery_blocked"
            if review["status"] == "blocked" else "assembly_review_required"))
    candidate = await carry_room_uses(session, candidate, plans)
    session.positions.bind(candidate, references, assembly_id=identity)
    state["candidate"] = candidate
    save()
    matches = [session.match(task_id, candidate, height_bounds=True) for task_id in sorted(elevations)]
    for value in matches:
        issues.extend(_issues_from_match(value))
    ids = sorted(value["match_id"] for value in matches if value["result"]["can_apply"])
    write = None
    if ids:
        # If killed after a height receipt but before the outer workflow receipt,
        # recover it verbatim. Do not even perform a second evidence confirmation.
        receipt = saved_application(session, ids)
        result = receipt["result"] if receipt is not None else await apply_heights(session, ids, candidate=candidate)
        meta = metadata(result)
        write = {"status": meta.get("status"), "matched_openings": sum(len(v["result"]["matches"]) for v in matches)}
        if meta.get("status") == "completed":
            candidate = (meta.get("claim_application") or {}).get("candidate") or meta.get("candidate", candidate)
        else:
            issues.append(decision("height_write_failed", "The safe height batch was not accepted; the base candidate is retained.",
                "Inspect the height transaction result before making a bounded correction.", detail=meta))
        state["height_result"] = meta
    review = session.assembly.check(candidate, require_all=True)
    return finish(candidate, review=review, matches=matches, write=write)
