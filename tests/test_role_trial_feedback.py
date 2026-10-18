from reader_delivery_fixtures import REVIEW, compact_elevation, accept_elevation

import asyncio
import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from src.agent.runtime_roles.trial import PlanTrial
from src.agent_runtime.adapter import convert_tool_result
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore


class InjectedTrial(PlanTrial):
    def __init__(self, receipt, images):
        super().__init__(object(), image_name="plan.png")
        self.injected_receipt = receipt
        self.injected_images = images

    async def run(self, plan=None, *, operations=None):
        return self.injected_receipt

    def _image_content(self, receipt):
        return list(self.injected_images)


def _receipt(tmp_path, *, passed):
    image = b"exact-overlay-bytes"
    image_sha = hashlib.sha256(image).hexdigest()
    complete = {
        "status": "passed" if passed else "failed",
        "plan_sha256": "plan-hash",
        "source_geometry_ready": passed,
        "candidate": "candidate_01" if passed else None,
        "receipt_file": "trial_receipts/trial_001.json",
        "returned_images": [{
            "file": "trial_receipts/trial_001_images/01_overlay.png",
            "sha256": image_sha,
            "mime_type": "image/png",
            "origin": {
                "source_image_name": "plan.png",
                "raw_metadata": {"large_marker": "origin-only-on-disk" * 100},
            },
        }],
        "drawing_differences": {"status": "reported", "items": [{"id": "DIFF-1"}]},
        "building_precision": {"status": "reported", "items": [{"id": "PREC-1"}]},
        "topology_issues": [{"id": "TOPO-1", "divider": "P1"}],
        "topology_dividers": {"P1": [[1, 2], [3, 4]]},
        "changes": [{"path": "plan.openings:W1", "before": [1, 2], "after": [2, 3]}],
        "operations": [{"op": "update", "id": "W1", "changes": {"p1": [2, 3]}}],
        "plan_revision": {"actual_changes": [{"path": "plan.openings:W1"}]},
    }
    if not passed:
        complete.update({
            "reason": "opening W1 has no full host; repro_marker",
            "repair_hint": {"path": "plan.openings[0]", "example": "move onto wall"},
            "unhosted_openings": [{"id": "W1", "p1": [2, 3], "p2": [4, 3]}],
            "source_findings": [{"code": "opening_overlap", "opening_ids": ["W1"]}],
        })
    receipt_path = tmp_path / complete["receipt_file"]
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(complete), encoding="utf-8", newline="\n")
    block = {"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/png"}
    return complete, block, receipt_path, image_sha


def _convert(tmp_path, envelope):
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=10_000)
    with EventStore(
        tmp_path / "events", run_id="trial-feedback", task_id="plan-reader",
        budget_limit=limits.ledger_limit(),
    ) as store:
        return convert_tool_result("trial-call", envelope, store)


def test_trial_model_projection_keeps_actionable_feedback_and_full_disk_receipt(tmp_path):
    complete, image, receipt_path, image_sha = _receipt(tmp_path, passed=True)
    envelope = asyncio.run(InjectedTrial(complete, [image]).call({}))
    visible = envelope["structuredContent"]

    assert visible["returned_images"] == [{
        "file": complete["returned_images"][0]["file"], "sha256": image_sha,
    }]
    assert "origin" not in visible["returned_images"][0]
    assert visible["drawing_differences"] == complete["drawing_differences"]
    assert visible["building_precision"] == complete["building_precision"]
    assert visible["topology_issues"] == complete["topology_issues"]
    assert visible["changes"] == complete["changes"]
    assert visible["receipt_audit"]["receipt_file"] == complete["receipt_file"]
    assert "operations" not in visible and "plan_revision" not in visible
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == complete
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["returned_images"][0]["origin"]
    assert hashlib.sha256(base64.b64decode(envelope["content"][0]["data"])).hexdigest() == image_sha

    message, pictures, _, image_refs = _convert(tmp_path, envelope)
    assert json.loads(message["content"]) == visible
    assert len(pictures) == 2 and len(image_refs) == 1


def test_failed_trial_is_one_json_after_adapter_and_loses_no_error_location(tmp_path):
    complete, image, receipt_path, image_sha = _receipt(tmp_path, passed=False)
    envelope = asyncio.run(InjectedTrial(complete, [image]).call({}))
    message, pictures, _, image_refs = _convert(tmp_path, envelope)

    assert message["content"].startswith("Tool error (isError=true):\n{")
    json_text = message["content"].split("\n", 1)[1]
    visible = json.loads(json_text)
    assert message["content"].count("repro_marker") == 1
    assert visible["reason"] == complete["reason"]
    assert visible["repair_hint"] == complete["repair_hint"]
    assert visible["unhosted_openings"] == complete["unhosted_openings"]
    assert visible["source_findings"] == complete["source_findings"]
    assert visible["topology_issues"] == complete["topology_issues"]
    assert visible["drawing_differences"] == complete["drawing_differences"]
    assert visible["building_precision"] == complete["building_precision"]
    assert visible["message"].startswith("trial_plan_bim failed")
    assert visible["next_action"]["objects"] == ["plan.openings[0]"]
    assert "Preserve all other observed walls, openings and room topology" in visible["next_action"]["instruction"]
    assert hashlib.sha256(base64.b64decode(envelope["content"][0]["data"])).hexdigest() == image_sha
    assert len(pictures) == 2 and len(image_refs) == 1
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == complete


