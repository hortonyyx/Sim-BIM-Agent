"""Declared wall faces remain visible without moving geometry or filling apertures."""
import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.runtime_roles.plan_reading import _views, _wall_view_geometry, build_reading


SIZE = (420, 420)
WHITE = (255, 255, 255)


def pixel(point, x_sign=1, y_sign=1):
    return tuple(round(210 + sign * 40 * value) for sign, value in zip((x_sign, y_sign), point))


def reading(*, side="centerline", thickness=.4, x_sign=1, y_sign=1, reverse=False):
    def px(point):
        return list(pixel(point, x_sign, y_sign))
    points = [px([0, -4]), px([0, 4])]
    if reverse:
        points.reverse()
    plan = {
        "reading_type": "plan", "floor_id": "F1", "basis": "Synthetic original pixel calibration.",
        "x_anchors": [[10, -5 * x_sign], [410, 5 * x_sign]],
        "y_anchors": [[10, -5 * y_sign], [410, 5 * y_sign]],
        "footprint_pixels": [px(p) for p in [[-4, -4], [4, -4], [4, 4], [-4, 4]]],
        "partitions": [{"id": "P", "points": points}],
        "space_seeds": [{"id": "left", "point": px([-2, 0])},
                        {"id": "right", "point": px([2, 0])}],
        "openings": [], "assumptions": [], "unresolved": [],
        "wall_metadata": [{"target": "partition:P", "thickness_m": thickness, "reference_side": side,
            "evidence_status": "unknown" if thickness is None else "observed",
            "basis": "Synthetic locally supplied wall faces.", "source_refs": ["plan.png: synthetic faces"]}],
    }
    return plan


def compile_reading(plan):
    return compile_plan_partition(plan, image_size=SIZE, image_name="plan.png")


@pytest.mark.parametrize("x_sign,y_sign", [(1, 1), (1, -1), (-1, 1), (-1, -1)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("side,faces", [
    ("centerline", (-.2, .2)), ("negative_face", (0, .4)), ("positive_face", (-.4, 0)),
])
def test_declared_faces_project_through_reversed_axes_and_segments_without_mutation(
        x_sign, y_sign, reverse, side, faces):
    plan = reading(side=side, x_sign=x_sign, y_sign=y_sign, reverse=reverse)
    _, metadata = compile_reading(plan)
    original = Image.new("RGB", SIZE, WHITE)
    before = copy.deepcopy((plan, metadata)), original.tobytes()
    overlay, clean = _views(plan, metadata, original)
    lo, hi = faces
    for face in faces:
        assert clean.getpixel(pixel([face, 2], x_sign, y_sign)) != WHITE
    interior = clean.getpixel(pixel([(lo + hi) / 2, 2], x_sign, y_sign))
    assert interior != WHITE
    assert interior != clean.getpixel(pixel([lo, 2], x_sign, y_sign))
    assert clean.getpixel(pixel([lo - .1, 2], x_sign, y_sign)) == WHITE
    assert clean.getpixel(pixel([hi + .1, 2], x_sign, y_sign)) == WHITE
    assert overlay.size == clean.size == original.size
    assert (plan, metadata) == before[0] and original.tobytes() == before[1]


@pytest.mark.parametrize("x_sign,y_sign", [(1, 1), (1, -1), (-1, 1), (-1, -1)])
@pytest.mark.parametrize("reverse_ring", [False, True])
def test_reentrant_footprint_band_follows_its_local_outer_face(x_sign, y_sign, reverse_ring):
    ring = [[-3, -3], [3, -3], [3, -1], [1, -1], [1, 3], [-3, 3]]
    if reverse_ring:
        ring.reverse()
    target = next(i for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1]))
                  if set(map(tuple, (a, b))) == {(3, -1), (1, -1)})
    plan = reading(x_sign=x_sign, y_sign=y_sign)
    plan.update(footprint_pixels=[list(pixel(p, x_sign, y_sign)) for p in ring],
                partitions=[], space_seeds=[{"id": "room", "point": list(pixel([-2, 0], x_sign, y_sign))}])
    plan["wall_metadata"][0].update(target=f"footprint:{target}", reference_side="outer_face")
    _, metadata = compile_reading(plan)
    _, clean = _views(plan, metadata, Image.new("RGB", SIZE, WHITE))
    # The reentrant horizontal wall's interior is south of its declared line.
    assert clean.getpixel(pixel([2, -1.2], x_sign, y_sign)) != WHITE
    assert clean.getpixel(pixel([2, -.8], x_sign, y_sign)) == WHITE


