"""Render a literal pixel declaration over its original plan image.

This view exists before source-geometry compilation.  It deliberately draws
only caller-declared coordinates: no snapping, extension, clipping, host
selection, space inference, or geometry repair is performed.
"""
from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageDraw


_FOOTPRINT = (0, 190, 255, 255)
_PARTITION = (255, 30, 210, 255)
_OPENING = (255, 145, 0, 255)
_LABEL_BG = (15, 15, 15, 220)


def _feature_id(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _point(value: Any, *, path: str, size: tuple[int, int], errors: list[dict]) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append({"path": path, "reason": "point must be [pixel_x, pixel_y]"})
        return None
    if any(isinstance(axis, bool) or not isinstance(axis, (int, float)) for axis in value):
        errors.append({"path": path, "declared_value": value,
                       "reason": "point coordinates must be finite numbers"})
        return None
    x, y = float(value[0]), float(value[1])
    if not math.isfinite(x) or not math.isfinite(y):
        errors.append({"path": path, "declared_value": value,
                       "reason": "point coordinates must be finite numbers"})
        return None
    if not (0 <= x < size[0] and 0 <= y < size[1]):
        errors.append({"path": path, "declared_value": value,
                       "reason": f"point is outside original image bounds {size[0]}x{size[1]}"})
        return None
    return x, y


def _label(
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float],
    text: str,
    *,
    size: tuple[int, int],
) -> None:
    x = min(max(round(point[0]) + 3, 0), size[0] - 1)
    y = min(max(round(point[1]) + 3, 0), size[1] - 1)
    bounds = draw.textbbox((x, y), text)
    draw.rectangle(bounds, fill=_LABEL_BG)
    draw.text((x, y), text, fill="white")


def _draw_runs(
    draw: ImageDraw.ImageDraw,
    raw_points: Any,
    *,
    path: str,
    color: tuple[int, int, int, int],
    width: int,
    errors: list[dict],
    size: tuple[int, int],
    close: bool = False,
) -> tuple[int, list[list[Any]], tuple[float, float] | None]:
    if not isinstance(raw_points, list):
        errors.append({"path": path, "reason": "points must be a list"})
        return 0, [], None
    drawable = [
        _point(value, path=f"{path}[{index}]", size=size, errors=errors)
        for index, value in enumerate(raw_points)
    ]
    segments = 0
    label_point = None
    run: list[tuple[float, float]] = []
    for point in [*drawable, None]:
        if point is not None:
            run.append(point)
            continue
        if len(run) >= 2:
            draw.line(run, fill=color, width=width, joint="curve")
            segments += len(run) - 1
            if label_point is None:
                label_point = ((run[0][0] + run[1][0]) / 2, (run[0][1] + run[1][1]) / 2)
        run = []
    if close and len(drawable) >= 3 and all(point is not None for point in drawable):
        first, last = drawable[0], drawable[-1]
        if first != last:
            draw.line([last, first], fill=color, width=width)
            segments += 1
    radius = max(2, width)
    for point in drawable:
        if point is not None:
            draw.ellipse((point[0] - radius, point[1] - radius,
                          point[0] + radius, point[1] + radius), fill=color)
    return segments, raw_points, label_point


