"""Reuse unchanged coordinator opening checks; never waive delivery or Q2 guards."""

from __future__ import annotations

import hashlib
import json


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _dependencies(run):
    # The frozen check reads source/ancestry, claims, source views/profiles,
    # calibrations and facade counts. Exclude its own generated reports/log rows.
    paths = {run / "inputs.json"}
    for name in ("source_model.json", "proposal.json", "application.json"):
        paths.update(run.glob(f"candidate_*/{name}"))
    for folder in ("claims", "image_views", "pixel_profiles", "overlay_calibrations",
                   "elevation_reviews", "facade_counts", "plan_assemblies"):
        paths.update((run / folder).rglob("*.json"))
    paths.update(path for path in (run / "images").glob("*") if path.is_file())
    files = {path.relative_to(run).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(paths) if path.is_file()}
    log = run / "tools.jsonl"
    # Only original-image observations affect input_view_status. A prior
    # check's timestamp/remaining_seconds must not defeat cache reuse.
    views = []
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("action") == "view_image":
                views.append(row.get("data"))
    return {"files": files, "input_views": views}


async def check_openings(session, arguments):
    candidate = arguments["candidate"]
    session._source(candidate)  # Same admitted-candidate/path check on hits.
    normalized = {"candidate": candidate, "review_json": arguments.get("review_json", ""),
                  "heights_only": arguments.get("heights_only", False)}
    inputs = {"arguments": normalized, "dependencies": _dependencies(session.run_directory)}
    key = hashlib.sha256(_bytes(inputs)).hexdigest()
    relative = "role_opening_checks/" + key + ".json"
    path = session.store.task_directory / relative
    if path.is_file():
        raw = path.read_bytes()
        from scripts.tool_scripts.bim_agent_saved_result import result_metadata
        previous = json.loads(raw)["result"]
        meta = {**result_metadata(previous), **(previous.get("structuredContent") or {})}
        conclusion = {key: meta[key] for key in ("status", "drawing_fidelity", "summary") if key in meta}
        for name in ("height_coverage", "facade_counts"):
            if isinstance(meta.get(name), dict):
                conclusion[name] = {key: meta[name][key] for key in ("status", "summary") if key in meta[name]}
        if isinstance(meta.get("conflicts"), list):
            conclusion["conflict_count"] = len(meta["conflicts"])
        # Retain a compact conclusion for the model after context eviction;
        # the pointer is an auditable role-run artifact, not a new certification.
        value = {"candidate": candidate, "status": "unchanged",
                 "message": "Candidate, check arguments and evidence are unchanged; see the previous conclusion.",
                 "previous_conclusion": conclusion,
                 "previous_result": {"root": "role_run", "file": relative,
                                     "sha256": hashlib.sha256(raw).hexdigest(), "json_pointer": "/result"}}
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value, "isError": False}
    result = await session.frozen.call_tool("check_openings", arguments)
    if not result.get("isError"):
        session.store.write_json(relative, {"schema": "role_opening_check_v1", **inputs, "result": result})
    return result
