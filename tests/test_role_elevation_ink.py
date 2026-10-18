from __future__ import annotations

from reader_delivery_fixtures import REVIEW

import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from src.agent.runtime_roles.elevation import (
    ElevationReaderTools,
    elevation_submission_tool,
    expand_elevation_submission,
    validate_elevation_artifact,
)
from src.agent.runtime_roles.elevation_ink import align_elevation_artifact
from src.agent.runtime_roles.height_evidence import derive_z_calibration, height_locations
from tests.test_role_readers import Frozen


def _artifact(*, opening: dict, mirrored: bool = False, with_z: bool = True) -> dict:
    artifact = {
        "x_calibration": {
            "pixel_start": 0,
            "pixel_end": 100,
            "world_start_m": 10 if mirrored else 0,
            "world_end_m": 0 if mirrored else 10,
        },
        "openings": [{"evidence_type": "pixels", **opening}],
    }
    if with_z:
        artifact["z_calibration"] = {
            "pixel_start": 10,
            "pixel_end": 110,
            "world_start_m": 10,
            "world_end_m": 0,
        }
    return artifact


def _outlined_rectangle(box, *, fill=0, width=1):
    image = Image.new("L", (140, 120), 255)
    ImageDraw.Draw(image).rectangle(box, outline=fill, width=width)
    return image


def test_aligns_all_four_edges_and_ignores_legacy_evidence_box_as_geometry():
    image = _outlined_rectangle((40, 30, 90, 80))
    artifact = _artifact(
        opening={
            "id": "W1",
            "x_px": [37, 93],
            "width_m": 5.6,
            "sill_m": 3.3,
            "head_m": 7.7,
            # Deliberately much larger: this is only an evidence crop.
            "bbox": [10, 5, 125, 112],
        }
    )

    result = align_elevation_artifact(image, artifact, search_distance_m=0.5)
    aligned = result["openings"][0]

    assert aligned["geometry_source"] == "legacy_x_px_and_calibrated_heights"
    assert aligned["original_bbox_px"] == pytest.approx([37, 33, 93, 77])
    assert aligned["aligned_bbox_px"] == pytest.approx([40, 30, 90, 80])
    assert {edge["status"] for edge in aligned["edges"].values()} == {"aligned"}
    assert aligned["aligned_values"]["x_px"] == pytest.approx([40, 90])
    assert aligned["aligned_values"]["width_m"] == pytest.approx(5)
    assert aligned["aligned_values"]["sill_m"] == pytest.approx(3)
    assert aligned["aligned_values"]["head_m"] == pytest.approx(8)


def test_preserves_mirrored_world_coordinates_while_width_stays_positive():
    image = _outlined_rectangle((30, 20, 70, 60))
    artifact = _artifact(
        mirrored=True,
        opening={
            "id": "D1",
            "opening_bbox": [28, 22, 72, 58],
            "x_px": [28, 72],
            "width_m": 4.4,
            "sill_m": 5.2,
            "head_m": 8.8,
            "bbox": [25, 18, 75, 62],
        },
    )

    aligned = align_elevation_artifact(
        image, artifact, search_distance_m=0.4
    )["openings"][0]

    assert aligned["aligned_values"]["x_px"] == pytest.approx([30, 70])
    assert aligned["aligned_values"]["world_span_m"] == pytest.approx([7, 3])
    assert aligned["aligned_values"]["width_m"] == pytest.approx(4)


def test_weak_ink_and_missing_vertical_calibration_leave_values_unchanged():
    image = _outlined_rectangle((40, 30, 90, 80), fill=225)
    opening = {
        "id": "W1",
        "x_px": [37, 93],
        "width_m": 5.6,
        "sill_m": 2.0,
        "head_m": 8.0,
        "bbox": [10, 5, 125, 112],
    }
    artifact = _artifact(opening=opening, with_z=False)

    aligned = align_elevation_artifact(
        image, artifact, search_distance_m=0.5
    )["openings"][0]

    assert aligned["original_bbox_px"] == [37.0, None, 93.0, None]
    assert aligned["edges"]["left"]["status"] == "no_strong_line"
    assert aligned["edges"]["right"]["status"] == "no_strong_line"
    assert aligned["edges"]["top"]["status"] == "missing_calibration"
    assert aligned["edges"]["bottom"]["status"] == "missing_calibration"
    assert aligned["aligned_values"]["x_px"] == opening["x_px"]
    assert aligned["aligned_values"]["width_m"] == opening["width_m"]
    assert aligned["aligned_values"]["sill_m"] == opening["sill_m"]
    assert aligned["aligned_values"]["head_m"] == opening["head_m"]


