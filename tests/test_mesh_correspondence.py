"""Sampled same-camera residuals bind exact input bytes and preserve unknowns."""
import copy
import json

import numpy as np
import pytest
import trimesh

from src.agent.geometry.mesh_correspondence import compare_mesh_source, file_sha256, load_observation, sample_pixels
from src.agent.geometry.mesh_observation import MeshObservation
from src.agent.geometry.source_model import _digest
from tests.test_mesh_observation import _export, _quad


def _source(mesh, z=.2):
    value = {'boundaries': [{'id': 'roof', 'geometry_type': 'ceiling', 'space_id': 'room',
        'vertices': [[-2, -1, z], [2, -1, z], [2, 1, z], [-2, 1, z]]}], 'openings': [],
        'mesh_frame': {'mesh_sha256': mesh.mesh_sha256, 'yaw_degrees': 0, 'translation_m': [0, 0, 0],
                       'reason': 'Synthetic known coordinates', 'source_refs': ['synthetic']}}
    return _seal(value)


def _seal(value):
    value['source_model_sha256'] = _digest({key: item for key, item in value.items() if key != 'source_model_sha256'})
    return value


@pytest.fixture
def scene(tmp_path):
    asset = MeshObservation(_export(trimesh.Scene(_quad(0, (100, 150, 200))), tmp_path/'flat.glb'))
    prefix = tmp_path/'mesh_001'
    asset.render(prefix, eye=[0, 0, 10], target=[0, 0, 0], width_m=6, height_m=4, width_px=60, height_px=40)
    return asset, prefix


def test_signed_depth_normal_and_residual_bands_follow_actual_same_ray_hits(scene):
    mesh, prefix = scene
    result = compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[30, 20]])
    row = result['samples'][0]
    assert row['status'] == 'paired_visible_ray_hits'
    assert row['signed_depth_residual_m'] == pytest.approx(-.2)
    assert row['surface_normal_angle_degrees_unsigned'] == pytest.approx(0)
    assert result['absolute_depth_residual_bands']['over_10_to_30cm'] == 1
    assert result['valid_sample_fraction'] == 1 and result['fidelity'] == 'not_evaluated'
    assert set(result['observation_file_sha256']) == {'metadata', 'image', 'pixel_buffer'}
    result = compare_mesh_source(mesh, prefix, _source(mesh, z=-.5), pixels=[[30, 20]])
    assert result['samples'][0]['signed_depth_residual_m'] == pytest.approx(.5)
    assert result['absolute_depth_residual_bands']['over_30cm'] == 1


@pytest.mark.parametrize('source_z,band', [(0, 'within_5cm'), (.04, 'over_5_to_10cm'),
                                         (-.15, 'over_10_to_30cm')])
def test_geographic_coordinates_and_oblique_camera_use_precise_triangle_hits(tmp_path, source_z, band):
    scene = trimesh.Scene()
    transform = np.eye(4)
    transform[0, 3] = 6_000_000
    scene.add_geometry(_quad(0, (100, 150, 200)), transform=transform)
    mesh = MeshObservation(_export(scene, tmp_path/'geographic.glb'))
    prefix = tmp_path/'mesh_001'
    mesh.render(prefix, eye=[6_000_010, 0, 10], target=[6_000_000, 0, 0],
                width_m=6, height_m=4, width_px=60, height_px=40)
    source = _source(mesh, z=source_z)
    for vertex in source['boundaries'][0]['vertices']:
        vertex[0] += 6_000_000
    result = compare_mesh_source(mesh, prefix, _seal(source),
                                 pixels=[[30, 21], [30, 22], [30, 23], [30, 24]])
    assert result['valid_sample_count'] == 4
    assert all(row['signed_depth_residual_m'] == pytest.approx(-source_z*np.sqrt(2), abs=2e-8)
               for row in result['samples'])
    assert result['absolute_depth_residual_bands'][band] == 4
    assert sum(result['absolute_depth_residual_bands'].values()) == 4


@pytest.mark.parametrize('changed', ['point', 'face'])
def test_recomputed_hit_still_checks_recorded_face_and_buffer_consistency(scene, changed):
    mesh, prefix = scene
    buffer_path = prefix.with_suffix('.npz')
    with np.load(buffer_path, allow_pickle=False) as saved:
        arrays = {key: saved[key] for key in saved.files}
    index = int(np.searchsorted(arrays['pixel_indices'], 20*60+30))
    if changed == 'point':
        arrays['world_points'][index, 2] += .25
    else:
        arrays['face_ids'][index] = 1-int(arrays['face_ids'][index])
    np.savez_compressed(buffer_path, **arrays)
    metadata_path = prefix.with_suffix('.json')
    metadata = json.loads(metadata_path.read_bytes())
    metadata['pixel_buffer_sha256'] = file_sha256(buffer_path)
    metadata_path.write_bytes(json.dumps(metadata).encode('utf-8'))
    with pytest.raises(ValueError, match='original (mesh )?face'):
        compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[30, 20]])


