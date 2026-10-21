from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from src.agent_runtime.adapter import HttpChatAdapter, ScriptedAdapter, parse_response, prepare_request
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.harness_contracts import (
    BudgetAmounts, BudgetEventPayload, BudgetLedger, BudgetReservation, BudgetSettlement, CostUnavailable,
    EventLog, InputMaterialRequirement, RemoteModelIdentity, ReturnRequirement,
    RoleDefinition, ToolGrant, UsageMissing, UsageReported, VersionManifest, VersionStamp,
)


def response(*calls, text=None, usage=True, reasoning=False):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [{"id": call[0], "type": "function", "function": {
            "name": call[1], "arguments": json.dumps(call[2])}} for call in calls]
    if reasoning:
        message["reasoning_content"] = "Public protocol test explanation."
        message["thinking_signature"] = "signature-is-not-thought"
    value = {"id": "test-response", "model": "test", "choices": [{
        "index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}]}
    if usage:
        value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                           "completion_tokens_details": {"reasoning_tokens": 2}}
    return value


def versions():
    stamp = VersionStamp(identifier="test-v1")
    return VersionManifest(code_commit=stamp, dependency_lock=stamp, prompt=stamp,
        tool_definitions=stamp, inference_parameters=stamp, model_route=stamp,
        remote_model=RemoteModelIdentity(route_id="offline", remote_alias="test", alias_status="unverified"))


class Tools:
    def __init__(self, directory, *, unknown_write=False):
        self.directory = directory
        self.directory.mkdir(exist_ok=True)
        self.calls = []
        self.unknown_write = unknown_write

    async def list_tools(self):
        return [{"name": name, "description": name, "inputSchema": {"type": "object"}}
                for name in ("save", "view", "error")]

    def repeatability(self, name):
        return "non_idempotent_write" if name == "save" else "read_only"

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "save":
            (self.directory / "saved.json").write_text(json.dumps(arguments))
            if self.unknown_write:
                raise ConnectionError("secret fake credential must never be logged")
        if name == "view":
            stream = io.BytesIO()
            Image.new("RGB", (4, 4), (255, 0, 0)).save(stream, format="PNG")
            return {"isError": False, "content": [{"type": "image", "mimeType": "image/png",
                "data": base64.b64encode(stream.getvalue()).decode()}]}
        return {"isError": name == "error", "content": [{"type": "text",
                "text": "intentional tool error" if name == "error" else "saved"}]}

    def snapshot_state(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.artifacts()}

    def artifacts(self):
        return sorted(self.directory.glob("*.json"))


def role(limits, readonly=False):
    return RoleDefinition(role_id="local_observer" if readonly else "coordinator",
        responsibilities=("test the single-role protocol",),
        tool_whitelist=tuple(ToolGrant(tool_name=n, access="write" if n == "save" else "read")
                            for n in (("view", "error") if readonly else ("save", "view", "error"))),
        input_materials=(InputMaterialRequirement(name="task", media_type="text/plain"),),
        return_requirements=(ReturnRequirement(name="answer", schema_ref="test"),),
        budget=limits.ledger_limit(), read_only=readonly)


def runtime(tmp_path, responses, *, limits=None, tools=None, readonly=False):
    limits = limits or RunLimits(model_calls=4, tool_calls=5, seconds=30.0, tokens=100_000)
    store = EventStore(tmp_path / "run", run_id="run-test", task_id="task", budget_limit=limits.ledger_limit())
    engine = Runtime(store=store, adapter=ScriptedAdapter(responses),
        tools=tools or Tools(tmp_path / "tools"), role=role(limits, readonly),
        model="test", parameters={"max_tokens": 256, "temperature": 0.0},
        versions=versions(), limits=limits)
    return engine


MESSAGES = [{"role": "system", "content": "unchanged system guidance"},
            {"role": "user", "content": "exercise tool protocol"}]


def test_wall_mode_deadline_keeps_downtime_on_resume_and_stops_before_late_tool(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.agent_runtime import loop

    clock, timeouts = [1000.0], []
    monkeypatch.setattr(loop, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))

    class Crash(BaseException):
        pass

    limits = RunLimits(model_calls=80, tool_calls=400, seconds=5400, tokens=None,
                       time_budget_mode="wall", model_reconnect_seconds=180)
    first = runtime(tmp_path, [], limits=limits)

    def crash_before_request(boundary, engine):
        if boundary == "before_request":
            raise Crash()

    first.fault_hook = crash_before_request
    with first.store:
        with pytest.raises(Crash):
            asyncio.run(first.run(MESSAGES))
    clock[0] += 5399  # Restart downtime must not grant a fresh 90 minutes.

    class Adapter:
        async def send(self, prepared, *, timeout):
            timeouts.append(timeout)
            clock[0] += 2
            return response(("late-write", "save", {"value": "must not be written"}))

    resumed = runtime(tmp_path, [], limits=limits, tools=first.tools)
    resumed.adapter = Adapter()
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "time_budget_exhausted"
        assert timeouts == [1.0]
        assert receipt["elapsed_seconds"] == 5401 and receipt["deadline_epoch"] == 6400
        assert receipt["model_calls"] == 1 and receipt["tool_calls"] == 0
        assert resumed.tools.calls == [] and not (tmp_path / "tools/saved.json").exists()
        assert receipt["budget"]["total_limit"]["seconds"] is None
        assert resumed.budget.ledger.charged.seconds == 2
        resumed.store.validate()


def test_single_role_keeps_complete_request_result_images_errors_and_public_thinking(tmp_path):
    engine = runtime(tmp_path, [response(("a", "view", {}), ("b", "error", {}),
        ("c", "save", {"value": 7}), reasoning=True), response(text="Done")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert receipt["model_calls"] == 2 and receipt["tool_calls"] == 3
        log = engine.store.validate()
        assert log.mode == "complete" and receipt["reported_tokens"] == 60
        requests = [e.payload for e in log.events if e.payload.event_type == "adapter_request"]
        for sent, captured in zip(engine.adapter.requests, requests):
            assert sent == engine.store.capture_bytes(captured.final_request_body)
        body = engine.store.resolve(requests[-1].final_request_body)
        assert [m["role"] for m in body["messages"][-4:]] == ["tool", "tool", "tool", "user"]
        assert requests[-1].images[0].original.sha256 == requests[-1].images[0].sent.sha256
        tool_events = [e.payload for e in log.events if e.payload.event_type == "tool_execution"]
        assert [e.outcome for e in tool_events] == ["succeeded", "failed", "succeeded"]
        shown = engine.store.resolve(tool_events[0].shown_result)
        assert shown["image_blocks"][-1]["image_url"]["url"].startswith("data:image/png")
        thoughts = [e.payload.thinking for e in log.events if e.payload.event_type == "model_response"][0]
        assert {x.kind for x in thoughts} == {"public_content", "signature", "reported_token_count"}
        assert receipt["billing_usd"] is None
        assert len(receipt["artifacts"]) == 1
        assert len([e for e in log.events if e.payload.event_type == "tool_presentation"]) == 3


@pytest.mark.parametrize("interrupt", [False, True])
def test_no_time_limit_keeps_long_requests_elapsed_and_completion_on_resume(tmp_path, monkeypatch, interrupt):
    from types import SimpleNamespace
    from src.agent_runtime import loop

    clock, io_timeouts, wait_timeouts = [1000.0], [], []
    monkeypatch.setattr(loop, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    real_wait_for = asyncio.wait_for

    async def observed_wait_for(awaitable, timeout):
        wait_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", observed_wait_for)

    class LongResponseAdapter(ScriptedAdapter):
        async def send(self, prepared, *, timeout):
            io_timeouts.append(timeout)
            # Simulate a response with ongoing I/O lasting beyond the idle cap.
            clock[0] += 350
            await asyncio.sleep(0)
            return await super().send(prepared, timeout=timeout)

    class Crash(BaseException):
        pass

    def completed(engine):
        if (engine.tools.directory / "saved.json").is_file():
            return {"origin": "domain_tool"}

    def fault(name, engine):
        if (interrupt and name == "after_checkpoint" and len(engine.used_ids) == 2
                and not engine.pending_response_id and not engine.completion):
            raise Crash()

    limits = RunLimits(model_calls=3, tool_calls=2, seconds=None, tokens=100_000)
    engine = runtime(tmp_path, [], limits=limits)
    engine.adapter = LongResponseAdapter([response(("v", "view", {})), response(("w", "save", {"value": 8}))])
    engine.domain_completion, engine.fault_hook = completed, fault
    with engine.store:
        if interrupt:
            with pytest.raises(Crash):
                asyncio.run(engine.run(MESSAGES))
        else:
            result = asyncio.run(engine.run(MESSAGES))
    if interrupt:
        clock[0] += 400  # Resume keeps downtime in elapsed; it buys no new finite budget.
        engine = runtime(tmp_path, [], limits=limits, tools=engine.tools)
        engine.domain_completion = completed
        with engine.store:
            result = asyncio.run(engine.run(MESSAGES, resume=True))
        assert engine.adapter.requests == []
    assert result["status"] == "completed" and result["completion"]["origin"] == "domain_tool"
    assert result["answer"] is None and result["model_calls"] == result["tool_calls"] == 2
    assert result["elapsed_seconds"] == (1100 if interrupt else 700)
    assert result["deadline_epoch"] is None and result["limits"]["seconds"] is None
    assert io_timeouts == [300.0, 300.0] and set(wait_timeouts) == {None}
    assert all(row["amounts"]["seconds"] is None for row in result["budget"]["reservations"])
    assert [Decimal(row["actual"]["seconds"]) for row in result["budget"]["settlements"]] == [350, 350]
    json.dumps(result, allow_nan=False)
    with engine.store:
        engine.store.validate()
    with pytest.raises(ValueError, match="limits/identity cannot change on resume"):
        runtime(tmp_path, [], tools=engine.tools, limits=limits.model_copy(update={"seconds": 1200.0}))


def test_no_time_limit_idle_network_failure_retries_without_time_budget_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.agent_runtime import loop

    clock, timeouts = [1000.0], []
    monkeypatch.setattr(loop, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))

    class IdleThenSuccess(ScriptedAdapter):
        async def send(self, prepared, *, timeout):
            timeouts.append(timeout)
            clock[0] += 301
            if len(timeouts) == 1:
                raise httpx.ReadTimeout("synthetic idle timeout")
            return await super().send(prepared, timeout=timeout)

    limits = RunLimits(model_calls=3, tool_calls=0, seconds=None, tokens=100_000,
                       retry_backoff_seconds=0)
    engine = runtime(tmp_path, [], limits=limits)
    engine.adapter = IdleThenSuccess([response(text="Done after transport retry")])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        failures = [event.payload for event in engine.store.events
                    if event.payload.event_type == "run_lifecycle" and event.payload.action == "failure"]
        assert len(failures) == 1 and failures[0].failure_stage == "model_request"
        assert failures[0].model_failure.category == "timeout"
        engine.store.validate()
    assert result["status"] == "completed" and result["retries"] == 1
    assert timeouts == [300.0, 300.0] and result["elapsed_seconds"] == 602
    assert [Decimal(row["actual"]["seconds"]) for row in result["budget"]["settlements"]] == [301, 301]


@pytest.mark.parametrize("dimension,limit,status", [
    ("model_calls", 1, "model_budget_exhausted"),
    ("tool_calls", 0, "tool_budget_exhausted"),
    ("tokens", 1, "token_budget_exhausted"),
])
def test_no_time_limit_preserves_other_budget_stops(tmp_path, dimension, limit, status):
    limits = RunLimits(model_calls=2, tool_calls=1, seconds=None, tokens=100_000).model_copy(update={dimension: limit})
    engine = runtime(tmp_path, [response(("v", "view", {}))], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        engine.store.validate()
    assert result["status"] == status


@pytest.mark.parametrize("root_seconds,child_seconds", [(None, 5.0), (5.0, None)])
def test_optional_time_limit_preserves_finite_root_or_child_allowance(tmp_path, root_seconds, child_seconds):
    class Capture(ScriptedAdapter):
        async def send(self, prepared, *, timeout):
            assert 0 < timeout <= 5
            return await super().send(prepared, timeout=timeout)

    async def scenario():
        root_limits = RunLimits(model_calls=2, tool_calls=0, seconds=root_seconds, tokens=100_000)
        child_limits = RunLimits(model_calls=1, tool_calls=0, seconds=child_seconds, tokens=100_000)
        with EventStore(tmp_path / "run", run_id="optional-time", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as store:
            engine = Runtime(store=store.for_task("child", "parent"), adapter=Capture([response(text="Done")]),
                tools=Tools(tmp_path / "tools"), role=role(child_limits), model="test",
                parameters={"max_tokens": 256}, versions=versions(), limits=child_limits)
            result = await engine.run(MESSAGES)
            assert result["status"] == "completed"
            assert 0 < Decimal(result["task_budget"]["reservations"][0]["amounts"]["seconds"]) <= 5
            store.validate()
    asyncio.run(scenario())


def test_no_time_limit_tool_timeout_is_not_a_nonexistent_budget_deadline(tmp_path):
    class TimedOutTools(Tools):
        async def call_tool(self, name, arguments):
            raise TimeoutError("tool-owned timeout")

    limits = RunLimits(model_calls=2, tool_calls=1, seconds=None, tokens=100_000)
    engine = runtime(tmp_path, [response(("v", "view", {}))], limits=limits,
                     tools=TimedOutTools(tmp_path / "tools"))
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        engine.store.validate()
    assert result["status"] == "operation_timeout"
    assert result["deadline_epoch"] is None


@pytest.mark.parametrize("lim,responses,status", [
    (RunLimits(model_calls=1, tool_calls=2, seconds=10.0, tokens=100_000),
     [response(("v", "view", {}))], "model_budget_exhausted"),
    (RunLimits(model_calls=2, tool_calls=0, seconds=10.0, tokens=100_000),
     [response(("w", "save", {}))], "tool_budget_exhausted"),
    (RunLimits(model_calls=2, tool_calls=2, seconds=10.0, tokens=1),
     [], "token_budget_exhausted"),
    (RunLimits(model_calls=2, tool_calls=2, seconds=0.000001, tokens=100_000),
     [], "time_budget_exhausted"),
])
def test_all_budget_dimensions_stop_without_an_extra_operation(tmp_path, lim, responses, status):
    engine = runtime(tmp_path, responses, limits=lim)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == status
        assert receipt["model_calls"] <= lim.model_calls
        assert receipt["tool_calls"] <= lim.tool_calls
        assert engine.store.events[-2].payload.reason == status
        assert engine.store.validate().mode == "complete"


def test_unknown_write_inspects_persisted_state_and_does_not_repeat(tmp_path):
    tools = Tools(tmp_path / "tools", unknown_write=True)
    engine = runtime(tmp_path, [response(("a", "save", {"value": 1}))], tools=tools)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "unknown_write_outcome"
        assert len(tools.calls) == 1 and len(receipt["artifacts"]) == 1
        checks = [e.payload for e in engine.store.events if e.payload.event_type == "state_inspection"]
        assert checks[0].conclusion == "inconclusive"
        observed = json.loads(engine.store.get_bytes(checks[0].persisted_state))
        assert observed["observed_after"] == tools.snapshot_state()
        assert "secret fake credential" not in engine.store.path.read_text()
        execution = next(e.payload for e in engine.store.events
                         if e.payload.event_type == "tool_execution")
        assert execution.failure.exception_type == "builtins.ConnectionError"
        assert execution.failure.stage == "tool:save"
        assert execution.failure.diagnostic == "exception message omitted by safety policy"


def test_unknown_write_keeps_empty_stop_iteration_identity(tmp_path):
    tools = Tools(tmp_path / "tools")

    def stop_iteration(name, arguments):
        tools.calls.append((name, arguments))
        raise StopIteration

    tools.call_tool = stop_iteration
    engine = runtime(tmp_path, [response(("a", "save", {"value": 1}))], tools=tools)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        execution = next(e.payload for e in engine.store.events
                         if e.payload.event_type == "tool_execution")
        assert receipt["status"] == "unknown_write_outcome"
        assert execution.failure.exception_type == "builtins.StopIteration"
        assert execution.failure.stage == "tool:save"
        assert execution.failure.diagnostic == "exception carried no message"
        assert len(tools.calls) == 1


def test_unknown_write_diagnostic_survives_invalid_unicode_without_leaking(tmp_path):
    class InvalidUnicodeError(Exception):
        def __str__(self):
            return "\udcff https://user:password@example.invalid/?access_token=do-not-save"

    tools = Tools(tmp_path / "tools")

    def fail(name, arguments):
        tools.calls.append((name, arguments))
        raise InvalidUnicodeError

    tools.call_tool = fail
    engine = runtime(tmp_path, [response(("a", "save", {"value": 1}))], tools=tools)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        execution = next(e.payload for e in engine.store.events
                         if e.payload.event_type == "tool_execution")
        assert receipt["status"] == "unknown_write_outcome"
        assert execution.failure.exception_type.endswith(".InvalidUnicodeError")
        assert execution.failure.diagnostic == "exception message omitted by safety policy"
        journal = engine.store.path.read_text(encoding="utf-8")
        assert "do-not-save" not in journal and "user:password" not in journal


def test_unknown_write_diagnostic_survives_broken_exception_string(tmp_path):
    class BrokenStringError(Exception):
        def __str__(self):
            raise UnicodeError("cannot render exception")

    tools = Tools(tmp_path / "tools")

    def fail(name, arguments):
        tools.calls.append((name, arguments))
        raise BrokenStringError

    tools.call_tool = fail
    engine = runtime(tmp_path, [response(("a", "save", {"value": 1}))], tools=tools)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        execution = next(e.payload for e in engine.store.events
                         if e.payload.event_type == "tool_execution")
        assert receipt["status"] == "unknown_write_outcome"
        assert execution.failure.exception_type.endswith(".BrokenStringError")
        assert execution.failure.diagnostic == "exception message unavailable because __str__ failed"


@pytest.mark.parametrize("child_seconds", [30.0, None])
def test_inflight_root_time_hold_waits_and_gives_older_sibling_first_turn(tmp_path, child_seconds):
    class OrderedAdapter:
        def __init__(self, label, replies, order, *, started=None, release=None):
            self.label, self.replies, self.order = label, list(replies), order
            self.started, self.release = started, release
            self.requests = []

        async def send(self, prepared, *, timeout):
            self.requests.append(prepared)
            self.order.append(self.label)
            if self.started is not None and len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            return self.replies.pop(0)

    async def scenario():
        root_limits = RunLimits(model_calls=4, tool_calls=0, seconds=5, tokens=100_000)
        child_limits = RunLimits(model_calls=3, tool_calls=0, seconds=child_seconds, tokens=100_000)
        started, release, order = asyncio.Event(), asyncio.Event(), []
        truncated = response(text="discarded partial answer")
        truncated["choices"][0]["finish_reason"] = "length"
        with EventStore(tmp_path / "run", run_id="shared-time", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as root:
            first = Runtime(store=root.for_task("first", "parent"),
                adapter=OrderedAdapter("first", [truncated, response(text="first done")], order,
                                       started=started, release=release),
                tools=Tools(tmp_path / "tools-first"), role=role(child_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=child_limits, request_timeout_seconds=5)
            second_adapter = OrderedAdapter("second", [response(text="second done")], order)
            second = Runtime(store=root.for_task("second", "parent"), adapter=second_adapter,
                tools=Tools(tmp_path / "tools-second"), role=role(child_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=child_limits, request_timeout_seconds=5)
            first_task = asyncio.create_task(first.run(MESSAGES))
            await started.wait()
            second_task = asyncio.create_task(second.run(MESSAGES))
            await asyncio.sleep(0)
            assert order == ["first"]
            release.set()
            first_receipt, second_receipt = await asyncio.gather(first_task, second_task)
            assert first_receipt["status"] == second_receipt["status"] == "completed"
            assert order == ["first", "second", "first"]
            assert len(second_adapter.requests) == 1
            waits = [e.payload for e in root.all_events
                     if e.task_id == "second" and e.payload.event_type == "budget_wait"]
            assert [event.phase for event in waits] == ["begin", "end"]
            assert waits[0].wait_id == waits[1].wait_id
            assert waits[0].root_available.seconds == 0
            assert waits[0].active_hold_ids == ("first:request-1",)
            assert waits[1].outcome == "capacity_changed"
            assert waits[1].elapsed_seconds is not None
            assert (waits[0].wall_remaining_seconds is None) is (child_seconds is None)
            assert (waits[1].wall_remaining_seconds is None) is (child_seconds is None)
            root.validate()

    asyncio.run(scenario())


def test_settled_root_time_exhaustion_stops_without_wait_or_send(tmp_path):
    async def scenario():
        root_limits = RunLimits(model_calls=3, tool_calls=0, seconds=5, tokens=100_000)
        child_limits = RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=100_000)
        with EventStore(tmp_path / "run", run_id="settled-time", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as root:
            reservation = BudgetReservation(reservation_id="prior:request-1",
                purpose="primary_task", task_id="parent",
                amounts=BudgetAmounts(tokens=30, calls=1, seconds=Decimal("5")))
            root.append(BudgetEventPayload(action="reserve", reservation=reservation))
            root.append(BudgetEventPayload(action="settle", settlement=BudgetSettlement(
                reservation_id=reservation.reservation_id,
                actual=BudgetAmounts(tokens=30, calls=1, seconds=Decimal("5")),
                usage=UsageReported(raw_usage={"total_tokens": 30}),
                cost=CostUnavailable(reason="no configured money budget"))))
            adapter = ScriptedAdapter([response(text="must not send")])
            engine = Runtime(store=root.for_task("child", "parent"), adapter=adapter,
                tools=Tools(tmp_path / "tools-child"), role=role(child_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=child_limits, request_timeout_seconds=5)
            receipt = await engine.run(MESSAGES)
            assert receipt["status"] == "root_time_budget_exhausted"
            assert adapter.requests == []

    asyncio.run(scenario())


def test_small_positive_root_time_remainder_is_used_as_request_timeout(tmp_path):
    class TimeoutRecordingAdapter:
        def __init__(self):
            self.timeouts = []

        async def send(self, prepared, *, timeout):
            self.timeouts.append(timeout)
            return response(text="done")

    async def scenario():
        root_limits = RunLimits(model_calls=2, tool_calls=0, seconds=5, tokens=100_000)
        child_limits = RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=100_000)
        with EventStore(tmp_path / "run", run_id="small-remainder", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as root:
            reservation = BudgetReservation(reservation_id="prior:request-1",
                purpose="primary_task", task_id="parent",
                amounts=BudgetAmounts(tokens=30, calls=1, seconds=Decimal("4.999")))
            root.append(BudgetEventPayload(action="reserve", reservation=reservation))
            root.append(BudgetEventPayload(action="settle", settlement=BudgetSettlement(
                reservation_id=reservation.reservation_id,
                actual=BudgetAmounts(tokens=30, calls=1, seconds=Decimal("4.999")),
                usage=UsageReported(raw_usage={"total_tokens": 30}),
                cost=CostUnavailable(reason="no configured money budget"))))
            adapter = TimeoutRecordingAdapter()
            engine = Runtime(store=root.for_task("child", "parent"), adapter=adapter,
                tools=Tools(tmp_path / "tools-child"), role=role(child_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=child_limits, request_timeout_seconds=5)
            receipt = await engine.run(MESSAGES)
            # Admission must use the positive remainder. Durable response
            # capture can exceed 1 ms on Windows, in which case the real full
            # elapsed charge correctly stops the run after this one request.
            assert receipt["status"] in {"completed", "root_time_budget_exhausted"}
            assert receipt["model_calls"] == 1
            assert len(adapter.timeouts) == 1
            assert 0 < adapter.timeouts[0] <= 0.001

    asyncio.run(scenario())


def test_wall_deadline_ends_wait_for_live_root_time_hold_without_sending(tmp_path):
    class HoldingAdapter:
        def __init__(self, started, release):
            self.started, self.release, self.requests = started, release, []

        async def send(self, prepared, *, timeout):
            self.requests.append(prepared)
            self.started.set()
            await self.release.wait()
            return response(text="holder done")

    async def scenario():
        root_limits = RunLimits(model_calls=2, tool_calls=0, seconds=1, tokens=100_000)
        holder_limits = RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=100_000)
        waiter_limits = RunLimits(model_calls=1, tool_calls=0, seconds=0.1, tokens=100_000)
        started, release = asyncio.Event(), asyncio.Event()
        with EventStore(tmp_path / "run", run_id="wall-deadline", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as root:
            holder = Runtime(store=root.for_task("holder", "parent"),
                adapter=HoldingAdapter(started, release), tools=Tools(tmp_path / "tools-holder"),
                role=role(holder_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=holder_limits, request_timeout_seconds=1)
            waiter_adapter = ScriptedAdapter([response(text="must not send")])
            waiter = Runtime(store=root.for_task("waiter", "parent"), adapter=waiter_adapter,
                tools=Tools(tmp_path / "tools-waiter"), role=role(waiter_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=waiter_limits, request_timeout_seconds=1)
            holder_task = asyncio.create_task(holder.run(MESSAGES))
            await started.wait()
            try:
                waiter_receipt = await waiter.run(MESSAGES)
                assert waiter_receipt["status"] == "child_time_budget_exhausted"
                assert waiter_adapter.requests == []
                waits = [e.payload for e in root.all_events
                         if e.task_id == "waiter" and e.payload.event_type == "budget_wait"]
                assert [event.phase for event in waits] == ["begin", "end"]
                assert waits[1].outcome == "wall_deadline_exhausted"
            finally:
                release.set()
                holder_receipt = await holder_task
            assert holder_receipt["status"] == "completed"

    asyncio.run(scenario())


def test_retry_waits_when_sibling_fills_root_time_during_backoff(tmp_path):
    class RetryAdapter:
        def __init__(self, failed):
            self.failed, self.requests = failed, []

        async def send(self, prepared, *, timeout):
            self.requests.append(prepared)
            if len(self.requests) == 1:
                self.failed.set()
                raise TimeoutError("temporary offline timeout")
            return response(text="retry done")

    class HoldingAdapter:
        def __init__(self, started, release):
            self.started, self.release, self.requests = started, release, []

        async def send(self, prepared, *, timeout):
            self.requests.append(prepared)
            self.started.set()
            await self.release.wait()
            return response(text="holder done")

    async def scenario():
        root_limits = RunLimits(model_calls=3, tool_calls=0, seconds=5, tokens=100_000)
        retry_limits = RunLimits(model_calls=2, tool_calls=0, seconds=30, tokens=100_000,
                                 max_model_retries=1, retry_backoff_seconds=0.03)
        holder_limits = RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=100_000)
        failed, holder_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        with EventStore(tmp_path / "run", run_id="retry-wait", task_id="parent",
                        budget_limit=root_limits.ledger_limit()) as root:
            retry_adapter = RetryAdapter(failed)
            retrying = Runtime(store=root.for_task("retrying", "parent"), adapter=retry_adapter,
                tools=Tools(tmp_path / "tools-retrying"), role=role(retry_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=retry_limits, request_timeout_seconds=5)
            holder = Runtime(store=root.for_task("holder", "parent"),
                adapter=HoldingAdapter(holder_started, release),
                tools=Tools(tmp_path / "tools-holder"), role=role(holder_limits), model="test",
                parameters={"max_tokens": 256, "temperature": 0.0}, versions=versions(),
                limits=holder_limits, request_timeout_seconds=5)
            retry_task = asyncio.create_task(retrying.run(MESSAGES))
            await failed.wait()
            holder_task = asyncio.create_task(holder.run(MESSAGES))
            await holder_started.wait()
            await asyncio.sleep(0.05)
            assert len(retry_adapter.requests) == 1 and not retry_task.done()
            release.set()
            retry_receipt, holder_receipt = await asyncio.gather(retry_task, holder_task)
            assert retry_receipt["status"] == holder_receipt["status"] == "completed"
            assert len(retry_adapter.requests) == 2
            waits = [e.payload for e in root.all_events
                     if e.task_id == "retrying" and e.payload.event_type == "budget_wait"]
            assert [event.phase for event in waits] == ["begin", "end"]
            assert waits[0].active_hold_ids == ("holder:request-1",)
            assert waits[1].outcome == "capacity_changed"

    asyncio.run(scenario())


def test_readonly_role_rejects_entire_batch_before_write(tmp_path):
    engine = runtime(tmp_path, [response(("r", "view", {}), ("w", "save", {}))], readonly=True)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "tool_authorization_failed"
        assert engine.tools.calls == []


def test_missing_usage_is_unknown_and_stops_a_token_limited_run(tmp_path):
    engine = runtime(tmp_path, [response(text="Done", usage=False)])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "token_usage_unavailable"
        settlements = [e.payload.settlement for e in engine.store.events
            if e.payload.event_type == "budget" and e.payload.action == "settle"]
        assert settlements[0].actual.tokens is None
        assert settlements[0].cost.kind == "unavailable"
        assert engine.store.events[-1].payload.usage.kind == "missing"


def test_prepared_tool_image_is_not_marked_delivered_when_call_budget_stops(tmp_path):
    engine = runtime(tmp_path, [response(("v", "view", {}))],
        limits=RunLimits(model_calls=1, tool_calls=2, seconds=30.0, tokens=100_000))
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "model_budget_exhausted"
        tool = next(e.payload for e in engine.store.events if e.payload.event_type == "tool_execution")
        assert tool.presentation_status == "prepared" and tool.shown_result.kind == "image_references"
        assert not [e for e in engine.store.events if e.payload.event_type == "tool_presentation"]


@pytest.mark.parametrize("malformed", ["call_dict", "stop_with_call", "bad_message", "truncated"])
def test_malformed_response_always_keeps_raw_event_and_never_executes_tools(tmp_path, malformed):
    raw = response(("w", "save", {"value": 3}))
    if malformed == "call_dict":
        raw["choices"][0]["message"]["tool_calls"] = {"bad": "shape"}
    elif malformed == "stop_with_call":
        raw["choices"][0]["finish_reason"] = "stop"
    elif malformed == "bad_message":
        raw["choices"][0]["message"] = 12
    else:
        raw["choices"][0]["finish_reason"] = "length"
    engine = runtime(tmp_path, [raw])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] != "completed" and engine.tools.calls == []
        record = next(e.payload for e in engine.store.events if e.payload.event_type == "model_response")
        assert engine.store.resolve(record.raw_response) == raw


def test_resume_completed_run_does_not_append_duplicate_stop_or_usage(tmp_path):
    engine = runtime(tmp_path, [response(text="Done")])
    with engine.store:
        asyncio.run(engine.run(MESSAGES))
        before = engine.store.path.read_bytes()
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["resume_status"] == "already_completed"
        assert resumed.store.path.read_bytes() == before


@pytest.mark.parametrize("crash_at", [None, "after_execution", "accepted_tool", "finished_batch", "completion"])
def test_domain_completion_stops_without_final_model_request_and_resumes_once(tmp_path, crash_at):
    class Crash(BaseException):
        pass

    def completed(engine):
        path = engine.tools.directory / "saved.json"
        if path.is_file() and json.loads(path.read_bytes()) == {"value": 8}:
            return {"origin": "domain_tool", "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return None

    def interrupt(name, engine):
        if ((crash_at == "after_execution" and name == "after_execution")
                or (name == "after_checkpoint" and engine.used_ids and (
                    (crash_at == "accepted_tool" and engine.pending_response_id)
                    or (crash_at == "finished_batch" and not engine.pending_response_id and not engine.completion)
                    or (crash_at == "completion" and engine.completion)))):
            raise Crash()

    engine = runtime(tmp_path, [response(("w", "save", {"value": 8}))])
    engine.domain_completion, engine.fault_hook = completed, interrupt
    with engine.store:
        if crash_at:
            with pytest.raises(Crash):
                asyncio.run(engine.run(MESSAGES))
        else:
            result = asyncio.run(engine.run(MESSAGES))
    if crash_at:
        resumed = runtime(tmp_path, [], tools=engine.tools)
        resumed.domain_completion = completed
        with resumed.store:
            result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert resumed.adapter.requests == []
    assert result["status"] == "completed" and result["answer"] is None
    assert result["completion"]["origin"] == "domain_tool"
    assert result["model_calls"] == result["tool_calls"] == 1
    assert len(engine.tools.calls) == 1
    resumed = runtime(tmp_path, [], tools=engine.tools)
    resumed.domain_completion = completed
    with resumed.store:
        before = resumed.store.path.read_bytes()
        assert asyncio.run(resumed.run(MESSAGES, resume=True))["resume_status"] == "already_completed"
        assert resumed.store.path.read_bytes() == before
    (engine.tools.directory / "saved.json").write_text('{"value":9}')
    stale = runtime(tmp_path, [], tools=engine.tools)
    stale.domain_completion = completed
    with stale.store:
        assert asyncio.run(stale.run(MESSAGES, resume=True))["status"] == "resume_state_changed"
        assert stale.adapter.requests == []


def test_domain_completion_waits_for_whole_tool_batch(tmp_path):
    observed = []

    def completed(engine):
        if engine.tools.calls:
            observed.append(list(engine.tools.calls))
            if json.loads((engine.tools.directory / "saved.json").read_bytes()) == {"value": 8}:
                return {"origin": "domain_tool"}
        return None

    limits = RunLimits(model_calls=2, tool_calls=2, seconds=30, tokens=100_000)
    engine = runtime(tmp_path, [response(("a", "save", {"value": 8}), ("b", "save", {"value": 9})),
                                response(text="Later edit is still pending review.")], limits=limits)
    engine.domain_completion = completed
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
    assert result["model_calls"] == 2 and "completion" not in result
    assert result["status"] == "model_budget_exhausted" and result["answer"] is None
    assert observed and all(len(batch) == 2 for batch in observed)


@pytest.mark.parametrize("model_calls,expected", [(1, "model_budget_exhausted"),
    (2, "model_budget_exhausted"), (8, "domain_completion_unmet")])
def test_domain_text_continuation_is_bounded_by_progress_and_existing_budget(tmp_path, model_calls, expected):
    limits = RunLimits(model_calls=model_calls, tool_calls=5, seconds=30, tokens=100_000)
    engine = runtime(tmp_path, [response(text="I have not completed the requested work.")] * 4, limits=limits)
    engine.domain_completion = lambda current: None
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == expected and result["answer"] is None
        assert "completion" not in result
        assert result["model_calls"] == result["consecutive_incomplete_responses"] == min(model_calls, 3)
        assert result["reported_tokens"] == result["model_calls"] * 30
        assert result["tool_calls"] == 0
        assert len(result["task_budget"]["settlements"]) == result["model_calls"]
        assert len(engine.adapter.requests) == result["model_calls"]
        engine.store.validate()


@pytest.mark.parametrize("context", [False, True])
@pytest.mark.parametrize("boundary", ["after_response", "after_checkpoint"])
def test_domain_text_continuation_resumes_once_and_can_complete(tmp_path, context, boundary):
    from src.agent_runtime.context import ContextPolicy

    class Crash(BaseException):
        pass

    def completed(current):
        if (current.tools.directory / "saved.json").is_file():
            return {"origin": "domain_tool", "artifact": "saved.json"}

    def interrupt(name, current):
        if name == boundary and current.counts["model_calls"] == 1:
            raise Crash()

    engine = runtime(tmp_path, [response(text="I still need to do the work.")])
    engine.domain_completion, engine.fault_hook = completed, interrupt
    if context:
        engine.context_policy = ContextPolicy(active_window_messages=8)
    with engine.store, pytest.raises(Crash):
        asyncio.run(engine.run(MESSAGES))
    resumed = runtime(tmp_path, [response(text="Still working."),
        response(("save-once", "save", {"value": 8}))], tools=engine.tools)
    resumed.domain_completion, resumed.context_policy = completed, engine.context_policy
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed" and result["answer"] is None
        assert result["model_calls"] == len(result["task_budget"]["settlements"]) == 3
        assert result["reported_tokens"] == 90 and result["tool_calls"] == 1
        assert result["consecutive_incomplete_responses"] == 0
        assert engine.tools.calls == [("save", {"value": 8})]
        first_request = json.loads(resumed.adapter.requests[0])
        assert first_request["messages"].count(resumed._domain_continuation_prompt()) == 1
        resumed.store.validate()


def test_domain_tool_turn_resets_text_continuation_limit(tmp_path):
    limits = RunLimits(model_calls=8, tool_calls=2, seconds=30, tokens=100_000)
    engine = runtime(tmp_path, [response(text="Pending.")] * 2 + [response(("view", "view", {}))]
        + [response(text="Pending.")] * 2 + [response(("save", "save", {"value": 8}))], limits=limits)
    engine.domain_completion = lambda current: ({"origin": "domain_tool"}
        if (current.tools.directory / "saved.json").is_file() else None)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 6
        assert result["tool_calls"] == 2 and result["consecutive_incomplete_responses"] == 0


def test_completed_domain_resume_requires_its_completion_record(tmp_path):
    def completed(current):
        if (current.tools.directory / "saved.json").is_file():
            return {"origin": "domain_tool", "artifact": "saved.json"}

    engine = runtime(tmp_path, [response(("saved", "save", {"value": 8}))])
    engine.domain_completion = completed
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed"
        result.pop("completion")
        engine.store.write_json("receipt.json", result)
    resumed = runtime(tmp_path, [], tools=engine.tools)
    resumed.domain_completion = completed
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "resume_state_changed" and resumed.adapter.requests == []


def test_invalidated_completion_at_finalization_is_not_success(tmp_path):
    def completion(engine):
        if engine.tools.calls:
            return {"origin": "domain_tool", "artifact": "saved.json"}
        return None

    engine = runtime(tmp_path, [response(("w", "save", {"value": 8}))])
    engine.domain_completion = completion
    engine.finalize_run = lambda engine, reason: {"completion_valid": False,
        "status": "handoff_changed", "delivery": None}
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
    assert result["status"] == "completion_state_changed" and result["answer"] is None
    assert result["completion"]["origin"] == "domain_tool"  # Preserve the prior attestation.
    assert result["model_calls"] == result["tool_calls"] == 1


def test_resume_rejects_changed_model_profile(tmp_path, monkeypatch):
    engine = runtime(tmp_path, [response(text="Done")])
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"

    from src.agent_runtime import loop as runtime_loop
    original = runtime_loop.get_model_profile("test")
    monkeypatch.setattr(runtime_loop, "get_model_profile", lambda *args, **kwargs:
                        replace(original, text_fixed_margin=original.text_fixed_margin + 1))
    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "resume_configuration_changed"


@pytest.mark.parametrize(
    ("model", "configured_limit", "output_tokens", "expected"),
    [
        ("test", 200, 256, "configured_context_limit_exhausted"),
        ("Qwen3.8-27B", None, 262_144, "model_profile_context_limit_exhausted"),
    ],
)
def test_context_limits_stop_before_adapter_send(
        tmp_path, model, configured_limit, output_tokens, expected):
    limits = RunLimits(model_calls=1, tool_calls=0, seconds=30.0,
                       tokens=1_000_000, context_tokens=configured_limit)
    engine = runtime(tmp_path, [], limits=limits)
    engine.model = model
    engine.parameters["max_tokens"] = output_tokens
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == expected
        assert engine.adapter.requests == []
        assert not [event for event in engine.store.events
                    if event.payload.event_type == "adapter_request"]


def test_actual_token_overrun_preserves_full_settlement_and_exhausts_root(tmp_path):
    raw = response(text="Done")
    raw["usage"]["total_tokens"] = 1_000_000
    engine = runtime(tmp_path, [raw])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "token_budget_exhausted"
        assert receipt["reported_tokens"] == 1_000_000
        settlements = [e.payload.settlement for e in engine.store.events
                       if e.payload.event_type == "budget" and e.payload.action == "settle"]
        assert len(settlements) == 1
        assert settlements[0].actual.tokens == 1_000_000
        assert settlements[0].token_overrun > 0
        assert engine.budget.ledger.charged.tokens == 1_000_000
        assert receipt["root_budget_available"]["tokens"] == 0


def test_crash_at_safe_checkpoint_resumes_without_repeating_completed_write(tmp_path):
    class Crash(BaseException):
        pass

    engine = runtime(tmp_path, [response(("w", "save", {"value": 8}))])
    save = engine._checkpoint

    def crash_after_saved_turn():
        save()
        if engine.counts["tool_calls"]:
            raise Crash()

    engine._checkpoint = crash_after_saved_turn
    with engine.store:
        with pytest.raises(Crash):
            asyncio.run(engine.run(MESSAGES))
    resumed = runtime(tmp_path, [response(text="Continued")], tools=engine.tools)
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "completed"
        assert receipt["model_calls"] == 2 and len(engine.tools.calls) == 1
        assert any(e.payload.event_type == "run_lifecycle" and e.payload.action == "resume"
                   for e in resumed.store.events)


def test_answer_repair_response_replays_without_duplicate_request_or_event(tmp_path):
    class Crash(BaseException):
        pass

    invalid = '{"answer": 7})'
    corrected = '{"answer": 7}'
    engine = runtime(tmp_path, [response(text=invalid), response(text=corrected)])
    engine.answer_validator = json.loads
    engine.max_answer_repairs = 1

    def crash_after_repair_response(name, running):
        if name == "after_response" and running.counts["model_calls"] == 2:
            raise Crash()

    engine.fault_hook = crash_after_repair_response
    with engine.store:
        with pytest.raises(Crash):
            asyncio.run(engine.run(MESSAGES))

    resumed = runtime(tmp_path, [])
    resumed.answer_validator = json.loads
    resumed.max_answer_repairs = 1
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "completed"
        assert receipt["answer"] == corrected
        assert resumed.adapter.requests == []
        repairs = [event.payload for event in resumed.store.events
                   if event.payload.event_type == "answer_repair"]
        assert [repair.phase for repair in repairs] == ["request", "result"]
        assert repairs[-1].accepted is True
        assert len([event for event in resumed.store.events
                    if event.payload.event_type == "adapter_request"]) == 2


def test_interrupted_answer_repair_request_is_never_retried_on_resume(tmp_path):
    class Crash(BaseException):
        pass

    limits = RunLimits(model_calls=3, tool_calls=0, seconds=30.0,
                       tokens=100_000, max_model_retries=1)
    engine = runtime(tmp_path,
        [response(text='{"answer": 7}}'), response(text='{"answer": 7}')],
        limits=limits)
    engine.answer_validator = json.loads
    engine.max_answer_repairs = 1

    def crash_after_durable_repair_request(name, running):
        if name == "after_request" and running.counts["model_calls"] == 2:
            raise Crash()

    engine.fault_hook = crash_after_durable_repair_request
    with engine.store:
        with pytest.raises(Crash):
            asyncio.run(engine.run(MESSAGES))
        assert len(engine.adapter.requests) == 1

    resumed = runtime(tmp_path, [response(text='{"answer": 7}')],
                      limits=limits)
    resumed.answer_validator = json.loads
    resumed.max_answer_repairs = 1
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "resume_request_outcome_unknown"
        assert resumed.adapter.requests == []
        assert len([event for event in resumed.store.events
                    if event.payload.event_type == "adapter_request"]) == 2
        repairs = [event.payload for event in resumed.store.events
                   if event.payload.event_type == "answer_repair"]
        assert [repair.phase for repair in repairs] == ["request"]


def test_resume_rejects_changed_saved_state(tmp_path):
    limits = RunLimits(model_calls=1, tool_calls=2, seconds=30.0, tokens=100_000)
    engine = runtime(tmp_path, [response(("w", "save", {"value": 7}))], limits=limits)
    with engine.store:
        asyncio.run(engine.run(MESSAGES))
    (tmp_path / "tools/saved.json").write_text('{"value":999}')
    resumed = runtime(tmp_path, [], limits=limits, tools=engine.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "resume_state_changed"
        assert resumed.adapter.requests == []


def test_exact_http_wire_body_matches_recorded_bytes_and_never_logs_key(tmp_path):
    engine = runtime(tmp_path, [])
    received = []

    def handle(request):
        received.append(request.content)
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(200, json=response(text="OK", reasoning=True))

    async def exercise():
        adapter = HttpChatAdapter(base_url="https://example.invalid/v1", api_key="test-secret",
                                  transport=httpx.MockTransport(handle))
        engine.adapter = adapter
        try:
            return await engine.run(MESSAGES)
        finally:
            await adapter.close()

    with engine.store:
        result = asyncio.run(exercise())
        assert result["status"] == "completed"
        request = next(e.payload for e in engine.store.events if e.payload.event_type == "adapter_request")
        assert engine.store.capture_bytes(request.final_request_body) == received[0]
    # Windows exclusive locks deny reads through a second handle too. Scan
    # every file, including writer.lock, after releasing the writer handle.
    assert all(b"test-secret" not in p.read_bytes() for p in engine.store.directory.rglob("*") if p.is_file())


def test_store_detects_corruption_and_torn_tail(tmp_path):
    engine = runtime(tmp_path, [response(text="OK")])
    with engine.store:
        asyncio.run(engine.run(MESSAGES))
        ref = engine.store.put_bytes(b"abc")
        (engine.store.directory / ref.uri).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="hash mismatch"):
            engine.store.get_bytes(ref)
    with (tmp_path / "run/events.jsonl").open("ab") as output:
        output.write(b'{"partial":')
    with pytest.raises(ValueError, match="incomplete"):
        runtime(tmp_path, [])


@pytest.mark.parametrize("bad", [True, "10", 1.2])
def test_core_budget_does_not_coerce_noninteger_tokens(bad):
    with pytest.raises(ValidationError):
        BudgetAmounts(tokens=bad)


def test_unknown_cost_is_valid_without_money_budget_but_cannot_fake_money_limit():
    from decimal import Decimal

    reservation = BudgetReservation(reservation_id="a", purpose="primary_task", task_id="t",
                                    amounts=BudgetAmounts(tokens=100, calls=1))
    settlement = BudgetSettlement(reservation_id="a", actual=BudgetAmounts(calls=1),
        usage=UsageMissing(reason="no receipt"), cost=CostUnavailable(reason="no bill"))
    BudgetLedger(total_limit=BudgetAmounts(tokens=100, calls=1),
                 reservations=(reservation,), settlements=(settlement,))
    with pytest.raises(ValidationError, match="unknown cost cannot settle"):
        BudgetLedger(total_limit=BudgetAmounts(tokens=100, calls=1, money_usd=Decimal("1")),
            reservations=(reservation.model_copy(update={"amounts": BudgetAmounts(
                tokens=100, calls=1, money_usd=Decimal("1"))}),), settlements=(settlement,))


def test_runtime_core_imports_stay_independent_of_building_layer():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for file in (root / "src/agent_runtime").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(), filename=str(file))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level <= 1, (file.name, "cannot import parent application")
                names = [node.module or ""] if not node.level else []
            else:
                continue
            for name in names:
                assert name not in {"src", "agent", "scripts"}, (file.name, name)
                assert not name.startswith(("src.agent.", "scripts.")), (file.name, name)
                assert name != "src.agent", (file.name, name)


def test_write_json_rides_out_a_brief_windows_sharing_violation(tmp_path, monkeypatch):
    # 10-07 sm24 run4: an external handle on checkpoint.json made os.replace
    # raise PermissionError and ended an elevation reader.
    import src.agent_runtime.store as store_module
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=30.0, tokens=1_000)
    store = EventStore(tmp_path / "run", run_id="run-test", task_id="task", budget_limit=limits.ledger_limit())
    real, calls = store_module.os.replace, []

    def flaky(source, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(source, target)

    monkeypatch.setattr(store_module.os, "name", "nt")
    monkeypatch.setattr(store_module.os, "replace", flaky)
    monkeypatch.setattr(store_module.time, "sleep", lambda _: None)
    store.write_json("checkpoint.json", {"saved": True})
    assert json.loads((tmp_path / "run" / "checkpoint.json").read_bytes()) == {"saved": True}
    assert len(calls) == 3
