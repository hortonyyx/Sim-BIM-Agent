"""Explicit continuity repairs retain accepted geometry and real apertures."""
import copy
import json

import pytest

from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.parametric_proposal import expand_parametric_proposal


def storeys():
    return {
        "templates": {"typical": {
            "footprint": [[0, 0], [6, 0], [6, 4], [0, 4]],
            "spaces": [
                {"id": "stair", "role": "stairwell", "rect": [0, 0, 2, 4], "source_refs": ["declared stair"]},
                {"id": "room", "role": "office", "rect": [2, 0, 6, 4], "source_refs": ["declared room"]}],
            "doors": [{"id": "landing", "space": "stair", "other_space": "room",
                       "p1": [2, 1], "p2": [2, 2], "z": [0, 2.1], "source_refs": ["landing"]}],
            "window_rows": [{"id": "west", "facade": "West", "plane": 0,
                             "spans": [[1, 2]], "z": [0.8, 2.2], "source_refs": ["window"]}]}},
        "instances": [],
        "repeats": [{"id": "storeys", "template": "typical", "id_prefix": "F", "start": 1, "count": 3,
                     "z": 0, "height": 3, "source_refs": ["explicit three storeys"]}],
        "assumptions": [], "unresolved": [],
    }


def continuity():
    return {"id": "STAIR", "space": "stair", "repeat_ids": ["storeys"],
            "source_refs": ["stair is explicitly continuous through these storeys"]}


def test_continuity_preserves_world_geometry_apertures_and_other_spaces(tmp_path):
    plan = storeys()
    original = expand_parametric_proposal(plan)
    plan["continuous_spaces"] = [continuity()]
    before = copy.deepcopy(plan)
    audit = {}
    proposal = expand_parametric_proposal(plan, expansion_audit=audit)
    assert plan == before
    floors = {floor["name"]: floor for floor in proposal["geometry"]["floors"]}
    for old in original["geometry"]["floors"]:
        new = floors[old["name"]]
        assert new["footprint"] == old["footprint"]
        assert new["z_floor"] == old["z_floor"] and new["ceiling_height"] == old["ceiling_height"]
        assert new["cells"] == [old["cells"][1]]
        assert new["spanning_space_ids"] == ["STAIR:stair"]
    for old, new in zip(original["geometry"]["openings"], proposal["geometry"]["openings"]):
        assert {k: v for k, v in new.items() if k != "space_id"} == {
            k: v for k, v in old.items() if k != "space_id"}
        assert new["space_id"] == "STAIR:stair"
    for old, new in zip(original["geometry"]["windows"], proposal["geometry"]["windows"]):
        assert {k: v for k, v in new.items() if k not in ("room", "floor")} == {
            k: v for k, v in old.items() if k not in ("room", "floor")}
        assert (new["room"], new["floor"]) == ("STAIR:stair", "STAIR")
    assert audit["continuous_spaces"][0]["member_space_ids"] == ["F1:stair", "F2:stair", "F3:stair"]
    report = export_source_proposal(proposal, tmp_path / "joined")
    assert report["source_geometry_ready"], report
    source = json.loads((tmp_path / "joined/source_model.json").read_bytes())
    core = next(space for space in source["spaces"] if space["id"] == "STAIR:stair")
    assert core["height"] == 9
    caps = [b for b in source["boundaries"] if b["space_id"] == core["id"] and b["geometry_type"] != "wall"]
    assert len(caps) == 2
    assert {round(p[2], 9) for b in caps for p in b["vertices"]} == {0, 9}
    assert len(source["openings"]) == 6 and len(source["connections"]) == 3
    by_id = {b["id"]: b for b in source["boundaries"]}
    for opening in source["openings"]:
        assert {by_id[bid]["space_id"] for bid in source["opening_hosts"][opening["id"]]} == set(opening["space_ids"])


def test_explicit_subset_does_not_absorb_unselected_storey_or_infer_service():
    plan = storeys()
    baseline = expand_parametric_proposal(plan)
    declaration = continuity()
    declaration.pop("repeat_ids")
    declaration["instance_ids"] = ["F2", "F1"]
    plan["continuous_spaces"] = [declaration]
    proposal = expand_parametric_proposal(plan)
    assert proposal["geometry"]["floors"][2] == baseline["geometry"]["floors"][2]
    assert proposal["geometry"]["floors"][-1]["ceiling_height"] == 6
    assert proposal["geometry"]["openings"][2] == baseline["geometry"]["openings"][2]
    assert proposal["geometry"]["windows"][2] == baseline["geometry"]["windows"][2]


