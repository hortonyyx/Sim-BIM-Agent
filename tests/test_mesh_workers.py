"""Offline native worker dispatch over the real shared Runtime and journal."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from PIL import Image
import pytest

import src.agent.runtime_mesh_workers as workers
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.budget import PriceSchedule, RuntimeBudget
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.harness_contracts import (MissingCapture, RunLifecyclePayload, ToolExecutionPayload,
                                  ToolGrant, ToolInvocationPayload, UsageMissing)
from src.agent.runtime_tools import coordinator_role
from test_agent_runtime import response, versions


ROOT = Path(__file__).resolve().parents[1]


def _answer(*, seen=False):
    return json.dumps({"directly_seen": ([{"observation_id": "mark", "statement": "A red mark is visible.",
        "evidence_kind": "image", "evidence_id": "sketch.png"}] if seen else []),
        "interpretations": [], "uncertain": [{"uncertainty_id": "u", "statement": "Scale is not established."}]})


class _Frozen:
    def __init__(self, run_directory, role=None):
        self.run_directory, self.role, self.calls = Path(run_directory), role, []

    async def list_tools(self):
        return [{"name": "view_image", "description": "Read admitted auxiliary image", "inputSchema": {
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"name": "inputs", "description": "Read inputs", "inputSchema": {"type": "object"}}]

    def repeatability(self, name):
        return "read_only"

    def recovery_policy(self, name):
        return "retry_read"

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "view_image":
            image = (self.run_directory / "images" / arguments["name"]).read_bytes()
            value = {"name": arguments["name"], "image_sha256": hashlib.sha256(image).hexdigest(),
                     "returned_png_sha256": hashlib.sha256(image).hexdigest()}
            return {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)},
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(image).decode()}]}
        return {"content": [{"type": "text", "text": "admitted inputs"}]}

    def snapshot_state(self):
        return {str(path.relative_to(self.run_directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.run_directory.rglob("*")) if path.is_file()}

    def image_origins(self, raw):
        return {}

    def artifacts(self):
        return []


@pytest.fixture
def local_tools(monkeypatch):
    children = []

    @asynccontextmanager
    async def client(run_directory, **kwargs):
        assert kwargs["readonly"] is True
        yield run_directory

    def frozen(client, role, *, run_directory):
        assert role.read_only is True
        value = _Frozen(run_directory, role)
        children.append(value)
        return value

    monkeypatch.setattr(workers, "frozen_bim_client", client)
    monkeypatch.setattr(workers, "FrozenBimTools", frozen)
    monkeypatch.setattr(workers, "make_versions", lambda *args, **kwargs: versions())
    return children


def _inputs(directory):
    run = directory / "bim"
    (run / "assets").mkdir(parents=True)
    mesh = run / "assets" / "input.glb"
    mesh.write_bytes(b"offline opaque GLB fixture; no geometry tool runs")
    (run / "images").mkdir()
    image = run / "images" / "sketch.png"
    Image.new("RGB", (8, 8), "red").save(image)
    manifest = {"mesh_input": {"frozen_path": "assets/input.glb", "sha256": hashlib.sha256(mesh.read_bytes()).hexdigest()},
        "images": {"sketch.png": {"sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "size": [8, 8]}},
        "image_kind": "unknown", "input_mode": "mesh"}
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def _limits(**overrides):
    return RunLimits(**{"model_calls": 12, "tool_calls": 20, "seconds": 600,
                        "tokens": 500_000, **overrides})


def _task(task_id="inspect", **budget):
    return {"task_id": task_id, "question": "Assess local uncertainty without assuming scale.",
            "budget": {"model_calls": 3, "tool_calls": 3, "seconds": 60, "tokens": 100_000, **budget}}


def _wrapper(store, run, limits, fixtures, *, max_concurrent=2, factory=None):
    created = []

    def adapter(task_id, child):
        offset = sum(event.payload.event_type == "adapter_request" for event in child.events)
        result = ScriptedAdapter(fixtures[task_id][offset:])
        created.append(result)
        return result

    value = workers.MeshWorkerTools(_Frozen(run), store=store, root=ROOT,
        adapter_factory=factory or adapter, model="test", parameters={"max_tokens": 256},
        route={"route_id": "offline", "model": "test", "context": {"enabled": False}},
        limits=limits, start_epoch=time.time(), max_concurrent=max_concurrent)
    return value, created


def _start(store):
    store.append(RunLifecyclePayload(action="start", reason="offline native coordinator fixture"))


def _result(raw):
    assert not raw.get("isError"), raw
    return raw["structuredContent"]["results"][0]


def _root_engine(store, tools, limits, replies):
    role = coordinator_role(limits.ledger_limit())
    role = role.model_copy(update={"tool_whitelist": (*role.tool_whitelist,
        *(ToolGrant(tool_name=name, access="read") for name in workers.MESH_WORKER_TOOL_NAMES))})
    return Runtime(store=store, adapter=ScriptedAdapter(replies), tools=tools, role=role,
        model="test", parameters={"max_tokens": 256}, versions=versions(), limits=limits,
        root_tool_calls=limits.tool_calls, start_epoch=tools.start_epoch)


def test_native_parent_child_images_and_usage_share_one_journal(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-native", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [
            response(("child-view", "view_image", {"name": "sketch.png"})), response(text=_answer(seen=True))]})
        engine = _root_engine(store, wrapper, limits, [
            response(("delegate", "delegate_mesh_workers", {"tasks": [_task()]})),
            response(("read", "read_mesh_worker", {"task_id": "inspect", "view_ids": ["sketch.png"]})),
            response(text="Coordinator retained decision authority.")])
        result = asyncio.run(engine.run([{"role": "user", "content": "Investigate a local uncertainty."}]))
        assert result["status"] == "completed"
        assert result["model_calls"] == 3  # Parent count is not the whole-run count.
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 5
        assert len(adapters) == 1 and len(adapters[0].requests) == 2
        assert RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.committed.calls == 5
        read = next(event for event in store.events if event.payload.event_type == "tool_execution"
                    and event.payload.tool_name == "read_mesh_worker")
        raw = store.resolve(read.payload.raw_result)
        assert any(block["type"] == "image" for block in raw["content"])
        assert raw["structuredContent"]["result"]["candidate_mutation"] is False
        preview = raw["structuredContent"]["evidence_previews"][0]
        assert preview["view_id"] == "mesh-worker:inspect:sketch.png"
        assert wrapper.image_origins(raw)[preview["returned_png_sha256"]]["view_id"] == preview["view_id"]
        assert not any(event.payload.event_type == "external_coordinator_mcp" for event in store.all_events)
        assert wrapper.state()["tasks"][0]["status"] == "completed"
        assert local_tools[0].run_directory != run
        store.validate()


def test_worker_capability_is_optional_with_no_child_requests(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-self", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, run, limits, {})
        engine = _root_engine(store, wrapper, limits, [response(text="Handled directly.")])
        assert asyncio.run(engine.run([{"role": "user", "content": "Observe if needed."}]))["status"] == "completed"
        assert adapters == [] and wrapper.state()["tasks"] == []
        assert wrapper.recovery_policy("delegate_mesh_workers") == "resume_task"
        assert wrapper.repeatability("delegate_mesh_workers") == "read_only"


@pytest.mark.parametrize("parent_seconds,child_seconds", [(None, None), (None, 30), (60, None)])
def test_worker_budget_context_preserves_unlimited_and_inherited_time(
        tmp_path, local_tools, parent_seconds, child_seconds):
    run, limits = _inputs(tmp_path), _limits(seconds=parent_seconds, tokens=None)
    with EventStore(tmp_path / "audit", run_id="mesh-context-time", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [
            response(("child-image", "view_image", {"name": "sketch.png"})),
            response(text=_answer(seen=True))]})
        wrapper.route["context"] = ContextPolicy(image_history_batches=1).model_dump(mode="json")
        task = _task(model_calls=2, tool_calls=2, tokens=None, seconds=child_seconds)
        engine = _root_engine(store, wrapper, limits, [
            response(("delegate", "delegate_mesh_workers", {"tasks": [task]})),
            response(text="The worker result remains subject to coordinator review.")])
        parent_receipt = asyncio.run(engine.run([{"role": "user", "content": "Observe the admitted image."}]))
        assert parent_receipt["status"] == "completed"
        assert wrapper.state()["tasks"][0]["status"] == "completed"
        assert len(adapters) == 1 and len(adapters[0].requests) == 2
        assert local_tools[0].calls == [("view_image", {"name": "sketch.png"})]
        child = store.for_task("inspect", parent_task_id=store.task_id)
        executions = [e for e in child.events if e.payload.event_type == "tool_execution"]
        assert len(executions) == 1 and executions[0].payload.outcome == "succeeded"
        second_request = json.loads(adapters[0].requests[1])
        state_messages = [m["content"] for m in second_request["messages"]
            if m["role"] == "user" and isinstance(m.get("content"), str)
            and m["content"].startswith("Current runtime state")]
        assert len(state_messages) == 1
        entries = json.loads(state_messages[0].partition(": ")[2])
        remaining = next(e["value"] for e in entries if e["key"] == "observer-remaining-budget")
        assert remaining["model_calls"] == 1 and remaining["tool_calls"] == 1
        assert remaining["tokens"] is None
        timing = json.loads((child.task_directory / "mesh_worker.json").read_bytes())["timing"]
        stop = next(e for e in reversed(child.events) if e.payload.event_type == "run_lifecycle"
                    and e.payload.action == "stop")
        receipt_ref = next(r.blob for r in stop.source_refs if r.source_id == "run-receipt")
        receipt = json.loads(child.get_bytes(receipt_ref))
        assert receipt["status"] == "completed" and receipt["model_calls"] == 2 and receipt["tool_calls"] == 1
        if parent_seconds is None and child_seconds is None:
            assert remaining["seconds"] is None
            assert timing["deadline_epoch"] is None and timing["time_budget_seconds"] is None
            assert receipt["limits"]["seconds"] is None and receipt["deadline_epoch"] is None
        else:
            ceiling = child_seconds if child_seconds is not None else parent_seconds
            assert 0 < remaining["seconds"] <= ceiling
            assert 0 < timing["time_budget_seconds"] <= ceiling
            assert timing["deadline_epoch"] is not None and receipt["deadline_epoch"] is not None
            assert receipt["limits"]["seconds"] <= ceiling
        # Null child limits still use the same root ledger; no private budget or
        # implicit deadline is introduced by the context projection.
        assert sum(e.payload.event_type == "adapter_request" for e in store.all_events) == 4
        assert RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.committed.calls == 4
        assert parent_receipt["model_calls"] == 2
        store.validate()


@pytest.mark.parametrize("arguments", [
    {"tasks": [_task(), _task()]},
    {"tasks": [{"task_id": "x", "question": "q"}]},
    {"tasks": [{**_task(), "mesh_path": "not-admitted.glb"}]},
    {"tasks": [_task("coordinator")]},
])
def test_invalid_dispatch_is_rejected_before_worker_requests(tmp_path, local_tools, arguments):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-reject", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {})
        raw = asyncio.run(wrapper.call_tool("delegate_mesh_workers", arguments))
        assert raw["isError"] and adapters == [] and wrapper.state()["tasks"] == []


@pytest.mark.parametrize("tokens", [6500, 7000, 32000])
@pytest.mark.parametrize("output_parameter", ["max_tokens", "max_completion_tokens"])
def test_infeasible_new_token_budget_rejected_before_freezing_inputs(
        tmp_path, local_tools, tokens, output_parameter):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-token-preflight", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        wrapper.parameters = {output_parameter: 32000}
        configuration = wrapper._configuration()
        before = list(store.all_events)
        result = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {
            "tasks": [_task(tokens=tokens)]})))
        assert result["status"] == "rejected" and result["reason_code"] == "worker_token_budget_infeasible"
        assert result["stage"] == "budget_preflight" and result["admitted"] is False
        assert result["result"] is None and result["model_calls"] == result["tool_calls"] == 0
        assert result["budget"]["configured_task_tokens"] == tokens
        assert result["budget"]["output_token_limit"] == 32000
        assert result["budget"]["minimum_task_tokens"] == 32001
        assert "tokens=null" in result["reason"] and "not evidence" in result["meaning"]
        assert list(store.all_events) == before and wrapper.state()["tasks"] == []
        child = store.for_task("inspect", parent_task_id=store.task_id)
        assert not (child.task_directory / "bim").exists()
        assert not (child.task_directory / "mesh_worker.json").exists()
        assert child.events == [] and adapters == [] and local_tools == []
        assert wrapper._configuration() == configuration
        # Rejection did not admit this ID or freeze the bad budget. Null adds no
        # child cap; the real shared Runtime still reserves against the root.
        accepted = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {
            "tasks": [_task(tokens=None)]})))
        assert accepted["status"] == "completed" and len(adapters) == 1
        assert len(adapters[0].requests) == 1
        assert wrapper.parameters == {output_parameter: 32000}
        record = json.loads((child.task_directory / "mesh_worker.json").read_bytes())
        assert record["task"]["budget"]["tokens"] is None
        assert record["configuration"] == configuration
        assert RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.committed.calls == 1
        store.validate()


def test_worker_state_and_schema_explain_total_tokens_and_output_reserve(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-budget-contract", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, _ = _wrapper(store, run, limits, {})
        wrapper.parameters = {"max_tokens": 32000}
        state = asyncio.run(wrapper.call_tool("mesh_worker_state", {}))["structuredContent"]
        contract = state["token_budget"]
        assert contract["output_token_limit"] == 32000 and contract["minimum_task_tokens"] == 32001
        assert "Necessary lower bound only" in contract["minimum_task_tokens_basis"]
        assert "across all requests" in contract["tokens_meaning"]
        assert "not a per-response output cap" in contract["tokens_meaning"]
        schema = next(tool for tool in workers.mesh_worker_tool_specs()
                      if tool["name"] == "delegate_mesh_workers")["inputSchema"]
        description = schema["properties"]["tasks"]["items"]["properties"]["budget"]["properties"]["tokens"]["description"]
        assert contract["tokens_meaning"] in description and "mesh_worker_state" in description


def test_preflight_does_not_reject_legacy_admitted_zero_call_result(tmp_path, local_tools, monkeypatch):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-legacy-budget", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": []})
        task = _task(tokens=1)
        # Reproduce a pre-preflight admission using the ordinary Runtime and
        # its immutable outcome, including the historical zero-call stop.
        with monkeypatch.context() as legacy:
            legacy.setattr(wrapper, "_new_task_budget_rejection", lambda task: None)
            saved = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]})))
        assert saved["status"] == "child_token_budget_exhausted" and saved["result"] is None
        assert saved["runtime"]["model_calls"] == 0
        before = list(store.all_events)
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]}))) == saved
        read = asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect"}))
        assert not read["isError"] and read["structuredContent"] == saved
        assert list(store.all_events) == before and len(adapters) == 1 and adapters[0].requests == []
        changed = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {
            "tasks": [_task(tokens=None)]})))
        assert changed["status"] == "rejected" and "different exact arguments" in changed["reason"]
        wrapper.parameters["max_tokens"] = 512
        assert asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect"}))["isError"]
        store.validate()


def test_completed_id_reuses_result_but_argument_config_or_asset_changes_fail(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-reuse", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        first = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert first["status"] == "completed"
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]}))) == first
        assert len(adapters) == 1
        changed = {**_task(), "question": "A different question"}
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [changed]})))["status"] == "rejected"
        wrapper.parameters["temperature"] = 0
        assert asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect"}))["isError"]
        wrapper.parameters.pop("temperature")
        (run / "assets" / "input.glb").write_bytes(b"changed input")
        assert asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect"}))["isError"]
        assert len(adapters) == 1


def test_parallel_workers_overlap_and_fail_independently(tmp_path, local_tools):
    activity = {"active": 0, "peak": 0}

    class Adapter:
        def __init__(self, fail):
            self.fail = fail

        async def send(self, prepared, *, timeout):
            activity["active"] += 1
            activity["peak"] = max(activity["active"], activity["peak"])
            await asyncio.sleep(0.02)
            activity["active"] -= 1
            if self.fail:
                raise ModelServiceError({"category": "permission_denied", "retryable": False,
                    "http_status": 403, "usage_received": False}, UsageMissing(reason="offline injected refusal"))
            return response(text=_answer())

    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-parallel", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {}, factory=lambda task, child: Adapter(task == "bad"))
        raw = asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task("one"), _task("bad"),
            _task("two"), _task("too-small", tokens=1)]}))
        assert [row["status"] for row in raw["structuredContent"]["results"]] == [
            "completed", "permission_denied", "completed", "rejected"]
        assert activity["peak"] == 2
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 3
        store.validate()


def test_child_cannot_write_finish_or_delegate(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-readonly", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {"inspect": [response(
            ("write", "build_bim", {}), ("finish", "finish_bim", {}),
            ("subtask", "delegate_mesh_workers", {"tasks": [_task("nested")]})), response(text=_answer())]})
        _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert local_tools[0].calls == []
        assert not any(event.task_id == "nested" for event in store.all_events)
        assert not (run / "selected_candidate.json").exists()


def test_root_request_and_tool_limits_include_children(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits(model_calls=1)
    with EventStore(tmp_path / "audit", run_id="mesh-root-limit", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {"one": [response(text=_answer())], "two": []}, max_concurrent=1)
        raw = asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task("one"), _task("two")]}))
        assert [row["status"] for row in raw["structuredContent"]["results"]] == ["completed", "root_model_budget_exhausted"]
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 1


def test_parent_cannot_exceed_root_tool_limit_after_child_spends_it(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits(tool_calls=2)
    with EventStore(tmp_path / "audit", run_id="mesh-root-tools", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, _ = _wrapper(store, run, limits, {"inspect": [
            response(("child", "inputs", {})), response(text=_answer())]})
        engine = _root_engine(store, wrapper, limits, [
            response(("delegate", "delegate_mesh_workers", {"tasks": [_task()]})),
            response(("parent-extra", "inputs", {}))])
        receipt = asyncio.run(engine.run([{"role": "user", "content": "Read if useful."}]))
        assert "tool_budget_exhausted" in receipt["status"]
        assert sum(event.payload.event_type == "tool_invocation" for event in store.all_events) == 2
        assert wrapper.frozen.calls == []
        assert local_tools[0].calls == [("inputs", {})]


def test_child_recovery_replays_durable_response_without_new_request(tmp_path, local_tools, monkeypatch):
    crashed = [False]

    def engine(**kwargs):
        value = Runtime(**kwargs)

        def fault(boundary, current):
            if boundary == "after_response" and not crashed[0]:
                crashed[0] = True
                raise RuntimeError("offline interruption after durable response")

        value.fault_hook = fault
        return value

    monkeypatch.setattr(workers, "Runtime", engine)
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-child-resume", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        first = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert first["status"] == "failed"
        second = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert second["status"] == "completed"
        assert len(adapters) == 2 and adapters[1].requests == []
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 1
        store.validate()


def test_parent_pending_dispatch_recovers_completed_child_without_paid_replay(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-parent-resume", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        first = _root_engine(store, wrapper, limits, [
            response(("delegate", "delegate_mesh_workers", {"tasks": [_task()]}))])

        def fault(boundary, current):
            if boundary == "after_tool":
                raise RuntimeError("interrupted after child saved handoff, before parent receipt")

        first.fault_hook = fault
        assert asyncio.run(first.run([{"role": "user", "content": "Observe as needed."}]))["status"] == "failed"
        assert len(adapters) == 1
        assert asyncio.run(wrapper.recover_pending())
        resumed = _root_engine(store, wrapper, limits, [response(text="Accepted saved advice.")])
        assert asyncio.run(resumed.run([], resume=True))["status"] == "completed"
        assert len(adapters) == 1
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 3
        assert sum(event.payload.event_type == "tool_execution" and event.payload.tool_name == "delegate_mesh_workers"
                   for event in store.events) == 1
        store.validate()


def test_child_timing_with_unlimited_parent_is_persisted_not_reset(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits(seconds=None)
    with EventStore(tmp_path / "audit", run_id="mesh-clock", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {"inspect": []})
        # Above the necessary output-only floor, but below the real input +
        # output reservation: Runtime still enforces the full request budget.
        task = _task(tokens=257, seconds=20)
        _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]})))
        child = store.for_task("inspect", parent_task_id=store.task_id)
        before = json.loads((child.task_directory / "mesh_worker.json").read_bytes())["timing"]
        _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [task]})))
        after = json.loads((child.task_directory / "mesh_worker.json").read_bytes())["timing"]
        assert after == before and after["time_budget_seconds"] == 20
        assert after["deadline_epoch"] - after["started_epoch"] == 20


def test_pricing_is_shared_with_child_and_bound_for_resume(tmp_path, local_tools):
    run, limits = _inputs(tmp_path), _limits(money_usd=Decimal("1"))
    with EventStore(tmp_path / "audit", run_id="mesh-money", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        wrapper.pricing = PriceSchedule(model_route="offline", source="offline fixture rates",
            input_usd_per_million=Decimal("1"), output_usd_per_million=Decimal("2"))
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))["status"] == "completed"
        budget = RuntimeBudget.from_events(store.budget_limit, store.all_events)
        assert budget.ledger.committed.money_usd > 0
        wrapper.pricing = None
        assert asyncio.run(wrapper.call_tool("read_mesh_worker", {"task_id": "inspect"}))["isError"]


def test_parallel_workers_cannot_double_reserve_remaining_root_tokens(tmp_path, local_tools):
    class Adapter:
        async def send(self, prepared, *, timeout):
            await asyncio.sleep(0.03)
            return response(text=_answer())

    run, limits = _inputs(tmp_path), _limits(tokens=4500)
    with EventStore(tmp_path / "audit", run_id="mesh-token-holds", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, _ = _wrapper(store, run, limits, {}, factory=lambda task, child: Adapter())
        raw = asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task("one"), _task("two")]}))
        statuses = [row["status"] for row in raw["structuredContent"]["results"]]
        assert statuses.count("completed") == 1
        assert statuses.count("root_token_budget_exhausted") == 1
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 1
        assert RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.committed.tokens <= limits.tokens
        store.validate()


@pytest.mark.parametrize("disk_receipt", ["unchanged", "missing", "forged"])
def test_recovery_uses_journal_receipt_not_editable_receipt_file(tmp_path, local_tools, monkeypatch, disk_receipt):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-receipt-binding", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        save = wrapper._save_outcome

        def interrupted(*args):
            raise RuntimeError("interruption after child runtime receipt")

        monkeypatch.setattr(wrapper, "_save_outcome", interrupted)
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))["status"] == "child_failed"
        child = store.for_task("inspect", parent_task_id=store.task_id)
        receipt_path = child.task_directory / "receipt.json"
        if disk_receipt == "missing":
            receipt_path.unlink()
        elif disk_receipt == "forged":
            receipt_path.write_text('{"status":"completed","answer":"forged"}')
        monkeypatch.setattr(wrapper, "_save_outcome", save)
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))["status"] == "completed"
        assert len(adapters) == (1 if disk_receipt == "unchanged" else 2)
        assert [len(adapter.requests) for adapter in adapters] == ([1] if disk_receipt == "unchanged" else [1, 0])
        receipt = json.loads(receipt_path.read_bytes())
        assert receipt["status"] == "completed" and receipt["answer"] == _answer()
        assert receipt["model_calls"] == 1 and receipt["usage_complete"]
        store.validate()


def test_worker_rebuilds_receipt_after_completed_stop_then_minimal_memory_failure(tmp_path, local_tools, monkeypatch):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-memory-receipt", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        capture, put_json = EventStore.capture, EventStore.put_json

        def fail_completed_capture(current, value, **kwargs):
            if (current.task_id == "inspect" and isinstance(value, dict)
                    and value.get("status") == "completed" and "artifact_paths" in value):
                raise MemoryError("offline failure after completed stop was journaled")
            return capture(current, value, **kwargs)

        def fail_failed_receipt(current, value):
            if (current.task_id == "inspect" and isinstance(value, dict)
                    and value.get("status") == "failed" and "artifact_paths" in value):
                raise MemoryError("offline failure while saving normal failure receipt")
            return put_json(current, value)

        with monkeypatch.context() as fault:
            fault.setattr(EventStore, "capture", fail_completed_capture)
            fault.setattr(EventStore, "put_json", fail_failed_receipt)
            first = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert first["status"] == "failed"
        child = store.for_task("inspect", parent_task_id=store.task_id)
        receipt_path = child.task_directory / "receipt.json"
        failed = json.loads(receipt_path.read_bytes())
        assert failed["status"] == "failed" and failed["receipt_kind"] == "minimal_memory_failure"
        assert wrapper._anchored(child, "mesh-worker-outcome") is None
        saved_receipts = [source.blob for event in child.events for source in event.source_refs
                          if source.source_id == "run-receipt" and source.blob is not None]
        assert json.loads(child.get_bytes(saved_receipts[-1]))["status"] == "completed"

        resumed = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert resumed["status"] == "completed"
        rebuilt = json.loads(receipt_path.read_bytes())
        assert rebuilt["status"] == "completed" and "receipt_kind" not in rebuilt
        assert rebuilt["answer"] == _answer() and rebuilt["model_calls"] == 1
        assert rebuilt["usage_complete"] and rebuilt["request_usage_accounting"]
        assert [len(adapter.requests) for adapter in adapters] == [1, 0]
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 1
        assert sum(event.payload.event_type == "tool_execution" for event in store.all_events) == 0
        assert all(not tool.calls for tool in local_tools)
        store.validate()


def test_child_version_catalog_includes_enabled_context_image_tool(tmp_path, local_tools, monkeypatch):
    catalogs, interrupted = [], [False]

    def make_versions(*args, **kwargs):
        catalogs.append(kwargs["tools"])
        return versions()

    def runtime(**kwargs):
        engine = Runtime(**kwargs)

        def fault(boundary, current):
            if (boundary == "after_response" and not interrupted[0] and
                    sum(event.payload.event_type == "model_response" for event in current.store.events) == 2):
                interrupted[0] = True
                raise RuntimeError("interrupt after worker viewed image and saved final response")

        engine.fault_hook = fault
        return engine

    monkeypatch.setattr(workers, "make_versions", make_versions)
    monkeypatch.setattr(workers, "Runtime", runtime)
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-context-identity", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [
            response(("child-image", "view_image", {"name": "sketch.png"})), response(text=_answer(seen=True))]})
        # The native entry persists the complete JSON policy, including tuple
        # fields serialized as lists; strict Python validation is not equivalent.
        wrapper.route["context"] = ContextPolicy(image_history_batches=1).model_dump(mode="json")
        assert wrapper.route["context"]["pinned_tags"] == []
        first = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert first["status"] == "failed"
        resumed = _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", {"tasks": [_task()]})))
        assert resumed["status"] == "completed"
        assert resumed["result"]["directly_seen"][0]["evidence"]["id"] == "sketch.png"
        assert "read_context_image" in {tool["function"]["name"] for tool in catalogs[0]}
        assert len(adapters) == 2 and len(adapters[0].requests) == 2 and adapters[1].requests == []
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 2
        assert local_tools[0].calls == [("view_image", {"name": "sketch.png"})]
        assert local_tools[1].calls == []
        child = store.for_task("inspect", parent_task_id=store.task_id)
        saved_role = child.latest_checkpoint()[1]["config"]["role"]
        assert sum(grant["tool_name"] == "read_context_image" for grant in saved_role["tool_whitelist"]) == 1
        store.validate()


def test_parent_retry_interruption_retains_retry_event_link(tmp_path, local_tools, monkeypatch):
    run, limits = _inputs(tmp_path), _limits()
    with EventStore(tmp_path / "audit", run_id="mesh-second-interruption", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        _start(store)
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        arguments = {"tasks": [_task()]}
        invocation = store.append(ToolInvocationPayload(call_id="dispatch", tool_name="delegate_mesh_workers",
            full_arguments=arguments, repeatability="read_only", state_before=store.put_json(wrapper.snapshot_state())))
        assert _result(asyncio.run(wrapper.call_tool("delegate_mesh_workers", arguments)))["status"] == "completed"
        store.append(ToolExecutionPayload(call_id="dispatch", tool_name="delegate_mesh_workers",
            full_arguments=arguments, repeatability="read_only", invocation_event_id=invocation.event_id,
            outcome="unknown", raw_result=MissingCapture(reason="interrupted before parent captured result"),
            shown_result=MissingCapture(reason="not yet presented")))
        original_call = wrapper.call_tool

        async def second_interruption(*args, **kwargs):
            raise RuntimeError("interruption after retry invocation, before result capture")

        monkeypatch.setattr(wrapper, "call_tool", second_interruption)
        with pytest.raises(RuntimeError, match="after retry invocation"):
            asyncio.run(wrapper.recover_pending())
        retries = [event for event in store.events
                   if event.payload.event_type == "run_lifecycle" and event.payload.action == "retry"]
        assert len(retries) == 1
        monkeypatch.setattr(wrapper, "call_tool", original_call)
        assert asyncio.run(wrapper.recover_pending())
        execution = next(event for event in reversed(store.events)
                         if event.payload.event_type == "tool_execution" and event.payload.outcome == "succeeded")
        assert execution.payload.retry_event_id == retries[0].event_id
        assert len(adapters) == 1
        assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 1
        assert sum(event.payload.event_type == "run_lifecycle" and event.payload.action == "retry"
                   for event in store.events) == 1
        store.validate()
