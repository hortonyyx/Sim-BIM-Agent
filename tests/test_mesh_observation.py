from __future__ import annotations

from pathlib import Path
import hashlib

import numpy as np
from PIL import Image
import pytest
import trimesh

from src.agent.geometry.mesh_observation import MeshObservation


_BACKGROUND_FOR_TEST = (238, 242, 246)


def _textured_mesh(
    bim_vertices: list[list[float]],
    faces: list[list[int]],
    uv: list[list[float]],
    texture: np.ndarray,
    base_color_factor: list[int] | None = None,
) -> trimesh.Trimesh:
    bim = np.asarray(bim_vertices, dtype=float)
    # Module input is glTF Y-up; inverse of public [X, -Z, Y].
    source = np.column_stack((bim[:, 0], bim[:, 2], -bim[:, 1]))
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(np.asarray(texture, dtype=np.uint8), mode="RGB"),
        baseColorFactor=base_color_factor,
    )
    visual = trimesh.visual.texture.TextureVisuals(
        uv=np.asarray(uv, dtype=float), material=material
    )
    return trimesh.Trimesh(
        vertices=source,
        faces=np.asarray(faces, dtype=np.int64),
        visual=visual,
        process=False,
    )


def _quad(z: float, color: tuple[int, int, int]) -> trimesh.Trimesh:
    return _textured_mesh(
        [[-2, -1, z], [2, -1, z], [2, 1, z], [-2, 1, z]],
        [[0, 1, 2], [0, 2, 3]],
        # One full positive repeat also exercises GLB's default wrap mode.
        [[1, 1], [2, 1], [2, 2], [1, 2]],
        np.full((2, 2, 3), color, dtype=np.uint8),
    )


def _blend_quad(z, color=(255, 0, 0), alpha=128, *, alpha_factor=255):
    mesh = _quad(z, color)
    mesh.visual.material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(np.full((2, 2, 4), (*color, alpha), dtype=np.uint8)),
        baseColorFactor=[255, 255, 255, alpha_factor], alphaMode="BLEND")
    return mesh


def _blend_view(tmp_path, meshes, *, name="blend", width=40, height=40):
    scene = trimesh.Scene()
    for index, mesh in enumerate(meshes):
        scene.add_geometry(mesh, node_name=f"part_{index}", geom_name=f"part_{index}")
    path = _export(scene, tmp_path/f"{name}.glb")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    observation = MeshObservation(path)
    prefix = tmp_path/name
    image, metadata = observation.render(prefix, eye=[0, 0, 10], target=[0, 0, 0],
        width_m=4, height_m=2, width_px=width, height_px=height)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before == observation.mesh_sha256
    return observation, prefix, image, metadata


def _expected_linear_over(layers, background):
    def decode(rgb):
        values = np.asarray(rgb, dtype=float)/255
        return np.where(values <= .04045, values/12.92, ((values+.055)/1.055)**2.4)
    output = decode(background)
    for color, alpha in reversed(layers):
        output = alpha*decode(color) + (1-alpha)*output
    encoded = np.where(output <= .0031308, output*12.92, 1.055*output**(1/2.4)-.055)
    return tuple(np.rint(encoded*255).astype(int))


def _vertical_quad(
    direction_degrees: float,
    *,
    origin: tuple[float, float],
    length: float,
    height: float,
    color: tuple[int, int, int] = (120, 140, 160),
) -> trimesh.Trimesh:
    angle = np.radians(direction_degrees)
    direction = np.array([np.cos(angle), np.sin(angle)])
    start = np.asarray(origin, dtype=float)
    end = start + length * direction
    return _textured_mesh(
        [
            [start[0], start[1], 0],
            [end[0], end[1], 0],
            [end[0], end[1], height],
            [start[0], start[1], height],
        ],
        [[0, 1, 2], [0, 2, 3]],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        np.full((2, 2, 3), color, dtype=np.uint8),
    )


def _export(scene: trimesh.Scene, path: Path) -> Path:
    path.write_bytes(trimesh.exchange.gltf.export_glb(scene))
    return path


