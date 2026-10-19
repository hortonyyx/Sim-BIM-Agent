"""Readonly mesh MCP can compare an admitted source snapshot without changing it."""
import asyncio
import copy
import hashlib
import json
import shutil

from scripts.tool_scripts.bim_agent_mesh import freeze_mesh
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.mesh_observation import MeshObservation
from src.agent.runtime_tools import (
    MESH_COORDINATOR_TOOL_NAMES,
    MESH_LOCAL_OBSERVER_TOOL_NAMES,
    local_observer_role,
    repeatability_for,
)
from src.harness_contracts.budget import BudgetAmounts
from src.agent.geometry.source_model import _digest
from test_bim_agent_mesh import _asset
from test_bim_agent_tools import _json_result, _server_session
from test_source_simple_polygon import proposal


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_readonly_overlay_uses_actual_source_and_preserves_snapshot_via_stdio(tmp_path):
    run = tmp_path / 'child'
    run.mkdir()
    mesh_input = freeze_mesh(_asset(tmp_path / 'original.glb'), run)
    (run / 'inputs.json').write_text(json.dumps({
        'images': {}, 'mesh_input': mesh_input, 'input_mode': 'runtime_local_mesh_observation',
        'scope': 'synthetic local source comparison; no fidelity claim',
    }), encoding='utf-8')

    value = proposal()
    value['mesh_frame'] = {
        'mesh_sha256': mesh_input['sha256'], 'yaw_degrees': 0, 'translation_m': [0, 0, 0],
        'reason': 'synthetic explicit frame', 'source_refs': ['synthetic:coordinates'],
    }
    parent_candidate = tmp_path / 'parent_candidate'
    assert export_source_proposal(value, parent_candidate)['source_geometry_ready']
    candidate = 'candidate_001'
    snapshot = run / candidate
    snapshot.mkdir()
    for name in ('source_model.json', 'proposal.json', 'report.json'):
        shutil.copyfile(parent_candidate / name, snapshot / name)
    hashes = {path.name: _sha(path) for path in snapshot.iterdir()}
    source = json.loads((snapshot / 'source_model.json').read_bytes())
    # A packet may admit just a later parent observation. Child views must not
    # overwrite its metadata, original pixels or hit buffer.
    prefix = run / 'mesh_observations' / 'mesh_002'
    MeshObservation(run / 'assets/input.glb').render(
        prefix, eye=[100, 100, 1.5], target=[1, 1, 1.5],
        width_m=8, height_m=5, width_px=80, height_px=50)
    admitted_hashes = {path.name: _sha(path) for path in prefix.parent.iterdir()}

    async def scenario():
        async with _server_session(run, readonly=True) as session:
            tools = (await session.list_tools()).tools
            names = [tool.name for tool in tools]
            assert set(names) == set(MESH_LOCAL_OBSERVER_TOOL_NAMES)
            assert len(names) == len(set(names))
            assert MESH_COORDINATOR_TOOL_NAMES.count('overlay_mesh_candidate') == 1
            grants = {grant.tool_name: grant.access for grant in
                      local_observer_role(BudgetAmounts(calls=2)).tool_whitelist}
            assert grants['overlay_mesh_candidate'] == 'read'
            assert repeatability_for('overlay_mesh_candidate') == 'read_only'

            cells = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': candidate, 'collection': 'cells', 'floor_id': 'F1',
            }))
            assert [cell['polygon'] for cell in cells['items']] == [
                cell['polygon'] for cell in value['geometry']['floors'][0]['cells']]
            boundaries, offset = [], 0
            while offset is not None:
                page = _json_result(await session.call_tool('read_candidate_items', {
                    'candidate': candidate, 'collection': 'boundaries', 'floor_id': 'F1',
                    'offset': offset, 'limit': 2,
                }))
                assert page['source_file_sha256'] == hashes['source_model.json']
                assert page['source_model_sha256'] == source['source_model_sha256']
                assert len(page['items']) <= 2
                boundaries.extend(page['items'])
                offset = page['next_offset']
            assert [row['id'] for row in boundaries] == [row['id'] for row in source['boundaries']]
            for boundary, saved in zip(boundaries, source['boundaries']):
                assert {key: boundary[key] for key in saved} == saved
                assert boundary['floor_id'] == 'F1'
            host = next(row['id'] for row in boundaries if 'window' in row['opening_ids'])
            assert host == source['opening_hosts']['window'][0]
            view = await session.call_tool('view_mesh', {
                'azimuth_degrees': 45, 'elevation_degrees': 0,
                'target': [1, 1, 1.5], 'width_m': 8, 'height_m': 5,
            })
            assert not view.isError, view
            observation = json.loads(view.content[1].text)['observation']
            assert observation != prefix.name
            overlay = await session.call_tool('overlay_mesh_candidate', {
                'candidate': candidate, 'observation': observation, 'boundary_ids': [host],
            })
            assert not overlay.isError, overlay
            assert overlay.content[0].type == 'image'
            record = json.loads(overlay.content[1].text)
            assert record['visibility'] == 'visible'
            assert record['visible_opening_count'] == 1
            assert record['source_file_sha256'] == hashes['source_model.json']
            assert record['fitted_to_mesh'] is False
            assert _sha(run / record['image_file']) == record['image_sha256']
            assert record['image_sha256'] != record['observation_image_sha256']
            report_file = record['complete_projection_file']
            assert report_file.startswith('tool_reports/')
            assert _sha(run / report_file) == record['complete_projection_sha256']
            pieces, offset = [], 0
            while offset is not None:
                page = _json_result(await session.call_tool('read_candidate_items', {
                    'candidate': '', 'collection': 'report', 'report_file': report_file,
                    'offset': offset, 'limit': 2048,
                }))
                assert page['sha256'] == record['complete_projection_sha256']
                assert len(page['text']) <= 2048
                pieces.append(page['text'])
                offset = page['next_offset']
            projection = json.loads(''.join(pieces))
            assert projection == json.loads((run / f'mesh_overlays/{record["overlay_id"]}.json').read_bytes())
            opening = next(row for row in projection['objects'] if row['id'] == 'window')
            assert opening['host_boundary_id'] == host
            assert opening['observation_xyz'] == next(
                row['vertices'] for row in source['openings'] if row['id'] == 'window')
            pointer = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': '', 'collection': 'report', 'report_file': report_file,
                'json_pointer': '/objects/0/id',
            }))
            assert pointer['value'] == projection['objects'][0]['id']
            measured = _json_result(await session.call_tool('measure_mesh_pixels', {
                'candidate': candidate, 'observation': observation, 'pixels': [[600, 300]],
            }))
            assert measured['source_file_sha256'] == hashes['source_model.json']
            assert measured['source_model_sha256'] == source['source_model_sha256']
            hit = measured['queries'][0]
            assert hit['hit'] and hit['source_xyz'] == hit['world_xyz']

            writes = {
                'set_candidate_mesh_frame': {'candidate': candidate, 'yaw_degrees': 10,
                    'translation_m': [0, 0, 0], 'reason': 'forbidden', 'source_refs': ['test']},
                'build_bim': {'proposal_json': json.dumps(value)},
                'revise_bim': {'candidate': candidate, 'edits_json': '[]'},
                'finish_bim': {'candidate': candidate},
            }
            for name, arguments in writes.items():
                assert name not in names
                assert (await session.call_tool(name, arguments)).isError
            for inaccessible in ('candidate_002', '../parent_candidate'):
                assert (await session.call_tool('overlay_mesh_candidate', {
                    'candidate': inaccessible, 'observation': observation,
                })).isError
                assert (await session.call_tool('measure_mesh_pixels', {
                    'candidate': inaccessible, 'observation': observation, 'pixels': [[600, 300]],
                })).isError
                assert (await session.call_tool('read_candidate_items', {
                    'candidate': inaccessible, 'collection': 'boundaries',
                })).isError
            assert (await session.call_tool('read_candidate_items', {
                'candidate': candidate, 'collection': 'boundaries', 'floor_id': 'absent',
            })).isError
            for private in ('inputs.json', f'{candidate}/source_model.json', '../parent_candidate/source_model.json',
                            f'mesh_overlays/{record["overlay_id"]}.json', str(parent_candidate / 'source_model.json')):
                assert (await session.call_tool('read_candidate_items', {
                    'candidate': '', 'collection': 'report', 'report_file': private,
                })).isError
            (run / report_file).write_text('{}', encoding='utf-8')
            assert (await session.call_tool('read_candidate_items', {
                'candidate': '', 'collection': 'report', 'report_file': report_file,
            })).isError

    asyncio.run(scenario())
    assert {path.name: _sha(path) for path in snapshot.iterdir()} == hashes
    assert {name: _sha(prefix.parent / name) for name in admitted_hashes} == admitted_hashes
    assert [path.name for path in run.glob('candidate_*')] == [candidate]
    assert not (run / 'final.json').exists()
    log = [json.loads(line) for line in (run / 'tools.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(row['action'] == 'overlay_mesh_candidate' for row in log)
    assert all(row['readonly'] for row in log)


def test_source_boundary_pages_filter_real_floors_bound_large_rows_and_reject_tampering(tmp_path):
    run = tmp_path / 'child'
    run.mkdir()
    (run / 'inputs.json').write_text('{"images": {}}', encoding='utf-8')
    value = proposal()
    floor = copy.deepcopy(value['geometry']['floors'][0])
    floor.update(name='F2', z_floor=3)
    for cell in floor['cells']:
        cell['id'] = 'upper_' + cell['id']
    value['geometry']['floors'].append(floor)
    candidate = 'candidate_001'
    assert export_source_proposal(value, run / candidate)['source_geometry_ready']
    source_path = run / candidate / 'source_model.json'
    source = json.loads(source_path.read_bytes())
    source['boundaries'][0]['source_refs'] = ['large retained observation ' * 1500]
    source['source_model_sha256'] = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
    source_path.write_text(json.dumps(source), encoding='utf-8')
    original_hash = _sha(source_path)

    async def scenario():
        async with _server_session(run, readonly=True) as session:
            upper = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': candidate, 'collection': 'boundaries', 'floor_id': 'F2', 'limit': 50,
            }))
            assert {row['space_id'] for row in upper['items']} == {'upper_a', 'upper_b'}
            assert {row['floor_id'] for row in upper['items']} == {'F2'}
            assert upper['next_offset'] is None
            large = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': candidate, 'collection': 'boundaries', 'floor_id': 'F1', 'limit': 1,
            }))
            assert len(json.dumps(large).encode()) < 24000
            assert large['source_model_sha256'] == source['source_model_sha256']
            assert large['source_file_sha256'] == original_hash
            assert _sha(run / large['details_file']) == large['details_sha256']
            item = _json_result(await session.call_tool('read_candidate_items', {
                'candidate': '', 'collection': 'report', 'report_file': large['details_file'],
                'json_pointer': '/items/0/id',
            }))
            assert item['value'] == source['boundaries'][0]['id']
            assert _sha(source_path) == original_hash
            source['boundaries'][0]['vertices'][0][0] += 1
            source_path.write_text(json.dumps(source), encoding='utf-8')
            rejected = await session.call_tool('read_candidate_items', {
                'candidate': candidate, 'collection': 'boundaries',
            })
            assert rejected.isError

    asyncio.run(scenario())