def test_wrapped_geometry_failure_reuses_original_coordinate_hint():
    from src.agent.geometry.plan_partition import compile_plan_partition
    from src.agent.geometry.plan_regularization import PlanRegularizationError
    from src.agent.runtime_roles.trial import _numeric_failure_hint
    from tests.test_plan_regularization import _plan, IMAGE_SIZE
    plan = _plan([('wall', [[40, 0], [40, 95]])])
    with pytest.raises(ValueError) as caught:
        compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name='plan.png')
    error = PlanRegularizationError('The adjusted draft did not compile literally.', report={
        'rejections': [{'type': 'strict_compile_failed_after_regularization', 'error': str(caught.value)}]})
    hint = _numeric_failure_hint(plan, error)
    assert hint['path'] == 'plan.partitions[0].points[1]'
    assert hint['junction_repairs'][0]['original_endpoint_pixel'] == [40, 95]
    assert 'example' not in hint


def test_failure_bookkeeping_is_readable_only_in_full_receipt(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=False)
    complete['reading_alignment'] = {'status': 'reported', 'repeated_ledger': ['audit-only'] * 100}
    complete['failed_stage_input_file'] = 'trial_001_failed_stage_input.json'
    complete['repair_coordinate_basis'] = 'Edit original pixels, not failed-stage coordinates.'
    visible = asyncio.run(InjectedTrial(complete, [image]).call({}))['structuredContent']
    assert 'reading_alignment' not in visible and 'candidate' not in visible
    assert visible['failed_stage_input_file'] == complete['failed_stage_input_file']
    assert visible['repair_coordinate_basis'] == complete['repair_coordinate_basis']
    assert visible['receipt_audit']['read']['arguments']['field'] == 'report:'


def test_large_failed_trial_keeps_located_actions_and_references_repeated_audit_detail(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=False)
    complete["reason"] = "many dangles; " + ("repeated compiler detail " * 200)
    complete["repair_hint"] = {
        "path": "plan.partitions[0].points[1]",
        "note": "Move only the named endpoint using the original drawing.",
        "junction_repairs": [{"path": f"plan.partitions[{i}].points[1]"} for i in range(20)],
    }
    complete["changes"] = [{"path": f"plan.partitions:{i}"} for i in range(30)]
    complete["alignment"] = {
        "schema_version": "reader_alignment.v1", "status": "rejected",
        "fallback": {"status": "not_applied", "aligned_compile_error": "x" * 5000,
                     "original_compile_error": "y" * 5000},
        "junction_preparation": {"status": "rolled_back", "compile_error": "z" * 5000,
                                 "changes": list(range(20)), "attempted_changes": list(range(20))},
    }
    receipt_path.write_text(json.dumps(complete), encoding="utf-8", newline="\n")
    visible = asyncio.run(InjectedTrial(complete, [image]).call({}))["structuredContent"]

    assert visible["next_action"]["objects"] == ["plan.partitions[0].points[1]"]
    assert len(visible["repair_hint"]["junction_repairs"]) == 8
    assert visible["repair_hint"]["junction_repairs_remaining"] == 12
    assert len(visible["changes"]) == 12 and visible["changes_remaining"] == 18
    assert visible["alignment"]["junction_preparation"]["has_compile_error"] is True
    assert "compile_error" not in visible["alignment"]["junction_preparation"]
    assert {"reason", "repair_hint", "changes"} <= set(visible["audit_refs"])
    assert len(json.dumps(visible)) < 6000
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["reason"] == complete["reason"]


def _large_precision():
    # Reproduce the observed shape, not case answers: three repeated ledgers,
    # each change containing the complete before/after geometry snapshot.
    geometry = [{"id": f"synthetic-{i}", "points": [[i, j] for j in range(28)]} for i in range(32)]
    changes = [{"path": f"partitions:P{i}", "before": geometry, "after": geometry} for i in range(24)]
    return {"status": "pass", "total": 0, "counts": {}, "items": [],
            "not_checked": ["drawing fidelity", "opening heights"],
            "regularization": {"reports": [{"status": "applied", "changes": changes,
                "reading_alignment": {"changes": changes}, "regularization_inputs": {"changes": changes}}]}}


