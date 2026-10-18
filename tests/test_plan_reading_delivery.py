import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image

from src.agent.runtime_roles.plan_format import PLAN_FORMAT, READER_PLAN_EXAMPLE
from src.agent.runtime_roles.readers import automatic_plan_evidence, validate_plan_artifact
from src.agent.runtime_roles.submission import ReaderSubmission
from src.agent.runtime_roles.trial import PlanTrial


def setup(tmp_path):
    (tmp_path / "images").mkdir()
    path = tmp_path / "images/plan.png"
    Image.new("RGB", (800, 600), "white").save(path)
    (tmp_path / "inputs.json").write_text(json.dumps({"images": {"plan.png": {
        "size": [800, 600], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}))
    plan = copy.deepcopy(READER_PLAN_EXAMPLE)
    return plan, PlanTrial(None, image_name="plan.png", workspace=tmp_path,
                           receipt_directory=tmp_path / "trial_receipts", grid_step_m=.1)


def test_planar_reading_has_no_bim_or_fabricated_heights_and_survives_reload(tmp_path):
    plan, trial = setup(tmp_path)
    receipt = asyncio.run(trial.run(plan))
    assert receipt["status"] == "passed", receipt.get("reason")
    assert receipt["reading_geometry_ready"] and receipt["source_geometry_ready"] is False
    assert not list(tmp_path.rglob("source_model.json"))
    product = json.loads((tmp_path / receipt["reading_product_file"]).read_bytes())
    assert "z_floor" not in product["geometry"]["floors"][0]
    assert all("z" not in row for row in product["geometry"]["openings"] + product["geometry"]["windows"])
    assert product["walls"] and all(row["thickness_m"] > 0 for row in product["walls"])
    restored = PlanTrial(None, image_name="plan.png", workspace=tmp_path, receipt_directory=tmp_path / "trial_receipts")
    _, validated = restored.verified_plan(receipt["plan_sha256"])
    assert len(validated["returned_images"]) == 2
    for row in validated["returned_images"]:
        with Image.open(tmp_path / row["file"]) as picture:
            assert picture.size == (800, 600)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", directory=tmp_path,
                                  trial=restored, target=plan["floor_id"])
    saved = submission.submit({"trial_id": "latest", "local_review": {"summary": "Offline fixture overlay inspected; no independent fidelity claim."}})
    assert saved["status"] == "accepted"
    assert "z_floor" not in submission.read()["artifact"]["plan"]


@pytest.mark.parametrize("provide_local_thickness", [False, True])
def test_required_wall_coverage_and_batch_defaults_need_no_extra_reading(tmp_path, provide_local_thickness):
    plan, trial = setup(tmp_path)
    if not provide_local_thickness:
        plan.pop("wall_metadata")
    result = asyncio.run(trial.call(plan))
    if not provide_local_thickness:
        assert result['isError']
        assert 'Every wall needs thickness' in trial.receipts[0]['reason']
        return
    assert not result['isError'] and len(trial.receipts) == 1
    receipt = trial.receipts[0]
    assert receipt['wall_metadata_summary']['counts'] == {
        'total': 5, 'supplied': 5, 'known': 5, 'unknown': 0, 'defaulted_unknown': 0}
    product_path = tmp_path / receipt['reading_product_file']
    before = product_path.read_bytes()
    product = json.loads(before)
    assert all(row['thickness_m'] > 0 and row['reference_side'] != 'unknown' for row in product['walls'])
    assert all(row['evidence_status'] == 'inferred' for row in product['walls'])
    submission = ReaderSubmission(role_id='plan_reader', image_name='plan.png', directory=tmp_path,
                                  trial=trial, target=plan['floor_id'])
    accepted = submission.submit({'trial_id': 'latest', 'local_review': {
        'summary': 'Offline fixture checked; inferred thickness retained as inferred.'},
        'notes': [{'item': 'wall_metadata', 'kind': 'assumption',
                   'basis': 'Construction estimates adopted for Lite BIM.'}]})
    assert accepted['status'] == 'accepted' and len(trial.receipts) == 1
    assert product_path.read_bytes() == before


def test_omitted_2d_contract_fields_accept_notes_without_values_or_product_changes(tmp_path):
    plan, trial = setup(tmp_path)
    original = copy.deepcopy(plan)
    receipt = asyncio.run(trial.run(plan))
    assert receipt["status"] == "passed", receipt.get("reason")
    numeric = trial.numeric_plan(receipt)
    omitted = sorted(set(PLAN_FORMAT["properties"]) - set(plan) - set(numeric))
    assert {"z_floor", "ceiling_height"} <= set(omitted)
    evidence = validate_plan_artifact({"plan": numeric, "unresolved": numeric["unresolved"],
        "evidence": automatic_plan_evidence(numeric, image_name="plan.png", image_size=(800, 600))},
        image_name="plan.png")["evidence"]
    files_before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    receipt_before = copy.deepcopy(receipt)
    notes = [{"item": field if index % 2 else f"plan.{field}", "kind": "unresolved",
              "basis": "This optional property is not established by this drawing."}
             for index, field in enumerate(omitted)]
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", directory=tmp_path,
                                  trial=trial, target=plan["floor_id"])
    result = submission.submit({"trial_id": "latest", "local_review": {
        "summary": "Offline fixture views checked; optional missing properties remain unknown."},
        "notes": notes})
    assert result["status"] == "accepted" and len(trial.receipts) == 1
    saved = submission.read()
    assert [row["item"] for row in saved["validation"]["notes"]] == [f"plan.{field}" for field in omitted]
    assert saved["artifact"]["plan"] == numeric and plan == original
    assert not set(omitted).intersection(saved["artifact"]["plan"])
    assert saved["artifact"]["evidence"] == evidence
    assert saved["validation"]["plan_sha256"] == receipt_before["plan_sha256"]
    assert trial.receipts == [receipt_before]
    assert all(path.read_bytes() == before for path, before in files_before.items())


