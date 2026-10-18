import copy

import pytest
from PIL import Image, ImageDraw

from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.plan_wall_metadata_checks import append_wall_metadata_checks


def scene(*, background="white", mirror=False, side="outer_face", thickness=.24, opening=False):
    plan = {"reading_type": "plan", "floor_id": "F1", "basis": "Synthetic 100px/m fixture",
            "x_anchors": [[100, 0], [900, 8]], "y_anchors": [[100, 8], [900, 0]],
            "footprint_pixels": [[100, 100], [900, 100], [900, 900], [100, 900]],
            "partitions": [], "space_seeds": [], "openings": [], "assumptions": [], "unresolved": [],
            "wall_metadata": [{"target": "footprint:0", "thickness_m": thickness,
                               "reference_side": side, "evidence_status": "observed" if thickness else "unknown",
                               "basis": "Synthetic paired strokes", "source_refs": ["fixture"]}]}
    if mirror:
        plan["x_anchors"] = [[900, 0], [100, 8]]
        plan["y_anchors"] = [[900, 8], [100, 0]]
    image = Image.new("RGB", (1000, 1000), background)
    draw = ImageDraw.Draw(image)
    color = "black" if background == "white" else "white"
    for y in (100, 124):
        draw.line([(100, y), (900, y)], fill=color, width=1)
    if opening:
        plan["openings"] = [{"id": "W1", "kind": "window", "p1": [180, 100], "p2": [820, 100]}]
        draw.rectangle([180, 95, 820, 130], fill=background)
    _, metadata = compile_plan_partition(plan, image_size=image.size, image_name="plan.png")
    report = {"status": "reported", "items": [], "total": 0, "counts": {}, "scope": "Existing scope", "coverage": {}}
    return plan, image, metadata, report


@pytest.mark.parametrize("background,mirror,opening", [("white", False, False), ("black", True, False), ("black", False, True)])
def test_matching_faces_and_openings_do_not_create_hints_or_change_inputs(background, mirror, opening):
    plan, image, metadata, report = scene(background=background, mirror=mirror, opening=opening)
    before = copy.deepcopy((plan, metadata, report)); pixels = image.tobytes()
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["items"] == []
    assert result["coverage"]["wall_metadata"]["compared_segments"] == 1
    assert (plan, metadata, report) == before and image.tobytes() == pixels


@pytest.mark.parametrize("side,thickness", [("inner_face", .24), ("outer_face", .72)])
def test_wrong_declared_side_or_thickness_yields_located_ink_hint(side, thickness):
    plan, image, metadata, report = scene(side=side, thickness=thickness)
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["total"] == 1
    issue = result["items"][0]
    assert issue["wall_target"] == "footprint:0"
    assert issue["nearby_parallel_ink_px"] == [100, 124]
    assert issue["ink_spacing_m"] == .24
    assert issue["look_box"][1] <= 100 < 124 <= issue["look_box"][3]
    assert result["status"] == "reported"


@pytest.mark.parametrize("unknown", ["side", "thickness", "blank", "third_line", "clipped"])
def test_unknown_absent_ambiguous_or_clipped_ink_is_not_negative_evidence(unknown):
    plan, image, metadata, report = scene(side="inner_face", thickness=.24)
    if unknown == "side":
        plan["wall_metadata"][0]["reference_side"] = "unknown"
    elif unknown == "thickness":
        plan["wall_metadata"][0].update(thickness_m=None, evidence_status="unknown")
    elif unknown == "blank":
        image.paste("white", (0, 0, *image.size))
    elif unknown == "third_line":
        ImageDraw.Draw(image).line([(100, 144), (900, 144)], fill="black")
    else:
        image = image.crop((0, 80, 1000, 1000))
        for point in plan["footprint_pixels"]:
            point[1] -= 80
        for anchor in plan["y_anchors"]:
            anchor[0] -= 80
        _, metadata = compile_plan_partition(plan, image_size=image.size, image_name="plan.png")
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["total"] == 0
    assert result["coverage"]["wall_metadata"]["compared_segments"] == (1 if unknown in {"side", "thickness"} else 0)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("thickness", [None, .24])
