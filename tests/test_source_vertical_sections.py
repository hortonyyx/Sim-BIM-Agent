"""A stepped volume stays one source space, with its actual external shell."""
import math

import pytest
from shapely.geometry import Polygon

from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.modelling import _newell
from src.agent.geometry.source_schema import SourcePolygonGeometry
from src.agent.geometry.source_volume import space_polygon_at_z, space_volume, space_overlap_volume


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def stepped():
    outer, upper, hole = rect(0, 0, 10, 8), rect(3, 0, 10, 8), rect(5, 2, 6, 3)
    return {
        "schema_version": "source_polygon_v1", "footprint_x": [0, 10], "footprint_y": [0, 8],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3.5,
                    "footprint": {"vertices": outer}, "cells": [
                        {"id": "office", "role": "office", "x": [0, 10], "y": [0, 8],
                         "polygon": outer, "holes": [hole], "vertical_sections": [
                             {"z": [0, 1.5], "polygon": outer, "holes": [hole]},
                             {"z": [1.5, 3.5], "polygon": upper, "holes": [hole]}]},
                        {"id": "core", "role": "office", "x": [5, 6], "y": [2, 3], "polygon": hole}]}],
        "windows": [], "openings": []}


def build(data=None):
    return build_source_bim(SourcePolygonGeometry.model_validate(data or stepped()), capability_profile="simple_polygon")


def face_area(face):
    return Polygon([v[:2] for v in face["vertices"]], [[v[:2] for v in h] for h in face.get("holes", [])]).area


def test_stepped_space_one_identity_no_internal_slab_and_preserved_hole():
    source = build()
    assert source["validation"]["status"] == "pass"
    assert {s["id"] for s in source["spaces"]} == {"office", "core"}
    office = next(s for s in source["spaces"] if s["id"] == "office")
    assert len(office["vertical_sections"]) == 2
    assert space_volume(office) == pytest.approx(228.5)
    assert space_polygon_at_z(office, 1).area == 79
    assert space_polygon_at_z(office, 1.5).area == 55
    assert space_polygon_at_z(office, 4).is_empty
    caps = [b for b in source["boundaries"] if b["space_id"] == "office" and b["geometry_type"] != "wall"]
    assert [(b["geometry_type"], b["vertices"][0][2], face_area(b)) for b in caps] == [
        ("ceiling", 1.5, 24), ("ceiling", 3.5, 55), ("floor", 0, 79)]
    assert all(not (r["space_ids"][0] == r["space_ids"][1]) for r in source["boundary_relations"])
    assert set(source_view_geometry(source)["zones"]) == {"office", "core"}


def test_coplanar_walls_host_apertures_across_height_seam():
    data = stepped()
    data["openings"] = [
        {"id": "window", "kind": "window", "space_id": "office", "other_space_id": None,
         "p1": [4, 0], "p2": [8, 0], "z": [1, 2.5], "source_refs": ["test:window"]},
        {"id": "core-door", "kind": "door", "space_id": "office", "other_space_id": "core",
         "p1": [5, 2.2], "p2": [5, 2.8], "z": [.5, 2.5], "source_refs": ["test:door"]}]
    source = build(data)
    assert source["validation"]["status"] == "pass"
    assert not source["unbuilt_openings"]
    assert len(source["opening_hosts"]["window"]) == 1
    assert len(source["opening_hosts"]["core-door"]) == 2
    for boundary in source["boundaries"]:
        if boundary["geometry_type"] == "wall":
            assert boundary["vertices"][0][:2] != boundary["vertices"][1][:2]


def test_empty_upper_setback_does_not_create_volume_overlap():
    data = stepped()
    projection = rect(0, 0, 3, 8)
    data["floors"].append({"name": "Terrace", "z_floor": 1.5, "ceiling_height": 2,
        "footprint": {"vertices": projection}, "cells": [
            {"id": "terrace", "role": "office", "x": [0, 3], "y": [0, 8], "polygon": projection}]})
    source = build(data)
    assert source["validation"]["status"] == "pass"
    spaces = {s["id"]: s for s in source["spaces"]}
    assert space_overlap_volume(spaces["office"], spaces["terrace"]) == 0
    assert any(set(r["space_ids"]) == {"office", "terrace"} for r in source["boundary_relations"])
    data["floors"][-1]["z_floor"] = 1
    conflict = build(data)
    finding = next(f for f in conflict["validation"]["findings"] if f["code"] == "source.space_overlap")
    assert finding["volume_m3"] == pytest.approx(12)


@pytest.mark.parametrize("change,message", [
    ("gap", "contiguous"), ("overlap", "contiguous"), ("unsorted", "contiguous"),
    ("top", "full source floor height"), ("projection", "projection"),
    ("disconnected", "positive area"), ("invalid", "valid and nonempty")])
