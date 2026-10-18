import asyncio
import copy
import hashlib
import json
import pytest

from PIL import Image, ImageDraw
from shapely.geometry import Polygon

from src.agent.geometry.plan_dimension_alignment import align_plan_to_dimensions
from src.agent.geometry.plan_ink_alignment import align_plan_to_ink, move_straight_wall
from src.agent.runtime_roles.plan_format import plan_format_errors
from src.agent.runtime_roles import trial as trial_module
from src.agent.runtime_roles.trial import PlanTrial


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_dimension_tick_points_match_axis_scalars_without_rewriting_declarations(axis):
    from src.agent.geometry.plan_dimension_alignment import _chain_shape
    scalar = {'id': 'ticks', 'axis': axis, 'segments_mm': [1000, 1000],
              'tick_pixels': [20, 30, 40], 'source_refs': ['fixture']}
    points = copy.deepcopy(scalar)
    points['tick_pixels'] = [[v, 87] if axis == 'x' else [87, v] for v in scalar['tick_pixels']]
    original = copy.deepcopy(points)
    assert _chain_shape(points, 0) == _chain_shape(scalar, 0)
    assert points == original
    plan = _plan()
    plan['dimension_chains'] = [points]
    assert not any('dimension_chains' in e['path'] for e in plan_format_errors(plan))
    points['tick_pixels'][1] = [True, 30]
    assert _chain_shape(points, 0)[1] is not None


def _plan():
    return {
        "floor_id": "F1",
        "z_floor": 0.0,
        "ceiling_height": 3.0,
        "x_anchors": [[20, 0.0], [200, 18.0]],
        "y_anchors": [[20, 14.0], [160, 0.0]],
        "basis": "synthetic reader alignment fixture",
        "footprint_pixels": [[22, 22], [198, 22], [198, 158], [22, 158]],
        "partitions": [
            {"id": "P1", "points": [[109, 22], [109, 158]], "source_refs": ["plan.png: divider"]},
            {"id": "P2", "points": [[22, 89], [109, 89]], "source_refs": ["plan.png: junction"]},
        ],
        "openings": [
            {"id": "D1", "kind": "door", "p1": [109, 74], "p2": [109, 106],
             "z": [0.0, 2.1], "source_refs": ["plan.png: door"], "state": "unknown"},
            {"id": "PASS1", "kind": "open", "p1": [109, 89], "p2": [109, 120],
             "z": [0.0, 3.0], "source_refs": ["plan.png: open passage"]},
        ],
        "space_seeds": [],
        "assumptions": [],
        "unresolved": [],
    }


def _drawing(with_ink=True):
    image = Image.new("RGB", (220, 180), "white")
    if with_ink:
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 200, 160), outline="black", width=2)
        draw.line((108, 20, 108, 75), fill="black")
        draw.line((108, 105, 108, 160), fill="black")
        draw.line((112, 20, 112, 75), fill="black")
        draw.line((112, 105, 112, 160), fill="black")
        draw.line((100, 75, 120, 75), fill="black")
        draw.line((100, 105, 120, 105), fill="black")
        draw.line((20, 88, 110, 88), fill="black")
        draw.line((20, 92, 110, 92), fill="black")
    return image


def test_ink_alignment_moves_wall_attached_opening_and_junction_to_supported_lines():
    aligned, report = align_plan_to_ink(_drawing(), _plan())

    assert aligned["footprint_pixels"] == [[20.0, 20.0], [200.0, 20.0],
                                            [200.0, 160.0], [20.0, 160.0]]
    assert aligned["partitions"][0]["points"] == [[110.0, 20.0], [110.0, 160.0]]
    assert aligned["partitions"][1]["points"][-1] == [110.0, 90.0]
    assert aligned["openings"][0]["p1"] == [110.0, 75.0]
    assert aligned["openings"][0]["p2"] == [110.0, 105.0]
    assert aligned["openings"][1]["p1"] == [110.0, 90.0]
    assert aligned["openings"][1]["p2"] == [110.0, 120.0]
    passage_item = next(row for row in report["items"] if row["object"] == "opening:PASS1")
    assert passage_item["action"] == "not_moved" and "passages" in passage_item["reason"]
    assert report["search"]["world_radius_m"] == 0.30
    assert report["summary"]["rejected"] == 0
    assert any(row["object"] == "partition:P1" and row["action"] == "moved"
               for row in report["items"])
    assert aligned["regularization_inputs"]["line_references"][0]["basis"] == "ink"


def test_wall_move_assigns_one_exact_coordinate_to_every_attached_endpoint():
    value = _plan()
    old_cross = value["partitions"][0]["points"][0][0]
    target = 0.1

    moved = move_straight_wall(
        value,
        collection="partitions",
        identity="P1",
        along_axis="y",
        old_cross=old_cross,
        new_cross=target,
        span=[22, 158],
    )

    coordinates = [
        *(point[0] for point in value["partitions"][0]["points"]),
        value["partitions"][1]["points"][-1][0],
        *(opening[field][0] for opening in value["openings"] for field in ("p1", "p2")),
    ]
    assert coordinates and all(coordinate == target for coordinate in coordinates)
    assert moved["junctions"] == ["P2:1"]
    assert moved["openings"] == ["D1", "PASS1"]


def test_dimension_move_does_not_amplify_near_perpendicular_junction_gap():
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [100, 2.0]],
        "y_anchors": [[0, 0.0], [100, 2.0]],
        "basis": "near-junction regression",
        "footprint_pixels": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "partitions": [
            {"id": "horizontal", "points": [[10, 50], [60, 50]],
             "source_refs": ["plan.png: horizontal divider"]},
            {"id": "vertical", "points": [[60.05, 50], [60.05, 90]],
             "source_refs": ["plan.png: near perpendicular junction"]},
        ],
        "openings": [], "space_seeds": [], "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "Y_OVERALL", "axis": "y", "segments_mm": [1100, 900],
            "total_mm": 2000, "tick_pixels": [0, 55, 100],
            "source_refs": ["plan.png: overall y chain"],
        }],
    }

    aligned, report = align_plan_to_dimensions(value)

    rows = {row["id"]: row["points"] for row in aligned["partitions"]}
    assert rows["horizontal"][-1] == [60.0, 55.0]
    assert rows["vertical"][0] == [60.05, 55.0]
    assert rows["vertical"][1][0] == rows["vertical"][0][0]
    change = next(row for row in report["changes"] if row["object"] == "partition:horizontal")
    assert change["moved_with_wall"]["junctions"] == ["vertical:0"]
    assert Polygon(aligned["footprint_pixels"]).is_valid


