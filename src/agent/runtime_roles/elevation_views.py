"""Render the exact adopted elevation geometry for both reader entrypoints."""

import math
from PIL import Image, ImageDraw
from .plan_review import box


def _world(calibration, pixel):
    return calibration["world_start_m"] + (pixel - calibration["pixel_start"]) * (
        calibration["world_end_m"] - calibration["world_start_m"]) / (
        calibration["pixel_end"] - calibration["pixel_start"])


def _pixel(calibration, world):
    return calibration["pixel_start"] + (world - calibration["world_start_m"]) * (
        calibration["pixel_end"] - calibration["pixel_start"]) / (
        calibration["world_end_m"] - calibration["world_start_m"])


def _check_image_coordinates(artifact, size):
    for axis, limit in (("x_calibration", size[0]), ("z_calibration", size[1])):
        calibration = artifact.get(axis)
        if calibration is not None and not (0 <= calibration["pixel_start"] < calibration["pixel_end"] <= limit):
            raise ValueError(f"{axis} pixel anchors exceed original image {size}")
    for row in artifact["elevations"] + artifact["openings"]:
        box(row["bbox"], size)
    for row in artifact["elevations"]:
        if row.get("line_y_px") is not None and not 0 <= row["line_y_px"] <= size[1]:
            raise ValueError(f"level {row['id']} line_y_px exceeds original image {size}")
    for row in artifact["openings"]:
        if not 0 <= row["x_px"][0] < row["x_px"][1] <= size[0]:
            raise ValueError(f"opening {row['id']} x_px exceeds original image {size}")
        if row.get("opening_bbox") is not None:
            box(row["opening_bbox"], size)
    for point in artifact.get("facade_outline", {}).get("points_px", []):
        if not (0 <= point[0] <= size[0] and 0 <= point[1] <= size[1]):
            raise ValueError(f"facade_outline point {point} exceeds original image {size}")


def _adopted_geometry(artifact, size):
    """Project the adopted width and absolute Z, not the submitted evidence box."""
    xcal, zcal = artifact["x_calibration"], artifact["z_calibration"]
    openings, levels, issues = [], [], []
    x_scale = abs((xcal["world_end_m"] - xcal["world_start_m"]) /
                  (xcal["pixel_end"] - xcal["pixel_start"]))
    for row in artifact["openings"]:
        centre = _world(xcal, sum(row["x_px"]) / 2)
        span = [centre - row["width_m"] / 2, centre + row["width_m"] / 2]
        left, right = sorted(_pixel(xcal, value) for value in span)
        bbox = [left, _pixel(zcal, row["head_m"]), right, _pixel(zcal, row["sill_m"])]
        if not all(math.isfinite(value) for value in bbox):
            raise ValueError(f"opening {row['id']} cannot be projected to finite pixels")
        openings.append({**{key: row[key] for key in ("id", "floor_id", "kind", "width_m",
            "sill_m", "head_m", "evidence_type")}, "x_px": row["x_px"],
            "world_span_m": span, "overlay_bbox_px": bbox})
        pixel_width = (row["x_px"][1] - row["x_px"][0]) * x_scale
        tolerance = max(0.05, 2 * x_scale)
        if abs(pixel_width - row["width_m"]) > tolerance + 1e-9:
            issues.append({"type": "width_span_conflict", "opening_id": row["id"],
                "floor_id": row["floor_id"], "adopted_width_m": row["width_m"],
                "pixel_span_width_m": pixel_width, "difference_m": abs(pixel_width - row["width_m"]),
                "tolerance_m": tolerance})
        if left < 0 or right > size[0] or bbox[1] < 0 or bbox[3] > size[1]:
            issues.append({"type": "adopted_projection_outside_image", "opening_id": row["id"],
                "floor_id": row["floor_id"], "overlay_bbox_px": bbox})
    for row in artifact["elevations"]:
        y = _pixel(zcal, row["value_m"])
        if not math.isfinite(y):
            raise ValueError(f"level {row['id']} cannot be projected to finite pixels")
        levels.append({**{key: row[key] for key in ("id", "floor_id", "kind", "value_m", "evidence_type", "line_y_px") if key in row},
            "overlay_y_px": y})
        if not 0 <= y <= size[1]:
            issues.append({"type": "adopted_projection_outside_image", "level_id": row["id"],
                "overlay_y_px": y})
    for row in artifact["ink_alignment"]["openings"]:
        for issue in row["inconsistencies"]:
            issues.append({"type": "retained_ink_conflict", "opening_id": row["id"], **issue})
    return openings, levels, issues


def _overlay(original, artifact, openings, levels):
    picture = original.copy()
    draw = ImageDraw.Draw(picture)
    width = max(1, round(min(picture.size) / 400))
    xcal = artifact["x_calibration"]
    outline = artifact.get("facade_outline")
    if outline:
        points = [tuple(point) for point in outline["points_px"]]
        if outline["closed"] and points[-1] != points[0]:
            points.append(points[0])
        draw.line(points, fill=(70, 95, 80), width=width + 1)
    for level in levels:
        y = level["overlay_y_px"]
        draw.line([(xcal["pixel_start"], y), (xcal["pixel_end"], y)], fill=(190, 70, 180), width=width)
        draw.text((max(0, xcal["pixel_start"]), max(0, min(y + 2, picture.height - 14))),
                  f"{level['id']} Z={level['value_m']:g}m", fill=(140, 25, 130))
    for row in openings:
        rectangle = row["overlay_bbox_px"]
        colour = (0, 115, 235) if row["kind"] == "window" else (235, 100, 0)
        draw.rectangle(rectangle, outline=colour, width=width + 1)
        draw.text((max(0, rectangle[0]), max(0, rectangle[1] - 13)),
                  f"{row['id']} w={row['width_m']:g} Z={row['sill_m']:g}..{row['head_m']:g}", fill=colour)
    return picture


def reading_views(original, artifact):
    _check_image_coordinates(artifact, original.size)
    openings, levels, issues = _adopted_geometry(artifact, original.size)
    # Match the plan review convention: original ink gray, adopted values color.
    overlay = _overlay(original.convert("L").convert("RGB"), artifact, openings, levels)
    diagram = _overlay(Image.new("RGB", original.size, "white"), artifact, openings, levels)
    ImageDraw.Draw(diagram).text((10, 10),
        f"{artifact['orientation']} - adopted opening and level readings (absolute Z, metres)", fill="black")
    outline = artifact.get("facade_outline")
    coverage = {"facade_outline": ("closed_perimeter" if outline["closed"] else "open_chain")
                if outline else "not_provided", "openings": "read", "levels": "read",
                "facade_outline_evidence_type": outline["evidence_type"] if outline else None,
                "located_level_line_ids": [row["id"] for row in artifact["elevations"]
                                           if row.get("line_y_px") is not None]}
    ImageDraw.Draw(diagram).text((10, 26), "Facade outline: " + coverage["facade_outline"], fill="black")
    observed_outline = None
    if outline:
        observed_outline = {**outline, "world_xz_points_m": [
            [_world(artifact["x_calibration"], x), _world(artifact["z_calibration"], y)]
            for x, y in outline["points_px"]]}
    return diagram, overlay, {"openings": openings, "levels": levels, "issues": issues,
                             "facade_outline": observed_outline, "coverage": coverage}
