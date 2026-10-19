"""Native mesh cuts intersect triangles; they do not select face centres."""
import numpy as np
import pytest
import trimesh

from src.agent.geometry.mesh_observation import MeshObservation
from src.agent.geometry.mesh_section import render_section, section_mesh
from tests.test_mesh_observation import _export, _textured_mesh, _vertical_quad, _quad


def test_section_cuts_tall_triangle_even_when_its_centre_is_outside_plane(tmp_path):
    triangle = _textured_mesh([[0, 0, 0], [8, 0, 0], [0, 0, 8]], [[0, 1, 2]],
                             [[0, 0], [1, 0], [0, 1]], np.full((2, 2, 3), 150, dtype=np.uint8))
    asset = MeshObservation(_export(trimesh.Scene(triangle), tmp_path/'triangle.glb'))
    result = section_mesh(asset, height_m=1)
    assert result['counts']['segments'] == 1
    np.testing.assert_allclose(result['segments'][0]['endpoints_xyz'], [[0, 0, 1], [7, 0, 1]])
    assert result['segments'][0]['face_ids'] == [0]
    clipped = section_mesh(asset, height_m=1, bounds_xy=[[2, -1], [4, 1]])
    np.testing.assert_allclose(clipped['segments'][0]['endpoints_xyz'], [[2, 0, 1], [4, 0, 1]])
    assert clipped['total_segment_length_m'] == 2


def test_section_preserves_scene_instances_and_explicit_source_frame(tmp_path):
    scene = trimesh.Scene()
    scene.add_geometry(_vertical_quad(0, origin=(0, 0), length=4, height=3),
                       transform=trimesh.transformations.translation_matrix([2, 5, -3]))
    asset = MeshObservation(_export(scene, tmp_path/'placed.glb'))
    result = section_mesh(asset, height_m=9, yaw_degrees=90, translation_m=[10, 20, 3])
    points = np.array([point for row in result['segments'] for point in row['endpoints_xyz']])
    np.testing.assert_allclose(points.min(axis=0), [7, 22, 9], atol=1e-6)
    np.testing.assert_allclose(points.max(axis=0), [7, 26, 9], atol=1e-6)
    assert result['total_segment_length_m'] == pytest.approx(4)
    image, mapping = render_section(result)
    assert image.size == tuple(mapping['resolution_px'].values())
    assert mapping['image_mapping']['metres_per_pixel'] > 0


def test_coplanar_triangles_and_point_contacts_are_not_invented_cut_lines(tmp_path):
    scene = trimesh.Scene([_quad(2, (120, 120, 120)),
        _textured_mesh([[5, 0, 2], [6, 0, 3], [5, 1, 3]], [[0, 1, 2]],
                       [[0, 0], [1, 0], [0, 1]], np.full((2, 2, 3), 150, dtype=np.uint8))])
    result = section_mesh(MeshObservation(_export(scene, tmp_path/'coplanar.glb')), height_m=2)
    assert result['counts']['coplanar_triangles'] == 2
    assert result['counts']['point_touch_triangles'] == 1
    assert result['segments'] == [] and result['total_segment_length_m'] == 0
    assert result['fidelity'] == 'not_evaluated'
    assert render_section(result)[0].size == (1000, 1000)


@pytest.mark.parametrize('arguments', [{'height_m': float('nan')}, {'height_m': True},
    {'height_m': 1, 'bounds_xy': [[1, 1], [0, 0]]}, {'height_m': 1, 'yaw_degrees': float('inf')}])
def test_invalid_section_coordinates_fail_explicitly(tmp_path, arguments):
    asset = MeshObservation(_export(trimesh.Scene(_quad(0, (120, 120, 120))), tmp_path/'flat.glb'))
    with pytest.raises(ValueError):
        section_mesh(asset, **arguments)


def test_empty_height_is_explicit_and_never_claims_building_coverage(tmp_path):
    asset = MeshObservation(_export(trimesh.Scene(_vertical_quad(30, origin=(0, 0), length=3, height=2)), tmp_path/'wall.glb'))
    result = section_mesh(asset, height_m=10)
    assert result['segments'] == [] and result['counts']['plane_crossing_triangles'] == 0
    assert result['height_m'] == 10 and result['fidelity'] == 'not_evaluated'