def test_invalid_section_volume_fails_explicitly(change, message):
    data = stepped()
    cell = data["floors"][0]["cells"][0]
    rows = cell["vertical_sections"]
    if change == "gap": rows[1]["z"][0] = 1.6
    if change == "overlap": rows[1]["z"][0] = 1.4
    if change == "unsorted": rows.reverse()
    if change == "top": rows[1]["z"][1] = 4
    if change == "projection": rows[0]["polygon"] = rect(1, 0, 10, 8)
    if change == "disconnected":
        rows[1]["polygon"], rows[1]["holes"] = rect(10, 0, 12, 8), []
    if change == "invalid": rows[1]["polygon"] = [[0, 0], [10, 8], [0, 8], [10, 0]]
    with pytest.raises(ValueError, match=message):
        build(data)


def test_legacy_prism_output_has_no_sections_and_retains_boundary_ids():
    data = stepped()
    del data["floors"][0]["cells"][0]["vertical_sections"]
    source = build(data)
    assert all("vertical_sections" not in s for s in source["spaces"])
    assert "space/office/floor" in {b["id"] for b in source["boundaries"]}
    assert "space/office/ceiling" in {b["id"] for b in source["boundaries"]}
    assert source["validation"]["status"] == "pass"


@pytest.mark.parametrize("angle,offset", [(0, 0), (31, 0), (31, 1000000)])
def test_closed_shell_volume_under_rotation_and_large_translation(angle, offset):
    data = stepped()
    sine, cosine = math.sin(math.radians(angle)), math.cos(math.radians(angle))
    def transform(ring):
        return [[x*cosine-y*sine+offset, x*sine+y*cosine-offset] for x, y in ring]
    floor = data["floors"][0]
    floor["footprint"]["vertices"] = transform(floor["footprint"]["vertices"])
    for cell in floor["cells"]:
        cell["polygon"] = transform(cell["polygon"])
        cell["holes"] = [transform(h) for h in cell.get("holes", [])]
        cell["x"] = [min(p[0] for p in cell["polygon"]), max(p[0] for p in cell["polygon"])]
        cell["y"] = [min(p[1] for p in cell["polygon"]), max(p[1] for p in cell["polygon"])]
        for row in cell.get("vertical_sections", []):
            row["polygon"] = transform(row["polygon"])
            row["holes"] = [transform(h) for h in row["holes"]]
    source = build(data)
    assert source["validation"]["status"] == "pass"
    signed_volume = 0
    for boundary in source["boundaries"]:
        if boundary["space_id"] != "office":
            continue
        # Translate near origin for a stable divergence-theorem volume.
        points = [[p[0]-offset, p[1]+offset, p[2]] for p in boundary["vertices"]]
        normal = _newell(points)
        if boundary["geometry_type"] == "wall":
            direction = [(points[1][i]-points[0][i])/math.dist(points[0][:2], points[1][:2]) for i in (0, 1)]
            project = lambda ring: [[(p[0]-offset-points[0][0])*direction[0] + (p[1]+offset-points[0][1])*direction[1], p[2]] for p in ring]
            area = Polygon(project(boundary["vertices"]), [project(h) for h in boundary.get("holes", [])]).area
        else:
            area = face_area(boundary)
        signed_volume += sum(normal[i]*points[0][i] for i in range(3))*area/3
    assert signed_volume == pytest.approx(228.5, abs=1e-6)


def test_wall_hole_from_middle_recess_rejects_aperture_through_missing_surface():
    data = stepped()
    floor = data["floors"][0]
    floor["cells"] = [floor["cells"][0]]
    cell = floor["cells"][0]
    cell["holes"] = []
    cell["vertical_sections"] = [
        {"z": [0, 1], "polygon": rect(0, 0, 10, 8)},
        {"z": [1, 2], "polygon": [[0, 0], [4, 0], [4, 2], [6, 2], [6, 0], [10, 0], [10, 8], [0, 8]]},
        {"z": [2, 3.5], "polygon": rect(0, 0, 10, 8)}]
    data["openings"] = [{"id": "bad", "kind": "window", "space_id": "office", "other_space_id": None,
                         "p1": [3, 0], "p2": [7, 0], "z": [.5, 2.5], "source_refs": ["test:bad"]}]
    source = build(data)
    assert [o["id"] for o in source["unbuilt_openings"]] == ["bad"]
    assert any(b.get("holes") for b in source["boundaries"] if b["geometry_type"] == "wall")
    assert source_view_geometry(source)["display_surface_parts"]