def test_describe_applies_scene_transform_y_up_mapping_yaw_and_selection(
    tmp_path: Path,
) -> None:
    mesh = _textured_mesh(
        [[0, 0, 0], [2, 0, 0], [0, 1, 3]],
        [[0, 1, 2]],
        [[0, 0], [1, 0], [0, 1]],
        np.full((2, 2, 3), 200, dtype=np.uint8),
    )
    # Scene transform is in source Y-up axes. It becomes [10, -30, 20].
    transform = trimesh.transformations.translation_matrix([10, 20, 30])
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="translated", transform=transform)
    observation = MeshObservation(_export(scene, tmp_path / "scene.glb"))

    description = observation.describe()
    np.testing.assert_allclose(
        description["bounds"], [[10, -30, 20], [12, -29, 23]], atol=1e-6
    )
    assert description["vertex_count"] == 3
    assert description["face_count"] == 1
    assert description["selected_face_count"] == 1

    yawed = observation.describe(yaw_degrees=90)
    np.testing.assert_allclose(
        yawed["bounds"], [[29, 10, 20], [30, 12, 23]], atol=1e-6
    )
    excluded = observation.describe(bounds=[[0, 0, 0], [1, 1, 1]])
    assert excluded["selected_face_count"] == 0
    assert excluded["selected_vertex_count"] == 0
    assert excluded["bounds"] is None
    assert "not evidence of enclosure truth" in excluded["selection_caveat"]


def test_render_metric_pixel_query_and_uv_image_orientation(tmp_path: Path) -> None:
    texture = np.array(
        [
            [[255, 0, 0], [0, 255, 0]],
            [[0, 0, 255], [255, 255, 0]],
        ],
        dtype=np.uint8,
    )
    mesh = _textured_mesh(
        [[-2, -1, 0], [2, -1, 0], [2, 1, 0], [-2, 1, 0]],
        [[0, 1, 2], [0, 2, 3]],
        # One full positive repeat also exercises GLB's default wrap mode.
        [[1, 1], [2, 1], [2, 2], [1, 2]],
        texture,
    )
    observation = MeshObservation(
        _export(trimesh.Scene(mesh), tmp_path / "metric.glb")
    )
    prefix = tmp_path / "views" / "top"
    image, metadata = observation.render(
        prefix,
        eye=[0, 0, 10],
        target=[0, 0, 0],
        width_m=4,
        height_m=2,
        width_px=40,
        height_px=20,
    )

    assert image.getpixel((5, 4)) == (255, 0, 0)
    assert image.getpixel((34, 4)) == (0, 255, 0)
    assert image.getpixel((5, 15)) == (0, 0, 255)
    assert image.getpixel((34, 15)) == (255, 255, 0)
    assert metadata["camera"]["up_reference"].startswith("+Y fallback")
    np.testing.assert_allclose(
        metadata["pixel_center_mapping"]["column_step_world_xyz"],
        [0.1, 0, 0],
        atol=1e-9,
    )

    query = observation.pixel_query(prefix, [[15, 10], [25, 10]])
    first, second = query["queries"]
    np.testing.assert_allclose(first["world_xyz"], [-0.45, -0.05, 0], atol=1e-6)
    np.testing.assert_allclose(second["world_xyz"], [0.55, -0.05, 0], atol=1e-6)
    assert query["first_two_distance"]["distance_m"] == pytest.approx(1.0)
    assert query["first_two_distance"]["same_triangle"] is False
    surface = first["triangle_surface_evidence"]
    assert surface["triangle_normal_xyz"][2] == pytest.approx(1)
    assert surface["plane_tilt_from_vertical_degrees"] == pytest.approx(90)
    assert surface["horizontal_surface_trace_direction_degrees"] is None
    assert (tmp_path / "views" / "top.png").is_file()
    assert (tmp_path / "views" / "top.json").is_file()
    assert (tmp_path / "views" / "top.npz").is_file()


