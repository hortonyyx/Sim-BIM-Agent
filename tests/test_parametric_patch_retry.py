"""Unapplied batches support explicit local edits without duplicating geometry writes."""
import copy
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import threading

import pytest

from scripts.tool_scripts import bim_agent_parametric as subject
from tests.test_bim_agent_tools import (
    _ParametricFunctionToolkit, _parametric_file_hashes, _parametric_function_op as op,
    _parametric_function_plan,
)


def setup(tmp_path, count=43):
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    first = subject.build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    operations = [op('add', '/assumptions/-', f'explicit item {i}') for i in range(count - 1)]
    operations.append(op('replace', '/connections', []))
    args = {'candidate': first['candidate'], 'expected_sha': first['parametric']['plan_sha256']}
    before = _parametric_file_hashes(toolkit.run)
    failed = subject.patch_parametric(toolkit, json.dumps(operations), **args)
    assert failed['failed_batch']['status'] == 'rejected_unapplied'
    assert not failed['save_effects']['geometry_applied']
    assert len(toolkit.calls) == 1
    after = _parametric_file_hashes(toolkit.run)
    assert all(after[name] == sha for name, sha in before.items())
    assert set(after) - set(before) == {'parametric_patch_failures/failed_patch_001.json'}
    request = {'retry_failed_batch': {key: failed['failed_batch'][key] for key in ('id', 'sha256')},
               'edits': [{'op': 'replace', 'index': count - 1, 'operation': op('add', '/connections', [])}]}
    return toolkit, first, args, operations, request


def invoke(toolkit, args, request):
    return subject.patch_parametric(toolkit, json.dumps(request), **args)


def test_parametric_retry_retains_all_43_operations_and_identical_repeat_reads_receipt(tmp_path):
    toolkit, first, args, operations, request = setup(tmp_path)
    result = invoke(toolkit, args, request)
    assert result['saved_candidate'] == 'candidate_02'
    assert result['retry_receipt']['status'] == 'completed'
    value = subject.inspect_parametric(toolkit, result['candidate'], full=True)['value']
    assert value['connections'] == []
    assert value['assumptions'] == ['test'] + [row['value'] for row in operations[:-1]]
    audit = json.loads((toolkit.run / result['parametric']['patch']['file']).read_bytes())
    assert audit['operations'][:-1] == operations[:-1]
    assert audit['retry']['failed_batch'] == request['retry_failed_batch']
    assert len(json.dumps(request)) < len(json.dumps(operations)) / 5
    before = _parametric_file_hashes(toolkit.run)
    reused = invoke(toolkit, args, request)
    assert reused['candidate'] == result['candidate'] and reused['retry_receipt']['status'] == 'reused'
    assert not reused['save_effects']['geometry_applied']
    assert not reused['save_effects']['created_candidates']
    assert len(toolkit.calls) == 2 and _parametric_file_hashes(toolkit.run) == before
    # An intervening candidate is not an expiry rule for the original bound base.
    subject.build_parametric(toolkit, json.dumps(_parametric_function_plan()))
    assert invoke(toolkit, args, request)['retry_receipt']['status'] == 'reused'


def test_parametric_retry_explicit_remove_and_insert_use_original_indices(tmp_path):
    toolkit, first, args, operations, request = setup(tmp_path, 19)
    request['edits'] += [{'op': 'remove', 'index': i} for i in range(11)]
    result = invoke(toolkit, args, request)
    audit = json.loads((toolkit.run / result['parametric']['patch']['file']).read_bytes())
    assert len(audit['operations']) == 8
    assert audit['operations'][:-1] == operations[11:-1]
    # A separate failed batch permits insert-before and append while preserving originals.
    failed = subject.patch_parametric(toolkit, json.dumps(operations), **args)
    request['retry_failed_batch'] = {key: failed['failed_batch'][key] for key in ('id', 'sha256')}
    request['edits'] = [request['edits'][0],
        {'op': 'insert', 'index': 0, 'operation': op('add', '/assumptions/-', 'before')},
        {'op': 'insert', 'index': 19, 'operation': op('add', '/assumptions/-', 'after')}]
    result = invoke(toolkit, args, request)
    value = subject.inspect_parametric(toolkit, result['candidate'], full=True)['value']
    assert value['assumptions'] == ['test', 'before'] + [row['value'] for row in operations[:-1]] + ['after']


@pytest.mark.parametrize('change', ['unknown', 'hash', 'tamper', 'digest', 'base', 'run', 'source',
                                    'duplicate', 'bool', 'range', 'extra', 'empty', 'invalid_operation'])
