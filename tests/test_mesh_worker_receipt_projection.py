"""Exact same-source records can share display references; selectors preserve raw evidence."""
import copy
import hashlib
import json

import pytest

from src.agent.runtime_mesh_workers import MeshWorkerTools, WORKER_GUIDANCE, mesh_worker_tool_specs


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def outcome():
    evidence = {'evidence_kind': 'image', 'evidence_id': 'plan.png', 'source_sha256': 'source-one',
        'prefetched_views': [{'view': {'box_original_pixels': [10, 20, 110, 120]},
                             'returned_png_sha256': 'same-png'}]}
    return {'task_id': 'labels', 'status': 'completed', 'result': {
        'directly_seen': [{'observation_id': 'label1', 'statement': 'Visible label.', 'box_pixels': None,
                          'evidence': copy.deepcopy(evidence)},
                         {'observation_id': 'label2', 'statement': 'Another label.', 'box_pixels': [20, 30, 40, 50],
                          'evidence': copy.deepcopy(evidence)}],
        'interpretations': [], 'uncertain': [], 'fidelity': 'not_evaluated'},
        'evidence_catalog': {'views': [{'id': 'plan.png'}, {'id': 'unselected'}]},
        'source_basis': {'candidate': 'candidate_01', 'sha256': 'frozen-source'}}


def project(value, name='read_mesh_worker', blocks=()):
    raw = {'structuredContent': value, 'content': [{'type': 'text', 'text': json.dumps(value)}, *blocks], 'isError': False}
    before = copy.deepcopy(raw)
    shown = MeshWorkerTools.project_tool_result(None, name, raw)
    assert raw == before
    assert shown['content'][1:] == raw['content'][1:]
    return shown['structuredContent']


def restore_evidence(result):
    records = {item['id']: item for item in result['evidence_sources']}
    rows = copy.deepcopy(result['directly_seen'])
    for row in rows:
        record = records[row.pop('evidence_ref')]
        assert canonical_hash(record['evidence']) == record['sha256']
        row['evidence'] = record['evidence']
    return rows


@pytest.mark.parametrize('changed', [None, 'source', 'roi', 'view_receipt', 'evidence_identity'])
def test_same_png_does_not_merge_different_evidence_records(changed):
    value = outcome()
    evidence = value['result']['directly_seen'][1]['evidence']
    if changed == 'source': evidence['source_sha256'] = 'source-two'
    elif changed == 'roi': evidence['prefetched_views'][0]['view']['box_original_pixels'] = [30, 40, 130, 140]
    elif changed == 'view_receipt': evidence['view_receipts'] = [{'tool_call_id': 'additional-successful-view'}]
    elif changed == 'evidence_identity': evidence['evidence_id'] = 'different-original.png'
    result = project(value)['result']
    assert len(result['evidence_sources']) == (1 if changed is None else 2)
    assert restore_evidence(result) == value['result']['directly_seen']
    assert result['directly_seen'][0]['box_pixels'] is None
    assert result['directly_seen'][1]['box_pixels'] == [20, 30, 40, 50]


@pytest.mark.parametrize('selection', [
    {'view_ids': ['plan.png'], 'measurement_call_ids': []},
    {'view_ids': [], 'measurement_call_ids': ['measure-1']},
    {'view_ids': ['plan.png'], 'measurement_call_ids': ['measure-1']},
])
def test_selection_only_shown_copy_keeps_complete_raw_and_complete_selected_measurement(selection):
    value = outcome()
    value['requested_selection'] = selection
    value['evidence_previews'] = [{'id': 'plan.png', 'original_sha256': 'source-one', 'returned_png_sha256': 'same-png'}]
    full = {'queries': [{'pixel': [20, 40], 'world_xyz': [1, 2, 3], 'measurement_valid': True,
                       'triangle_surface_evidence': {'triangle_vertices_xyz': [[1, 2, 3]] * 3}}]}
    if selection['measurement_call_ids']:
        value['full_measurements'] = [{'tool_call_id': 'measure-1', 'tool_result_sha256': 'verified', 'result': full}]
    shown = project(value, blocks=[{'type': 'image', 'data': 'exact-png', 'mimeType': 'image/png'}])
    assert 'result' not in shown and 'evidence_catalog' not in shown
    assert shown['source_basis'] == value['source_basis']
    assert shown['evidence_previews'] == value['evidence_previews']
    assert shown['result_reference']['canonical_result_sha256'] == canonical_hash(value['result'])
    assert shown['result_reference']['read'] == {'tool': 'read_mesh_worker', 'arguments': {'task_id': 'labels'}}
    if selection['measurement_call_ids']:
        assert shown['full_measurements'] == value['full_measurements']
        assert 'triangle_vertices_xyz' in json.dumps(shown['full_measurements'])
    default = project({key: item for key, item in value.items() if key not in {'requested_selection', 'full_measurements', 'evidence_previews'}})
    assert default['result_reference'] == shown['result_reference']
    assert restore_evidence(default['result']) == value['result']['directly_seen']


def test_empty_selector_keeps_answer_and_failed_result_remains_unchanged():
    value = outcome()
    value['requested_selection'] = {'view_ids': [], 'measurement_call_ids': []}
    assert 'result' in project(value)
    raw = {'structuredContent': value, 'content': [], 'isError': True}
    assert MeshWorkerTools.project_tool_result(None, 'read_mesh_worker', raw) is raw


def test_guidance_matches_optional_coordinates_without_changing_answer_schema():
    from src.agent.runtime_mesh_evidence import MeshDirectObservation
    assert 'box_pixels is optional' in WORKER_GUIDANCE
    assert 'or use null when the task does not request coordinates' in WORKER_GUIDANCE
    description = next(row for row in mesh_worker_tool_specs() if row['name'] == 'delegate_mesh_workers')['inputSchema']['properties']['tasks']['items']['properties']['image_view_ids']['description']
    assert 'omitted/null' in description and 'if supplied, use ORIGINAL pixels' in description
    assert 'box_pixels' not in MeshDirectObservation.model_json_schema().get('required', [])