@pytest.mark.parametrize("side,thickness", [("unknown", .4), ("centerline", None), ("unknown", None)])
def test_unknown_thickness_or_side_never_creates_a_second_face(side, thickness):
    plan = reading(side=side, thickness=thickness)
    _, metadata = compile_reading(plan)
    original = Image.new("RGB", SIZE, WHITE)
    actual = _views(plan, metadata, original)
    baseline = copy.deepcopy(plan)
    baseline.pop("wall_metadata")
    expected = _views(baseline, metadata, original)
    assert [p.tobytes() for p in actual] == [p.tobytes() for p in expected]
    assert _wall_view_geometry(plan, metadata)["bands"] == []
    assert actual[1].getpixel(pixel([0, 2])) != WHITE
    assert actual[1].getpixel(pixel([.2, 2])) == WHITE


def test_wall_faces_use_adopted_partition_world_coordinates():
    plan = reading()
    _, metadata = compile_reading(plan)
    metadata["partition_mapping"][0]["world_points_m"] = [[1, -4], [1, 4]]
    before = copy.deepcopy((plan, metadata))
    _, clean = _views(plan, metadata, Image.new("RGB", SIZE, WHITE))
    assert clean.getpixel(pixel([.8, 2])) != WHITE
    assert clean.getpixel(pixel([1.2, 2])) != WHITE
    assert clean.getpixel(pixel([0, 2])) == WHITE
    assert (plan, metadata) == before


@pytest.mark.parametrize("x_sign,y_sign", [(1, 1), (1, -1), (-1, 1), (-1, -1)])
def test_hosted_door_and_window_spans_remain_clear_in_both_views(x_sign, y_sign):
    plan = reading(x_sign=x_sign, y_sign=y_sign)
    plan["wall_metadata"].append({**plan["wall_metadata"][0], "target": "footprint:2",
                                  "reference_side": "outer_face"})
    plan["openings"] = [
        {"id": "D", "kind": "door", "p1": list(pixel([0, -2], x_sign, y_sign)),
         "p2": list(pixel([0, -1], x_sign, y_sign))},
        {"id": "W", "kind": "window", "p1": list(pixel([1, 4], x_sign, y_sign)),
         "p2": list(pixel([2, 4], x_sign, y_sign))},
    ]
    _, metadata = compile_reading(plan)
    views = _views(plan, metadata, Image.new("RGB", SIZE, WHITE))
    for view in views:
        assert view.getpixel(pixel([-.15, -1.5], x_sign, y_sign)) == WHITE
        assert view.getpixel(pixel([1.5, 3.8], x_sign, y_sign)) == WHITE
        assert view.getpixel(pixel([0, -1.5], x_sign, y_sign)) != WHITE  # Door mark remains.
        assert view.getpixel(pixel([1.5, 4], x_sign, y_sign)) != WHITE  # Window mark remains.
        assert view.getpixel(pixel([-.15, 2], x_sign, y_sign)) != WHITE
        assert view.getpixel(pixel([-2, 3.8], x_sign, y_sign)) != WHITE


def test_overlay_preserves_visible_original_ink_under_declared_band():
    plan = reading()
    _, metadata = compile_reading(plan)
    original = Image.new("RGB", SIZE, WHITE)
    ImageDraw.Draw(original).rectangle([pixel([-.1, 1.5]), pixel([.1, 2.5])], fill=(20, 20, 20))
    overlay, _ = _views(plan, metadata, original)
    assert max(overlay.getpixel(pixel([0, 2]))) < 50
    assert min(overlay.getpixel(pixel([0, 3]))) > 210
    assert original.getpixel(pixel([0, 2])) == (20, 20, 20)