def test_failed_compiled_candidate_also_bounds_precision_without_losing_repair_actions(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=False)
    complete["trial_id"] = "trial_001"
    complete["building_precision"] = _large_precision()
    complete["building_precision"].update(status="failed", total=1,
        items=[{"type": "thin_space", "space_id": "C1", "boundary_ids": ["B1"], "look_box": [1, 2, 3, 4]}])
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    envelope = asyncio.run(trial.call({}))
    visible = envelope["structuredContent"]
    assert len(original) > 1_000_000 and len(json.dumps(visible).encode()) < 20_000
    assert envelope["isError"] and visible["status"] == "failed"
    assert visible["source_geometry_ready"] is False
    assert visible["reason"] == complete["reason"]
    assert visible["repair_hint"] == complete["repair_hint"]
    assert visible["next_action"]["objects"] == ["plan.openings[0]"]
    assert visible["building_precision"]["items"] == complete["building_precision"]["items"]
    assert "submission" not in visible and "tool" not in visible["next_action"]
    leaf = trial.read_draft(field="report:/building_precision/regularization/reports/0/changes/23/before/31")
    assert leaf["value"] == complete["building_precision"]["regularization"]["reports"][0]["changes"][23]["before"][31]
    assert receipt_path.read_bytes() == original

    from src.agent_runtime.adapter import ScriptedAdapter
    from src.agent_runtime.loop import Runtime
    from src.harness_contracts import ToolGrant
    from tests.test_agent_runtime import MESSAGES, response, role, versions

    trial.injected_images = []

    class Boundary:
        async def list_tools(self):
            return [{"name": name, "description": name, "inputSchema": {"type": "object"}}
                    for name in ("trial_plan_bim", "read_plan_draft")]

        def repeatability(self, name):
            return "read_only" if name == "read_plan_draft" else "idempotent_write"

        async def call_tool(self, name, arguments):
            if name == "trial_plan_bim":
                return await trial.call({})
            value = trial.read_draft(**arguments)
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}

        def snapshot_state(self):
            return {"receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}

        def artifacts(self):
            return [receipt_path]

    def inspect_after_failure(body):
        tool = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
        value = json.loads(tool["content"].split("\n", 1)[1])
        assert value["status"] == "failed" and value["next_action"]["objects"] == ["plan.openings[0]"]
        assert len(json.dumps(body).encode()) < 30_000
        return response(("read", "read_plan_draft", {
            "field": "report:/building_precision/regularization/reports/0/changes/23/before/31"}))

    def inspected(body):
        tool = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
        value = json.loads(tool["content"])
        assert value["value"] == leaf["value"]
        assert value["trial_status"] == "failed"
        return response(text="Located evidence recovered; next action remains repair, not submit.")

    limits = RunLimits(model_calls=3, tool_calls=2, seconds=60, tokens=200_000, context_tokens=40_000)
    tools_role = role(limits).model_copy(update={"tool_whitelist": (
        ToolGrant(tool_name="trial_plan_bim", access="write"), ToolGrant(tool_name="read_plan_draft", access="read"))})
    with EventStore(tmp_path / "failure-events", run_id="failed-trial", task_id="reader",
                    budget_limit=limits.ledger_limit()) as store:
        engine = Runtime(store=store, adapter=ScriptedAdapter([
            response(("trial", "trial_plan_bim", {})), inspect_after_failure, inspected]),
            tools=Boundary(), role=tools_role, model="test", parameters={"max_tokens": 256},
            versions=versions(), limits=limits)
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 3
        store.validate()
    assert receipt_path.read_bytes() == original


def test_megabyte_passed_receipt_remains_browsable_and_all_topology_decisions_discoverable(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=True)
    complete["trial_id"] = "trial_001"
    complete["building_precision"] = _large_precision()
    complete["topology_issues"] = [{"issue_id": f"issue-{i}", "divider": f"P{i}",
        "look_box": [i, 1, i + 2, 3], "check": "Resolve this observed separator with a located decision."}
        for i in range(30)]
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    assert len(original) > 1_000_000
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    visible = asyncio.run(trial.call({}))["structuredContent"]
    assert len(json.dumps(visible).encode()) < 25_000
    assert visible["building_precision"]["status"] == "pass"
    assert visible["building_precision"]["not_checked"] == ["drawing fidelity", "opening heights"]
    assert visible["submission"]["status"] == "not_submitted"
    assert visible["submission"]["topology_decisions_required"] == 30
    assert visible["next_action"]["tool"] == "submit_plan_reading"
    assert visible["receipt_audit"]["sha256"] == hashlib.sha256(json.dumps(
        complete, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert "receipt_audit" not in complete

    # Traverse all pages; giant nested rows are references, never giant pages.
    args = {"field": "report:/building_precision/regularization/reports/0/changes"}
    indexes = []
    while args:
        page = trial.read_draft(**args)
        assert len(json.dumps(page).encode()) < 20_000
        indexes.extend(row["key"] for row in page["entries"])
        args = page.get("next")
    assert indexes == list(range(24))
    leaf = trial.read_draft(field="report:/building_precision/regularization/reports/0/changes/23/before/31")
    assert leaf["value"] == complete["building_precision"]["regularization"]["reports"][0]["changes"][23]["before"][31]
    issues, args = [], {"field": "report:/topology_issues"}
    while args:
        page = trial.read_draft(**args)
        issues.extend(row["value"]["issue_id"] for row in page["entries"])
        args = page.get("next")
    assert issues == [row["issue_id"] for row in complete["topology_issues"]]
    assert receipt_path.read_bytes() == original
    with pytest.raises(ValueError, match="pointer"):
        trial.read_draft(field="report:/../../outside.json")
    with pytest.raises(ValueError, match="page"):
        trial.read_draft(field="report:/building_precision", object_id="page:-1")
    corrupted = dict(complete, status="failed")
    receipt_path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since execution"):
        trial.read_draft(field="report:/building_precision")


def test_megabyte_success_continues_through_runtime_adapter_to_explicit_submission(tmp_path):
    from src.agent.runtime_roles.submission import ReaderSubmission, SUBMISSION_TOOLS
    from src.agent_runtime.adapter import ScriptedAdapter
    from src.agent_runtime.loop import Runtime
    from src.harness_contracts import ToolGrant
    from tests.test_agent_runtime import MESSAGES, response, role, versions
    from tests.test_role_readers import plan
    from tests.test_role_trial import Tools

    workspace = tmp_path / "workspace"
    (workspace / "images").mkdir(parents=True)
    original_image = b"hash-stable offline image fixture; no drawing"
    (workspace / "images/plan.png").write_bytes(original_image)
    image_sha = hashlib.sha256(original_image).hexdigest()
    (workspace / "inputs.json").write_text(json.dumps({"images": {"plan.png": {"sha256": image_sha}}}))
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(stream, "PNG")

    class LargeTools(Tools):
        async def call_tool(self, name, arguments):
            result = await super().call_tool(name, arguments)
            (workspace / "candidate_01/precision_report.json").write_text(json.dumps(_large_precision()))
            result["structuredContent"]["drawing_differences"] = {"status": "reported", "total": 0, "items": []}
            result["content"][0]["data"] = base64.b64encode(stream.getvalue()).decode()
            return result

    compiler = LargeTools(workspace=workspace)
    trial = PlanTrial(compiler, image_name="plan.png", workspace=workspace,
                      receipt_directory=workspace / "trial_receipts")
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial,
                                  directory=tmp_path / "submission", target="F1")

    class ReaderBoundary:
        async def list_tools(self):
            return [{"name": "trial_plan_bim", "description": "trial", "inputSchema": {"type": "object"}},
                    SUBMISSION_TOOLS["plan_reader"]]

        def repeatability(self, name):
            return "idempotent_write"

        async def call_tool(self, name, arguments):
            if name == "trial_plan_bim":
                return await trial.call(**arguments)
            saved = submission.submit(arguments)
            return {"content": [{"type": "text", "text": json.dumps(saved)}], "structuredContent": saved}

        def snapshot_state(self):
            return {"trial": trial.durable_snapshot(), "submission": submission.snapshot()}

        def artifacts(self):
            return trial.artifacts() + ([submission.path] if submission.path.is_file() else [])

    def submit_after_trial(body):
        assert submission.read() is None  # passed was not silently promoted to delivery
        tool = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
        visible = json.loads(tool["content"])
        assert visible["status"] == "passed" and visible["source_geometry_ready"]
        assert visible["submission"]["status"] == "not_submitted"
        assert any(m["role"] == "user" and isinstance(m["content"], list) for m in body["messages"])
        assert len(json.dumps(body).encode()) < 40_000
        return response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"}))

    limits = RunLimits(model_calls=3, tool_calls=2, seconds=60, tokens=200_000, context_tokens=40_000)
    reader_role = role(limits).model_copy(update={"tool_whitelist": tuple(
        ToolGrant(tool_name=n, access="write") for n in ("trial_plan_bim", "submit_plan_reading"))})
    adapter = ScriptedAdapter([response(("trial", "trial_plan_bim", {"plan": plan()})),
                               submit_after_trial, response(text="Submitted")])
    with EventStore(tmp_path / "events", run_id="passed-trial", task_id="reader",
                    budget_limit=limits.ledger_limit()) as store:
        engine = Runtime(store=store, adapter=adapter, tools=ReaderBoundary(), role=reader_role, model="test",
                         parameters={"max_tokens": 256}, versions=versions(), limits=limits)
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed", result
        assert result["model_calls"] == 3 and result["tool_calls"] == 2
        assert len(compiler.calls) == 1
        assert not [e for e in store.events if e.payload.event_type == "model_failure"]
        store.validate()
    receipt = trial.receipts[0]
    saved = submission.read()
    assert saved["validation"]["validation_passed"]
    assert saved["artifact"]["plan"] == trial.numeric_plan(receipt)
    assert saved["artifact"]["evidence"]
    full = json.loads((workspace / receipt["receipt_file"]).read_bytes())
    assert full["building_precision"] == _large_precision()
    assert len(json.dumps(full).encode()) > 1_000_000
    identities = {key: receipt[key] for key in ("receipt_file", "plan_sha256", "compiled_numeric_plan_sha256",
                                               "candidate", "candidate_source_sha256")}
    receipt_sha = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert trial.verified_unsubmitted_draft(image_sha, **identities,
        expected_receipt_sha256=receipt_sha)[0] == plan()
    with pytest.raises(ValueError, match="event-bound full receipt digest"):
        trial.verified_unsubmitted_draft(image_sha, **identities)
    inherited = PlanTrial(None, image_name="plan.png")
    inherited.inherit_reference(trial, receipt["plan_sha256"], ["plan.openings"])
    assert "building_precision" not in inherited.read_draft()
    inherited_report = inherited.read_draft(field="report:/building_precision/regularization/reports/0/changes")
    assert inherited_report["inherited"] and inherited_report["next"]["trial_id"] == "reference"
    assert inherited.read_draft(**inherited_report["next"])["entries"][0]["key"] == 8
    assert inherited.read_draft(field="report:/topology_issues")["value"] == receipt["topology_issues"]
    receipt_path = workspace / receipt["receipt_file"]
    original_receipt = receipt_path.read_bytes()
    tampered = dict(receipt, topology_issues=[{"issue_id": "changed-obligation"}])
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="inherited trial receipt changed"):
        inherited.read_draft(field="report:/topology_issues")
    reloaded = PlanTrial(None, image_name="plan.png", workspace=workspace,
                         receipt_directory=workspace / "trial_receipts")
    with pytest.raises(ValueError, match="full receipt differs"):
        reloaded.verified_unsubmitted_draft(image_sha, **identities, expected_receipt_sha256=receipt_sha)
    receipt_path.write_bytes(original_receipt)
    with pytest.raises(ValueError, match="event result"):
        trial.verified_unsubmitted_draft(image_sha, **{**identities, "candidate_source_sha256": "0" * 64})
    source_path = workspace / receipt["candidate"] / "source_model.json"
    source_path.write_bytes(source_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="source hash"):
        trial.verified_unsubmitted_draft(image_sha, **identities, expected_receipt_sha256=receipt_sha)


def test_bounded_json_browser_preserves_escaped_keys_and_long_text_pages():
    from src.agent.runtime_roles.trial_feedback import bounded_json_view

    text = "located evidence 中文 " * 800
    document = {"a/b~c": {"text": text, "rows": [{"nested": text}] * 20}}
    ref = lambda pointer, value: {"pointer": pointer, "sha256": hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()).hexdigest()}
    pointer = "/a~1b~0c/text"
    parts, offset = [], 0
    while True:
        page = bounded_json_view(document, pointer=pointer, offset=offset, reference_for=ref)
        parts.append(page["text"])
        assert len(json.dumps(page).encode()) < 2500
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert "".join(parts) == text
    page = bounded_json_view(document, pointer="/a~1b~0c/rows", reference_for=ref)
    assert len(page["entries"]) == 8 and page["next_offset"] == 8
    assert len(json.dumps(page).encode()) < 12_000
    assert page["entries"][0]["value"]["full_record"]["pointer"] == "/a~1b~0c/rows/0"
    for pointer in ("/a~2b", "/a~1b~0c/rows/-1", "/a~1b~0c/rows/01"):
        with pytest.raises(ValueError, match="pointer"):
            bounded_json_view(document, pointer=pointer, reference_for=ref)
    with pytest.raises(ValueError, match="offset"):
        bounded_json_view(document, offset=-1, reference_for=ref)
    with pytest.raises(ValueError, match="offset"):
        bounded_json_view(document, offset=True, reference_for=ref)
    with pytest.raises(ValueError, match="beyond"):
        bounded_json_view(document, pointer="/a~1b~0c/rows", offset=20, reference_for=ref)
    with pytest.raises(ValueError, match="beyond"):
        bounded_json_view(document, pointer="/a~1b~0c/text", offset=len(text), reference_for=ref)


