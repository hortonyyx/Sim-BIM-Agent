"""Optional native mesh tasks share the registered Runtime and root allowance."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from src.agent import runtime_configuration, runtime_entry
from src.agent_runtime.store import EventStore
from tests.test_mesh_input_admission import _case, _load
from tests.test_source_proposal import _proposal


def test_mesh_worker_configuration_is_optional_and_reaches_native_entry(tmp_path):
    case = _case(tmp_path)
    assert "--mesh-workers" not in runtime_configuration.argv_for(_load(tmp_path, case))
    case["mesh_workers"] = {"max_concurrent": 8}
    command = runtime_configuration.argv_for(_load(tmp_path, case))
    assert command[2] == "src.agent.runtime_entry"
    args = runtime_entry.parser().parse_args(command[3:])
    assert args.mesh_workers == 8
    assert args.mesh_worker_script is None


@pytest.mark.parametrize("configuration", [True, {}, {"max_concurrent": True},
    {"max_concurrent": 0}, {"max_concurrent": -1}, {"max_concurrent": 1.5},
    {"max_concurrent": 2, "worker_model": "unconfigured"}])
def test_invalid_worker_configuration_fails_before_launch(tmp_path, configuration):
    case = _case(tmp_path)
    case["mesh_workers"] = configuration
    with pytest.raises(ValueError, match="mesh_workers"):
        _load(tmp_path, case)


@pytest.mark.parametrize("mode,kind", [("single_model", "images"),
    ("role_division", "mesh"), ("external_coordinator_mcp", "mesh")])
def test_worker_option_does_not_silently_change_other_entrypoints(tmp_path, mode, kind):
    case = _case(tmp_path)
    case.update(mode=mode, input_kind=kind, mesh_workers={"max_concurrent": 2})
    with pytest.raises(ValueError, match="mesh_workers"):
        _load(tmp_path, case)


def _response(identity, tool=None, arguments=None, answer=None):
    message = {"role": "assistant", "content": answer}
    if tool:
        message["tool_calls"] = [{"id": identity, "type": "function", "function": {
            "name": tool, "arguments": json.dumps(arguments or {})}}]
    return {"id": identity, "model": "scripted-model", "choices": [{
        "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def test_native_mesh_workers_return_verified_views_and_complete_shared_handoff(tmp_path):
    asyncio.run(_native_mesh_worker_handoff(tmp_path))


async def _native_mesh_worker_handoff(tmp_path):
    case = _case(tmp_path)
    inputs = Path(case["input_root"])
    mesh = inputs / "building.glb"
    proposal = _proposal()
    proposal["mesh_frame"] = {"mesh_sha256": hashlib.sha256(mesh.read_bytes()).hexdigest(),
        "yaw_degrees": 0, "translation_m": [0, 0, 0],
        "reason": "Synthetic integration only; no geometric fidelity claim.", "source_refs": ["synthetic"]}
    tasks = [
        {"task_id": "facade", "question": "Inspect this synthetic mesh and candidate projection.",
         "candidate": "candidate_01", "budget": {"model_calls": 5, "tool_calls": 4}},
        {"task_id": "unknown", "question": "State what cannot be established from this packet.",
         "budget": {"model_calls": 1, "tool_calls": 0}},
    ]
    parent = [
        _response("p1", "inputs"),
        _response("p2", "build_bim", {"proposal_json": json.dumps(proposal)}),
        _response("p3", "delegate_mesh_workers", {"tasks": tasks}),
        _response("p4", "read_mesh_worker", {"task_id": "facade", "view_ids": ["mesh_001", "overlay_001"]}),
        _response("p5", "view_mesh"),
        _response("p6", "overlay_mesh_candidate", {"candidate": "candidate_01", "observation": "mesh_001"}),
        _response("p7", "finish_bim", {"candidate": "candidate_01", "reviews_json": json.dumps({
            "mesh_review": {"coverage": "whole_building", "summary": "Synthetic plumbing only; fidelity not evaluated.",
                            "overlay_ids": ["overlay_001"]}})}),
    ]
    answer = {"directly_seen": [
        {"observation_id": "surface", "statement": "The rendered synthetic asset has a visible surface.",
         "evidence_kind": "mesh_observation", "evidence_id": "mesh_001", "measurement_call_id": "f2"},
        {"observation_id": "projection", "statement": "A candidate projection was rendered; agreement is unassessed.",
         "evidence_kind": "mesh_overlay", "evidence_id": "overlay_001"}],
        "uncertain": [{"uncertainty_id": "quality", "statement": "No real building quality is evaluated."}]}
    workers = {
        "facade": [
            _response("f1", "view_mesh"),
            _response("f2", "measure_mesh_pixels", {"observation": "mesh_001", "pixels": [[600, 450], [610, 450]]}),
            _response("f3", "overlay_mesh_candidate", {"candidate": "candidate_01", "observation": "mesh_001"}),
            _response("f4", answer=json.dumps(answer)),
        ],
        "unknown": [_response("u1", answer=json.dumps({"uncertain": [
            {"uncertainty_id": "interior", "statement": "No interior layout has been established."}]}))],
    }
    parent_script, worker_script = tmp_path / "parent.json", tmp_path / "workers.json"
    parent_script.write_text(json.dumps(parent), encoding="utf-8")
    worker_script.write_text(json.dumps(workers), encoding="utf-8")
    out = tmp_path / "native-workers"
    args = runtime_entry.parser().parse_args([
        "--out", str(out), "--run-root", str(tmp_path), "--mesh", str(mesh),
        "--images", str(inputs / "sketches"), "--image-kind", "unknown",
        "--building-input", str(inputs / "declaration.json"),
        "--provider", "scripted", "--script", str(parent_script), "--mesh-workers", "2",
        "--mesh-worker-script", str(worker_script), "--model-calls", "16", "--tool-calls", "24",
        "--seconds", "300", "--no-token-limit", "--image-history-batches", "1"])
    receipt = await runtime_entry.execute(args)
    assert receipt["status"] == "completed", receipt
    assert receipt["finalization"]["completion_valid"] is True
    assert receipt["finalization"]["delivery"]["complete_building"] is True
    assert receipt["completion"]["mesh_review"]["fidelity"] == "not_evaluated"
    assert receipt["model_calls"] == 7
    assert receipt["whole_run_counts"]["model_requests"] == 12
    assert receipt["whole_run_counts"]["tool_calls"] == 10
    assert {row["task_id"]: row["status"] for row in receipt["mesh_workers"]["tasks"]} == {
        "facade": "completed", "unknown": "completed"}
    assert receipt["root_usage_accounting"]["requests"] == 12
    events = EventStore.read_events(out / "events.jsonl")
    assert all(event.payload.tool_name not in {"build_bim", "revise_bim", "finish_bim"}
        for event in events if event.task_id != "coordinator" and event.payload.event_type == "tool_invocation")
    assert all(event.payload.outcome == "succeeded" for event in events
               if event.payload.event_type == "tool_execution")
    before = (out / "events.jsonl").read_bytes()
    args.resume = True
    resumed = await runtime_entry.execute(args)
    assert resumed["whole_run_counts"] == receipt["whole_run_counts"]
    assert (out / "events.jsonl").read_bytes() == before
    args.mesh_workers = 3
    with pytest.raises(ValueError, match="configuration changed"):
        await runtime_entry.execute(args)
    assert (out / "events.jsonl").read_bytes() == before