@pytest.mark.parametrize("note", [
    {"item": "ceiling_heigth", "kind": "unresolved", "basis": "Misspelled property."},
    {"item": "plan.openings:nonexistent", "kind": "unresolved", "basis": "Fabricated object."},
    {"item": "ceiling_height", "kind": "unresolved", "basis": "Unknown height.", "value": 3.0},
])
def test_2d_optional_notes_reject_unknown_fields_objects_and_value_injection(tmp_path, note):
    plan, trial = setup(tmp_path)
    receipt = asyncio.run(trial.run(plan))
    assert receipt["status"] == "passed", receipt.get("reason")
    product = tmp_path / receipt["reading_product_file"]
    before = product.read_bytes()
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", directory=tmp_path,
                                  trial=trial, target=plan["floor_id"])
    with pytest.raises(ValueError, match="not a declared plan object|Additional properties"):
        submission.submit({"trial_id": "latest", "local_review": {
            "summary": "Offline fixture views checked."}, "notes": [note]})
    assert submission.read() is None and len(trial.receipts) == 1
    assert product.read_bytes() == before


def test_planar_product_tamper_rejected_and_aperture_overlap_fails(tmp_path):
    plan, trial = setup(tmp_path)
    duplicate = copy.deepcopy(plan["openings"][0])
    duplicate["id"] = "duplicate"
    bad = {**plan, "openings": [*plan["openings"], duplicate]}
    failed = asyncio.run(trial.run(bad))
    assert failed["status"] == "failed" and "overlapping plan openings" in failed["reason"]
    passed = asyncio.run(trial.run(plan))
    assert passed["status"] == "passed", passed.get("reason")
    path = tmp_path / passed["reading_product_file"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        trial.verified_plan(passed["plan_sha256"])


@pytest.mark.parametrize("metadata_update", [False, True])
def test_reader_unit_accepts_reviewed_planar_delivery_without_bim(tmp_path, monkeypatch, metadata_update):
    from src.agent.runtime_roles.entry import execute, parser
    from src.agent_runtime.adapter import ScriptedAdapter
    from test_agent_runtime import versions, response
    monkeypatch.setattr("src.agent.runtime_roles.entry.make_versions", lambda *a, **k: versions())
    monkeypatch.setattr("src.agent.runtime_roles.session.make_versions", lambda *a, **k: versions())
    images = tmp_path / "inputs"
    images.mkdir()
    Image.new("RGB", (800, 600), "white").save(images / "plan.png")
    marker = tmp_path / "script.json"
    marker.write_text("{}")
    routes = {role: {"provider": "scripted", "model": "scripted-model", "output_tokens": 4000}
              for role in ("coordinator", "plan_reader", "elevation_reader")}
    task = {"task_id": "plan-unit", "role_id": "plan_reader", "image": "plan.png", "target": "F1"}
    args = parser().parse_args(["--out", str(tmp_path / "out"), "--run-root", str(tmp_path),
        "--images", str(images), "--provider", "scripted", "--model", "scripted-model", "--script", str(marker),
        "--roles-json", json.dumps(routes), "--seconds", "120", "--model-calls", "2", "--tool-calls", "4",
        "--tokens", "100000000", "--reader-task-json", json.dumps(task)])
    def accept(request):
        assert "reading_geometry_ready" in json.dumps(request["messages"])
        calls = []
        if metadata_update:
            changed = copy.deepcopy(READER_PLAN_EXAMPLE)
            changed["wall_metadata"][0]["basis"] = "Revised construction explanation"
            calls.append(("metadata", "trial_plan_bim", {"plan": changed}))
        calls.append(("accept", "submit_plan_reading", {"trial_id": "latest", "local_review": {
            "summary": "Offline scripted fixture inspects its actual returned 2D views."}}))
        return response(*calls)
    def factory(task_id, config, store):
        return ScriptedAdapter([response(("trial", "trial_plan_bim", {"plan": READER_PLAN_EXAMPLE})), accept])
    result = asyncio.run(execute(args, adapter_factory=factory))
    assert result["status"] == "completed", result
    assert result["role_accounting"]["requests"] == 2
    assert not result["whole_building_delivered"]
    assert not list((tmp_path / "out").rglob("source_model.json"))
