"""Native asset admission and real MCP mesh observation checks, without model calls."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

import pytest
import trimesh
import numpy as np
from PIL import Image

from scripts.tool_scripts import run_bim_agent as runner
from test_bim_agent_tools import _server_session, _json_result


def _asset(path):
    box = trimesh.creation.box(extents=[4, 6, 8])
    box.visual = trimesh.visual.TextureVisuals(uv=np.zeros((len(box.vertices),2)),
        image=Image.new('RGB',(4,4),(100,150,200)))
    path.write_bytes(trimesh.Scene(box).export(file_type='glb'))
    return path


def _prepare(tmp_path, monkeypatch, *, images=False):
    source = _asset(tmp_path / 'source.glb')
    declaration = tmp_path / 'building.json'
    declaration.write_text('{"use":"synthetic","unknown_path":"private.glb"}')
    captured = {}
    def offline(run, prompt, **kwargs):
        captured.update(prompt=prompt, manifest=runner.Toolkit(run).manifest, kwargs=kwargs)
        record = {'elapsed_seconds': 0, 'result': {'is_error': False, 'total_cost_usd': 0}}
        runner.dump(run / 'agent_receipt.json', record)
        return record
    monkeypatch.setattr(runner, 'subscription', offline)
    argv = ['runner', 'run', '--mesh', str(source), '--building-input', str(declaration),
            '--out', str(tmp_path / 'run'), '--timeout', '30']
    if images:
        from PIL import Image
        folder = tmp_path / 'originals'; folder.mkdir()
        Image.new('RGB', (10, 10), 'white').save(folder / 'plan.png')
        argv += ['--images', str(folder)]
    monkeypatch.setattr(sys, 'argv', argv)
    runner.main()
    return source, tmp_path / 'run', captured


@pytest.mark.parametrize('images', [False, True])
def test_native_asset_input_and_optional_images_keep_exact_originals(tmp_path, monkeypatch, images):
    source, run, captured = _prepare(tmp_path, monkeypatch, images=images)
    assert (run / 'assets/input.glb').read_bytes() == source.read_bytes()
    manifest = captured['manifest']
    assert len(manifest['images']) == int(images)
    assert manifest['mesh_input']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest['input_mode'] == 'native_mesh_agent_experiment'
    assert manifest['input_contents']['original_png_images']['included'] is images
    assert manifest['input_contents']['ground_truth_or_evaluation']['included'] is False
    assert not (run / 'mesh_observations').exists()  # no developer-prepared cameras
    assert not (run / 'private.glb').exists()
    assert captured['kwargs']['model'] == 'sonnet'


def test_mesh_stdio_renders_measures_and_rejects_unadmitted_or_changed_asset(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    async def scenario():
        async with _server_session(run, readonly=False) as session:
            names = {row.name for row in (await session.list_tools()).tools}
            assert {'inspect_mesh','view_mesh','measure_mesh_pixels','view_mesh_observation'} <= names
            desc = _json_result(await session.call_tool('inspect_mesh', {}))
            assert desc['bounds'] == [[-2,-4,-3],[2,4,3]]
            directions = _json_result(await session.call_tool('inspect_mesh_directions', {}))
            assert directions['eligible_triangle_count'] == 8
            assert directions['excluded_triangle_counts']['outside_plane_tilt_limit'] == 4
            assert (run/directions['complete_evidence_file']).exists()
            result = await session.call_tool('view_mesh', {
                'azimuth_degrees': 0, 'elevation_degrees': 0,
                'target': [0,0,0], 'width_m': 12, 'height_m': 9})
            assert not result.isError, result
            assert result.content[0].type == 'image'
            metadata = json.loads(result.content[1].text)
            observation = metadata['observation']
            queried = await session.call_tool('measure_mesh_pixels', {
                'observation': observation, 'pixels': [[600,450],[650,450]]})
            assert not queried.isError, queried
            measured = _json_result(queried)
            assert measured['first_two_distance']['distance_m'] == pytest.approx(0.5, abs=1e-6)
            assert all(row['hit'] and row['world_xyz'][0] == pytest.approx(2) for row in measured['queries'])
            assert all(row['triangle_surface_evidence']['plane_tilt_from_vertical_degrees'] == 0
                       for row in measured['queries'])
            assert measured['first_two_plan_geometry']['horizontal_heading_degrees'] == pytest.approx(90)
            reopened = await session.call_tool('view_mesh_observation', {'observation': observation})
            assert not reopened.isError
            assert reopened.content[0].data == result.content[0].data
            rejected = await session.call_tool('measure_mesh_pixels', {'observation':'../assets/input','pixels':[[0,0]]})
            assert rejected.isError
            with (run / 'assets/input.glb').open('ab') as stream:
                stream.write(b'changed')
            assert (await session.call_tool('inspect_mesh', {})).isError
    asyncio.run(scenario())


def test_tall_mesh_view_preserves_metric_aspect_instead_of_stretching_angles(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    async def scenario():
        async with _server_session(run, readonly=True) as session:
            result = await session.call_tool('view_mesh', {
                'azimuth_degrees':0,'elevation_degrees':90,'width_m':4,'height_m':8})
            assert not result.isError, result
            metadata = json.loads(result.content[1].text)
            assert metadata['resolution_px'] == {'width':600,'height':1200}
            mapping = metadata['pixel_center_mapping']
            assert mapping['pixel_width_m'] == pytest.approx(mapping['pixel_height_m'])
            names = {t.name for t in (await session.list_tools()).tools}
            assert 'inspect_mesh' in names and 'build_parametric_bim' not in names
    asyncio.run(scenario())


def test_empty_input_is_rejected_before_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['runner','run','--out',str(tmp_path/'run')])
    monkeypatch.setattr(runner, 'subscription', lambda *a, **k: pytest.fail('unexpected model call'))
    with pytest.raises(ValueError, match='provide PNG drawings or a native'):
        runner.main()


def test_mesh_frame_and_overlay_reach_stdio_and_preserve_saved_geometry(tmp_path, monkeypatch):
    from test_bim_agent_tools import _two_room_proposal
    _, run, _ = _prepare(tmp_path, monkeypatch)
    built = runner.Toolkit(run).build(json.loads(_two_room_proposal()))
    original = run/built['candidate']/'source_model.json'
    original_bytes = original.read_bytes()
    async def scenario():
        async with _server_session(run, readonly=False) as session:
            result = await session.call_tool('view_mesh', {'azimuth_degrees':0,
                'elevation_degrees':90, 'target':[0,0,0], 'width_m':12, 'height_m':12})
            assert not result.isError
            observation = json.loads(result.content[1].text)['observation']
            missing = await session.call_tool('overlay_mesh_candidate', {
                'candidate':built['candidate'], 'observation':observation})
            assert missing.isError  # never infer a transform from prose or camera
            response = _json_result(await session.call_tool('set_candidate_mesh_frame', {
                'candidate':built['candidate'], 'yaw_degrees':90, 'translation_m':[3,4,0],
                'reason':'synthetic coordinate reference', 'source_refs':['synthetic']}))
            assert response['source_geometry_ready']
            candidate = response['candidate']
            overlay = await session.call_tool('overlay_mesh_candidate', {
                'candidate':candidate, 'observation':observation, 'floor_id':'F1'})
            assert not overlay.isError, overlay
            assert overlay.content[0].type == 'image'
            record = json.loads(overlay.content[1].text)
            assert record['fitted_to_mesh'] is False
            assert (run/record['complete_projection_file']).exists()
            points = _json_result(await session.call_tool('measure_mesh_pixels', {
                'observation':observation,'pixels':[[600,600]],'candidate':candidate}))
            x,y,z = points['queries'][0]['world_xyz']
            assert points['queries'][0]['source_xyz'] == pytest.approx([3-y,4+x,z])
            source = json.loads((run/candidate/'source_model.json').read_text())
            for key in ('spaces','boundaries','openings','connections'):
                assert source[key] == json.loads(original_bytes)[key]
        async with _server_session(run, readonly=True) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert 'set_candidate_mesh_frame' not in names and 'overlay_mesh_candidate' in names
    asyncio.run(scenario())
    assert original.read_bytes() == original_bytes


def test_simple_polygon_reaches_shared_mesh_tools_and_saved_handoff(tmp_path, monkeypatch):
    from test_source_simple_polygon import proposal
    from test_bim_agent_tools import _full_delivery_result
    _, run, _ = _prepare(tmp_path, monkeypatch, images=True)
    value = proposal()
    value['mesh_frame'] = dict(mesh_sha256=runner.Toolkit(run).manifest['mesh_input']['sha256'],
        yaw_degrees=0, translation_m=[0,0,0], reason='synthetic fixture only',
        source_refs=['synthetic case coordinates, no fidelity claim'])

    async def scenario():
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool('build_bim', {'proposal_json': json.dumps(value)}))
            assert built['source_geometry_ready'], built
            candidate = built['candidate']
            saved = json.loads((run/candidate/'source_model.json').read_bytes())
            viewed = await session.call_tool('view_mesh', {'azimuth_degrees':45, 'elevation_degrees':0,
                'target':[1,1,1.5], 'width_m':8, 'height_m':5})
            assert not viewed.isError, viewed
            observation = json.loads(viewed.content[1].text)['observation']
            host = saved['opening_hosts']['window'][0]
            overlay = await session.call_tool('overlay_mesh_candidate', {
                'candidate':candidate, 'observation':observation, 'boundary_ids':[host]})
            assert not overlay.isError, overlay
            record = json.loads(overlay.content[1].text)
            assert record['visibility'] == 'visible'
            assert record['visible_opening_count'] == 1
            projection = json.loads((run/record['complete_projection_file']).read_bytes())
            assert next(row for row in projection['objects'] if row['id'] == 'window')['host_boundary_id'] == host
            delivery = _full_delivery_result(run, await session.call_tool('finish_bim', {'candidate':candidate}))
            assert delivery['source_model_sha256'] == saved['source_model_sha256']
            assert (run/candidate/'viewer.html').is_file()
    asyncio.run(scenario())


def test_new_polygon_contract_cannot_bypass_explicit_drawing_grid(tmp_path, monkeypatch):
    from test_source_simple_polygon import proposal
    _, run, _ = _prepare(tmp_path, monkeypatch)
    manifest_path = run/'inputs.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['lite_bim_grid_step_m'] = .1
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    value = proposal()
    # One off-grid opening remains valid geometry on its slanted host.
    value['geometry']['openings'][1]['p1'] = [3.53, .47]
    result = runner.Toolkit(run).build(value)
    assert result['error_stage'] == 'source_hard_constraints', result
    assert not result['source_geometry_ready']


def test_large_candidate_reads_are_scoped_paged_and_complete(tmp_path, monkeypatch):
    from test_bim_agent_tools import _two_room_proposal
    import copy
    _, run, _ = _prepare(tmp_path, monkeypatch)
    proposal = json.loads(_two_room_proposal())
    template = proposal['geometry']['floors'][0]
    floors = []
    for i in range(20):
        floor = copy.deepcopy(template)
        floor.update(name=f'F{i+1}', z_floor=3*i)
        for cell in floor['cells']:
            cell['id'] = f'F{i+1}_{cell["id"]}'
            cell['source_refs'] = ['large synthetic saved proposal '*40]
        floors.append(floor)
    proposal['geometry'].update(floors=floors,openings=[])
    result = runner.Toolkit(run).build(proposal)
    candidate = result['candidate']
    async def scenario():
        async with _server_session(run, readonly=False) as session:
            summary = _json_result(await session.call_tool('inspect_candidate', {'candidate':candidate}))
            assert summary['summary_due_to_size'] and not summary['geometry_included']
            assert len(json.dumps(summary)) < 22000
            single = _json_result(await session.call_tool('inspect_candidate', {'candidate':candidate,'floor_id':'F4'}))
            assert single['geometry_included']
            assert [f['name'] for f in single['proposal']['geometry']['floors']] == ['F4']
            seen=[];offset=0
            while offset is not None:
                page = _json_result(await session.call_tool('read_candidate_items', {
                    'candidate':candidate,'collection':'cells','offset':offset,'limit':7}))
                assert len(json.dumps(page)) < 22000 and page['page_is_partial']
                seen.extend(c['id'] for c in page['items']);offset=page['next_offset']
            assert seen == [c['id'] for f in floors for c in f['cells']]
            wall = _json_result(await session.call_tool('check_wall_dimensions', {
                'candidate':candidate,'floor_id':'F4','include_inventory':True,'limit':3}))
            assert wall['boundary_inventory_page']['total'] == 8
            assert wall['boundary_inventory_page']['next_offset'] == 3
            assert all(b['space_id'].startswith('F4_') for b in wall['boundary_inventory'])
    asyncio.run(scenario())