def test_overlay_grays_original_colors_but_preserves_adopted_marks_and_clean_view():
    plan = reading()
    plan["openings"] = [
        {"id": "W", "kind": "window", "p1": list(pixel([-2, 4])), "p2": list(pixel([-1, 4]))},
        {"id": "D", "kind": "door", "p1": list(pixel([0, -2])), "p2": list(pixel([0, -1]))},
    ]
    _, metadata = compile_reading(plan)
    original = Image.new("RGB", SIZE, "black")
    draw = ImageDraw.Draw(original)
    draw.line([(130, 365), (170, 365)], fill=(0, 255, 255), width=1)
    draw.line([(216, 130), (216, 170)], fill=(255, 0, 255), width=1)
    before = copy.deepcopy((plan, metadata)), original.tobytes()
    overlay, clean = _views(plan, metadata, original)
    for point in ((150, 365), (216, 150)):
        expected = original.convert("L").getpixel(point)
        assert overlay.getpixel(point) == (expected, expected, expected)
    # Adopted openings retain their distinct colors at the unchanged pixels.
    assert overlay.getpixel((150, 370)) == (10, 167, 151)
    assert overlay.getpixel((210, 150)) == (214, 83, 32)
    assert overlay.size == clean.size == original.size
    assert clean.tobytes() == _views(plan, metadata, Image.new("RGB", SIZE, WHITE))[1].tobytes()
    assert (plan, metadata) == before[0] and original.tobytes() == before[1]


def test_saved_views_and_rendering_summary_leave_reading_data_and_source_image_unchanged(tmp_path):
    plan = complete_reading()
    before = copy.deepcopy(plan)
    proposal, metadata = compile_reading(plan)
    (tmp_path / "images").mkdir()
    original_path = tmp_path / "images/plan.png"
    Image.new("RGB", SIZE, WHITE).save(original_path)
    original_bytes = original_path.read_bytes()
    result = build_reading(plan, workspace=tmp_path, image_name="plan.png", number=1)
    receipt = result["structuredContent"]
    product = json.loads((tmp_path / receipt["reading_product_file"]).read_bytes())
    assert product["geometry"] == proposal["geometry"]
    assert product["mapping"] == metadata
    assert json.loads((tmp_path / "reading_001/plan.json").read_bytes()) == plan == before
    assert original_path.read_bytes() == original_bytes
    assert product["image_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    rendering = receipt["wall_metadata_summary"]["rendering"]
    assert rendering["walls_with_known_faces"] == 5
    assert rendering["walls_shown_as_lines"] == 0
    assert rendering["solid_band_segments"] == 5
    assert len(receipt["source_plan_views"]) == len(result["content"]) == 2
    for view in receipt["source_plan_views"]:
        path = tmp_path / view["file"]
        with Image.open(path) as image:
            assert image.size == SIZE
        assert hashlib.sha256(path.read_bytes()).hexdigest() == view["sha256"]


@pytest.mark.parametrize("change", [{"thickness_m": .6}, {"reference_side": "negative_face"}])
def test_changed_declared_faces_need_a_fresh_observation_when_rendered_views_change(tmp_path, change):
    from src.agent.runtime_roles.reader_delivery import require_observed_plan_trial
    from src.agent.runtime_roles.trial import PlanTrial
    from tests.test_plan_review_reuse import journal
    plan = complete_reading()
    (tmp_path / "images").mkdir()
    image_path = tmp_path / "images/plan.png"
    Image.new("RGB", SIZE, WHITE).save(image_path)
    (tmp_path / "inputs.json").write_text(json.dumps({"images": {"plan.png": {
        "size": SIZE, "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()}}}))
    trial = PlanTrial(None, image_name="plan.png", workspace=tmp_path,
                      receipt_directory=tmp_path / "trial_receipts", grid_step_m=.1)
    before = asyncio.run(trial.run(plan))
    revised = copy.deepcopy(plan)
    next(r for r in revised["wall_metadata"] if r["target"] == "partition:P").update(change)
    after = asyncio.run(trial.run(revised))
    assert before["status"] == after["status"] == "passed"
    assert all(a["sha256"] != b["sha256"]
               for a, b in zip(before["returned_images"], after["returned_images"]))
    with pytest.raises(ValueError, match="have not reached a model request"):
        require_observed_plan_trial(journal(before, after), trial, after)


def complete_reading():
    from src.agent.geometry.reader_wall_metadata import require_wall_thickness
    plan = reading()
    plan['wall_metadata'].append(dict(target='footprint:*', thickness_m=.24,
        evidence_status='inferred', basis='Synthetic perimeter', source_refs=['fixture']))
    return require_wall_thickness(plan)
