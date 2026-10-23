"""Lineage advice describes verified history without changing edit authority."""
import asyncio
import copy
import hashlib
import json

import pytest

from scripts.tool_scripts import bim_agent_parametric_lineage as subject
from scripts.tool_scripts.bim_agent_edit_route import candidate_edit_route
from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric, patch_parametric
from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report
from scripts.tool_scripts.bim_agent_saved_result import saved_result
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_model import _digest
from tests.test_bim_agent_tools import (
    _ParametricFunctionToolkit, _parametric_file_hashes, _parametric_function_op,
)
from tests.test_parametric_validation import plan


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SourceToolkit(_ParametricFunctionToolkit):
    def build(self, proposal, *, action, parent, **kwargs):
        candidate = f'candidate_{len(list(self.run.glob("candidate_*"))) + 1:02d}'
        provenance = ({'parent_candidate': parent,
            'parent_proposal_sha256': sha(self.run / parent / 'proposal.json')} if parent else {})
        report = export_source_proposal(proposal, self.run / candidate, provenance=provenance)
        assert report['source_geometry_ready'], report
        return saved_result({'candidate': candidate, **report}, candidate=candidate,
                            created=[candidate], geometry_applied=True, audit_written=True)


def change(z=(1.1, 2.1)):
    return {'op': 'update_opening', 'id': 'F1:row:south:1', 'changes': {'z': list(z)},
            'reason': 'observed height', 'source_refs': ['view:south']}


def revise(toolkit, parent, operations):
    proposal = json.loads((toolkit.run / parent / 'proposal.json').read_bytes())
    return toolkit.build(apply_proposal_edits(proposal, operations), action='revise_bim', parent=parent)


@pytest.fixture
def saved(tmp_path):
    toolkit = SourceToolkit(tmp_path / 'run')
    first = build_parametric(toolkit, json.dumps(plan()))
    second = revise(toolkit, first['candidate'], [change()])
    return toolkit, first, second


def history(toolkit, candidate):
    return candidate_edit_route(toolkit, candidate)['parametric_lineage_advisory']


def report(toolkit, summary):
    ref = summary['details']
    path = toolkit.run / ref['report_file']
    assert sha(path) == ref['report_sha256']
    return json.loads(path.read_bytes())


def sync_node(toolkit, candidate, mutate):
    """Fault fixtures keep hashes coherent unless the hash itself is under test."""
    folder = toolkit.run / candidate
    source = json.loads((folder / 'source_model.json').read_bytes())
    proposal = json.loads((folder / 'proposal.json').read_bytes())
    original_proposal = copy.deepcopy(proposal)
    saved_report = json.loads((folder / 'report.json').read_bytes())
    mutate(source, proposal, saved_report)
    if proposal != original_proposal:
        write(folder / 'proposal.json', proposal)
    source['generation']['proposal_sha256'] = sha(folder / 'proposal.json')
    source['source_model_sha256'] = _digest({key: value for key, value in source.items()
                                            if key != 'source_model_sha256'})
    saved_report['proposal_sha256'] = source['generation']['proposal_sha256']
    saved_report['source_model_sha256'] = source['source_model_sha256']
    saved_report['provenance'] = copy.deepcopy(source['generation']['provenance'])
    write(folder / 'source_model.json', source)
    write(folder / 'report.json', saved_report)


def test_verified_history_and_repeated_inspection_reuse_one_readable_report(saved):
    toolkit, first, second = saved
    protected = {path: digest for path, digest in _parametric_file_hashes(toolkit.run).items()
                 if path.startswith('candidate_') or path.startswith('parametric_drafts/')}
    result = history(toolkit, second['candidate'])
    assert result['status'] == 'verified_lineage'
    assert result['exact_parametric_ancestor']['candidate'] == first['candidate']
    assert result['exact_parametric_ancestor']['plan_sha256'] == first['parametric']['plan_sha256']
    assert result['source_sha256'] == sha(toolkit.run / second['candidate'] / 'source_model.json')
    assert result['intervening_changes']['by_operation'] == {'update_opening': 1}
    assert result['intervening_changes']['object_ids_preview'] == ['F1:row:south:1']
    assert not result['automatic_merge'] and 'not a net diff' in result['meaning']
    full = report(toolkit, result)
    assert full['steps'][0]['operations'][0]['correction_index'] == 0
    assert full['steps'][0]['operations'][0]['record']['before']['z'] == [1, 2]
    assert full['steps'][0]['operations'][0]['record']['after']['z'] == [1.1, 2.1]
    ref = result['details']
    page = read_report(toolkit.run, ref['report_file'], 0, 12000)
    assert json.loads(page['text']) == full
    assert history(toolkit, second['candidate']) == result
    inspected = inspect_parametric(toolkit, second['candidate'])
    assert inspected['parametric_lineage_advisory'] == result
    assert 'edit_route' not in inspected
    assert len(list((toolkit.run / 'tool_reports').glob('parametric_lineage_*.json'))) == 1
    after = _parametric_file_hashes(toolkit.run)
    assert all(after[path] == digest for path, digest in protected.items())


