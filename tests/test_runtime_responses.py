"""Subscription protocol tests: all HTTP is injected, no credentials are read."""

import asyncio
import base64
import copy
import hashlib
import io
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from src.agent_runtime.estimation import conservative_compatibility_profile
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.responses import (ENDPOINT, build_responses_body,
    estimate_responses_messages, estimate_responses_request, HttpResponsesAdapter,
    parse_responses_response, prepare_responses_request, present_tools)
from src.agent_runtime.store import EventStore, json_bytes
from src.harness_contracts import BudgetAmounts
from src.harness_contracts.usage import reported_total_tokens
from test_agent_runtime import versions


TOOLS = [{"type": "function", "function": {"name": "view", "description": "inspect",
          "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}}]
PARAMETERS = {"max_tokens": 500, "reasoning_effort": "high", "temperature": 0.7}
USAGE = {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52,
         "input_tokens_details": {"cached_tokens": 30},
         "output_tokens_details": {"reasoning_tokens": 7}}


def response(output=None, status="completed", usage=True):
    result = {"object": "response", "id": "resp-offline", "status": status,
        "output": output if output is not None else [{"id": "msg-1", "type": "message",
            "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Done", "annotations": []}]}]}
    if usage:
        result["usage"] = copy.deepcopy(USAGE)
    return result


def sse(*events):
    return b"".join(("event: " + event.get("type", "message") + "\ndata: " + json.dumps(event) + "\n\n").encode() for event in events)


def completed(raw):
    return {"type": "response.completed", "response": raw}


@pytest.fixture
def store(tmp_path):
    with EventStore(tmp_path / "run", run_id="responses-test", task_id="root",
                    budget_limit=BudgetAmounts(tokens=1000000, seconds=Decimal(120), calls=10)) as value:
        yield value


def prepare(store, messages=None, **kwargs):
    messages = messages or [{"role": "system", "content": "Inspect locally"}, {"role": "user", "content": "View"}]
    return prepare_responses_request(store=store, model="offline-responses", messages=messages,
        message_sources=[store.source("message", message) for message in messages],
        tools=TOOLS, tool_source=store.source("tools", TOOLS), parameters=PARAMETERS,
        versions=versions(), **kwargs)


def picture():
    stream = io.BytesIO()
    Image.new("RGB", (12, 10), (20, 80, 120)).save(stream, format="PNG")
    raw = stream.getvalue()
    return raw, "data:image/png;base64," + base64.b64encode(raw).decode()


def send(adapter, request):
    async def run():
        try:
            return await adapter.send(request, timeout=5)
        finally:
            await adapter.close()
    return asyncio.run(run())


def provider(token="offline-oauth-token"):
    async def get():
        return token
    return get


def test_two_round_tools_image_cache_opaque_replay_and_wire_capture(store):
    reasoning = {"id": "rs-1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Inspect the view"}], "encrypted_content": "opaque-bytes"}
    opaque = {"id": "future-1", "type": "future_protocol_item", "data": {"opaque": [1, 2]}}
    call = {"id": "fc-1", "type": "function_call", "call_id": "call-view",
            "name": "view", "namespace": "runtime", "arguments": '{"name":"room"}', "status": "completed"}
    first = response([reasoning, opaque, call])
    second = response()
    seen, tokens = [], []
    async def token_provider():
        tokens.append("provided")
        return "offline-oauth-token"
    def handle(request):
        seen.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "Bearer offline-oauth-token"
        if len(seen) == 1:
            # Complete reasoning is available only in output_item.done here.
            final = copy.deepcopy(first)
            del final["output"][0]["encrypted_content"]
            events = [{"type": "response.created", "response": {"object": "response", "status": "in_progress"}},
                {"type": "response.output_text.delta", "delta": "ignored partial"},
                {"type": "response.output_item.done", "output_index": 0, "item": reasoning}, completed(final)]
        else:
            events = [completed(second)]
        return httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream"})
    adapter = HttpResponsesAdapter(token_provider=token_provider, transport=httpx.MockTransport(handle))
    first_request = prepare(store)
    image_bytes, image_url = picture()
    async def run():
        try:
            raw = await adapter.send(first_request, timeout=5)
            parsed = parse_responses_response(raw, "request-first", store)
            assert parsed.protocol_error is None and parsed.finish_reason == "tool_calls"
            assert parsed.assistant_message["responses_output"] == first["output"]
            assert parsed.assistant_message["tool_calls"][0]["namespace"] == "runtime"
            assert parsed.event_payload.tool_calls[0].full_arguments == {"name": "room"}
            assert reported_total_tokens(parsed.event_payload.usage.raw_usage) == 52
            assert parsed.event_payload.usage.raw_usage == USAGE
            assert {item.kind for item in parsed.event_payload.thinking} == {"summary", "reported_token_count"}
            # A persisted/reloaded assistant message has exactly the same replay.
            assistant = store.resolve(store.capture(parsed.assistant_message, force_blob=True))
            messages = [{"role": "system", "content": "Inspect locally"}, {"role": "user", "content": "View"}, assistant,
                {"role": "tool", "tool_call_id": "call-view", "content": [{"type": "text", "text": "room image"},
                    {"type": "image_url", "image_url": {"url": image_url}}]}]
            with pytest.raises(ValueError, match="reasoning_history=all"):
                prepare(store, messages, reasoning_history="current_tool_chain")
            second_request = prepare(store, messages, reasoning_history="all")
            assert second_request.body["input"][2:5] == first["output"]
            assert second_request.body["input"][5] == {"type": "function_call_output", "call_id": "call-view", "output": [
                {"type": "input_text", "text": "room image"}, {"type": "input_image", "image_url": image_url, "detail": "high"}]}
            assert len(second_request.event_payload.images) == 1
            image = second_request.event_payload.images[0]
            assert store.get_bytes(image.sent) == image_bytes
            assert image.request_reference == "/input/5/output/1/image_url"
            final = parse_responses_response(await adapter.send(second_request, timeout=5), "request-second", store)
            assert final.protocol_error is None and final.event_payload.visible_text == ("Done",)
            for prepared, sent in zip([first_request, second_request], seen, strict=True):
                assert sent.content == prepared.wire_bytes == store.capture_bytes(prepared.event_payload.final_request_body)
                assert prepared.event_payload.wire_sha256 == hashlib.sha256(sent.content).hexdigest()
                assert prepared.body["input"][0]["role"] == "developer"
                assert prepared.body["store"] is False and prepared.body["stream"] is True
                assert prepared.body["include"] == ["reasoning.encrypted_content"]
                assert not {"temperature", "max_tokens", "max_output_tokens", "previous_response_id"} & prepared.body.keys()
                assert prepared.event_payload.parameters.requested == PARAMETERS
                assert prepared.token_estimate.output_token_limit == 500
                assert prepared.body["tools"][0]["type"] == "namespace"
                assert prepared.body["tools"][0]["tools"][0]["strict"] is False
            assert len(tokens) == 2
        finally:
            await adapter.close()
    asyncio.run(run())


