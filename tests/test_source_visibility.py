"""Camera-local visibility must not mistake rear windows for visible facades."""
import copy
import json

import numpy as np
from PIL import Image
import pytest

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_visibility import source_depth_buffer, visible_edge_segments
from test_source_proposal import _proposal


def observation():
    return {'mesh_sha256': 'a'*64, 'resolution_px': {'width': 200, 'height': 200},
        'source_coordinate_transform': {'yaw_degrees_counterclockwise_about_positive_z': 0},
        'camera': {'eye': [-10,3,1.5], 'target': [3,3,1.5], 'forward_from_eye': [1,0,0],
                   'screen_right': [0,1,0], 'screen_up': [0,0,1]},
        'view_span_m': {'width': 8, 'height': 4}}


def source(tmp_path):
    tmp_path = tmp_path/'candidate'
    proposal = _proposal()
    proposal['geometry']['windows'].append({'id': 'rear', 'floor': 'F1', 'facade': 'East',
        'span': [1,2], 'z': [1,2], 'room': 'room'})
    proposal['mesh_frame'] = {'mesh_sha256': 'a'*64, 'yaw_degrees': 0,
        'translation_m': [0,0,0], 'reason': 'test frame', 'source_refs': ['synthetic']}
    assert export_source_proposal(proposal, tmp_path)['source_geometry_ready']
    return json.loads((tmp_path/'source_model.json').read_bytes())


def test_visible_overlay_keeps_hidden_objects_auditable_and_uses_actual_host(tmp_path):
    saved = source(tmp_path)
    original = copy.deepcopy(saved)
    picture = Image.new('RGB', (200,200), 'white')
    _, record = render_mesh_bim_overlay(picture, observation(), saved, visibility='visible')
    rows = {r['id']: r for r in record['objects']}
    assert rows['window']['visibility']['visible_edge_samples'] > 0
    assert rows['rear']['visibility']['status'] == 'back_facing_or_edge_on'
    assert record['opening_count'] == 2 and record['visible_opening_count'] == 1
    assert rows['rear']['host_boundary_id'] == saved['opening_hosts']['rear'][0]
    assert record['fidelity'] == 'not_evaluated' and saved == original
    rear_host = rows['rear']['host_boundary_id']
    _, scoped = render_mesh_bim_overlay(picture, observation(), saved, visibility='visible',
        boundary_ids=[rear_host])
    assert scoped['opening_count'] == 1 and scoped['visible_opening_count'] == 0
    assert all(r['host_boundary_id'] == rear_host for r in scoped['objects'])
    _, xray = render_mesh_bim_overlay(picture, observation(), saved, visibility='xray')
    assert xray['opening_count'] == 2 and xray['visible_opening_count'] is None
    assert picture.getpixel((100,100)) == (255,255,255)
    with pytest.raises(ValueError, match='boundary_ids'):
        render_mesh_bim_overlay(picture, observation(), saved, boundary_ids=['absent'])


def test_geometry_occlusion_includes_other_storeys_and_open_regions():
    camera = observation()['camera']
    span = observation()['view_span_m']
    # Both panels face the camera. The near panel hides half the distant edge.
    near = {'vertices': [[0,2,0], [0,0,0], [0,0,3], [0,2,3]]}
    depth = source_depth_buffer([near], np.asarray, camera, span, (200,200))
    world = np.array([[2,1,1], [2,3,1], [2,3,2], [2,1,2]])
    from src.agent.geometry.source_visibility import project_points
    screen = project_points(world, camera, span, (200,200))
    _, stats = visible_edge_segments(world, screen, depth, camera, span)
    assert 0 < stats['visible_edge_samples'] < stats['tested_edge_samples']
    near['enclosure_regions'] = [{'condition': 'open', 'vertices': near['vertices']}]
    opened = source_depth_buffer([near], np.asarray, camera, span, (200,200))
    _, full = visible_edge_segments(world, screen, opened, camera, span)
    assert full['visible_edge_samples'] == full['tested_edge_samples']