def test_unknown_side_still_reports_reference_outside_pair_without_guessing_side(mirror, thickness):
    plan, image, metadata, report = scene(side="unknown", thickness=thickness, mirror=mirror)
    # Keep a real orthogonal footprint but read this wall 20 cm past its
    # outer face. No declared side is available to construct wall faces.
    plan["footprint_pixels"][0][1] = plan["footprint_pixels"][1][1] = 80
    _, metadata = compile_plan_partition(plan, image_size=image.size, image_name="plan.png")
    before = copy.deepcopy((plan, metadata, report)); pixels = image.tobytes()
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert len(result["items"]) == 1
    issue = result["items"][0]
    assert issue["reference_side"] == "unknown"
    assert issue["declared_faces_px"] is None and issue["face_residual_m"] is None
    assert issue["declared_reference_px"] == 80
    assert issue["reference_outside_ink_m"] == .2
    assert issue["nearby_parallel_ink_px"] == [100, 124]
    assert issue["declared_thickness_m"] == thickness
    assert (plan, metadata, report) == before and image.tobytes() == pixels


def test_unknown_side_still_checks_known_width_without_guessing_faces():
    plan, image, metadata, report = scene(side="unknown", thickness=.72)
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert len(result["items"]) == 1
    issue = result["items"][0]
    assert issue["declared_faces_px"] is None
    assert issue["reference_outside_ink_m"] == 0
    assert issue["declared_thickness_m"] == .72 and issue["ink_spacing_m"] == .24


@pytest.mark.parametrize('mirror', [False, True])
@pytest.mark.parametrize('side,thickness,expansion,comparison', [
    ('inner_face', .24, 'up', 'placement_only'),
    ('outer_face', .72, 'down', 'thickness_and_placement')])
def test_hint_describes_material_direction_in_image_without_world_sign_arithmetic(
        mirror, side, thickness, expansion, comparison):
    plan, image, metadata, report = scene(side=side, thickness=thickness, mirror=mirror)
    issue = append_wall_metadata_checks(report, image, plan, metadata)['items'][0]
    assert issue['image_expansion'] == expansion
    assert issue['comparison'] == comparison
    assert issue['declared_reference_px'] == 100
    assert issue['nearby_parallel_ink_px'] == [100, 124]


def short_piers(*, pixels_per_m=100, sample_length_m=.6, side="outer_face", thickness=.16):
    """One door leaves two clear piers after the unchanged end/opening margins."""
    start, end = 100, 100 + 3 * pixels_per_m
    door_start = round(start + (.3 + sample_length_m / 2) * pixels_per_m)
    door_end = start + end - door_start
    plan = {"reading_type": "plan", "floor_id": "F1", "basis": "Synthetic short wall piers.",
            "x_anchors": [[start, 0], [end, 3]], "y_anchors": [[start, 3], [end, 0]],
            "footprint_pixels": [[start, start], [end, start], [end, end], [start, end]],
            "partitions": [], "space_seeds": [], "assumptions": [], "unresolved": [],
            "openings": [{"id": "D", "kind": "door", "p1": [door_start, start], "p2": [door_end, start]}],
            "wall_metadata": [{"target": "footprint:0", "thickness_m": thickness,
                "reference_side": side, "evidence_status": "unknown" if thickness is None else "observed",
                "basis": "Synthetic local paired faces.", "source_refs": ["fixture: piers"]}]}
    image = Image.new("RGB", (500, 500), "white")
    draw = ImageDraw.Draw(image)
    second = round(start + .16 * pixels_per_m)
    for y in (start, second):
        draw.line([(start, y), (end, y)], fill="black", width=1)
    draw.rectangle([door_start, start - 1, door_end, second + 1], fill="white")
    # Drawing detail inside the aperture must not enter the wall samples.
    for y in (start - 5, start + 4, second + 6):
        draw.line([(door_start, y), (door_end, y)], fill="black", width=1)
    _, metadata = compile_plan_partition(plan, image_size=image.size, image_name="plan.png")
    report = {"status": "reported", "items": [], "total": 0, "counts": {}, "scope": "Existing scope", "coverage": {}}
    return plan, image, metadata, report


