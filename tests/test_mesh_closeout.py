"""Current mesh blockers, unknowns and handoff evidence stay separate."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from scripts.tool_scripts.bim_agent_closeout import closeout_summary, mesh_closeout
from scripts.tool_scripts.bim_agent_replies import compact_reply, precision_summary
from src.agent.geometry.source_model import _digest
from src.agent.runtime_context import update_building_context
from src.agent_runtime.context import ContextManager
from src.agent_runtime.store import EventStore
from src.harness_contracts import BudgetAmounts
from tests.test_mesh_delivery_review import prepared, _review


def _saved(*, findings=(), notes=(), **changes):
    source = {"validation": {"status": "severe" if findings else "pass", "findings": list(findings)},
              "spaces": [], "boundaries": [], "openings": [],
              "conflicts": [], "unbuilt_openings": [], "unsupported": [],
              "generation": {"unresolved": list(notes)}, **changes}
    source["source_model_sha256"] = _digest(source)
    report = {"source_model_sha256": source["source_model_sha256"],
              "source_geometry_ready": not findings, "unresolved": list(notes)}
    return source, report


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_notes_do_not_become_blockers_or_accepted_omissions():
    source, report = _saved(notes=["Roof equipment is not known", "Interior inferred"])
    result = closeout_summary("candidate_01", source, report)
    assert result["machine_check"]["status"] == "clear"
    assert result["retained_notes"]["status"] == "reported_unknown_not_accepted"
    assert result["retained_notes"]["count"] == 2
    assert result["mesh_review"]["fidelity"] == "not_evaluated"
    assert not result["mesh_review"]["current_whole_building_declaration"]
    assert "normal finish_bim" in result["next_action"]
    assert "finish" not in result and "ready" not in result


def test_all_machine_error_sources_survive_grouping():
    source, report = _saved(findings=[
        {"severity": "severe", "code": "source.space_overlap", "space_ids": ["office", "core"]},
        {"severity": "severe", "code": "source.space_overlap", "space_ids": ["room", "core"]}],
        conflicts=[{"code": "invalid_host", "opening_id": "D1"}],
        unbuilt_openings=[{"id": "D2", "reason": "no host"}],
        unsupported=[{"id": "curve", "reason": "unsupported shape"}])
    machine = closeout_summary("candidate_01", source, report)["machine_check"]
    assert machine["status"] == "blocked"
    assert machine["finding_count"] == 5
    assert machine["counts"] == {"source_validation": 2, "conflicts": 1,
                                 "unbuilt_openings": 1, "unsupported": 1}
    overlap = next(group for group in machine["groups"] if group["code"] == "source.space_overlap")
    assert overlap["count"] == 2 and overlap["examples"][0]["space_ids"] == ["office", "core"]
    assert machine["report_file"] == "candidate_01/report.json"


@pytest.mark.parametrize("change", ["source", "report", "validation", "ready"])
def test_unavailable_checks_cannot_be_reported_clear(change):
    source, report = _saved()
    if change == "source":
        source["spaces"].append({"id": "changed"})
    elif change == "report":
        report["source_model_sha256"] = "other"
    elif change == "validation":
        source, report = _saved(validation={"status": "pass"})
    else:
        report["source_geometry_ready"] = False
    result = closeout_summary("candidate_01", source, report,
        review={"candidate": "candidate_01", "status": "current", "complete_building": True})
    assert result["machine_check"]["status"] == "unavailable"
    assert "before delivery" in result["next_action"]


def test_missing_review_does_not_hash_mesh_and_saved_review_uses_existing_validation(tmp_path, monkeypatch):
    import scripts.tool_scripts.bim_agent_mesh_review as reviews
    source, report = _saved()
    folder = tmp_path / "candidate_01"
    _write(folder / "source_model.json", source)
    _write(folder / "report.json", report)
    toolkit = SimpleNamespace(run=tmp_path, manifest={"mesh_input": {"sha256": "fixture"}})
    calls = []
    def validate(toolkit, candidate):
        calls.append(candidate)
        return {"candidate": candidate, "status": "stale", "complete_building": False}
    monkeypatch.setattr(reviews, "mesh_review_status", validate)
    result = mesh_closeout(toolkit, "candidate_01")
    assert result["mesh_review"]["status"] == "missing" and calls == []
    _write(folder / "mesh_review.json", {"untrusted": "not sufficient"})
    result = mesh_closeout(toolkit, "candidate_01")
    assert result["mesh_review"]["status"] == "stale" and calls == ["candidate_01"]
    mesh_closeout(toolkit, "candidate_01", review={"candidate": "candidate_01", "status": "missing"})
    assert calls == ["candidate_01"]  # A caller's freshly checked result is reused.
    toolkit.manifest = {}
    assert mesh_closeout(toolkit, "candidate_01") is None


def test_real_saved_review_tamper_is_not_a_delivery_declaration(prepared):
    from scripts.tool_scripts.bim_agent_mesh_review import carry_mesh_review
    toolkit, candidate = prepared
    carry_mesh_review(toolkit, candidate, _review())
    before = {path: path.read_bytes() for path in toolkit.run.rglob("*") if path.is_file()}
    current = mesh_closeout(toolkit, candidate)
    assert current["mesh_review"]["current_whole_building_declaration"]
    assert current["mesh_review"]["fidelity"] == "not_evaluated"
    assert all(path.read_bytes() == raw for path, raw in before.items())
    image = toolkit.run / "mesh_overlays/overlay_001.png"
    image.write_bytes(image.read_bytes() + b"tampered")
    stale = mesh_closeout(toolkit, candidate)
    assert stale["mesh_review"]["status"] == "stale"
    assert not stale["mesh_review"]["current_whole_building_declaration"]


def test_runtime_notes_are_visible_but_only_machine_errors_reopen_todo(tmp_path):
    run = tmp_path / "bim"
    _write(run / "inputs.json", {"mesh_input": {"sha256": "fixture"}})
    first, report1 = _saved(notes=["Unknown equipment, retained for user scope"])
    second, report2 = _saved(findings=[
        {"severity": "severe", "code": "source.floor_coverage", "floor_id": "F2"}])
    for candidate, source, report in (("candidate_01", first, report1), ("candidate_02", second, report2)):
        _write(run / candidate / "source_model.json", source)
        _write(run / candidate / "report.json", report)
    with EventStore(tmp_path / "audit", run_id="closeout", task_id="t",
                    budget_limit=BudgetAmounts(tokens=1000, calls=3)) as store:
        context = ContextManager(store)
        engine = SimpleNamespace(tools=SimpleNamespace(run_directory=run), store=store,
            context=context, _event_source=lambda _: store.source("tool-result", {}))
        event = SimpleNamespace(payload=SimpleNamespace(tool_name="build_bim"))
        def update(candidate, source, report):
            closeout = closeout_summary(candidate, source, report)
            update_building_context(engine, event, {"structuredContent": {
                "saved_candidate": candidate, "save_effects": {"created_candidates": [candidate],
                "geometry_applied": True, "audit_written": True}, "closeout": closeout}})
            return {state.key: state for state in context.state}
        states = update("candidate_01", first, report1)
        assert not states["source-bim-todo"].active
        assert states["source-bim-unresolved"].active
        assert states["source-bim-unresolved"].epistemic_status == "unresolved"
        assert states["mesh-closeout"].epistemic_status == "computed"
        visible = next(row["value"] for row in context.model_state() if row["key"] == "mesh-closeout")
        assert visible["source_version"] == "sha256:" + first["source_model_sha256"]
        states = update("candidate_02", second, report2)
        assert states["source-bim-todo"].active
        assert states["source-bim-todo"].value["machine_check"]["counts"]["source_validation"] == 1
        event.payload.tool_name = "inspect_candidate"
        update_building_context(engine, event, {"structuredContent": {"candidate": "candidate_01",
            "closeout": closeout_summary("candidate_01", first, report1)}})
        current = next(state for state in context.state if state.key == "mesh-closeout")
        assert current.value["candidate"] == "candidate_02"
        assert current.value["machine_check"]["status"] == "blocked"


def test_bounded_reply_preserves_current_closeout_and_full_report(tmp_path):
    findings = [{"severity": "severe", "code": f"failure-{i}", "space_ids": ["对象" * 1000] * 20,
                 "reason": "依据" * 1000, "floor_id": "层" * 1000} for i in range(100)]
    source, report = _saved(findings=findings, notes=["未知" * 1000] * 30)
    closeout = closeout_summary("candidate_01", source, report)
    assert len(json.dumps(closeout, ensure_ascii=False).encode("utf-8")) < 12000
    raw = {"candidate": "candidate_01", "closeout": closeout,
           **{f"large_{i}": ["evidence" * 1000] * 10 for i in range(20)}}
    original = copy.deepcopy(raw)
    result = compact_reply(tmp_path, "build_bim", raw)
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 24000
    assert result["closeout"] == closeout
    assert result["closeout"]["machine_check"]["finding_count"] == 100
    assert result["closeout"]["machine_check"]["omitted_group_count"] == 92
    assert json.loads((tmp_path / result["details_file"]).read_bytes()) == raw == original


def test_precision_warnings_remain_visible_without_becoming_closeout_blockers():
    report = {"schema": "building_precision_v2", "status": "pass", "items": [], "total": 0,
              "counts": {}, "warnings": [{"code": "thin_horizontal_contact"}],
              "warning_count": 1, "warning_counts": {"thin_horizontal_contact": 1},
              "policy_classification": {"thin_horizontal_contact": "warning"}}
    compact = precision_summary(report)
    assert compact["warnings"] == report["warnings"]
    assert compact["warning_count"] == 1
    assert compact["policy_classification"] == report["policy_classification"]
    assert "0 blocking precision findings" in compact["coverage"]
    source, saved_report = _saved()
    saved_report["building_precision"] = report
    assert closeout_summary("candidate_01", source, saved_report)["machine_check"]["status"] == "clear"