def test_exact_selection_and_explicit_rollback_remain_authorized_without_branch_guess(saved):
    toolkit, first, second = saved
    other = revise(toolkit, first['candidate'], [change((0.8, 1.8))])
    selected = history(toolkit, second['candidate'])
    assert selected['chain'] == [second['candidate'], first['candidate']]
    assert other['candidate'] not in json.dumps(selected)
    route = candidate_edit_route(toolkit, first['candidate'])
    assert route['patch_allowed'] and 'parametric_lineage_advisory' not in route
    rollback = patch_parametric(toolkit, json.dumps([_parametric_function_op(
        'add', '/assumptions/-', 'explicitly keep the original plan')]), candidate=first['candidate'],
        expected_sha=first['parametric']['plan_sha256'])
    assert rollback['source_geometry_ready']
    opening = json.loads((toolkit.run / rollback['candidate'] / 'proposal.json').read_bytes())['geometry']['openings'][0]
    assert opening['z'] == [1, 2]  # No source-only branch is silently inherited.
    assert rollback['parametric']['base']['candidate'] == first['candidate']


def test_cumulative_history_and_inverse_edit_are_events_not_missing_edits(saved):
    toolkit, first, second = saved
    third = revise(toolkit, second['candidate'], [change((1, 2))])
    result = history(toolkit, third['candidate'])
    assert result['intervening_changes']['operation_count'] == 2
    assert result['intervening_changes']['object_count'] == 1
    assert result['intervening_changes']['by_operation'] == {'update_opening': 2}
    full = report(toolkit, result)
    assert [step['operations'][0]['correction_index'] for step in full['steps']] == [1, 0]
    assert 'missing_edits' not in full and 'replay_operations' not in full


def frame():
    return {'mesh_sha256': '0' * 64, 'yaw_degrees': 15, 'translation_m': [1, 2, 0],
            'reason': 'explicit registration fixture', 'source_refs': ['frame observation']}


def test_frame_change_outside_corrections_is_explicit_partial_history(saved):
    toolkit, first, second = saved
    proposal = json.loads((toolkit.run / first['candidate'] / 'proposal.json').read_bytes())
    proposal['mesh_frame'] = frame()
    changed = toolkit.build(proposal, action='set_candidate_mesh_frame', parent=first['candidate'])
    result = history(toolkit, changed['candidate'])
    assert result['status'] == 'partial' and 'mesh_frame' in result['reason']
    assert not result['intervening_changes']['history_complete']
    assert result['intervening_changes']['operation_count'] == 0
    full = report(toolkit, result)
    limitation = full['steps'][0]['history_limitations'][0]
    assert limitation['proposal_pointer'] == '/mesh_frame'
    assert limitation['before'] is None and limitation['after'] == frame()
    assert candidate_edit_route(toolkit, changed['candidate'])['recommended_tool'] == 'revise_bim'


def test_unchanged_declared_frame_does_not_hide_verified_generic_history(tmp_path):
    toolkit = SourceToolkit(tmp_path / 'run')
    value = plan()
    value['mesh_frame'] = frame()
    first = build_parametric(toolkit, json.dumps(value))
    second = revise(toolkit, first['candidate'], [change()])
    result = history(toolkit, second['candidate'])
    assert result['status'] == 'verified_lineage'
    assert result['intervening_changes']['history_complete']
    assert result['intervening_changes']['operation_count'] == 1


@pytest.mark.parametrize('fault', ['parent_hash', 'missing_parent', 'missing_report', 'source_hash',
    'report_hash', 'provenance', 'ancestor_plan', 'missing_corrections', 'cross_run'])
