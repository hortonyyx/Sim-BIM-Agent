"""Deterministic source-BIM elevation views, without original-image calibration.

The view is a source-geometry inspection aid.  It deliberately does not sample
drawing pixels, infer a facade from an image name, or certify drawing fidelity.
"""
from __future__ import annotations

import copy
import math

from PIL import Image, ImageDraw, ImageFont

from src.agent.geometry.opening_review import facade_inventory
from src.agent.geometry.source_image_overlay import _source_hash


_SIZE = (1200, 800)
_WALL = (130, 130, 130)
_FLOOR_LINE = (195, 195, 195)
_WINDOW = (255, 145, 0)
_DOOR = (0, 190, 90)
_TEXT = (235, 235, 235)
_BACKGROUND = (24, 24, 24)
_LABEL_BACKGROUND = (0, 0, 0)
_FACADES = {
    "South": ("x", 1, "+x"),
    "North": ("x", -1, "-x"),
    "East": ("y", 1, "+y"),
    "West": ("y", -1, "-y"),
}


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _vertices(value: object, *, name: str) -> list[tuple[float, float, float]]:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{name} requires at least three 3D vertices")
    result = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) < 3:
            raise ValueError(f"{name}[{index}] must be a 3D vertex")
        result.append((_number(point[0], name=f"{name}[{index}][0]"),
                       _number(point[1], name=f"{name}[{index}][1]"),
                       _number(point[2], name=f"{name}[{index}][2]")))
    return result


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _rounded(points: list[tuple[float, ...]]) -> list[list[float]]:
    return [[round(value, 6) for value in point] for point in points]


def _label(draw: ImageDraw.ImageDraw, point: tuple[float, float], text: str,
           *, font: ImageFont.ImageFont, fill: tuple[int, int, int]) -> None:
    left, top, right, bottom = draw.textbbox(point, text, font=font)
    draw.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=_LABEL_BACKGROUND)
    draw.text(point, text, font=font, fill=fill)


