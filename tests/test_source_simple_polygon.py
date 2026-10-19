"""The source-only polygon contract preserves real contacts and saved geometry."""
import copy
import json
import math

import pytest
from shapely.geometry import Polygon

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import WallOpening
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.build import build_geometry
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_enclosure import apply_source_enclosure
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_plan_view import render_source_plan


def cell(identity, points):
    return dict(id=identity, role="office", polygon=points,
                x=[min(p[0] for p in points), max(p[0] for p in points)],
                y=[min(p[1] for p in points), max(p[1] for p in points)],
                source_refs=["test:declared"])


def proposal():
    return {"geometry": {
        "schema_version": "source_polygon_v1", "footprint_x": [0, 4], "footprint_y": [0, 4],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3,
                    "footprint": {"vertices": [[0, 0], [4, 0], [0, 4]]},
                    "cells": [cell("a", [[0, 0], [4, 0], [2, 2]]),
                              cell("b", [[0, 0], [2, 2], [0, 4]])]}],
        "openings": [
            {"id": "door", "kind": "door", "space_id": "a", "other_space_id": "b",
             "p1": [.5, .5], "p2": [1.5, 1.5], "z": [0, 2.1], "source_refs": ["test:shared-door"]},
            {"id": "window", "kind": "window", "space_id": "a", "other_space_id": None,
             "p1": [3.5, .5], "p2": [2.5, 1.5], "z": [1, 2], "source_refs": ["test:external-window"]},
        ],
    }, "assumptions": [], "unresolved": []}


def build(value=None):
    return build_source_bim(ensure_corrected_geometry((value or proposal())["geometry"]),
                            capability_profile="simple_polygon")


def test_diagonal_shared_wall_and_exterior_window_roundtrip(tmp_path):
    submitted = proposal()
    source = build(submitted)
    assert source["validation"]["status"] == "pass"
    assert len(source["boundary_relations"]) == 1
    relation = source["boundary_relations"][0]
    assert relation["regions"][0]["area_m2"] == pytest.approx(math.sqrt(8) * 3)
    assert len(source["opening_hosts"]["door"]) == 2
    assert len(source["opening_hosts"]["window"]) == 1
    assert [row["opening_id"] for row in source["connections"]] == ["door"]
    window = next(row for row in source["openings"] if row["id"] == "window")
    assert window["vertices"] == [[3.5, .5, 1], [2.5, 1.5, 1], [2.5, 1.5, 2], [3.5, .5, 2]]
    assert window["source_refs"] == ["test:external-window"]

    out = tmp_path / "saved"
    report = export_source_proposal(submitted, out)
    assert report["source_geometry_ready"], report
    saved = json.loads((out / "source_model.json").read_text(encoding="utf-8"))
    assert saved["source_model_sha256"] == _digest({k: v for k, v in saved.items() if k != "source_model_sha256"})
    assert saved["spaces"] == source["spaces"]
    assert saved["boundaries"] == source["boundaries"]
    assert saved["openings"] == source["openings"]
    assert saved["boundary_relations"] == json.loads(json.dumps(source["boundary_relations"]))
    display = json.loads((out / "display_geometry.json").read_text(encoding="utf-8"))
    assert display["windows"][0]["parent"] == window["host_boundary_id"]
    assert display["windows"][0]["verts"] == window["vertices"]
    _, plan = render_source_plan(saved, "F1")
    assert set(plan["opening_ids"]) == {"door", "window"}
    assert (out / "viewer.html").is_file()


@pytest.mark.parametrize("change", ["off_plane", "partly_outside", "wrong_neighbor", "false_exterior", "height", "duplicate"])
def test_diagonal_opening_rejection_preserves_unbuilt_evidence(change):
    value = proposal()
    opening = value["geometry"]["openings"][0]
    if change == "off_plane":
        opening["p2"] = [1.5, 1.6]
    elif change == "partly_outside":
        opening["p2"] = [2.5, 2.5]
    elif change == "wrong_neighbor":
        opening["other_space_id"] = "missing"
    elif change == "false_exterior":
        opening["other_space_id"] = None
    elif change == "height":
        opening["z"] = [0, 4]
    else:
        value["geometry"]["openings"].append(copy.deepcopy(opening))
    source = build(value)
    assert source["validation"]["status"] == "severe"
    assert any(row["id"] == "door" and row["record"]["source_refs"] == ["test:shared-door"]
               for row in source["unbuilt_openings"])