@pytest.mark.parametrize("background,stroke", [
    ((255, 255, 255, 255), (0, 0, 0, 255)),
    ((0, 0, 0, 255), (255, 255, 255, 255)),
    ((0, 0, 0, 255), (255, 0, 0, 255)),
    ((0, 0, 0, 0), (255, 255, 255, 255)),
    ((255, 255, 255, 0), (0, 0, 0, 255)),
])
def test_real_strokes_align_independently_of_background_polarity_or_transparency(background, stroke):
    picture = Image.new("RGBA", (140, 120), background)
    ImageDraw.Draw(picture).rectangle((40, 30, 90, 80), outline=stroke)
    artifact = _artifact(opening={"id": "W1", "opening_bbox": [37, 33, 93, 77]})
    result = align_elevation_artifact(picture, artifact, search_distance_m=0.5)
    assert result["openings"][0]["aligned_bbox_px"] == pytest.approx([40, 30, 90, 80])
    assert not result["issues"]
    assert result["source_image"]["confident"]


@pytest.mark.parametrize("background", ["black", "white", (0, 0, 0, 0)])
def test_blank_background_never_confirms_an_opening(background):
    picture = Image.new("RGBA", (140, 120), background)
    result = align_elevation_artifact(picture, _artifact(opening={
        "id": "W1", "opening_bbox": [40, 30, 90, 80]}))
    assert {edge["status"] for edge in result["openings"][0]["edges"].values()} == {"no_strong_line"}
    assert result["summary"]["moved_edge_count"] == 0
    issue, = result["issues"]
    assert issue["type"] == "opening_geometry_without_image_support"
    assert issue["opening_id"] == "W1"
    assert set(issue["near_empty_edges"]) == {"left", "top", "right", "bottom"}
    assert issue["look_box"] == [37, 27, 94, 84]


@pytest.mark.parametrize("case", ["faint", "dashed", "filled", "two_edges", "nonuniform"])
def test_uncertain_source_ink_is_not_a_hard_geometry_rejection(case):
    picture = Image.new("RGB", (140, 120), "white")
    draw = ImageDraw.Draw(picture)
    if case == "faint":
        draw.rectangle((40, 30, 90, 80), outline=(235, 235, 235))
    elif case == "dashed":
        for x in range(40, 91, 4):
            draw.point((x, 30), fill="black")
            draw.point((x, 80), fill="black")
        for y in range(30, 81, 4):
            draw.point((40, y), fill="black")
            draw.point((90, y), fill="black")
    elif case == "filled":
        draw.rectangle((35, 25, 95, 85), fill="black")
    elif case == "two_edges":
        draw.line((40, 80, 90, 80, 90, 30), fill="black")
    else:
        for x in range(140):
            shade = round(x / 139 * 255)
            draw.line((x, 0, x, 119), fill=(shade, shade, shade))
    result = align_elevation_artifact(picture, _artifact(opening={
        "id": "W1", "opening_bbox": [40, 30, 90, 80]}))
    assert not result["issues"]
    if case in {"faint", "dashed", "filled", "nonuniform"}:
        assert {edge["status"] for edge in result["openings"][0]["edges"].values()} == {"no_strong_line"}
    if case == "nonuniform":
        assert not result["source_image"]["confident"]