def test_all_draft_read_paths_are_bounded_and_reference_exact_saved_values(tmp_path):
    from src.agent.runtime_roles.trial import canonical_plan_sha256
    from tests.test_role_trial import example

    document = example()
    document["regularization"] = _large_precision()
    document["regularization_inputs"] = {"a/b~c": {"text": "中文 evidence " * 1200}}
    document["openings"] = [{**document["openings"][0], "id": f"door-{i}",
                             "source_refs": ["located evidence " * 800]}
                            for i in range(47)]
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    assert len(raw) > 1_000_000
    receipt = {"trial_id": "trial_001", "status": "failed", "receipt_file": "receipt.json",
               "input_plan_file": "plan.json", "plan_sha256": canonical_plan_sha256(document)}
    trial = PlanTrial(None, image_name="plan.png", workspace=tmp_path)
    trial.receipts = [receipt]
    (tmp_path / "plan.json").write_bytes(raw)
    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    for arguments in ({}, {"field": "plan"}, {"field": "regularization"},
                      {"field": "regularization_inputs"}, {"field": "openings"},
                      {"field": "openings", "object_id": "door-46"},
                      {"field": "plan:/regularization_inputs/a~1b~0c"}):
        page = trial.read_draft(**arguments)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 16_000
        assert page["reference"]["plan_sha256"] == receipt["plan_sha256"]
        assert page["reference"]["read"]["arguments"]["trial_id"] == "trial_001"
    summary = trial.read_draft()
    assert summary["object_counts"]["openings"] == 47
    assert len(summary["object_ids"]["openings"]) == 12
    assert summary["reference"]["sha256"] == receipt["plan_sha256"]

    indexes, args = [], {"field": "openings"}
    while args:
        page = trial.read_draft(**args)
        indexes.extend(row["key"] for row in page["entries"])
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 16_000
        args = page.get("next")
    assert indexes == list(range(47))
    object_page = trial.read_draft(field="openings", object_id="door-46")
    assert object_page["reference"]["json_pointer"] == "/openings/46"
    leaf = trial.read_draft(field="plan:/openings/46/id")
    assert leaf["value"] == "door-46"
    parts, args = [], {"field": "plan:/regularization_inputs/a~1b~0c/text"}
    while args:
        page = trial.read_draft(**args)
        parts.append(page["text"])
        args = page.get("next")
    assert "".join(parts) == document["regularization_inputs"]["a/b~c"]["text"]
    assert (tmp_path / "plan.json").read_bytes() == raw

    for arguments in ({"field": "plan:/../../outside.json"},
                      {"field": "plan:/openings/01"},
                      {"field": "plan:/regularization_inputs/a~2b"},
                      {"field": "regularization", "object_id": "page:-1"},
                      {"field": "openings", "object_id": "missing"}):
        with pytest.raises(ValueError):
            trial.read_draft(**arguments)
    (tmp_path / "plan.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        trial.read_draft(field="plan:/openings/46/id")


def test_draft_reference_keeps_custom_tool_and_exact_draft_identity():
    from src.agent.runtime_roles.trial_feedback import read_plan

    plan = {"openings": [{"id": f"D{i}", "source_refs": ["original" * 200]} for i in range(20)]}
    page = read_plan(plan, {"plan_sha256": "verified-plan"}, field="openings",
                     read_tool="inspect_plan_draft", read_arguments={"draft_id": "draft_001"})
    assert page["next"] == {"draft_id": "draft_001", "field": "plan:/openings", "object_id": "page:8"}
    assert page["reference"]["read"] == {"tool": "inspect_plan_draft", "arguments": {
        "draft_id": "draft_001", "field": "plan:/openings"}}


def _opening_differences(count):
    # Synthetic geometry with the observed report shape, not a case answer.
    return [{"type": "opening_offset_from_gap", "opening": f"opening-{i}",
             "divider": f"wall-{i}", "end_offsets_m": [-0.24, -0.16],
             "declared": {"axis": "horizontal", "y_px": 500, "x_px": [520, 560],
                          "y_m": 8.0, "x_m": [5.2, 6.0]},
             "gap": {"axis": "horizontal", "y_px": 500, "x_px": [532, 568],
                     "y_m": 8.0, "x_m": [5.44, 6.16]},
             "look_box": [480, 460, 600, 540],
             "check": "Compare the declared opening endpoints with the overlapping inkless stretch."}
            for i in range(count)]


def test_floor_difference_list_keeps_offsets_without_per_opening_reads(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=True)
    complete["trial_id"] = "trial_001"
    rows = _opening_differences(17)
    assert 350 < len(json.dumps(rows[0], separators=(",", ":")).encode()) < 1200
    complete["drawing_differences"] = {"status": "reported", "total": len(rows), "items": rows}
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    envelope = asyncio.run(trial.call({}))
    message, _, _, _ = _convert(tmp_path, envelope)
    visible = json.loads(message["content"])
    shown = visible["drawing_differences"]
    assert [dict({k: v for k, v in row.items() if k != 'guidance'},
                 check=shown['guidance'][row['guidance']]) for row in shown['items']] == rows
    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000
    assert visible["submission"]["status"] == "not_submitted"
    assert visible["next_action"]["tool"] == "submit_plan_reading"
    # Page reads also keep the full small rows; no drill-down is required to
    # recover offsets, declared spans or the original locations.
    read_rows, args = [], {"field": "report:/drawing_differences/items"}
    while args:
        page = trial.read_draft(**args)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 12_000
        read_rows.extend(row["value"] for row in page["entries"])
        args = page.get("next")
    assert read_rows == rows
    assert receipt_path.read_bytes() == original


def test_large_difference_list_stays_bounded_and_references_original_evidence(tmp_path):
    from src.agent.runtime_roles.trial_feedback import reference

    complete, image, receipt_path, _ = _receipt(tmp_path, passed=True)
    complete["trial_id"] = "trial_001"
    rows = _opening_differences(100)
    complete["drawing_differences"] = {"status": "reported", "total": len(rows), "items": rows}
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    visible = asyncio.run(trial.call({}))["structuredContent"]
    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000
    items = visible["drawing_differences"]["items"]
    assert items["full_record"] == reference(complete, "/drawing_differences/items", rows)
    assert items["items"][0]["summary"]["end_offsets_m"] == rows[0]["end_offsets_m"]
    assert items["items"][0]["summary"]["declared"] == rows[0]["declared"]
    args = dict(items["full_record"]["read"]["arguments"])
    recovered = []
    while args:
        page = trial.read_draft(**args)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 12_000
        recovered.extend(row["value"] for row in page["entries"])
        args = page.get("next")
    assert recovered == rows
    assert receipt_path.read_bytes() == original


def _wall_differences(count):
    # Reproduce a batch of located measurements with a repeated long heuristic.
    rows = [{"type": "wall_metadata_ink_mismatch", "wall_target": f"partition:synthetic-wall-{i}",
             "axis": "y", "comparison": "placement_only", "image_expansion": "both",
             "declared_reference_px": 495.2, "declared_faces_px": [492.422, 497.978],
             "declared_thickness_m": .12, "nearby_parallel_ink_px": [489.5, 495.0],
             "ink_spacing_m": .1188, "face_residual_m": [.0631, .0643],
             "reference_outside_ink_m": .0043, "reference_side": "centerline",
             "look_box": [282, 474, 513, 517], "sample_pixels": 166, "sampled_length_m": 3.59,
             "check": ("Compare the original ink pair, wall reference, expansion side and thickness basis. "
                       "Persistent strokes can be another feature; an audited grid or merge shift is "
                       "permitted. Correct a wrong physical feature or side, not every small displacement. "
                       "Preserve real partitions and openings; use the original drawing as evidence.")}
            for i in range(count)]
    return rows


def test_wall_difference_batch_exposes_decisions_before_prose_is_budgeted(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=True)
    complete['trial_id'] = 'trial_001'
    rows = _wall_differences(19)
    complete['drawing_differences'] = {'status': 'reported', 'items': rows, 'total': len(rows),
                                     'coverage': {'note': 'x' * 1500}}
    receipt_path.write_text(json.dumps(complete), encoding='utf-8')
    before = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    shown = asyncio.run(trial.call({}))['structuredContent']
    differences = shown['drawing_differences']
    assert len(json.dumps(rows, separators=(',', ':')).encode()) > 8000
    assert len(differences['items']) == 19
    assert len(differences['guidance']) == 1
    for actual, original in zip(differences['items'], rows):
        for key in ('wall_target', 'axis', 'comparison', 'image_expansion', 'declared_reference_px',
                    'declared_faces_px', 'declared_thickness_m', 'nearby_parallel_ink_px',
                    'ink_spacing_m', 'face_residual_m', 'look_box'):
            assert actual[key] == original[key]
        assert not {'check', 'sample_pixels', 'sampled_length_m', 'reference_side',
                    'reference_outside_ink_m'} & actual.keys()
    assert len(json.dumps(shown, ensure_ascii=False).encode()) <= 24000
    assert trial.read_draft(field='report:/drawing_differences/items/18')['value'] == rows[-1]
    assert receipt_path.read_bytes() == before


def test_wall_difference_projection_preserves_only_available_side_and_metric():
    from src.agent.runtime_roles.trial_feedback import report_summary
    row = _wall_differences(1)[0]
    row.update(image_expansion='unknown', face_residual_m=None)
    receipt = {'drawing_differences': {'items': [row]}}
    shown = report_summary(receipt, 'drawing_differences')['items'][0]
    assert shown['reference_side'] == row['reference_side']
    assert shown['reference_outside_ink_m'] == row['reference_outside_ink_m']


def test_oversized_wall_projection_references_original_unstripped_rows():
    from src.agent.runtime_roles.trial_feedback import report_summary, reference
    rows = _wall_differences(100)
    receipt = {'trial_id': 'trial_005', 'drawing_differences': {'items': rows}}
    shown = report_summary(receipt, 'drawing_differences')['items']
    assert shown['full_record'] == reference(receipt, '/drawing_differences/items', rows)
    assert shown['items'][0]['full_record'] == reference(receipt, '/drawing_differences/items/0', rows[0])


def test_giant_single_regularization_rejection_keeps_location_and_verified_full_receipt(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=False)
    complete["trial_id"] = "trial_001"
    repeated = [{"space_id": f"S{i}", "geometry_audit": "before and after " * 120} for i in range(150)]
    complete["regularization_failure"] = {"status": "rejected", "rejections": [{
        "type": "cross_storey_relationship_changed", "path": "plan.partitions:W1",
        "objects": [{"floor_id": "F1", "partition_id": "W1"}],
        "before": repeated, "after": repeated, "attempted_changes": repeated,
    }]}
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    assert len(original) > 500_000
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    visible = asyncio.run(trial.call({}))["structuredContent"]
    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000
    assert visible["status"] == "failed"
    assert visible["next_action"]["objects"] == ["plan.openings[0]"]
    rejection = visible["regularization_failure"]["summary"]["rejections"]["items"][0]
    assert rejection["summary"]["path"] == "plan.partitions:W1"
    assert rejection["summary"]["type"] == "cross_storey_relationship_changed"
    reference = rejection["full_record"]
    assert reference["json_pointer"] == "/regularization_failure/rejections/0"
    located = trial.read_draft(field="report:" + reference["json_pointer"] + "/objects")
    assert located["value"] == complete["regularization_failure"]["rejections"][0]["objects"]
    assert receipt_path.read_bytes() == original


def test_large_parallel_literal_diagnostic_is_bounded_and_full_details_remain_readable(tmp_path):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=False)
    complete["trial_id"] = "trial_001"
    repairs = [{"path": f"plan.partitions[{i}].points[0]", "partition_id": f"P{i}",
                "original_endpoint_pixel": [i, 12], "detail": "original geometry evidence " * 200}
               for i in range(80)]
    complete["literal_geometry_diagnostic"] = {
        "status": "rejected", "scope": "Resolved original pixels before dimensions",
        "input_file": "trial_receipts/trial_001_unaligned_input.json", "input_sha256": "a" * 64,
        "reason": "polygonize produced dangles: " + "located evidence " * 1000,
        "repair_hint": {"path": repairs[0]["path"], "junction_repairs": repairs},
        "loose_partition_ends": [{"partition": f"P{i}", "end": [i, 12], "gap_px": 12} for i in range(80)],
    }
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]

    visible = asyncio.run(trial.call({}))["structuredContent"]

    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000
    assert visible["status"] == "failed"
    diagnostic = visible["literal_geometry_diagnostic"]
    assert diagnostic["truncated"] and diagnostic["summary"]["status"] == "rejected"
    assert "repair_hint" in diagnostic["summary"]
    ref = visible["audit_refs"]["literal_geometry_diagnostic"]
    assert ref["json_pointer"] == "/literal_geometry_diagnostic"
    page = trial.read_draft(field="report:" + ref["json_pointer"] + "/repair_hint/junction_repairs/79/original_endpoint_pixel")
    assert page["value"] == [79, 12]
    assert receipt_path.read_bytes() == original