def test_new_profile_does_not_relax_drawing_or_ep_contracts():
    geom = ensure_corrected_geometry(proposal()["geometry"])
    with pytest.raises(ValueError, match="capability_profile"):
        build_source_bim(geom, capability_profile="orthogonal_polygon")
    with pytest.raises(ValueError, match="source BIM builder"):
        build_geometry(geom, capability_profile="simple_polygon")
    with pytest.raises(ValueError, match="orthogonal"):
        WallOpening.model_validate(proposal()["geometry"]["openings"][0])
    legacy = proposal()
    legacy["geometry"]["schema_version"] = "2"
    legacy["geometry"]["openings"] = []
    with pytest.raises(ValueError, match="non-orthogonal"):
        build_source_bim(ensure_corrected_geometry(legacy["geometry"]), capability_profile="simple_polygon")


def test_setback_contacts_only_actual_overlap_and_keeps_exposed_roof():
    value = proposal()
    value["geometry"]["floors"].append({
        "name": "F2", "z_floor": 3, "ceiling_height": 3,
        "footprint": {"vertices": [[0, 0], [2, 0], [0, 2]]},
        "cells": [cell("upper", [[0, 0], [2, 0], [0, 2]])],
    })
    source = build(value)
    assert source["validation"]["status"] == "pass"
    horizontal = [row for row in source["boundary_relations"] if "upper" in row["space_ids"]]
    assert len(horizontal) == 2
    assert sum(patch["area_m2"] for row in horizontal for patch in row["regions"]) == pytest.approx(2)
    roofs = [row for row in source["boundaries"] if row["space_id"] in {"a", "b"} and row["geometry_type"] == "ceiling"]
    total_roof = sum(Polygon([v[:2] for v in row["vertices"]]).area for row in roofs)
    assert total_roof == pytest.approx(8)
    assert total_roof - sum(p["area_m2"] for r in horizontal for p in r["regions"]) == pytest.approx(6)
    display = source_view_geometry(source)
    assert all(row["id"] in display["display_surface_parts"] for row in roofs)


def test_spanning_diagonal_space_does_not_create_middle_slab():
    value = proposal()
    value["geometry"]["openings"] = []
    lower = value["geometry"]["floors"][0]
    core = lower["cells"].pop()
    lower["spanning_space_ids"] = ["b"]
    upper = copy.deepcopy(lower)
    upper.update(name="F2", z_floor=3)
    upper["cells"][0]["id"] = "a2"
    value["geometry"]["floors"].extend([upper, {
        "name": "CORE", "z_floor": 0, "ceiling_height": 6,
        "footprint": {"vertices": core["polygon"]}, "cells": [core],
    }])
    source = build(value)
    assert source["validation"]["status"] == "pass"
    slabs = [row for row in source["boundaries"] if row["space_id"] == "b" and row["geometry_type"] != "wall"]
    assert len(slabs) == 2
    assert {v[2] for row in slabs for v in row["vertices"]} == {0, 6}
    assert len([row for row in source["spaces"] if row["id"] == "b"]) == 1


def test_polygon_horizontal_open_enclosure_is_reciprocal_and_persisted(tmp_path):
    value = proposal()
    upper = copy.deepcopy(value["geometry"]["floors"][0])
    upper.update(name="F2", z_floor=3)
    for room in upper["cells"]:
        room["id"] += "2"
    value["geometry"]["floors"].append(upper)
    base = build(value)
    bounds = {row["id"]: row for row in base["boundaries"]}
    ids = ["space/a/ceiling", "space/a2/floor"]
    declaration = {"schema_version": "source_enclosure_input_v1", "base_source_model_sha256": base["source_model_sha256"],
                   "boundaries": [{"boundary_id": bid, "condition": "open", "scope": "whole",
                                   "source_refs": ["test:atrium"], "evidence_kind": "observed"} for bid in ids]}
    with pytest.raises(ValueError, match="matching open declaration"):
        apply_source_enclosure(base, {**declaration, "boundaries": declaration["boundaries"][:1]})
    result = apply_source_enclosure(base, declaration)
    assert result["validation"]["status"] == "pass"
    assert all(row["kind"] == "virtual" for row in result["boundaries"] if row["id"] in ids)
    assert len(result["source_enclosure"]["open_connections"]) == 2
    assert all(row["vertices"] == bounds[row["id"]]["vertices"] for row in result["boundaries"])
    value["enclosure_declaration"] = declaration
    report = export_source_proposal(value, tmp_path / "atrium")
    assert report["source_geometry_ready"], report
    display = json.loads((tmp_path / "atrium" / "display_geometry.json").read_text(encoding="utf-8"))
    assert all(display["display_surface_parts"][bid] == [] for bid in ids)