def test_dimension_move_does_not_drag_parallel_gap_endpoint_into_diagonal():
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [100, 2.0]],
        "y_anchors": [[0, 0.0], [100, 2.0]],
        "basis": "parallel near-gap regression",
        "footprint_pixels": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "partitions": [
            {"id": "a", "points": [[10, 50], [40, 50]],
             "source_refs": ["plan.png: first horizontal divider"]},
            {"id": "b", "points": [[40.05, 50], [80, 50]],
             "source_refs": ["plan.png: second horizontal divider"]},
        ],
        "openings": [], "space_seeds": [], "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "Y_OVERALL", "axis": "y", "segments_mm": [1100, 900],
            "total_mm": 2000, "tick_pixels": [0, 55, 100],
            "source_refs": ["plan.png: overall y chain"],
        }],
    }

    aligned, report = align_plan_to_dimensions(value)

    rows = {row["id"]: row["points"] for row in aligned["partitions"]}
    assert rows == {
        "a": [[10, 55.0], [40, 55.0]],
        "b": [[40.05, 55.0], [80, 55.0]],
    }
    first_change = next(row for row in report["changes"] if row["object"] == "partition:a")
    assert first_change["moved_with_wall"]["junctions"] == []
    assert all(p1[1] == p2[1]
               for points in rows.values() for p1, p2 in zip(points, points[1:]))


def test_dimension_move_keeps_split_collinear_footprint_orthogonal():
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [100, 10.0]],
        "y_anchors": [[0, 10.0], [100, 0.0]],
        "basis": "split perimeter regression",
        "footprint_pixels": [
            [0, 0], [100, 0], [100, 100], [60, 100], [60, 80],
            [60, 60], [60, 60], [60, 20], [0, 20],
        ],
        "partitions": [], "openings": [], "space_seeds": [],
        "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "X_OVERALL", "axis": "x", "segments_mm": [5950, 4050],
            "total_mm": 10000, "tick_pixels": [0, 59.5, 100],
            "source_refs": ["plan.png: overall x chain"],
        }],
    }

    aligned, report = align_plan_to_dimensions(value)

    assert [point[0] for point in aligned["footprint_pixels"][3:8]] == [59.5] * 5
    assert all(first[0] == second[0] or first[1] == second[1]
               for first, second in zip(
                   aligned["footprint_pixels"],
                   [*aligned["footprint_pixels"][1:], aligned["footprint_pixels"][0]],
               ))
    assert Polygon(aligned["footprint_pixels"]).is_valid
    assert not report["rejections"]


@pytest.mark.parametrize("would_shrink", [False, True])
def test_dimension_tick_ownership_and_step_guard_preserve_distinct_perimeter_lines(would_shrink):
    value = {
        "floor_id": "F2", "z_floor": 4.0, "ceiling_height": 3.6,
        "x_anchors": [[0, 0.0], [100, 10.0]],
        "y_anchors": [[0, 12.0], [120, 0.0]],
        "basis": "short perimeter step regression",
        "footprint_pixels": [
            [0, 0], [94.24, 0], [94.24, 50], [92.86, 50],
            [92.86, 100], [100, 100], [100, 120], [0, 120],
        ],
        "partitions": [], "openings": [], "space_seeds": [],
        "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "X_WITH_STEP", "axis": "x", "segments_mm": [9290, 710],
            "total_mm": 10000, "tick_pixels": [0, 92.9, 100],
            "source_refs": ["plan.png: overall x chain"],
        }],
    }
    if would_shrink:
        # This tick uniquely belongs to the outer step, but its annotated
        # position would shrink the connecting edge below the kernel minimum.
        value["dimension_chains"][0].update(
            segments_mm=[9350, 650], tick_pixels=[0, 94.3, 100])

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["footprint_pixels"][1:4] == [
        [94.24, 0], [94.24, 50], [92.86 if would_shrink else 92.9, 50],
    ]
    assert aligned["footprint_pixels"][2][0] - aligned["footprint_pixels"][3][0] >= 1.0
    if would_shrink:
        rejection = next(row for row in report["rejections"]
                         if row.get("object") == "footprint:1")
        assert rejection["reason"] == "would shrink footprint edge 2 below min_edge_length_m 0.100000 m"
    else:
        association = next(row for row in report["items"]
                           if row.get("object") == "footprint:1")
        assert association["action"] == "not_applied"
        assert association["nearest_wall_pixels"] == [92.86]
        assert association["reason"] == "nearest tick is closer to another observed wall coordinate"
        assert not report["rejections"]
    assert Polygon(aligned["footprint_pixels"]).is_valid


def test_reader_preserves_declared_geometry_across_different_ink(tmp_path):
    original = _plan()
    original["openings"] = []
    results = []
    for with_ink in (False, True):
        image_path = tmp_path / f"plan-{with_ink}.png"
        _drawing(with_ink=with_ink).save(image_path)
        effective, reading, alignment = trial_module._reading_align(
            original, image_path=image_path, image_name=image_path.name, grid_step_m=.1)
        assert reading["ink"]["status"] == "not_applied"
        assert alignment["status"] == "lite_grid_applied"
        results.append(effective)
    for field in ("footprint_pixels", "partitions", "x_anchors", "y_anchors"):
        assert results[0][field] == results[1][field] == original[field]


