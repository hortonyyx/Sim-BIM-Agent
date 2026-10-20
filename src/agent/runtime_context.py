"""Build runtime state references from saved BIM artifacts, without changing tools."""

from __future__ import annotations

import json
from pathlib import Path

from src.agent_runtime.context import StateEntry
from src.harness_contracts import SourceRef


def update_building_context(engine, event, raw_result):
    """Index actual saved values; claims remain interpretations, never verified facts.

    Full dimensions, IDs, and geometry live in hash-addressed original artifacts.
    Current state keeps their references rather than regenerating geometry in text.
    """
    run = Path(engine.tools.run_directory).resolve()
    context, store = engine.context, engine.store

    def save(key, category, value, status, source, *, active=True):
        sources = source if isinstance(source, tuple) else (source,)
        old = next((s for s in context.state if s.key == key), None)
        if old and (old.value, old.epistemic_status, old.source_refs, old.active) == (value, status, sources, active):
            return
        context.set_state(StateEntry(key=key, category=category, value=value,
            epistemic_status=status, source_refs=sources, active=active,
            revision=old.revision + 1 if old else 1))

    def source_for(path):
        path = path.resolve()
        if not path.is_relative_to(run):
            raise ValueError("context artifact escapes run directory")
        ref = store.put_bytes(path.read_bytes(), "application/json")
        return SourceRef(source_id=path.relative_to(run).as_posix(), source_kind="tool",
            locator=path.relative_to(run).as_posix(), blob=ref)

    metadata = raw_result.get("structuredContent")
    if not isinstance(metadata, dict):
        metadata = {}
        for block in raw_result.get("content", []):
            if block.get("type") == "text":
                try:
                    value = json.loads(block["text"])
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    metadata.update(value)
    if event.payload.tool_name in {"delegate_mesh_workers", "read_mesh_worker", "mesh_worker_state"}:
        workers = (metadata.get("results", []) if event.payload.tool_name == "delegate_mesh_workers"
                   else metadata.get("tasks", []) if event.payload.tool_name == "mesh_worker_state"
                   else [metadata])
        for worker in workers:
            if not isinstance(worker, dict) or not worker.get("task_id"):
                continue
            # Preserve a compact reference through context windows. Advice is
            # never promoted to a verified building fact or applied geometry.
            ref = store.put_json(worker)
            save("mesh-worker:" + worker["task_id"], "evidence_reference", {
                "task_id": worker["task_id"], "status": worker.get("status"),
                "source_basis": worker.get("source_basis"), "record": ref.model_dump(mode="json"),
                "read_tool": "read_mesh_worker",
                "meaning": "worker advice on frozen evidence; coordinator judges applicability and quality",
            }, "inferred", engine._event_source(event))
    view_fields = ("view_id", "name", "image_sha256", "returned_png_sha256",
                   "box_original_pixels", "original_pixels_per_returned_pixel")
    saved_view_ids = set()
    # A tool can create views inside another operation (for example claim
    # evidence crops), without exposing a top-level view_id in its response.
    # Index the actual saved records as well as directly returned views.
    for view_path in sorted((run / "image_views").glob("view_*.json")):
        view = json.loads(view_path.read_bytes())
        view_id = view.get("view_id")
        if not view_id:
            continue
        source = source_for(view_path)
        value = {key: view[key] for key in view_fields if key in view}
        value["record"] = source.blob.model_dump(mode="json")
        save("view:" + view_id, "evidence_reference", value, "observed", source)
        saved_view_ids.add(view_id)
    if metadata.get("view_id") and metadata["view_id"] not in saved_view_ids:
        save("view:" + metadata["view_id"], "evidence_reference", {
            key: metadata[key] for key in view_fields if key in metadata},
            "observed", engine._event_source(event))
    for folder, pattern, status in (("claims", "claim_*.json", "inferred"),
                                    ("inferences", "inference_*.json", "inferred")):
        decisions = {}
        for decision_path in sorted((run / folder).glob("decision_*.json")):
            decision = json.loads(decision_path.read_bytes())
            if decision.get("claim_id"):
                decisions[decision["claim_id"]] = (decision, source_for(decision_path))
        for path in sorted((run / folder).glob(pattern)):
            source = source_for(path)
            value = json.loads(path.read_bytes())
            disposition, decision_source = decisions.get(path.stem, ({}, None))
            active = disposition.get("disposition", value.get("status")) not in {
                "retracted", "withdrawn", "resolved", "superseded"}
            sources = (source, decision_source) if decision_source else (source,)
            save(folder + ":" + path.stem, "evidence_reference",
                {"id": path.stem, "record": source.blob.model_dump(mode="json"),
                 "interpretation_status": "model interpretation; not independently verified"}, status, sources,
                 active=active)
            claim = value.get("claim", value.get("declaration", value))
            for field in ("unresolved", "uncertain"):
                items = claim.get(field, []) if isinstance(claim, dict) else []
                if items or any(s.key == path.stem + ":" + field for s in context.state):
                    save(path.stem + ":" + field, "unresolved", items, "unresolved", sources,
                         active=active and bool(items))
    reviews = sorted((run / "work_reviews").glob("review_*.json"))
    if reviews:
        review_source = source_for(reviews[-1])
        review = json.loads(reviews[-1].read_bytes())
        save("current-work-review", "todo", {"record": review,
            "basis": "latest explicitly saved model review; planned action is not proof of completion"},
            "inferred", review_source, active=bool(review.get("next_action")))
    candidates = sorted(p for p in run.glob("candidate_*/source_model.json") if p.is_file())
    old = next((s for s in context.state if s.key == "current-source-bim"), None)
    selected = old.value["candidate"] if old else None
    from scripts.tool_scripts.bim_agent_saved_result import read_saved_result
    saved = read_saved_result(metadata, tool=event.payload.tool_name, run=run)
    if not raw_result.get("isError"):
        selected = saved["saved_candidate"] or selected
    path = run / str(selected) / "source_model.json" if selected else None
    if path is None or not path.is_file():
        path = candidates[-1] if candidates else run / "seed/source_model.json"
        if not path.is_file():
            path = None
    selection_path = run / "delivery_selection.json"
    if selection_path.is_file():
        selection_source = source_for(selection_path)
        save("selected-source-bim", "artifact_version", json.loads(selection_path.read_bytes()),
            "computed", selection_source)
    if path:
        source = source_for(path)
        saved = json.loads(path.read_bytes())
        save("current-source-bim", "artifact_version", {
            "candidate": path.parent.name, "file": source.blob.model_dump(mode="json"),
            "source_model_sha256": saved.get("source_model_sha256"),
            "source_geometry_sha256": saved.get("source_geometry_sha256")}, "computed", source)
        save("source-bim-geometry", "geometry", {"file": source.blob.model_dump(mode="json"),
            "fields": ["spaces", "boundaries", "openings", "connections"]}, "computed", source)
        save("source-bim-objects", "object_id", {name: [o.get("id") for o in saved.get(name, [])]
            for name in ("spaces", "boundaries", "openings")}, "computed", source)
        save("source-bim-dimensions", "dimension", {"file": source.blob.model_dump(mode="json"),
            "fields": ["spaces", "boundaries", "openings"],
            "basis": "saved values; geometric consistency does not establish observation accuracy"}, "computed", source)
        save("source-bim-assumptions", "general", saved.get("assumptions", []), "assumed", source)
        from scripts.tool_scripts.bim_agent_closeout import closeout_summary, source_blockers
        unresolved = source_blockers(saved)
        unresolved_sources = (source,)
        report_path = path.parent / "report.json"
        report = {}
        if report_path.is_file():
            report_source = source_for(report_path)
            unresolved_sources += (report_source,)
            report = json.loads(report_path.read_bytes())
            unresolved["report_unresolved"] = report.get("unresolved", [])
        else:
            unresolved["report_status"] = "not available"
        has_unresolved = any(unresolved.get(name) for name in (
            "source_validation", "conflicts", "unbuilt_openings", "unsupported", "report_unresolved"))
        manifest_path = run / "inputs.json"
        manifest = json.loads(manifest_path.read_bytes()) if manifest_path.is_file() else {}
        if manifest.get("mesh_input"):
            supplied = metadata.get("closeout")
            review = None
            if (isinstance(supplied, dict) and supplied.get("schema") == "mesh_closeout_v1"
                    and supplied.get("candidate") == path.parent.name
                    and supplied.get("source_model_sha256") == saved.get("source_model_sha256")):
                current_review = supplied.get("mesh_review", {})
                review = {**current_review, "candidate": path.parent.name,
                          "complete_building": current_review.get("current_whole_building_declaration") is True}
            # Recompute machine facts from saved files rather than replaying a
            # tool reply. Do not hash original mesh/images on unrelated tools;
            # absent fresh review evidence is explicitly "not_rechecked".
            closeout = closeout_summary(path.parent.name, saved, report, review=review)
            save("mesh-closeout", "constraint", closeout, "computed", unresolved_sources)
            notes = {"report_unresolved": unresolved.get("report_unresolved", []),
                     "meaning": "Reported unknowns, not accepted omissions or automatic repair tasks; judge user scope."}
            save("source-bim-unresolved", "unresolved", notes, "unresolved", unresolved_sources,
                 active=bool(notes["report_unresolved"]))
            machine = closeout["machine_check"]
            save("source-bim-todo", "todo", {"machine_check": machine,
                "status": "Repair current machine defects locally; clear does not certify requested functionality or fidelity."},
                 "unresolved", unresolved_sources, active=machine["status"] != "clear")
        else:
            save("source-bim-unresolved", "unresolved", unresolved, "unresolved", unresolved_sources,
                 active=has_unresolved)
            save("source-bim-todo", "todo", {"unresolved": unresolved,
                "status": "open items from saved artifacts; empty does not certify whole-case quality"}, "unresolved", unresolved_sources,
                 active=has_unresolved)
        # Explicitly superseded notes retire from the current checklist, even
        # when the older claim record remains immutable in the audit archive.
        proposal_path = path.parent / "proposal.json"
        if proposal_path.is_file():
            proposal = json.loads(proposal_path.read_bytes())
            retired = {row["before"] for row in proposal.get("geometry", {}).get("corrections", [])
                       if row.get("operation") == "replace_note" and row.get("field") == "unresolved"
                       and row.get("before") not in proposal.get("unresolved", [])}
            if retired:
                correction_source = source_for(proposal_path)
                for entry in context.state:
                    if entry.active and entry.category == "unresolved" and isinstance(entry.value, list):
                        remaining = [item for item in entry.value if not isinstance(item, str) or item not in retired]
                        if remaining != entry.value:
                            save(entry.key, entry.category, remaining, entry.epistemic_status,
                                 (*entry.source_refs, correction_source), active=bool(remaining))
    current = next((s.value.get("candidate") for s in context.state
                    if s.active and s.key == "current-source-bim"), None)
    save("context-retrieval", "general", [
        "Original images: view_image(name=<filename from initial input list>, box=<optional original-pixel crop>).",
        f"Saved evidence: claim_status(candidate={current!r}); full history: claim_status().",
        f"Current saved model: inspect_candidate(candidate={current!r}, include_geometry=True)." if current
            else "No saved model yet; inspect_candidate(candidate=<saved candidate>, include_geometry=True) after creating one.",
    ], "computed", engine._event_source(event))
