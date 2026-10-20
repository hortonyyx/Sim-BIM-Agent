"""Offline bounded reconnection with immutable state, accounting and restart holds."""

import asyncio
import json

import httpx
import pytest

from src.agent_runtime.failures import transport_diagnostics
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.network_recovery import recovery_summary
from src.agent_runtime.responses import HttpResponsesAdapter
from src.harness_contracts import NetworkRecoveryPayload, RunLifecyclePayload
from test_agent_runtime import MESSAGES, response, runtime
from test_runtime_recovery_edges import InjectedCrash
from test_runtime_responses import completed, provider, sse
from test_runtime_responses_integration import engine_for, native


def configured(tmp_path, handler, *, tools=None, cap=2.0, calls=12, retries=1,
               backoff=0.001, seconds=None, inherited=0.0):
    limits = RunLimits(model_calls=calls, tool_calls=5, seconds=seconds, tokens=None,
        max_model_retries=retries, retry_backoff_seconds=backoff, model_reconnect_seconds=cap)
    engine = engine_for(tmp_path, [], limits=limits, **({"tools": tools} if tools else {}))
    engine.adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(handler))
    engine.reconnect_consumed_before_seconds = inherited
    return engine


async def run(engine, *, resume=False):
    try:
        return await engine.run(MESSAGES, resume=resume)
    finally:
        await engine.adapter.close()


def test_recovery_exceeds_legacy_retry_count_without_replaying_saved_writes(tmp_path):
    seen = []
    async def handle(request):
        seen.append(json.loads(request.content))
        if len(seen) in (2, 3, 4):
            # Exceed Windows monotonic's coarse resolution for this duration
            # assertion; immediate MockTransport failures can legitimately be 0.
            await asyncio.sleep(0.02)
            raise httpx.ConnectError("must not persist connection secret")
        raw = (native(("before", "save", {"value": 1})) if len(seen) == 1 else
               native(("after", "save", {"value": 2})) if len(seen) == 5 else native(text="Done"))
        return httpx.Response(200, content=sse(completed(raw)))
    engine = configured(tmp_path, handle)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "completed" and result["model_calls"] == 6
        assert engine.tools.calls == [("save", {"value": 1}), ("save", {"value": 2})]
        assert result["retries"] == 3 and result["usage_complete"] is False
        settlements = [e.payload.settlement for e in engine.store.events
                       if e.payload.event_type == "budget" and e.payload.action == "settle"]
        assert sum(s.usage.kind == "missing" and s.actual.tokens is None for s in settlements) == 3
        recovery = result["network_recovery"]
        assert 0 < recovery["backoff_seconds"] < recovery["consumed_seconds"] < 2
        outcome = result["timing"]["request_outcomes"]
        assert outcome["failed_requests_measured"] == 3
        assert outcome["successful_requests_measured"] == 3
        assert outcome["failed_request_seconds"] > 0
        assert outcome["backoff_seconds"] == recovery["backoff_seconds"]
        engine.store.validate()


def test_default_zero_preserves_one_retry_policy(tmp_path):
    seen = []
    def handle(request):
        seen.append(request)
        raise httpx.ConnectError("offline")
    engine = configured(tmp_path, handle, cap=0)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "model_retries_exhausted:transport_error"
        assert len(seen) == 2 and result["network_recovery"]["consumed_seconds"] == 0


def test_failure_marked_after_response_is_counted_once_in_request_timing(tmp_path):
    seen = []
    async def handle(request):
        seen.append(request)
        if len(seen) == 1:
            await asyncio.sleep(0.02)
            raw = native(text="")
            raw["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        else:
            raw = native(text="Done")
        return httpx.Response(200, content=sse(completed(raw)))
    engine = configured(tmp_path, handle, cap=0)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "completed" and result["model_calls"] == 2
        timing = result["timing"]["request_outcomes"]
        assert timing["failed_requests_measured"] == 1
        assert timing["successful_requests_measured"] == 1
        assert timing["failed_request_seconds"] > 0


def test_response_header_wait_cannot_exceed_remaining_recovery_allowance(tmp_path):
    seen = []
    async def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("offline")
        await asyncio.sleep(2)
        return httpx.Response(200, content=sse(completed(native(text="Must not arrive"))))
    engine = configured(tmp_path, handle, cap=0.06)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "model_reconnect_budget_exhausted"
        assert len(seen) == 2
        assert result["network_recovery"]["remaining_seconds"] == 0
        assert result["network_recovery"]["pending_reserved_seconds"] == 0
        assert result["network_recovery"]["reconnect_request_wait_seconds"] < 0.5
        failures = [e.payload.model_failure for e in engine.store.events
                    if e.payload.event_type == "run_lifecycle" and e.payload.model_failure]
        assert failures[-1].category == "timeout"
        assert failures[-1].transport_phase == "awaiting_response_headers"
        engine.store.validate()


def test_connected_stream_can_complete_after_recovery_window(tmp_path):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.15)
            yield sse(completed(native(text="Done")))
    seen = []
    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, stream=SlowStream())
    engine = configured(tmp_path, handle, cap=0.06)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "completed"
        assert result["timing"]["request_outcomes"]["successful_request_seconds"] >= 0.15
        assert result["network_recovery"]["consumed_seconds"] < 0.06


