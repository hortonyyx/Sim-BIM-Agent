"""Offline routing, credential isolation, wire audit and subscription budgets."""

import asyncio
import base64
import copy
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from src.agent.runtime_entry import parser, runtime_model_profile
from src.agent.runtime_configuration import argv_for, load_configuration
from src.agent_runtime.connections import resolve_connection
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.providers import (GLM_SUBSCRIPTION,
    GLM_SUBSCRIPTION_BASE_URL, provider_parameters, subscription_credentials)
from test_agent_runtime import MESSAGES, response, runtime


def test_paratera_standard_image_detail_preserves_pixels_sources_and_tool_continuation(tmp_path):
    """A real 400 counterexample: local original hint is not a wire enum."""
    from src.agent_runtime.adapter import HttpChatAdapter
    engine = runtime(tmp_path, [], limits=RunLimits(model_calls=2, tool_calls=1, seconds=30, tokens=200000))
    engine.model = 'GLM-5.3-Flash'
    engine.parameters = provider_parameters('paratera', model=engine.model,
                                            output_tokens=32000, reasoning_effort='high')
    engine.versions = engine.versions.model_copy(update={'remote_model':
        engine.versions.remote_model.model_copy(update={'route_id':'paratera','remote_alias':engine.model})})
    buffer = io.BytesIO()
    Image.new('RGB', (8,8), 'green').save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode()
    messages = copy.deepcopy(MESSAGES)
    messages[-1]['content'] = [{'type':'text','text':'Read the images, then use view once.'}, *[
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+encoded,'detail':detail}}
        for detail in ('low','high','auto','original')]]
    original = copy.deepcopy(messages)
    call_tool = engine.tools.call_tool
    async def hinted_tool(name, arguments):
        raw = await call_tool(name, arguments)
        raw['content'][0]['_meta'] = {'codex/imageDetail':'original'}
        return raw
    engine.tools.call_tool = hinted_tool
    sent = []
    def server(request):
        body = json.loads(request.content)
        sent.append(body)
        for message in body['messages']:
            content = message.get('content')
            for block in content if isinstance(content,list) else []:
                if block.get('type')=='image_url' and block['image_url'].get('detail')=='original':
                    return httpx.Response(400, json={'error':{'code':'400',
                        'message':'messages.content.image_url.detail supports low, high, auto'}})
        return httpx.Response(200, json=response(('view-1','view',{})) if len(sent)==1 else response(text='OK'))
    async def scenario():
        adapter = HttpChatAdapter(base_url='https://fake.example.test/v1', api_key='offline-test-key',
                                  transport=httpx.MockTransport(server))
        engine.adapter = adapter
        try:
            with engine.store:
                receipt = await engine.run(messages)
                assert receipt['status']=='completed' and receipt['model_calls']==2
                assert receipt['usage_complete'] and receipt['tool_calls']==1
                assert messages==original
                assert [b['image_url']['detail'] for b in sent[0]['messages'][1]['content'][1:]]==['low','high','auto','high']
                requests=[e.payload for e in engine.store.events if e.payload.event_type=='adapter_request']
                assert [len(r.images) for r in requests]==[4,5]
                assert all(i.original.sha256==i.sent.sha256 for r in requests for i in r.images)
                assert any(i.source.source_id=='paratera-image-detail-projection' for i in requests[0].injected_content)
                execution=next(e.payload for e in engine.store.events if e.payload.event_type=='tool_execution')
                raw=engine.store.resolve(execution.raw_result)
                shown=engine.store.resolve(execution.shown_result)
                assert raw['content'][0]['_meta']['codex/imageDetail']=='original'
                assert shown['image_blocks'][-1]['image_url']['detail']=='high'
                assert shown['projected_image_display_hints']==[{'image_index':0,'from':'original','to':'high'}]
                engine.store.validate()
        finally:
            await adapter.close()
    asyncio.run(scenario())


def subscription_engine(tmp_path, responses, **kwargs):
    engine = runtime(tmp_path, responses, **kwargs)
    engine.model = "glm-5.3-flash"
    engine.parameters = provider_parameters(GLM_SUBSCRIPTION, output_tokens=32000)
    engine.versions = engine.versions.model_copy(update={"remote_model":
        engine.versions.remote_model.model_copy(update={"route_id": GLM_SUBSCRIPTION,
            "remote_alias": engine.model})})
    return engine


def test_credentials_only_read_explicit_file_without_environment_fallback(monkeypatch, tmp_path):
    import dotenv
    monkeypatch.setenv("GLM_API_KEY", "environment-must-not-be-used")
    seen = []
    credentials = tmp_path / "subscription.env"
    credentials.touch()
    def read(path, **kwargs):
        seen.append((path, kwargs))
        return {"GLM_BASE_URL": GLM_SUBSCRIPTION_BASE_URL, "GLM_API_KEY": "file-test-key"}
    monkeypatch.setattr(dotenv, "dotenv_values", read)
    assert subscription_credentials(credentials) == (GLM_SUBSCRIPTION_BASE_URL, "file-test-key")
    assert seen == [(credentials, {"interpolate": False})]
    with pytest.raises(ValueError, match="explicit credentials file"):
        subscription_credentials()
    with pytest.raises(ValueError, match="does not exist"):
        subscription_credentials(tmp_path / "missing.env")
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **kw: {"GLM_BASE_URL": GLM_SUBSCRIPTION_BASE_URL})
    with pytest.raises(ValueError, match="missing"):
        subscription_credentials(credentials)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **kw: {"GLM_BASE_URL": "https://elsewhere.invalid", "GLM_API_KEY": "private"})
    with pytest.raises(ValueError, match="reviewed Coding Plan"):
        subscription_credentials(credentials)


