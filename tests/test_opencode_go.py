"""Go subscription isolation and real runtime protocol/tool-image round trips."""
import asyncio
import base64
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.agent_runtime.connections import resolve_connection
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.opencode_go import BASE_URL, HttpGoAdapter, conversation_id, model_contract
from src.agent_runtime.providers import provider_parameters, validate_provider_model
from src.agent_runtime.versions import run_model_configuration
from test_agent_runtime import MESSAGES, response, runtime


def private(tmp_path):
    path = tmp_path / 'go.env'
    path.write_text(f'OPENCODE_GO_BASE_URL={BASE_URL}\nOPENCODE_GO_API_KEY=private-go-key\n')
    return path


def test_go_credentials_and_public_identity_never_become_paratera(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCODE_GO_API_KEY', 'environment-key-must-not-be-used')
    connection = resolve_connection('opencode-go', private(tmp_path), model='kimi-k3')
    public = connection.descriptor.model_route('kimi-k3')
    assert public['billing_mode'] == 'subscription'
    assert public['base_url'] == 'https://opencode.ai/inference/go/openai/v1'
    assert public['adapter_kind'] == 'openai_chat_completions'
    assert 'private-go-key' not in json.dumps(public) and 'private-go-key' not in repr(connection)
    with pytest.raises(ValueError, match='explicit credentials'):
        resolve_connection('opencode-go', None, model='kimi-k3')
    path = private(tmp_path)
    path.write_text('OPENCODE_GO_BASE_URL=https://opencode.ai/inference/v1\nOPENCODE_GO_API_KEY=private-go-key\n')
    with pytest.raises(ValueError, match='reviewed Go subscription'):
        resolve_connection('opencode-go', path, model='kimi-k3')


def test_go_model_protocol_sampling_and_reasoning_fail_closed():
    for name in ('gpt-6-sol', 'deepseek-v4-pro', 'omen-alpha', 'opencode-go/kimi-k3'):
        with pytest.raises(ValueError, match='no admitted'):
            validate_provider_model('opencode-go', name)
    assert provider_parameters('opencode-go', model='kimi-k3', output_tokens=32000) == {'max_tokens':32000}
    assert provider_parameters('opencode-go', model='gpt-6-luna', output_tokens=32000,
        reasoning_effort='low') == {'max_tokens':32000, 'reasoning_effort':'low'}
    for effort in ('high', 'max'):
        assert provider_parameters('opencode-go', model='kimi-k3', output_tokens=32000,
            reasoning_effort=effort) == {'max_tokens':32000, 'reasoning_effort':effort}
    for kwargs in ({'reasoning_effort':'medium'}, {'reasoning_effort':'low'},
                   {'temperature':0}, {'thinking':False}):
        with pytest.raises(ValueError):
            provider_parameters('opencode-go', model='kimi-k3', output_tokens=32000, **kwargs)


@pytest.mark.parametrize('model', ['glm-5.3-flash','mimo-v2.6-flash','minimax-m3','gpt-6-luna'])
def test_go_transport_uses_own_agent_stable_session_exact_wire_and_no_redirect(tmp_path, model):
    async def scenario():
        connection = resolve_connection('opencode-go', private(tmp_path), model=model)
        session = conversation_id(tmp_path / 'run', 'plan-reader')
        assert session == conversation_id(tmp_path / 'run', 'plan-reader')
        assert session != conversation_id(tmp_path / 'run', 'elevation-reader')
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(307, headers={'location':'https://unapproved.invalid/chat/completions'})
        adapter = HttpGoAdapter(base_url=connection.descriptor.base_url, api_key=connection.api_key,
            protocol=connection.descriptor.adapter_kind, session_id=session, transport=httpx.MockTransport(handler))
        from types import SimpleNamespace
        wire=b'{"model":"synthetic","messages":[]}'
        with pytest.raises(ModelServiceError) as error:
            await adapter.send(SimpleNamespace(wire_bytes=wire),timeout=5)
        assert 'private-go-key' not in str(error.value)
        assert len(seen)==1 and seen[0].content==wire
        assert seen[0].headers['x-opencode-session']==session
        assert seen[0].headers['user-agent']=='Sim-BIM-Agent/work-model'
        assert seen[0].headers['authorization']=='Bearer private-go-key'
        expected={'glm-5.3-flash':'/openai/v1/chat/completions',
                  'mimo-v2.6-flash':'/openai/v1/chat/completions',
                  'minimax-m3':'/anthropic/v1/messages',
                  'gpt-6-luna':'/openai/v1/responses'}[model]
        assert str(seen[0].url)=='https://opencode.ai/inference/go'+expected
        assert str(seen[0].url).startswith(connection.descriptor.base_url + '/')
        assert 'anthropic-beta' not in seen[0].headers
        if model=='minimax-m3':
            assert seen[0].headers['anthropic-version']=='2023-06-01'
        await adapter.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('base_url,protocol', [
    (BASE_URL, 'openai_chat_completions'),
    ('https://opencode.ai/inference/go/openai/v1', 'anthropic_messages'),
    ('https://opencode.ai/inference/go/anthropic/v1', 'openai_responses'),
    ('https://opencode.ai/inference/openai/v1', 'openai_chat_completions'),
])
def test_go_rejects_catalog_cross_protocol_and_non_subscription_transport(base_url, protocol):
    with pytest.raises(ValueError, match='unreviewed OpenCode Go transport'):
        HttpGoAdapter(base_url=base_url, api_key='offline-test-key', protocol=protocol,
                      session_id='cb6130de-46b0-4f0c-9baa-cb8fc04892d9')


def native_response(model, *, tool=False):
    if model == 'minimax-m3':
        return {'id':'msg-test', 'type':'message','role':'assistant','model':model,
            'content':[{'type':'tool_use','id':'view-1','name':'view','input':{}}] if tool else [{'type':'text','text':'OK'}],
            'stop_reason':'tool_use' if tool else 'end_turn','usage':{'input_tokens':40,'output_tokens':11}}
    return {'id':'resp-test','object':'response','status':'completed','model':model,
        'output':[{'id':'reason-test','type':'reasoning','summary':[],'encrypted_content':'opaque-replay'},
                  {'id':'fc-test','type':'function_call','call_id':'view-1','name':'view','arguments':'{}'}] if tool else
                 [{'id':'message-test','type':'message','role':'assistant','content':[{'type':'output_text','text':'OK'}]}],
        'usage':{'input_tokens':40,'output_tokens':11,'total_tokens':51}}


def kimi_go_response(*, tool=False):
    # Shape and reasoning fields from the two 2026-10-21 Go/high preflight
    # captures. Only the first message's tool call is synthetic for this loop.
    raw = response(('view-1', 'view', {})) if tool else response(text='OK')
    text = ('The user wants exactly OK. Final should be exactly OK.' if tool else
            'The user wants exact reply OK. Need final exactly OK no extra. Ensure no punctuation. ')
    raw.update(model='kimi-k3', object='chat.completion')
    raw['choices'][0]['message'].update(refusal=None, reasoning=text,
        reasoning_details=[{'type':'reasoning.text', 'text':text, 'format':'unknown', 'index':0}])
    raw['usage'] = {'prompt_tokens':90, 'completion_tokens':26 if tool else 33,
                    'total_tokens':116 if tool else 123,
                    'completion_tokens_details':{'reasoning_tokens':15 if tool else 19}}
    return raw


@pytest.mark.parametrize('model', ['glm-5.3-flash','mimo-v2.6-flash','kimi-k3','minimax-m3','gpt-6-luna'])
def test_go_real_runtime_preserves_tool_images_native_history_usage_and_subscription(tmp_path, model):
    chat_model = model in {'glm-5.3-flash', 'mimo-v2.6-flash', 'kimi-k3'}
    replies=[response(('view-1','view',{}),reasoning=True),response(text='OK',reasoning=True)] if chat_model else [native_response(model,tool=True),native_response(model)]
    if model == 'kimi-k3':
        replies = [kimi_go_response(tool=True), kimi_go_response()]
    engine=runtime(tmp_path,replies,limits=RunLimits(model_calls=2,tool_calls=4,seconds=30,tokens=200000))
    engine.model=model
    engine.parameters=provider_parameters('opencode-go',model=model,output_tokens=32000,
        reasoning_effort='high' if model == 'kimi-k3' else None)
    engine.strict_model_profile=True
    engine.versions=engine.versions.model_copy(update={'remote_model':engine.versions.remote_model.model_copy(
        update={'route_id':'opencode-go','remote_alias':model})})
    call_tool = engine.tools.call_tool
    async def hinted_tool(name, arguments):
        raw = await call_tool(name, arguments)
        for part in raw.get('content', []):
            if part.get('type') == 'image':
                part['_meta'] = {'codex/imageDetail': 'original'}
        return raw
    engine.tools.call_tool = hinted_tool
    messages = copy.deepcopy(MESSAGES)
    if model == 'mimo-v2.6-flash':
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), (0, 255, 0)).save(stream, format='PNG')
        messages[-1]['content'] = [{'type': 'text', 'text': messages[-1]['content']},
            {'type': 'image_url', 'image_url': {'detail': 'original',
             'url': 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()}}]
    original_messages = copy.deepcopy(messages)
    with engine.store:
        result=asyncio.run(engine.run(messages))
        assert messages == original_messages
        assert result['status']=='completed' and result['model_calls']==2
        assert result['usage_accounting']['billing_modes']==['subscription']
        assert result['billing_usd'] is None and result['estimated_cost_cny'] is None
        profile=engine._config()['model_profile']
        assert profile['text_safety_factor']==2.0 and 'unverified_endpoint' in profile['profile_status']
        assert profile['context_window_tokens']=={'gpt-6-luna':1050000,'mimo-v2.6-flash':1048576,
            'kimi-k3':1048576}.get(model,1000000)
        requests=[event.payload for event in engine.store.events if event.payload.event_type=='adapter_request']
        assert len(requests[1].images)==(2 if model == 'mimo-v2.6-flash' else 1)
        execution = next(e.payload for e in engine.store.events if e.payload.event_type == 'tool_execution')
        presentation = next(e.payload for e in engine.store.events if e.payload.event_type == 'tool_presentation')
        raw = engine.store.resolve(execution.raw_result)
        assert raw['content'][0]['_meta']['codex/imageDetail'] == 'original'
        shown = engine.store.resolve(execution.shown_result)
        if model == 'mimo-v2.6-flash':
            assert presentation.shown_result == execution.shown_result
            assert shown['omitted_image_display_hints'] == [{'image_index': 0, 'detail': 'original'}]
            assert all('detail' not in block['image_url'] for block in shown['image_blocks']
                       if block.get('type') == 'image_url')
        assert shown['images'][0]['sha256'] == requests[1].images[-1].sent.sha256
        for wire,event in zip(engine.adapter.requests,requests):
            assert wire==engine.store.capture_bytes(event.final_request_body)
            if model == 'kimi-k3':
                assert json.loads(wire)['reasoning_effort'] == 'high'
            body=json.loads(wire)
            assert body['model']==model and body['stream'] is False
            if chat_model:
                assert 'n' not in body and 'enable_thinking' not in body
                output_key = 'max_completion_tokens' if model == 'mimo-v2.6-flash' else 'max_tokens'
                assert body[output_key]==32000 and event.parameters.requested[output_key]==32000
                assert ('max_tokens' in body) == (model != 'mimo-v2.6-flash')
                if model == 'mimo-v2.6-flash':
                    for message in body['messages']:
                        content = message.get('content')
                        for block in content if isinstance(content, list) else []:
                            if block.get('type') == 'image_url':
                                assert set(block['image_url']) == {'url'}
                    if event.images:
                        assert any(item.source.source_id == 'go-mimo-image-projection'
                                   for item in event.injected_content)
            elif model=='minimax-m3':
                assert body['max_tokens']==32000 and 'context_management' not in body
                assert all(message['role'] in {'user','assistant'} for message in body['messages'])
            else:
                assert body['max_output_tokens']==32000 and body['store'] is False
                assert all(tool['type']=='function' for tool in body['tools'])
        if model=='gpt-6-luna':
            second=json.loads(engine.adapter.requests[1])
            assert any(item.get('encrypted_content')=='opaque-replay' for item in second['input'])
            assert any(item.get('type')=='function_call_output' for item in second['input'])
        if model=='kimi-k3':
            second=json.loads(engine.adapter.requests[1])
            assistant=next(message for message in second['messages'] if message['role']=='assistant')
            observed = replies[0]['choices'][0]['message']
            for field in ('reasoning', 'reasoning_details'):
                assert assistant[field] == observed[field]
            assert 'reasoning_content' not in assistant
            assert assistant['tool_calls'][0]['id']=='view-1'
        engine.store.validate()