def test_parametric_retry_rejects_unbound_or_ambiguous_requests_without_geometry(tmp_path, change):
    toolkit, first, args, operations, request = setup(tmp_path)
    batch = toolkit.run / 'parametric_patch_failures/failed_patch_001.json'
    if change == 'unknown': request['retry_failed_batch']['id'] = 'failed_patch_999'
    elif change == 'hash': request['retry_failed_batch']['sha256'] = '0' * 64
    elif change == 'tamper': batch.write_bytes(batch.read_bytes() + b' ')
    elif change in {'digest', 'base', 'run'}:
        record = json.loads(batch.read_bytes())
        if change == 'digest': record['operations'][0]['value'] = 'tampered'
        elif change == 'base': record['base']['candidate'] = 'candidate_999'
        else: record['run_binding'] = '0' * 64
        batch.write_text(json.dumps(record), encoding='utf-8')
        request['retry_failed_batch']['sha256'] = hashlib.sha256(batch.read_bytes()).hexdigest()
    elif change == 'source':
        source = toolkit.run / first['candidate'] / 'source_model.json'
        source.write_bytes(source.read_bytes() + b' ')
    elif change == 'duplicate': request['edits'] *= 2
    elif change == 'bool': request['edits'][0]['index'] = True
    elif change == 'range': request['edits'][0]['index'] = 43
    elif change == 'extra': request['edits'][0]['guess'] = True
    elif change == 'empty': request['edits'] = []
    else: request['edits'][0]['operation'].pop('reason')
    before = _parametric_file_hashes(toolkit.run)
    result = invoke(toolkit, args, request)
    assert result['error'] and result['saved_candidate'] is None
    assert len(toolkit.calls) == 1
    after = _parametric_file_hashes(toolkit.run)
    assert all(after[name] == sha for name, sha in before.items())
    assert all(name.startswith('parametric_patch_failures/') for name in after.keys() - before.keys())


def test_parametric_retry_copied_run_is_rejected(tmp_path):
    toolkit, first, args, operations, request = setup(tmp_path)
    other = copy.copy(toolkit)
    other.run = tmp_path / 'other'
    shutil.copytree(toolkit.run, other.run)
    assert 'this run' in invoke(other, args, request)['error']
    assert len(toolkit.calls) == 1


@pytest.mark.parametrize('point', ['before_build', 'after_build'])
def test_parametric_retry_interruption_never_repeats_write_and_exposes_saved_draft(tmp_path, monkeypatch, point):
    toolkit, first, args, operations, request = setup(tmp_path)
    if point == 'before_build':
        original = subject._build
        def interrupted(*args, **kwargs):
            raise OSError('simulated interruption after claim')
        monkeypatch.setattr(subject, '_build', interrupted)
    else:
        original = subject._write
        def interrupted(path, value):
            if path.name.endswith('.claim.result.json'):
                raise OSError('simulated interruption before receipt')
            return original(path, value)
        monkeypatch.setattr(subject, '_write', interrupted)
    with pytest.raises(OSError, match='simulated interruption'):
        invoke(toolkit, args, request)
    monkeypatch.setattr(subject, '_build' if point == 'before_build' else '_write', original)
    before = _parametric_file_hashes(toolkit.run)
    resumed = copy.copy(toolkit)
    result = invoke(resumed, args, request)
    assert result['error_stage'] == 'parametric_retry_pending'
    assert result['retry_receipt']['write_repeated'] is False
    assert len(toolkit.calls) == (1 if point == 'before_build' else 2)
    assert _parametric_file_hashes(toolkit.run) == before
    state = result['retry_receipt']['saved_state']
    assert (state is None) == (point == 'before_build')
    if state:
        assert state['candidate'] == 'candidate_02'
        assert result['edit_route']['patch_arguments']['draft'] == state['draft']
    assert toolkit.logs[-1][1]['retry_receipt'] == result['retry_receipt']


@pytest.mark.parametrize('change', ['edits', 'receipt', 'candidate', 'patch'])
def test_parametric_retry_completed_attempt_rejects_divergence(tmp_path, change):
    toolkit, first, args, operations, request = setup(tmp_path)
    saved = invoke(toolkit, args, request)
    if change == 'edits': request['edits'][0]['operation']['reason'] = 'different request'
    else:
        path = (toolkit.run / 'parametric_patch_failures/failed_patch_001.claim.result.json' if change == 'receipt'
                else toolkit.run / saved['candidate'] / 'source_model.json' if change == 'candidate'
                else toolkit.run / saved['parametric']['patch']['file'])
        if change == 'receipt':
            data = json.loads(path.read_bytes())
            data['result']['candidate'] = 'candidate_99'
            path.write_text(json.dumps(data), encoding='utf-8')
        else: path.write_bytes(path.read_bytes() + b' ')
    before = _parametric_file_hashes(toolkit.run)
    result = invoke(toolkit, args, request)
    assert result['error'] and len(toolkit.calls) == 2
    assert _parametric_file_hashes(toolkit.run) == before


def test_parametric_retry_expansion_refusal_uses_new_draft_and_cached_result(tmp_path):
    toolkit, first, args, operations, request = setup(tmp_path)
    request['edits'][0]['operation'] = op('remove', '/unresolved')
    result = invoke(toolkit, args, request)
    assert result['error_stage'] == 'parametric_expansion'
    assert 'failed_batch' not in result
    assert result['draft'] != first['draft']
    assert result['edit_route']['patch_arguments']['draft'] == result['draft']
    before = _parametric_file_hashes(toolkit.run)
    assert invoke(toolkit, args, request)['retry_receipt']['status'] == 'reused'
    assert _parametric_file_hashes(toolkit.run) == before
    repaired = subject.patch_parametric(toolkit, json.dumps([op('add', '/unresolved', [])]),
        draft=result['draft'], expected_sha=result['plan_sha256'])
    assert repaired['saved_candidate'] == 'candidate_02'


