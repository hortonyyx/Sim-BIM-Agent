"""Locate long neutral-ink samples inside submitted spaces, without wall inference.

Experimental review aid. Inputs are the original bitmap and compiler pixel
polygons; no GT, room labels, floor naming convention or metric scale is used.
"""
from __future__ import annotations

import math
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon


def _intervals(values):
    start = None
    for i, value in enumerate([*values, False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            yield start, i - 1
            start = None


def _raster(polygon, size, origin):
    mask = Image.new("1", size)
    draw = ImageDraw.Draw(mask)
    parts = list(polygon.geoms) if polygon.geom_type == "MultiPolygon" else [polygon]
    for part in parts:
        if part.is_empty:
            continue
        points = lambda ring: [(x-origin[0], y-origin[1]) for x, y in ring.coords]
        draw.polygon(points(part.exterior), fill=1)
        for ring in part.interiors:
            draw.polygon(points(ring), fill=0)
    return np.asarray(mask, dtype=bool)


def measure_space_ink(image, spaces, *, inset_fraction=0.12, min_support_fraction=0.85,
                      min_span_pixels=20, neutral_chroma=60, background_contrast=80):
    """Sample horizontal/vertical contiguous runs in each inset pixel polygon.

    Filtering thresholds are reported, not semantic wall or acceptance rules.
    The inset is relative to the submitted space, so a unit error does not
    silently disable observation. Furniture and drafting marks can be returned.
    """
    for name, value, lower, upper in (
        ("inset_fraction", inset_fraction, 0, 0.49),
        ("min_support_fraction", min_support_fraction, 0.01, 1),
        ("neutral_chroma", neutral_chroma, 0, 255),
        ("background_contrast", background_contrast, 1, 255),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{name} must be finite in {lower}..{upper}")
    if type(min_span_pixels) is not int or min_span_pixels < 2:
        raise ValueError("min_span_pixels must be an integer >=2")
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    reports, strokes, ids = [], [], set()
    for space in spaces:
        sid = space["space_id"]
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError("spaces require unique nonempty space_id values")
        ids.add(sid)
        points = np.asarray(space["pixel_polygon"], dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
            raise ValueError("pixel_polygon requires finite XY points")
        if (points < 0).any() or (points[:, 0] >= image.width).any() or (points[:, 1] >= image.height).any():
            raise ValueError("pixel polygon outside original image")
        polygon = Polygon(points)
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError("pixel polygon must be valid with positive area")
        x0, y0, x1, y1 = polygon.bounds
        box = [math.floor(x0), math.floor(y0), min(image.width, math.ceil(x1)+1), min(image.height, math.ceil(y1)+1)]
        inset = min(x1-x0, y1-y0) * inset_fraction
        interior = polygon.buffer(-inset, join_style=2)
        row = dict(space_id=sid, box_original_pixels=box, pixel_polygon=points.tolist(),
                   inset_pixels=inset, status="sampled", stroke_ids=[])
        reports.append(row)
        if interior.is_empty:
            row["status"] = "no_inset_interior"
            continue
        mask = _raster(interior, (box[2]-box[0], box[3]-box[1]), box[:2])
        crop = rgb[box[1]:box[3], box[0]:box[2]]
        pixels = crop[mask]
        if len(pixels) == 0:
            row["status"] = "no_pixel_samples"
            continue
        colours, counts = np.unique(pixels, axis=0, return_counts=True)
        background = colours[int(counts.argmax())]
        row["dominant_interior_rgb"] = background.tolist()
        ink = (np.ptp(crop, axis=2) <= neutral_chroma) & (
            np.abs(crop.mean(axis=2)-background.mean()) >= background_contrast)
        bands = []
        eligible = 0
        for axis in (0, 1):
            # axis is the varying location of parallel lines; support runs along
            # the other axis. Never bridge holes or disjoint concave intervals.
            membership, support = (mask.T, ink.T) if axis == 0 else (mask, ink)
            traces = []
            for cross, membership_line in enumerate(membership):
                for lo, hi in _intervals(membership_line):
                    length = hi-lo+1
                    if length < min_span_pixels:
                        continue
                    eligible += 1
                    count = int(support[cross, lo:hi+1].sum())
                    if count / length >= min_support_fraction:
                        traces.append((cross, lo, hi, count, length))
            # Only adjacent parallel pixels with identical sampled extents merge.
            for cross, lo, hi, count, length in traces:
                if bands and bands[-1]["axis"] == "xy"[axis] and bands[-1]["cross_pixels"][1]+1 == cross+box[axis] and bands[-1]["support_span_pixels"] == [lo+box[1-axis], hi+box[1-axis]]:
                    bands[-1]["cross_pixels"][1] = cross+box[axis]
                    bands[-1]["matching_pixel_counts"].append(count)
                else:
                    bands.append(dict(axis="xy"[axis], cross_pixels=[cross+box[axis]]*2,
                        support_span_pixels=[lo+box[1-axis], hi+box[1-axis]],
                        sample_count=length, matching_pixel_counts=[count]))
        row["eligible_line_count"] = eligible
        if eligible == 0:
            row["status"] = "below_sampling_span"
        for band in bands:
            band.update(id=f"I{len(strokes)+1:03d}", space_id=sid,
                        min_support_fraction=min(band["matching_pixel_counts"])/band["sample_count"])
            row["stroke_ids"].append(band["id"])
            strokes.append(band)
    return dict(schema_version="space_interior_ink_v1", image_size=list(image.size),
        parameters=dict(inset_fraction=inset_fraction, min_support_fraction=min_support_fraction,
                        min_span_pixels=min_span_pixels, neutral_chroma=neutral_chroma,
                        background_contrast=background_contrast),
        spaces=reports, strokes=strokes, drawing_fidelity="not_evaluated",
        interpretation="Long horizontal/vertical neutral ink inside the submitted space, not detected walls. Inspect the complete clean room and both sides of each line. It may be a partition, furniture, text or other drawing mark. No strokes is not proof of an open or correctly partitioned space.",
        limits=["Only axis-aligned, high-support neutral lines at the reported filter settings are sampled; oblique, short, interrupted or low-contrast lines may be missed.",
                "Inset boundary bands are excluded. Results depend on the submitted room polygon; unbuilt rooms outside it are not searched.",
                "No wall classification, room count, automatic repair or acceptance verdict."])


def render_space_ink(image, report, space_id):
    """Keep a full clean room beside a marked copy; report exact pixel mapping."""
    space, = [s for s in report["spaces"] if s["space_id"] == space_id]
    b = space["box_original_pixels"]
    padding = max(12, round(min(b[2]-b[0], b[3]-b[1]) * 0.08))
    box = [max(0, b[0]-padding), max(0, b[1]-padding), min(image.width, b[2]+padding), min(image.height, b[3]+padding)]
    clean = image.convert("RGB").crop(box)
    marked = clean.copy()
    draw = ImageDraw.Draw(marked)
    poly = [(p[0]-box[0], p[1]-box[1]) for p in space["pixel_polygon"]]
    draw.line([*poly, poly[0]], fill=(255, 70, 200), width=2)
    for stroke in report["strokes"]:
        if stroke["space_id"] != space_id:
            continue
        axis = "xy".index(stroke["axis"])
        cross = sum(stroke["cross_pixels"])/2
        points = [[cross, v] if axis == 0 else [v, cross] for v in stroke["support_span_pixels"]]
        points = [(p[0]-box[0], p[1]-box[1]) for p in points]
        draw.line(points, fill=(255, 160, 0), width=2)
        draw.text((points[0][0]+4, points[0][1]+4), stroke["id"], fill="yellow", stroke_width=1, stroke_fill="black")
    header, gap = 28, 8
    board = Image.new("RGB", (clean.width*2+gap, clean.height+header), "white")
    board.paste(clean, (0, header)); board.paste(marked, (clean.width+gap, header))
    ImageDraw.Draw(board).text((5, 5), f"{space_id} | clean original (left) / submitted room and sampled ink (right)", fill="black")
    native = list(board.size)
    board.thumbnail((1600, 1200))
    return board, dict(space_id=space_id, crop_original_pixels=box,
        native_size=native, returned_size=list(board.size),
        native_pixels_per_returned_pixel=[native[0]/board.width, native[1]/board.height],
        clean_panel_native_pixels=[0, header, clean.width, header+clean.height],
        marked_panel_native_pixels=[clean.width+gap, header, native[0], native[1]],
        mapping="Multiply returned coordinates by native_pixels_per_returned_pixel, subtract the chosen panel origin, then add crop_original_pixels origin. Stroke numeric coordinates already use original pixels.",
        drawing_fidelity="not_evaluated")