def test_estimate_real_history_tools_and_image_references(store):
    _, url = picture()
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url, "detail": "low"}}]}]
    request = prepare(store, messages)
    assert request.body["input"][0]["content"][0]["detail"] == "low"
    assert request.token_estimate.images[0].request_reference == "/input/0/content/0/image_url"
    assert request.token_estimate.image_tokens == 120
    base = estimate_responses_messages(model="offline-responses", messages=messages, tools=TOOLS, parameters=PARAMETERS)
    assert base == request.token_estimate
    changed = copy.deepcopy(request.body)
    changed["input"].append({"type": "reasoning", "encrypted_content": "long_opaque_item " * 1000})
    changed["tools"][0]["tools"][0]["description"] += "schema_text " * 200
    larger = estimate_responses_request(changed, output_token_limit=500)
    assert larger.input_tokens_upper_bound > base.input_tokens_upper_bound + 1000
    tight = replace(conservative_compatibility_profile("offline-responses"), context_window_tokens=501)
    assert not estimate_responses_request(request.body, output_token_limit=500, profile=tight).fits_context
    with pytest.raises(ValueError, match="reviewed"):
        estimate_responses_messages(model="unregistered-very-specific-model", messages=messages, tools=TOOLS, parameters=PARAMETERS, strict=True)