@pytest.mark.parametrize("kind,status", [
    ("retry_hint", "transport_error"), ("permission", "permission_denied"),
    ("quota", "quota_exhausted"), ("server_error", "service_unavailable"),
])
def test_recovery_does_not_override_veto_or_nontransport_policy(tmp_path, kind, status):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: {}\n\n"
            raise httpx.ReadError("offline")
    seen = []
    def handle(request):
        seen.append(request)
        if kind == "retry_hint":
            return httpx.Response(200, stream=BrokenStream(), headers={"x-retry-metadata": "NO_MORE_RETRY"})
        code = 401 if kind == "permission" else 429 if kind == "quota" else 503
        return httpx.Response(code, json={"error": {"code": "insufficient_quota" if kind == "quota" else "error"}})
    engine = configured(tmp_path, handle, retries=0)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] in {status, "model_retries_exhausted:" + status}
        assert len(seen) == 1 and result["network_recovery"]["consumed_seconds"] == 0


def test_reconnect_cannot_extend_model_call_or_wall_time_limits(tmp_path):
    def handle(request):
        raise httpx.ConnectError("offline")
    engine = configured(tmp_path / "calls", handle, calls=3)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "model_budget_exhausted" and result["model_calls"] == 3
    engine = configured(tmp_path / "time", handle, seconds=1, backoff=2)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "time_budget_exhausted" and result["model_calls"] <= 1


def test_resume_conserves_interrupted_backoff_reservation_and_does_not_repeat_write(tmp_path):
    seen = []
    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, content=sse(completed(native(("once", "save", {"value": 7})))))
        raise httpx.ConnectError("offline")
    engine = configured(tmp_path, handle, cap=0.25, backoff=0.2, retries=0)
    def crash(boundary, _):
        if boundary == "after_reconnect_backoff_reservation":
            raise InjectedCrash(boundary)
    engine.fault_hook = crash
    with engine.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(run(engine))
        summary = recovery_summary(engine.store, limit_seconds=0.25)
        assert summary["pending_reserved_seconds"] == 0.2
        assert summary["remaining_seconds"] == pytest.approx(0.05)
    resumed = configured(tmp_path, lambda request: httpx.Response(200, content=sse(completed(native(text="Recovered")))),
                         tools=engine.tools, cap=0.25, backoff=0.2, retries=0)
    with resumed.store:
        result = asyncio.run(run(resumed, resume=True))
        assert result["status"] == "completed"
        assert result["network_recovery"]["consumed_seconds"] >= 0.2
        assert result["network_recovery"]["remaining_seconds"] <= 0.05
        assert engine.tools.calls == [("save", {"value": 7})]
        resumed.store.validate()


def test_resume_does_not_promote_unknown_summary_into_primary_request(tmp_path):
    limits = RunLimits(model_calls=6, tool_calls=3, seconds=None, tokens=None,
        max_model_retries=1, retry_backoff_seconds=0.001, model_reconnect_seconds=2, summary_every=1)
    policy = ContextPolicy(active_window_messages=16, preserve_initial_messages=2)
    engine = runtime(tmp_path, [response(("saved", "save", {"value": 4})), ConnectionError("offline")],
                     limits=limits)
    engine.context_policy = policy
    def crash(boundary, _):
        if boundary == "after_reconnect_backoff_reservation":
            raise InjectedCrash(boundary)
    engine.fault_hook = crash
    with engine.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
        assert [e.payload.logical_purpose for e in engine.store.events
                if e.payload.event_type == "adapter_request"] == ["primary_task", "context_summary"]
    resumed = runtime(tmp_path, [response(text="Must not dispatch")], limits=limits, tools=engine.tools)
    resumed.context_policy = policy
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "resume_request_outcome_unknown"
        assert resumed.adapter.requests == []
        assert engine.tools.calls == [("save", {"value": 4})]


