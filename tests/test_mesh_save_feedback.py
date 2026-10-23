"""Saved-current facts and bundled inference records preserve quality boundaries."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.tool_scripts import bim_agent_inference as inference
from scripts.tool_scripts.bim_agent_save_feedback import attach_save_facts
from tests.test_bim_agent_inference import _Toolkit, _rehash, _run, _source


def _toolkit(tmp_path):
    toolkit = _Toolkit(_run(tmp_path))
    toolkit.manifest = {"mesh_input": {"sha256": "synthetic"}}
    return toolkit


def _result(toolkit, candidate="candidate_02"):
    source = json.loads((toolkit.candidate_path(candidate) / "source_model.json").read_bytes())
    return {"candidate": candidate, "saved_candidate": candidate, "source_geometry_ready": True,
            "source_model_sha256": source["source_model_sha256"]}


def _save(toolkit, candidate, source):
    path = toolkit.candidate_path(candidate) / "source_model.json"
    path.write_text(json.dumps(_rehash(source), indent=2), encoding="utf-8")


def _declaration(identity="A"):
    return {"statement": "Keep the saved spatial interpretation.", "basis": "inferred",
            "reason": "Observed external form does not resolve internal use.", "source_refs": [],
            "missing_information": ["interior survey"],
            "object_refs": [{"kind": "space", "id": identity}]}


def test_save_feedback_reports_diff_and_unchanged_opening_on_changed_host(tmp_path):
    toolkit = _toolkit(tmp_path)
    source = _source(2)
    source["boundaries"][-1]["vertices"][1][0] = 5.2
    source["opening_hosts"] = {row["id"]: [row["host_boundary_id"]] for row in source["openings"]}
    source["validation"] = {"status": "warning", "findings": [
        {"code": "synthetic", "severity": "warning", "opening_id": "wU"}]}
    _save(toolkit, "candidate_02", source)
    protected = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in toolkit.run.glob("candidate_*/source_model.json")}
    result = attach_save_facts(toolkit, _result(toolkit), parent="candidate_01")
    facts = result["save_facts"]
    assert facts["status"] == "available"
    full = json.loads((toolkit.run / facts["facts_file"]).read_bytes())
    assert full["entity_diff"]["entities"]["spaces"]["changed"]["ids"] == ["A"]
    assert full["entity_diff"]["entities"]["spaces"]["preserved"]["ids"] == ["U"]
    affected = {row["id"] for row in full["affected_opening_hosts"]["openings"]}
    assert "wU" in affected  # same aperture, changed boundary
    audit = json.loads((toolkit.run / facts["audit_file"]).read_bytes())
    assert "wU" in audit["comparison"]["entities"]["windows"]["preserved_ids"]
    assert facts["saved_source_check"]["status"] == "warning"
    assert facts["saved_source_check"]["finding_counts_by_severity"] == {"warning": 1}
    assert facts["source_model_sha256"] == _result(toolkit)["source_model_sha256"]
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in protected.items())
    assert result["source_geometry_ready"] is True
    assert "delivery acceptance" in facts["scope"]
    from scripts.tool_scripts.bim_agent_replies import read_report
    readable = read_report(toolkit.run, facts["details_file"], 0, 12000)
    assert readable["sha256"] == facts["details_sha256"]
    assert len(json.dumps(facts, ensure_ascii=False).encode()) <= 3000


def test_saved_continuous_volume_and_actual_doors_reuse_existing_check(tmp_path, monkeypatch):
    from src.agent.execution.source_proposal import export_source_proposal
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal
    from src.agent.geometry import source_vertical_continuity
    from tests.test_parametric_continuity import storeys, continuity

    run = tmp_path / "run"
    toolkit = _Toolkit(run)
    toolkit.manifest = {"mesh_input": {"sha256": "synthetic"}}
    plan = storeys()
    plan["templates"]["typical"].pop("doors")
    plan["continuous_spaces"] = [continuity()]
    plan["core_connections"] = [{"id": "landing", "core_space_id": "STAIR:stair", "space": "room",
        "repeat_ids": ["storeys"], "p1": [2, 1], "p2": [2, 2], "z": [0, 2.1],
        "source_refs": ["explicit served storeys"]}]
    report = export_source_proposal(expand_parametric_proposal(plan), run / "candidate_01",
                                    vertical_continuity_policy="explicit_stack_v1")
    assert report["source_geometry_ready"], report

    def no_recheck(*args, **kwargs):
        pytest.fail("save feedback must not rerun the kernel continuity check")
    monkeypatch.setattr(source_vertical_continuity, "review_vertical_continuity", no_recheck)
    result = attach_save_facts(toolkit, _result(toolkit, "candidate_01"))
    facts = result["save_facts"]
    assert facts["status"] == "available", facts
    cores = facts["continuous_space_facts"]
    assert cores["declared_spanning_volume_count"] == 1
    assert cores["actual_door_connection_count"] == 3
    assert cores["saved_vertical_continuity_review"]["status"] == "resolved"
    full = json.loads((run / facts["facts_file"]).read_bytes())
    core = full["continuous_space_facts"]["volumes"][0]
    assert core["space_id"] == "STAIR:stair"
    assert core["height_m"] == 9
    assert core["declared_floor_ids"] == ["F1", "F2", "F3"]
    assert {tuple(row["adjacent_floor_ids"]) for row in core["door_connections"]} == {
        ("F1",), ("F2",), ("F3",)}
    assert {row["bottom_z_m"] for row in core["door_connections"]} == {0, 3, 6}
    assert all(row["connection_record_count"] == 1 for row in core["door_connections"])
    assert facts["affected_opening_hosts"]["missing_host_count"] == 0


def test_invalid_current_source_feedback_does_not_erase_saved_candidate(tmp_path):
    toolkit = _toolkit(tmp_path)
    result = _result(toolkit)
    source = _source(2)
    source["spaces"][0]["role"] = "tampered"
    (toolkit.candidate_path("candidate_02") / "source_model.json").write_text(json.dumps(source))
    before = copy.deepcopy(result)
    attach_save_facts(toolkit, result, parent="candidate_01")
    assert result["save_facts"]["status"] == "unavailable"
    assert {key: value for key, value in result.items() if key != "save_facts"} == before
    assert not list(toolkit.run.glob("inference_audits/*.json"))


def test_missing_parent_keeps_current_facts_without_claiming_no_changes(tmp_path):
    toolkit = _toolkit(tmp_path)
    result = attach_save_facts(toolkit, _result(toolkit), parent="candidate_99")
    facts = result["save_facts"]
    assert facts["status"] == "available"
    assert facts["entity_diff"]["status"] == "unavailable"
    assert facts["affected_opening_hosts"]["count"] == len(_source(2)["openings"])
    assert facts["saved_source_check"]["status"] == "unavailable"
    assert facts["continuous_space_facts"]["saved_vertical_continuity_review"]["status"] == "unavailable"


def test_nonmesh_save_does_not_create_optional_feedback(tmp_path):
    toolkit = _toolkit(tmp_path)
    toolkit.manifest = {}
    result = _result(toolkit)
    before = copy.deepcopy(result)
    assert attach_save_facts(toolkit, result) == before
    assert not list(toolkit.run.glob("inference_audits/*.json"))


def test_all_inferences_validate_before_any_record_or_log_is_written(tmp_path):
    toolkit = _toolkit(tmp_path)
    with pytest.raises(ValueError, match="unknown space"):
        inference.record_inferences(toolkit, json.dumps([
            _declaration(), _declaration("missing")]), "candidate_01")
    assert not list(toolkit.run.glob("inferences/*.json"))
    assert toolkit.events == []
    result = inference.record_inferences(toolkit, json.dumps([
        _declaration(), _declaration("U")]), "candidate_01")
    assert result["count"] == 2
    binding = result["candidate_binding"]
    assert binding["source_model_sha256"] == _source(1)["source_model_sha256"]
    assert all(row["candidate_binding"] == binding for row in result["records"])
    assert len(list(toolkit.run.glob("inferences/*.json"))) == 2
    for record in result["records"]:
        assert inference.inspect_inference(toolkit, record["inference_id"])["binding_status"]["status"] == "current"


@pytest.mark.parametrize("value", [{}, [], "wrong", [None]])
def test_inference_batch_rejects_malformed_input_without_partial_writes(tmp_path, value):
    toolkit = _toolkit(tmp_path)
    with pytest.raises(ValueError):
        inference.record_inferences(toolkit, json.dumps(value), "candidate_01")
    assert not list(toolkit.run.glob("inferences/*.json"))


def test_source_change_between_snapshot_and_audit_is_reported_unavailable(tmp_path, monkeypatch):
    from scripts.tool_scripts import bim_agent_save_feedback as feedback
    toolkit = _toolkit(tmp_path)
    original = feedback.audit_inference_candidate

    def changed(*args, **kwargs):
        summary = original(*args, **kwargs)
        current = _source(2)
        current["spaces"][0]["role"] = "office"
        _save(toolkit, "candidate_02", current)
        return summary

    monkeypatch.setattr(feedback, "audit_inference_candidate", changed)
    result = attach_save_facts(toolkit, _result(toolkit), parent="candidate_01")
    assert result["save_facts"]["status"] == "unavailable"
    assert result["saved_candidate"] == "candidate_02"
    assert not (toolkit.run / "candidate_02/save_facts.json").exists()


def _delta_facts(tmp_path, before, after):
    toolkit = _toolkit(tmp_path)
    _save(toolkit, "candidate_01", before)
    _save(toolkit, "candidate_02", after)
    reply = attach_save_facts(toolkit, _result(toolkit), parent="candidate_01")["save_facts"]
    assert reply["status"] == "available", reply
    full = json.loads((toolkit.run / reply["facts_file"]).read_bytes())
    return reply, full


def _topics(scope):
    return {row["topic"] for row in scope["actions"]}


def test_save_scope_ignores_numerical_recomputation_and_provenance(tmp_path):
    before = _source(1)
    after = copy.deepcopy(before)
    for row in after["spaces"]:
        row["polygon"][0][0] += 1e-10
        row["height"] += 1e-10
        row["source_refs"] = ["same geometry recomputed"]
    for row in after["boundaries"] + after["openings"]:
        row["vertices"][0][0] += 1e-10
        row["assumptions"] = ["provenance clarified"]
    reply, full = _delta_facts(tmp_path, before, after)
    assert full["entity_diff"]["entities"]["spaces"]["changed"]["count"] == 3
    assert reply["review_scope"]["status"] == "no_material_changes"
    assert reply["review_scope"]["actions"] == []
    assert reply["review_scope"]["affected"] == {}
    assert full["affected_opening_hosts"]["count"] == 0
    assert "not quality acceptance" in reply["review_scope"]["meaning"]


@pytest.mark.parametrize("kind", ["polygon", "hole", "section_polygon", "section_height"])
def test_saved_core_shape_changes_target_its_served_floors(tmp_path, kind):
    before = _source(1)
    core = before["spaces"][2]
    core["role"] = "shaft"
    core["holes"] = [[[1, 0.5], [2, 0.5], [2, 1.5], [1, 1.5]]]
    core["vertical_sections"] = [
        {"z": [3, 5], "polygon": copy.deepcopy(core["polygon"]), "holes": copy.deepcopy(core["holes"])},
        {"z": [5, 7], "polygon": copy.deepcopy(core["polygon"]), "holes": copy.deepcopy(core["holes"])},
    ]
    after = copy.deepcopy(before)
    changed = after["spaces"][2]
    if kind == "polygon":
        changed["polygon"][1][0] += 0.2
    elif kind == "hole":
        changed["holes"][0][0][0] += 0.2
    elif kind == "section_polygon":
        changed["vertical_sections"][1]["polygon"][1][0] -= 0.2
    else:
        changed["vertical_sections"][0]["z"][1] += 0.2
        changed["vertical_sections"][1]["z"][0] += 0.2
    reply, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert scope["status"] == "changes_detected"
    assert scope["affected"]["floors"] == ["F2"]
    assert scope["affected"]["spaces"] == ["U"]
    assert scope["affected"]["windows"] == ["wU"]
    if kind == "section_height":
        assert "height_alignment" in _topics(scope)
        assert "plan_relations" not in _topics(scope)
    else:
        assert "plan_relations" in _topics(scope)
        assert "exterior overlays do not establish" in next(
            row["action"] for row in scope["actions"] if row["topic"] == "plan_relations")
    assert "opening_hosts" in _topics(reply["review_scope"])


@pytest.mark.parametrize("kind", ["role", "identity", "membership"])
def test_identity_use_and_membership_cannot_be_suppressed_as_geometry_noise(tmp_path, kind):
    before = _source(1)
    after = copy.deepcopy(before)
    if kind == "role":
        after["spaces"][2]["role"] = "shaft"
    elif kind == "identity":
        after["spaces"][2]["id"] = "renamed_U"  # same role and coordinates
    else:
        after["floors"][0]["spanning_space_ids"] = ["U"]
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert scope["status"] == "changes_detected"
    assert "identity_and_use" in _topics(scope)
    if kind == "role":
        assert _topics(scope) == {"identity_and_use"}
        assert full["affected_opening_hosts"]["count"] == 0
    if kind == "identity":
        changes = scope["changes"]
        assert any(row["id"] == "U" and row["change"] == "removed" for row in changes)
        assert any(row["id"] == "renamed_U" and row["change"] == "added" for row in changes)


@pytest.mark.parametrize("kind", ["host_mapping", "connection", "opening_height", "boundary_hole"])
def test_opening_and_boundary_changes_remain_local_and_actionable(tmp_path, kind):
    before = _source(1)
    after = copy.deepcopy(before)
    if kind == "host_mapping":
        after["opening_hosts"] = {"d1": ["bB"]}
    elif kind == "connection":
        after["connections"][0]["state"] = "closed"
    elif kind == "opening_height":
        after["openings"][0]["vertices"][2][2] += 0.2
    else:
        after["boundaries"][0]["holes"] = [[[1, 0, 1], [2, 0, 1], [2, 0, 2], [1, 0, 2]]]
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert scope["status"] == "changes_detected"
    assert scope["affected"]["floors"] == ["F1"]
    assert "d1" in scope["affected"]["openings"]
    assert "wU" not in scope["affected"]["windows"]
    assert "opening_hosts" in _topics(scope)
    if kind == "opening_height":
        assert "height_alignment" in _topics(scope)


def test_opening_in_spanning_core_only_targets_its_intersected_storey(tmp_path):
    before = _source(1)
    before["spaces"][0].update(role="shaft", height=7)
    before["floors"][0].update(z_floor=0, height=3, spanning_space_ids=["A"])
    before["floors"][1].update(z_floor=3, height=4, spanning_space_ids=["A", "U"])
    after = copy.deepcopy(before)
    after["openings"][0]["connectivity"] = "closed"
    _, full = _delta_facts(tmp_path, before, after)
    assert full["review_scope"]["affected"]["floors"] == ["F1"]


def test_frame_change_targets_registration_without_claiming_rooms_moved(tmp_path):
    before = _source(1)
    before["mesh_frame"] = {"mesh_sha256": "a" * 64, "yaw_degrees": 0, "translation_m": [0, 0, 0]}
    after = copy.deepcopy(before)
    after["mesh_frame"]["translation_m"][0] = 2
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert _topics(scope) == {"mesh_registration"}
    assert scope["affected"]["floors"] == ["F1", "F2"]
    assert scope["affected"]["spaces"] == ["A", "B", "U"]
    assert all(row["collection"] == "mesh_frame" for row in scope["changes"])


def test_invalid_comparison_baseline_does_not_claim_unchanged(tmp_path, monkeypatch):
    from scripts.tool_scripts import bim_agent_save_feedback as feedback
    toolkit = _toolkit(tmp_path)
    original = feedback.audit_inference_candidate

    def changed_parent(*args, **kwargs):
        summary = original(*args, **kwargs)
        previous = _source(1)
        previous["spaces"][0]["height"] += 0.2
        _save(toolkit, "candidate_01", previous)
        return summary

    monkeypatch.setattr(feedback, "audit_inference_candidate", changed_parent)
    reply = attach_save_facts(toolkit, _result(toolkit), parent="candidate_01")["save_facts"]
    assert reply["status"] == "available"
    assert reply["entity_diff"]["status"] == "unavailable"
    assert reply["review_scope"]["status"] == "unavailable"
    assert reply["review_scope"]["actions"] == []


def test_initial_source_has_no_change_localization_not_an_unchanged_verdict(tmp_path):
    toolkit = _toolkit(tmp_path)
    scope = attach_save_facts(toolkit, _result(toolkit))["save_facts"]["review_scope"]
    assert scope["status"] == "no_baseline"
    assert scope["actions"] == []


@pytest.mark.parametrize("kind", ["add_partial_open", "unknown", "resize", "provenance"])
def test_partial_enclosure_changes_are_not_lost_when_parent_wall_stays_physical(tmp_path, kind):
    from src.agent.geometry.source_enclosure import apply_source_enclosure
    from tests.test_source_enclosure import _source as enclosure_source, _declaration, _evidence

    base = enclosure_source()
    boundary = next(row for row in base["boundaries"] if row["space_id"] == "a"
                    and row["geometry_type"] == "wall" and not row["adjacent_space_ids"])
    a, b, c, d = boundary["vertices"]

    def enclosed(condition="open", fraction=0.5):
        region = [a, b, [b[0], b[1], b[2] + (c[2] - b[2]) * fraction],
                  [a[0], a[1], a[2] + (d[2] - a[2]) * fraction]]
        return apply_source_enclosure(base, _declaration(base, boundaries=[{
            "boundary_id": boundary["id"], "condition": condition, "scope": "partial",
            "vertices": region, **_evidence()}]))

    before = base if kind == "add_partial_open" else enclosed()
    after = enclosed("unknown" if kind == "unknown" else "open", 0.75 if kind == "resize" else 0.5)
    if kind == "provenance":
        for row in after["boundaries"]:
            for region in row.get("enclosure_regions", []):
                region["source_refs"] = ["same observation, clearer reference"]
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    if kind == "provenance":
        assert scope["status"] == "no_material_changes"
    else:
        assert scope["status"] == "changes_detected"
        assert "enclosure_relations" in _topics(scope)
        assert scope["affected"]["boundaries"] == [boundary["id"]]
        assert scope["affected"]["spaces"] == ["a"]
        assert next(row for row in after["boundaries"] if row["id"] == boundary["id"])["kind"] == "physical"


def test_space_enclosure_change_is_material_even_with_same_shape_and_role(tmp_path):
    before = _source(1)
    after = copy.deepcopy(before)
    after["spaces"][2]["enclosure"] = "semi_open"
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert _topics(scope) == {"enclosure_relations"}
    assert scope["affected"]["spaces"] == ["U"]
    assert scope["affected"]["floors"] == ["F2"]


def test_local_counterpart_reindex_does_not_invalidate_every_core_landing(tmp_path):
    before = _source(1)
    before["spaces"][0].update(role="shaft", height=7)
    before["floors"][0].update(z_floor=0, height=3, spanning_space_ids=["A"])
    before["floors"][1].update(z_floor=3, height=4, spanning_space_ids=["A", "U"])
    core_wall = before["boundaries"][0]
    core_wall["vertices"][2][2] = core_wall["vertices"][3][2] = 7
    core_wall.update(adjacent_space_ids=["B", "U"], counterpart_ids=["bB", "bU"])
    before["openings"][-1].update(host_boundary_id="bA", space_ids=["A", "U"])
    after = copy.deepcopy(before)
    after["boundaries"][1]["id"] = "bB-reindexed"
    after["boundaries"][0]["counterpart_ids"] = ["bB-reindexed", "bU"]
    _, full = _delta_facts(tmp_path, before, after)
    scope = full["review_scope"]
    assert scope["status"] == "changes_detected"
    assert scope["affected"]["floors"] == ["F1"]
    assert scope["affected"]["spaces"] == ["A", "B"]
    assert "d1" in scope["affected"]["openings"]
    assert "wU" not in scope["affected"]["windows"]
    assert {row["id"] for row in scope["changes"]} == {"bA", "bB", "bB-reindexed"}


@pytest.mark.parametrize("geometry_type, expected_floor", [("floor", "F1"), ("ceiling", "F3")])
def test_continuous_space_horizontal_enclosure_change_targets_touching_floor(tmp_path, geometry_type, expected_floor):
    from src.agent.execution.source_proposal import export_source_proposal
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal
    from src.agent.geometry.source_enclosure import apply_source_enclosure
    from tests.test_parametric_continuity import storeys, continuity
    from tests.test_source_enclosure import _declaration, _evidence

    plan = storeys()
    plan["continuous_spaces"] = [continuity()]
    report = export_source_proposal(expand_parametric_proposal(plan), tmp_path / "joined")
    assert report["source_geometry_ready"], report
    before = json.loads((tmp_path / "joined/source_model.json").read_bytes())
    boundary = next(row for row in before["boundaries"]
                    if row["space_id"] == "STAIR:stair" and row["geometry_type"] == geometry_type)
    after = apply_source_enclosure(before, _declaration(before, boundaries=[{
        "boundary_id": boundary["id"], "condition": "unknown", "scope": "whole", **_evidence()}]))
    assert after["schema_version"] == "source_bim_v3"
    assert before["spaces"] == after["spaces"]
    _, full = _delta_facts(tmp_path, before, after)
    assert full["review_scope"]["affected"]["floors"] == [expected_floor]
    assert full["review_scope"]["affected"]["boundaries"] == [boundary["id"]]
    assert "enclosure_relations" in _topics(full["review_scope"])


def test_horizontal_cap_touches_both_adjacent_storeys_but_aperture_uses_positive_overlap():
    from scripts.tool_scripts.bim_agent_review_scope import _floors_for_space
    source = _source(1)
    source["spaces"][0].update(role="shaft", height=7)
    source["floors"][0].update(z_floor=0, height=3, spanning_space_ids=["A"])
    source["floors"][1].update(z_floor=3, height=4, spanning_space_ids=["A", "U"])
    cap = {"geometry_type": "floor", "vertices": [[0, 0, 3], [1, 0, 3], [1, 1, 3], [0, 1, 3]]}
    assert _floors_for_space(source, "A", cap) == {"F1", "F2"}
    door = {"kind": "door", "vertices": [[0, 0, 0], [1, 0, 0], [1, 0, 3], [0, 0, 3]]}
    assert _floors_for_space(source, "A", door) == {"F1"}