@pytest.mark.parametrize('policy', ['all', 'current_tool_chain'])
def test_go_kimi_real_shape_survives_image_tool_resume_and_history_selection(tmp_path, policy):
    class Crash(BaseException):
        pass

    def configured(replies, tools=None):
        engine = runtime(tmp_path, replies, tools=tools,
            limits=RunLimits(model_calls=2, tool_calls=4, seconds=60, tokens=200000))
        engine.model = 'kimi-k3'
        engine.parameters = provider_parameters('opencode-go', model=engine.model,
            output_tokens=32000, reasoning_effort='high')
        engine.versions = engine.versions.model_copy(update={'remote_model':
            engine.versions.remote_model.model_copy(update={'route_id':'opencode-go',
                                                         'remote_alias':engine.model})})
        engine.reasoning_history = policy
        return engine

    tool_response = kimi_go_response(tool=True)
    older = kimi_go_response()['choices'][0]['message']
    older['content'] = 'Previous complete answer'
    messages = copy.deepcopy(MESSAGES) + [copy.deepcopy(older),
                {'role':'user', 'content':'Inspect the tool image and finish.'}]
    engine = configured([tool_response])
    fields = ['reasoning_content', 'reasoning', 'reasoning_details']
    assert engine._config()['echo_fields'] == fields  # Also valid before run().
    def interrupt(name, current):
        if name == 'after_response' and current.counts['model_calls'] == 1:
            raise Crash()
    engine.fault_hook = interrupt
    with engine.store, pytest.raises(Crash):
        asyncio.run(engine.run(messages))
    resumed = configured([kimi_go_response()], tools=engine.tools)
    assert resumed._config()['echo_fields'] == fields
    with resumed.store:
        result = asyncio.run(resumed.run(messages, resume=True))
        assert result['status'] == 'completed'
        assert result['model_calls'] == 2 and result['tool_calls'] == 1
        assert resumed.tools.calls == [('view', {})]
        wire = json.loads(resumed.adapter.requests[0])
        assistants = [m for m in wire['messages'] if m['role'] == 'assistant']
        assert len(assistants) == 2
        for field in ('reasoning', 'reasoning_details'):
            assert assistants[-1][field] == tool_response['choices'][0]['message'][field]
            if policy == 'all':
                assert assistants[0][field] == older[field]
            else:
                assert field not in assistants[0]
        assert assistants[0]['content'] == 'Previous complete answer'
        assert assistants[-1]['tool_calls'][0]['id'] == 'view-1'
        assert any(m.get('tool_call_id') == 'view-1' for m in wire['messages'])
        request = next(e.payload for e in reversed(resumed.store.events)
                       if e.payload.event_type == 'adapter_request')
        assert len(request.images) == 1
        # Filtering only affects wire history, never the preserved audit record.
        original = next(m for m in resumed.messages if m.get('content') == 'Previous complete answer')
        assert original['reasoning_details'] == older['reasoning_details']
        resumed.store.validate()