def test_usage_anchor_keeps_cached_input_and_accounts_only_new_items():
    from src.agent_runtime.responses import ResponsesUsageCalibration
    body = build_responses_body(model="gpt-6-sol", messages=[
        {"role": "system", "content": "stable rules"},
        {"role": "user", "content": "long source evidence " * 3000}], tools=TOOLS,
        parameters={"max_tokens": 32000, "reasoning_effort": "high"})
    calibration = ResponsesUsageCalibration("gpt-6-sol")
    raw = {"usage": {"input_tokens": 8000, "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 7900}}, "output": []}
    calibration.observe(body, raw, response_event_id="event-observed")
    base = estimate_responses_request(body, output_token_limit=32000, strict=True)
    actual = calibration.estimate(body, base, strict=True)
    assert actual.input_tokens_estimate == 8000  # not 100 uncached tokens
    assert actual.input_tokens_upper_bound >= 8000
    assert "event-observed" in actual.source and actual.input_tokens_upper_bound < base.input_tokens_upper_bound
    changed = copy.deepcopy(body)
    changed["input"].append({"role": "user", "content": [{"type": "input_text", "text": "new evidence"}]})
    newer = calibration.estimate(changed, estimate_responses_request(changed, output_token_limit=32000))
    assert 8000 < newer.input_tokens_estimate < 8500
    wrong_model = ResponsesUsageCalibration("gpt-6-luna")
    wrong_model.observe(body, raw, response_event_id="wrong-model")
    assert wrong_model.estimate(body, base) == base
    changed_tools = {**body, "tools": []}
    uncalibrated = estimate_responses_request(changed_tools, output_token_limit=32000)
    assert calibration.estimate(changed_tools, uncalibrated) == uncalibrated


def test_reasoning_ciphertext_length_never_becomes_context_tokens():
    from src.agent_runtime.responses import ResponsesUsageCalibration
    original = build_responses_body(model="gpt-6-luna", messages=[{"role": "user", "content": "read"}],
        tools=[], parameters={"max_tokens": 32000, "reasoning_effort": "high"})
    estimates, calibrated = [], []
    for length in (20, 100000):
        opaque = {"type": "reasoning", "id": "rs_opaque", "summary": [],
                  "encrypted_content": "encrypted-state-" * length}
        body = {**original, "input": [*original["input"], opaque]}
        base = estimate_responses_request(body, output_token_limit=32000, strict=True)
        estimates.append(base.reservation_tokens)
        calibration = ResponsesUsageCalibration("gpt-6-luna")
        calibration.observe(original, {"usage": {"input_tokens": 1000, "output_tokens": 750,
            "output_tokens_details": {"reasoning_tokens": 700}}, "output": [opaque]},
            response_event_id="reasoning-observed")
        result = calibration.estimate(body, base, strict=True)
        calibrated.append(result.reservation_tokens)
        assert result.input_tokens_estimate == 1700
        assert body["input"][-1] == opaque
    assert estimates[0] == estimates[1] and calibrated[0] == calibrated[1]


@pytest.mark.parametrize("code,expected", [("context_length_exceeded", "context_length_exceeded"),
    ("context_window_exceeded", "context_length_exceeded"), ("invalid_parameter", "invalid_request")])
def test_context_error_uses_machine_code_without_inventing_usage(code, expected):
    from src.agent_runtime.failures import http_failure
    failure = http_failure(httpx.Response(400, json={"error": {"code": code,
        "message": "context length mentioned in arbitrary prose"}}), secret="")
    assert failure.details["category"] == expected
    assert failure.details["retryable"] is False and failure.usage.kind == "missing"


@pytest.mark.parametrize("kind", ["response.failed", "error"])
def test_stream_context_error_is_classified_without_inventing_usage(store, kind):
    error = {"type": "invalid_request_error", "code": "context_length_exceeded"}
    event = ({"type": kind, "response": {"error": error}} if kind == "response.failed"
             else {"type": kind, "error": error})
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                       content=sse(event))))
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.details["category"] == "context_length_exceeded"
    assert caught.value.details["retryable"] is False and caught.value.usage.kind == "missing"