def test_render_depth_uses_nearest_surface_and_global_face_id(tmp_path: Path) -> None:
    scene = trimesh.Scene()
    scene.add_geometry(_quad(0, (220, 0, 0)), node_name="far")
    scene.add_geometry(_quad(2, (0, 220, 0)), node_name="near")
    observation = MeshObservation(_export(scene, tmp_path / "depth.glb"))
    prefix = tmp_path / "depth"
    image, _ = observation.render(
        prefix,
        eye=[0, 0, 10],
        target=[0, 0, 0],
        width_m=4,
        height_m=2,
        width_px=40,
        height_px=20,
    )

    assert image.getpixel((20, 10)) == (0, 220, 0)
    hit = observation.pixel_query(prefix, [[20, 10]])["queries"][0]
    assert hit["world_xyz"][2] == pytest.approx(2.0)
    assert isinstance(hit["face_id"], int)
    assert 0 <= hit["face_id"] < 4


def test_base_color_factor_is_applied_in_linear_not_srgb_space(tmp_path: Path) -> None:
    mesh = _textured_mesh(
        [[-1, -1, 0], [1, -1, 0], [0, 1, 0]],
        [[0, 1, 2]],
        [[0, 0], [1, 0], [0.5, 1]],
        np.full((2, 2, 3), 255, dtype=np.uint8),
        base_color_factor=[64, 64, 64, 255],
    )
    observation = MeshObservation(_export(trimesh.Scene(mesh), tmp_path / "factor.glb"))
    image, _ = observation.render(
        tmp_path / "factor",
        eye=[0, 0, 5],
        target=[0, 0, 0],
        width_m=2,
        height_m=2,
        width_px=20,
        height_px=20,
    )
    # A linear factor of 64/255 encodes to about 137 in sRGB. Direct byte
    # multiplication would incorrectly produce 64 and visibly darken the mesh.
    assert image.getpixel((10, 10)) == (137, 137, 137)


def test_bounds_filter_is_centroid_selection_for_render(tmp_path: Path) -> None:
    mesh = _textured_mesh(
        [[-2, -1, 0], [0, -1, 0], [-1, 1, 0], [0, -1, 0], [2, -1, 0], [1, 1, 0]],
        [[0, 1, 2], [3, 4, 5]],
        [[0, 0], [1, 0], [0.5, 1], [0, 0], [1, 0], [0.5, 1]],
        np.full((2, 2, 3), (30, 80, 160), dtype=np.uint8),
    )
    observation = MeshObservation(_export(trimesh.Scene(mesh), tmp_path / "select.glb"))
    prefix = tmp_path / "selected"
    image, metadata = observation.render(
        prefix,
        eye=[0, 0, 5],
        target=[0, 0, 0],
        width_m=4,
        height_m=2,
        width_px=40,
        height_px=20,
        bounds=[[-3, -2, -1], [-0.01, 2, 1]],
    )
    assert metadata["selected_face_count"] == 1
    assert image.getpixel((10, 10)) != _BACKGROUND_FOR_TEST
    assert image.getpixel((30, 10)) == _BACKGROUND_FOR_TEST


def test_surface_direction_evidence_weights_vertical_faces_and_excludes_roof(
    tmp_path: Path,
) -> None:
    scene = trimesh.Scene()
    scene.add_geometry(
        _vertical_quad(14, origin=(0, 0), length=4, height=3), node_name="surface_14"
    )
    scene.add_geometry(
        _vertical_quad(16, origin=(20, 0), length=2, height=3), node_name="surface_16"
    )
    # Its much larger area must not dominate a near-vertical surface query.
    scene.add_geometry(_quad(5, (60, 80, 100)), node_name="horizontal_surface")
    observation = MeshObservation(_export(scene, tmp_path / "directions.glb"))

    evidence = observation.surface_direction_evidence(
        angle_bin_degrees=2,
        max_plane_tilt_degrees=10,
    )

    assert evidence["selected_triangle_count"] == 6
    assert evidence["eligible_triangle_count"] == 4
    assert evidence["excluded_triangle_counts"]["outside_plane_tilt_limit"] == 2
    assert evidence["eligible_surface_area_m2"] == pytest.approx(18)
    assert [item["angle_bin_center_degrees"] for item in evidence["candidates"]] == [
        14,
        16,
    ]
    assert evidence["candidates"][0]["surface_area_m2"] == pytest.approx(12)
    assert evidence["candidates"][0]["eligible_area_fraction"] == pytest.approx(2 / 3)
    assert evidence["candidates"][0][
        "area_weighted_direction_degrees"
    ] == pytest.approx(14)
    assert "do not make it a fitted wall" in evidence["evidence_caveat"]

    local = observation.surface_direction_evidence(
        bounds=[[-1, -1, -1], [6, 3, 4]], angle_bin_degrees=2
    )
    assert [item["angle_bin_center_degrees"] for item in local["candidates"]] == [14]
    assert local["selection_method"].startswith("triangle centroid")

    selected_face = evidence["candidates"][1]["face_id_sample"][0]
    by_face = observation.surface_direction_evidence(
        face_ids=[selected_face], angle_bin_degrees=2
    )
    assert by_face["selected_triangle_count"] == 1
    assert by_face["candidates"][0]["angle_bin_center_degrees"] == 16