def test_dimensions_bind_once_to_original_ticks_independent_of_chain_order():
    original = _plan()
    original.update(x_anchors=[[0, 0], [1000, 10]], y_anchors=[[0, 0], [300, 3]],
                    footprint_pixels=[[0, 0], [1000, 0], [1000, 300], [0, 300]],
                    partitions=[{"id": "wall", "points": [[500, 0], [500, 300]],
                                 "source_refs": ["drawing: wall"]},
                                {"id": "branch", "points": [[0, 150], [500, 150]],
                                 "source_refs": ["drawing: junction"]}],
                    openings=[{"id": "door", "kind": "door", "p1": [500, 30], "p2": [500, 120],
                               "z": [0, 2.1], "source_refs": ["drawing: door"]}])
    wall_chain = {"id": "wall-chain", "axis": "x", "segments_mm": [5000, 5000],
                  "tick_pixels": [0, 500, 1000], "source_refs": ["drawing: wall chain"]}
    jamb_chain = {"id": "jamb-chain", "axis": "x", "segments_mm": [5200, 4800],
                  "tick_pixels": [0, 520, 1000], "source_refs": ["drawing: window jamb chain"]}
    for chains in ([wall_chain, jamb_chain], [jamb_chain, wall_chain]):
        effective, report = align_plan_to_dimensions({**original, "dimension_chains": chains}, grid_step_m=.1)
        assert not report["rejections"]
        assert effective["partitions"] == original["partitions"]
        assert effective["openings"] == original["openings"]
        moves = [r for r in report["changes"] if r["object"] == "partition:wall"]
        assert len(moves) == 1 and moves[0]["chain_id"] == "wall-chain"
        assert moves[0]["observed_wall_pixel"] == moves[0]["observed_tick_pixel"] == 500


def _competing_wall_plan():
    return {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [100, 10.0]],
        "y_anchors": [[0, 0.0], [100, 10.0]],
        "basis": "independent wall coordinates near one dimension tick",
        "footprint_pixels": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "partitions": [
            {"id": "upper", "points": [[50.5, 0], [50.5, 40]]},
            {"id": "lower", "points": [[52, 60], [52, 100]]},
        ],
        "openings": [
            {"id": "upper-door", "kind": "door", "p1": [50.5, 10], "p2": [50.5, 20],
             "z": [0.0, 2.1], "source_refs": ["drawing: upper door"]},
            {"id": "lower-door", "kind": "door", "p1": [52, 70], "p2": [52, 80],
             "z": [0.0, 2.1], "source_refs": ["drawing: lower door"]},
        ],
        "space_seeds": [], "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "lower-wall", "axis": "x", "segments_mm": [5300, 4700],
            "tick_pixels": [0, 52, 100], "source_refs": ["drawing: lower wall dimensions"],
        }],
    }


def test_tick_does_not_capture_different_wall_or_fall_back_to_farther_tick():
    value = _competing_wall_plan()
    value["dimension_chains"].append({
        "id": "detail", "axis": "x", "segments_mm": [4900, 5100],
        "tick_pixels": [0, 48.5, 100], "source_refs": ["drawing: detailed dimensions"],
    })
    for chains in (value["dimension_chains"], list(reversed(value["dimension_chains"]))):
        original = {**value, "dimension_chains": chains}
        preserved = copy.deepcopy(original)
        aligned, report = align_plan_to_dimensions(original)

        assert original == preserved
        assert aligned["partitions"][0] == value["partitions"][0]
        assert aligned["openings"][0] == value["openings"][0]
        assert aligned["partitions"][1]["points"] == [[53.0, 60], [53.0, 100]]
        assert aligned["openings"][1]["p1"] == [53.0, 70]
        association = next(row for row in report["items"] if row.get("object") == "partition:upper")
        assert association["observed_wall_pixel"] == 50.5
        assert association["observed_tick_pixel"] == 52
        assert association["nearest_wall_pixels"] == [52.0]
        assert association["reason"] == "nearest tick is closer to another observed wall coordinate"
        assert report["summary"]["wall_associations_not_applied"] == 1
        assert not report["rejections"]
        audit = aligned["regularization_inputs"]["reading_alignment"]["dimensions"]["items"]
        assert association in audit
        assert not any(row.get("partition_id") == "upper"
                       for row in aligned["regularization_inputs"].get("line_references", []))


def test_tick_between_distinct_equidistant_wall_lines_preserves_both():
    value = _competing_wall_plan()
    value["partitions"][0]["points"] = [[50, 0], [50, 40]]
    value["openings"][0].update(p1=[50, 10], p2=[50, 20])
    value["dimension_chains"][0].update(tick_pixels=[0, 51, 100], segments_mm=[5150, 4850])

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["partitions"] == value["partitions"]
    assert aligned["openings"] == value["openings"]
    associations = [row for row in report["items"] if row.get("kind") == "wall_tick_association"]
    assert len(associations) == 2
    assert all(row["nearest_wall_pixels"] == [50.0, 52.0] for row in associations)
    assert all(row["reason"] == "nearest tick has ambiguous wall coordinates" for row in associations)
    assert not report["rejections"]


def test_tick_nearest_partition_does_not_move_nearby_footprint_step():
    value = _competing_wall_plan()
    value["footprint_pixels"] = [[0, 0], [100, 0], [100, 100], [49, 100], [49, 50], [0, 50]]
    value["partitions"] = value["partitions"][:1]
    value["openings"] = value["openings"][:1]
    value["dimension_chains"][0].update(tick_pixels=[0, 50.5, 100], segments_mm=[5100, 4900])

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["footprint_pixels"] == value["footprint_pixels"]
    assert aligned["partitions"][0]["points"] == [[51.0, 0], [51.0, 40]]
    assert aligned["openings"][0]["p1"] == [51.0, 10]
    association = next(row for row in report["items"] if row.get("object") == "footprint:3")
    assert association["nearest_wall_pixels"] == [50.5]
    assert not report["rejections"]


def test_unmovable_polyline_segment_still_reserves_its_nearest_tick():
    value = _competing_wall_plan()
    value["partitions"][1]["points"].append([75, 100])

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["partitions"] == value["partitions"]
    assert aligned["openings"] == value["openings"]
    association = next(row for row in report["items"] if row.get("object") == "partition:upper")
    assert association["nearest_wall_pixels"] == [52.0]
    assert not report["rejections"]