@pytest.mark.parametrize('route,model', [('opencode-go','glm-5.3-flash'), ('paratera','kimi-k3')])
def test_go_kimi_native_echo_does_not_expand_other_routes(tmp_path, route, model):
    from src.agent_runtime.adapter import parse_response, reasoning_history_messages
    engine = runtime(tmp_path, [])
    engine.model = model
    engine.versions = engine.versions.model_copy(update={'remote_model':
        engine.versions.remote_model.model_copy(update={'route_id':route, 'remote_alias':model})})
    assert engine._effective_echo_fields() == ('reasoning_content',)
    with engine.store:
        parsed = parse_response(kimi_go_response(), 'request-test', engine.store,
                                echo_fields=engine._effective_echo_fields())
        assert 'reasoning' not in parsed.assistant_message
        assert 'reasoning_details' not in parsed.assistant_message
    # Existing generic policy still only strips its original pair of fields.
    older = kimi_go_response()['choices'][0]['message']
    selected = reasoning_history_messages([older, {'role':'user','content':'next'}], 'current_tool_chain')
    assert selected[0]['reasoning_details'] == older['reasoning_details']


def test_go_mixed_role_configuration_preserves_selected_models_and_concurrency(tmp_path):
    from src.agent.runtime_configuration import argv_for, load_configuration
    from test_role_configuration import case, configuration, role
    selected=case()
    selected.update(provider='opencode-go',model='glm-5.3-flash',reasoning_effort=None,
                    max_concurrent_readers=3,credentials_file=str(private(tmp_path)))
    selected['roles']={name:role('opencode-go',model,None,32000) for name,model in
        [('coordinator','glm-5.3-flash'),('plan_reader','kimi-k3'),('elevation_reader','minimax-m3')]}
    path=tmp_path/'run.json'
    path.write_text(json.dumps(configuration(selected)))
    loaded=load_configuration(path)['cases'][0]
    command=argv_for(loaded)
    assert command[command.index('--max-concurrent-readers')+1]=='3'
    routes=json.loads(command[command.index('--roles-json')+1])
    assert routes['plan_reader']['model']=='kimi-k3' and routes['elevation_reader']['model']=='minimax-m3'
    assert model_contract(routes['plan_reader']['model'])[0]!=model_contract(routes['elevation_reader']['model'])[0]