def test_pixel_query_reports_operation_frame_triangle_normal_and_direction(
    tmp_path: Path,
) -> None:
    observation = MeshObservation(
        _export(
            trimesh.Scene(_vertical_quad(0, origin=(-2, 0), length=4, height=3)),
            tmp_path / "pixel_surface.glb",
        )
    )
    yaw_degrees = 14
    normal_angle = np.radians(yaw_degrees + 90)
    target = np.array([0, 0, 1.5])
    eye = target + 10 * np.array([np.cos(normal_angle), np.sin(normal_angle), 0])
    prefix = tmp_path / "vertical_view"
    observation.render(
        prefix,
        eye=eye.tolist(),
        target=target.tolist(),
        width_m=4,
        height_m=3,
        width_px=80,
        height_px=60,
        yaw_degrees=yaw_degrees,
    )

    hit = observation.pixel_query(prefix, [[40, 30]])["queries"][0]
    assert hit["hit"] is True
    surface = hit["triangle_surface_evidence"]
    assert surface["horizontal_surface_trace_direction_degrees"] == pytest.approx(14)
    assert surface["plane_tilt_from_vertical_degrees"] == pytest.approx(0)
    assert surface["triangle_area_m2"] == pytest.approx(6)
    assert surface["triangle_horizontal_span_m"] == pytest.approx(4)


def test_invalid_parameters_missing_buffer_and_asset_hash_are_rejected(
    tmp_path: Path,
) -> None:
    first = MeshObservation(
        _export(trimesh.Scene(_quad(0, (1, 2, 3))), tmp_path / "first.glb")
    )
    with pytest.raises(ValueError, match="distinct"):
        first.render(
            tmp_path / "bad",
            eye=[0, 0, 0],
            target=[0, 0, 0],
            width_m=1,
            height_m=1,
        )
    with pytest.raises(ValueError, match="at most 1600"):
        first.render(
            tmp_path / "bad",
            eye=[0, 0, 1],
            target=[0, 0, 0],
            width_m=1,
            height_m=1,
            width_px=1601,
        )
    with pytest.raises(FileNotFoundError, match="pixel buffer"):
        first.pixel_query(tmp_path / "missing", [[0, 0]])

    prefix = tmp_path / "owned"
    first.render(
        prefix,
        eye=[0, 0, 5],
        target=[0, 0, 0],
        width_m=4,
        height_m=2,
        width_px=20,
        height_px=10,
    )
    second = MeshObservation(
        _export(trimesh.Scene(_quad(0, (4, 5, 6))), tmp_path / "second.glb")
    )
    with pytest.raises(ValueError, match="different mesh asset"):
        second.pixel_query(prefix, [[10, 5]])
    with pytest.raises(ValueError, match="outside image bounds"):
        first.pixel_query(prefix, [[20, 5]])


def test_non_textured_geometry_fails_explicitly(tmp_path: Path) -> None:
    mesh = trimesh.creation.box()
    path = _export(trimesh.Scene(mesh), tmp_path / "plain.glb")
    with pytest.raises(ValueError, match="UV-mapped base-color texture"):
        MeshObservation(path)