def test_unassociated_wall_only_uses_normal_grid_in_complete_reading_preparation():
    value = _competing_wall_plan()
    value["partitions"].extend([
        {"id": "cross-upper", "points": [[0, 40], [100, 40]]},
        {"id": "cross-lower", "points": [[0, 60], [100, 60]]},
    ])
    for wall in value["partitions"]:
        wall["source_refs"] = ["drawing: wall"]
    dimensioned, _ = align_plan_to_dimensions(value, grid_step_m=.1)

    aligned, reading, alignment = trial_module._reading_align(
        value, image_path=None, image_name="plan.png", grid_step_m=.1)

    assert dimensioned["partitions"][0]["points"][0][0] == 50.5
    assert dimensioned["openings"][0]["p1"][0] == 50.5
    # The unrelated tick remains available as a coordinate reference, but it
    # cannot recapture this wall during the existing common 10 cm grid pass.
    assert aligned["partitions"][0]["points"] == [[51.0, 0.0], [51.0, 40.0]]
    assert aligned["openings"][0]["p1"][0] == 51.0
    assert aligned["partitions"][1]["points"] == [[53.0, 60.0], [53.0, 100.0]]
    assert aligned["openings"][1]["p1"][0] == 53.0
    assert reading["lite_bim"]["status"] == "pass"
    assert reading["lite_bim"]["topology"]["status"] == "preserved"
    assert alignment["status"] == "lite_grid_applied"


