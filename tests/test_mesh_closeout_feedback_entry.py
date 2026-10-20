"""Real MCP save-to-finish feedback, without a model or skipped quality checks."""
import asyncio
import hashlib
import json

from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_bim_agent_mesh import _prepare
from tests.test_bim_agent_tools import _server_session
from tests.test_parametric_continuity import continuity, storeys


def _declaration():
    return {"statement": "Synthetic repeated storeys remain simplified.",
            "basis": "simplified", "reason": "Interface fixture, not visual acceptance.",
            "source_refs": ["fixture"], "missing_information": [],
            "object_refs": [{"kind": "space", "id": "STAIR:stair"}]}


def test_save_feedback_and_batched_finish_through_mcp(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    plan = storeys()
    plan["continuous_spaces"] = [continuity()]
    plan["mesh_frame"] = {
        "mesh_sha256": Toolkit(run).manifest["mesh_input"]["sha256"],
        "yaw_degrees": 0, "translation_m": [0, 0, 0],
        "reason": "Synthetic fixture frame", "source_refs": ["fixture"],
    }

    async def scenario():
        async with _server_session(run, readonly=False) as session:
            async def call(name, args):
                response = await session.call_tool(name, args)
                assert not response.isError, response
                value = response.structuredContent or json.loads(
                    next(block.text for block in response.content if block.type == "text"))
                return value, response

            view, _ = await call("view_mesh", {})
            first, first_response = await call("build_parametric_bim", {
                "plan_json": json.dumps(plan), "mesh_observations": [view["observation"]],
            })
            assert first["source_geometry_ready"], first
            assert first["save_facts"]["status"] == "available"
            assert first["edit_route"]["exact_plan"]
            assert first["edit_route"]["recommended_tool"] == "patch_parametric_bim"
            assert len(first["mesh_feedback"]["overlays"]) == 1
            assert not first["mesh_feedback"]["errors"]
            assert any(block.type == "image" for block in first_response.content)
            old_path = run / first["candidate"] / "source_model.json"
            old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
            patched, _ = await call("patch_parametric_bim", {
                "candidate": first["candidate"],
                "expected_plan_sha256": first["parametric"]["plan_sha256"],
                "operations_json": json.dumps([{
                    "op": "add", "path": "/assumptions/-", "value": "Retain the synthetic simplified scope",
                    "reason": "Retain final interpretation together", "source_refs": ["fixture"],
                }]),
                "mesh_observations": [view["observation"], view["observation"]],
                "inferences_json": json.dumps([_declaration()]),
            })
            assert patched["source_geometry_ready"], patched
            assert patched["inference_batch"]["count"] == 1
            assert hashlib.sha256(old_path.read_bytes()).hexdigest() == old_hash
            assert patched["save_facts"]["entity_diff"]["status"] == "compared"
            overlays = patched["mesh_feedback"]["overlays"]
            assert len(overlays) == 1
            assert overlays[0]["candidate"] == patched["candidate"]
            current_path = run / patched["candidate"] / "source_model.json"
            assert overlays[0]["source_file_sha256"] == hashlib.sha256(current_path.read_bytes()).hexdigest()
            assert overlays[0]["overlay_id"] != first["mesh_feedback"]["overlays"][0]["overlay_id"]
            # A new camera can return its current overlay immediately.
            combined, combined_response = await call("view_mesh", {
                "candidate": patched["candidate"], "azimuth_degrees": 135,
            })
            assert sum(block.type == "image" for block in combined_response.content) == 2
            assert combined["candidate_overlay"]["candidate"] == patched["candidate"]
            # Old-source evidence must still fail even though the old shape was identical.
            rejected = await session.call_tool("finish_bim", {
                "candidate": patched["candidate"], "reviews_json": json.dumps({
                    "mesh_review": {"coverage": "whole_building", "summary": "Fixture",
                                    "overlay_ids": [first["mesh_feedback"]["overlays"][0]["overlay_id"]]}}),
            })
            assert rejected.isError
            assert not (run / "delivery_selection.json").exists()
            finished, _ = await call("finish_bim", {
                "candidate": patched["candidate"], "inferences_json": json.dumps([_declaration()]),
                "reviews_json": json.dumps({"mesh_review": {
                    "coverage": "whole_building", "summary": "Synthetic interface only; current views inspected.",
                    "overlay_ids": [overlays[0]["overlay_id"], combined["candidate_overlay"]["overlay_id"]],
                }}),
            })
            assert finished["mesh_review"]["complete_building"]
            assert finished["inference_batch"]["count"] == 1
            assert finished["closeout"]["machine_check"]["status"] == "clear"

    asyncio.run(scenario())


def test_optional_feedback_failure_retains_saved_source(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    plan = storeys()
    plan["continuous_spaces"] = [continuity()]

    async def scenario():
        async with _server_session(run, readonly=False) as session:
            response = await session.call_tool("build_parametric_bim", {
                "plan_json": json.dumps(plan), "mesh_observations": ["missing_observation"],
                "inferences_json": json.dumps([_declaration(), {"invalid": True}]),
            })
            assert not response.isError, response
            saved = response.structuredContent
            assert saved["source_geometry_ready"], saved
            assert saved["saved_candidate"]
            assert saved["mesh_feedback"]["errors"]
            assert not saved["mesh_feedback"]["overlays"]
            assert saved["inference_batch"]["status"] == "rejected"
            assert not list((run / "inferences").glob("inference_*.json"))
            assert (run / saved["candidate"] / "source_model.json").is_file()
            assert not (run / "delivery_selection.json").exists()
            # Contract errors include a bound repair route without another inspect.
            failed = await session.call_tool("revise_bim", {
                "candidate": saved["candidate"], "operations_json": json.dumps([{
                    "op": "update_opening", "id": "F1:door:landing", "changes": {"kind": "window"},
                    "reason": "Synthetic unsupported field", "source_refs": ["fixture"],
                }]),
            })
            assert failed.isError
            assert failed.structuredContent["edit_route"]["exact_plan"]
            assert failed.structuredContent["edit_route"]["targeted_parametric_paths"]
            assert len(list(run.glob("candidate_*/source_model.json"))) == 1

    asyncio.run(scenario())


def test_large_save_reply_keeps_actionable_routes_and_selected_overlay_ids(tmp_path):
    from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report
    overlay_ids = [f"overlay_{index:03d}" for index in range(15)]
    route = {
        "candidate": "candidate_02", "exact_plan": True, "recommended_tool": "patch_parametric_bim",
        "plan_sha256": "a" * 64,
        "patch_arguments": {"candidate": "candidate_02", "expected_plan_sha256": "a" * 64},
        "targeted_parametric_paths": [{
            "object_id": f"F1:door:{index}", "path": f"/templates/typical/doors/{index}",
            "current_declaration": {"notes": "x" * 800},
            "scope": "shared_declaration",
            "affected_object_ids": [f"F{floor}:door:{index}" for floor in range(23)],
            "proposed_patch": {"op": "add", "path": f"/templates/typical/doors/{index}/kind",
                "value": "window", "reason": "Evidence", "source_refs": ["fixture"]},
            "untranslated_changes": ["z"],
        } for index in range(5)],
    }
    result = {
        "candidate": "candidate_02", "source_geometry_ready": True,
        "source_validation": {"status": "pass", "findings": []},
        "edit_route": route, "large_inventory": [{"id": str(i), "note": "x" * 400} for i in range(150)],
        "mesh_feedback": {"overlays": [{
            "candidate": "candidate_02", "source_file_sha256": "b" * 64,
            "observation": f"mesh_{i:03d}", "overlay_id": identity,
            "image_file": f"mesh_overlays/{identity}.png",
        } for i, identity in enumerate(overlay_ids)], "errors": []},
    }
    reply = compact_reply(tmp_path, "build_bim", result)
    assert len(json.dumps(reply).encode()) <= 24000
    assert reply["mesh_feedback"]["overlay_ids"] == overlay_ids
    assert reply["edit_route"]["patch_arguments"] == route["patch_arguments"]
    targets = reply["edit_route"]["targeted_parametric_paths"]
    assert len(targets) == 5
    assert all(row["affected_object_count"] == 23 and row["scope"] == "shared_declaration" for row in targets)
    assert all(row["proposed_patch"]["path"].endswith("/kind") and row["untranslated_changes"] == ["z"]
               for row in targets)
    assert reply["source_validation"]["status"] == "pass"
    full = read_report(tmp_path, reply["details_file"], 0, 12000, json_pointer="/edit_route")
    assert full


def test_repaired_draft_facts_compare_bound_ancestor_without_faking_lineage(tmp_path, monkeypatch):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    from scripts.tool_scripts.bim_agent_save_feedback import attach_save_facts

    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    plan = storeys()
    plan["continuous_spaces"] = [continuity()]
    first = build_parametric(toolkit, json.dumps(plan))
    failed = patch_parametric(toolkit, json.dumps([{
        "op": "remove", "path": "/unresolved", "reason": "Synthetic failure",
        "source_refs": ["fixture"],
    }]), candidate=first["candidate"], expected_sha=first["parametric"]["plan_sha256"])
    assert not failed.get("source_geometry_ready")
    repaired = patch_parametric(toolkit, json.dumps([{
        "op": "add", "path": "/unresolved", "value": [], "reason": "Repair declaration",
        "source_refs": ["fixture"],
    }]), draft=failed["draft"], expected_sha=failed["plan_sha256"])
    assert repaired["source_geometry_ready"], repaired
    diff = repaired["save_facts"]["entity_diff"]
    assert diff["status"] == "compared"
    assert diff["previous_candidate"] == first["candidate"]
    assert diff["comparison_origin"] == "bound_feedback_ancestor"
    source = json.loads((run / repaired["candidate"] / "source_model.json").read_bytes())
    assert "parent_candidate" not in source.get("generation", {})
    # A changed ancestor cannot be silently relabelled as an initial build.
    ancestor = repaired["parametric"]["feedback_parent"]
    (run / first["candidate"] / "parametric_plan.json").write_text("{}")
    attach_save_facts(toolkit, repaired, feedback_parent=ancestor)
    assert repaired["save_facts"]["entity_diff"]["status"] == "unavailable"
    recovered_without_ancestor = patch_parametric(toolkit, json.dumps([{
        "op": "add", "path": "/unresolved", "value": [], "reason": "Repair after stale ancestor",
        "source_refs": ["fixture"],
    }]), draft=failed["draft"], expected_sha=failed["plan_sha256"])
    assert recovered_without_ancestor["source_geometry_ready"], recovered_without_ancestor
    assert recovered_without_ancestor["parametric"]["feedback_parent_unavailable"]
    unavailable = recovered_without_ancestor["save_facts"]["entity_diff"]
    assert unavailable["status"] == "unavailable"
    assert unavailable["comparison_origin"] == "unavailable_feedback_ancestor"