def test_unsupported_preview_fields_are_local_only_and_overrides_rejected():
    omitted = {"max_tokens": 10, "temperature": 1, "top_p": .5, "background": True,
        "previous_response_id": "forbidden-server-history", "max_output_tokens": 99, "metadata": {"test": 1}}
    body = build_responses_body(model="offline", messages=[{"role": "user", "content": "x"}], tools=TOOLS, parameters=omitted)
    assert not set(omitted) & body.keys()
    for key in ["model", "input", "messages", "tools", "stream", "store", "include", "n", "unreviewed"]:
        with pytest.raises(ValueError, match="unreviewed"):
            build_responses_body(model="offline", messages=[], tools=[], parameters={"max_tokens": 10, key: "override"})
    for allowance in [None, 0, -1, True, "10"]:
        with pytest.raises(ValueError, match="allowance"):
            build_responses_body(model="offline", messages=[], tools=[], parameters={"max_tokens": allowance})


def test_legacy_assistant_tool_calls_and_following_image_message(store):
    _, url = picture()
    messages = [{"role": "assistant", "content": "Looking", "tool_calls": [{"id": "call", "type": "function",
        "function": {"name": "view", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "call", "content": "Image follows"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]
    body = prepare(store, messages).body
    assert body["input"][1] == {"type": "function_call", "call_id": "call", "name": "view", "namespace": "runtime", "arguments": "{}"}
    assert body["input"][2]["output"] == [{"type": "input_text", "text": "Image follows"}]
    assert body["input"][3]["content"][0]["image_url"] == url


@pytest.mark.parametrize("output", [
    [{"type": "function_call", "call_id": "c", "namespace": "other", "name": "view", "arguments": "{}"}],
    [{"type": "function_call", "call_id": "c", "name": "view", "arguments": "[]"}],
    [{"type": "function_call", "call_id": "c", "name": "view", "arguments": "{}", "status": "in_progress"}],
    [{"type": "function_call", "call_id": "c", "name": "view", "arguments": "{"}],
    [{"type": "function_call", "call_id": "c", "name": "view", "arguments": "{}"}] * 2,
])
def test_malformed_tool_batches_are_never_executable(store, output):
    parsed = parse_responses_response(response(output), "req", store)
    assert parsed.protocol_error == "malformed_response"
    assert parsed.event_payload.tool_calls == () and parsed.assistant_message == {}
    assert parsed.event_payload.usage.raw_usage == USAGE


def test_incomplete_tool_calls_never_execute_missing_usage_stays_missing(store):
    raw = response([{"type": "function_call", "call_id": "c", "name": "view", "arguments": "{"}], status="incomplete", usage=False)
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    parsed = parse_responses_response(raw, "req", store)
    assert parsed.protocol_error == "incomplete_response" and parsed.finish_reason == "length"
    assert parsed.event_payload.tool_calls == () and parsed.event_payload.usage.kind == "missing"
    assert parsed.assistant_message["responses_output"] == raw["output"]


@pytest.mark.parametrize("kind,status,category", [("response.failed", "failed", "service_error"), ("error", "failed", "service_error")])
def test_stream_failures_preserve_usage_and_never_leak_oauth(store, kind, status, category):
    token = "test-oauth-must-never-leak"
    raw = response(status=status)
    raw["error"] = {"type": "failure", "message": "Bearer " + token}
    event = {"type": kind, "response": raw} if kind != "error" else {"type": kind, "error": raw["error"], "usage": USAGE}
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=sse(event))
    with pytest.raises(ModelServiceError) as caught:
        send(HttpResponsesAdapter(token_provider=provider(token), transport=httpx.MockTransport(handle)), prepare(store))
    assert caught.value.details["category"] == category
    assert caught.value.details["usage_received"] is True
    assert caught.value.usage.raw_usage == USAGE
    assert token not in str(caught.value) + json.dumps(caught.value.details) + repr(caught.value.__cause__)
    assert len(requests) == 1


def test_failed_terminal_without_usage_keeps_prior_frame_usage_and_rejects_partial_tools(store):
    known_usage = {"input_tokens": 30, "output_tokens": 22, "total_tokens": 52}
    partial_call = {"type": "function_call", "call_id": "partial-call", "name": "view",
        "namespace": "runtime", "status": "in_progress", "arguments": '{"name":'}
    progress = response([partial_call], status="in_progress")
    progress["usage"] = known_usage
    failed = response([partial_call], status="failed", usage=False)
    failed["error"] = {"type": "server_error", "message": "stream failed"}
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=sse(
            {"type": "response.in_progress", "response": progress},
            {"type": "response.failed", "response": failed}))
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(handle))
    # A failed stream must raise, never return partial calls to the parser or
    # execution layer, even when the terminal response itself is nonempty.
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.details["category"] == "service_error"
    assert caught.value.details["usage_received"] is False
    assert caught.value.usage.kind == "missing"
    assert caught.value.observed_usage.raw_usage == known_usage
    assert reported_total_tokens(caught.value.observed_usage.raw_usage) == 52
    assert adapter.last_received_usage.raw_usage == known_usage
    assert len(requests) == 1


@pytest.mark.parametrize("reason,finish", [("max_output_tokens", "length"), ("content_filter", "stop")])
def test_incomplete_terminal_retains_raw_evidence_but_parser_rejects(store, reason, finish):
    raw = response([{"type": "function_call", "call_id": "c", "namespace": "runtime", "name": "view", "arguments": "{"}], status="incomplete")
    raw["incomplete_details"] = {"reason": reason}
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=sse({"type": "response.incomplete", "response": raw}))))
    returned = send(adapter, prepare(store))
    assert returned == raw
    parsed = parse_responses_response(returned, "req", store)
    assert parsed.protocol_error == "incomplete_response" and parsed.finish_reason == finish
    assert parsed.event_payload.tool_calls == () and parsed.event_payload.usage.raw_usage == USAGE
    assert store.resolve(parsed.event_payload.raw_response) == raw


