"""Original-vendor Paratera parameters and captured image/tool response shapes."""
import asyncio
import base64
import copy
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from src.agent_runtime.adapter import HttpChatAdapter
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.providers import provider_parameters
from test_agent_runtime import MESSAGES, runtime


@pytest.mark.parametrize('model,effort,cap,key,temperature', [
    ('Kimi-K3', 'high', 1048576, 'max_completion_tokens', 1.0),
    ('kimi-k3', 'low', 32000, 'max_completion_tokens', 1.0),
    ('Qwen3.8-Max', 'medium', 131072, 'max_tokens', 0.6),
    ('qwen3.8-max', 'xhigh', 32000, 'max_tokens', 0.6),
])
def test_paratera_original_vendor_parameter_contract(model, effort, cap, key, temperature):
    assert provider_parameters('paratera', model=model, output_tokens=cap,
        reasoning_effort=effort) == {key: cap, 'reasoning_effort': effort, 'temperature': temperature}


@pytest.mark.parametrize('model,kwargs', [
    ('Kimi-K3', {'reasoning_effort': 'medium'}),
    ('Kimi-K3', {'temperature': 0.0}),
    ('Kimi-K3', {'output_tokens': 1048577}),
    ('Qwen3.8-Max', {'reasoning_effort': 'max'}),
    ('Qwen3.8-Max', {'output_tokens': 131073}),
    ('Qwen3.8-Max', {'thinking': False}),
])
def test_paratera_flagship_unreviewed_parameters_fail_before_send(model, kwargs):
    with pytest.raises(ValueError):
        provider_parameters('paratera', model=model, **{'output_tokens': 32000, **kwargs})


def test_flagship_parameters_do_not_change_legacy_or_go_routes():
    assert provider_parameters('paratera', model='Qwen3.8-27B', output_tokens=32000) == {
        'max_tokens': 32000, 'temperature': 0.0, 'enable_thinking': True}
    with pytest.raises(ValueError, match='does not offer'):
        provider_parameters('paratera', model='GLM-5.3-Flash', output_tokens=32000,
                            reasoning_effort='medium')
    assert provider_parameters('opencode-go', model='kimi-k3', output_tokens=32000,
        reasoning_effort='high') == {'max_tokens': 32000, 'reasoning_effort': 'high'}
    assert provider_parameters('opencode-go', model='qwen3.8-max', output_tokens=131072,
        reasoning_effort='medium') == {'max_tokens': 131072, 'output_config': {'effort': 'medium'}}


@pytest.mark.parametrize('model,effort,key', [
    ('Kimi-K3', 'high', 'max_completion_tokens'),
    ('Qwen3.8-Max', 'medium', 'max_tokens'),
])
def test_captured_paratera_thinking_and_tool_images_continue_without_duplicate_wrapper(tmp_path, model, effort, key):
    fixtures = json.loads((Path(__file__).parent / 'fixtures' / 'paratera_flagship_responses.json').read_text())
    first, second = copy.deepcopy(fixtures[model])
    # Keep the captured response shape/IDs/reasoning; use the local fixture's
    # image-returning tool so the second request exercises tool image delivery.
    first['choices'][0]['message']['tool_calls'][0]['function'] = {'name': 'view', 'arguments': '{}'}
    original_assistant = copy.deepcopy(first['choices'][0]['message'])
    assert original_assistant['provider_specific_fields'] == {
        'refusal': None, 'reasoning_content': original_assistant['reasoning_content']}
    engine = runtime(tmp_path, [], limits=RunLimits(model_calls=2, tool_calls=1,
        seconds=30, tokens=None))
    engine.model = model
    engine.parameters = provider_parameters('paratera', model=model, output_tokens=32000,
                                            reasoning_effort=effort)
    engine.versions = engine.versions.model_copy(update={'remote_model':
        engine.versions.remote_model.model_copy(update={'route_id': 'paratera', 'remote_alias': model})})
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), 'green').save(buffer, format='PNG')
    messages = copy.deepcopy(MESSAGES)
    messages[-1]['content'] = [{'type': 'text', 'text': 'Inspect the synthetic image then call view once.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode(), 'detail': 'original'}}]
    sent = []
    def server(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body[key] == 32000 and body['reasoning_effort'] == effort
        assert body['temperature'] == (1.0 if model == 'Kimi-K3' else 0.6)
        assert 'thinking_budget' not in body and 'output_config' not in body
        if len(sent) == 2:
            assistant = next(item for item in body['messages'] if item['role'] == 'assistant')
            assert assistant['reasoning_content'] == original_assistant['reasoning_content']
            assert assistant['tool_calls'] == original_assistant['tool_calls']
            assert 'provider_specific_fields' not in assistant
            tool = next(item for item in body['messages'] if item['role'] == 'tool')
            assert tool['tool_call_id'] == assistant['tool_calls'][0]['id']
        return httpx.Response(200, json=first if len(sent) == 1 else second)
    async def scenario():
        adapter = HttpChatAdapter(base_url='https://offline.example.test/v1', api_key='offline-key',
                                  transport=httpx.MockTransport(server))
        engine.adapter = adapter
        try:
            with engine.store:
                receipt = await engine.run(messages)
                assert receipt['status'] == 'completed' and receipt['model_calls'] == 2
                assert receipt['tool_calls'] == 1 and receipt['usage_complete']
                requests = [event.payload for event in engine.store.events if event.payload.event_type == 'adapter_request']
                assert [len(item.images) for item in requests] == [1, 2]
                assert all(image.original.sha256 == image.sent.sha256 for item in requests for image in item.images)
                responses = [event.payload for event in engine.store.events if event.payload.event_type == 'model_response']
                saved = engine.store.resolve(responses[0].raw_response)
                assert saved['choices'][0]['message']['provider_specific_fields'] == original_assistant['provider_specific_fields']
                engine.store.validate()
        finally:
            await adapter.close()
    asyncio.run(scenario())


def test_paratera_profile_capacity_keeps_upstream_and_endpoint_evidence_separate():
    kimi = get_model_profile('Kimi-K3', strict=True)
    qwen = get_model_profile('Qwen3.8-Max', strict=True)
    assert kimi.context_window_tokens == 1048576 and qwen.context_window_tokens == 1000000
    assert kimi.endpoint_context_window_tokens is None and qwen.endpoint_context_window_tokens is None
    assert 'input plus output' in kimi.context_source
    assert '1048576' in kimi.output_limit_source and 'Go catalog output remains131072' in kimi.output_limit_source
    assert not kimi.reasoning_may_exceed_max_tokens and kimi.reasoning_token_allowance == 0
    assert qwen.reasoning_may_exceed_max_tokens and qwen.reasoning_token_allowance == 0
    assert 'not an upper bound' in qwen.reasoning_allowance_source