def test_go_messages_lifts_later_system_context_without_glm_beta_extension():
    from src.agent_runtime.anthropic import convert_messages
    from src.agent_runtime.opencode_go import estimate_messages
    messages=[{'role':'system','content':'Initial guide'}, {'role':'user','content':'Original task'},
              {'role':'assistant','content':'Earlier reply'}, {'role':'system','content':'Current runtime context'},
              {'role':'user','content':'Continue'}]
    sources=list(range(len(messages)))
    system, origins, native, native_origins=convert_messages(messages,sources,native_system_only=True)
    assert [block['text'] for block in system]==['Initial guide','Current runtime context']
    assert origins==[0,3] and all(row['role'] in {'user','assistant'} for row in native)
    estimate=estimate_messages(model='minimax-m3',messages=messages,tools=[],parameters={'max_tokens':32000})
    assert estimate.context_window_tokens==1000000 and estimate.reservation_tokens>32000


@pytest.mark.parametrize('change', ['money', 'opaque_history'])
def test_go_direct_runtime_rejects_unreviewed_budget_or_history_before_send(tmp_path, change):
    from decimal import Decimal
    engine=runtime(tmp_path,[response(text='must not send')])
    engine.model='gpt-6-luna'
    engine.parameters=provider_parameters('opencode-go',model=engine.model,output_tokens=32000)
    engine.versions=engine.versions.model_copy(update={'remote_model':engine.versions.remote_model.model_copy(
        update={'route_id':'opencode-go','remote_alias':engine.model})})
    if change=='money':
        engine.limits=engine.limits.model_copy(update={'money_usd':Decimal('1')})
    else:
        engine.reasoning_history='current_tool_chain'
    with engine.store, pytest.raises(ValueError):
        asyncio.run(engine.run(MESSAGES))
    assert not engine.adapter.requests


