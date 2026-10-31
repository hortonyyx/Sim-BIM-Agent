"""Render bounded, non-semantic colour-region candidates for an entire image."""

from __future__ import annotations

from math import isfinite
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .pixel_region import MAX_IMAGE_PIXELS


_FOUR_CONNECTED = np.asarray([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
_PANEL_GAP_PX = 8
_MAX_PREVIEW_EDGE_PX = 1600
_COLOURS = (
    (230, 75, 75),
    (55, 145, 235),
    (55, 180, 105),
    (235, 155, 45),
    (165, 95, 210),
    (45, 180, 185),
)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _rgb(rgb: Sequence[int]) -> tuple[int, int, int]:
    if isinstance(rgb, (str, bytes)) or len(rgb) != 3:
        raise ValueError("background_rgb must contain three RGB channels")
    result: list[int] = []
    for channel in rgb:
        if (
            isinstance(channel, bool)
            or not isinstance(channel, (int, np.integer))
            or not 0 <= int(channel) <= 255
        ):
            raise ValueError("background_rgb channels must be integers from 0 to 255")
        result.append(int(channel))
    return result[0], result[1], result[2]


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be a positive integer")
    result = int(value)
    if result < 1:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _seed_pixel(mask: np.ndarray) -> list[int]:
    """Choose a reproducible interior pixel, preferring the deepest one."""

    distances = ndimage.distance_transform_edt(mask)
    y, x = np.unravel_index(int(np.argmax(distances)), mask.shape)
    # argmax of a non-empty mask is necessarily within the mask.  Keep the
    # assertion close to this promise because callers use this point as a seed.
    assert bool(mask[y, x])
    return [int(x), int(y)]


def render_pixel_region_overview(
    image: Image.Image,
    background_rgb: Sequence[int],
    tolerance: float = 60,
    min_pixels: int = 500,
    max_regions: int = 40,
    include_border: bool = False,
) -> tuple[Image.Image, dict[str, Any]]:
    """Show the largest 4-connected colour candidates without BIM semantics.

    Eligible pixels are within Euclidean RGB ``tolerance`` of ``background_rgb``.
    The returned candidates are raster observations only: furniture, text, door
    swings, crop edges, and drawing marks may determine their shapes.
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
    if not isinstance(include_border, bool):
        raise ValueError("include_border must be a boolean")

    background = _rgb(background_rgb)
    tolerance_value = _number(tolerance, "tolerance")
    if tolerance_value < 0:
        raise ValueError("tolerance must be non-negative")
    minimum = _positive_integer(min_pixels, "min_pixels")
    maximum = _positive_integer(max_regions, "max_regions")

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    delta = rgb.astype(np.float32) - np.asarray(background, dtype=np.float32)
    eligible = np.linalg.norm(delta, axis=2) <= tolerance_value
    labels, region_count = ndimage.label(eligible, structure=_FOUR_CONNECTED)
    counts = np.bincount(labels.ravel(), minlength=region_count + 1)
    slices = ndimage.find_objects(labels)

    qualifying: list[dict[str, Any]] = []
    too_small = 0
    border_excluded = 0
    for label, region_slice in enumerate(slices, start=1):
        if region_slice is None:  # Defensive; scipy normally supplies every label.
            continue
        y_slice, x_slice = region_slice
        x0, x1 = int(x_slice.start), int(x_slice.stop)
        y0, y1 = int(y_slice.start), int(y_slice.stop)
        pixel_count = int(counts[label])
        touches_border = x0 == 0 or y0 == 0 or x1 == width or y1 == height
        if pixel_count < minimum:
            too_small += 1
            continue
        if touches_border and not include_border:
            border_excluded += 1
            continue
        qualifying.append(
            {
                "label": label,
                "pixel_count": pixel_count,
                "bbox_px": [x0, y0, x1, y1],
                "touches_image_border": touches_border,
            }
        )

    # Python's sort is stable, so equal-area components retain scipy's row-major
    # label order and candidate identifiers stay reproducible.
    qualifying.sort(key=lambda item: -int(item["pixel_count"]))
    truncated = max(0, len(qualifying) - maximum)
    selected = qualifying[:maximum]

    source = image.convert("RGB")
    tinted_array = np.asarray(source, dtype=np.uint8).copy()
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(selected, start=1):
        mask = labels == int(item["label"])
        colour = np.asarray(_COLOURS[(index - 1) % len(_COLOURS)], dtype=np.float32)
        tinted_array[mask] = np.rint(
            tinted_array[mask].astype(np.float32) * 0.42 + colour * 0.58
        ).astype(np.uint8)
        candidates.append(
            {
                "id": f"R{index:02d}",
                "seed_pixel": _seed_pixel(mask),
                "pixel_count": int(item["pixel_count"]),
                "bbox_px": item["bbox_px"],
                "touches_image_border": bool(item["touches_image_border"]),
            }
        )

    tinted = Image.fromarray(tinted_array, mode="RGB")
    scale = min(
        1.0,
        (_MAX_PREVIEW_EDGE_PX - _PANEL_GAP_PX) / (2 * width),
        _MAX_PREVIEW_EDGE_PX / height,
    )
    panel_width = max(1, round(width * scale))
    panel_height = max(1, round(height * scale))
    if (panel_width, panel_height) != source.size:
        source = source.resize((panel_width, panel_height), Image.Resampling.LANCZOS)
        tinted = tinted.resize((panel_width, panel_height), Image.Resampling.LANCZOS)
    preview = Image.new(
        "RGB", (2 * panel_width + _PANEL_GAP_PX, panel_height), (235, 235, 235)
    )
    preview.paste(source, (0, 0))
    right_x = panel_width + _PANEL_GAP_PX
    preview.paste(tinted, (right_x, 0))
    draw = ImageDraw.Draw(preview)
    for candidate in candidates:
        x, y = candidate["seed_pixel"]
        # Seed pixels are original-image centres; this map identifies the same
        # location on the resized right panel and remains inside its source mask.
        draw.text(
            (right_x + (x + 0.5) * panel_width / width, (y + 0.5) * panel_height / height),
            candidate["id"],
            fill=(20, 20, 20),
            anchor="mm",
            stroke_width=2,
            stroke_fill=(255, 255, 255),
        )

    result: dict[str, Any] = {
        "candidates": candidates,
        "background_rgb": list(background),
        "tolerance": tolerance_value,
        "min_pixels": minimum,
        "max_regions": maximum,
        "include_border": include_border,
        "region_exclusions": {
            "below_min_pixels": {
                "count": too_small,
                "reason": "pixel_count is below min_pixels",
            },
            "touching_image_border": {
                "count": border_excluded,
                "reason": (
                    "touches the image border and include_border is false; this is "
                    "excluded as a possible external image background, not asserted "
                    "to be exterior space"
                ),
            },
            "truncated_by_max_regions": {
                "count": truncated,
                "reason": "qualifying candidates after stable area ordering exceed max_regions",
            },
        },
        "coordinate_map": {
            "source_coordinate_convention": "pixel centres are integer (x, y)",
            "bbox_convention": "[x_min, y_min, x_max_exclusive, y_max_exclusive]",
            "source_size_px": [width, height],
            "preview_size_px": [preview.width, preview.height],
            "left_clean_panel": {"offset_px": [0, 0], "size_px": [panel_width, panel_height]},
            "right_numbered_panel": {
                "offset_px": [right_x, 0],
                "size_px": [panel_width, panel_height],
            },
            "source_to_panel": {
                "formula": "panel_xy = (source_xy + 0.5) * scale_xy + panel_offset_xy",
                "scale_xy": [panel_width / width, panel_height / height],
            },
        },
        "warnings": [
            "Candidates are colour-connected raster regions, not asserted rooms, walls, openings, or exterior space.",
            "Use a selected seed with render_pixel_region for a separate contour observation.",
        ],
    }
    return preview, result


__all__ = ["render_pixel_region_overview"]