@pytest.mark.parametrize("content,category", [
    (sse({"type": "response.in_progress", "response": response(status="in_progress")}), "transport_error"),
    (sse({"type": "response.in_progress", "response": response(status="in_progress")}) + b"data: {broken\n\n", "invalid_response"),
    (sse({"type": "response.in_progress", "response": response(status="in_progress")}, completed(response(status="in_progress"))), "invalid_response"),
    (sse({"type": "response.in_progress", "response": response(status="in_progress")}) + b"data: [DONE]\n\n", "invalid_response"),
])
def test_nonterminal_or_invalid_stream_is_not_success(store, content, category):
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.details["category"] == category
    assert caught.value.usage.kind == "missing"
    assert caught.value.details["usage_received"] is False
    assert caught.value.observed_usage.raw_usage == USAGE


def test_interrupted_transport_carries_last_usage_no_exception_secrets(store):
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"type": "response.in_progress", "response": response(status="in_progress")})
            raise httpx.ReadError("private-oauth-token")
    adapter = HttpResponsesAdapter(token_provider=provider("private-oauth-token"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Interrupted())))
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.details["category"] == "transport_error"
    assert caught.value.usage.kind == "missing"
    assert caught.value.details["usage_received"] is False
    assert caught.value.observed_usage.raw_usage == USAGE
    assert "private-oauth-token" not in json.dumps(caught.value.details)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("status,error,category", [(401, "authentication_error", "permission_denied"),
    (429, "insufficient_quota", "quota_exhausted"), (429, "rate_limit_error", "temporary_rate_limit"),
    (503, "server_error", "service_unavailable"), (307, "redirect", "invalid_request")])