def test_blend_texture_zero_fractional_and_full_alpha_preserve_measurement_semantics(tmp_path):
    foreground = _blend_quad(1)
    foreground.visual.material.baseColorTexture = Image.fromarray(np.array(
        [[[255, 0, 0, 0], [255, 0, 0, 128], [255, 0, 0, 255]]], dtype=np.uint8))
    mesh, prefix, image, metadata = _blend_view(tmp_path, [foreground, _quad(0, (0, 0, 255))], width=30)
    pixels = [[5, 20], [15, 20], [25, 20]]
    queries = mesh.pixel_query(prefix, pixels)['queries']
    assert image.getpixel(tuple(pixels[0])) == (0, 0, 255)
    assert image.getpixel(tuple(pixels[1])) == _expected_linear_over([((255, 0, 0), 128/255)], (0, 0, 255))
    assert image.getpixel(tuple(pixels[2])) == (255, 0, 0)
    assert queries[0]['hit'] and queries[0]['world_xyz'][2] == 0
    assert queries[2]['hit'] and queries[2]['world_xyz'][2] == 1
    assert queries[2]['triangle_surface_evidence']['material_alpha_mode'] == 'BLEND'
    assert queries[1]['reason'] == 'transparent_composite' and not queries[1]['background']
    assert queries[1]['measurement_valid'] is False and 'world_xyz' not in queries[1]
    assert mesh.pixel_query(prefix, pixels[:2])['first_two_distance']['available'] is False
    assert metadata['rasterizer']['transparent_composite_pixel_count'] == 400
    assert metadata['rasterizer']['measurement_valid_pixel_count'] == 800


@pytest.mark.parametrize('front_mode', ['OPAQUE', 'BLEND'])
@pytest.mark.parametrize('reverse_order', [False, True])
def test_fully_opaque_foreground_hides_partial_blend_without_invalidating_points(tmp_path, front_mode, reverse_order):
    foreground = _quad(2, (20, 200, 50)) if front_mode == 'OPAQUE' else _blend_quad(2, (20, 200, 50), 255)
    meshes = [foreground, _blend_quad(1), _quad(0, (0, 0, 255))]
    if reverse_order:
        meshes.reverse()
    mesh, prefix, image, metadata = _blend_view(tmp_path, meshes)
    query = mesh.pixel_query(prefix, [[20, 20]])['queries'][0]
    assert query['hit'] and query['measurement_valid'] and query['world_xyz'][2] == 2
    assert image.getpixel((20, 20)) == (20, 200, 50)
    assert metadata['rasterizer']['transparent_composite_pixel_count'] == 0


@pytest.mark.parametrize('reverse_winding', [False, True])
def test_shared_quad_diagonal_is_blended_once_and_alpha_factor_is_linear(tmp_path, reverse_winding):
    foreground = _blend_quad(1, alpha_factor=128)
    if reverse_winding:
        foreground.faces = foreground.faces[:, ::-1]
    mesh, prefix, image, metadata = _blend_view(tmp_path, [foreground, _quad(0, (0, 0, 255))])
    expected = _expected_linear_over([((255, 0, 0), (128/255)**2)], (0, 0, 255))
    # All 40 diagonal pixel centres sit on the shared triangle edge.
    assert all(image.getpixel((i, 39-i)) == expected for i in range(40))
    assert metadata['rasterizer']['blend']['statistics']['peak_pixel_layers'] == 1
    assert not mesh.pixel_query(prefix, [[20, 19]])['queries'][0]['hit']


@pytest.mark.parametrize('reverse_order', [False, True])
def test_crossing_blend_planes_sort_fragments_per_pixel_not_triangle_centroid(tmp_path, reverse_order):
    red, green = _blend_quad(0), _blend_quad(0, (0, 255, 0))
    # Source Y is public Z. These planes cross at x=0 and reverse depth order.
    red.vertices[:, 1] = red.vertices[:, 0]/4
    green.vertices[:, 1] = -green.vertices[:, 0]/4
    meshes = [red, green, _quad(-2, (0, 0, 255))]
    if reverse_order:
        meshes.reverse()
    mesh, prefix, image, _ = _blend_view(tmp_path, meshes)
    a = 128/255
    assert image.getpixel((5, 20)) == _expected_linear_over([((0, 255, 0), a), ((255, 0, 0), a)], (0, 0, 255))
    assert image.getpixel((35, 20)) == _expected_linear_over([((255, 0, 0), a), ((0, 255, 0), a)], (0, 0, 255))
    assert all(row['reason'] == 'transparent_composite' for row in mesh.pixel_query(prefix, [[5, 20], [35, 20]])['queries'])


