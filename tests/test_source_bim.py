"""Source geometry must not depend on a solver's surface partition."""

import copy
import json
from pathlib import Path

import pytest
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, box

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import CorrectedGeometry, WallOpening
from src.agent.geometry.source_bim import EPS, build_source_bim, source_view_geometry, _on_wall, _space_overlap_area
from src.agent.geometry.source_model import _digest

ROOT = Path(__file__).resolve().parents[1]


def three_rooms():
    return CorrectedGeometry.model_validate({
        "schema_version": "2", "footprint_x": [0,6], "footprint_y": [0,6],
        "floors": [{"name":"F1", "z_floor":0, "ceiling_height":3, "cells":[
            {"id":"hall", "x":[0,3], "y":[0,6]},
            {"id":"a", "x":[3,6], "y":[0,3]},
            {"id":"b", "x":[3,6], "y":[3,6]}]}],
        "windows":[{"id":"w", "floor":"F1", "facade":"West", "span":[1,2], "z":[1,2], "room":"hall"}],
        "openings":[{"id":"door", "kind":"door", "space_id":"hall", "other_space_id":"a",
                     "p1":[3,1], "p2":[3,2], "z":[0,2.1], "source_refs":["test:explicit"]}]
    })


def test_long_boundary_contacts_multiple_rooms_without_splitting_or_ep(monkeypatch):
    import src.agent.geometry.build as build
    import src.agent.geometry.split_pairing as pairing
    import src.agent.geometry.to_idf as idf
    import src.validator.interzone as interzone

    def forbidden(*args, **kwargs):
        pytest.fail("source BIM invoked the EP geometry path")

    monkeypatch.setattr(build, "pair_surfaces", forbidden)
    monkeypatch.setattr(pairing, "pair_surfaces", forbidden)
    monkeypatch.setattr(build, "build_geometry", forbidden)
    monkeypatch.setattr(idf, "building_to_idf", forbidden)
    monkeypatch.setattr(interzone, "validate_interzone_surface_pairs", forbidden)
    source = build_source_bim(three_rooms(), capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "pass"
    hall_walls = [b for b in source["boundaries"] if b["space_id"] == "hall" and b["geometry_type"] == "wall"]
    assert len(hall_walls) == 4
    shared = next(b for b in hall_walls if b["adjacent_space_ids"] == ["a", "b"])
    assert len(shared["counterpart_ids"]) == 2
    assert source["opening_hosts"]["door"][0] == shared["id"]
    assert len(source["connections"]) == 1
    assert source["connections"][0]["space_ids"] == ["hall", "a"]
    assert not any(k in json.dumps(source) for k in ('"obc"', '"obc_obj"', '"construction"', '"derived"'))
    display = source_view_geometry(source)
    assert len(display["surfaces"]) == 18


@pytest.mark.parametrize("other,p1,p2", [("b",[3,1],[3,2]), (None,[3,1],[3,2]), ("a",[3,2],[3,4])])
def test_wrong_or_partial_door_connection_stays_unbuilt(other,p1,p2):
    geom=three_rooms()
    geom.openings[0] = WallOpening.model_validate({**geom.openings[0].model_dump(), "other_space_id":other,"p1":p1,"p2":p2})
    source=build_source_bim(geom,capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "severe"
    assert [o["id"] for o in source["unbuilt_openings"]] == ["door"]
    unbuilt = source["unbuilt_openings"][0]
    if other is None:
        assert unbuilt["reason"] == "opening does not connect the declared spaces/outdoors over its full area"
        assert "height_host_diagnostic" not in unbuilt
    else:
        assert unbuilt["reason"] == "opening requires one complete source boundary on each declared side"
        assert all(side["within_declared_space_z_bounds"]
                   for side in unbuilt["height_host_diagnostic"]["declared_space_bounds"])
    assert not source["connections"]
    assert len(source["openings"]) == 1
    assert len(source_view_geometry(source)["surfaces"]) == 18


def test_upper_floor_relative_opening_height_has_diagnostic_but_absolute_height_builds():
    geom = three_rooms()
    upper = geom.floors[0].model_copy(deep=True)
    upper.name, upper.z_floor = "F2", 3
    for cell in upper.cells:
        cell.id += "_up"
    geom.floors.append(upper)
    base = {"id": "upper-door", "kind": "door", "space_id": "hall_up", "other_space_id": "a_up",
            "p1": [3, 1], "p2": [3, 2], "z": [0, 2.1], "source_refs": ["test:explicit"]}
    geom.openings = [WallOpening.model_validate(base)]

    relative = build_source_bim(geom, capability_profile="orthogonal_polygon")
    unbuilt = relative["unbuilt_openings"][0]
    assert unbuilt["reason"] == "opening requires one complete source boundary on each declared side"
    diagnostic = unbuilt["height_host_diagnostic"]
    assert diagnostic["actual_opening_z_bounds_m"] == [0.0, 2.1]
    assert [(side["space_id"], side["expected_z_bounds_m"], side["below_floor_m"])
            for side in diagnostic["declared_space_bounds"]] == [
                ("hall_up", [3.0, 6.0], 3.0), ("a_up", [3.0, 6.0], 3.0)]
    assert not any(side["within_declared_space_z_bounds"] for side in diagnostic["declared_space_bounds"])
    finding = relative["validation"]["findings"][0]
    assert finding["reason"] == unbuilt["reason"]
    assert finding["height_host_diagnostic"] == diagnostic

    geom.openings[0] = WallOpening.model_validate({**base, "z": [3, 5.1]})
    absolute = build_source_bim(geom, capability_profile="orthogonal_polygon")
    assert absolute["validation"]["status"] == "pass"
    assert not absolute["unbuilt_openings"]
    assert absolute["opening_hosts"]["upper-door"]


@pytest.mark.parametrize("change", ["floor", "internal", "too_wide", "duplicate", "overlap"])
def test_window_identity_floor_host_and_overlap_are_checked(change):
    geom=three_rooms()
    if change=="floor": geom.windows[0].floor="F2"
    if change=="internal": geom.windows[0].facade="East"
    if change=="too_wide": geom.windows[0].span=[-1,7]
    if change in {"duplicate","overlap"}:
        other=geom.windows[0].model_copy(deep=True)
        if change=="overlap": other.id="w2"
        geom.windows.append(other)
    source=build_source_bim(geom,capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "severe"


def test_short_edges_and_layer_contacts_do_not_trigger_ep_minimum():
    geom=three_rooms()
    geom.floors[0].cells[1].y=[0,.065]
    geom.floors[0].cells[2].y=[.065,6]
    geom.openings=[]
    upper=geom.floors[0].model_copy(deep=True)
    upper.name="F2";upper.z_floor=3
    for cell in upper.cells: cell.id+="_up"
    geom.floors.append(upper)
    source=build_source_bim(geom,capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "pass"
    assert len(source["spaces"])==6 and len(source["boundaries"])==36
    assert sum(r["space_ids"]==["a","a_up"] for r in source["boundary_relations"])==1


def test_missing_room_and_overlapping_room_block_source_geometry():
    geom=three_rooms();geom.floors[0].cells.pop()
    assert any(f["code"]=="source.floor_coverage" for f in build_source_bim(geom,capability_profile="orthogonal_polygon")["validation"]["findings"])
    geom=three_rooms();geom.floors[0].cells[2].y=[2,6]
    assert any(f["code"]=="source.space_overlap" for f in build_source_bim(geom,capability_profile="orthogonal_polygon")["validation"]["findings"])


@pytest.mark.parametrize("angle,offset", [(0, 0), (31, 0), (0, 1e6), (31, 1e6)])
@pytest.mark.parametrize("width,conflict", [(-EPS / 2, False), (EPS / 4, False), (2 * EPS, True), (0.001, True)])
def test_space_overlap_uses_length_tolerance_under_rotation_and_translation(angle, offset, width, conflict):
    # A very long boundary must not turn a microscopic seam into a severe
    # conflict; even a two-EPS true intrusion remains a conflict.
    a, b = box(0, 0, 3, 100), box(3 - width, 0, 6, 100)
    a, b = [translate(rotate(p, angle, origin=(0, 0)), offset, -offset) for p in (a, b)]
    originals = a.wkb, b.wkb
    result = _space_overlap_area(a, b)
    assert bool(result) == conflict
    if conflict:
        assert result == a.intersection(b).area
    reversed_result = _space_overlap_area(b, a)
    assert bool(reversed_result) == conflict
    assert reversed_result == pytest.approx(result)
    assert (a.wkb, b.wkb) == originals


@pytest.mark.parametrize("duplicate", [False, True])
def test_contained_or_duplicate_narrow_space_is_still_a_conflict(duplicate):
    narrow = box(0, 0, EPS / 4, 10)
    other = narrow if duplicate else box(-1, -1, 1, 11)
    assert narrow.buffer(-EPS / 2).is_empty
    assert _space_overlap_area(narrow, other) == narrow.area
    assert _space_overlap_area(other, narrow) == narrow.area


def test_nonconvex_and_corner_intrusions_are_not_hidden_by_small_contact_regions():
    a = box(0, 0, 4, 4)
    corner = Polygon([(3.999, 4), (4, 3.999), (5, 5)])
    nonconvex = Polygon([(3.999, 0), (5, 0), (5, 5), (0, 5), (0, 3.999), (3.999, 3.999)])
    for b in (corner, nonconvex):
        assert _space_overlap_area(a, b) == a.intersection(b).area > 0


@pytest.mark.parametrize("half_width,depth,conflict", [(EPS / 8, 20 * EPS, False), (2 * EPS, 3 * EPS, True)])
def test_short_corner_and_sharp_wedge_use_interior_radius_not_maximum_depth(half_width, depth, conflict):
    a = box(-1, 0, 1, 1)
    wedge = Polygon([(-2 * half_width, -depth), (2 * half_width, -depth), (0, depth)])
    # The first wedge extends far beyond EPS but has no numerically resolved
    # interior. The second small triangle has enough interior and must block.
    intersection = a.intersection(wedge)
    assert intersection.area > EPS ** 2
    assert bool(_space_overlap_area(a, wedge)) == conflict


@pytest.mark.parametrize("width,conflict", [(EPS / 4, False), (2 * EPS, True)])
def test_source_overlap_filter_preserves_geometry_and_original_volume(width, conflict):
    geom = three_rooms()
    geom.windows, geom.openings = [], []
    geom.floors[0].cells[2].y = [3 - width, 6]
    original = geom.model_dump(mode="json")
    source = build_source_bim(geom, capability_profile="orthogonal_polygon")
    overlap = [f for f in source["validation"]["findings"] if f["code"] == "source.space_overlap"]
    assert bool(overlap) == conflict
    if conflict:
        assert overlap[0]["volume_m3"] == pytest.approx((3 - (3 - width)) * 3 * 3)
    assert geom.model_dump(mode="json") == original
    spaces = {s["id"]: s for s in source["spaces"]}
    assert set(spaces) == {"hall", "a", "b"}
    assert spaces["b"]["polygon"] == [[3, 3 - width], [6, 3 - width], [6, 6], [3, 6]]


@pytest.mark.parametrize("offset", [2e7, 2e8, 1e9])
def test_source_overlap_does_not_disappear_at_large_world_coordinates(offset):
    geom = CorrectedGeometry.model_validate({
        "schema_version": "2",
        "footprint_x": [offset, offset + 6],
        "footprint_y": [-offset, 100 - offset],
        "floors": [{
            "name": "F1", "z_floor": 0, "ceiling_height": 3,
            "cells": [
                {"id": "a", "x": [offset, offset + 3], "y": [-offset, 100 - offset]},
                {"id": "b", "x": [offset + 2, offset + 6], "y": [-offset, 100 - offset]},
            ],
        }],
    })
    original = geom.model_dump(mode="json")
    source = build_source_bim(geom, capability_profile="orthogonal_polygon")
    overlaps = [f for f in source["validation"]["findings"] if f["code"] == "source.space_overlap"]
    assert source["validation"]["status"] == "severe"
    assert len(overlaps) == 1
    assert overlaps[0]["space_ids"] == ["a", "b"]
    assert overlaps[0]["volume_m3"] == 300
    assert geom.model_dump(mode="json") == original
    assert [s["polygon"] for s in source["spaces"]] == [
        [[offset, -offset], [offset + 3, -offset], [offset + 3, 100 - offset], [offset, 100 - offset]],
        [[offset + 2, -offset], [offset + 6, -offset], [offset + 6, 100 - offset], [offset + 2, 100 - offset]],
    ]


@pytest.mark.parametrize("z_overlap,conflict", [(EPS / 4, False), (2 * EPS, True)])
def test_identical_footprints_in_adjacent_storeys_keep_length_tolerance_in_z(z_overlap, conflict):
    geom = three_rooms()
    geom.windows, geom.openings = [], []
    upper = geom.floors[0].model_copy(deep=True)
    upper.name, upper.z_floor = "F2", 3 - z_overlap
    for cell in upper.cells:
        cell.id += "_up"
    geom.floors.append(upper)
    source = build_source_bim(geom, capability_profile="orthogonal_polygon")
    overlaps = [f for f in source["validation"]["findings"] if f["code"] == "source.space_overlap"]
    assert len(overlaps) == (3 if conflict else 0)


def test_display_preserves_full_boundary_area_holes_and_partial_contact_visibility():
    from src.agent.correction.schema import SourceBoundary
    geom=three_rooms()
    source=build_source_bim(geom,capability_profile="orthogonal_polygon")
    display=source_view_geometry(source)
    for boundary in source["boundaries"]:
        if boundary["geometry_type"] != "wall": continue
        b=SourceBoundary.model_validate(boundary)
        full=_on_wall(b.vertices,b).area
        holes=sum(_on_wall(o["vertices"],b).area for o in source["openings"] if b.id in source["opening_hosts"][o["id"]])
        parts=display["display_surface_parts"][b.id]
        displayed=sum(_on_wall(p["verts"],b).area-sum(_on_wall(h,b).area for h in p["holes"]) for p in parts)
        assert displayed == pytest.approx(full-holes)
    assert any(p["duplicate_at_rest"] for parts in display["display_surface_parts"].values() for p in parts)
    assert source["source_model_sha256"]==_digest({k:v for k,v in source.items() if k!="source_model_sha256"})
    changed=copy.deepcopy(source);changed["spaces"][0]["height"]+=1
    with pytest.raises(ValueError,match="digest"): source_view_geometry(changed)






def test_export_source_bim_is_separate_readonly_and_rejects_overwrite(tmp_path):
    from src.agent.execution.source_bim import export_source_bim
    out=tmp_path/"bim"
    run = tmp_path / "input"
    input_file = run / "1_correction/attempts/001/output.json"
    input_file.parent.mkdir(parents=True)
    input_file.write_text(three_rooms().model_dump_json(), encoding="utf-8")
    original = input_file.read_bytes()
    report=export_source_bim(run,out,capability_profile="orthogonal_polygon", candidate_attempt=1)
    assert report["source_geometry_ready"]
    assert report["drawing_fidelity"]=="not_evaluated"
    assert report["model_calls"]==report["solver_calls"]==0
    assert (out/"viewer.html").exists() and (out/"source_model.json").exists()
    assert not (out/"_run/geometry_approval.json").exists()
    assert input_file.read_bytes() == original
    with pytest.raises(FileExistsError): export_source_bim(run,out,capability_profile="orthogonal_polygon", candidate_attempt=1)
    bad=export_source_bim(tmp_path/"missing",tmp_path/"failure",capability_profile="rectangular")
    assert bad["status"]=="error" and not bad["source_geometry_ready"]