def test_broken_binding_is_unavailable_without_changing_generic_edit_permission(saved, fault):
    toolkit, first, second = saved
    folder = toolkit.run / second['candidate']
    if fault == 'missing_report':
        (folder / 'report.json').unlink()
    elif fault == 'ancestor_plan':
        (toolkit.run / first['candidate'] / 'parametric_plan.json').write_text('{}')
    elif fault in {'source_hash', 'report_hash', 'provenance'}:
        path = folder / ('source_model.json' if fault == 'source_hash' else 'report.json')
        value = json.loads(path.read_bytes())
        if fault in {'source_hash', 'report_hash'}:
            value['source_model_sha256'] = '0' * 64
        else:
            value['provenance']['parent_candidate'] = 'candidate_99'
        write(path, value)
    else:
        def mutate(source, proposal, saved_report):
            if fault == 'missing_corrections':
                source['generation'].pop('corrections')
            else:
                key = 'parent_proposal_sha256' if fault == 'parent_hash' else 'parent_candidate'
                source['generation']['provenance'][key] = {
                    'parent_hash': '0' * 64, 'missing_parent': 'candidate_99', 'cross_run': '../other/candidate_01'}[fault]
        sync_node(toolkit, second['candidate'], mutate)
    route = candidate_edit_route(toolkit, second['candidate'])
    assert route['recommended_tool'] == 'revise_bim' and not route['patch_allowed']
    result = route['parametric_lineage_advisory']
    assert result['status'] in {'unavailable', 'partial'} and result['reason']
    assert 'exact_parametric_ancestor' not in result
    assert not result['intervening_changes']['history_complete']


@pytest.mark.parametrize('fault', ['prefix', 'cycle', 'depth'])
def test_multi_step_failures_never_claim_complete_history(saved, monkeypatch, fault):
    toolkit, first, second = saved
    third = revise(toolkit, second['candidate'], [change((1, 2))])
    if fault == 'prefix':
        def mutate(source, proposal, saved_report):
            source['generation']['corrections'][0]['reason'] = 'changed parent history'
            proposal['geometry']['corrections'] = copy.deepcopy(source['generation']['corrections'])
        sync_node(toolkit, third['candidate'], mutate)
    elif fault == 'cycle':
        def mutate(source, proposal, saved_report):
            source['generation']['provenance'].update(parent_candidate=third['candidate'],
                parent_proposal_sha256=sha(toolkit.run / third['candidate'] / 'proposal.json'))
        sync_node(toolkit, second['candidate'], mutate)
    else:
        monkeypatch.setattr(subject, 'MAX_CHAIN_LENGTH', 2)
    result = history(toolkit, third['candidate'])
    assert result['status'] in {'unavailable', 'partial'}
    assert not result['intervening_changes']['history_complete']
    assert 'exact_parametric_ancestor' not in result
    assert {'prefix': 'prefix', 'cycle': 'cycle', 'depth': 'exceeds'}[fault] in result['reason']


def test_long_ids_many_objects_and_reasons_fit_utf8_cap_with_exact_full_report(saved):
    toolkit, first, second = saved
    ids = [f'对象{i}-' + '窗' * 500 for i in range(1000)]
    def mutate(source, proposal, saved_report):
        rows = [{'operation': 'update_opening', 'id': identity, 'reason': '依据' * 1000} for identity in ids]
        source['generation']['corrections'] = rows
        proposal['geometry']['corrections'] = copy.deepcopy(rows)
    sync_node(toolkit, second['candidate'], mutate)
    result = history(toolkit, second['candidate'])
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= subject.MAX_ADVISORY_BYTES
    stats = result['intervening_changes']
    assert stats['object_count'] == 1000 and stats['operation_count'] == 1000
    assert len(stats['object_ids_preview']) + stats['omitted_object_ids'] == 1000
    full = report(toolkit, result)
    assert full['intervening_changes']['object_ids'] == sorted(ids)
    assert 'reason' not in full['steps'][0]['operations'][0]
    assert full['steps'][0]['operations'][0]['record']['reason'] == '依据' * 1000
    # The existing reader exposes bounded JSON and validates the content hash.
    read = read_report(toolkit.run, result['details']['report_file'], 0, 12000,
                       json_pointer='/intervening_changes/object_ids')
    assert read['sha256'] == result['details']['report_sha256']


