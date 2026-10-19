"""Contact caching must preserve the pre-optimization geometry byte for byte."""
import copy
from itertools import combinations
import math

import pytest
from shapely.geometry import Polygon

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import FootprintRing, SourceBoundary
from src.agent.geometry import source_bim as source


def _legacy_on_wall(vertices, boundary):
    origin = boundary.vertices[0][:2]
    direction, _ = source._frame(origin, boundary.vertices[1][:2])
    if any(abs((v[0]-origin[0])*direction[1] - (v[1]-origin[1])*direction[0]) > source.EPS
           for v in vertices):
        return None
    return source._project(vertices, origin, direction)


def _legacy_contacts(boundaries):
    """Frozen original pair loop; intentionally recomputes its invariants."""
    relations = []
    for a, b in combinations(sorted(boundaries.values(), key=lambda b: b.id), 2):
        if a.space_id == b.space_id:
            continue
        regions = []
        if a.geometry_type == b.geometry_type == "wall":
            if float(source._newell(a.vertices) @ source._newell(b.vertices)) > -1 + source.EPS:
                continue
            projected = _legacy_on_wall(b.vertices, a)
            if projected is None:
                continue
            contact = _legacy_on_wall(a.vertices, a).intersection(projected)
            origin = a.vertices[0][:2]
            direction, _ = source._frame(origin, a.vertices[1][:2])
            lift = lambda ring: source._lift(list(ring), origin, direction)
        elif {a.geometry_type, b.geometry_type} == {"floor", "ceiling"}:
            if abs(a.vertices[0][2] - b.vertices[0][2]) > source.EPS:
                continue
            contact = Polygon([v[:2] for v in a.vertices]).intersection(
                Polygon([v[:2] for v in b.vertices]))
            z = a.vertices[0][2]
            lift = lambda ring: [[x, y, z] for x, y in ring]
        else:
            continue
        for part in source._parts(contact):
            regions.append({"vertices": lift(list(part.exterior.coords)[:-1]),
                            "holes": [lift(list(r.coords)[:-1]) for r in part.interiors],
                            "area_m2": part.area})
        if not regions:
            continue
        a.adjacent_space_ids.append(b.space_id)
        b.adjacent_space_ids.append(a.space_id)
        a.counterpart_ids.append(b.id)
        b.counterpart_ids.append(a.id)
        relations.append({"boundary_ids": [a.id, b.id], "space_ids": [a.space_id, b.space_id],
                          "regions": regions})
    for boundary in boundaries.values():
        boundary.adjacent_space_ids = sorted(set(boundary.adjacent_space_ids))
        boundary.counterpart_ids = sorted(set(boundary.counterpart_ids))
    return relations


def _wall(identity, start=(0, 0), end=(4, 0), z=(0, 3), *, reverse=False, space=None):
    if reverse:
        start, end = end, start
    return SourceBoundary(id=identity, space_id=space or identity, kind="physical", geometry_type="wall",
                          vertices=[[*start, z[0]], [*end, z[0]], [*end, z[1]], [*start, z[1]]])


def _horizontal(identity, kind, points, z):
    return SourceBoundary(id=identity, space_id=identity, kind="physical", geometry_type=kind,
                          vertices=[[*point, z] for point in points])


def _assert_equivalent(boundaries):
    expected = copy.deepcopy(boundaries)
    actual = copy.deepcopy(boundaries)
    relations = _legacy_contacts(expected)
    assert source._contacts(actual) == relations
    assert {key: row.model_dump() for key, row in actual.items()} == {
        key: row.model_dump() for key, row in expected.items()}
    reversed_input = copy.deepcopy(dict(reversed(list(boundaries.items()))))
    assert source._contacts(reversed_input) == relations
    return relations


@pytest.mark.parametrize("angle", [0, math.pi/4, .371, math.pi/2])
@pytest.mark.parametrize("origin", [(0, 0), (8_000_000, -9_000_000)])
def test_wall_contact_cache_matches_original_for_nonorthogonal_cross_storey_pairs(angle, origin):
    dx, dy = math.cos(angle), math.sin(angle)

    def point(t, offset=0):
        return (origin[0] + t*dx - offset*dy, origin[1] + t*dy + offset*dx)

    walls = [
        _wall("a/whole", point(0), point(8), (0, 9)),
        _wall("b/low", point(1), point(4), (0, 3), reverse=True),
        _wall("c/middle", point(3), point(6), (3, 6), reverse=True),
        _wall("d/high", point(4), point(8), (6, 9), reverse=True),
        _wall("e/z_touch", point(1), point(4), (9, 12), reverse=True),
        _wall("f/edge_touch", point(8), point(10), (0, 3), reverse=True),
        _wall("g/disjoint", point(11), point(14), (0, 3), reverse=True),
        _wall("h/offset_inside_eps", point(1, source.EPS/4), point(4, source.EPS/4), reverse=True),
        _wall("i/offset_outside_eps", point(1, source.EPS*4), point(4, source.EPS*4), reverse=True),
        _wall("j/same_normal", point(0), point(8)),
        _wall("k/same_space", point(0), point(8), reverse=True, space="a/whole"),
    ]
    # A sloping upper edge remains in the same vertical wall plane.
    walls[2].vertices[2][2] = 5.5
    relations = _assert_equivalent({row.id: row for row in walls})
    pairs = {tuple(row["boundary_ids"]) for row in relations}
    assert ("a/whole", "b/low") in pairs
    assert ("a/whole", "c/middle") in pairs
    assert ("a/whole", "h/offset_inside_eps") in pairs
    assert not any("i/offset_outside_eps" in pair for pair in pairs)
    assert ("a/whole", "e/z_touch") not in pairs
    assert ("a/whole", "k/same_space") not in pairs


