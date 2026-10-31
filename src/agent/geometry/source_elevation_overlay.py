"""Project an actual source elevation onto a caller-calibrated original image."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageOps

from src.agent.geometry.source_elevation_view import render_source_elevation
from src.agent.geometry.source_image_overlay import _axis_anchors, _overlay_font


_OPENING_COLOURS = {"window": (255, 145, 0), "door": (215, 65, 225), "open": (215, 65, 225)}
_WALL_COLOUR = (0, 190, 255)
_FLOOR_COLOUR = (80, 210, 70)


def _opening_labels(image, openings, font):
    """Place IDs beside openings only on locally uniform pixels.

    This is conservative annotation layout, not drawing recognition. Busy or
    noisy regions may have no label; the opening inventory still carries IDs.
    Leaders leave gaps at existing ink instead of painting across it.
    """
    draw = ImageDraw.Draw(image)
    occupied = []
    labels = []

    def bounds(points):
        return (min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points))

    opening_boxes = [bounds(row["pixel_vertices"]) for row in openings]

    def intersects(a, b):
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    def blank(box):
        if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
            return False
        return all(high - low <= 12 for low, high in image.crop(box).getextrema())

    for index, row in enumerate(openings):
        left, top, right, bottom = opening_boxes[index]
        colour = _OPENING_COLOURS[row["kind"]]
        glyph = draw.textbbox((0, 0), row["id"], font=font)
        width, height = glyph[2] - glyph[0] + 8, glyph[3] - glyph[1] + 6
        placed = None
        for fraction in (0.5, 0.35, 0.65):
            if placed:
                break
            y = round(top + (bottom - top) * fraction)
            for gap in (8, 16, 28, 48, 72):
                if placed:
                    break
                for side in ("right", "left"):
                    x = math.ceil(right + gap) if side == "right" else math.floor(left - gap - width)
                    box = (x, y - height // 2, x + width, y - height // 2 + height)
                    guard = (box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3)
                    if any(intersects(guard, other) for other in opening_boxes + occupied) or not blank(guard):
                        continue
                    start = math.ceil(right + 3) if side == "right" else box[2] + 3
                    end = box[0] - 3 if side == "right" else math.floor(left - 3)
                    corridor = (start, y - 2, end, y + 2)
                    if any(intersects(corridor, other) for i, other in enumerate(opening_boxes) if i != index):
                        continue
                    # Determine segments before drawing, so the leader cannot
                    # mistake its own previous pixels for pre-existing ink.
                    pixels = [px for px in range(start, end + 1)
                              if blank((px - 2, y - 2, px + 3, y + 3))
                              and not any(intersects((px - 2, y - 2, px + 2, y + 2), b) for b in occupied)]
                    segments = []
                    for px in pixels:
                        if segments and px == segments[-1][1][0] + 1:
                            segments[-1][1][0] = px
                        else:
                            segments.append([[px, y], [px, y]])
                    for segment in segments:
                        draw.line([tuple(p) for p in segment], fill=colour, width=1)
                    draw.rounded_rectangle(box, radius=2, fill=(20, 20, 20))
                    draw.text((box[0] + 4 - glyph[0], box[1] + 3 - glyph[1]),
                              row["id"], fill=colour, font=font)
                    occupied.append(guard)
                    placed = {"id": row["id"], "status": "placed", "side": side,
                              "box_original_pixels": list(box), "leader_segments": segments}
                    break
        labels.append(placed or {"id": row["id"], "status": "omitted",
                                 "reason": "No clear side position; use the opening inventory for the ID."})
    return labels


def render_elevation_overlay(source, original, *, facade, horizontal_anchors, z_anchors, basis):
    """Use two original-pixel/world-metre anchors per axis; never fit source geometry.

    Horizontal metres are world x on North/South and world y on East/West,
    without a second sign reversal. Vertical metres are absolute source z.
    Only axis-aligned drawings are supported. Calibration is caller evidence,
    not a correspondence or fidelity verdict.
    """
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("basis must describe the observed horizontal and absolute-z references")
    image = ImageOps.grayscale(original).convert("RGB")
    hs, ho, horizontal = _axis_anchors(horizontal_anchors, axis="horizontal", size=image.width)
    zs, zo, vertical = _axis_anchors(z_anchors, axis="z", size=image.height)
    _, view = render_source_elevation(source, facade)
    axis = 0 if view["horizontal_axis"] == "x" else 1
    draw = ImageDraw.Draw(image)
    font = _overlay_font(image)

    def project(h, z):
        return [(h - ho) / hs, (z - zo) / zs]

    def record(row, points):
        return {**row, "pixel_vertices": [[round(v, 6) for v in p] for p in points],
                "out_of_image": any(not (0 <= x < image.width and 0 <= y < image.height)
                                    for x, y in points)}

    walls, openings, floors = [], [], []
    for row in view["projected_exterior_walls"]:
        points = [project(p[axis], p[2]) for p in row["world_vertices"]]
        draw.line(points + [points[0]], fill=_WALL_COLOUR, width=1)
        walls.append(record(row, points))
    for row in view["projected_floor_lines"]:
        points = [project(h, z) for h, z in row["world_vertices"]]
        draw.line(points, fill=_FLOOR_COLOUR, width=1)
        floors.append(record(row, points))
    for row in view["projected_openings"]:
        points = [project(p[axis], p[2]) for p in row["world_vertices"]]
        colour = _OPENING_COLOURS[row["kind"]]
        draw.line(points + [points[0]], fill=colour, width=2)
        openings.append(record(row, points))

    labels = _opening_labels(image, openings, font)

    warnings = []
    if abs(abs(hs / zs) - 1) > 0.05:
        warnings.append("Horizontal and vertical scales differ by more than 5%; inspect the anchor references.")
    return image, {
        "mode": "source_elevation_overlay",
        "source_model_sha256": view["source_model_sha256"],
        "facade": facade,
        "horizontal_axis": view["horizontal_axis"],
        "direction": ("+" if hs > 0 else "-") + view["horizontal_axis"],
        "image_size": list(image.size),
        "reference_background": "grayscale_copy_original_file_unchanged",
        "opening_colours_rgb": {kind: list(colour) for kind, colour in _OPENING_COLOURS.items()},
        "overlay_colours_rgb": {
            "exterior_wall_edges": list(_WALL_COLOUR),
            "floor_lines": list(_FLOOR_COLOUR),
            **{kind: list(colour) for kind, colour in _OPENING_COLOURS.items()},
        },
        "legend": {
            "gray": "original drawing",
            "cyan": "source exterior wall edges, including per-space wall segmentation",
            "green": "source floor datum lines",
            "orange": "source windows",
            "purple": "source doors and open passages",
        },
        "anchors": {"horizontal": horizontal, "absolute_z": vertical},
        "basis": basis.strip(),
        "calibration_unverified": True,
        "transform": {"horizontal_metres_per_pixel": hs, "horizontal_offset_m": ho,
                      "z_metres_per_pixel": zs, "z_offset_m": zo},
        "calibration_warnings": warnings,
        "projected_exterior_walls": walls,
        "projected_openings": openings,
        "opening_labels": labels,
        "projected_floor_lines": floors,
        "opening_heights": view["opening_heights"],
        "drawing_fidelity": "not_evaluated",
        "view_status": "not_visually_verified",
        "limits": [
            "Caller selects the image/facade and original-pixel/world-metre references; neither is independently verified.",
            "Horizontal values are world x/y, not distance from the facade's left edge; z is absolute, not above-floor height.",
            "Only axis-aligned drawings are supported; no rotation, perspective or automatic pixel matching.",
            "All same-facing exterior hosts are projected; recess-depth occlusion is not resolved.",
            "The reference background is grayscale; consult the original colour drawing for colour conventions. All projected geometry is coloured: cyan wall edges, green floor datum lines, orange windows, purple doors/open passages.",
            "Cyan wall edges include per-space segmentation; these edges do not establish visible joints in the original facade.",
            "IDs use clear side regions; labels may be omitted on busy images. Leaders skip existing ink. IDs remain in the opening inventory.",
            "No source geometry is changed. Repeat with the same observed anchors after revision; do not fit anchors to generated openings.",
        ],
    }