def test_parametric_preflight_feedback_preserves_ring_rejection_and_independent_fields(tmp_path):
    from tests.test_parametric_validation import plan
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    toolkit.manifest = {'architectural_modelling_policy': {'schema_version': '1'}}
    value = plan()
    template = value['templates']['main']
    template['footprint']['vertices'] = [[0, 0], [4, 4], [4, 0], [0, 4]]
    del template['opening_rows'][0]['kind']
    del template['spaces'][0]['role_evidence']['role']
    result = subject.build_parametric(toolkit, json.dumps(value))
    assert result['error_stage'] == 'parametric_expansion'
    assert result['parametric']['regularization_failure']['rejections']
    assert {row['pointer'] for row in result['error_details']['field_diagnostics']} == {
        '/templates/main/opening_rows/0/kind', '/templates/main/spaces/0/role_evidence/role'}
    assert toolkit.calls == [] and json.loads((toolkit.run / result['draft']).read_bytes()) == value


@pytest.mark.parametrize('error', [ValueError, TypeError, KeyError])
def test_parametric_preflight_failure_never_overrides_formal_pipeline(tmp_path, monkeypatch, error):
    from src.agent.geometry import parametric_validation
    def unavailable(plan):
        raise error('synthetic advisory failure')
    monkeypatch.setattr(parametric_validation, 'parametric_field_diagnostics', unavailable)
    toolkit = _ParametricFunctionToolkit(tmp_path / 'run')
    value = _parametric_function_plan()
    assert subject.build_parametric(toolkit, json.dumps(value))['source_geometry_ready']
    del value['unresolved']
    refused = subject.build_parametric(toolkit, json.dumps(value))
    assert refused['error_stage'] == 'parametric_expansion'
    assert 'unresolved' in refused['error']
    assert 'synthetic advisory failure' in refused['error_details']['field_diagnostics_unavailable']


def test_parametric_retry_concurrent_admission_never_overwrites_patch_audit_or_builds_twice(tmp_path, monkeypatch):
    toolkit, first, args, operations, request = setup(tmp_path)
    original = subject._new_path
    barrier, lock, selected = threading.Barrier(2), threading.Lock(), []
    def choose(*args, **kwargs):
        path = original(*args, **kwargs)
        if args[1] == 'parametric_patches':
            with lock:
                selected.append(path)
                wait = len(selected) <= 2
            if wait:
                barrier.wait(timeout=10)
        return path
    monkeypatch.setattr(subject, '_new_path', choose)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(toolkit, args, request), range(2)))
    assert selected[0] == selected[1]  # Force collision on the original free audit filename.
    assert len(toolkit.calls) == 2
    audits = list((toolkit.run / 'parametric_patches').glob('patch_*.json'))
    assert len(audits) == 2
    for path in audits:
        assert json.loads(path.read_bytes())['retry']['edits'] == request['edits']
    final = invoke(toolkit, args, request)
    assert final['saved_candidate'] == 'candidate_02' and final['retry_receipt']['status'] == 'reused'
    assert any(row.get('saved_candidate') == 'candidate_02' for row in results)


def test_parametric_retry_formal_mcp_rejects_side_effect_options_before_claim_then_reuses_result(tmp_path):
    from tests.test_bim_agent_tools import _run_with_one_image, _server_session, _json_result
    run = _run_with_one_image(tmp_path)
    async def scenario():
        async with _server_session(run, readonly=False) as session:
            first = _json_result(await session.call_tool('build_parametric_bim', {'plan_json': json.dumps(_parametric_function_plan())}))
            args = {'candidate': first['candidate'], 'expected_plan_sha256': first['parametric']['plan_sha256']}
            failed = _json_result(await session.call_tool('patch_parametric_bim', {**args,
                'operations_json': json.dumps([op('replace', '/connections', [])])}))
            request = {'retry_failed_batch': {key: failed['failed_batch'][key] for key in ('id', 'sha256')},
                       'edits': [{'op': 'replace', 'index': 0, 'operation': op('add', '/connections', [])}]}
            args['operations_json'] = json.dumps(request)
            for extra in ({'inferences_json': '[]'}, {'mesh_observations': ['mesh_001']}):
                refused = _json_result(await session.call_tool('patch_parametric_bim', {**args, **extra}))
                assert 'cannot combine' in refused['error']
                assert not list((run / 'parametric_patch_failures').glob('*.claim.json'))
            saved = _json_result(await session.call_tool('patch_parametric_bim', args))
            repeated = _json_result(await session.call_tool('patch_parametric_bim', args))
            assert saved['saved_candidate'] == repeated['saved_candidate'] == 'candidate_02'
            assert repeated['retry_receipt']['status'] == 'reused'
            assert not repeated['save_effects']['created_candidates']
            assert len(list(run.glob('candidate_*'))) == 2
    asyncio.run(scenario())
