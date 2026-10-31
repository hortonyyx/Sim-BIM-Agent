"""Finish saved BIMs with T1's shared selection and delivery implementation."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from scripts.tool_scripts.bim_agent_budget import fallback_selection
from scripts.tool_scripts.run_bim_agent import Toolkit


def _delivery_scope(manifest: dict, delivery: dict) -> dict:
    """Keep drawing coverage and model-reported mesh scope distinguishable."""
    if manifest.get("mesh_input"):
        review = delivery.get("mesh_review", {})
        return {
            "complete_building": (review.get("status") == "current"
                and review.get("coverage") == "whole_building"
                and review.get("complete_building") is True),
            "scope_basis": "mesh_model_reported_review",
            "mesh_review": {key: review[key] for key in
                ("status", "coverage", "fidelity", "review_file", "record_sha256") if key in review},
        }
    return {"complete_building": delivery["floor_completeness"]["complete_building"]}


def attest_finished_handoff(run: Path, result: dict, candidate: str) -> dict | None:
    """Verify one explicit finish's saved artifacts for any runtime adapter.

    This reads evidence only; it cannot select, finish, repair or approve a BIM.
    The caller must supply the result of its actual successful finish call.
    """
    from scripts.tool_scripts.bim_agent_saved_result import full_result_metadata
    from src.agent.geometry.source_model import _digest
    try:
        run = Path(run).resolve()
        result = full_result_metadata({"structuredContent": result}, run)
        if (not isinstance(candidate, str) or Path(candidate).name != candidate
                or candidate in {".", ".."} or result.get("candidate") != candidate):
            return None
        expected = result.get("delivery_artifact_sha256", {})
        manifest = json.loads((run / "inputs.json").read_bytes())
        names = {"inputs.json", "delivery_selection.json", "delivery.json", "delivery.html",
                 f"{candidate}/source_model.json", f"{candidate}/viewer.html"}
        if manifest.get("mesh_input") and (run / candidate / "mesh_review.json").is_file():
            names.add(f"{candidate}/mesh_review.json")
        if set(expected) != names or any(not (run / name).resolve().is_relative_to(run) for name in names):
            return None
        contents = {name: (run / name).read_bytes() for name in names}
        if any(hashlib.sha256(contents[name]).hexdigest() != expected[name] for name in names):
            return None
        selection = json.loads(contents["delivery_selection.json"])
        delivery = json.loads(contents["delivery.json"])
        source = json.loads(contents[f"{candidate}/source_model.json"])
        source_hash = _digest({k: v for k, v in source.items() if k != "source_model_sha256"})
        if (source.get("schema_version") not in {"source_bim_v2", "source_bim_v3"}
                or any(row.get("source_model_sha256") != source_hash for row in (source, selection, delivery, result))
                or any(row.get("candidate") != candidate for row in (selection, delivery))
                or delivery.get("selection_origin") != "agent_selected"
                or delivery.get("source_validation", {}).get("status") != "pass"
                or delivery.get("viewer") != f"{candidate}/viewer.html"
                or delivery.get("source_model") != f"{candidate}/source_model.json"
                or not delivery.get("viewer_exists")):
            return None
        scope = _delivery_scope(manifest, delivery)
        if scope["complete_building"] is not True:
            return None
        if manifest.get("mesh_input"):
            from scripts.tool_scripts.bim_agent_mesh_review import mesh_review_status
            if mesh_review_status(Toolkit(run), candidate) != delivery.get("mesh_review"):
                return None
        return {"origin": "domain_tool", "tool_name": "finish_bim", "candidate": candidate,
                "source_model_sha256": source_hash, "artifact_sha256": expected,
                **({"scope_basis": scope["scope_basis"], "mesh_review": scope["mesh_review"]}
                   if manifest.get("mesh_input") else {})}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def completed_building_handoff(engine) -> dict | None:
    """Attest the last explicit finish against the exact files it handed off.

    The single-model and role coordinator entrypoints opt into this hook.
    Other MCP clients may keep working after finish_bim. A later write, failed/unknown finish,
    incomplete building, or changed artifact never becomes task completion.
    """
    if engine.role.read_only:
        return None
    event = next((e for e in reversed(engine.store.events)
        if e.payload.event_type == "tool_execution" and e.payload.repeatability != "read_only"), None)
    if event is None or event.payload.tool_name != "finish_bim" or event.payload.outcome != "succeeded":
        return None
    from scripts.tool_scripts.bim_agent_saved_result import result_metadata
    try:
        raw = engine.store.resolve(event.payload.raw_result)
        if raw.get("isError"):
            return None
        attested = attest_finished_handoff(engine.tools.run_directory, result_metadata(raw),
            event.payload.full_arguments.get("candidate"))
        return {**attested, "event_id": event.event_id} if attested is not None else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def finalize_building(run: Path, *, reason: str, elapsed_seconds: float) -> dict:
    """Mirror the subscription runner's timeout/selection precedence.

    Time or CNY budget stops prefer the latest complete building, even if a
    later partial candidate was selected. Other stops preserve a selection.
    Completeness, geometry checks and HTML all come from the shared T1 code.
    """
    timed_out = reason.endswith("time_budget_exhausted")
    money_stopped = reason.endswith("money_budget_exhausted") or reason == "money_cny_usage_unavailable"
    generation = {"state": "completed" if reason == "completed" else "interrupted",
        "agent_response_completed": reason == "completed", "timed_out": timed_out,
        "elapsed_seconds": elapsed_seconds, "runtime_stop_reason": reason}
    try:
        toolkit = Toolkit(run)
        selection = run / "delivery_selection.json"
        if selection.is_file() and not (timed_out or money_stopped):
            chosen = json.loads(selection.read_bytes())["candidate"]
            origin = "agent_selected"
        else:
            chosen, origin = fallback_selection(toolkit)
        if chosen is None:
            return {"status": "no_saved_candidate", "delivery": None}
        delivery = toolkit.delivery(chosen, selection_origin=origin, generation_status=generation)
        return {"status": "delivered", "delivery": {
            "candidate": chosen, "selection_origin": origin,
            **_delivery_scope(toolkit.manifest, delivery),
            "report": "bim/delivery.json", "viewer": "bim/delivery.html"}}
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Keep the stop reason and saved sources inspectable if handoff fails.
        return {"status": "delivery_failed", "delivery": None,
                "error_type": type(error).__name__, "error": str(error)}


def finalize_runtime_building(engine, reason: str) -> dict:
    if engine.role.read_only:
        return {"status": "readonly_no_building_delivery", "delivery": None}
    completion = getattr(engine, "completion", None)
    if completion is not None or getattr(engine, "domain_completion", None) is completed_building_handoff:
        verified = completed_building_handoff(engine)
        if completion is not None and verified != completion:
            return {"status": "delivery_failed", "delivery": None, "completion_valid": False,
                    "error": "The completed handoff artifacts changed; no fallback replaced the selection."}
        if verified is not None:
            # A later read in the same batch may time out. Preserve the explicit
            # selection without claiming that the unfinished batch completed.
            return {"status": "delivered", "reused_handoff": True, "completion_valid": True, "delivery": {
                "candidate": verified["candidate"], "selection_origin": "agent_selected",
                "complete_building": True,
                **({"scope_basis": verified["scope_basis"], "mesh_review": verified["mesh_review"]}
                   if "scope_basis" in verified else {}),
                "report": "bim/delivery.json", "viewer": "bim/delivery.html"}}
    return finalize_building(engine.tools.run_directory, reason=reason,
        elapsed_seconds=engine.elapsed_seconds())