def test_http_failure_is_one_request_no_retry_redirect_or_invented_usage(store, status, error, category):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"type": error, "message": "private-oauth-token"}},
            headers={"Location": "https://elsewhere.invalid/", "x-request-id": "private-oauth-token"})
    with pytest.raises(ModelServiceError) as caught:
        send(HttpResponsesAdapter(token_provider=provider("private-oauth-token"), transport=httpx.MockTransport(handle)), prepare(store))
    assert caught.value.details["category"] == category
    assert caught.value.usage.kind == "missing"
    assert not caught.value.details["usage_received"]
    assert "private-oauth-token" not in json.dumps(caught.value.details)
    assert len(requests) == 1


def test_http_failure_keeps_nested_cache_reasoning_usage(store):
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request:
        httpx.Response(503, json={"error": {"type": "server_error"}, "usage": USAGE})))
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.usage.raw_usage == USAGE
    assert reported_total_tokens(caught.value.usage.raw_usage) == 52


@pytest.mark.parametrize("token", ["", "sk-not-an-oauth-token", "with whitespace", None])
def test_only_injected_oauth_token_accepted_no_network(store, token):
    def handle(request):
        pytest.fail("invalid token must not issue HTTP")
    with pytest.raises(ModelServiceError) as caught:
        send(HttpResponsesAdapter(token_provider=provider(token), transport=httpx.MockTransport(handle)), prepare(store))
    assert caught.value.details["category"] == "permission_denied"


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_token_provider_failure_omits_arbitrary_text(store, error_type):
    async def broken():
        raise error_type("secret from external provider")
    with pytest.raises(ModelServiceError) as caught:
        send(HttpResponsesAdapter(token_provider=broken, transport=httpx.MockTransport(lambda request: pytest.fail("no HTTP"))), prepare(store))
    assert "secret from external provider" not in json.dumps(caught.value.details)
    assert caught.value.details["category"] == "permission_denied"
    assert caught.value.__cause__ is None


def test_sse_multiline_comments_and_no_trailing_blank_line(store):
    raw = response()
    event = json.dumps(completed(raw), indent=2)
    content = (": heartbeat\r\nevent: response.completed\r\n" + "\r\n".join("data: " + line for line in event.splitlines())).encode()
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
    assert send(adapter, prepare(store)) == raw


def test_present_tools_retains_actual_wire_and_requires_context_for_changes(store, monkeypatch):
    # Parent integrates the new contract discriminator separately. Capture the
    # helper's payload here without modifying the frozen base contract.
    import src.harness_contracts
    monkeypatch.setattr(src.harness_contracts, "ToolPresentationPayload", lambda **fields: SimpleNamespace(**fields))
    _, url = picture()
    logical = {"type": "image_url", "image_url": {"url": url}}
    original = {"tool_message": {"role": "tool", "tool_call_id": "call", "content": "Image follows"}, "image_blocks": [logical]}
    execution = SimpleNamespace(event_id="exec-1", payload=SimpleNamespace(event_type="tool_execution", call_id="call", shown_result=store.capture(original)))
    captured = []
    fake = SimpleNamespace(events=[execution], resolve=store.resolve, capture=store.capture, append=captured.append)
    body = build_responses_body(model="offline", messages=[original["tool_message"], {"role": "user", "content": [logical]}], tools=[], parameters=PARAMETERS)
    present_tools(fake, body, SimpleNamespace(event_id="req"), SimpleNamespace(event_id="resp"), None)
    assert len(captured) == 1 and captured[0].protocol_conversion == "responses_v1"
    evidence = store.resolve(captured[0].shown_result)
    assert evidence["tool_message"] == original["tool_message"]
    assert evidence["image_blocks"] == [logical]
    assert evidence["wire_tool_result"] == body["input"][0]
    assert evidence["wire_image_blocks"] == [body["input"][1]["content"][0]]
    body["input"][0]["output"][0]["text"] = "Changed"
    with pytest.raises(ValueError, match="context evidence"):
        present_tools(fake, body, SimpleNamespace(event_id="req"), SimpleNamespace(event_id="resp"), None)
    present_tools(fake, body, SimpleNamespace(event_id="req"), SimpleNamespace(event_id="resp"), "ctx")
    assert captured[-1].context_event_id == "ctx"