@pytest.mark.parametrize("passed", [False, True])
def test_receipt_total_budget_covers_many_small_fields_and_new_large_fields(tmp_path, passed):
    complete, image, receipt_path, _ = _receipt(tmp_path, passed=passed)
    complete["trial_id"] = "trial_001"
    complete.update({f"future_audit_{i}": {"status": "reported", "detail": "x" * 1400}
                     for i in range(80)})
    complete["future_large_audit"] = [{"path": f"plan.openings:{i}", "detail": "y" * 4000}
                                       for i in range(80)]
    complete["format_errors"] = [{"path": f"plan.partitions[{i}]", "message": "z" * 4000}
                                 for i in range(80)]
    receipt_path.write_text(json.dumps(complete), encoding="utf-8")
    original = receipt_path.read_bytes()
    trial = InjectedTrial(complete, [image])
    trial.workspace, trial.receipts = tmp_path, [complete]
    visible = asyncio.run(trial.call({}))["structuredContent"]
    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000
    assert visible["status"] == complete["status"]
    assert visible["omitted_field_count"] > 0
    assert visible["receipt_audit"]["read"]["arguments"] == {"trial_id": "trial_001", "field": "report:"}
    if passed:
        assert visible["submission"]["status"] == "not_submitted"
        assert visible["next_action"]["tool"] == "submit_plan_reading"
    else:
        assert visible["next_action"]["objects"][0] == "plan.openings[0]"
    assert trial.read_draft(field="report:/future_audit_79")["value"] == complete["future_audit_79"]
    tail = trial.read_draft(field="report:/future_large_audit/79/path")
    assert tail["value"] == "plan.openings:79"
    assert receipt_path.read_bytes() == original