def test_go_external_coordinator_is_rejected_before_credentials(tmp_path):
    from src.agent.runtime_configuration import load_configuration
    from test_role_configuration import case, configuration
    selected=case('external_coordinator_mcp')
    selected.update(provider='opencode-go',model='kimi-k3',reasoning_effort=None,
                    credentials_file='must-not-read.env')
    path=tmp_path/'external.json'
    path.write_text(json.dumps(configuration(selected)))
    with pytest.raises(ValueError,match='single_model and role_division only'):
        load_configuration(path)


@pytest.mark.parametrize('mixed_providers', [False, True])
def test_role_version_records_use_each_models_own_parameter_contract(mixed_providers):
    configured = {
        'coordinator': {'provider': 'opencode-go', 'model': 'glm-5.3-flash',
                        'output_tokens': 32000, 'reasoning_effort': 'high', 'temperature': .3},
        'plan_reader': {'route_id': 'opencode-go', 'model': 'claude-haiku-5-5',
                        'output_tokens': 40000, 'reasoning_effort': 'medium', 'enable_thinking': None},
        'elevation_reader': {'provider': 'opencode-go', 'model': 'gpt-6-luna',
                             'output_tokens': 48000, 'reasoning_effort': 'low'},
    }
    expected = {
        'coordinator': {'max_tokens': 32000, 'reasoning_effort': 'high', 'temperature': .3},
        'plan_reader': {'max_tokens': 40000, 'output_config': {'effort': 'medium'}},
        'elevation_reader': {'max_tokens': 48000, 'reasoning_effort': 'low'},
    }
    if mixed_providers:
        configured['coordinator'] = {'provider': 'chatgpt-subscription', 'model': 'gpt-6-sol',
                                     'output_tokens': 32000, 'reasoning_effort': 'high'}
        configured['elevation_reader'] = {'provider': 'glm-subscription', 'model': 'glm-5.3-flash',
                                          'output_tokens': 48000, 'reasoning_effort': 'max'}
        expected['coordinator'] = {'max_tokens': 32000, 'reasoning_effort': 'high'}
        expected['elevation_reader'] = {'max_tokens': 48000, 'reasoning_effort': 'max'}
    route = {'route_id': configured['coordinator']['provider'],
             'model': configured['coordinator']['model'], 'roles': {'roles': configured}}
    original = copy.deepcopy(route)
    mode, recorded = run_model_configuration(SimpleNamespace(is_root_task=True),
        route=route, parameters=expected['coordinator'])
    assert mode == 'role_division' and set(recorded) == set(configured)
    for name, config in configured.items():
        assert recorded[name] == {
            'route_id': config.get('route_id', config.get('provider')), 'model': config['model'],
            'reasoning_effort': config['reasoning_effort'], 'output_tokens': config['output_tokens'],
            'parameters': expected[name],
        }
    assert route == original