@pytest.mark.parametrize('alpha,background', [(0, True), (128, False), (255, False)])
def test_blend_over_empty_background_never_invents_a_surface(tmp_path, alpha, background):
    mesh, prefix, image, _ = _blend_view(tmp_path, [_blend_quad(1, alpha=alpha)])
    query = mesh.pixel_query(prefix, [[20, 20]])['queries'][0]
    assert query.get('background', False) is background
    assert query['hit'] is (alpha == 255)
    if alpha == 128:
        assert query['reason'] == 'transparent_composite'
    assert image.getpixel((20, 20)) == _expected_linear_over([((255, 0, 0), alpha/255)], _BACKGROUND_FOR_TEST)


@pytest.mark.parametrize('budget', ['_MAX_BLEND_TILE_FRAGMENTS', '_MAX_BLEND_TOTAL_FRAGMENTS', '_MAX_BLEND_PIXEL_LAYERS'])
def test_blend_budget_failure_is_explicit_and_saves_no_partial_observation(tmp_path, monkeypatch, budget):
    from src.agent.geometry import mesh_observation
    monkeypatch.setattr(mesh_observation, budget, 1)
    mesh = MeshObservation(_export(trimesh.Scene([_blend_quad(1), _blend_quad(0)]), tmp_path/'asset.glb'))
    prefix = tmp_path/'refused'
    with pytest.raises(ValueError, match='BLEND .*budget exceeded'):
        mesh.render(prefix, eye=[0, 0, 10], target=[0, 0, 0], width_m=4, height_m=2, width_px=40, height_px=40)
    assert not any(prefix.with_suffix(suffix).exists() for suffix in ('.png', '.json', '.npz'))


@pytest.mark.parametrize('removed', ['measurement_contract', 'transparent_composite_indices'])
def test_blend_buffer_without_invalidity_contract_is_rejected(tmp_path, removed):
    mesh, prefix, _, _ = _blend_view(tmp_path, [_blend_quad(1)])
    with np.load(prefix.with_suffix('.npz'), allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files if key != removed}
    np.savez_compressed(prefix.with_suffix('.npz'), **arrays)
    with pytest.raises(ValueError, match='measurement contract and invalid indices'):
        mesh.pixel_query(prefix, [[20, 20]])


def test_mask_material_is_still_explicitly_unsupported(tmp_path):
    mesh = _blend_quad(1)
    mesh.visual.material.alphaMode = 'MASK'
    with pytest.raises(ValueError, match='unsupported material alpha mode'):
        MeshObservation(_export(trimesh.Scene(mesh), tmp_path/'mask.glb'))


def test_blend_tiles_have_no_missing_or_duplicated_seams(tmp_path):
    _, _, image, metadata = _blend_view(tmp_path, [_blend_quad(1), _quad(0, (0, 0, 255))], width=129, height=129)
    expected = _expected_linear_over([((255, 0, 0), 128/255)], (0, 0, 255))
    assert np.all(np.asarray(image) == np.asarray(expected))
    assert metadata['rasterizer']['transparent_composite_pixel_count'] == 129**2
    assert metadata['rasterizer']['blend']['statistics']['retained_fragments'] == 129**2


def test_blend_composites_unrounded_linear_opaque_backing(tmp_path):
    opaque = _quad(0, (255, 255, 255))
    opaque.visual.material.baseColorFactor = [64, 128, 192, 255]
    _, _, image, _ = _blend_view(tmp_path, [_blend_quad(1), opaque])
    alpha = 128/255
    linear = alpha*np.array([1, 0, 0])+(1-alpha)*np.array([64, 128, 192])/255
    expected = np.rint((1.055*linear**(1/2.4)-.055)*255).astype(int)
    np.testing.assert_array_equal(image.getpixel((20, 20)), expected)