@pytest.mark.parametrize("height_delta", [0, source.EPS/2, source.EPS*2])
def test_horizontal_cache_keeps_coplanarity_multipart_and_zero_area_rules(height_delta):
    concave = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 3), (4, 3), (4, 4), (0, 4)]
    rows = [
        _horizontal("a", "ceiling", concave, 3),
        _horizontal("b", "floor", [(2, -1), (3, -1), (3, 5), (2, 5)], 3+height_delta),
        _horizontal("c/touch", "floor", [(4, 0), (5, 0), (5, 4), (4, 4)], 3),
        _horizontal("d/disjoint", "floor", [(5, 0), (6, 0), (6, 4), (5, 4)], 3),
        _horizontal("e/same_kind", "ceiling", concave, 3),
    ]
    relations = _assert_equivalent({row.id: row for row in rows})
    if height_delta <= source.EPS:
        assert len(relations) == 2
        assert all(len(row["regions"]) == 2 for row in relations)
    else:
        assert relations == []


def _geometry(case):
    if case == "diagonal":
        floors = []
        for i in range(2):
            floors.append({"name": f"F{i}", "z_floor": i*3, "ceiling_height": 3,
                           "footprint": {"vertices": [[0, 0], [4, 0], [0, 4]]},
                           "cells": [{"id": f"a{i}", "x": [0, 4], "y": [0, 2],
                                      "polygon": [[0, 0], [4, 0], [2, 2]]},
                                     {"id": f"b{i}", "x": [0, 2], "y": [0, 4],
                                      "polygon": [[0, 0], [2, 2], [0, 4]]}]})
        return ensure_corrected_geometry({
            "schema_version": "source_polygon_v1", "footprint_x": [0, 4], "footprint_y": [0, 4],
            "floors": floors, "openings": [{"id": "shared_door", "kind": "door", "space_id": "a0",
                "other_space_id": "b0", "p1": [.5, .5], "p2": [1.5, 1.5], "z": [0, 2.1],
                "source_refs": ["test:shared-wall"]}]}), "simple_polygon"
    floors = []
    for i in range(3):
        floors.append({"name": f"F{i}", "z_floor": i*3, "ceiling_height": 3,
                       "footprint": {"vertices": [[0, 0], [4, 0], [4, 4], [0, 4]]},
                       "spanning_space_ids": ["core"],
                       "cells": [{"id": f"a{i}", "x": [0, 3], "y": [0, 2]},
                                 {"id": f"b{i}", "x": [0, 3], "y": [2, 4]}]})
    floors.append({"name": "CORE", "z_floor": 0, "ceiling_height": 9,
                   "footprint": {"vertices": [[3, 0], [4, 0], [4, 4], [3, 4]]},
                   "cells": [{"id": "core", "role": "stairwell", "x": [3, 4], "y": [0, 4]}]})
    geometry = ensure_corrected_geometry({"schema_version": "2", "footprint_x": [0, 4],
                                          "footprint_y": [0, 4], "floors": floors})
    for floor in geometry.floors:
        floor.footprint = FootprintRing.model_validate(floor.footprint)
    return geometry, "orthogonal_polygon"


@pytest.mark.parametrize("case", ["diagonal", "spanning_core"])
def test_full_source_contacts_ids_opening_hosts_and_hash_exactly_match_legacy(case, monkeypatch):
    geometry, profile = _geometry(case)
    current = source.build_source_bim(geometry.model_copy(deep=True), capability_profile=profile)
    monkeypatch.setattr(source, "_contacts", _legacy_contacts)
    legacy = source.build_source_bim(geometry.model_copy(deep=True), capability_profile=profile)
    assert current["validation"]["status"] == "pass"
    assert current["boundary_relations"]
    assert current == legacy


def test_normal_computation_is_linear_and_cache_does_not_survive_a_call(monkeypatch):
    rows = []
    for i in range(20):
        rows.extend([_wall(f"{i:02d}/a", z=(i*3, i*3+3)),
                     _wall(f"{i:02d}/b", z=(i*3, i*3+3), reverse=True)])
    boundaries = {row.id: row for row in rows}
    original = source._newell
    count = 0

    def counted(vertices):
        nonlocal count
        count += 1
        return original(vertices)

    monkeypatch.setattr(source, "_newell", counted)
    expected = _legacy_contacts(copy.deepcopy(boundaries))
    legacy_count = count
    count = 0
    assert source._contacts(copy.deepcopy(boundaries)) == expected
    assert count == len(rows)
    assert legacy_count == len(rows)*(len(rows)-1)
    # Reusing the same IDs with new geometry must recompute, not reuse a cache.
    for vertex in boundaries["00/b"].vertices:
        vertex[1] += 1
    expected_changed = _legacy_contacts(copy.deepcopy(boundaries))
    assert expected_changed != expected
    assert source._contacts(copy.deepcopy(boundaries)) == expected_changed
