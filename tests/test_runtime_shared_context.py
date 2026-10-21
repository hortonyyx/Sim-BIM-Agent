"""Shared context capacity is distinct from output budget and planning headroom."""
import asyncio
import json
from dataclasses import asdict, replace

import httpx
import pytest

from src.agent_runtime import estimation, loop
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.loop import RunLimits
from test_agent_runtime import MESSAGES, response, runtime
from test_mesh_workers import local_tools


def _engine(tmp_path, responses, *, context=True, limits=None, tools=None):
    engine = runtime(tmp_path, responses, limits=limits or RunLimits(
        model_calls=4, tool_calls=3, seconds=30, tokens=None), tools=tools)
    engine.model = "Kimi-K3"
    engine.parameters = {"max_completion_tokens": 1048576, "reasoning_effort": "high", "temperature": 1.0}
    engine.versions = engine.versions.model_copy(update={"remote_model":
        engine.versions.remote_model.model_copy(update={"route_id": "paratera", "remote_alias": engine.model})})
    engine.context_policy = ContextPolicy() if context else None
    return engine


def _decisions(engine):
    return [json.loads(engine.store.get_bytes(ref.blob))
            for event in engine.store.events for ref in event.source_refs
            if ref.source_id == "request-budget-decision"]


def test_native_shared_cap_preserves_images_reasoning_actual_wire_and_budget(tmp_path):
    engine = _engine(tmp_path, [response(("view", "view", {}), reasoning=True), response(text="Done")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        sent = [json.loads(raw) for raw in engine.adapter.requests]
        assert len(sent) == 2
        assert all(131072 < body["max_completion_tokens"] < 1048576 for body in sent)
        assert sent[1]["max_completion_tokens"] < sent[0]["max_completion_tokens"]
        assert any("reasoning_content" in row for row in sent[1]["messages"])
        assert any(block.get("type") == "image_url" for row in sent[1]["messages"]
                   if isinstance(row.get("content"), list) for block in row["content"])
        for body, decision in zip(sent, _decisions(engine), strict=True):
            audit = decision["context_output"]
            assert audit["configured_output_token_limit"] == audit["requested_output_token_limit"] == 1048576
            assert audit["planning_headroom_tokens"] == 131072
            assert audit["effective_output_token_limit"] == body["max_completion_tokens"]
            assert audit["context_window_tokens"] - audit["input_tokens_upper_bound"] == body["max_completion_tokens"]
            assert decision["reservation"]["amounts"]["tokens"] == 1048576
            assert decision["token_estimate"]["output_token_limit"] == body["max_completion_tokens"]
            assert audit["context_source"] and audit["input_estimate_source"] and audit["uncertainty"]
        assert engine.parameters["max_completion_tokens"] == 1048576
        pressure, target = engine._context_pressure()
        assert 0 < target < pressure < 1048576 and pressure > 131072
        assert engine._estimate_context_tokens(MESSAGES, engine.sources[:2]) < 131072
        engine.store.validate()


def test_shared_cap_uses_explicit_context_window_and_does_not_send_zero_room(tmp_path):
    engine = _engine(tmp_path, [], context=False, limits=RunLimits(
        model_calls=1, tool_calls=0, seconds=30, tokens=None, context_tokens=100))
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "context_recovery_required"
        assert engine.adapter.requests == []
        audit = [json.loads(engine.store.get_bytes(ref.blob)) for event in engine.store.events
                 for ref in event.source_refs if ref.source_id == "context-recovery"][-1]["context_output"]
        assert audit["context_window_tokens"] == 100
        assert audit["effective_output_token_limit"] == 0
        assert not _decisions(engine)
        engine.store.validate()


def test_shared_cap_can_grow_again_after_real_context_compaction(tmp_path, monkeypatch):
    profile = replace(estimation.get_model_profile("test"), context_window_tokens=10000,
                      context_output_headroom_tokens=1000, text_safety_factor=1.0, text_fixed_margin=0)
    monkeypatch.setattr(estimation, "get_model_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(loop, "get_model_profile", lambda *args, **kwargs: profile)
    first = response(("large", "error", {}))
    first["choices"][0]["message"]["reasoning_content"] = "observed thought. " * 1500
    engine = _engine(tmp_path, [first, response(("small", "error", {})), response(text="Done")])
    engine.model = "test"
    engine.parameters["max_completion_tokens"] = 10000
    engine.context_policy = ContextPolicy(compact_at_tokens=4000)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed", receipt["status"]
        sent = [json.loads(raw) for raw in engine.adapter.requests]
        caps = [body["max_completion_tokens"] for body in sent]
        assert len(caps) == 3 and caps[1] < caps[0] and caps[2] > caps[1]
        assert any(event.payload.event_type == "context" for event in engine.store.events)
        assert not any("observed thought." in str(row) for row in sent[2]["messages"])
        assert engine.parameters["max_completion_tokens"] == 10000
        engine.store.validate()


def test_finite_budget_reduction_is_never_reexpanded_by_context_capacity(tmp_path):
    engine = _engine(tmp_path, [response(text="Done")], context=False,
        limits=RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=200000, near_limit="reduce_output"))
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        body = json.loads(engine.adapter.requests[0])
        decision = _decisions(engine)[0]
        assert 131072 < body["max_completion_tokens"] < 200000
        assert decision["reservation"]["amounts"]["tokens"] == 200000
        assert decision["context_output"]["requested_output_token_limit"] == body["max_completion_tokens"]
        assert decision["context_output"]["configured_output_token_limit"] == 1048576
        assert decision["near_limit_action"] == "reduce_output"
        engine.store.validate()


def test_transport_retry_recomputes_same_capacity_without_accumulating_reduction(tmp_path):
    engine = _engine(tmp_path, [httpx.ConnectError("offline transport fixture"), response(text="Done")],
        limits=RunLimits(model_calls=2, tool_calls=0, seconds=30, tokens=None,
                         max_model_retries=1, retry_backoff_seconds=0))
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed" and receipt["model_calls"] == 2
        sent = [json.loads(raw) for raw in engine.adapter.requests]
        assert sent[0] == sent[1]
        assert sent[0]["max_completion_tokens"] > 131072
        assert engine.parameters["max_completion_tokens"] == 1048576
        assert len(_decisions(engine)) == 2
        engine.store.validate()


def test_native_worker_inherits_shared_context_policy_and_root_call_budget(tmp_path, local_tools, monkeypatch):
    import src.agent.runtime_mesh_workers as workers
    from src.agent_runtime.budget import RuntimeBudget
    from src.agent_runtime.store import EventStore
    from test_mesh_workers import _answer, _inputs, _root_engine, _task, _wrapper

    run = _inputs(tmp_path)
    limits = RunLimits(model_calls=3, tool_calls=1, seconds=30, tokens=None, time_budget_mode="wall")
    with EventStore(tmp_path / "audit", run_id="shared-context-worker", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        wrapper, adapters = _wrapper(store, run, limits, {"inspect": [response(text=_answer())]})
        wrapper.model = "Kimi-K3"
        wrapper.parameters = {"max_completion_tokens": 1048576, "reasoning_effort": "high", "temperature": 1.0}
        wrapper.route = {"route_id": "paratera", "model": wrapper.model,
                         "context": ContextPolicy().model_dump(mode="json")}
        engine = _root_engine(store, wrapper, limits, [
            response(("delegate", "delegate_mesh_workers", {"tasks": [
                _task(model_calls=1, tool_calls=0, seconds=None, tokens=None)]})), response(text="Done")])
        engine.model, engine.parameters = wrapper.model, dict(wrapper.parameters)
        engine.versions = engine.versions.model_copy(update={"remote_model":
            engine.versions.remote_model.model_copy(update={"route_id": "paratera", "remote_alias": wrapper.model})})
        monkeypatch.setattr(workers, "make_versions", lambda *args, **kwargs: engine.versions)
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert wrapper.state()["tasks"][0]["status"] == "completed"
        assert len(adapters) == 1 and len(adapters[0].requests) == 1
        child_body = json.loads(adapters[0].requests[0])
        assert 131072 < child_body["max_completion_tokens"] < 1048576
        child = store.for_task("inspect", parent_task_id=store.task_id)
        config = child.latest_checkpoint()[1]["config"]
        assert config["parameters"]["max_completion_tokens"] == 1048576
        assert config["model_profile"]["context_output_headroom_tokens"] == 131072
        ledger = RuntimeBudget.from_events(store.budget_limit, store.all_events).ledger
        assert ledger.charged.calls == 3 and ledger.available.calls == 0
        assert all(reservation.amounts.tokens == 1048576 for reservation in ledger.reservations)
        store.validate()


@pytest.mark.parametrize("change_profile", [False, True])
def test_shared_context_resume_freezes_policy_and_keeps_native_configuration(tmp_path, monkeypatch, change_profile):
    engine = _engine(tmp_path, [response(text="Done")])
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
    before = (engine.store.directory / "events.jsonl").read_bytes()
    resumed = _engine(tmp_path, [], tools=engine.tools)
    if change_profile:
        profile = loop.get_model_profile("Kimi-K3")
        monkeypatch.setattr(loop, "get_model_profile", lambda *args, **kwargs:
                            replace(profile, context_output_headroom_tokens=65536))
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == ("resume_configuration_changed" if change_profile else "completed")
        assert resumed.adapter.requests == []
        if not change_profile:
            assert (resumed.store.directory / "events.jsonl").read_bytes() == before
        assert resumed.parameters["max_completion_tokens"] == 1048576


@pytest.mark.parametrize("route,cap", [("paratera", 131072), ("offline", 1048576)])
def test_only_opted_in_paratera_large_output_uses_shared_policy(tmp_path, route, cap):
    engine = _engine(tmp_path, [])
    engine.parameters["max_completion_tokens"] = cap
    engine.versions = engine.versions.model_copy(update={"remote_model":
        engine.versions.remote_model.model_copy(update={"route_id": route})})
    assert engine._context_output_headroom() is None


def test_default_profile_keeps_legacy_frozen_shape_and_can_resume(tmp_path):
    engine = runtime(tmp_path, [response(text="Done")])
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        assert "context_output_headroom_tokens" not in engine.store.latest_checkpoint()[1]["config"]["model_profile"]
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        assert asyncio.run(resumed.run(MESSAGES, resume=True))["status"] == "completed"
        assert resumed.adapter.requests == []


@pytest.mark.parametrize("headroom,window,allowance", [
    (True, 10000, 0), (0, 10000, 0), (-1, 10000, 0), (10000, 10000, 0),
    (1000, None, 0), (1000, 10000, 1),
])
def test_ambiguous_shared_window_profile_is_rejected(headroom, window, allowance):
    row = asdict(estimation.get_model_profile("test"))
    row.update(context_output_headroom_tokens=headroom, context_window_tokens=window,
               reasoning_token_allowance=allowance)
    with pytest.raises(ValueError, match="shared context|context_output_headroom"):
        estimation._profile_from_dict(row)
