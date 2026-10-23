"""Model-facing worker projections never replace canonical measurement evidence."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from src.agent.geometry.mesh_observation import MeshObservation
from src.agent.runtime_mesh_evidence import mesh_worker_measurements
from src.agent.runtime_mesh_continuation import ContinuedMeshWorkers
from src.agent.runtime_mesh_workers import _measurement_summary
from src.agent_runtime.budget import RuntimeBudget
from src.agent_runtime.responses import build_responses_body
from src.agent_runtime.store import EventStore
from src.harness_contracts import ToolExecutionPayload, ToolInvocationPayload
from test_agent_runtime import response, runtime, Tools
from test_mesh_delivery_review import prepared
from test_mesh_worker_evidence import _packet, _store, _record, _render, _answer
from test_mesh_workers import local_tools, _Frozen, _limits, _wrapper, _task, _root_engine


def _measure(child):
    return MeshObservation(child / "assets/input.glb").pixel_query(
        child / "mesh_observations/mesh_001", [[12, 12], [13, 12]])


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("interruption", [None, "after_tool", "after_execution"])
def test_worker_projection_and_selected_full_read_survive_replay(
        tmp_path, prepared, local_tools, monkeypatch, native, interruption):
    original_list, original_call = _Frozen.list_tools, _Frozen.call_tool

    async def list_tools(self):
        return [*await original_list(self), {"name": "measure_mesh_pixels", "description": "query",
            "inputSchema": {"type": "object"}}]

    async def call_tool(self, name, arguments):
        if name == "measure_mesh_pixels":
            self.calls.append((name, arguments))
            return {"structuredContent": _measure(self.run_directory)}
        return await original_call(self, name, arguments)

    monkeypatch.setattr(_Frozen, "list_tools", list_tools)
    monkeypatch.setattr(_Frozen, "call_tool", call_tool)

    def reply(call_id, name, arguments):
        if not native:
            return response((call_id, name, arguments))
        return {"object": "response", "id": call_id, "model": "test", "status": "completed",
            "output": [{"type": "function_call", "call_id": call_id, "name": name,
                        "arguments": json.dumps(arguments)}],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}

    answer = _answer(measurement_call_id="measure-call")
    answer["directly_seen"].append({**answer["directly_seen"][0], "observation_id": "seen-2"})
    task = {**_task(), "observation_ids": ["mesh_001"]}
    limits = _limits()
    with EventStore(tmp_path / "audit", run_id="projection", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, prepared[0].run, limits, {"inspect": [
            reply("measure-call", "measure_mesh_pixels", {"observation": "mesh_001", "pixels": [[12, 12], [13, 12]]}),
            response(text=json.dumps(answer))]})
        rest = [reply("read", "read_mesh_worker", {"task_id": "inspect"}),
            reply("detail", "read_mesh_worker", {"task_id": "inspect", "measurement_call_ids": ["measure-call"]}),
            reply("view", "read_mesh_worker", {"task_id": "inspect", "view_ids": ["mesh_001"]}),
            response(text="Advice inspected.")]
        first = _root_engine(store, wrapper, limits, [
            reply("delegate", "delegate_mesh_workers", {"tasks": [task]}), *([] if interruption else rest)])
        if interruption:
            def fault(boundary, current):
                if boundary == interruption:
                    raise RuntimeError("offline parent interruption")
            first.fault_hook = fault
        outcome = asyncio.run(first.run([{"role": "user", "content": "Inspect two measured points."}]))
        requests = list(first.adapter.requests)
        if interruption:
            assert outcome["status"] == "failed"
            asyncio.run(wrapper.recover_pending())
            resumed = _root_engine(store, wrapper, limits, rest)
            outcome = asyncio.run(resumed.run([], resume=True))
            requests += resumed.adapter.requests
        assert outcome["status"] == "completed"
        assert len(adapters) == 1 and len(adapters[0].requests) == 2
        assert RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger.committed.calls == 7
        executions = {event.payload.call_id: event for event in store.events
                      if event.payload.event_type == "tool_execution"}
        delegate = store.resolve(executions["delegate"].payload.raw_result)
        canonical = delegate["structuredContent"]["results"][0]["result"]
        full = canonical["directly_seen"][0]["computed_measurement"]["result"]
        assert "triangle_vertices_xyz" in json.dumps(full)
        child = wrapper._child("inspect")
        saved = json.loads((child.task_directory / "mesh_worker_outcome.json").read_bytes())
        assert saved["result"] == canonical
        for call_id in ("delegate", "read", "view"):
            shown = store.resolve(executions[call_id].payload.shown_result)
            text = shown["tool_message"]["content"]
            assert "triangle_vertices_xyz" not in text
            visible, _ = json.JSONDecoder().raw_decode(text)
            result = visible["results"][0]["result"] if call_id == "delegate" else visible["result"]
            assert len(result["measurement_summaries"]) == 1
            assert len(result["directly_seen"]) == 2
            for obs in result["directly_seen"]:
                assert "result" not in obs["computed_measurement"]
                assert obs["computed_measurement"]["tool_call_id"] == "measure-call"
            summary = result["measurement_summaries"][0]
            assert summary["full_result_read"]["arguments"] == {
                "task_id": "inspect", "measurement_call_ids": ["measure-call"]}
            assert summary["result"]["queries"][0]["world_xyz"] == full["queries"][0]["world_xyz"]
            assert summary["result"]["queries"][0]["measurement_valid"] is True
            surface = summary["result"]["queries"][0]["surface_summary"]
            for key, value in surface.items():
                assert value == full["queries"][0]["triangle_surface_evidence"][key]
        detail = json.loads(store.resolve(executions["detail"].payload.shown_result)["tool_message"]["content"])
        assert detail["full_measurements"][0]["result"] == full
        assert detail["full_measurements"][0]["tool_result_sha256"] == canonical["directly_seen"][0]["computed_measurement"]["tool_result_sha256"]
        preview = store.resolve(executions["view"].payload.shown_result)
        raw_preview = store.resolve(executions["view"].payload.raw_result)
        assert preview["images"] and wrapper.image_origins(raw_preview)
        before = copy.deepcopy(delegate)
        wrapper.project_tool_result("delegate_mesh_workers", delegate)
        assert delegate == before
        for request in requests:
            messages = json.loads(request)["messages"]
            native_body = build_responses_body(model="gpt-6.1-sol", messages=messages, tools=[], parameters={"max_tokens": 256})
            for message in messages:
                if message.get("role") == "tool" and message["tool_call_id"] in {"delegate", "read", "view"}:
                    assert "triangle_vertices_xyz" not in message["content"]
            for row in native_body["input"]:
                if row.get("type") == "function_call_output" and row["call_id"] in {"delegate", "read", "view"}:
                    assert "triangle_vertices_xyz" not in json.dumps(row["output"])
        # Continuation uses the same compact/read contract for both imported
        # workers and newly delegated workers. Neither path starts a new child.
        continued, _ = _wrapper(store, prepared[0].run, limits, {})
        continued.__class__ = ContinuedMeshWorkers
        continued.start_epoch = wrapper.start_epoch
        for imported in (False, True):
            continued.imported_readers = {"inspect": wrapper} if imported else {}
            for selection in ([], ["measure-call"]):
                raw = asyncio.run(continued.call_tool("read_mesh_worker", {
                    "task_id": "inspect", "measurement_call_ids": selection}))
                assert not raw.get("isError"), raw
                projected = continued.project_tool_result("read_mesh_worker", raw)["structuredContent"]
                assert len(projected["result"]["measurement_summaries"]) == 1
                if selection:
                    assert projected["full_measurements"][0]["result"] == full
        # A worker whose final outcome was interrupted still exposes its
        # journaled measurements through this exact API, without an answer.
        anchored = wrapper._anchored
        monkeypatch.setattr(wrapper, "_anchored", lambda current, name:
            None if name == "mesh-worker-outcome" else anchored(current, name))
        recovered = asyncio.run(wrapper.call_tool("read_mesh_worker", {
            "task_id": "inspect", "measurement_call_ids": ["measure-call"]}))["structuredContent"]
        assert recovered["status"] == "incomplete" and recovered["result"] is None
        assert recovered["full_measurements"][0]["result"] == full
        assert len(adapters) == 1
        store.validate()


@pytest.mark.parametrize("failure", ["unknown", "non_measurement", "failed", "cross_worker", "argument", "tamper"])
def test_full_measurement_read_rejects_unverified_evidence(prepared, failure):
    _, child, packet = _packet(prepared)
    _, _, view = _render(child)
    with _store(child) as store:
        _record(store, packet, child, "view_mesh", {}, view, call_id="view-call")
        measurement = _measure(child)
        raw = _record(store, packet, child, "measure_mesh_pixels", {"observation": "mesh_001"},
                      {"structuredContent": measurement}, call_id="measure-call")
        call_id = "measure-call"
        if failure == "unknown":
            call_id = "other-worker-call"
        elif failure == "non_measurement":
            call_id = "view-call"
        elif failure == "cross_worker":
            store.task_id = "another-worker"
        elif failure in {"failed", "argument"}:
            call_id = "bad-call"
            arguments = {"observation": "mesh_999" if failure == "argument" else "mesh_001"}
            intent = store.append(ToolInvocationPayload(call_id=call_id, tool_name="measure_mesh_pixels",
                full_arguments=arguments, repeatability="read_only"))
            store.append(ToolExecutionPayload(call_id=call_id, tool_name="measure_mesh_pixels",
                full_arguments=arguments, repeatability="read_only", outcome="failed" if failure == "failed" else "succeeded",
                raw_result=store.capture(raw, force_blob=True), shown_result=store.capture(raw, force_blob=True),
                invocation_event_id=intent.event_id))
        else:
            path = child / "mesh_observations/mesh_001.json"
            path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(ValueError):
            mesh_worker_measurements([call_id], packet, store, child)


def test_full_read_does_not_require_completed_answer_and_is_bounded(prepared):
    _, child, packet = _packet(prepared)
    _render(child)
    with _store(child) as store:
        full = _measure(child)
        _record(store, packet, child, "measure_mesh_pixels", {"observation": "mesh_001"},
                {"structuredContent": full}, call_id="measure-call")
        assert mesh_worker_measurements(["measure-call"], packet, store, child)[0]["result"] == full
        for selection in (["measure-call"] * 2, ["a", "b", "c", "d"], [""], "measure-call"):
            with pytest.raises(ValueError):
                mesh_worker_measurements(selection, packet, store, child)


def test_compact_invalid_queries_and_aggregate_caveats_remain_explicit():
    full = {"queries": [{"pixel": [1, 2], "hit": False, "measurement_valid": False,
                "reason": "transparent_composite", "measurement_invalid_reason": "No unique surface"},
            {"pixel": [3, 4], "world_xyz": [1, 2, 3], "measurement_valid": True,
                "triangle_surface_evidence": {"triangle_vertices_xyz": [[1, 2, 3]] * 3,
                    "material_geometry_caveat": "Texture is not a boundary"}}],
        "samples": [{"large": "detail"}] * 10, "fidelity": "not_evaluated",
        "depth_residual_summary_m": {"mean_absolute": None}, "valid_sample_count": 0,
        "limitations": ["No-hit is unknown."]}
    compact = _measurement_summary(full)
    assert compact["queries"][0] == full["queries"][0]
    assert compact["queries"][1]["surface_summary"]["material_geometry_caveat"] == "Texture is not a boundary"
    assert compact["limitations"] == full["limitations"] and compact["valid_sample_count"] == 0
    assert "samples" not in compact and full["samples"]


def test_generic_projection_keeps_raw_even_if_hook_mutates_its_copy(tmp_path):
    tools = Tools(tmp_path / "tools")
    def project(name, raw):
        raw["content"][0]["text"] = "visible copy"
        return raw
    tools.project_tool_result = project
    engine = runtime(tmp_path, [response(("save-call", "save", {})), response(text="done")], tools=tools)
    with engine.store:
        assert asyncio.run(engine.run([{"role": "user", "content": "save"}]))["status"] == "completed"
        event = next(e for e in engine.store.events if e.payload.event_type == "tool_execution")
        assert engine.store.resolve(event.payload.raw_result)["content"][0]["text"] == "saved"
        assert engine.store.resolve(event.payload.shown_result)["tool_message"]["content"] == "visible copy"