@pytest.mark.parametrize("tamper", ["changed", "missing"])
def test_failed_dimensions_preserve_stage_and_original_numeric_input(tmp_path, tamper):
    workspace = tmp_path / "trial"
    (workspace / "images").mkdir(parents=True)
    _drawing().save(workspace / "images" / "plan.png")
    value = _plan()
    value["dimension_chains"] = [{"id": "bad-sum", "axis": "x", "segments_mm": [9000, 9000],
                                  "total_mm": 19000, "tick_pixels": [20, 110, 200],
                                  "source_refs": ["drawing: original printed chain"]}]
    trial = PlanTrial(None, image_name="plan.png", workspace=workspace,
                      receipt_directory=workspace / "trial_receipts")
    envelope = asyncio.run(trial.call(value))
    receipt = trial.receipts[-1]
    assert receipt["status"] == "failed" and receipt["failure_stage"] == "dimension_alignment"
    assert receipt["reading_alignment"]["dimensions"]["rejections"][0]["chain_id"] == "bad-sum"
    assert receipt["unaligned_numeric_input_file"] and receipt["failed_stage_input_file"]
    for path_field, hash_field in (("unaligned_numeric_input_file", "unaligned_numeric_input_sha256"),
                                   ("failed_stage_input_file", "failed_stage_input_sha256")):
        raw = (workspace / receipt[path_field]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt[hash_field]
    assert json.loads((workspace / receipt["unaligned_numeric_input_file"]).read_bytes())["dimension_chains"] == value["dimension_chains"]
    assert envelope["structuredContent"]["failure_stage"] == "dimension_alignment"
    assert receipt["compiled_numeric_plan_file"] is None
    assert "Operations edit the saved original" in envelope["structuredContent"]["repair_coordinate_basis"]
    image_sha = hashlib.sha256((workspace / "images" / "plan.png").read_bytes()).hexdigest()
    (workspace / "inputs.json").write_text(json.dumps({"images": {"plan.png": {"sha256": image_sha}}}), encoding="utf-8")
    event = {"receipt_file": receipt["receipt_file"], "plan_sha256": receipt["plan_sha256"]}
    assert trial.verified_failed_draft(image_sha, **event)[1]["validation_passed"] is False
    failed_path = workspace / receipt["failed_stage_input_file"]
    if tamper == "changed":
        failed_path.write_text("{}", encoding="utf-8")
    else:
        failed_path.unlink()
    with pytest.raises(ValueError, match="failed-stage input changed"):
        PlanTrial(None, image_name="plan.png", workspace=workspace, receipt_directory=workspace / "trial_receipts")
    with pytest.raises(ValueError, match="failed-stage input changed"):
        trial.verified_failed_draft(image_sha, **event)
    with pytest.raises(ValueError, match="failed-stage input changed"):
        trial.read_draft(field="report:/alignment")


def _dimension_rejection_trial(tmp_path, *, disconnected=False):
    (tmp_path / "images").mkdir()
    _drawing().save(tmp_path / "images/plan.png")
    value = _plan()
    value["partitions"] = [{"id": "divider", "points": [[110, 32 if disconnected else 22], [110, 158]],
                            "source_refs": ["drawing: divider"]}]
    value["openings"] = []
    value["dimension_chains"] = [{
        "id": "bad-sum", "axis": "x", "segments_mm": [9000, 9000],
        "total_mm": 19000, "tick_pixels": [20, 110, 200], "source_refs": ["drawing: chain"],
    }]
    return value, PlanTrial(None, image_name="plan.png", workspace=tmp_path,
                           receipt_directory=tmp_path / "trial_receipts",
                           profile_directory=tmp_path / "pixel_profiles")


@pytest.mark.parametrize("referenced_coordinates", [False, True])
def test_dimension_failure_also_reports_same_original_literal_topology(tmp_path, referenced_coordinates):
    value, trial = _dimension_rejection_trial(tmp_path, disconnected=True)
    if referenced_coordinates:
        value["x_anchors"][0][1] = {"value": 0, "unit": "mm"}
        value["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
        trial.profile_directory.mkdir()
        (trial.profile_directory / "profile_001.json").write_text(json.dumps({
            "name": "plan.png", "axis": "x",
            "image_sha256": hashlib.sha256((tmp_path / "images/plan.png").read_bytes()).hexdigest(),
            "candidates": [{"id": "C01", "pixels": [20, 20], "peak": 20}],
        }), encoding="utf-8")
    original = copy.deepcopy(value)

    result = asyncio.run(trial.call(value))

    receipt = trial.receipts[-1]
    assert result["isError"] and receipt["status"] == "failed"
    assert receipt["failure_stage"] == "dimension_alignment"
    assert "segment sum does not close" in receipt["reason"]
    assert receipt["regularization_failure"]["rejections"][0]["chain_id"] == "bad-sum"
    assert receipt["candidate"] is None and not receipt["source_geometry_ready"]
    assert not list(tmp_path.glob("reading_*")) and not list(tmp_path.glob("candidate_*"))
    diagnostic = receipt["literal_geometry_diagnostic"]
    assert diagnostic["status"] == "rejected" and "polygonize produced dangles" in diagnostic["reason"]
    assert diagnostic["input_file"] == receipt["unaligned_numeric_input_file"]
    assert diagnostic["input_sha256"] == receipt["unaligned_numeric_input_sha256"]
    assert diagnostic["omitted_reader_fields"] == ["dimension_chains"]
    raw_numeric = (tmp_path / diagnostic["input_file"]).read_bytes()
    assert hashlib.sha256(raw_numeric).hexdigest() == diagnostic["input_sha256"]
    numeric = json.loads(raw_numeric)
    assert numeric["x_anchors"][0] == [20, 0]
    assert numeric["partitions"] == original["partitions"]
    assert value == original
    assert json.loads((tmp_path / receipt["input_plan_file"]).read_bytes()) == original
    assert diagnostic["loose_partition_ends"][0]["partition"] == "divider"
    repair = diagnostic["repair_hint"]["junction_repairs"][0]
    assert repair["original_endpoint_pixel"] == [110, 32]
    visible = result["structuredContent"]
    assert visible["literal_geometry_diagnostic"] == diagnostic
    assert repair["path"] in visible["next_action"]["objects"]
    assert "primary dimension error" in visible["next_action"]["instruction"]
    pointer = visible["audit_refs"]["literal_geometry_diagnostic"]["json_pointer"]
    recovered = trial.read_draft(field="report:" + pointer + "/repair_hint")
    assert recovered["value"] == diagnostic["repair_hint"]
    assert len(json.dumps(visible, ensure_ascii=False).encode()) <= 24_000


def test_dimension_failure_with_valid_original_has_no_literal_problem(tmp_path):
    value, trial = _dimension_rejection_trial(tmp_path)
    receipt = asyncio.run(trial.run(value))
    assert receipt["status"] == "failed" and receipt["failure_stage"] == "dimension_alignment"
    assert "literal_geometry_diagnostic" not in receipt


def test_dimension_topology_rejection_does_not_blame_valid_original(tmp_path):
    value, trial = _dimension_rejection_trial(tmp_path)
    trial.grid_step_m = None
    value.update(x_anchors=[[0, 0.0], [100, 10.0]], y_anchors=[[0, 12.0], [120, 0.0]],
                 footprint_pixels=[[0, 0], [94.24, 0], [94.24, 50], [92.86, 50],
                                   [92.86, 100], [100, 100], [100, 120], [0, 120]],
                 partitions=[], dimension_chains=[{
                     "id": "step", "axis": "x", "segments_mm": [9350, 650],
                     "tick_pixels": [0, 94.3, 100], "source_refs": ["drawing: step"],
                 }])
    receipt = asyncio.run(trial.run(value))
    assert receipt["status"] == "failed" and receipt["failure_stage"] == "dimension_alignment"
    assert "would shrink footprint edge" in receipt["reason"]
    assert "literal_geometry_diagnostic" not in receipt


def test_secondary_diagnostic_exception_preserves_dimension_failure(tmp_path, monkeypatch):
    value, trial = _dimension_rejection_trial(tmp_path, disconnected=True)
    def fail_diagnostic(*args, **kwargs):
        raise RuntimeError("diagnostic failed independently")
    monkeypatch.setattr(trial_module, "_literal_geometry_diagnostic", fail_diagnostic)
    receipt = asyncio.run(trial.run(value))
    assert receipt["status"] == "failed" and receipt["failure_stage"] == "dimension_alignment"
    assert "segment sum does not close" in receipt["reason"]
    assert "diagnostic failed" not in receipt["reason"]
    assert receipt["literal_geometry_diagnostic"]["status"] == "unavailable"
    assert receipt["literal_geometry_diagnostic"]["error_type"] == "RuntimeError"


def test_unresolved_profile_does_not_run_literal_dimension_diagnostic(tmp_path, monkeypatch):
    value, trial = _dimension_rejection_trial(tmp_path, disconnected=True)
    value["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
    monkeypatch.setattr(trial_module, "_literal_geometry_diagnostic",
                        lambda *args, **kwargs: pytest.fail("original pixels are not resolved"))
    receipt = asyncio.run(trial.run(value))
    assert receipt["status"] == "failed" and "existing profile_id" in receipt["reason"]
    assert "literal_geometry_diagnostic" not in receipt


def test_missing_ink_never_moves_geometry_and_is_explicit():
    original = _plan()
    aligned, report = align_plan_to_ink(_drawing(with_ink=False), original)

    assert aligned["footprint_pixels"] == original["footprint_pixels"]
    assert aligned["partitions"] == original["partitions"]
    assert aligned["openings"] == original["openings"]
    assert report["status"] == "unchanged"
    assert any(row["action"] == "not_moved" and "no sufficiently long" in row["reason"]
               for row in report["items"] if row["kind"] in {"perimeter", "partition"})


def test_low_resolution_scan_never_applies_a_candidate_beyond_world_radius():
    image = Image.new("RGB", (12, 8), "black")
    draw = ImageDraw.Draw(image)
    for x in (5, 7):
        draw.line((x, 0, x, 7), fill=(128, 128, 128))
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [10, 6.0]],
        "y_anchors": [[0, 0.0], [6, 4.0]],
        "basis": "low resolution physical-radius regression",
        "footprint_pixels": [[0, 0], [10, 0], [10, 7], [0, 7]],
        "partitions": [{"id": "P1", "points": [[6, 1], [6, 7]],
                        "source_refs": ["plan.png: divider"]}],
        "openings": [], "space_seeds": [], "assumptions": [], "unresolved": [],
    }

    aligned, report = align_plan_to_ink(image, value)

    assert aligned["partitions"][0]["points"] == [[6, 1], [6, 7]]
    assert all(abs(row.get("movement_m", 0.0)) <= 0.30
               for row in report["items"] if row.get("action") == "moved")
    partition = next(row for row in report["items"] if row["object"] == "partition:P1")
    assert partition["action"] == "not_moved" and "physical search radius" in partition["reason"]


def test_overall_dimension_chain_sets_outer_face_axis_and_overrides_ink_priority():
    ink_aligned, _ = align_plan_to_ink(_drawing(), _plan())
    ink_aligned["dimension_chains"] = [{
        "id": "X_OVERALL", "axis": "x", "segments_mm": [9000, 9000],
        "total_mm": 18000, "tick_pixels": [20, 110, 200],
        "source_refs": ["plan.png: lower overall chain"],
    }]
    assert plan_format_errors(ink_aligned) == []

    aligned, report = align_plan_to_dimensions(ink_aligned)

    assert "dimension_chains" not in aligned
    assert aligned["x_anchors"] == [[20.0, 0.0], [200.0, 18.0]]
    assert aligned["partitions"][0]["points"][0][0] == 110.0
    line_ref = next(row for row in aligned["regularization_inputs"]["line_references"]
                    if row["partition_id"] == "P1")
    assert line_ref["basis"] == "dimension"
    coordinates = aligned["regularization_inputs"]["coordinate_references"]
    assert [row["value_m"] for row in coordinates] == [0.0, 9.0, 18.0]
    assert report["summary"]["chains_supplied"] == 1
    assert report["summary"]["chains_applied"] == 1
    assert report["summary"]["axes_calibrated"] == 1
    assert report["summary"]["partitions_snapped"] == 1
    assert report["summary"]["footprint_edges_snapped"] == 0
    assert report["summary"]["rejected"] == 0
    assert report["items"][0]["origin_basis"] == "legacy_anchor"
    assert report["items"][0]["anchor_cross_check"]["origin_basis"] == "legacy_anchor"
    assert report["items"][0]["anchor_cross_check"]["status"] == "consistent"
    audit = aligned["regularization_inputs"]["reading_alignment"]
    assert set(audit) == {"schema_version", "ink", "dimensions"}
    assert set(audit["dimensions"]) == {"summary", "items", "rejections", "search_or_tolerance"}


def test_dimension_chain_nonzero_origin_sets_both_axis_endpoints_and_audits_old_anchors():
    value = _plan()
    value["dimension_chains"] = [{
        "id": "X_WITH_ORIGIN", "axis": "x", "segments_mm": [9000, 9000],
        "total_mm": 18000, "tick_pixels": [22, 110, 198], "start_world_m": 125.5,
        "source_refs": [
            "plan.png: overall x dimension 9000/9000",
            "task coordinate contract: first west tick is world x=125.5 m",
        ],
    }]

    assert plan_format_errors(value) == []
    aligned, report = align_plan_to_dimensions(value)

    assert aligned["x_anchors"] == [[22.0, 125.5], [198.0, 143.5]]
    assert [row["value_m"] for row in aligned["regularization_inputs"]["coordinate_references"]] == [
        125.5, 134.5, 143.5,
    ]
    item = report["items"][0]
    assert item["origin_basis"] == "dimension_chain"
    cross_check = item["anchor_cross_check"]
    assert cross_check["origin_basis"] == "dimension_chain"
    assert cross_check["old_start_world_m"] == 0.2
    assert cross_check["dimension_start_world_m"] == 125.5
    assert cross_check["old_end_world_m"] == 17.8
    assert cross_check["dimension_end_world_m"] == 143.5
    assert cross_check["start_difference_m"] == 125.3
    assert cross_check["end_difference_m"] == 125.7
    assert cross_check["status"] == "warning"


def test_dimension_chain_origin_follows_first_tick_when_pixels_run_in_reverse():
    value = _plan()
    value["dimension_chains"] = [{
        "id": "Y_SOUTH_TO_NORTH", "axis": "y", "segments_mm": [7000, 7000],
        "total_mm": 14000, "tick_pixels": [158, 90, 22], "start_world_m": 0.0,
        "source_refs": [
            "plan.png: overall y dimension 7000/7000",
            "task coordinate contract: first south tick is world y=0 m and y increases northward",
        ],
    }]

    assert plan_format_errors(value) == []
    aligned, report = align_plan_to_dimensions(value)

    assert aligned["y_anchors"] == [[158.0, 0.0], [22.0, 14.0]]
    coordinates = aligned["regularization_inputs"]["coordinate_references"]
    assert [row["value_m"] for row in coordinates] == [0.0, 7.0, 14.0]
    assert report["items"][0]["outer_face_start"] == {"pixel": 158.0, "world_m": 0.0}
    assert report["items"][0]["origin_basis"] == "dimension_chain"


def test_dimension_chain_rejects_nonfinite_or_wrong_shape_origin_values():
    for invalid in (float("inf"), float("nan"), True, "0", [0.0]):
        value = _plan()
        original_anchors = copy.deepcopy(value["x_anchors"])
        value["dimension_chains"] = [{
            "id": "INVALID_ORIGIN", "axis": "x", "segments_mm": [18000],
            "total_mm": 18000, "tick_pixels": [22, 198], "start_world_m": invalid,
            "source_refs": ["plan.png: overall x dimension", "task coordinate contract"],
        }]

        assert plan_format_errors(value)
        aligned, report = align_plan_to_dimensions(value)

        assert aligned["x_anchors"] == original_anchors
        assert report["summary"]["chains_applied"] == 0
        assert report["summary"]["rejected"] == 1


def test_dimension_ticks_move_only_nearby_concave_footprint_edge_with_attached_objects():
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [200, 20.0]],
        "y_anchors": [[0, 16.0], [160, 0.0]],
        "basis": "concave dimension alignment fixture",
        "footprint_pixels": [[0, 0], [200, 0], [200, 160], [100.8, 160],
                             [100.8, 80], [0, 80]],
        "partitions": [{
            "id": "P_JUNCTION", "points": [[100.8, 120], [150, 120]],
            "source_refs": ["plan.png: partition meeting notch"],
        }],
        "openings": [{
            "id": "D_NOTCH", "kind": "door", "p1": [100.8, 95], "p2": [100.8, 115],
            "z": [0.0, 2.1], "source_refs": ["plan.png: door on notch wall"],
            "state": "unknown",
        }],
        "space_seeds": [], "assumptions": [], "unresolved": [],
        "dimension_chains": [{
            "id": "X_OVERALL_WITH_NOTCH", "axis": "x",
            "segments_mm": [10000, 10000], "total_mm": 20000,
            "tick_pixels": [0, 100, 200], "start_world_m": 0.0,
            "source_refs": [
                "plan.png: overall x chain 10000/10000",
                "task coordinate contract: first west tick is world x=0 m",
            ],
        }],
    }

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["footprint_pixels"][3:5] == [[100.0, 160], [100.0, 80]]
    assert aligned["openings"][0]["p1"] == [100.0, 95]
    assert aligned["openings"][0]["p2"] == [100.0, 115]
    assert aligned["partitions"][0]["points"] == [[100.0, 120], [150, 120]]
    perimeter = next(row for row in report["changes"] if row["kind"] == "perimeter")
    assert perimeter["object"].startswith("footprint:")
    assert perimeter["chain_id"] == "X_OVERALL_WITH_NOTCH"
    assert perimeter["moved_with_wall"]["openings"] == ["D_NOTCH"]
    assert perimeter["moved_with_wall"]["junctions"] == ["P_JUNCTION:0"]
    assert report["summary"]["footprint_edges_snapped"] == 1
    assert report["summary"]["partitions_snapped"] == 0