def test_repeat_exception_is_not_implicitly_merged():
    plan = storeys()
    plan["repeats"][0]["exclude_ids"] = ["F3"]
    plan["instances"] = [{"id": "F3", "template": "typical", "z": 6, "height": 3}]
    before = expand_parametric_proposal(plan)
    plan["continuous_spaces"] = [continuity()]
    proposal = expand_parametric_proposal(plan)
    assert proposal["geometry"]["floors"][0] == before["geometry"]["floors"][0]
    assert proposal["geometry"]["floors"][-1]["ceiling_height"] == 6


@pytest.mark.parametrize("failure", ["shape", "role", "role_evidence", "gap", "overlap", "unknown",
    "duplicate_selection", "duplicate_declaration", "existing_id", "single", "already_spanning", "self_connection"])
def test_ambiguous_or_shape_changing_continuity_is_rejected(failure):
    plan = storeys()
    plan["instances"] = [{"id": f"F{i}", "template": "typical", "z": (i - 1) * 3, "height": 3}
                         for i in range(1, 4)]
    plan.pop("repeats")
    declaration = continuity()
    declaration.pop("repeat_ids")
    declaration["instance_ids"] = ["F1", "F2", "F3"]
    plan["continuous_spaces"] = [declaration]
    if failure in ("shape", "role", "role_evidence"):
        plan["templates"]["other"] = copy.deepcopy(plan["templates"]["typical"])
        plan["instances"][1]["template"] = "other"
        space = plan["templates"]["other"]["spaces"][0]
        if failure == "shape":
            space["rect"][0] = 0.01
        elif failure == "role":
            space["role"] = "shaft"
        else:
            space["role_evidence"] = {"role": "stairwell", "basis": "inferred",
                                      "source_refs": ["inferred"], "assumptions": []}
    elif failure in ("gap", "overlap"):
        plan["instances"][1]["z"] += 0.01 if failure == "gap" else -0.01
    elif failure == "unknown":
        declaration["instance_ids"][-1] = "unknown"
    elif failure == "duplicate_selection":
        declaration["instance_ids"].append("F1")
    elif failure == "duplicate_declaration":
        plan["continuous_spaces"].append({**copy.deepcopy(declaration), "id": "STAIR2"})
    elif failure == "existing_id":
        declaration["id"] = "F1"
    elif failure == "single":
        declaration["instance_ids"] = ["F1"]
    elif failure == "already_spanning":
        plan["instances"][1]["spanning_space_ids"] = ["F1:stair"]
    else:
        plan["connections"] = [{"id": "not_discardable", "space_id": "F1:stair", "other_space_id": "F2:stair",
                                "p1": [0, 0], "p2": [1, 0], "z": [1, 2], "kind": "door"}]
    before = copy.deepcopy(plan)
    with pytest.raises(ValueError):
        expand_parametric_proposal(plan)
    assert plan == before


def test_existing_core_service_declarations_can_target_explicit_continuity(tmp_path):
    plan = storeys()
    plan["templates"]["typical"].pop("doors")
    plan["continuous_spaces"] = [continuity()]
    plan["core_connections"] = [{"id": "landing", "core_space_id": "STAIR:stair", "space": "room",
        "repeat_ids": ["storeys"], "p1": [2, 1], "p2": [2, 2], "z": [0, 2.1],
        "source_refs": ["explicit served storeys"]}]
    proposal = expand_parametric_proposal(plan)
    report = export_source_proposal(proposal, tmp_path / "served")
    assert report["source_geometry_ready"], report
    assert [o["id"] for o in proposal["geometry"]["openings"]] == [f"F{i}:core:landing" for i in range(1, 4)]


