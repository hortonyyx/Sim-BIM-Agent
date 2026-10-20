"""Real MCP saves, local continuity repair and evidence-bound finish, no model."""
import asyncio
import copy
import json

from scripts.tool_scripts.bim_agent_regularization import classify_source_precision
from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_bim_agent_mesh import _prepare
from tests.test_bim_agent_tools import _server_session
from tests.test_parametric_continuity import continuity, storeys
from tests.test_source_shape_gate import cell


def test_mesh_local_closeout_through_mcp_preserves_accepted_shape(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    plan = storeys()
    plan["unresolved"] = ["Unobserved equipment detail is retained as unknown."]
    plan["mesh_frame"] = {
        "mesh_sha256": Toolkit(run).manifest["mesh_input"]["sha256"],
        "yaw_degrees": 0, "translation_m": [0, 0, 0],
        "reason": "Synthetic interface check, not fidelity evidence", "source_refs": ["fixture"],
    }

    async def scenario():
        async with _server_session(run, readonly=False) as session:
            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, result
                if result.structuredContent is not None:
                    return result.structuredContent
                return json.loads(next(block.text for block in result.content if block.type == "text"))

            first = await call("build_parametric_bim", {"plan_json": json.dumps(plan)})
            assert first["source_geometry_ready"], first
            assert first["closeout"]["machine_check"]["status"] == "clear"
            assert first["closeout"]["retained_notes"]["count"] == 1
            before = json.loads((run / first["candidate"] / "source_model.json").read_bytes())
            inspection = await call("inspect_parametric_plan", {"candidate": first["candidate"]})
            patched = await call("patch_parametric_bim", {
                "candidate": first["candidate"], "expected_plan_sha256": inspection["plan_sha256"],
                "operations_json": json.dumps([{
                    "op": "add", "path": "/continuous_spaces", "value": [continuity()],
                    "reason": "Remove intermediate stair caps without changing the other rooms",
                    "source_refs": ["explicit fixture continuity"],
                }]),
            })
            assert patched["source_geometry_ready"], patched
            candidate = patched["candidate"]
            after = json.loads((run / candidate / "source_model.json").read_bytes())
            other = lambda source: [row for row in source["spaces"] if not row["id"].endswith(":stair")]
            assert other(before) == other(after)
            aperture = lambda row: {key: value for key, value in row.items()
                                    if key in {"id", "vertices", "kind"}}
            assert [aperture(row) for row in before["openings"]] == [
                aperture(row) for row in after["openings"]]
            caps = [row for row in after["boundaries"]
                    if row["space_id"] == "STAIR:stair" and row["geometry_type"] != "wall"]
            assert len(caps) == 2
            current = await call("inspect_candidate", {"candidate": candidate, "include_geometry": False})
            assert current["closeout"]["machine_check"]["status"] == "clear"
            assert current["closeout"]["retained_notes"]["status"] == "reported_unknown_not_accepted"
            view = await call("view_mesh", {})
            observation = view["observation"]
            overlay = await call("overlay_mesh_candidate", {"candidate": candidate, "observation": observation})
            overlay_id = overlay["overlay_id"]
            finished = await call("finish_bim", {"candidate": candidate, "reviews_json": json.dumps({
                "mesh_review": {"coverage": "whole_building",
                    "summary": "Synthetic plumbing only: declared scope complete; unknown equipment retained.",
                    "overlay_ids": [overlay_id]}})})
            assert finished["mesh_review"]["complete_building"]
            assert finished["closeout"]["mesh_review"]["current_whole_building_declaration"]
            assert finished["closeout"]["retained_notes"]["count"] == 1
            assert finished["closeout"]["mesh_review"]["fidelity"] == "not_evaluated"
    asyncio.run(scenario())


def _contact_partition_proposal():
    def floor(name, z, left, right, cells):
        return {"name": name, "z_floor": z, "ceiling_height": 3,
                "footprint": {"vertices": [[left, 0], [right, 0], [right, 6], [left, 6]]},
                "cells": cells}
    return {"geometry": {"schema_version": "source_polygon_v1", "openings": [], "windows": [],
        "floors": [
            floor("A", 0, 0, 6, [cell("left", [[0, 0], [3, 0], [3, 6], [0, 6]]),
                                cell("right", [[3, 0], [6, 0], [6, 6], [3, 6]])]),
            floor("B", 3, -4, -1, [cell("separate_wing", [[-4, 0], [-1, 0], [-1, 6], [-4, 6]])]),
            floor("C", 3, 2.9, 6, [cell("roof", [[2.9, 0], [6, 0], [6, 6], [2.9, 6]])]),
        ]}, "assumptions": ["Synthetic separate wing and roof contact partition"], "unresolved": []}


def test_mesh_thin_contact_is_visible_warning_not_shape_rework(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    result = Toolkit(run).build(_contact_partition_proposal())
    assert result["source_geometry_ready"], result
    precision = result["building_precision"]
    assert precision["items"] == []
    assert precision["warning_counts"] == {"thin_horizontal_contact": 1}
    assert precision["warnings"][0]["width_m"] == .1
    assert result["closeout"]["machine_check"]["status"] == "clear"
    source = json.loads((run / result["candidate"] / "source_model.json").read_bytes())
    assert next(s for s in source["spaces"] if s["id"] == "roof")["polygon"][0] == [2.9, 0.0]


def test_contact_policy_preserves_drawing_rules_and_other_mesh_blockers():
    from src.agent.geometry.building_precision import precision_report
    from tests.test_building_precision import source
    report = precision_report(source(.1))
    original = copy.deepcopy(report)
    for manifest in ({}, {"mesh_input": {"sha256": "fixture"}}):
        assert classify_source_precision(report, manifest) == original
    result = classify_source_precision(report, {
        "mesh_input": {"sha256": "fixture"}, "architectural_modelling_policy": {"schema_version": "1"}})
    assert result["status"] == "rejected"
    assert result["counts"] == {"storey_wall_offset": 1}
    assert result["warning_counts"] == {"thin_horizontal_contact": 1}
    assert report == original


def test_long_delivery_preserves_closeout_before_generic_compaction(tmp_path):
    from scripts.tool_scripts.bim_agent_closeout import closeout_summary
    from scripts.tool_scripts.bim_agent_replies import compact_reply
    from scripts.tool_scripts.run_bim_agent import delivery_tool_reply
    from tests.test_mesh_closeout import _saved
    source, report = _saved(notes=["Unobserved equipment"])
    closeout = closeout_summary("candidate_01", source, report)
    full = {"candidate": "candidate_01", "closeout": closeout,
            "generation": {"unresolved": ["Long stated uncertainty " * 1000] * 30},
            "generation_status": {"state": "completed"}, "source_validation": source["validation"]}
    delivery = delivery_tool_reply(full)
    assert delivery["detail_level"] == "counts_only"
    assert delivery["closeout"] == closeout
    compact = compact_reply(tmp_path, "finish_bim", delivery, full_result=full, force=True)
    assert compact["closeout"] == closeout
    assert json.loads((tmp_path / compact["details_file"]).read_bytes()) == full