def render_plan_draft(
    image: Image.Image,
    plan: Any,
    *,
    image_name: str,
    image_sha256: str,
    plan_file: str,
    plan_sha256: str,
) -> tuple[Image.Image, dict]:
    """Return an original-pixel declaration view and its complete sidecar data."""
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL.Image.Image")
    if not isinstance(image_name, str) or not image_name:
        raise ValueError("image_name must be a nonempty string")
    if not isinstance(plan_file, str) or not plan_file:
        raise ValueError("plan_file must be a nonempty string")

    preview = image.convert("RGBA")
    overlay = Image.new("RGBA", preview.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width = max(2, round(min(preview.size) / 250))
    errors: list[dict] = []
    rendered = {"footprint": [], "partitions": [], "openings": []}

    if not isinstance(plan, dict):
        errors.append({"path": "plan", "reason": "parsed plan must be an object"})
    else:
        footprint = plan.get("footprint_pixels")
        count, raw, _ = _draw_runs(
            draw, footprint, path="plan.footprint_pixels", color=_FOOTPRINT,
            width=width + 2, errors=errors, size=preview.size, close=True,
        )
        rendered["footprint"].append({
            "id": "footprint", "declared_pixel_points": raw,
            "drawn_segment_count": count,
            "closure_semantics": "closed only as declared footprint-ring semantics",
        })

        partitions = plan.get("partitions")
        if not isinstance(partitions, list):
            errors.append({"path": "plan.partitions", "reason": "partitions must be a list"})
        else:
            for index, item in enumerate(partitions):
                path = f"plan.partitions[{index}]"
                if not isinstance(item, dict):
                    errors.append({"path": path, "reason": "partition must be an object"})
                    continue
                feature_id = _feature_id(item.get("id"), f"partition[{index}]")
                count, raw, label_point = _draw_runs(
                    draw, item.get("points"), path=f"{path}.points", color=_PARTITION,
                    width=width + 1, errors=errors, size=preview.size,
                )
                rendered["partitions"].append({
                    "id": feature_id, "declared_pixel_points": raw,
                    "drawn_segment_count": count,
                })
                if label_point is not None:
                    _label(draw, label_point, f"P:{feature_id}", size=preview.size)

        openings = plan.get("openings")
        if not isinstance(openings, list):
            errors.append({"path": "plan.openings", "reason": "openings must be a list"})
        else:
            for index, item in enumerate(openings):
                path = f"plan.openings[{index}]"
                if not isinstance(item, dict):
                    errors.append({"path": path, "reason": "opening must be an object"})
                    continue
                feature_id = _feature_id(item.get("id"), f"opening[{index}]")
                raw_points = [item.get("p1"), item.get("p2")]
                before = len(errors)
                first = _point(raw_points[0], path=f"{path}.p1", size=preview.size, errors=errors)
                second = _point(raw_points[1], path=f"{path}.p2", size=preview.size, errors=errors)
                drawn = first is not None and second is not None
                if drawn:
                    # Draw the full declared segment.  It is never shortened to a host.
                    draw.line([first, second], fill=_OPENING, width=width + 4)
                    radius = width + 3
                    for point in (first, second):
                        draw.ellipse((point[0] - radius, point[1] - radius,
                                      point[0] + radius, point[1] + radius), fill=_OPENING)
                    midpoint = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
                    _label(draw, midpoint, f"O:{feature_id}", size=preview.size)
                rendered["openings"].append({
                    "id": feature_id, "kind": item.get("kind"),
                    "declared_p1": item.get("p1"), "declared_p2": item.get("p2"),
                    "drawn": drawn, "unrenderable_count": len(errors) - before,
                })

    composite = Image.alpha_composite(preview, overlay).convert("RGB")
    legend_height = 24
    output_width = max(composite.width, 440)
    preview = Image.new("RGB", (output_width, composite.height + legend_height), "white")
    preview.paste(composite, (0, 0))
    legend = ImageDraw.Draw(preview)
    legend.text((3, composite.height + 5),
                "DRAFT ONLY  cyan=footprint  magenta=partition  orange=opening",
                fill=(0, 0, 0))
    metadata = {
        "schema_version": "plan_draft_view_v1",
        "mode": "draft_only",
        "draft_only": True,
        "drawing_fidelity": "not_evaluated",
        "source_geometry_ready": False,
        "source_bim": False,
        "room_status": "not_inferred_or_claimed",
        "image": {"name": image_name, "sha256": image_sha256,
                  "size": [image.width, image.height]},
        "plan": {"file": plan_file, "sha256": plan_sha256},
        "declaration": plan,
        "rendered": rendered,
        "unrenderable": errors,
        "method": {
            "coordinate_frame": "identity_original_image_pixels",
            "original_image_box_in_preview": [0, 0, image.width, image.height],
            "preview_output_size": [preview.width, preview.height],
            "endpoints_modified": False,
            "endpoint_markers_centered_at_declared_points": True,
            "partitions_extended_or_snapped": False,
            "openings_clipped_or_hosted": False,
            "spaces_or_rooms_derived": False,
        },
        "scope": (
            "Literal caller-declared pixel linework over the original image. This is neither "
            "a source BIM nor evidence that walls, openings, rooms, or drawing fidelity are valid."
        ),
    }
    return preview, metadata


def render_opening_host_failure(
    image: Image.Image,
    plan: dict,
    *,
    opening_id: str,
    p1_pixel: list[float],
    p2_pixel: list[float],
    image_name: str,
    image_sha256: str,
    plan_file: str,
    plan_sha256: str,
) -> tuple[Image.Image, dict]:
    """Place a clean local crop beside the literal declaration that failed hosting."""
    original = image.convert("RGB")
    points = [tuple(p1_pixel), tuple(p2_pixel)]
    if not all(0 <= x < original.width and 0 <= y < original.height for x, y in points):
        raise ValueError("opening failure endpoints are outside the original image")
    span = max(abs(points[1][0] - points[0][0]), abs(points[1][1] - points[0][1]))
    margin = max(60, min(220, round(span * 1.5)))
    crop_box = [
        max(0, math.floor(min(point[0] for point in points) - margin)),
        max(0, math.floor(min(point[1] for point in points) - margin)),
        min(original.width, math.ceil(max(point[0] for point in points) + margin + 1)),
        min(original.height, math.ceil(max(point[1] for point in points) + margin + 1)),
    ]
    clean = original.crop(tuple(crop_box))
    marked = original.convert("RGBA")
    ink = Image.new("RGBA", original.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ink)

    footprint = plan.get("footprint_pixels", [])
    if isinstance(footprint, list) and len(footprint) >= 2:
        draw.line([*map(tuple, footprint), tuple(footprint[0])], fill=_FOOTPRINT, width=2)
    for partition in plan.get("partitions", []):
        path = partition.get("points") if isinstance(partition, dict) else None
        if isinstance(path, list) and len(path) >= 2:
            draw.line([tuple(point) for point in path], fill=_PARTITION, width=2)
    draw.line(points, fill=_OPENING, width=3)
    for x, y in points:
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=_OPENING)
    marked = Image.alpha_composite(marked, ink).convert("RGB").crop(tuple(crop_box))

    scale = min(4.0, 780 / clean.width, 1450 / clean.height)
    panel_size = (max(1, round(clean.width * scale)), max(1, round(clean.height * scale)))
    clean = clean.resize(panel_size, Image.Resampling.NEAREST)
    marked = marked.resize(panel_size, Image.Resampling.NEAREST)
    header, gutter = 24, 12
    output = Image.new("RGB", (panel_size[0] * 2 + gutter, panel_size[1] + header), "white")
    output.paste(clean, (0, header))
    output.paste(marked, (panel_size[0] + gutter, header))
    title = ImageDraw.Draw(output)
    title.text((3, 5), "ORIGINAL CROP", fill="black")
    title.text((panel_size[0] + gutter + 3, 5), f"DECLARED {opening_id}", fill="black")
    metadata = {
        "schema_version": "opening_host_failure_view_v1",
        "mode": "declaration_feedback_only",
        "image": {"name": image_name, "sha256": image_sha256,
                  "size": [original.width, original.height]},
        "plan": {"file": plan_file, "sha256": plan_sha256},
        "opening": {"id": opening_id, "p1_original_pixels": p1_pixel,
                    "p2_original_pixels": p2_pixel, "full_declared_segment_preserved": True},
        "crop_original_pixels": crop_box,
        "panels": {
            "clean_original": [0, header, panel_size[0], header + panel_size[1]],
            "declaration": [panel_size[0] + gutter, header,
                            panel_size[0] * 2 + gutter, header + panel_size[1]],
        },
        "panel_size_pixels": list(panel_size),
        "display_scale": [panel_size[0] / (crop_box[2] - crop_box[0]),
                          panel_size[1] / (crop_box[3] - crop_box[1])],
        "mapping": (
            "returned-image pixel = panel origin + (original pixel - original crop origin) "
            "* display_scale; use clean_original or declaration panel origin from panels. "
            "Both panels show the same original crop."
        ),
        "drawing_fidelity": "not_evaluated",
        "source_geometry_ready": False,
        "note": (
            "Left is the clean original drawing; right overlays only caller-declared "
            "footprint, partitions and the full opening. This does not identify a valid host. "
            "Recheck the original wall and aperture before changing the declaration; do not "
            "shorten an observed opening merely to clear a host error."
        ),
    }
    return output, metadata