def render_source_elevation(source: dict, facade: str) -> tuple[Image.Image, dict]:
    """Draw an orthographic source elevation from a determinable cardinal facade.

    A facade's horizontal direction is intentionally explicit and stable:
    South ``+x``, North ``-x``, East ``+y``, West ``-y``.  Window and exterior
    door selection follows their actual source host boundary, never proposal
    facade text or a filename.
    """
    source_hash = _source_hash(source)
    if facade not in _FACADES:
        raise ValueError("facade must be North, South, East, or West")
    horizontal_axis, sign, direction = _FACADES[facade]
    axis_index = 0 if horizontal_axis == "x" else 1
    inventory = facade_inventory(source)
    if inventory["source_model_sha256"] != source_hash:
        raise ValueError("source facade inventory hash disagrees with source content")

    unsupported = [
        {"floor_id": floor["floor_id"], **copy.deepcopy(row)}
        for floor in inventory["floors"]
        for row in floor["unsupported_exterior_boundaries"]
    ]
    if unsupported:
        raise ValueError("facade view requires a fully determinable cardinal exterior scope")

    selected_boundary_ids = []
    for floor in inventory["floors"]:
        for row in floor["facades"]:
            if row["facade"] == facade:
                selected_boundary_ids.extend(row["exterior_boundary_ids"])
    selected_boundary_ids = sorted(set(selected_boundary_ids))
    if not selected_boundary_ids:
        raise ValueError(f"facade {facade} has no determinable exterior source walls")

    boundaries = {
        row.get("id"): row for row in source.get("boundaries", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    spaces = {
        row.get("id"): row for row in source.get("spaces", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    openings = {
        row.get("id"): row for row in source.get("openings", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if any(boundary_id not in boundaries for boundary_id in selected_boundary_ids):
        raise ValueError("facade inventory references an unknown source boundary")

    wall_rows = []
    for boundary_id in selected_boundary_ids:
        boundary = boundaries[boundary_id]
        vertices = _vertices(boundary.get("vertices"), name=f"boundary {boundary_id} vertices")
        space = spaces.get(boundary.get("space_id"))
        if space is None or not isinstance(space.get("floor_id"), str):
            raise ValueError(f"facade boundary {boundary_id} has no valid source space")
        wall_rows.append({
            "id": boundary_id,
            "space_id": boundary["space_id"],
            "floor_id": space["floor_id"],
            "world_vertices": vertices,
        })

    classifications = inventory["opening_classifications"]
    opening_rows = []
    for opening_id, classification in sorted(classifications.items()):
        if classification.get("facade") != facade:
            continue
        opening = openings.get(opening_id)
        if opening is None or opening.get("host_boundary_id") not in selected_boundary_ids:
            continue
        vertices = _vertices(opening.get("vertices"), name=f"opening {opening_id} vertices")
        kind = opening.get("kind")
        if kind not in {"window", "door", "open"}:
            raise ValueError(f"opening {opening_id} has unsupported kind")
        opening_rows.append({
            "id": opening_id,
            "kind": kind,
            "host_boundary_id": opening["host_boundary_id"],
            "space_ids": copy.deepcopy(opening.get("space_ids")),
            "floor_id": classification["floor_id"],
            "world_vertices": vertices,
        })

    def projected_world(point: tuple[float, float, float]) -> tuple[float, float]:
        return (sign * point[axis_index], point[2])

    all_points = [projected_world(point) for row in [*wall_rows, *opening_rows]
                  for point in row["world_vertices"]]
    if not all_points:
        raise ValueError(f"facade {facade} has no projectable source geometry")
    h_min, h_max = min(point[0] for point in all_points), max(point[0] for point in all_points)
    z_min, z_max = min(point[1] for point in all_points), max(point[1] for point in all_points)
    if h_min == h_max or z_min == z_max:
        raise ValueError("facade source geometry has a degenerate horizontal or vertical extent")
    h_pad = max(0.25, (h_max - h_min) * 0.05)
    z_pad = max(0.25, (z_max - z_min) * 0.08)
    left, right, top, bottom = 110, 50, 65, 95
    scale = min((_SIZE[0] - left - right) / (h_max - h_min + 2 * h_pad),
                (_SIZE[1] - top - bottom) / (z_max - z_min + 2 * z_pad))
    view_h_min = h_min - h_pad
    view_z_max = z_max + z_pad

    def project(point: tuple[float, float, float]) -> tuple[float, float]:
        horizontal, z = projected_world(point)
        return (left + (horizontal - view_h_min) * scale, top + (view_z_max - z) * scale)

    image = Image.new("RGB", _SIZE, _BACKGROUND)
    draw = ImageDraw.Draw(image)
    small_font, label_font = _font(16), _font(18)
    # A shared metre scale makes sill/head errors visible without reading a plan.
    for z_tick in range(math.ceil(z_min), math.floor(z_max) + 1):
        y_tick = top + (view_z_max - z_tick) * scale
        x_tick = left + (h_min - view_h_min) * scale
        draw.line([(x_tick - 7, y_tick), (x_tick, y_tick)], fill=_TEXT, width=1)
        draw.text((x_tick - 49, y_tick - 8), f"{z_tick:g} m", font=small_font, fill=_TEXT)

    floor_lines = []
    for floor in source.get("floors", []):
        if not isinstance(floor, dict) or not isinstance(floor.get("id"), str):
            continue
        matching = [row for row in wall_rows if row["floor_id"] == floor["id"]]
        if not matching:
            continue
        z = _number(floor.get("z_floor"), name=f"floor {floor['id']} z_floor")
        points = [projected_world(vertex) for row in matching for vertex in row["world_vertices"]]
        lo, hi = min(point[0] for point in points), max(point[0] for point in points)
        endpoints_world = [(sign * lo, z), (sign * hi, z)]
        endpoints_pixel = [(left + (lo - view_h_min) * scale, top + (view_z_max - z) * scale),
                            (left + (hi - view_h_min) * scale, top + (view_z_max - z) * scale)]
        draw.line(endpoints_pixel, fill=_FLOOR_LINE, width=2)
        _label(draw, (12, endpoints_pixel[0][1] - 9), f"{floor['id']} z={z:g} m",
               font=small_font, fill=_TEXT)
        floor_lines.append({
            "floor_id": floor["id"], "coordinate_axes": [horizontal_axis, "z"],
            "world_vertices": _rounded(endpoints_world),
            "pixel_vertices": _rounded(endpoints_pixel),
        })

    projected_walls = []
    for row in wall_rows:
        pixels = [project(point) for point in row["world_vertices"]]
        draw.line(pixels + [pixels[0]], fill=_WALL, width=3, joint="curve")
        projected_walls.append({
            **{key: row[key] for key in ("id", "space_id", "floor_id")},
            "world_vertices": _rounded(row["world_vertices"]),
            "pixel_vertices": _rounded(pixels),
        })

    projected_openings = []
    floors = {floor["id"]: floor for floor in source.get("floors", [])}
    opening_heights = []
    for row in opening_rows:
        pixels = [project(point) for point in row["world_vertices"]]
        colour = _WINDOW if row["kind"] == "window" else _DOOR
        draw.line(pixels + [pixels[0]], fill=colour, width=4, joint="curve")
        label_y = max(top, min(point[1] for point in pixels) - 20)
        _label(draw, (min(point[0] for point in pixels), label_y), row["id"], font=small_font, fill=colour)
        projected_openings.append({
            **{key: row[key] for key in ("id", "kind", "host_boundary_id", "space_ids", "floor_id")},
            "world_vertices": _rounded(row["world_vertices"]),
            "pixel_vertices": _rounded(pixels),
        })
        floor = floors[row["floor_id"]]
        base = _number(floor.get("z_floor"), name=f"floor {row['floor_id']} z_floor")
        zs = [point[2] for point in row["world_vertices"]]
        hs = [point[axis_index] for point in row["world_vertices"]]
        opening_heights.append({
            **{key: row[key] for key in ("id", "kind", "floor_id", "host_boundary_id")},
            "horizontal_span_m": [round(min(hs), 6), round(max(hs), 6)],
            "absolute_z_m": [round(min(zs), 6), round(max(zs), 6)],
            "floor_base_z_m": base,
            "above_floor_m": [round(min(zs) - base, 6), round(max(zs) - base, 6)],
            "opening_height_m": round(max(zs) - min(zs), 6),
        })

    draw.text((left, _SIZE[1] - 48),
              f"{facade} elevation  |  horizontal {horizontal_axis}: {direction} to right  |  metres",
              font=label_font, fill=_TEXT)
    draw.text((left, 18), "source geometry view - not visually verified", font=small_font, fill=_TEXT)
    legend_x = _SIZE[0] - right - 310
    draw.rectangle((legend_x, 18, _SIZE[0] - right, 46), fill=_LABEL_BACKGROUND)
    draw.text((legend_x + 8, 23), "gray wall  orange window  green door/open", font=_font(12), fill=_TEXT)

    metadata = {
        "mode": "source_elevation_view",
        "source_model_sha256": source_hash,
        "facade": facade,
        "horizontal_axis": horizontal_axis,
        "direction": direction,
        "image_size": list(_SIZE),
        "metres_per_pixel": 1 / scale,
        "world_view_extent_m": {
            "horizontal_projected": [round(h_min, 6), round(h_max, 6)],
            "z": [round(z_min, 6), round(z_max, 6)],
        },
        "projected_exterior_walls": projected_walls,
        "projected_openings": projected_openings,
        "projected_floor_lines": floor_lines,
        "opening_heights": opening_heights,
        "drawing_fidelity": "not_evaluated",
        "view_status": "not_visually_verified",
        "limits": [
            "Only source-derived cardinal exterior hosts on the selected facade are shown.",
            "This view does not inspect original drawing pixels, infer missing openings, or mutate source geometry.",
            "Projection coordinates are world horizontal-axis/Z values, not a calibration against an original image.",
            "All same-facing exterior hosts are projected; recess-depth occlusion is not resolved.",
        ],
    }
    return image, metadata
