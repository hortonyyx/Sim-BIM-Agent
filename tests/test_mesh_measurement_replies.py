"""Direct measurement display stays small without changing canonical evidence."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report
from scripts.tool_scripts.bim_agent_saved_result import full_result_metadata, result_metadata
from src.agent.runtime_mesh_workers import MeshWorkerTools, _EvidenceTools
from src.agent.runtime_tools import FrozenBimTools
from test_bim_agent_tools import _json_result, _server_session
from test_mesh_delivery_review import prepared
from test_agent_runtime import response
from test_mesh_workers import _limits, _root_engine
from src.agent_runtime.store import EventStore


def _envelope(value):
    return {'structuredContent': value, 'content': [
        {'type': 'text', 'text': json.dumps(value)},
        {'type': 'text', 'text': 'Remaining time and reference note.'},
        {'type': 'image', 'mimeType': 'image/png', 'data': 'cG5n'}], 'isError': False}


def _projection(wrapper, raw):
    frozen = SimpleNamespace(project_tool_result=lambda name, value:
        FrozenBimTools.project_tool_result(None, name, value))
    if wrapper == 'plain':
        return frozen.project_tool_result('measure_mesh_pixels', raw)
    if wrapper == 'parent':
        return MeshWorkerTools.project_tool_result(SimpleNamespace(frozen=frozen), 'measure_mesh_pixels', raw)
    return _EvidenceTools.project_tool_result(SimpleNamespace(frozen=frozen), 'measure_mesh_pixels', raw)


@pytest.mark.parametrize('wrapper', ['plain', 'parent', 'worker'])
def test_projection_preserves_128_numbers_invalid_reasons_and_mirrors(tmp_path, wrapper):
    hit = {'pixel': [1, 2], 'hit': True, 'measurement_valid': True, 'world_xyz': [1, 2, 3],
        'source_xyz': [4, 5, 6], 'face_id': 7,
        'triangle_surface_evidence': {'global_face_id': 7, 'triangle_normal_xyz': [0, 1, 0],
            'triangle_vertices_xyz': [[0, 0, 0], [1, 0, 0], [0, 0, 1]],
            'plane_tilt_from_vertical_degrees': 0, 'horizontal_surface_trace_direction_degrees': 0,
            'material_alpha_mode': 'OPAQUE', 'material_geometry_caveat': 'Texture is not a boundary.'}}
    rows = [copy.deepcopy(hit) for _ in range(125)] + [
        {'pixel': [2, 3], 'hit': False, 'measurement_valid': False, 'background': True},
        {'pixel': [3, 4], 'hit': False, 'measurement_valid': False, 'background': False,
         'reason': 'transparent_composite', 'measurement_invalid_reason': 'No unique surface.'},
        {'pixel': [4, 5], 'hit': True, 'measurement_valid': True, 'world_xyz': [2, 3, 4],
         'triangle_surface_evidence': None,
         'triangle_surface_evidence_unavailable_reason': 'legacy pixel buffer does not record its operation yaw'}]
    full = {'queries': rows, 'mesh_sha256': 'mesh', 'candidate': 'candidate_01',
        'source_model_sha256': 'source', 'source_file_sha256': 'file',
        'mesh_frame': {'yaw_degrees': 30, 'translation_m': [4, 5, 6]},
        'first_two_distance': {'distance_m': 2, 'same_triangle': True,
            'surface_relation_caveat': 'Different face IDs and similar normals do not establish one wall.'},
        'first_two_plan_geometry': {'horizontal_heading_degrees': 90, 'note': 'Not an inferred building axis.'}}
    ref = compact_reply(tmp_path, 'measure_mesh_pixels', {}, full_result=full, force=True)
    raw = _envelope({**full, **ref})
    before = copy.deepcopy(raw)
    shown = _projection(wrapper, raw)
    visible = shown['structuredContent']
    assert raw == before and shown is not raw
    assert isinstance(visible['queries'], list) and len(visible['queries']) == 128
    assert json.loads(shown['content'][0]['text']) == visible
    assert shown['content'][1:] == raw['content'][1:]
    for old, new in zip(rows, visible['queries']):
        assert {k: v for k, v in old.items() if k != 'triangle_surface_evidence'} == {
            k: v for k, v in new.items() if k != 'surface_summary'}
    for key in full.keys() - {'queries'}:
        assert visible[key] == full[key]
    assert visible['queries'][0]['surface_summary']['material_geometry_caveat'] == 'Texture is not a boundary.'
    assert 'triangle_vertices_xyz' not in json.dumps(visible)
    assert full_result_metadata(shown, tmp_path)['queries'] == rows


def test_other_tools_errors_and_legacy_reports_are_not_projected():
    raw = _envelope({'queries': [{'triangle_surface_evidence': {'triangle_vertices_xyz': [[1, 2, 3]]}}]})
    assert FrozenBimTools.project_tool_result(None, 'measure_mesh_pixels', raw) is raw
    raw['structuredContent'].update(details_file='tool_reports/x.json', details_sha256='x')
    assert FrozenBimTools.project_tool_result(None, 'read_candidate_items', raw) is raw
    assert FrozenBimTools.project_tool_result(None, 'read_mesh_worker', raw) is raw
    error = {**raw, 'isError': True}
    assert FrozenBimTools.project_tool_result(None, 'measure_mesh_pixels', error) is error
    raw['structuredContent']['error'] = 'failed query'
    assert FrozenBimTools.project_tool_result(None, 'measure_mesh_pixels', raw) is raw


def test_real_mcp_keeps_full_snapshot_report_and_paged_detail(prepared):
    toolkit, candidate = prepared
    run = toolkit.run

    async def scenario():
        async with _server_session(run, readonly=True) as session:
            args = {'observation': 'mesh_001', 'pixels': [[12, 12], [13, 12]] * 64, 'candidate': candidate}
            class DirectMcpTools:
                # The ordinary runtime drives the real local MCP transport.
                # Registry sealing is a separate entry check, not this fixture.
                project_tool_result = FrozenBimTools.project_tool_result
                start_epoch = time.time()

                async def list_tools(self):
                    return [{'name': 'measure_mesh_pixels', 'description': 'query', 'inputSchema': {'type': 'object'}}]

                async def call_tool(self, name, arguments):
                    return (await session.call_tool(name, arguments)).model_dump(mode='json', exclude_none=True)

                def repeatability(self, name):
                    return 'read_only'

                def snapshot_state(self):
                    return {}

                def artifacts(self):
                    return []

            limits = _limits()
            with EventStore(run.parent / 'runtime-audit', run_id='measurement', task_id='coordinator',
                            budget_limit=limits.ledger_limit()) as store:
                engine = _root_engine(store, DirectMcpTools(), limits, [
                    response(('measure', 'measure_mesh_pixels', args)), response(text='Observed; not fidelity approval.')])
                assert (await engine.run([{'role': 'user', 'content': 'Measure the supplied points.'}]))['status'] == 'completed'
                event = next(e for e in store.events if e.payload.event_type == 'tool_execution')
                raw = store.resolve(event.payload.raw_result)
                wire = store.resolve(event.payload.shown_result)['tool_message']['content']
                assert 'triangle_vertices_xyz' in json.dumps(raw)
                assert 'triangle_vertices_xyz' not in wire
                assert 'triangle_vertices_xyz' not in engine.adapter.requests[-1].decode()
            full = result_metadata(raw)
            assert len(full['queries']) == 128
            assert 'triangle_vertices_xyz' in json.dumps(full['queries'][0])
            for path, digest in full['observation_snapshot']['file_sha256'].items():
                assert hashlib.sha256((run / path).read_bytes()).hexdigest() == digest
            assert set(full['observation_snapshot']['file_sha256']) == {
                f'mesh_observations/mesh_001{ext}' for ext in ('.json', '.png', '.npz')}
            assert full['observation'] == 'mesh_001'
            source_path = run / candidate / 'source_model.json'
            source = json.loads(source_path.read_bytes())
            assert full['source_file_sha256'] == hashlib.sha256(source_path.read_bytes()).hexdigest()
            assert full['source_model_sha256'] == source['source_model_sha256']
            assert full['mesh_frame'] == source['mesh_frame']
            assert full['queries'][0]['source_xyz'] != full['queries'][0]['world_xyz']
            shown = _projection('plain', raw)
            assert len(result_metadata(shown)['queries']) == 128
            assert 'triangle_vertices_xyz' not in json.dumps(shown)
            assert full_result_metadata(shown, run)['queries'] == full['queries']
            pages, offset = [], 0
            while offset is not None:
                page = _json_result(await session.call_tool('read_candidate_items', {
                    'candidate': '', 'collection': 'report', 'report_file': full['details_file'],
                    'offset': offset, 'limit': 12000}))
                pages.append(page['text'])
                offset = page['next_offset']
            joined = ''.join(pages)
            assert len(pages) > 1
            assert hashlib.sha256(joined.encode()).hexdigest() == full['details_sha256']
            assert json.loads(joined)['queries'] == full['queries']
            pointer = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': '', 'collection': 'report', 'report_file': full['details_file'],
                'json_pointer': '/queries/0/triangle_surface_evidence'}))
            assert pointer['value'] == full['queries'][0]['triangle_surface_evidence']
            # Reports describe the captured snapshot, never recompute against a changed source.
            from src.agent.geometry.source_model import _digest
            source['mesh_frame']['yaw_degrees'] += 30
            source['source_model_sha256'] = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
            source_path.write_text(json.dumps(source), encoding='utf-8')
            snapshot = full_result_metadata(shown, run)
            assert snapshot['source_file_sha256'] == full['source_file_sha256']
            assert snapshot['mesh_frame'] == full['mesh_frame'] != source['mesh_frame']
            assert snapshot['queries'] == full['queries']
            assert read_report(run, full['details_file'], 0, 12000)['sha256'] == full['details_sha256']
            for bad in ('../inputs.json', 'tool_reports/missing.json'):
                rejected = await session.call_tool('read_candidate_items', {
                    'candidate': '', 'collection': 'report', 'report_file': bad})
                assert rejected.isError
            for extra in ({'json_pointer': '/queries/128'}, {'offset': -1}, {'limit': 12001}):
                assert (await session.call_tool('read_candidate_items', {
                    'candidate': '', 'collection': 'report', 'report_file': full['details_file'], **extra})).isError
            corrupt = run / full['details_file']
            corrupt.write_bytes(corrupt.read_bytes() + b' ')
            assert (await session.call_tool('read_candidate_items', {
                'candidate': '', 'collection': 'report', 'report_file': full['details_file']})).isError
            with pytest.raises(ValueError, match='hash mismatch'):
                full_result_metadata(shown, run)
    asyncio.run(scenario())


def test_identical_query_does_not_advertise_preexisting_corrupt_report(prepared):
    run = prepared[0].run

    async def scenario():
        async with _server_session(run, readonly=True) as session:
            args = {'observation': 'mesh_001', 'pixels': [[12, 12]]}
            full = _json_result(await session.call_tool('measure_mesh_pixels', args))
            report = run / full['details_file']
            report.write_bytes(report.read_bytes() + b' ')
            retry = await session.call_tool('measure_mesh_pixels', args)
            assert retry.isError
    asyncio.run(scenario())


def test_legacy_numeric_buffer_remains_queryable_without_png(prepared):
    run = prepared[0].run
    (run / 'mesh_observations/mesh_001.png').unlink()

    async def scenario():
        async with _server_session(run, readonly=True) as session:
            full = _json_result(await session.call_tool('measure_mesh_pixels', {
                'observation': 'mesh_001', 'pixels': [[12, 12]]}))
            assert full['queries'][0]['measurement_valid'] is True
            snapshot = full['observation_snapshot']
            assert snapshot['missing_files'] == ['mesh_observations/mesh_001.png']
            assert set(snapshot['file_sha256']) == {
                'mesh_observations/mesh_001.json', 'mesh_observations/mesh_001.npz'}
            assert full_result_metadata(_envelope(full), run)['queries'] == full['queries']
    asyncio.run(scenario())
