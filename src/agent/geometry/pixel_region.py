"""Inspect a colour-connected image region without assigning BIM semantics.

The selected region is only a raster colour candidate.  Its boundary can follow
door swings, furniture, text, or other drawing marks, so callers must not treat
it as a room outline or as proof that an opening exists (or does not exist).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


MAX_IMAGE_PIXELS = 12_000_000
_MAX_SIMPLIFY_PIXELS = 5.0
_PANEL_GAP_PX = 8
_MAX_PREVIEW_EDGE_PX = 1600
_CROP_CONTEXT_PX = 16


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _seed(seed_pixel: Sequence[int], width: int, height: int) -> tuple[int, int]:
    if isinstance(seed_pixel, (str, bytes)) or len(seed_pixel) != 2:
        raise ValueError("seed_pixel must be a two-item (x, y) pixel coordinate")
    x, y = seed_pixel
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, np.integer))
        or not isinstance(y, (int, np.integer))
    ):
        raise ValueError("seed_pixel coordinates must be integers")
    x, y = int(x), int(y)
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(
            f"seed_pixel {(x, y)} is outside image bounds {(width, height)}"
        )
    return x, y


def _rgb(rgb: Sequence[int]) -> tuple[int, int, int]:
    if isinstance(rgb, (str, bytes)) or len(rgb) != 3:
        raise ValueError("background_rgb must contain three RGB channels")
    channels: list[int] = []
    for channel in rgb:
        if (
            isinstance(channel, bool)
            or not isinstance(channel, (int, np.integer))
            or not 0 <= int(channel) <= 255
        ):
            raise ValueError("background_rgb channels must be integers from 0 to 255")
        channels.append(int(channel))
    return channels[0], channels[1], channels[2]


def _run_rectangles(region: np.ndarray) -> BaseGeometry:
    """Union half-pixel rectangles for all horizontal runs in ``region``."""

    rectangles = []
    for y, row in enumerate(region):
        padded = np.pad(row.astype(np.int8, copy=False), (1, 1))
        transitions = np.diff(padded)
        starts = np.flatnonzero(transitions == 1)
        stops = np.flatnonzero(transitions == -1)
        rectangles.extend(
            box(float(x0) - 0.5, float(y) - 0.5, float(x1) - 0.5, float(y) + 0.5)
            for x0, x1 in zip(starts, stops, strict=True)
        )
    if not rectangles:
        raise ValueError("seed_pixel did not select a non-empty region")
    return unary_union(rectangles)


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    return [part for part in geometry.geoms if isinstance(part, Polygon)]


def _outline(draw: ImageDraw.ImageDraw, geometry: BaseGeometry, offset: tuple[int, int]) -> None:
    ox, oy = offset
    for polygon in _polygon_parts(geometry):
        rings = [polygon.exterior, *polygon.interiors]
        for ring in rings:
            points = [(x - ox, y - oy) for x, y in ring.coords]
            if len(points) >= 2:
                draw.line(points, fill=(255, 35, 95), width=2, joint="curve")


def _preview(
    image: Image.Image,
    region: np.ndarray,
    geometry: BaseGeometry,
    bbox: tuple[int, int, int, int],
) -> tuple[Image.Image, dict[str, Any]]:
    region_x0, region_y0, region_x1, region_y1 = bbox
    x0 = max(0, region_x0 - _CROP_CONTEXT_PX)
    y0 = max(0, region_y0 - _CROP_CONTEXT_PX)
    x1 = min(image.width, region_x1 + _CROP_CONTEXT_PX)
    y1 = min(image.height, region_y1 + _CROP_CONTEXT_PX)
    display_crop_bbox = (x0, y0, x1, y1)
    original = image.convert("RGB").crop(display_crop_bbox)
    region_crop = region[y0:y1, x0:x1]

    original_array = np.asarray(original, dtype=np.uint8)
    tinted_array = original_array.copy()
    tint = np.asarray((255, 150, 35), dtype=np.float32)
    tinted_array[region_crop] = np.rint(
        original_array[region_crop].astype(np.float32) * 0.55 + tint * 0.45
    ).astype(np.uint8)
    tinted = Image.fromarray(tinted_array, mode="RGB")
    _outline(ImageDraw.Draw(tinted), geometry, (x0, y0))

    crop_width, crop_height = original.size
    scale = min(
        1.0,
        (_MAX_PREVIEW_EDGE_PX - _PANEL_GAP_PX) / (2 * crop_width),
        _MAX_PREVIEW_EDGE_PX / crop_height,
    )
    display_width = max(1, round(crop_width * scale))
    display_height = max(1, round(crop_height * scale))
    if (display_width, display_height) != original.size:
        size = (display_width, display_height)
        original = original.resize(size, Image.Resampling.LANCZOS)
        tinted = tinted.resize(size, Image.Resampling.LANCZOS)

    preview = Image.new(
        "RGB",
        (2 * display_width + _PANEL_GAP_PX, display_height),
        (235, 235, 235),
    )
    preview.paste(original, (0, 0))
    right_x = display_width + _PANEL_GAP_PX
    preview.paste(tinted, (right_x, 0))

    coordinate_map = {
        "source_coordinate_convention": (
            "pixel centres are integer (x, y); pixel outer edges are half-integers"
        ),
        "bbox_convention": "[x_min, y_min, x_max_exclusive, y_max_exclusive]",
        "source_crop_bbox_px": [x0, y0, x1, y1],
        "region_bbox_px": [region_x0, region_y0, region_x1, region_y1],
        "preview_size_px": [preview.width, preview.height],
        "left_original_panel": {
            "offset_px": [0, 0],
            "size_px": [display_width, display_height],
        },
        "right_tinted_panel": {
            "offset_px": [right_x, 0],
            "size_px": [display_width, display_height],
        },
        "source_to_panel": {
            "formula": "panel_xy = (source_xy - source_crop_xy) * scale_xy + panel_offset_xy",
            "scale_xy": [display_width / crop_width, display_height / crop_height],
        },
    }
    return preview, coordinate_map


def render_pixel_region(
    image: Image.Image,
    seed_pixel: Sequence[int],
    background_rgb: Sequence[int],
    tolerance: float = 60,
    simplify_pixels: float = 1.5,
) -> tuple[Image.Image, dict[str, Any]]:
    """Render the 4-connected colour region containing ``seed_pixel``.

    A pixel is eligible when its Euclidean RGB distance from ``background_rgb``
    is at most ``tolerance``.  The returned polygon is an approximate observation
    aid.  Holes count enclosed non-matching image pixels only; they are not
    asserted building holes.
    """

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL.Image.Image")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("image must have positive width and height")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"image has {width * height} pixels; maximum is {MAX_IMAGE_PIXELS}"
        )

    x, y = _seed(seed_pixel, width, height)
    background = _rgb(background_rgb)
    tolerance_value = _number(tolerance, "tolerance")
    if tolerance_value < 0:
        raise ValueError("tolerance must be non-negative")
    simplify_value = _number(simplify_pixels, "simplify_pixels")
    if not 0 <= simplify_value <= _MAX_SIMPLIFY_PIXELS:
        raise ValueError("simplify_pixels must be between 0 and 5")

    rgb_array = np.asarray(image.convert("RGB"), dtype=np.int16)
    delta = rgb_array.astype(np.float32) - np.asarray(background, dtype=np.float32)
    colour_distance = np.linalg.norm(delta, axis=2)
    eligible = colour_distance <= tolerance_value
    if not bool(eligible[y, x]):
        actual_rgb = tuple(int(value) for value in rgb_array[y, x])
        raise ValueError(
            "seed_pixel does not match background_rgb within tolerance: "
            f"seed_rgb={actual_rgb}, distance={colour_distance[y, x]:.3f}, "
            f"tolerance={tolerance_value:g}"
        )

    labels, _ = ndimage.label(
        eligible,
        structure=np.asarray([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8),
    )
    seed_label = int(labels[y, x])
    region = labels == seed_label
    ys, xs = np.nonzero(region)
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    bbox = (x0, y0, x1, y1)

    exact_geometry = _run_rectangles(region)
    simplified = exact_geometry.simplify(simplify_value, preserve_topology=True)
    outer_polygons = _polygon_parts(simplified)
    outer_polygon_pixels: list[list[float]] | list[list[list[float]]]
    exteriors = [
        [[float(px), float(py)] for px, py in polygon.exterior.coords]
        for polygon in outer_polygons
    ]
    outer_polygon_pixels = exteriors[0] if len(exteriors) == 1 else exteriors

    exact_parts = _polygon_parts(exact_geometry)
    hole_count = sum(len(polygon.interiors) for polygon in exact_parts)
    touches_border = x0 == 0 or y0 == 0 or x1 == width or y1 == height
    warnings = [
        (
            "This is a colour-connected candidate, not a semantic room boundary "
            "or an opening acceptance result. Marks such as door arcs and furniture "
            "can alter its outline."
        )
    ]
    if touches_border:
        warnings.append(
            "The candidate touches the image border. This may indicate a colour "
            "leak or a valid crop edge; it does not prove that an opening exists "
            "or does not exist."
        )

    preview, coordinate_map = _preview(image, region, simplified, bbox)
    result: dict[str, Any] = {
        "region_pixel_count": int(region.sum()),
        "bbox_px": [x0, y0, x1, y1],
        "touches_image_border": touches_border,
        "outer_polygon_pixels": outer_polygon_pixels,
        "hole_count": hole_count,
        "holes_meaning": (
            "enclosed non-matching image regions only; not asserted building holes"
        ),
        "seed_pixel": [x, y],
        "background_rgb": list(background),
        "tolerance": tolerance_value,
        "simplify_pixels": simplify_value,
        "coordinate_map": coordinate_map,
        "warnings": warnings,
    }
    return preview, result


__all__ = ["MAX_IMAGE_PIXELS", "render_pixel_region"]