@pytest.mark.parametrize('provider, model, options, expected', [
    ('chatgpt-subscription', 'gpt-6-sol', {'reasoning_effort': 'high'},
     {'max_tokens': 32000, 'reasoning_effort': 'high'}),
    ('glm-subscription', 'glm-5.3-flash', {'reasoning_effort': 'medium', 'temperature': .2},
     {'max_tokens': 32000, 'reasoning_effort': 'medium', 'temperature': .2}),
    ('glm-subscription-anthropic', 'glm-5.3-flash', {'reasoning_effort': 'low'},
     {'max_tokens': 32000, 'thinking': {'type': 'adaptive', 'display': 'omitted'},
      'output_config': {'effort': 'low'},
      'context_management': {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]}}),
    ('paratera', 'Qwen3.8-27B', {'temperature': .7, 'enable_thinking': False},
     {'max_tokens': 32000, 'temperature': .7, 'enable_thinking': False}),
])
def test_role_version_records_preserve_non_go_parameters(provider, model, options, expected):
    config = {'provider': provider, 'model': model, 'output_tokens': 32000, **options}
    mode, recorded = run_model_configuration(SimpleNamespace(is_root_task=True),
        route={'route_id': provider, 'model': model, 'roles': {'roles': {'plan_reader': config}}},
        parameters=expected)
    assert mode == 'role_division'
    assert recorded['plan_reader'] == {'route_id': provider, 'model': model,
        'output_tokens': 32000, 'reasoning_effort': options.get('reasoning_effort'), 'parameters': expected}


@pytest.fixture
def reader_request(tmp_path):
    from src.agent.runtime_roles.readers import ReaderTools
    from src.agent.runtime_roles.trial import PlanTrial
    from tests.test_role_readers import Frozen
    frozen = Frozen()
    reader = ReaderTools(frozen, role_id='plan_reader', image_name='plan.png',
                         trial=PlanTrial(frozen, image_name='plan.png'))
    catalog = asyncio.run(reader.list_tools())
    tools = [{'type': 'function', 'function': {'name': row['name'],
              'description': row['description'], 'parameters': copy.deepcopy(row['inputSchema'])}}
             for row in catalog]
    engine = runtime(tmp_path, [])
    with engine.store as store:
        source = store.source('messages', MESSAGES)
        yield reader, dict(store=store, model='claude-haiku-5-5', messages=MESSAGES,
            message_sources=[source] * len(MESSAGES), tools=tools,
            tool_source=store.source('reader-tools', tools), parameters={'max_tokens': 32000},
            versions=engine.versions)