@pytest.mark.parametrize("events", [
    [{"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "encrypted_content": "one"}},
     {"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "encrypted_content": "two"}}],
    [{"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "wrong-message-id"}}, completed(response())],
    [{"type": "response.output_item.done", "output_index": 2, "item": {"type": "reasoning", "encrypted_content": "gap"}}, completed(response())],
])
def test_conflicting_or_gapped_sse_items_rejected(store, events):
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=sse(*events))))
    with pytest.raises(ModelServiceError) as caught:
        send(adapter, prepare(store))
    assert caught.value.details["category"] == "invalid_response"


def test_opaque_item_done_not_in_terminal_is_preserved(store):
    opaque = {"type": "future_item", "id": "opaque-1", "data": "exact opaque"}
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=sse({"type": "response.output_item.done", "output_index": 1, "item": opaque}, completed(response())))))
    raw = send(adapter, prepare(store))
    assert raw["output"][1] == opaque
    parsed = parse_responses_response(raw, "req", store)
    assert parsed.protocol_error is None and parsed.assistant_message["responses_output"][1] == opaque


def test_completed_missing_usage_does_not_synthesize_zero(store):
    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=sse(completed(response(usage=False))))))
    parsed = parse_responses_response(send(adapter, prepare(store)), "req", store)
    assert parsed.protocol_error is None and parsed.event_payload.usage.kind == "missing"


def test_cancelled_send_propagates_cancellation_without_hidden_request(store):
    requests = []
    async def cancelled():
        raise asyncio.CancelledError()
    adapter = HttpResponsesAdapter(token_provider=cancelled, transport=httpx.MockTransport(lambda request: requests.append(request)))
    with pytest.raises(asyncio.CancelledError):
        send(adapter, prepare(store))
    assert requests == [] and adapter.last_send_timing["http_round_trip_seconds"] >= 0


def test_incomplete_terminal_error_diagnostics_redact_bearer_before_journal(store):
    raw = response(status="incomplete")
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    raw["error"] = {"code": "failure", "message": "Bearer secret-oauth-from-response"}
    adapter = HttpResponsesAdapter(token_provider=provider("secret-oauth-from-response"), transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=sse({"type": "response.incomplete", "response": raw}))))
    parsed = parse_responses_response(send(adapter, prepare(store)), "req", store)
    assert parsed.protocol_error == "incomplete_response"
    assert b"secret-oauth-from-response" not in store.capture_bytes(parsed.event_payload.raw_response)
    assert parsed.event_payload.usage.raw_usage == USAGE


def test_wait_for_cancelled_stream_preserves_current_usage_but_next_request_resets(store):
    class WaitingStream(httpx.AsyncByteStream):
        def __init__(self, with_usage):
            self.with_usage = with_usage

        async def __aiter__(self):
            progress = response(status="in_progress", usage=self.with_usage)
            yield sse({"type": "response.in_progress", "response": progress})
            await asyncio.Event().wait()

    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=WaitingStream(with_usage=len(requests) == 1))

    adapter = HttpResponsesAdapter(token_provider=provider(), transport=httpx.MockTransport(handle))
    assert adapter.last_received_usage.kind == "missing"
    request = prepare(store)
    async def run():
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(adapter.send(request, timeout=5), timeout=.03)
            assert adapter.last_received_usage.kind == "reported"
            assert adapter.last_received_usage.raw_usage == USAGE
            assert reported_total_tokens(adapter.last_received_usage.raw_usage) == 52
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(adapter.send(request, timeout=5), timeout=.03)
            assert adapter.last_received_usage.kind == "missing"
            assert len(requests) == 2
        finally:
            await adapter.close()
    asyncio.run(run())