def test_report_write_failure_and_corrupted_report_fail_open(saved, monkeypatch):
    toolkit, first, second = saved
    original = subject._store_report
    def denied(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(subject, '_store_report', denied)
    route = candidate_edit_route(toolkit, second['candidate'])
    assert route['recommended_tool'] == 'revise_bim'
    assert route['parametric_lineage_advisory']['status'] == 'unavailable'
    monkeypatch.setattr(subject, '_store_report', original)
    result = history(toolkit, second['candidate'])
    (toolkit.run / result['details']['report_file']).write_text('{}')
    route = candidate_edit_route(toolkit, second['candidate'])
    assert route['recommended_tool'] == 'revise_bim'
    assert 'immutable lineage report changed' in route['parametric_lineage_advisory']['reason']
    with pytest.raises(ValueError, match='hash mismatch'):
        read_report(toolkit.run, result['details']['report_file'], 0, 12000)


def test_read_time_source_change_is_not_presented_as_verified(saved, monkeypatch):
    toolkit, first, second = saved
    original = subject._read_node
    def change_after_read(toolkit, candidate, bindings):
        node = original(toolkit, candidate, bindings)
        if candidate == first['candidate']:
            path = toolkit.run / candidate / 'report.json'
            path.write_bytes(path.read_bytes() + b' ')
        return node
    monkeypatch.setattr(subject, '_read_node', change_after_read)
    result = history(toolkit, second['candidate'])
    assert result['status'] == 'partial' and 'changed while reading' in result['reason']
    assert 'exact_parametric_ancestor' not in result
    assert not result['intervening_changes']['history_complete']


def test_long_operation_names_and_missing_ancestor_are_bounded_and_explicit(saved):
    toolkit, first, second = saved
    long_name = '观察' * 2000
    def mutate(source, proposal, saved_report):
        source['generation']['corrections'][0]['operation'] = long_name
        proposal['geometry']['corrections'] = copy.deepcopy(source['generation']['corrections'])
    sync_node(toolkit, second['candidate'], mutate)
    result = history(toolkit, second['candidate'])
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= subject.MAX_ADVISORY_BYTES
    assert result['intervening_changes']['omitted_operation_types'] == 1
    assert report(toolkit, result)['intervening_changes']['by_operation'] == {long_name: 1}
    def remove_parent(source, proposal, saved_report):
        source['generation']['provenance'] = {}
    sync_node(toolkit, second['candidate'], remove_parent)
    result = history(toolkit, second['candidate'])
    assert result['status'] == 'unavailable' and 'no exact parametric ancestor' in result['reason']
    assert not result['intervening_changes']['history_complete']


def test_large_wrapper_and_responses_body_keep_one_bounded_lineage_summary(saved):
    from src.agent_runtime.responses import build_responses_body
    toolkit, first, second = saved
    route = candidate_edit_route(toolkit, second['candidate'])
    result = compact_reply(toolkit.run, 'revise_bim', {'candidate': second['candidate'],
        'source_geometry_ready': True, 'edit_route': route, 'large_inventory': ['value' * 50] * 1000})
    assert result['edit_route']['parametric_lineage_advisory'] == route['parametric_lineage_advisory']
    payload = json.dumps(result, ensure_ascii=False)
    body = build_responses_body(model='offline', messages=[{'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'one', 'type': 'function', 'function': {'name': 'revise_bim', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'one', 'content': payload}], tools=[], parameters={'max_tokens': 256})
    outputs = [row for row in body['input'] if row.get('type') == 'function_call_output']
    assert len(outputs) == 1
    encoded = json.dumps(outputs[0], ensure_ascii=False)
    assert encoded.count('parametric_lineage_advisory') == 1
    assert route['parametric_lineage_advisory']['details']['report_file'] in encoded


def test_actual_mcp_generic_save_and_inspect_expose_same_readable_advice(tmp_path):
    from tests.test_bim_agent_tools import _run_with_one_image, _server_session, _json_result
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            first = _json_result(await session.call_tool('build_parametric_bim', {'plan_json': json.dumps(plan())}))
            revised = _json_result(await session.call_tool('revise_bim', {
                'candidate': first['candidate'], 'operations_json': json.dumps([change()])}))
            assert revised['source_geometry_ready']
            advice = revised['edit_route']['parametric_lineage_advisory']
            assert advice['status'] == 'verified_lineage'
            inspected = _json_result(await session.call_tool('inspect_parametric_plan', {'candidate': revised['candidate']}))
            assert inspected['parametric_lineage_advisory'] == advice
            reference = advice['details']['read']
            assert reference['arguments']['json_pointer'] == '/steps'
            full = _json_result(await session.call_tool(reference['tool'], reference['arguments']))
            assert full['sha256'] == advice['details']['report_sha256']
            assert full['value'][0]['operations'][0]['record']['before']['z'] == [1, 2]
            assert full['value'][0]['operations'][0]['record']['after']['z'] == [1.1, 2.1]
    asyncio.run(scenario())