def test_reader_submission_binds_image_alignment_and_reloads_immutably(tmp_path):
    async def scenario():
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        image_path = image_dir / "North.png"
        _outlined_rectangle((40, 30, 90, 80)).save(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        (tmp_path / "inputs.json").write_text(
            json.dumps({"images": {"North.png": {"sha256": digest}}}),
            encoding="utf-8",
        )
        arguments = {
            "x_calibration": {
                "pixel_start": 0,
                "pixel_end": 100,
                "distance_start_m": 0,
                "distance_end_m": 10,
                "facade_length_m": 10,
            },
            "z_calibration": {
                "pixel_start": 10,
                "pixel_end": 110,
                "world_start_m": 10,
                "world_end_m": 0,
            },
            "elevations": [
                {"id": "roof", "kind": "roof", "value_m": 10,
                 "evidence_type": "pixels", "bbox": [0, 9, 120, 11]},
                {"id": "ground", "kind": "ground", "value_m": 0,
                 "evidence_type": "pixels", "bbox": [0, 109, 120, 111]},
            ],
            "openings": [
                {"id": "W1", "floor_id": "F1", "kind": "window",
                 "evidence_type": "pixels", "opening_bbox": [37, 33, 93, 77]},
            ],
            "counts": [{"floor_id": "F1", "window_count": 1, "door_count": 0}],
            "unresolved": [],
        }
        tools = ElevationReaderTools(
            Frozen(tmp_path),
            role_id="elevation_reader",
            image_name="North.png",
            target="North/F1",
        )
        prepared = await tools.call_tool("trial_elevation_reading", arguments)
        assert not prepared["isError"], prepared
        assert tools.submission.read() is None
        review_args = {"trial_id": prepared["structuredContent"]["trial_id"], "local_review": REVIEW}
        accepted = await tools.call_tool("submit_elevation_reading", review_args)
        assert not accepted["isError"], accepted
        saved = tools.submission.read()
        artifact = saved["artifact"]
        assert artifact["openings"][0]["x_px"] == pytest.approx([40, 90])
        assert artifact["openings"][0]["width_m"] == pytest.approx(5)
        assert artifact["openings"][0]["sill_m"] == pytest.approx(3)
        assert artifact["openings"][0]["head_m"] == pytest.approx(8)
        assert artifact["openings"][0]["opening_bbox"] == [37.0, 33.0, 93.0, 77.0]
        assert artifact["ink_alignment"]["openings"][0]["aligned_bbox_px"] == pytest.approx(
            [40, 30, 90, 80]
        )
        assert saved["validation"]["image_sha256"] == digest
        assert saved["validation"]["ink_alignment_sha256"]
        assert (await tools.call_tool("submit_elevation_reading", review_args)) == accepted

        resumed = ElevationReaderTools(
            Frozen(tmp_path),
            role_id="elevation_reader",
            image_name="North.png",
            target="North/F1",
        )
        assert resumed.submission.read() == saved
        changed = Image.new("L", (140, 120), 255)
        changed.save(image_path)
        with pytest.raises(ValueError):
            ElevationReaderTools(
                Frozen(tmp_path),
                role_id="elevation_reader",
                image_name="North.png",
                target="North/F1",
            )

    asyncio.run(scenario())


def test_saved_level_calibration_requires_two_consistent_horizontal_bands():
    levels = [
        {"id": "roof", "kind": "roof", "value_m": 10,
         "evidence_type": "pixels", "bbox": [0, 9, 120, 11]},
    ]
    assert derive_z_calibration(levels)["status"] == "missing_two_horizontal_level_bands"

    levels.append(
        {"id": "ground", "kind": "ground", "value_m": 0,
         "evidence_type": "pixels", "bbox": [0, 109, 120, 111]}
    )
    derived = derive_z_calibration(levels)
    assert derived["status"] == "derived_from_saved_level_lines"
    assert derived["calibration"] == {
        "pixel_start": 10.0,
        "pixel_end": 110.0,
        "world_start_m": 10.0,
        "world_end_m": 0.0,
    }

    inconsistent = copy.deepcopy(levels)
    inconsistent.append(
        {"id": "middle", "kind": "floor", "floor_id": "F1", "value_m": 7,
         "evidence_type": "pixels", "bbox": [0, 59, 120, 61]}
    )
    assert derive_z_calibration(inconsistent)["status"] == (
        "rejected_inconsistent_saved_level_lines"
    )


def test_height_evidence_uses_aligned_opening_box_for_its_source_region():
    artifact = {
        "artifact_sha256": "a" * 64,
        "x_calibration": {
            "pixel_start": 0, "pixel_end": 100,
            "world_start_m": 0, "world_end_m": 10,
        },
        "z_calibration": {
            "pixel_start": 0, "pixel_end": 100,
            "world_start_m": 10, "world_end_m": 0,
        },
        "elevations": [],
        "openings": [{
            "id": "W1", "floor_id": "F1", "kind": "window",
            "x_px": [40, 60], "width_m": 2, "sill_m": 4, "head_m": 7,
            "evidence_type": "pixels", "bbox": [5, 5, 95, 95],
            "opening_bbox": [38, 28, 62, 62],
        }],
        "ink_alignment": {"openings": [{"id": "W1", "aligned_bbox_px": [40, 30, 60, 60]}]},
    }
    match = {
        "tolerances_m": {"position": 0.2, "width": 0.2},
        "matches": [{"source_opening_id": "source-W1", "artifact_opening_id": "W1"}],
    }

    located = height_locations(artifact, match, [100, 100])["openings"]["W1"]

    assert located["aligned_opening_bbox"] == [40, 30, 60, 60]
    assert located["original_bbox"] == [5, 5, 95, 95]
    assert located["source_box"] == [37, 28, 63, 62]


def test_legacy_expanded_recording_stays_readable_but_is_not_public_schema():
    x_schema = elevation_submission_tool()["inputSchema"]["properties"]["x_calibration"]
    assert "world_start_m" not in x_schema["properties"]
    recorded = {
        "orientation": "North",
        "view_direction": "South",
        "x_calibration": {
            "pixel_start": 100,
            "pixel_end": 900,
            "world_start_m": 10,
            "world_end_m": 0,
            "world_axis": "x",
        },
    }

    expanded = expand_elevation_submission(recorded, "North/F1")

    assert expanded == recorded
    with pytest.raises(ValueError):
        expand_elevation_submission({**recorded, "orientation": "South"}, "North/F1")


@pytest.mark.parametrize("basis", ["annotation", "annotation_and_pixels", "visual_estimate", "assumption", "declared"])
def test_numeric_and_mixed_evidence_is_checked_without_replacing_any_value(basis):
    opening = {"id": "W1", "evidence_type": basis, "opening_bbox": [37, 33, 93, 77],
               "x_px": [37, 93], "width_m": 5.7, "sill_m": 3.3, "head_m": 7.7}
    result = align_elevation_artifact(_outlined_rectangle((40, 30, 90, 80)),
        _artifact(opening=opening), search_distance_m=0.5)["openings"][0]
    assert all(result["aligned_values"][key] == opening[key] for key in ("x_px", "width_m", "sill_m", "head_m"))
    assert not result["applied_fields"]
    assert {r["field"] for r in result["inconsistencies"]} == {"x_px", "width_m", "sill_m", "head_m"}
    assert result["candidate_bbox_px"] == pytest.approx([40, 30, 90, 80])
    assert result["effective_bbox_px"] == pytest.approx([37, 33, 93, 77])
    assert result["tolerance_m"] == pytest.approx({"x": 0.2, "z": 0.2})


def test_multiple_strong_strokes_do_not_authorize_pixel_replacement():
    image = _outlined_rectangle((40, 30, 90, 80))
    ImageDraw.Draw(image).line((45, 30, 45, 80), fill=0)
    opening = {"id": "W1", "x_px": [43, 93], "width_m": 5, "sill_m": 3.3, "head_m": 7.7}
    result = align_elevation_artifact(image, _artifact(opening=opening), search_distance_m=0.5)["openings"][0]
    assert len(result["edges"]["left"]["candidates"]) == 2
    assert result["aligned_values"]["x_px"] == opening["x_px"]
    assert result["aligned_values"]["width_m"] == opening["width_m"]
    assert "x_px" not in result["applied_fields"]


def test_confirmed_pixel_edges_do_not_reround_original_numeric_values():
    opening = {"id": "W1", "x_px": [40, 90], "width_m": 4.98, "sill_m": 3, "head_m": 8}
    result = align_elevation_artifact(_outlined_rectangle((40, 30, 90, 80)),
        _artifact(opening=opening))["openings"][0]
    assert not result["applied_fields"] and not result["inconsistencies"]
    assert result["aligned_values"]["width_m"] == 4.98


def test_floor_contact_sill_is_not_replaced_by_another_lower_ink_line():
    opening = {"id": "D1", "kind": "door", "floor_id": "F1", "opening_bbox": [37, 33, 93, 110],
               "x_px": [37, 93], "width_m": 5.6, "sill_m": 0, "head_m": 7.7}
    artifact = _artifact(opening=opening)
    artifact["elevations"] = [{"id": "floor", "kind": "floor", "floor_id": "F1", "value_m": 0,
                               "evidence_type": "annotation", "bbox": [0, 109, 130, 111]}]
    result = align_elevation_artifact(_outlined_rectangle((40, 30, 90, 114)), artifact,
        search_distance_m=0.5)["openings"][0]
    assert len(result["edges"]["bottom"]["candidates"]) == 1
    assert result["ink_values"]["sill_m"] == pytest.approx(-0.4)
    assert result["aligned_values"]["sill_m"] == 0
    assert result["effective_bbox_px"][3] == 110
    assert result["floor_contact"][0]["id"] == "floor"
    assert result["field_decisions"]["sill_m"] == "retained_floor_contact"
    assert any(r["field"] == "sill_m" for r in result["inconsistencies"])
    # A raised threshold or a different-floor line does not establish contact.
    artifact["openings"][0]["sill_m"] = 0.2
    raised = align_elevation_artifact(_outlined_rectangle((40, 30, 90, 114)), artifact,
        search_distance_m=0.5)["openings"][0]
    assert not raised["floor_contact"]


def test_normalization_retains_annotated_originals_beside_grid_adopted_values():
    from src.agent.runtime_roles.guidance import COMPACT_ELEVATION_EXAMPLE
    args = copy.deepcopy(COMPACT_ELEVATION_EXAMPLE)
    args["openings"][0].update(evidence_type="annotation_and_pixels", x_px=[205, 297],
                               width_m=1.1, sill_m=1.05, head_m=2.55)
    normalized = validate_elevation_artifact(expand_elevation_submission(args, "North/F1"), image_name="North.png")
    opening = normalized["openings"][0]
    assert opening["x_px"] == [205, 297]
    assert [opening[key] for key in ("width_m", "sill_m", "head_m")] == pytest.approx([1.1, 1.1, 2.6])
    readings = {(row["item_id"], row["field"]): row for row in normalized["regularization"]["readings"]}
    assert readings[("W1", "sill_m")]["original_m"] == pytest.approx(1.05)
    assert readings[("W1", "head_m")]["original_m"] == pytest.approx(2.55)
    args["openings"][0].pop("width_m")
    with pytest.raises(ValueError):
        validate_elevation_artifact(expand_elevation_submission(args, "North/F1"), image_name="North.png")


def _node_arguments():
    return {
        "x_calibration": {"pixel_start": 10, "pixel_end": 110, "distance_start_m": 0,
                          "distance_end_m": 10, "facade_length_m": 10},
        "z_calibration": {"pixel_start": 10, "pixel_end": 110,
                          "world_start_m": 10, "world_end_m": 0},
        "elevations": [
            {"id": "roof", "kind": "roof", "value_m": 10,
             "evidence_type": "pixels", "bbox": [0, 9, 120, 11], "line_y_px": 10},
            {"id": "ground", "kind": "ground", "value_m": 0,
             "evidence_type": "pixels", "bbox": [0, 109, 120, 111], "line_y_px": 110},
        ],
        "openings": [],
        "counts": [{"floor_id": "F1", "window_count": 0, "door_count": 0}],
        "unresolved": [],
    }


def _node_tools(root, picture=None):
    image_dir = root / "images"
    image_dir.mkdir()
    image_path = image_dir / "North.png"
    (picture if picture is not None else Image.new("RGB", (140, 120), "white")).save(image_path)
    (root / "inputs.json").write_text(json.dumps({"images": {"North.png": {
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()}}}), encoding="utf-8")
    return ElevationReaderTools(Frozen(root), role_id="elevation_reader", image_name="North.png", target="North/F1")


def test_reversed_vertical_anchor_pairs_preserve_trial_geometry_and_input(tmp_path):
    async def scenario():
        from src.agent.runtime_roles.elevation_trial import load_trial
        tools = _node_tools(tmp_path)
        forward = _node_arguments()
        reverse = copy.deepcopy(forward)
        reverse["z_calibration"] = {"pixel_start": 110, "pixel_end": 10,
                                    "world_start_m": 0, "world_end_m": 10}
        saved = copy.deepcopy(reverse)
        trials = []
        for arguments in (forward, reverse):
            result = await tools.call_tool("trial_elevation_reading", arguments)
            assert not result["isError"], result
            trials.append(load_trial(tools.submission, result["structuredContent"]["trial_id"]))
        assert reverse == saved
        assert trials[0]["geometry"] == trials[1]["geometry"]
        assert [v["sha256"] for v in trials[0]["views"]] == [v["sha256"] for v in trials[1]["views"]]
    asyncio.run(scenario())


@pytest.mark.parametrize("calibration", [
    {"pixel_start": 110, "pixel_end": 110, "world_start_m": 0, "world_end_m": 10},
    {"pixel_start": 110, "pixel_end": 10, "world_start_m": 10, "world_end_m": 0},
    {"pixel_start": 110, "pixel_end": 10, "world_start_m": 0, "world_end_m": 0},
    {"pixel_start": 110, "pixel_end": -10, "world_start_m": 0, "world_end_m": 10},
    {"pixel_start": 110, "pixel_end": 10, "world_start_m": float("nan"), "world_end_m": 10},
])
def test_anchor_order_normalization_still_rejects_invalid_vertical_frames(calibration):
    arguments = _node_arguments()
    arguments["z_calibration"] = calibration
    with pytest.raises(ValueError):
        validate_elevation_artifact(expand_elevation_submission(arguments, "North/F1"), image_name="North.png")


def test_annotated_opening_box_supplies_pixels_without_inventing_annotated_dimensions():
    arguments = _node_arguments()
    arguments["openings"] = [{"id": "W1", "floor_id": "F1", "kind": "window",
        "opening_bbox": [40, 30, 90, 80], "width_m": 4.97, "sill_m": 3.04, "head_m": 7.96,
        "evidence_type": "annotation_and_pixels", "bbox": [35, 25, 95, 85]}]
    arguments["counts"][0]["window_count"] = 1
    saved = copy.deepcopy(arguments)
    result = validate_elevation_artifact(expand_elevation_submission(arguments, "North/F1"), image_name="North.png")
    assert arguments == saved
    assert result["openings"][0]["x_px"] == [40, 90]
    original = {row["field"]: row["original_m"] for row in result["regularization"]["readings"]
                if row["item_id"] == "W1"}
    assert original == {"width_m": 4.97, "sill_m": 3.04, "head_m": 7.96}
    arguments["openings"][0].pop("head_m")
    with pytest.raises(ValueError, match="explicit width_m/sill_m/head_m"):
        validate_elevation_artifact(expand_elevation_submission(arguments, "North/F1"), image_name="North.png")


@pytest.mark.parametrize("closed", [False, True])
def test_observed_facade_outline_is_preserved_and_open_chains_are_not_closed(tmp_path, closed):
    async def scenario():
        from src.agent.runtime_roles.elevation_trial import load_trial
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        outline = {"points_px": [[10, 110], [10, 40], [110, 40]], "closed": closed,
                   "evidence_type": "pixels", "basis": "visible perimeter" if closed else "right lower side cropped"}
        arguments["facade_outline"] = outline
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        body = result["structuredContent"]
        assert body["coverage"]["facade_outline"] == ("closed_perimeter" if closed else "open_chain")
        assert body["coverage"]["facade_outline_evidence_type"] == "pixels"
        trial = load_trial(tools.submission, body["trial_id"])
        observed = trial["geometry"]["facade_outline"]
        assert observed["points_px"] == outline["points_px"]
        assert observed["world_xz_points_m"] == [[10, 0], [10, 7], [0, 7]]
        with Image.open(tmp_path / trial["views"][0]["file"]) as picture:
            assert picture.getpixel((60, 75)) == ((70, 95, 80) if closed else (255, 255, 255))
        submitted = await tools.call_tool("submit_elevation_reading", {"trial_id": body["trial_id"], "local_review": REVIEW})
        assert not submitted["isError"], submitted
        assert tools.submission.read()["artifact"]["facade_outline"] == outline
    asyncio.run(scenario())


@pytest.mark.parametrize("points,closed", [
    ([[0, 0], [80, 80], [80, 0], [0, 80]], True),
    ([[10, 10], [150, 10]], False),
])
def test_crossing_or_outside_facade_outline_is_rejected(tmp_path, points, closed):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        arguments["facade_outline"] = {"points_px": points, "closed": closed,
            "evidence_type": "pixels", "basis": "observed perimeter"}
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert result["isError"]
        assert "facade_outline" in result["structuredContent"]["reason"]
        assert tools.submission.read() is None
    asyncio.run(scenario())


def test_explicit_z_frame_cannot_waive_conflicting_located_levels(tmp_path):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        arguments["z_calibration"].update(world_start_m=10.5, world_end_m=0.5)
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        body = result["structuredContent"]
        assert not body["ready_for_delivery"]
        assert {row["level_id"] for row in body["blocking_issues"]} == {"roof", "ground"}
        rejected = await tools.call_tool("submit_elevation_reading", {"trial_id": body["trial_id"], "local_review": REVIEW})
        assert rejected["isError"] and tools.submission.read() is None
        fixed = await tools.call_tool("trial_elevation_reading", _node_arguments())
        assert fixed["structuredContent"]["ready_for_delivery"]
        assert fixed["structuredContent"]["coverage"]["facade_outline"] == "not_provided"
        accepted = await tools.call_tool("submit_elevation_reading", {
            "trial_id": fixed["structuredContent"]["trial_id"], "local_review": REVIEW})
        assert not accepted["isError"], accepted
    asyncio.run(scenario())


def test_projected_opening_outside_original_is_not_deliverable(tmp_path):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        arguments["openings"] = [{"id": "W1", "floor_id": "F1", "kind": "window",
            "opening_bbox": [40, 30, 90, 80], "width_m": 5, "sill_m": -2, "head_m": 8,
            "evidence_type": "annotation_and_pixels"}]
        arguments["counts"][0]["window_count"] = 1
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        body = result["structuredContent"]
        assert not body["ready_for_delivery"]
        assert body["blocking_issues"][0]["type"] == "adopted_projection_outside_image"
        rejected = await tools.call_tool("submit_elevation_reading", {"trial_id": body["trial_id"], "local_review": REVIEW})
        assert rejected["isError"]
    asyncio.run(scenario())


def test_wrong_source_pixel_frame_cannot_be_waived_by_local_review(tmp_path):
    async def scenario():
        picture = Image.new("RGB", (140, 120), "black")
        ImageDraw.Draw(picture).rectangle((40, 30, 90, 80), outline="cyan")
        tools = _node_tools(tmp_path, picture)
        arguments = _node_arguments()
        arguments["openings"] = [{"id": "W1", "floor_id": "F1", "kind": "window",
            "opening_bbox": [10, 12, 25, 25], "evidence_type": "pixels"}]
        arguments["counts"][0]["window_count"] = 1
        wrong = await tools.call_tool("trial_elevation_reading", arguments)
        assert not wrong["isError"], wrong
        body = wrong["structuredContent"]
        assert not body["ready_for_delivery"]
        assert body["blocking_issues"][0]["type"] == "opening_geometry_without_image_support"
        rejected = await tools.call_tool("submit_elevation_reading", {
            "trial_id": body["trial_id"], "local_review": REVIEW})
        assert rejected["isError"] and tools.submission.read() is None
        arguments["openings"][0]["opening_bbox"] = [40, 30, 90, 80]
        fixed = await tools.call_tool("trial_elevation_reading", arguments)
        assert fixed["structuredContent"]["ready_for_delivery"], fixed
        accepted = await tools.call_tool("submit_elevation_reading", {
            "trial_id": fixed["structuredContent"]["trial_id"], "local_review": REVIEW})
        assert not accepted["isError"], accepted
    asyncio.run(scenario())


@pytest.mark.parametrize("shape", ["diamond", "circle", "thin_diagonal", "extrema_only"])
def test_nonrectangular_or_thin_aperture_bbox_can_complete_trial_and_submit(tmp_path, shape):
    async def scenario():
        from src.agent.runtime_roles.elevation_trial import load_trial
        picture = Image.new("RGB", (140, 120), "black")
        draw = ImageDraw.Draw(picture)
        if shape == "diamond":
            draw.line([(65, 30), (90, 55), (65, 80), (40, 55), (65, 30)], fill="cyan", width=1)
        elif shape == "circle":
            draw.ellipse((40, 30, 90, 80), outline="cyan", width=1)
        elif shape == "thin_diagonal":
            draw.line((40, 30, 90, 80), fill=(16, 16, 16), width=1)
        else:
            # The old trimmed bands discarded corner-only evidence entirely.
            # One visible endpoint must make absence uncertain, not prove shape.
            draw.point((40, 30), fill="cyan")
            draw.point((90, 80), fill="cyan")
        tools = _node_tools(tmp_path, picture)
        arguments = _node_arguments()
        arguments["openings"] = [{"id": "W1", "floor_id": "F1", "kind": "window",
            "opening_bbox": [40, 30, 90, 80], "evidence_type": "pixels"}]
        arguments["counts"][0]["window_count"] = 1
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        body = result["structuredContent"]
        assert body["ready_for_delivery"] and not body["blocking_issues"], body
        assert all(row["type"] == "opening_geometry_weak_linear_support" for row in body["issues"])
        trial = load_trial(tools.submission, body["trial_id"])
        reading = trial["value"]["artifact"]["ink_alignment"]["openings"][0]
        assert not reading["applied_fields"]
        assert reading["effective_bbox_px"] == [40, 30, 90, 80]
        if reading["source_image_issue"] is not None:
            assert all(count > 0 for count in reading["source_image_issue"]["full_band_visible_pixels"].values())
        accepted = await tools.call_tool("submit_elevation_reading", {
            "trial_id": body["trial_id"], "local_review": REVIEW})
        assert not accepted["isError"], accepted
        assert tools.submission.read()["artifact"]["openings"][0]["opening_bbox"] == [40, 30, 90, 80]
    asyncio.run(scenario())


def test_annotation_crop_position_is_not_used_as_a_level_line(tmp_path):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        for row in arguments["elevations"]:
            row.pop("line_y_px")
            row["evidence_type"] = "annotation"
            row["bbox"] = [0, 50, 120, 52]
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        body = result["structuredContent"]
        assert body["ready_for_delivery"]
        assert body["coverage"]["located_level_line_ids"] == []
    asyncio.run(scenario())


def test_level_line_outside_original_is_rejected(tmp_path):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        arguments["elevations"][0]["line_y_px"] = 130
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert result["isError"]
        assert "line_y_px" in result["structuredContent"]["reason"]
    asyncio.run(scenario())


def test_missing_z_frame_uses_located_lines_instead_of_displaced_label_boxes(tmp_path):
    async def scenario():
        tools = _node_tools(tmp_path)
        arguments = _node_arguments()
        arguments.pop("z_calibration")
        arguments["elevations"][0]["bbox"] = [0, 15, 40, 25]
        arguments["elevations"][1]["bbox"] = [0, 95, 40, 105]
        result = await tools.call_tool("trial_elevation_reading", arguments)
        assert not result["isError"], result
        assert result["structuredContent"]["ready_for_delivery"], result
        derived = derive_z_calibration(arguments["elevations"])
        assert derived["calibration"] == {"pixel_start": 10.0, "pixel_end": 110.0,
                                           "world_start_m": 10.0, "world_end_m": 0.0}
        assert all(row["position_source"] == "line_y_px" for row in derived["points"])
    asyncio.run(scenario())
