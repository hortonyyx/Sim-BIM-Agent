"""Batch overview crops for the plan-reader role.

The public tool is intentionally argument-free.  Its reader task already fixes
one admitted image, so callers should not spend a model turn choosing crop
coordinates or repeat the image name.
"""

from __future__ import annotations

from collections.abc import Mapping
import base64
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image


PLAN_VIEWS_TOOL = {
    "name": "view_plan_blocks",
    "description": (
        "Show the current plan once as 4, 6, or 8 overlapping blocks at about 3x, "
        "with original-pixel coordinate grids. No image name or box is needed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

DISPLAY_SCALE = 3.0
OVERLAP_FRACTION = 0.04
MAX_RETURNED_SIDE = 4096
# Keep the established 4/6/8 layout near the benchmark's roughly 500-source-
# pixel blocks.  Rendering is role-local now, so this chooses readable blocks
# rather than inheriting the shared view tool's 1600-pixel output limit.
PREFERRED_SOURCE_SIDE = 1600 / 2.9

_GRID_OPTIONS = {
    4: ((2, 2), (4, 1), (1, 4)),
    6: ((3, 2), (2, 3), (6, 1), (1, 6)),
    8: ((4, 2), (2, 4), (8, 1), (1, 8)),
}


def initial_coordinate_view(raw_image, *, image_name, directory):
    """One full-frame original-pixel grid in the first request, with saved lineage."""
    from scripts.tool_scripts.run_bim_agent import coordinate_grid_view
    with Image.open(io.BytesIO(raw_image)) as original:
        original = original.convert("RGB")
    picture, grid = coordinate_grid_view(original.copy(), [0, 0, *original.size])
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    png = buffer.getvalue()
    metadata = {"name": image_name, "image_sha256": hashlib.sha256(raw_image).hexdigest(),
                "original_size": list(original.size), "coordinate_grid": grid,
                "box_original_pixels": [0, 0, *original.size], "returned_size": list(picture.size),
                "original_pixels_per_returned_pixel": [1, 1],
                "coordinate_note": "Read the printed ORIGINAL pixel grid. Display/image-token resizing never changes these labels.",
                "render_profile": "reader_initial_coordinate_view_v1",
                "returned_png_sha256": hashlib.sha256(png).hexdigest()}
    saved = _save_view_record(Path(directory), metadata, png)
    return png, saved


def plan_block_boxes(width: int, height: int) -> list[dict[str, Any]]:
    """Return a complete, slightly overlapping 4/6/8-block partition."""

    if (not isinstance(width, int) or isinstance(width, bool) or width <= 0
            or not isinstance(height, int) or isinstance(height, bool) or height <= 0):
        raise ValueError("plan image dimensions must be positive integers")
    if width * height < 4:
        raise ValueError("plan image needs at least four pixels for four non-empty blocks")

    selected: tuple[int, int, list[dict[str, Any]]] | None = None
    for count in (4, 6, 8):
        layouts = [
            _boxes_for_grid(width, height, columns, rows)
            for columns, rows in _GRID_OPTIONS[count]
            if columns <= width and rows <= height
        ]
        if not layouts:
            continue
        boxes = min(layouts, key=_layout_score)
        selected = (boxes[0]["grid"]["columns"], boxes[0]["grid"]["rows"], boxes)
        if _largest_source_side(boxes) <= PREFERRED_SOURCE_SIDE:
            break
    if selected is None:
        raise ValueError("plan image cannot be divided into four non-empty blocks")
    return selected[2]


async def view_plan_blocks(frozen: Any, image_name: str) -> dict[str, Any]:
    """Render every automatic plan block without changing the shared view tool."""

    run, source_bytes, source_sha256, size = _admitted_image(frozen, image_name)
    boxes = plan_block_boxes(*size)
    previews: list[dict[str, Any]] = []
    image_content: list[dict[str, Any]] = []

    with Image.open(io.BytesIO(source_bytes)) as admitted:
        original = admitted.convert("RGB")
    for block in boxes:
        png, metadata = _render_block(
            original,
            image_name=image_name,
            image_sha256=source_sha256,
            original_size=size,
            block=block,
        )
        metadata = _save_view_record(run, metadata, png)
        previews.append(metadata)
        image_content.append({
            "type": "image",
            "mimeType": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        })

    columns = boxes[0]["grid"]["columns"]
    rows = boxes[0]["grid"]["rows"]
    payload = {
        "status": "completed",
        "image_name": image_name,
        "image_sha256": source_sha256,
        "original_size": list(size),
        "block_count": len(boxes),
        "grid": {"columns": columns, "rows": rows},
        "display_scale_requested": DISPLAY_SCALE,
        "overlap_fraction_of_core": OVERLAP_FRACTION,
        "coverage": {"bbox_original_pixels": [0, 0, *size], "complete": True},
        "views": [_public_view(preview) for preview in previews],
        # FrozenBimTools.image_origins recognizes this established key and maps
        # each returned image hash back to its own view_id and original crop.
        "evidence_previews": previews,
    }
    return {
        "content": [*image_content, {
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }],
        "structuredContent": payload,
        "isError": False,
    }


def _admitted_image(
    frozen: Any, image_name: str
) -> tuple[Path, bytes, str, tuple[int, int]]:
    if not isinstance(image_name, str) or not image_name or Path(image_name).name != image_name:
        raise ValueError("image_name must be one admitted filename")
    run_directory = getattr(frozen, "run_directory", None)
    if run_directory is None:
        raise ValueError("plan block viewing requires the frozen run directory")
    run = Path(run_directory).resolve()
    manifest_path = run / "inputs.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("frozen run inputs.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    admitted = manifest.get("images", {}).get(image_name)
    if not isinstance(admitted, Mapping) or not isinstance(admitted.get("sha256"), str):
        raise ValueError(f"image is not admitted to this reader task: {image_name!r}")
    path = (run / "images" / image_name).resolve()
    if path.parent != (run / "images").resolve() or not path.is_file():
        raise FileNotFoundError(f"admitted plan image is missing: {image_name!r}")
    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != admitted["sha256"]:
        raise ValueError(f"admitted plan image changed: {image_name!r}")
    with Image.open(io.BytesIO(source_bytes)) as source:
        size = source.size
    size = (int(size[0]), int(size[1]))
    declared_size = admitted.get("size")
    if declared_size is not None and declared_size != list(size):
        raise ValueError(f"admitted plan image size changed: {image_name!r}")
    return run, source_bytes, source_sha256, size


def _render_block(
    original: Image.Image,
    *,
    image_name: str,
    image_sha256: str,
    original_size: tuple[int, int],
    block: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    from scripts.tool_scripts.run_bim_agent import coordinate_grid_view

    region = list(block["bbox_original_pixels"])
    picture = original.crop(region)
    crop_size = picture.size
    scale = _bounded_scale(crop_size)
    returned_size = tuple(max(1, round(length * scale)) for length in crop_size)
    picture = picture.resize(returned_size, Image.Resampling.NEAREST)
    picture, grid = coordinate_grid_view(picture, region)
    encoded = io.BytesIO()
    picture.save(encoded, "PNG")
    png = encoded.getvalue()
    actual_scale = [picture.width / crop_size[0], picture.height / crop_size[1]]
    metadata = {
        "name": image_name,
        "image_sha256": image_sha256,
        "original_size": list(original_size),
        "coordinate_grid": grid,
        "box_original_pixels": region,
        "returned_size": list(picture.size),
        "display_scale_requested": DISPLAY_SCALE,
        "display_scale_used": scale,
        "display_scale_actual": actual_scale,
        "original_pixels_per_returned_pixel": [
            (region[2] - region[0]) / picture.width,
            (region[3] - region[1]) / picture.height,
        ],
        "coordinate_note": (
            "Grid labels show ORIGINAL pixels. Original pixel = crop origin + returned pixel * "
            "original_pixels_per_returned_pixel. Use ORIGINAL pixels for later evidence."
        ),
        "render_profile": "plan_reader_blocks_v1",
        "render_limit_long_side": MAX_RETURNED_SIDE,
        "block_id": block["block_id"],
        "core_bbox_original_pixels": list(block["core_bbox_original_pixels"]),
        "returned_png_sha256": hashlib.sha256(png).hexdigest(),
    }
    if scale < DISPLAY_SCALE:
        metadata["magnification_note"] = (
            f"Requested {DISPLAY_SCALE:g}x; the plan-reader {MAX_RETURNED_SIDE}px safety limit "
            f"reduced this extreme block to {min(actual_scale):.2f}x."
        )
    return png, metadata


def _bounded_scale(crop_size: tuple[int, int]) -> float:
    return min(DISPLAY_SCALE, MAX_RETURNED_SIDE / max(crop_size))


def _save_view_record(
    run: Path, metadata: Mapping[str, Any], png: bytes
) -> dict[str, Any]:
    folder = run / "image_views"
    folder.mkdir(exist_ok=True)
    numbers = [int(path.stem[5:]) for path in folder.glob("view_*.json")
               if path.stem[5:].isdigit()]
    index = max(numbers, default=0) + 1
    while True:
        view_id = f"view_{index:04d}"
        record = {
            **metadata,
            "view_id": view_id,
            "source_reference": {"view_id": view_id},
            "returned_png_sha256": hashlib.sha256(png).hexdigest(),
        }
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        path = folder / f"{view_id}.json"
        try:
            with path.open("xb") as saved:
                saved.write(raw)
            record["view_record_sha256"] = hashlib.sha256(raw).hexdigest()
            return record
        except FileExistsError:
            index += 1


def _boxes_for_grid(width: int, height: int, columns: int, rows: int) -> list[dict[str, Any]]:
    x_cuts = _axis_cuts(width, columns)
    y_cuts = _axis_cuts(height, rows)
    output = []
    for row in range(rows):
        for column in range(columns):
            left, right = x_cuts[column], x_cuts[column + 1]
            top, bottom = y_cuts[row], y_cuts[row + 1]
            overlap_x = max(1, math.ceil((right - left) * OVERLAP_FRACTION))
            overlap_y = max(1, math.ceil((bottom - top) * OVERLAP_FRACTION))
            bbox = [
                max(0, left - overlap_x if column else left),
                max(0, top - overlap_y if row else top),
                min(width, right + overlap_x if column + 1 < columns else right),
                min(height, bottom + overlap_y if row + 1 < rows else bottom),
            ]
            output.append({
                "block_id": f"block_{len(output) + 1:02d}",
                "grid": {"column": column + 1, "row": row + 1,
                         "columns": columns, "rows": rows},
                "core_bbox_original_pixels": [left, top, right, bottom],
                "bbox_original_pixels": bbox,
            })
    return output


def _axis_cuts(length: int, parts: int) -> list[int]:
    return [index * length // parts for index in range(parts)] + [length]


def _largest_source_side(boxes: list[dict[str, Any]]) -> int:
    return max(max(box["bbox_original_pixels"][2] - box["bbox_original_pixels"][0],
                   box["bbox_original_pixels"][3] - box["bbox_original_pixels"][1])
               for box in boxes)


def _layout_score(boxes: list[dict[str, Any]]) -> tuple[float, float]:
    longest = _largest_source_side(boxes)
    shapes = []
    for box in boxes:
        left, top, right, bottom = box["bbox_original_pixels"]
        shapes.append(abs(math.log((right - left) / (bottom - top))))
    return longest, max(shapes)


def _public_view(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {key: preview.get(key) for key in (
        "block_id",
        "view_id",
        "core_bbox_original_pixels",
        "box_original_pixels",
        "returned_size",
        "display_scale_requested",
        "display_scale_actual",
        "original_pixels_per_returned_pixel",
        "coordinate_grid",
        "magnification_note",
    )}