@pytest.mark.parametrize("side,thickness,has_hint", [
    ("outer_face", .16, False), ("outer_face", .32, True), ("inner_face", .16, True),
])
def test_clear_short_piers_check_declared_faces_with_visible_support_and_no_edits(side, thickness, has_hint):
    plan, image, metadata, report = short_piers(side=side, thickness=thickness)
    before = copy.deepcopy((plan, metadata, report)), image.tobytes()
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["coverage"]["wall_metadata"]["compared_segments"] == 1
    assert result["total"] == int(has_hint)
    if has_hint:
        issue = result["items"][0]
        assert issue["wall_target"] == "footprint:0"
        assert issue["nearby_parallel_ink_px"] == [100, 116]
        assert issue["ink_spacing_m"] == .16
        assert issue["sampled_length_m"] == .6 and issue["sample_pixels"] == 60
        assert issue["declared_thickness_m"] == thickness
        assert issue["look_box"][1] <= 100 < 116 <= issue["look_box"][3]
    assert result["status"] == "reported"
    assert (plan, metadata, report) == before[0] and image.tobytes() == before[1]


@pytest.mark.parametrize("pixels_per_m,sample_length_m,support", [
    (100, .4, (40, .4)),  # Exact physical support threshold.
    (20, .6, (12, .6)),  # Exact independent raster support threshold.
    (100, .38, None),    # Enough pixels, insufficient net physical support.
    (15, .6, None),      # Enough physical support, fewer than twelve pixels.
])
def test_short_pier_minima_require_both_physical_length_and_pixel_support(
        pixels_per_m, sample_length_m, support):
    plan, image, metadata, report = short_piers(
        pixels_per_m=pixels_per_m, sample_length_m=sample_length_m, thickness=.32)
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["coverage"]["wall_metadata"]["compared_segments"] == int(support is not None)
    assert result["total"] == int(support is not None)
    if support is not None:
        assert (result["items"][0]["sample_pixels"], result["items"][0]["sampled_length_m"]) == support


@pytest.mark.parametrize("erased_pixels,has_hint", [(12, True), (13, False)])
def test_short_piers_still_need_seventy_percent_parallel_stroke_support(erased_pixels, has_hint):
    plan, image, metadata, report = short_piers(sample_length_m=.4, thickness=.32)
    ImageDraw.Draw(image).line([(120, 116), (119 + erased_pixels, 116)], fill="white", width=1)
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["total"] == int(has_hint)
    assert result["coverage"]["wall_metadata"]["compared_segments"] == int(has_hint)


@pytest.mark.parametrize("ambiguity", ["third_line", "unknown"])
def test_short_support_does_not_resolve_ambiguous_faces_or_fill_unknown_metadata(ambiguity):
    plan, image, metadata, report = short_piers()
    if ambiguity == "third_line":
        ImageDraw.Draw(image).line([(100, 132), (400, 132)], fill="black", width=1)
        plan["wall_metadata"][0]["thickness_m"] = .32
    else:
        plan["wall_metadata"][0].update(thickness_m=None, reference_side="unknown", evidence_status="unknown")
    before = copy.deepcopy((plan, metadata, report)), image.tobytes()
    result = append_wall_metadata_checks(report, image, plan, metadata)
    assert result["total"] == 0
    assert result["coverage"]["wall_metadata"]["compared_segments"] == (1 if ambiguity == "unknown" else 0)
    assert (plan, metadata, report) == before[0] and image.tobytes() == before[1]