def test_no_hit_is_unknown_not_zero_and_grid_keeps_actual_coverage(scene):
    mesh, prefix = scene
    result = compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[0, 0]])
    assert result['counts'] == {'neither_hit': 1}
    assert result['valid_sample_count'] == 0
    assert all(value is None for value in result['depth_residual_summary_m'].values())
    grid = compare_mesh_source(mesh, prefix, _source(mesh), grid=[8, 8])
    assert grid['sample_count'] == 64 and 0 < grid['valid_sample_fraction'] < 1
    assert sum(grid['absolute_depth_residual_bands'].values()) == grid['valid_sample_count']


def test_selected_rear_source_remains_occluded_by_full_source(scene):
    mesh, prefix = scene
    source = _source(mesh)
    source['boundaries'].append({**source['boundaries'][0], 'id': 'rear',
        'vertices': [[-2, -1, -1], [2, -1, -1], [2, 1, -1], [-2, 1, -1]]})
    result = compare_mesh_source(mesh, prefix, _seal(source), pixels=[[30, 20]], boundary_ids=['rear'])
    assert result['samples'][0]['status'] == 'selected_source_occluded'
    assert result['samples'][0]['selected_source_hit_depth_m'] == pytest.approx(11)
    assert result['valid_sample_count'] == 0


@pytest.mark.parametrize('opening,expected', [('open', 1), ('open_door', 1), ('window', -.2), ('closed_door', -.2)])
def test_open_apertures_let_rays_through_but_glazing_uses_envelope(scene, opening, expected):
    mesh, prefix = scene
    source = _source(mesh)
    source['boundaries'].append({**source['boundaries'][0], 'id': 'rear',
        'vertices': [[-2, -1, -1], [2, -1, -1], [2, 1, -1], [-2, 1, -1]]})
    source['openings'] = [{'id': 'aperture', 'kind': 'door' if 'door' in opening else opening,
        'connectivity': 'open' if opening == 'open_door' else 'closed', 'host_boundary_id': 'roof',
        'vertices': [[-.5, -.5, .2], [.5, -.5, .2], [.5, .5, .2], [-.5, .5, .2]]}]
    result = compare_mesh_source(mesh, prefix, _seal(source), pixels=[[30, 20]])
    assert result['samples'][0]['signed_depth_residual_m'] == pytest.approx(expected)


def test_enclosure_open_region_and_source_frame_are_applied(scene):
    mesh, prefix = scene
    source = _source(mesh)
    source['boundaries'][0]['enclosure_regions'] = [{'condition': 'open',
        'vertices': [[-.5, -.5, .2], [.5, -.5, .2], [.5, .5, .2], [-.5, .5, .2]]}]
    result = compare_mesh_source(mesh, prefix, _seal(source), pixels=[[30, 20]])
    assert result['samples'][0]['status'] == 'source_no_hit'
    source = _source(mesh)
    source['mesh_frame'].update(yaw_degrees=90, translation_m=[4, 3, 2])
    source['boundaries'][0]['vertices'] = [[4-y, 3+x, 2+z] for x, y, z in source['boundaries'][0]['vertices']]
    result = compare_mesh_source(mesh, prefix, _seal(source), pixels=[[30, 20]])
    assert result['samples'][0]['signed_depth_residual_m'] == pytest.approx(-.2)
    assert result['samples'][0]['source']['source_xyz'][2] == pytest.approx(2.2)


@pytest.mark.parametrize('filename', ['.png', '.npz'])
def test_changed_creation_time_observation_bytes_are_rejected(scene, filename):
    mesh, prefix = scene
    path = prefix.with_suffix(filename)
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='creation hashes'):
        compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[30, 20]])


@pytest.mark.parametrize('change', ['resolution', 'camera', 'span', 'old_hash'])
def test_invalid_observation_camera_dimensions_or_legacy_hashes_are_rejected(scene, change):
    mesh, prefix = scene
    record = json.loads(prefix.with_suffix('.json').read_bytes())
    if change == 'resolution':
        record['resolution_px']['width'] += 1
    elif change == 'camera':
        record['camera']['forward_from_eye'] = [1, 0, 0]
    elif change == 'span':
        record['view_span_m']['width'] = -1
    else:
        record.pop('pixel_buffer_sha256')
    prefix.with_suffix('.json').write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError):
        compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[30, 20]])


def test_near_silhouette_is_disclosed_and_excluded_from_bands(scene):
    mesh, prefix = scene
    result = compare_mesh_source(mesh, prefix, _source(mesh), pixels=[[10, 20]])
    assert result['samples'][0]['status'] == 'edge_sample'
    assert result['samples'][0]['mesh']['missing_adjacent_pixels'] == 1
    assert result['valid_sample_count'] == 0


@pytest.mark.parametrize('options', [{'pixels': [[-1, 0]]}, {'pixels': [[1, 1], [1, 1]]},
    {'pixels': [[1, 1]], 'grid': [2, 2]}, {'grid': [17, 1]}, {'grid': [True, 1]}, {'box': [0, 0, 61, 40]}])
def test_invalid_sampling_does_not_create_false_residuals(options):
    with pytest.raises(ValueError):
        sample_pixels({'width': 60, 'height': 40}, **options)