def test_inherited_consumption_is_not_reset_for_new_execution(tmp_path):
    def handle(request):
        raise httpx.ConnectError("offline")
    engine = configured(tmp_path, handle, cap=0.2, inherited=0.19, backoff=0.02)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "model_reconnect_budget_exhausted" and result["model_calls"] == 1
        assert result["network_recovery"]["consumed_seconds"] == 0.19
        assert result["network_recovery"]["remaining_seconds"] == pytest.approx(0.01)


def test_observed_timeout_overrun_stays_charged_when_continued(tmp_path):
    engine = configured(tmp_path, lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
                         cap=0.1, inherited=0.1001)
    with engine.store:
        result = asyncio.run(run(engine))
        assert result["status"] == "model_reconnect_budget_exhausted"
        assert result["model_calls"] == 1
        assert result["network_recovery"]["consumed_seconds"] == 0.1001
        assert result["network_recovery"]["remaining_seconds"] == 0


def test_two_children_wait_for_live_header_reservation_instead_of_false_exhaustion(tmp_path):
    root = configured(tmp_path / "root", lambda request: None, cap=1.0)
    children = []
    async def together():
        failed = set()
        barrier = asyncio.Event()
        for name in ("a", "b"):
            calls = []
            async def handler(request, name=name, calls=calls):
                calls.append(request)
                if len(calls) == 1:
                    failed.add(name)
                    if len(failed) == 2:
                        barrier.set()
                    await barrier.wait()
                    raise httpx.ConnectError("offline")
                await asyncio.sleep(0.08)
                return httpx.Response(200, content=sse(completed(native(text="Recovered " + name))))
            child = configured(tmp_path / name, handler, cap=1.0)
            child.store.close()
            child.store = root.store.for_task(name, parent_task_id=root.store.task_id)
            children.append(child)
        return await asyncio.wait_for(asyncio.gather(*(run(child) for child in children)), timeout=5)
    with root.store:
        root.store.append(RunLifecyclePayload(action="start", reason="shared recovery test"),
            source_refs=(root.store.source("runtime-config", {
                "limits": {"model_reconnect_seconds": 1.0}, "reconnect_consumed_before_seconds": 0.2}),))
        results = asyncio.run(together())
        assert all(result["status"] == "completed" and result["model_calls"] == 2 for result in results)
        summary = recovery_summary(root.store, limit_seconds=1)
        assert summary["inherited_seconds"] == 0.2
        assert 0.35 < summary["consumed_seconds"] < 1
        assert summary["pending_reserved_seconds"] == 0
        root.store.validate()
    asyncio.run(root.adapter.close())


def test_recovery_events_reject_duplicate_settlement_before_append(tmp_path):
    engine = configured(tmp_path, lambda request: httpx.Response(200, content=sse(completed(native(text="Done")))))
    with engine.store:
        asyncio.run(run(engine))
        request = next(e for e in engine.store.events if e.payload.event_type == "adapter_request")
        reserve = NetworkRecoveryPayload(action="reserve", operation_id="test-reserve",
            phase="backoff", request_event_id=request.event_id, reserved_seconds=0.1)
        engine.store.append(reserve)
        settle = reserve.model_copy(update={"action": "settle", "elapsed_seconds": 0.01})
        engine.store.append(settle)
        before = engine.store.path.read_bytes()
        with pytest.raises(ValueError, match="unique reservation"):
            engine.store.append(settle)
        assert engine.store.path.read_bytes() == before
        engine.store.validate()


def test_transport_diagnostics_record_numeric_os_codes_without_secret_text():
    cause = ConnectionRefusedError(10061, "password=private")
    outer = httpx.ConnectError("Authorization: Bearer private")
    outer.__cause__ = cause
    value = transport_diagnostics(outer)
    assert value["os_error_codes"] == (10061,)
    assert "private" not in json.dumps(value)
