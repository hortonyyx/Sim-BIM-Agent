"""Three-turn signed Messages regressions; no external model calls."""
import asyncio
import copy
import hashlib
import io
import json
import base64

import httpx
import pytest
from PIL import Image

from src.agent_runtime.context import ContextPolicy, StateEntry
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.opencode_go import HttpGoAdapter, inference_base_url
from src.agent_runtime.providers import provider_parameters
from src.agent_runtime.adapter import prepare_request
from src.agent_runtime.anthropic import convert_messages
from src.agent_runtime.anthropic_binding import project_bound_thinking
from test_agent_runtime import MESSAGES, Tools, runtime
from test_runtime_recovery_edges import InjectedCrash


def without_cache(block):
    value = copy.deepcopy(block)
    value.pop("cache_control", None)
    if value.get("type") == "tool_result" and isinstance(value.get("content"), list):
        value["content"] = [without_cache(b) for b in value["content"]]
    return value


def prefix(body, messages):
    return {"system": [without_cache(b) for b in body["system"]],
            "tools": body.get("tools", []), "model": body["model"],
            "messages": [{"role": m["role"], "content": [without_cache(b) for b in m["content"]]}
                         for m in messages]}


class SignedServer:
    """Rejects reusing a signature after changing its originating request prefix."""
    def __init__(self):
        self.requests = []
        self.bindings = {}

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        for i, message in enumerate(body["messages"]):
            for block in message["content"]:
                if block.get("type") == "thinking":
                    assert block["thinking"] == "original thought"
                    if self.bindings.get(block["signature"]) != prefix(body, body["messages"][:i]):
                        return httpx.Response(400, json={"error": {
                            "type": "invalid_request_error", "message": "Invalid signature: changed prefix"}})
        count = len(self.requests)
        signature = f"signed-{count}"
        self.bindings[signature] = prefix(body, body["messages"])
        blocks = [{"type": "thinking", "thinking": "original thought", "signature": signature}]
        if count < 3:
            blocks += [{"type": "tool_use", "id": f"view-{count}", "name": "view", "input": {}}]
        else:
            blocks += [{"type": "text", "text": "Done"}]
        return httpx.Response(200, json={"id": f"message-{count}", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": blocks,
            "stop_reason": "tool_use" if count < 3 else "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10}})


class DifferentImages(Tools):
    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        image = io.BytesIO()
        Image.new("RGB", (4, 4), (len(self.calls), 0, 0)).save(image, format="PNG")
        return {"content": [{"type": "image", "mimeType": "image/png",
                             "data": base64.b64encode(image.getvalue()).decode()}]}


def engine_for(tmp_path, server, *, mode, tools=None):
    limits = RunLimits(model_calls=3, tool_calls=2, seconds=60, tokens=500000, max_model_retries=0)
    engine = runtime(tmp_path, [], limits=limits, tools=tools or DifferentImages(tmp_path / "tools"))
    engine.model = "claude-haiku-5-5"
    engine.parameters = provider_parameters("opencode-go", model=engine.model, output_tokens=32000)
    engine.versions = engine.versions.model_copy(update={"remote_model": engine.versions.remote_model.model_copy(
        update={"route_id": "opencode-go", "remote_alias": engine.model})})
    states = mode in {"state", "constant_state", "resume", "response_resume", "state_images"}
    engine.context_policy = (ContextPolicy(image_history_batches=1, append_state_history=states)
                            if mode in {"images", "state_images"} else
                            ContextPolicy(active_window_messages=1) if mode == "compact" else
                            ContextPolicy(append_state_history=states))
    if states:
        def update(active, event, raw):
            if mode == "constant_state" and len(active.tools.calls) > 1:
                return
            active.context.set_state(StateEntry(key="probe-state", category="constraint", value=len(active.tools.calls),
                epistemic_status="computed", source_refs=(active._event_source(event),),
                revision=len(active.tools.calls)))
        engine.context_update = update
    engine.adapter = HttpGoAdapter(base_url=inference_base_url("anthropic_messages"), api_key="offline-test-key", protocol="anthropic_messages",
        session_id="cb6130de-46b0-4f0c-9baa-cb8fc04892d9", transport=httpx.MockTransport(server))
    return engine


@pytest.mark.parametrize("mode", ["unchanged", "state", "constant_state", "images", "compact",
                                  "resume", "response_resume", "state_images"])
def test_three_signed_turns_survive_projection_and_restore(tmp_path, mode):
    server = SignedServer()
    engine = engine_for(tmp_path, server, mode=mode)
    recovering = mode in {"resume", "response_resume"}
    if recovering:
        def crash(boundary, active):
            if ((mode == "resume" and boundary == "after_checkpoint"
                    and len(active.tools.calls) == 2 and not active.pending_response_id)
                    or (mode == "response_resume" and boundary == "after_response"
                        and len(server.requests) == 2)):
                raise InjectedCrash("before third request")
        engine.fault_hook = crash

    async def run(active, resume=False):
        try:
            return await active.run(MESSAGES, resume=resume)
        finally:
            await active.adapter.close()

    with engine.store:
        if recovering:
            with pytest.raises(InjectedCrash):
                asyncio.run(run(engine))
        else:
            result = asyncio.run(run(engine))
    if recovering:
        engine = engine_for(tmp_path, server, mode=mode, tools=engine.tools)
        with engine.store:
            result = asyncio.run(run(engine, True))

    assert result["status"] == "completed"
    assert result["model_calls"] == 3 and result["tool_calls"] == 2
    assert result["usage_complete"] and result["reported_tokens"] == 60
    assert len(engine.tools.calls) == 2
    assert len(server.requests) == 3
    second_thinking = [b for m in server.requests[1]["messages"] for b in m["content"] if b["type"] == "thinking"]
    assert [b["signature"] for b in second_thinking] == ["signed-1"]
    third_thinking = [b for m in server.requests[2]["messages"] for b in m["content"] if b["type"] == "thinking"]
    rewrites = mode in {"images", "compact", "state_images"}
    if not rewrites:
        assert [b["signature"] for b in third_thinking] == ["signed-1", "signed-2"]
    else:
        assert "signed-2" not in [b["signature"] for b in third_thinking]
    responses = [e for e in engine.store.events if e.payload.event_type == "model_response"]
    assert engine.store.resolve(responses[1].payload.raw_response)["content"][0]["signature"] == "signed-2"
    audits = []
    for event in engine.store.events:
        if event.payload.event_type == "adapter_request":
            wire = engine.store.capture_bytes(event.payload.final_request_body)
            assert hashlib.sha256(wire).hexdigest() == event.payload.wire_sha256
            for injected in event.payload.injected_content:
                if injected.source.source_id == "go-messages-thinking-binding":
                    audit = engine.store.resolve(injected.content)
                    assert audit == json.loads(wire)["messages"]
                    audits.append(json.loads(engine.store.get_bytes(injected.source.blob)))
    assert bool(audits) == rewrites
    if mode == "compact":
        assert any(e.payload.event_type == "context" and e.payload.action == "compact" for e in engine.store.events)
    if mode in {"images", "state_images"}:
        assert len([b for m in server.requests[2]["messages"] for b in m["content"] if b["type"] == "image"]) == 1
    if engine.context_policy.append_state_history:
        snapshots = [r for r in engine.context.history if "runtime-state-snapshot" in r.tags]
        assert len(snapshots) == (1 if mode == "constant_state" else 2)
        before = engine.context.full_history()
        for event in engine.store.events:
            if event.payload.event_type == "adapter_request":
                engine.context.commit_request_state(event)
        assert engine.context.full_history() == before
    engine.store.validate()


def test_state_previews_and_removed_constraints_do_not_rewrite_history(tmp_path):
    server = SignedServer()
    engine = engine_for(tmp_path, server, mode="state")
    with engine.store:
        async def run():
            try:
                return await engine.run(MESSAGES)
            finally:
                await engine.adapter.close()
        assert asyncio.run(run())["status"] == "completed"
        manager = engine.context
        previous = next(s for s in manager.state if s.key == "probe-state")
        manager.set_state(previous.model_copy(update={"active": False, "revision": previous.revision + 1}))
        before = manager.full_history()
        first = manager.project(consume_retrievals=False)
        second = manager.project(consume_retrievals=False)
        assert first.messages == second.messages and manager.full_history() == before
        # The empty latest snapshot clears a formerly active constraint.
        assert first.messages[-1]["content"].endswith("[]")
        assert first.sources[-1].source_id == "context-current-state"


def test_current_state_is_reprovided_after_snapshot_compaction(tmp_path):
    server = SignedServer()
    engine = engine_for(tmp_path, server, mode="constant_state")
    with engine.store:
        async def run():
            try:
                return await engine.run(MESSAGES)
            finally:
                await engine.adapter.close()
        assert asyncio.run(run())["status"] == "completed"
        manager = engine.context
        before = manager.full_history()
        projection = manager.project(consume_retrievals=False, force_compact=True,
                                     token_estimator=lambda messages: len(messages), target_tokens=1)
        snapshot = next(r for r in manager.history if "runtime-state-snapshot" in r.tags)
        assert snapshot.history_id in projection.omitted_history_ids
        assert projection.messages[-1] == snapshot.message
        assert projection.sources[-1].source_id == "context-current-state"
        assert manager.full_history() == before


@pytest.mark.parametrize("change", ["none", "suffix_only", "system", "task", "tools", "thinking", "origin", "cache_argument"])
def test_binding_uses_original_request_and_preserves_non_thinking_blocks(tmp_path, change):
    server = SignedServer()
    engine = engine_for(tmp_path, server, mode="unchanged")
    with engine.store:
        async def run():
            try:
                return await engine.run(MESSAGES)
            finally:
                await engine.adapter.close()
        assert asyncio.run(run())["status"] == "completed"
        messages, sources = copy.deepcopy(engine.messages), list(engine.sources)
        tools = copy.deepcopy(engine.specs)
        if change == "system":
            messages[0]["content"] += " changed guide"
        elif change == "task":
            messages[1]["content"] += " changed task"
        elif change == "tools":
            tools[0]["function"]["description"] += " changed tool"
        elif change == "thinking":
            messages[2]["anthropic_content"][0]["thinking"] = "modified thought"
        elif change == "origin":
            sources[2] = engine.store.source("unknown-origin", {"unverified": True})
        elif change == "cache_argument":
            messages[2]["anthropic_content"][-1]["input"]["cache_control"] = "semantic tool argument"
        elif change == "suffix_only":
            messages.append({"role": "user", "content": "new suffix after all signed responses"})
            sources.append(engine.store.source("new-user-turn", {"new": True}, kind="user"))
        original_messages = copy.deepcopy(messages)
        prepared = prepare_request(store=engine.store, model=engine.model, messages=messages,
            message_sources=sources, tools=tools, tool_source=engine.tool_source,
            parameters=engine.parameters, versions=engine.versions)
        assert messages == original_messages
        body = prepared.body
        thoughts = [b for m in body["messages"] for b in m["content"] if b["type"] == "thinking"]
        assert len(thoughts) == (3 if change in {"none", "suffix_only"} else 1 if change == "cache_argument" else 0)
        before = convert_messages(messages, sources, native_system_only=True)[2]
        def ordinary(native):
            return [{"role": m["role"], "content": [without_cache(b) for b in m["content"]
                    if b["type"] not in {"thinking", "redacted_thinking"}]} for m in native]
        assert ordinary(body["messages"]) == ordinary(before)
        assert engine.store.capture_bytes(prepared.event_payload.final_request_body) == prepared.wire_bytes
        audit = [i for i in prepared.event_payload.injected_content
                 if i.source.source_id == "go-messages-thinking-binding"]
        assert bool(audit) == (change not in {"none", "suffix_only"})


@pytest.mark.parametrize("kind", ["thinking", "redacted_thinking"])
def test_unknown_thinking_is_removed_whole_even_when_message_becomes_empty(tmp_path, kind):
    engine = runtime(tmp_path, [])
    with engine.store:
        source = engine.store.source("unverified-input", {"unknown": True})
        block = ({"type": "thinking", "thinking": "text", "signature": "not-attested"}
                 if kind == "thinking" else {"type": "redacted_thinking", "data": "opaque"})
        body = {"model": "claude-haiku-5-5", "system": [], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "before"}]},
            {"role": "assistant", "content": [block]},
            {"role": "user", "content": [{"type": "text", "text": "after"}]}]}
        sources, audit = project_bound_thinking(engine.store, body, [[source], [source], [source]])
        assert body["messages"] == [{"role": "user", "content": [
            {"type": "text", "text": "before"}, {"type": "text", "text": "after"}]}]
        assert len(sources) == 1 and len(sources[0]) == 2
        assert json.loads(engine.store.get_bytes(audit.blob))["changes"][0]["reason"] == "unverified_response_origin"
