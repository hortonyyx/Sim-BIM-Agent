"""A wall shell void is absent geometry, including after contact subtraction."""
import copy

import pytest
from PIL import Image

from src.agent.geometry.mesh_bim_frame import render_mesh_bim_overlay
from src.agent.geometry.opening_review import _wall_exposure, facade_inventory
from src.agent.geometry.source_model import _digest


def wall_fixture():
    outer = [[0, 0, 0], [6, 0, 0], [6, 0, 3], [0, 0, 3]]
    hole = [[2, 0, 1], [4, 0, 1], [4, 0, 2], [2, 0, 2]]
    boundary = {"id": "wall", "space_id": "A", "geometry_type": "wall", "kind": "physical",
                "vertices": outer, "holes": [hole], "adjacent_space_ids": ["B"]}
    relation = {"boundary_ids": ["wall", "other"], "space_ids": ["A", "B"],
                "regions": [{"vertices": outer, "holes": [hole], "area_m2": 16}]}
    return boundary, relation


def test_fully_contacted_wall_with_hole_has_zero_exposed_area():
    boundary, relation = wall_fixture()
    exposed, contact, error = _wall_exposure(boundary, [relation])
    assert error is None
    assert exposed.area == 0
    assert contact.area == 16


def test_uncontacted_wall_area_excludes_its_void():
    boundary, _ = wall_fixture()
    boundary["adjacent_space_ids"] = []
    exposed, contact, error = _wall_exposure(boundary, [])
    assert error is None and contact.is_empty
    assert exposed.area == 16
    assert len(exposed.interiors) == 1


@pytest.mark.parametrize("contacted", [True, False])
def test_exterior_overlay_and_facade_inventory_never_restore_wall_void(contacted):
    boundary, relation = wall_fixture()
    if not contacted:
        boundary["adjacent_space_ids"] = []
    source = {
        "floors": [{"id": "F1"}],
        "spaces": [{"id": "A", "floor_id": "F1", "polygon": [[0, 0], [6, 0], [6, 2], [0, 2]]}],
        "boundaries": [boundary], "boundary_relations": [relation] if contacted else [], "openings": [],
        "mesh_frame": {"mesh_sha256": "a" * 64, "yaw_degrees": 0, "translation_m": [0, 0, 0],
                       "reason": "synthetic wall-hole regression", "source_refs": ["test:wall-hole"]}}
    source["source_model_sha256"] = _digest(source)
    before = copy.deepcopy(source)
    observation = {"mesh_sha256": "a" * 64, "resolution_px": {"width": 200, "height": 200},
        "source_coordinate_transform": {"yaw_degrees_counterclockwise_about_positive_z": 0},
        "camera": {"target": [3, 0, 1.5], "screen_right": [1, 0, 0], "screen_up": [0, 0, 1]},
        "view_span_m": {"width": 8, "height": 4}}
    _, record = render_mesh_bim_overlay(Image.new("RGB", (200, 200), "white"),
                                         observation, source, exterior_only=True)
    rows = [row for row in record["objects"] if row["id"] == "wall"]
    assert len(rows) == (0 if contacted else 1)
    if not contacted:
        assert len(rows[0]["hole_observation_xyz"]) == 1
    inventory = facade_inventory(source)
    # Fully contacted physical wall shell contributes no exterior facade.
    if contacted:
        assert not inventory["floors"][0]["facades"]
    assert source == before