def test_consistent_supplemental_overall_chain_adds_ticks_and_conflict_is_rejected():
    value = _plan()
    value["footprint_pixels"] = [[20, 20], [200, 20], [200, 160], [20, 160]]
    value["partitions"][0]["points"] = [[79, 20], [79, 160]]
    value["dimension_chains"] = [
        {
            "id": "X_PRIMARY", "axis": "x", "segments_mm": [9000, 9000],
            "total_mm": 18000, "tick_pixels": [20, 110, 200], "start_world_m": 0.0,
            "source_refs": ["plan.png: primary overall chain", "task origin contract"],
        },
        {
            "id": "X_SUPPLEMENT", "axis": "x", "segments_mm": [6000, 3000, 9000],
            "total_mm": 18000, "tick_pixels": [20, 80, 110, 200], "start_world_m": 0.0,
            "source_refs": ["plan.png: second overall chain", "task origin contract"],
        },
        {
            "id": "X_CONFLICT", "axis": "x", "segments_mm": [6000, 3000, 9000],
            "total_mm": 18000, "tick_pixels": [20, 80, 110, 200], "start_world_m": 1.0,
            "source_refs": ["plan.png: conflicting transcription", "task origin contract"],
        },
    ]

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["x_anchors"] == [[20.0, 0.0], [200.0, 18.0]]
    assert aligned["partitions"][0]["points"] == [[80.0, 20], [80.0, 160]]
    supplement = next(row for row in report["items"] if row["chain_id"] == "X_SUPPLEMENT")
    assert supplement["action"] == "supplemental_ticks_applied"
    assert supplement["axis_cross_check"]["status"] == "consistent"
    conflict = next(row for row in report["rejections"] if row["chain_id"] == "X_CONFLICT")
    assert conflict["axis_cross_check"]["status"] == "conflict"
    assert report["summary"]["chains_applied"] == 2
    assert report["summary"]["axes_calibrated"] == 1
    assert report["summary"]["supplemental_chains_applied"] == 1
    assert report["summary"]["partitions_snapped"] == 1
    assert report["summary"]["rejected"] == 1