def test_go_messages_projects_reader_presence_rules_only_on_wire(reader_request, monkeypatch):
    from src.agent_runtime import opencode_go
    reader, arguments = reader_request
    original = copy.deepcopy(arguments['tools'])
    prepared = opencode_go.prepare_request(**arguments)
    schemas = {row['name']: row['input_schema'] for row in prepared.body['tools']}
    assert 'oneOf' not in schemas['trial_plan_bim']
    assert 'anyOf' not in schemas['submit_plan_reading']
    assert 'oneOf' in schemas['trial_plan_bim']['properties']['operations']['items']
    for tool in original:
        function = tool['function']
        expected = copy.deepcopy(function['parameters'])
        if function['name'] == 'trial_plan_bim':
            expected.pop('oneOf')
        elif function['name'] == 'submit_plan_reading':
            expected.pop('anyOf')
        assert schemas[function['name']] == expected
    assert arguments['tools'] == original
    store = arguments['store']
    assert store.capture_bytes(prepared.event_payload.final_request_body) == prepared.wire_bytes
    injection = next(row for row in prepared.event_payload.injected_content if row.request_location == '/tools')
    assert store.resolve(injection.content) == prepared.body['tools']
    assert json.loads(store.get_bytes(injection.source.blob)) == original
    estimated = []
    estimate = opencode_go.estimate_chat_request
    def capture(body, **kwargs):
        estimated.append(body)
        return estimate(body, **kwargs)
    monkeypatch.setattr(opencode_go, 'estimate_chat_request', capture)
    opencode_go.estimate_messages(model=arguments['model'], messages=MESSAGES,
        tools=arguments['tools'], parameters=arguments['parameters'])
    assert estimated[0]['tools'] == prepared.body['tools']
    # These remain execution constraints, even when not expressible on the wire.
    for invalid in ({}, {'plan': {}, 'operations': []}):
        with pytest.raises(ValueError, match='exactly one'):
            asyncio.run(reader.trial.run(**invalid))
    with pytest.raises(ValueError, match='Minimum correct example'):
        reader.submission.submit({'local_review': {'summary': 'Offline check'}})
    assert 'oneOf' in reader._catalog['trial_plan_bim']['inputSchema']
    assert 'anyOf' in reader._catalog['submit_plan_reading']['inputSchema']


@pytest.mark.parametrize('route', ['go-chat', 'go-responses', 'non-go-messages'])
def test_messages_schema_projection_does_not_change_other_routes(reader_request, route):
    from src.agent_runtime.opencode_go import prepare_request
    from src.agent_runtime.anthropic import prepare_anthropic_request
    _, arguments = reader_request
    original = copy.deepcopy(arguments['tools'])
    if route == 'non-go-messages':
        arguments['model'] = 'glm-5.3-flash'
        prepared = prepare_anthropic_request(**arguments)
        schemas = {row['name']: row['input_schema'] for row in prepared.body['tools']}
    else:
        arguments['model'] = 'glm-5.3-flash' if route == 'go-chat' else 'gpt-6-luna'
        prepared = prepare_request(**arguments)
        rows = ([row['function'] for row in prepared.body['tools']]
                if route == 'go-chat' else prepared.body['tools'])
        schemas = {row['name']: row['parameters'] for row in rows}
    assert schemas == {row['function']['name']: row['function']['parameters'] for row in original}
    assert arguments['tools'] == original


@pytest.mark.parametrize('constraint', [
    {'allOf': [{'required': ['a']}, {'required': ['b']}]},
    {'oneOf': [{'required': ['a'], 'properties': {'a': {'const': 1}}}, {'required': ['b']}]},
    {'anyOf': [{'required': ['a']}, {'required': ['missing']}]},
    {'oneOf': [{'required': ['a']}, {'required': ['a']}]},
])
def test_go_messages_leaves_unreviewed_combinations_unchanged(constraint):
    from src.agent_runtime.opencode_go import _messages_tools
    tools = [{'type': 'function', 'function': {'name': 'synthetic_tool', 'parameters': {
        'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
        **constraint}}}]
    assert _messages_tools(tools) == tools