def test_sha_bound_patch_adds_one_continuity_declaration_and_preserves_base(tmp_path):
    from tests.test_bim_agent_tools import _ParametricFunctionToolkit, _parametric_file_hashes
    toolkit = _ParametricFunctionToolkit(tmp_path / "run")
    first = build_parametric(toolkit, json.dumps(storeys()))
    before = _parametric_file_hashes(toolkit.run)
    inspected = inspect_parametric(toolkit, first["candidate"])
    operations = json.dumps([{"op": "add", "path": "/continuous_spaces", "value": [continuity()],
                              "reason": "remove artificial intermediate caps", "source_refs": ["explicit continuity"]}])
    rejected = patch_parametric(toolkit, operations, candidate=first["candidate"], expected_sha="0" * 64)
    assert rejected["error"] and len(toolkit.calls) == 1
    second = patch_parametric(toolkit, operations, candidate=first["candidate"], expected_sha=inspected["plan_sha256"])
    assert second["source_geometry_ready"]
    assert all(_parametric_file_hashes(toolkit.run)[key] == value for key, value in before.items())
    assert second["parametric"]["expansion_audit"]["continuous_spaces"][0]["member_space_ids"] == [
        "F1:stair", "F2:stair", "F3:stair"]
    assert second["parametric"]["base"]["plan_sha256"] == inspected["plan_sha256"]
    assert inspect_parametric(toolkit, second["candidate"])["exact_plan"]


def test_mesh_policy_build_preserves_continuity_and_new_core_services(tmp_path, monkeypatch):
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from tests.test_bim_agent_mesh import _prepare
    _, run, _ = _prepare(tmp_path, monkeypatch)
    plan = storeys()
    plan["templates"]["typical"].pop("doors")
    plan["continuous_spaces"] = [continuity()]
    plan["core_connections"] = [{"id": "landing", "core_space_id": "STAIR:stair", "space": "room",
        "repeat_ids": ["storeys"], "p1": [2, 1], "p2": [2, 2], "z": [0, 2.1],
        "source_refs": ["explicit served storeys"]}]
    built = build_parametric(Toolkit(run), json.dumps(plan))
    assert built["source_geometry_ready"], built
    source = json.loads((run / built["candidate"] / "source_model.json").read_bytes())
    assert len(source["connections"]) == 3
    assert built["parametric"]["expansion_audit"]["continuous_spaces"][0]["space_id"] == "STAIR:stair"
    assert inspect_parametric(Toolkit(run), built["candidate"])["exact_plan"]


def test_adoption_checks_every_continuous_member_even_when_only_one_is_served():
    from src.agent.geometry.architectural_regularization import (
        ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY, regularize_architectural_plan)
    plan = storeys()
    plan["templates"]["typical"].pop("doors")
    plan["templates"]["other"] = copy.deepcopy(plan["templates"]["typical"])
    plan["instances"] = [{"id": "F1", "template": "typical", "z": 0, "height": 3},
                         {"id": "F2", "template": "other", "z": 3, "height": 3}]
    plan.pop("repeats")
    plan["continuous_spaces"] = [{"id": "STAIR", "space": "stair", "instance_ids": ["F1", "F2"],
                                  "source_refs": ["explicit continuous members"]}]
    plan["core_connections"] = [{"id": "landing", "core_space_id": "STAIR:stair", "space": "room",
        "instance_ids": ["F1"], "p1": [2, 1], "p2": [2, 2], "z": [0, 2.1],
        "source_refs": ["only first storey is declared served"]}]
    policy = {**DEFAULT_ARCHITECTURAL_POLICY,
              "frames": [{"id": "shifted", "origin": [0.04, 0], "yaw_degrees": 0, "templates": ["other"]}]}
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(plan, policy=policy)
    assert any(row["code"] == "cross_template_host_conflict" for row in caught.value.audit["rejections"])


@pytest.mark.parametrize("offset", [-1e-8, 1e-8, -2e-7, 2e-7])
def test_continuity_uses_source_numerical_touching_without_moving_other_spaces(offset):
    plan = storeys()
    plan.pop("repeats")
    plan["instances"] = [{"id": "F1", "template": "typical", "z": 0, "height": 3},
                         {"id": "F2", "template": "typical", "z": 3 + offset, "height": 3}]
    before = expand_parametric_proposal(plan)
    plan["continuous_spaces"] = [{"id": "STAIR", "space": "stair", "instance_ids": ["F1", "F2"],
                                  "source_refs": ["explicit continuous core"]}]
    if abs(offset) > 1e-7:
        with pytest.raises(ValueError, match="gap or overlap"):
            expand_parametric_proposal(plan)
    else:
        after = expand_parametric_proposal(plan)
        assert after["geometry"]["floors"][-1]["ceiling_height"] == 6 + offset
        for old, new in zip(before["geometry"]["floors"], after["geometry"]["floors"]):
            assert old["cells"][1] == new["cells"][0]
            assert old["z_floor"] == new["z_floor"]
            assert old["ceiling_height"] == new["ceiling_height"]