def test_bad_or_internal_dimension_chain_does_not_replace_axis():
    value = _plan()
    original_anchors = copy.deepcopy(value["x_anchors"])
    value["dimension_chains"] = [
        {"id": "BAD", "axis": "x", "segments_mm": [9000, 8000], "total_mm": 18000,
         "tick_pixels": [20, 110, 200], "source_refs": ["plan.png: bad transcription"]},
        {"id": "INTERNAL", "axis": "x", "segments_mm": [3000, 3000], "total_mm": 6000,
         "tick_pixels": [60, 90, 120], "source_refs": ["plan.png: internal chain"]},
    ]

    aligned, report = align_plan_to_dimensions(value)

    assert aligned["x_anchors"] == original_anchors
    assert report["summary"]["chains_applied"] == 0
    assert report["rejections"][0]["chain_id"] == "BAD"
    assert report["rejections"][0]["worst_segment_index"] in {0, 1}
    internal = next(row for row in report["items"] if row["chain_id"] == "INTERNAL")
    assert internal["action"] == "checked_not_applied"
    assert "outer faces" in internal["reason"]


def test_concave_footprint_uses_edge_normal_and_keeps_existing_geometry_inside():
    image = Image.new("RGB", (130, 130), "white")
    draw = ImageDraw.Draw(image)
    ring = [[20, 60], [20, 20], [100, 20], [100, 100], [60, 100], [60, 60]]
    draw.line([*map(tuple, ring), tuple(ring[0])], fill="black", width=1)
    # The notch's two wall faces straddle the declared representative.  For
    # this CCW edge the exterior is downward (+pixel y), even though the edge
    # lies above the footprint's global centroid.
    draw.line((20, 58, 60, 58), fill="black")
    draw.line((20, 62, 60, 62), fill="black")
    value = {
        "floor_id": "F1", "z_floor": 0.0, "ceiling_height": 3.0,
        "x_anchors": [[20, 0.0], [100, 8.0]],
        "y_anchors": [[20, 8.0], [100, 0.0]],
        "basis": "concave regression", "footprint_pixels": copy.deepcopy(ring),
        "partitions": [
            {"id": "near_notch", "points": [[20, 59], [60, 59]],
             "source_refs": ["plan.png: nearby interior wall"]},
            {"id": "notch_continuation", "points": [[60, 60], [90, 60]],
             "source_refs": ["plan.png: continuation from notch"]},
            {"id": "far_branch", "points": [[90, 60], [90, 90]],
             "source_refs": ["plan.png: branch at continuation end"]},
        ],
        "openings": [], "space_seeds": [], "assumptions": [], "unresolved": [],
    }

    aligned, report = align_plan_to_ink(image, value)

    assert aligned["footprint_pixels"][0][1] == 62.0
    assert aligned["footprint_pixels"][5][1] == 62.0
    continuation = next(row for row in aligned["partitions"] if row["id"] == "notch_continuation")
    branch = next(row for row in aligned["partitions"] if row["id"] == "far_branch")
    assert continuation["points"] == [[60, 62.0], [90, 62.0]]
    assert branch["points"] == [[90, 62.0], [90, 90]]
    assert all(first[0] == second[0] or first[1] == second[1]
               for partition in aligned["partitions"]
               for first, second in zip(partition["points"], partition["points"][1:]))
    edge = next(row for row in report["items"] if row["object"] == "footprint:5")
    assert edge["action"] == "moved" and edge["aligned_pixel"] == 62.0
    assert edge["moved_with_wall"]["walls"] == ["notch_continuation"]
    assert not any(row.get("object") == "footprint:5" for row in report["rejections"])


