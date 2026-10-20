"""Saved-work import preserves history, source state and finite allowances."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.continuation import ReadOnlyRun, continuation_allowance
from src.agent_runtime.context_continuation import ImportedContext
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.loop import Runtime
from src.agent_runtime.store import EventStore
from src.harness_contracts import UsageMissing
from src.agent.runtime_mesh_continuation import ContinuedRuntime, tree_hashes
from tests.test_agent_runtime import MESSAGES, response, runtime


def test_exact_context_import_keeps_tool_pairs_without_replaying_write(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    failure = ModelServiceError({"category": "transport_error", "retryable": False,
        "usage_received": False}, UsageMissing(reason="no terminal usage"))
    old = runtime(source_root, [response(("old-write", "save", {"value": 7})), failure])
    old.context_policy = ContextPolicy(image_history_batches=1)
    with old.store:
        old.store.write_json("versions.json", old.versions.model_dump(mode="json"))
        assert asyncio.run(old.run(MESSAGES))["status"] == "transport_error"
    source = ReadOnlyRun(old.store.directory)
    imported = ImportedContext(source)
    before = tree_hashes(source.directory)
    dest_root = tmp_path / "destination"
    dest_root.mkdir()
    engine = runtime(dest_root, [response(text="continued")])
    engine.__class__ = ContinuedRuntime
    engine.context_policy = old.context_policy
    engine.context_import = imported
    with engine.store:
        result = asyncio.run(engine.run([]))
        assert result["status"] == "completed"
        assert engine.tools.calls == []
        request = json.loads(engine.adapter.requests[0])
        messages = request["messages"]
        assert any(c["id"] == "old-write" for m in messages for c in m.get("tool_calls", []))
        assert any(m.get("tool_call_id") == "old-write" for m in messages)
        assert "old-write" in engine.used_ids
        assert not any(e.payload.event_type == "tool_invocation" for e in engine.store.events)
        assert imported.manifest["import_event_id"]
    assert tree_hashes(source.directory) == before


def test_import_claim_waits_for_durable_checkpoint_and_resume_does_not_reimport(tmp_path):
    from src.agent_runtime.continuation import claim_continuation
    source_root = tmp_path / "source"
    source_root.mkdir()
    failure = ModelServiceError({"category": "transport_error", "retryable": False,
        "usage_received": False}, UsageMissing(reason="no terminal usage"))
    old = runtime(source_root, [response(("old-write", "save", {"value": 7})), failure])
    old.context_policy = ContextPolicy(image_history_batches=1)
    with old.store:
        old.store.write_json("versions.json", old.versions.model_dump(mode="json"))
        asyncio.run(old.run(MESSAGES))
    source = ReadOnlyRun(old.store.directory)
    before = tree_hashes(source.directory)
    dest_root = tmp_path / "destination"
    dest_root.mkdir()
    engine = runtime(dest_root, [response(text="must not be called")])
    engine.__class__ = ContinuedRuntime
    engine.context_policy = old.context_policy
    engine.context_import = ImportedContext(source)
    def interrupt_after_checkpoint(name, engine):
        if name == "after_checkpoint":
            raise RuntimeError("synthetic interruption after complete import")
    engine.fault_hook = interrupt_after_checkpoint
    with engine.store, pytest.raises(RuntimeError, match="synthetic interruption"):
        asyncio.run(engine.run([]))
    assert not engine.adapter.requests
    assert EventStore.latest_checkpoint(engine.store) is not None
    assert not (source.directory.parent / ".continuation-claims").exists()
    resumed = runtime(dest_root, [response(text="continued once")])
    resumed.context_policy = old.context_policy
    with resumed.store:
        claim_continuation(source, resumed.store.directory)
        result = asyncio.run(resumed.run([], resume=True))
        assert result["status"] == "completed"
        assert len(resumed.adapter.requests) == 1 and resumed.tools.calls == []
        assert sum(e.payload.event_type == "external_coordinator_mcp" for e in resumed.store.events) == 1
    assert tree_hashes(source.directory) == before


@pytest.mark.parametrize("valid_failure", [False, True])
def test_explicit_context_import_discards_only_settled_failed_retry_intent(tmp_path, valid_failure):
    from src.harness_contracts import CheckpointPayload
    failure = ModelServiceError({"category": "transport_error", "retryable": False,
        "usage_received": False}, UsageMissing(reason="no terminal usage"))
    old = runtime(tmp_path, [response(("old-write", "save", {"value": 7})), failure])
    old.context_policy = ContextPolicy(image_history_batches=1)
    with old.store:
        old.store.write_json("versions.json", old.versions.model_dump(mode="json"))
        asyncio.run(old.run(MESSAGES))
        saved = copy.deepcopy(old.store.latest_checkpoint()[1])
        requests = [e.event_id for e in old.store.events if e.payload.event_type == "adapter_request"]
        saved["retry_of"] = requests[-1] if valid_failure else requests[0]
        saved["last_event_id"] = old.store.events[-1].event_id
        old.store.append(CheckpointPayload(state=old.store.put_json_tree(saved),
            after_event_id=saved["last_event_id"]))
    source = ReadOnlyRun(old.store.directory)
    if valid_failure:
        assert ImportedContext(source).saved["retry_of"] == requests[-1]
    else:
        with pytest.raises(ValueError, match="prior request intent"):
            ImportedContext(source)


def test_context_import_does_not_reset_unfinished_consecutive_truncation(tmp_path):
    truncated = response(text="incomplete response")
    truncated["choices"][0]["finish_reason"] = "length"
    failure = ModelServiceError({"category": "transport_error", "retryable": False,
        "usage_received": False}, UsageMissing(reason="no terminal usage"))
    engine = runtime(tmp_path, [truncated, failure])
    engine.context_policy = ContextPolicy(image_history_batches=1)
    with engine.store:
        engine.store.write_json("versions.json", engine.versions.model_dump(mode="json"))
        assert asyncio.run(engine.run(MESSAGES))["status"] == "transport_error"
    source = ReadOnlyRun(engine.store.directory)
    with pytest.raises(ValueError, match="still recovering a truncated response"):
        ImportedContext(source)


@pytest.mark.parametrize("worker_count", [0, 1])
def test_native_mesh_saved_candidate_continues_to_handoff_and_resume(tmp_path, monkeypatch, worker_count):
    from src.agent import runtime_entry
    from src.agent_runtime.agent_registry import register_versions
    from tests.test_mesh_input_admission import _case
    from tests.test_mesh_worker_entry import _response
    from tests.test_source_proposal import _proposal
    from src.agent.runtime_mesh_continuation import inspect_mesh_continuation

    root = Path(__file__).resolve().parents[1]
    registry = tmp_path / "versions.json"
    shutil.copyfile(root / "src/agent_runtime/agent_versions.json", registry)
    register_versions(root, registry_path=registry)
    monkeypatch.setenv("BIM_AGENT_REGISTRY_PATH", str(registry))
    case = _case(tmp_path)
    mesh = Path(case["input_root"]) / "building.glb"
    proposal = _proposal()
    proposal["mesh_frame"] = {"mesh_sha256": hashlib.sha256(mesh.read_bytes()).hexdigest(),
        "yaw_degrees": 0, "translation_m": [0, 0, 0], "reason": "synthetic integration",
        "source_refs": ["synthetic"]}
    task = {"task_id": "unfinished", "question": "Read only", "budget": {"model_calls": 1, "tool_calls": 1}}
    worker = tmp_path / "worker.json"
    worker.write_text(json.dumps({"unfinished": [_response("worker1", "inputs")]}), encoding="utf-8")
    original_adapter = runtime_entry.ScriptedAdapter
    def adapter(values):
        values = [ModelServiceError({"category": "transport_error", "retryable": False,
            "usage_received": False}, UsageMissing(reason="offline transport interruption"))
            if value.get("simulate_disconnect") else value for value in values]
        return original_adapter(values)
    monkeypatch.setattr(runtime_entry, "ScriptedAdapter", adapter)
    original = tmp_path / "original"
    script = tmp_path / "script.json"
    original_responses = [
        _response("old1", "build_bim", {"proposal_json": json.dumps(proposal)}),
        *([_response("old2", "delegate_mesh_workers", {"tasks": [task]})] if worker_count else []),
        _response("old3", "view_mesh"),
        _response("old4", "overlay_mesh_candidate", {"candidate": "candidate_01", "observation": "mesh_001"}),
        {"simulate_disconnect": True}]
    script.write_text(json.dumps(original_responses), encoding="utf-8")
    common = ["--run-root", str(tmp_path), "--mesh", str(mesh), "--provider", "scripted",
        "--script", str(script),
        *(["--mesh-workers", "1", "--mesh-worker-script", str(worker)] if worker_count else []),
        "--model-calls", "16", "--tool-calls", "24", "--no-time-limit", "--no-token-limit",
        "--image-history-batches", "1"]
    args = runtime_entry.parser().parse_args(["--out", str(original), *common])
    first = asyncio.run(runtime_entry.execute(args))
    assert first["status"] == "transport_error"
    if worker_count:
        assert first["mesh_workers"]["tasks"][0]["status"] == "child_model_budget_exhausted"
    source = ReadOnlyRun(original)
    before = tree_hashes(original)
    requested = runtime_entry.RunLimits.model_validate(EventStore.latest_checkpoint(source)[1]["config"]["limits"])
    inspect_mesh_continuation(original, tmp_path / "not-created", requested, root=root,
        expected_events_sha256=source.evidence_hashes["events.jsonl"])
    assert not (tmp_path / "not-created").exists()
    # A new unjournaled candidate must not become the latest saved state.
    stray = original / "bim/candidate_99/source_model.json"
    stray.parent.mkdir()
    stray.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="file set or bytes"):
        inspect_mesh_continuation(original, tmp_path / "not-created", requested, root=root,
            expected_events_sha256=source.evidence_hashes["events.jsonl"])
    stray.unlink()
    stray.parent.rmdir()
    continued = tmp_path / "continued"
    script.write_text(json.dumps([
        *([_response("new1", "read_mesh_worker", {"task_id": "unfinished"})] if worker_count else []),
        _response("new2", "finish_bim", {"candidate": "candidate_01", "reviews_json": json.dumps({
            "mesh_review": {"coverage": "whole_building", "summary": "synthetic continuation only",
                "overlay_ids": ["overlay_001"]}})})]), encoding="utf-8")
    args = runtime_entry.parser().parse_args(["--out", str(continued), *common,
        "--continue-from", str(original), "--source-events-sha256", source.evidence_hashes["events.jsonl"]])
    def never_prepare(*args, **kwargs):
        raise AssertionError("continuation must not cold start")
    monkeypatch.setattr(runtime_entry, "prepare_inputs", never_prepare)
    result = asyncio.run(runtime_entry.execute(args))
    assert result["completion"] and result["finalization"]["completion_valid"]
    assert result["finalization"]["delivery"]["complete_building"]
    assert result["model_calls"] == 1 + worker_count
    if worker_count:
        assert result["mesh_workers"]["tasks"][0]["status"] == "child_model_budget_exhausted"
    assert tree_hashes(original) == before
    from src.agent.runtime_mesh_continuation import continuation_lineage
    lineage = continuation_lineage(ReadOnlyRun(continued))
    assert len(lineage) == 2
    assert sum(row["elapsed_seconds"] for row in lineage) == result["continuation"]["combined_execution_seconds"]
    events = EventStore.read_events(continued / "events.jsonl")
    assert [e.payload.tool_name for e in events if e.payload.event_type == "tool_invocation"] == (
        ["read_mesh_worker", "finish_bim"] if worker_count else ["finish_bim"])
    imported = json.loads((continued / "continuation_manifest.json").read_bytes())
    assert imported["effective_limits"]["model_calls"] == 12 - 2 * worker_count
    assert imported["effective_limits"]["tool_calls"] == 21 - 2 * worker_count
    assert imported["effective_limits"]["seconds"] is None
    args.continue_from = args.source_events_sha256 = None
    args.resume = True
    resumed = asyncio.run(runtime_entry.execute(args))
    assert resumed["completion"]
    assert resumed["continuation"] == result["continuation"]
    if worker_count:
        assert resumed["whole_run_counts"] == result["whole_run_counts"]
    assert tree_hashes(original) == before