def test_credentials_do_not_interpolate_secrets_or_read_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_VALUE", "do-not-expand")
    credentials = tmp_path / "private.env"
    credentials.write_text(f"GLM_BASE_URL={GLM_SUBSCRIPTION_BASE_URL}\nGLM_API_KEY=${{PRIVATE_VALUE}}\n")
    assert subscription_credentials(credentials)[1] == "${PRIVATE_VALUE}"


def test_public_connection_descriptor_and_adapter_share_fake_endpoint_without_secret(tmp_path):
    credentials = tmp_path / "fake.env"
    credentials.write_text(
        "PARATERA_BASE_URL=https://fake.example.test/v9\nPARATERA_API_KEY=fake-private-key\n",
        encoding="utf-8",
    )
    connection = resolve_connection("paratera", credentials)
    public = connection.descriptor.model_route("Qwen3.8-27B")
    adapter = connection.create_adapter()
    try:
        assert adapter.endpoint == "https://fake.example.test/v9/chat/completions"
        assert public == {
            "route_id": "paratera", "model": "Qwen3.8-27B",
            "base_url": "https://fake.example.test/v9", "billing_mode": "metered",
            "adapter_kind": "openai_chat_completions",
        }
        assert "fake-private-key" not in json.dumps(public) and "fake-private-key" not in repr(connection)
    finally:
        asyncio.run(adapter.close())


@pytest.mark.parametrize("base_url,match", [
    ("https://user:private@fake.example.test/v1", "userinfo"),
    ("https://fake.example.test/v1?api_key=private", "sensitive query"),
])
def test_public_connection_rejects_url_credentials_before_version_record(tmp_path, base_url, match):
    credentials = tmp_path / "unsafe.env"
    credentials.write_text(
        f"PARATERA_BASE_URL={base_url}\nPARATERA_API_KEY=fake-private-key\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=match):
        resolve_connection("paratera", credentials)


def test_cli_route_and_service_defaults_are_explicit():
    args = parser().parse_args(["--out", "unused", "--provider", GLM_SUBSCRIPTION, "--model", "glm-5.3-flash"])
    assert args.temperature is None and args.output_tokens is None
    assert runtime_model_profile(args.provider, args.model).recommended_min_output_tokens == 32000
    assert provider_parameters(GLM_SUBSCRIPTION, output_tokens=32000) == {"max_tokens": 32000}
    with pytest.raises(ValueError, match="requires model"):
        runtime_model_profile(GLM_SUBSCRIPTION, "Qwen3.8-27B")
    with pytest.raises(ValueError, match="cannot be disabled"):
        provider_parameters(GLM_SUBSCRIPTION, output_tokens=32000, thinking=False)
    with pytest.raises(ValueError, match="unverified"):
        provider_parameters(GLM_SUBSCRIPTION, output_tokens=32000, reasoning_effort="minimal")
    assert provider_parameters(GLM_SUBSCRIPTION, output_tokens=32000, reasoning_effort="medium") == {
        "max_tokens": 32000, "reasoning_effort": "medium"}
    with pytest.raises(ValueError, match="does not offer"):
        provider_parameters("paratera", output_tokens=32000, reasoning_effort="medium")
    assert provider_parameters("paratera", output_tokens=32000) == {
        "max_tokens": 32000, "temperature": 0.0, "enable_thinking": True}


def test_subscription_tool_image_thinking_audit_and_currency(tmp_path):
    engine = subscription_engine(tmp_path, [response(("image", "view", {}), reasoning=True), response(text="OK", reasoning=True)])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 2
        assert result["billing_usd"] is None and result["estimated_cost_cny"] is None
        assert result["usage_accounting"]["billing_modes"] == ["subscription"]
        requests = [e.payload for e in engine.store.events if e.payload.event_type == "adapter_request"]
        for sent, request in zip(engine.adapter.requests, requests):
            assert sent == engine.store.capture_bytes(request.final_request_body)
            body = json.loads(sent)
            assert set(body) == {"model", "messages", "tools", "tool_choice", "stream", "max_tokens"}
            assert body["model"] == "glm-5.3-flash" and body["max_tokens"] == 32000
        assert len(requests[1].images) == 1
        responses = [e.payload for e in engine.store.events if e.payload.event_type == "model_response"]
        assert any(item.kind == "public_content" for item in responses[0].thinking)
        assert "reasoning_content" not in json.loads(engine.adapter.requests[1])["messages"][-1]
        engine.store.validate()


@pytest.mark.parametrize("limits,status", [
    (RunLimits(model_calls=2, tool_calls=0, seconds=30, tokens=1), "token_budget_exhausted"),
    (RunLimits(model_calls=2, tool_calls=0, seconds=0.000001, tokens=100000), "time_budget_exhausted"),
])
def test_subscription_still_stops_before_send_when_budget_exhausted(tmp_path, limits, status):
    engine = subscription_engine(tmp_path, [response(text="must not send")], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == status and engine.adapter.requests == []