def test_visibility_is_invariant_under_source_frame_rotation(tmp_path):
    saved = source(tmp_path)
    base = observation()
    _, first = render_mesh_bim_overlay(Image.new('RGB', (200,200)), base, saved, visibility='visible')
    # Rotate the observation camera/frame together, leaving source and projected pixels unchanged.
    from src.agent.geometry.mesh_bim_frame import rotate_points
    rotated = copy.deepcopy(base)
    rotated['source_coordinate_transform']['yaw_degrees_counterclockwise_about_positive_z'] = 37
    for key, vector in rotated['camera'].items():
        rotated['camera'][key] = rotate_points([vector], 37)[0].tolist()
    _, second = render_mesh_bim_overlay(Image.new('RGB', (200,200)), rotated, saved, visibility='visible')
    for a, b in zip(first['objects'], second['objects']):
        assert a['id'] == b['id']
        assert a['visibility']['status'] == b['visibility']['status']
        np.testing.assert_allclose(a['projected_pixels'], b['projected_pixels'], atol=1e-10)


@pytest.mark.parametrize('kind,state,seen', [('open', 'open', True), ('door', 'open', True),
                                           ('door', 'unknown', False), ('door', 'closed', False)])
def test_explicit_passages_expose_rear_window_but_unknown_doors_occlude(kind, state, seen):
    from src.agent.correction.parse import ensure_corrected_geometry
    from src.agent.geometry.source_bim import build_source_bim
    from test_source_simple_polygon import cell
    floors = [{'name': name, 'z_floor': 0, 'ceiling_height': 3,
               'footprint': {'vertices': [[x,0],[x+2,0],[x+2,6],[x,6]]},
               'cells': [cell(name, [[x,0],[x+2,0],[x+2,6],[x,6]])]}
              for name, x in [('front', 0), ('rear', 4)]]
    apertures = [{'id': name, 'kind': kind, 'space_id': 'front', 'other_space_id': None, 'p1':[x,1], 'p2':[x,5],
                 'z':[0,2.5], 'state':state, 'source_refs':['synthetic passage']}
                for name, x in [('entry', 0), ('exit', 2)]]
    apertures.append({'id':'window', 'kind':'window', 'space_id':'rear', 'other_space_id':None, 'p1':[4,2],
                     'p2':[4,3], 'z':[1,2], 'source_refs':['synthetic rear window']})
    saved = build_source_bim(ensure_corrected_geometry({'schema_version':'source_polygon_v1',
        'footprint_x':[0,6], 'footprint_y':[0,6], 'floors':floors, 'openings':apertures}),
        capability_profile='simple_polygon')
    assert saved['validation']['status'] == 'pass'
    saved['mesh_frame'] = {'mesh_sha256':'a'*64, 'yaw_degrees':0, 'translation_m':[0,0,0],
                          'reason':'test', 'source_refs':['synthetic']}
    saved['source_model_sha256'] = _digest({k:v for k,v in saved.items() if k != 'source_model_sha256'})
    # Selecting only the rear floor must not drop the front floor's occlusion.
    _, record = render_mesh_bim_overlay(Image.new('RGB', (200,200)), observation(), saved,
        floor_id='rear', visibility='visible')
    row = next(r for r in record['objects'] if r['id'] == 'window')
    assert (row['visibility']['visible_edge_samples'] > 0) is seen


@pytest.mark.parametrize('x,forward', [(1, [1,0,0]), (5, [-1,0,0])])
def test_interior_door_visible_from_either_room_and_reciprocal_host(tmp_path, x, forward):
    saved = source(tmp_path)
    view = observation()
    view['camera'].update(eye=[x,3,1.5], target=[3,3,1.5], forward_from_eye=forward,
                          screen_right=[0,forward[0],0])
    for host in saved['opening_hosts']['door']:
        _, record = render_mesh_bim_overlay(Image.new('RGB', (200,200)), view, saved,
            visibility='visible', exterior_only=False, boundary_ids=[host])
        door = next(r for r in record['objects'] if r['id'] == 'door')
        assert door['scoped_host_boundary_ids'] == [host]
        assert len(door['host_boundary_ids']) == 2
        assert door['visibility']['visible_edge_samples'] > 0
