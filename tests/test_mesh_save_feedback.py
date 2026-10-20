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