def test_trial_compiles_aligned_numeric_plan_and_returns_only_compact_summary(tmp_path):
    workspace = tmp_path / "trial"
    (workspace / "images").mkdir(parents=True)
    _drawing().save(workspace / "images" / "plan.png")

    class Tools:
        async def call_tool(self, name, arguments):
            assert name == "build_plan_bim"
            submitted = json.loads(arguments["plan_json"])
            effective = copy.deepcopy(submitted)
            effective["regularization"] = {
                "rule_version": "plan_regularization_v1", "changes": [], "rejections": [],
            }
            draft = workspace / "plan_drafts" / "draft_001"
            draft.mkdir(parents=True)
            effective_path = draft / "plan.json"
            effective_path.write_text(
                json.dumps(effective, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            raw = effective_path.read_bytes()
            candidate = workspace / "candidate_01"
            candidate.mkdir()
            (candidate / "source_model.json").write_text(
                json.dumps({"spaces": [], "boundaries": []}), encoding="utf-8", newline="\n",
            )
            return {"structuredContent": {
                "source_geometry_ready": True,
                "candidate": "candidate_01",
                "plan_input": {"plan_file": "plan_drafts/draft_001/plan.json",
                               "plan_sha256": hashlib.sha256(raw).hexdigest(),
                               "image": "plan.png",
                               "regularization": {"rule_version": "plan_regularization_v1",
                                                  "status": "passed"}},
                "drawing_differences": {"status": "reported", "total": 0, "items": []},
                "building_precision": {"status": "reported", "items": []},
            }, "content": []}

        def image_origins(self, raw):
            return {}

    value = _plan()
    value["dimension_chains"] = [{
        "id": "X_OVERALL", "axis": "x", "segments_mm": [9000, 9000],
        "total_mm": 18000, "tick_pixels": [20, 110, 200], "source_refs": ["plan.png: dimensions"],
    }]

    async def scenario():
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=workspace,
                          receipt_directory=workspace / "trial_receipts", grid_step_m=None)
        envelope = await trial.call(value)
        receipt = trial.receipts[-1]
        assert receipt["status"] == "passed"
        assert receipt["reading_alignment"]["ink"]["status"] == "not_applied"
        assert receipt["reading_alignment"]["dimensions"]["summary"]["chains_applied"] == 1
        visible = envelope["structuredContent"]
        assert visible['receipt_audit']['read']['arguments']['field'] == 'report:'
        assert 'reading_alignment' not in visible
        assert trial.read_draft(field='report:/reading_alignment/ink/status')['value'] == 'not_applied'
        numeric = trial.numeric_plan(receipt)
        assert receipt["unaligned_numeric_input_file"] != receipt["aligned_numeric_input_file"]
        unaligned = json.loads(
            (workspace / receipt["unaligned_numeric_input_file"]).read_text(encoding="utf-8")
        )
        assert unaligned["partitions"][0]["points"][0][0] == 109
        assert receipt["aligned_numeric_input_file"] != receipt["compiled_numeric_plan_file"]
        aligned_input = json.loads(
            (workspace / receipt["aligned_numeric_input_file"]).read_text(encoding="utf-8")
        )
        assert "regularization" not in aligned_input
        assert receipt["compiled_numeric_plan_sha256"] == receipt["compiled_plan_sha256"]
        assert "dimension_chains" not in numeric
        assert numeric["regularization"]["rule_version"] == "plan_regularization_v1"
        assert numeric["partitions"][0]["points"][0][0] == 110.0
        assert numeric["regularization_inputs"]["reading_alignment"]["dimensions"]["items"]

    asyncio.run(scenario())


def test_trial_rejects_unverified_regularized_toolkit_plan(tmp_path):
    workspace = tmp_path / "trial"
    (workspace / "images").mkdir(parents=True)
    _drawing().save(workspace / "images" / "plan.png")

    class Tools:
        async def call_tool(self, name, arguments):
            draft = workspace / "plan_drafts" / "draft_001"
            draft.mkdir(parents=True)
            (draft / "plan.json").write_text(arguments["plan_json"], encoding="utf-8", newline="\n")
            return {"structuredContent": {
                "source_geometry_ready": True,
                "candidate": "candidate_01",
                "plan_input": {
                    "plan_file": "plan_drafts/draft_001/plan.json",
                    "plan_sha256": "0" * 64,
                    "image": "plan.png",
                    "regularization": {"rule_version": "plan_regularization_v1",
                                       "status": "passed"},
                },
            }, "content": []}

        def image_origins(self, raw):
            return {}

    async def scenario():
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=workspace,
                          receipt_directory=workspace / "trial_receipts", grid_step_m=None)
        receipt = await trial.run(_plan())
        assert receipt["status"] == "failed"
        assert "Toolkit effective plan changed or is missing" in receipt["reason"]
        assert receipt["aligned_numeric_input_file"]
        assert receipt["compiled_numeric_plan_file"] is None
        assert receipt["compiled_numeric_plan_sha256"] is None

    asyncio.run(scenario())
