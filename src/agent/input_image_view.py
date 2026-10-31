"""Shared auxiliary PNG rendering for view_image and verified worker prefetch."""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

from PIL import Image


def render_input_view(path, *, name, box=None, coordinate_grid=True, display_scale=1.0):
    """Return the same rendered PNG and coordinate contract as the public tool."""
    from scripts.tool_scripts.run_bim_agent import coordinate_grid_view
    if (not isinstance(display_scale, (int, float)) or isinstance(display_scale, bool)
            or not math.isfinite(display_scale)):
        raise ValueError("display_scale must be a finite number; allowed range is 1 through 8")
    # GLM sm24 requested 0.7x. Preserve intent within the display-only limits.
    requested_scale = display_scale
    display_scale = max(1, min(8, display_scale))
    with Image.open(path) as raw:
        pic = raw.convert("RGB")
        original_size = list(pic.size)
        region = box or [0, 0, pic.width, pic.height]
        if box is not None:
            x0,y0,x1,y1 = box
            if not (0 <= x0 < x1 <= pic.width and 0 <= y0 < y1 <= pic.height):
                raise ValueError("crop outside original image bounds")
            pic = pic.crop(box)
        crop_size = pic.size
        if display_scale == 1:
            # Preserve the existing default presentation, including its
            # downsampling behaviour for large full-image views.
            pic.thumbnail((1600,1600))
        else:
            # Calculate the capped target before resizing so a high requested
            # scale never creates a large temporary bitmap.
            actual_scale = min(float(display_scale), 1600 / max(crop_size))
            target_size = tuple(max(1, round(length * actual_scale)) for length in crop_size)
            pic = pic.resize(target_size, Image.Resampling.NEAREST)
        grid = {"shown": False}
        if coordinate_grid:
            pic, grid = coordinate_grid_view(pic, region)
        data = io.BytesIO(); pic.save(data, "PNG")
    actual_scale = [pic.width / crop_size[0], pic.height / crop_size[1]]
    metadata = {"name": name, "image_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "original_size": original_size, "coordinate_grid": grid,
                "box_original_pixels": region, "returned_size": list(pic.size),
                "display_scale_requested": requested_scale,
                "display_scale_used": display_scale,
                "display_scale_actual": actual_scale,
                "original_pixels_per_returned_pixel": [
                    (region[2] - region[0]) / pic.width,
                    (region[3] - region[1]) / pic.height],
                "coordinate_note": ("Grid labels show ORIGINAL pixels. Original pixel = crop origin + returned "
                                    "pixel * original_pixels_per_returned_pixel. Use ORIGINAL pixels for the next "
                                    "crop or measurement.")}
    if requested_scale != display_scale:
        metadata["display_scale_note"] = f"Requested {requested_scale}x; clamped to allowed [1, 8]: {display_scale}x."
    if display_scale > 1 and min(actual_scale) < display_scale - 0.05:
        # run94 asked 1.5-2x for 1100-1500 px strips and got 1.08-1.43x without noticing.
        metadata["magnification_note"] = (
            f"Returned images are at most 1600 px on their long side, so this {crop_size[0]}x{crop_size[1]} px "
            f"box was enlarged only {min(actual_scale):.2f}x; choose a smaller box to magnify more.")
    return data.getvalue(), metadata