def test_revision_preserves_triangle_hosts_and_allows_explicit_window_edit(tmp_path):
    original = proposal()
    operations = [
        {"op": "reshape_spaces", "spaces": [{"id": "a", "polygon": [[0, 0], [4, 0], [2, 2]]}],
         "reason": "retain measured triangle", "source_refs": ["test:verified"]},
        {"op": "update_opening", "id": "window", "changes": {"z": [1.1, 2.1]},
         "reason": "updated sill", "source_refs": ["test:updated-window"]},
        {"op": "add_opening", "opening": {"id": "new", "kind": "window", "space_id": "b",
         "other_space_id": None, "p1": [1.5, 2.5], "p2": [.5, 3.5], "z": [1, 2]},
         "reason": "observed glazing", "source_refs": ["test:new-window"]},
    ]
    revised = apply_proposal_edits(original, operations)
    assert "corrections" not in original["geometry"]
    report = export_source_proposal(revised, tmp_path / "revised")
    assert report["source_geometry_ready"], report
    saved = json.loads((tmp_path / "revised" / "source_model.json").read_text(encoding="utf-8"))
    assert len(saved["openings"]) == 3
    assert {v[2] for o in saved["openings"] if o["id"] == "window" for v in o["vertices"]} == {1.1, 2.1}
    assert len(saved["generation"]["corrections"]) == 3


def test_local_repartition_conserves_outer_shape_and_requires_new_door_host(tmp_path):
    original = proposal()
    reshaped = apply_proposal_edits(original, [{
        "op": "replace_space_region", "space_id": "a", "neighbor_space_id": "b",
        "polygon": [[0, 0], [4, 0], [1, 3]],
        "reason": "new observed partition", "source_refs": ["test:repartition"],
    }])
    failed = build(reshaped)
    assert failed["validation"]["status"] == "severe"
    assert [row["id"] for row in failed["unbuilt_openings"]] == ["door"]
    assert reshaped["geometry"]["openings"] == original["geometry"]["openings"]
    repaired = apply_proposal_edits(reshaped, [{
        "op": "update_opening", "id": "door",
        "changes": {"p1": [.2, .6], "p2": [.5, 1.5]},
        "reason": "door on observed revised partition", "source_refs": ["test:revised-door"],
    }])
    report = export_source_proposal(repaired, tmp_path / "repartition")
    assert report["source_geometry_ready"], report
    source = json.loads((tmp_path / "repartition" / "source_model.json").read_text(encoding="utf-8"))
    shapes = [Polygon(row["polygon"]) for row in source["spaces"]]
    assert shapes[0].intersection(shapes[1]).area == pytest.approx(0)
    assert shapes[0].union(shapes[1]).symmetric_difference(Polygon([[0, 0], [4, 0], [0, 4]])).area == pytest.approx(0)
    assert len(source["opening_hosts"]["door"]) == 2
    assert len(source["generation"]["corrections"]) == 2


def test_concave_polygon_preserves_reentrant_exterior_window(tmp_path):
    points = [[0, 0], [4, 0], [4, 4], [2, 2], [0, 4]]
    value = proposal()
    value["geometry"]["floors"][0].update(footprint={"vertices": points}, cells=[cell("a", points)])
    value["geometry"]["openings"] = [{
        "id": "notch", "kind": "window", "space_id": "a", "other_space_id": None,
        "p1": [3.5, 3.5], "p2": [2.5, 2.5], "z": [1, 2], "source_refs": ["test:reentrant-window"],
    }]
    report = export_source_proposal(value, tmp_path / "concave")
    assert report["source_geometry_ready"], report
    source = json.loads((tmp_path / "concave" / "source_model.json").read_text(encoding="utf-8"))
    assert Polygon(source["spaces"][0]["polygon"]).equals(Polygon(points))
    assert Polygon(source["spaces"][0]["polygon"]).area == pytest.approx(12)
    assert len([row for row in source["boundaries"] if row["geometry_type"] == "wall"]) == 5
    opening = source["openings"][0]
    assert opening["exterior"] and opening["vertices"][0] == [3.5, 3.5, 1]
    host = next(row for row in source["boundaries"] if row["id"] == opening["host_boundary_id"])
    assert {tuple(vertex[:2]) for vertex in host["vertices"]} == {(4, 4), (2, 2)}


@pytest.mark.parametrize("field", ["holes", "curves", "top_polygon"])
def test_unimplemented_shape_fields_fail_instead_of_disappearing(field):
    value = proposal()["geometry"]
    value["floors"][0]["cells"][0][field] = []
    with pytest.raises(ValueError, match="Extra inputs"):
        ensure_corrected_geometry(value)
