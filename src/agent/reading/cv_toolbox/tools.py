"""Deterministic CV toolbox operations for clean vector drawings."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.signal import find_peaks, peak_widths

from .recipes import get_recipe

TOOL_VERSION = "1"


def _load_rgb(image: str | Path | Image.Image) -> Image.Image:
    if isinstance(image, Image.Image):
        img = image
    else:
        img = Image.open(image)
    if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def _recipe(recipe_id: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(recipe_id, dict):
        return dict(recipe_id)
    return get_recipe(recipe_id)


def _mask_clean_vector(img: Image.Image, recipe: dict[str, Any]) -> np.ndarray:
    arr = np.asarray(_load_rgb(img), dtype=np.int16)
    mean = arr.mean(axis=2)
    spread = arr.max(axis=2) - arr.min(axis=2)
    return (
        (mean >= recipe["gray_lo"])
        & (mean <= recipe["gray_hi"])
        & (spread <= recipe["rgb_tol"])
    )


def _make_payload(
    tool: str,
    recipe: dict[str, Any],
    params: dict[str, Any],
    results: list[dict[str, Any]],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "tool_version": TOOL_VERSION,
        "recipe_id": recipe["recipe_id"],
        "params": params,
        "results": results,
        "diagnostics": diagnostics or {},
        "applicability": recipe["applicability"],
    }


def _base_result(
    *,
    candidate_id: str,
    candidate_kind: str,
    anchor_px: dict[str, Any],
    tool: str,
    recipe: dict[str, Any],
    visual_confidence: str = "medium",
    metric_confidence: str = "medium",
    metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metric_payload = {
        "dimension_refs": [],
        "raw_segments": [],
        "scale_px_per_m": None,
        "residuals": [],
        "confidence": metric_confidence,
    }
    if metric:
        metric_payload.update(metric)
    return {
        "candidate_id": candidate_id,
        "candidate_kind": candidate_kind,
        "coord_space": "source_px",
        "anchor_px": anchor_px,
        "visual": {
            "anchor_px": anchor_px,
            "relative": {},
            "confidence": visual_confidence,
        },
        "metric": metric_payload,
        "provenance": {
            "tool": tool,
            "tool_version": TOOL_VERSION,
            "recipe_id": recipe["recipe_id"],
            "source_image_sha256": "",
            "crop_chain_id": "",
        },
    }


def _crop_step(
    source_size: tuple[int, int],
    bbox_px: tuple[float, float, float, float],
    scale: float,
    crop_size: tuple[int, int],
) -> dict[str, Any]:
    x0, y0, x1, y1 = bbox_px
    return {
        "op": "crop_zoom",
        "bbox_px": [x0, y0, x1, y1],
        "scale": scale,
        "source_size_px": list(source_size),
        "crop_size_px": list(crop_size),
        "local_to_source": {
            "source_x": f"{x0} + local_x / {scale}",
            "source_y": f"{y0} + local_y / {scale}",
        },
        "source_to_local": {
            "local_x": f"(source_x - {x0}) * {scale}",
            "local_y": f"(source_y - {y0}) * {scale}",
        },
    }


def _prepare_image(
    image: str | Path | Image.Image,
    bbox_px: Iterable[float] | None = None,
    scale: float = 1.0,
) -> tuple[Image.Image, list[dict[str, Any]], tuple[float, float, float]]:
    if scale < 1:
        raise ValueError("scale must be >= 1")
    src = _load_rgb(image)
    if bbox_px is None:
        return src, [], (0.0, 0.0, 1.0)

    x0, y0, x1, y1 = [float(v) for v in bbox_px]
    x0i, y0i, x1i, y1i = math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)
    if not (0 <= x0i < x1i <= src.width and 0 <= y0i < y1i <= src.height):
        raise ValueError("bbox_px must be half-open and inside the source image")
    crop = src.crop((x0i, y0i, x1i, y1i))
    if scale != 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.NEAREST)
    step = _crop_step(src.size, (float(x0i), float(y0i), float(x1i), float(y1i)), scale, crop.size)
    return crop, [step], (float(x0i), float(y0i), scale)


def _source_coord(local: float, offset: float, scale: float) -> float:
    return offset + local / scale


def _source_bbox(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float, float],
) -> list[float]:
    ox, oy, scale = origin
    x0, y0, x1, y1 = bbox
    return [
        _source_coord(x0, ox, scale),
        _source_coord(y0, oy, scale),
        _source_coord(x1, ox, scale),
        _source_coord(y1, oy, scale),
    ]


def crop_zoom(
    image: str | Path | Image.Image,
    bbox_px: Iterable[float],
    *,
    scale: float = 1.0,
    output_path: str | Path | None = None,
    recipe_id: str = "clean_vector_v1",
    source_name: str = "image",
) -> tuple[dict[str, Any], Image.Image, list[dict[str, Any]]]:
    recipe = get_recipe(recipe_id)
    crop, crop_chain, _origin = _prepare_image(image, bbox_px, scale)
    if output_path is not None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        crop.save(output_path)
    bbox = [float(v) for v in bbox_px]
    result = _base_result(
        candidate_id=f"{Path(source_name).stem}:crop_zoom:001:crop",
        candidate_kind="crop",
        anchor_px={"kind": "bbox", "value": {"bbox_px": bbox}},
        tool="crop_zoom",
        recipe=recipe,
        visual_confidence="high",
        metric_confidence="high",
    )
    result.update(
        {
            "bbox_px": bbox,
            "crop_size_px": list(crop.size),
            "output_image": str(output_path) if output_path is not None else None,
            "geometry": {"kind": "bbox", "bbox_px": bbox},
        }
    )
    payload = _make_payload(
        "crop_zoom",
        recipe,
        {"bbox_px": bbox, "scale": scale, "output_path": str(output_path) if output_path else None},
        [result],
        {"crop_chain": crop_chain},
    )
    return payload, crop, crop_chain


def _line_results(
    *,
    image: Image.Image,
    recipe: dict[str, Any],
    axis: str,
    tool: str,
    candidate_kind: str,
    source_name: str,
    origin: tuple[float, float, float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if axis not in {"row", "col"}:
        raise ValueError("axis must be 'row' or 'col'")
    mask = _mask_clean_vector(image, recipe)
    orthogonal_len = image.width if axis == "row" else image.height
    projection = mask.sum(axis=1 if axis == "row" else 0).astype(float) / float(orthogonal_len)
    peaks, props = find_peaks(
        projection,
        prominence=float(recipe["prominence"]),
        distance=int(recipe.get("min_peak_distance_px", 1)),
    )
    widths = peak_widths(projection, peaks, rel_height=0.5) if len(peaks) else ([], [], [], [])
    prominences = props.get("prominences", np.zeros(len(peaks), dtype=float))
    ox, oy, scale = origin
    offset = oy if axis == "row" else ox

    results: list[dict[str, Any]] = []
    for i, peak_idx in enumerate(peaks, start=1):
        left = float(widths[2][i - 1])
        right = float(widths[3][i - 1])
        lo = max(0, int(math.floor(left)))
        hi = min(len(projection) - 1, int(math.ceil(right)))
        xs = np.arange(lo, hi + 1, dtype=float)
        weights = projection[lo : hi + 1]
        centroid = float(np.average(xs, weights=weights)) if float(weights.sum()) > 0 else float(peak_idx)
        source_pos = _source_coord(centroid, offset, scale)
        source_peak = _source_coord(float(peak_idx), offset, scale)
        source_width = float(widths[0][i - 1]) / scale
        support_source = [_source_coord(left, offset, scale), _source_coord(right, offset, scale)]
        anchor_value = {"axis": axis, "position_px": source_pos}
        if axis == "row":
            geometry = {"kind": "line", "axis": "row", "y_px": source_pos}
        else:
            geometry = {"kind": "line", "axis": "col", "x_px": source_pos}
        result = _base_result(
            candidate_id=f"{Path(source_name).stem}:{tool}:{i:03d}:{axis}{round(source_pos)}",
            candidate_kind=candidate_kind,
            anchor_px={"kind": "line", "value": anchor_value},
            tool=tool,
            recipe=recipe,
            visual_confidence="high" if float(prominences[i - 1]) >= recipe["prominence"] * 2 else "medium",
            metric_confidence="high",
            metric={"raw_segments": [{"axis": axis, "position_px": source_pos, "width_px": source_width}]},
        )
        result.update(
            {
                "axis": axis,
                "position_px": source_pos,
                "local_position_px": centroid,
                "peak_index_px": source_peak,
                "local_peak_index_px": int(peak_idx),
                "strength": float(prominences[i - 1]),
                "width_px": source_width,
                "local_width_px": float(widths[0][i - 1]),
                "fwhm_support_px": support_source,
                "local_fwhm_support_px": [left, right],
                "geometry": geometry,
            }
        )
        results.append(result)

    diagnostics = {
        "image_size_px": list(image.size),
        "mask_pixels": int(mask.sum()),
        "projection_axis": axis,
        "projection_length": int(len(projection)),
        "peak_count": len(results),
        "prominence_threshold": float(recipe["prominence"]),
    }
    return results, diagnostics


def wall_line_profiler(
    image: str | Path | Image.Image,
    *,
    axis: str,
    recipe_id: str = "clean_vector_v1",
    bbox_px: Iterable[float] | None = None,
    scale: float = 1.0,
    source_name: str = "image",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recipe = get_recipe(recipe_id)
    prepared, crop_chain, origin = _prepare_image(image, bbox_px, scale)
    results, diagnostics = _line_results(
        image=prepared,
        recipe=recipe,
        axis=axis,
        tool="wall_line_profiler",
        candidate_kind="wall_line",
        source_name=source_name,
        origin=origin,
    )
    params = {"axis": axis, "bbox_px": list(bbox_px) if bbox_px is not None else None, "scale": scale}
    diagnostics["crop_chain"] = crop_chain
    return _make_payload("wall_line_profiler", recipe, params, results, diagnostics), crop_chain


def storey_line_profiler(
    image: str | Path | Image.Image,
    *,
    recipe_id: str = "clean_vector_v1",
    bbox_px: Iterable[float] | None = None,
    scale: float = 1.0,
    source_name: str = "image",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recipe = get_recipe(recipe_id)
    prepared, crop_chain, origin = _prepare_image(image, bbox_px, scale)
    results, diagnostics = _line_results(
        image=prepared,
        recipe=recipe,
        axis="row",
        tool="storey_line_profiler",
        candidate_kind="storey_line",
        source_name=source_name,
        origin=origin,
    )
    params = {"axis": "row", "bbox_px": list(bbox_px) if bbox_px is not None else None, "scale": scale}
    diagnostics["crop_chain"] = crop_chain
    return _make_payload("storey_line_profiler", recipe, params, results, diagnostics), crop_chain


def _span_px(anchor: dict[str, Any]) -> float:
    axis = anchor["axis"]
    if axis == "xy":
        a = anchor["px_a"]
        b = anchor["px_b"]
        return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    return abs(float(anchor["px_b"]) - float(anchor["px_a"]))


def px_m_calibrator(
    anchors: list[dict[str, Any]],
    *,
    recipe_id: str = "clean_vector_v1",
    residual_warn_px: float | None = None,
    residual_warn_m: float | None = None,
    source_name: str = "image",
) -> dict[str, Any]:
    recipe = get_recipe(recipe_id)
    if not anchors:
        raise ValueError("anchors must not be empty")
    spans = np.array([_span_px(anchor) for anchor in anchors], dtype=float)
    values = np.array([float(anchor["value_m"]) for anchor in anchors], dtype=float)
    if np.any(values <= 0):
        raise ValueError("anchor value_m must be positive")
    if np.any(spans <= 0):
        raise ValueError("anchor pixel span must be positive")

    axis_px_per_m = {}
    for axis in ("x", "y"):
        axis_indices = [index for index, anchor in enumerate(anchors) if anchor["axis"] == axis]
        if axis_indices:
            axis_values = values[axis_indices]
            axis_spans = spans[axis_indices]
            axis_px_per_m[axis] = float(
                np.sum(axis_values * axis_spans) / np.sum(axis_values**2)
            )
    axis_relative_deviation = None
    max_axis_relative_deviation = float(
        recipe["calibration_max_axis_relative_deviation"]
    )
    # C1 (2026-08-17): a legal exit instead of a dead end. This used to `raise`
    # here (2026-07-31, 421c9d3), before blending — the 07-07/07-08 good-window
    # tool never had this check and always returned a usable px_per_m. A hard
    # raise with no escape has an observed real-world consequence (F-34): the
    # reader abandons pixel calibration entirely and falls back to eyeballing
    # meters, which is strictly worse than a flagged, low-confidence number.
    # The disagreement is real signal, so it is surfaced loudly — a boolean
    # flag, a `warnings` entry with explicit next-step guidance, and a forced
    # `metric_confidence="low"` — rather than hidden by a silent average, but
    # the caller always gets a number back. Same-family judgment as F-51:
    # once the staged image and the reader's frame agree (see
    # src.agent.execution.vision_resize), a uniform-scale reader error no
    # longer masquerades as a real cross-axis disagreement here, so this gate
    # should fire less often AND more truthfully than before.
    warnings: list[dict[str, Any]] = []
    axis_calibration_disagreement = False
    if set(axis_px_per_m) == {"x", "y"}:
        x_scale = axis_px_per_m["x"]
        y_scale = axis_px_per_m["y"]
        axis_relative_deviation = abs(x_scale - y_scale) / ((x_scale + y_scale) / 2.0)
        if axis_relative_deviation > max_axis_relative_deviation:
            axis_calibration_disagreement = True
            warnings.append(
                {
                    "type": "cross_axis_disagreement",
                    "x_px_per_m": x_scale,
                    "y_px_per_m": y_scale,
                    "relative_deviation": axis_relative_deviation,
                    "relative_deviation_limit": max_axis_relative_deviation,
                    "guidance": (
                        f"x={x_scale:.9g} px/m and y={y_scale:.9g} px/m disagree by "
                        f"{axis_relative_deviation:.6%}, over the "
                        f"{max_axis_relative_deviation:.6%} limit — at least one endpoint "
                        "pair or transcribed dimension is likely wrong. Do not silently "
                        "trust the blended px_per_m below: re-crop and re-measure both "
                        "axes' anchors at their dimension-chain extension-line "
                        "intersections, then recalibrate. If you must proceed now, prefer "
                        "the single-axis scale (axis_px_per_m) for geometry measured along "
                        "that axis over the blended value, and record this disagreement "
                        "instead of treating the reading as clean."
                    ),
                }
            )
    px_per_m = float(np.sum(values * spans) / np.sum(values**2))
    m_per_px = 1.0 / px_per_m
    residuals: list[dict[str, Any]] | None
    rmse_px: float | None
    rmse_m: float | None
    if len(anchors) == 1:
        residuals = None
        rmse_px = None
        rmse_m = None
    else:
        residuals = []
        for anchor, span, value in zip(anchors, spans, values, strict=True):
            residual_px = float(span - px_per_m * value)
            residual_m = float(residual_px / px_per_m)
            residuals.append(
                {
                    "axis": anchor["axis"],
                    "dimension_ref": anchor.get("dimension_ref"),
                    "span_px": float(span),
                    "value_m": float(value),
                    "residual_px": residual_px,
                    "residual_m": residual_m,
                }
            )
        rmse_px = float(math.sqrt(np.mean([r["residual_px"] ** 2 for r in residuals])))
        rmse_m = float(math.sqrt(np.mean([r["residual_m"] ** 2 for r in residuals])))

    warn_px = float(residual_warn_px if residual_warn_px is not None else recipe["calibration_warn_residual_px"])
    warn_m = float(residual_warn_m if residual_warn_m is not None else recipe["calibration_warn_residual_m"])
    # NOTE: `warnings` was seeded above (possibly with a cross_axis_disagreement
    # entry) — appended to here, never reset, so neither warning source can
    # clobber the other.
    if residuals is not None:
        for residual in residuals:
            if abs(residual["residual_px"]) > warn_px or abs(residual["residual_m"]) > warn_m:
                warnings.append({"type": "residual_exceeds_threshold", **residual})

    raw_segments = [
        {
            "axis": anchor["axis"],
            "px_a": anchor["px_a"],
            "px_b": anchor["px_b"],
            "value_m": anchor["value_m"],
            "dimension_ref": anchor.get("dimension_ref"),
        }
        for anchor in anchors
    ]
    dimension_refs = [a["dimension_ref"] for a in anchors if a.get("dimension_ref")]
    if axis_calibration_disagreement:
        metric_confidence = "low"
    elif warnings:
        metric_confidence = "medium"
    else:
        metric_confidence = "high"
    result = _base_result(
        candidate_id=f"{Path(source_name).stem}:px_m_calibrator:001:scale",
        candidate_kind="calibration",
        anchor_px={"kind": "span", "value": {"anchors": raw_segments}},
        tool="px_m_calibrator",
        recipe=recipe,
        visual_confidence="high",
        metric_confidence=metric_confidence,
        metric={
            "dimension_refs": dimension_refs,
            "raw_segments": raw_segments,
            "scale_px_per_m": px_per_m,
            "axis_px_per_m": axis_px_per_m,
            "axis_relative_deviation": axis_relative_deviation,
            "axis_relative_deviation_limit": max_axis_relative_deviation,
            "axis_calibration_disagreement": axis_calibration_disagreement,
            "residuals": residuals,
        },
    )
    result.update(
        {
            "px_per_m": px_per_m,
            "m_per_px": m_per_px,
            "rmse_px": rmse_px,
            "rmse_m": rmse_m,
            "residuals": residuals,
            "warnings": warnings,
            "axis_px_per_m": axis_px_per_m,
            "axis_relative_deviation": axis_relative_deviation,
            "axis_relative_deviation_limit": max_axis_relative_deviation,
            "axis_calibration_disagreement": axis_calibration_disagreement,
        }
    )
    return _make_payload(
        "px_m_calibrator",
        recipe,
        {
            "anchors": anchors,
            "residual_warn_px": warn_px,
            "residual_warn_m": warn_m,
            "max_axis_relative_deviation": max_axis_relative_deviation,
        },
        [result],
        {"anchor_count": len(anchors), "warnings": warnings},
    )


def _bbox_iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax0, ay0, ax1, ay1 = a["bbox"]
    bx0, by0, bx1, by1 = b["bbox"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _gap_overlap_merge_reason(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    merge_gap_px: float,
    merge_overlap_ratio: float,
    merge_iou: float,
) -> str | None:
    if _bbox_iou(a, b) >= merge_iou:
        return "iou"
    ax0, ay0, ax1, ay1 = a["bbox"]
    bx0, by0, bx1, by1 = b["bbox"]
    x_gap = max(0.0, max(bx0 - ax1, ax0 - bx1))
    y_gap = max(0.0, max(by0 - ay1, ay0 - by1))
    x_overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    y_overlap = max(0.0, min(ay1, by1) - max(ay0, by0))
    min_w = max(1e-9, min(ax1 - ax0, bx1 - bx0))
    min_h = max(1e-9, min(ay1 - ay0, by1 - by0))
    if x_gap <= merge_gap_px and y_overlap / min_h >= merge_overlap_ratio:
        return "gap_x_overlap_y"
    if y_gap <= merge_gap_px and x_overlap / min_w >= merge_overlap_ratio:
        return "gap_y_overlap_x"
    return None


def _merge_components(
    boxes: list[dict[str, Any]],
    *,
    merge_gap_px: float,
    merge_overlap_ratio: float,
    merge_iou: float,
) -> list[dict[str, Any]]:
    current = sorted(boxes, key=lambda box: (box["bbox"][1], box["bbox"][0], box["bbox"][3], box["bbox"][2]))
    changed = True
    while changed:
        changed = False
        merged: list[dict[str, Any]] = []
        used = [False] * len(current)
        for i, box in enumerate(current):
            if used[i]:
                continue
            acc = dict(box)
            for j in range(i + 1, len(current)):
                if used[j]:
                    continue
                reason = _gap_overlap_merge_reason(
                    acc,
                    current[j],
                    merge_gap_px=merge_gap_px,
                    merge_overlap_ratio=merge_overlap_ratio,
                    merge_iou=merge_iou,
                )
                if reason is None:
                    continue
                ax0, ay0, ax1, ay1 = acc["bbox"]
                bx0, by0, bx1, by1 = current[j]["bbox"]
                total_area = acc["area_px"] + current[j]["area_px"]
                acc = {
                    "bbox": [min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)],
                    "source_component_ids": acc["source_component_ids"] + current[j]["source_component_ids"],
                    "area_px": total_area,
                    "centroid": [
                        (acc["centroid"][0] * acc["area_px"] + current[j]["centroid"][0] * current[j]["area_px"]) / total_area,
                        (acc["centroid"][1] * acc["area_px"] + current[j]["centroid"][1] * current[j]["area_px"]) / total_area,
                    ],
                    "merge_reason": reason,
                }
                used[j] = True
                changed = True
            used[i] = True
            merged.append(acc)
        current = sorted(merged, key=lambda box: (box["bbox"][1], box["bbox"][0], box["bbox"][3], box["bbox"][2]))
    return current


def window_cc_detector(
    image: str | Path | Image.Image,
    *,
    recipe_id: str = "clean_vector_v1",
    min_area_px: int | None = None,
    bbox_px: Iterable[float] | None = None,
    scale: float = 1.0,
    min_width_px: float | None = None,
    min_height_px: float | None = None,
    max_width_px: float | None = None,
    max_height_px: float | None = None,
    min_aspect: float | None = None,
    max_aspect: float | None = None,
    merge_gap_px: float | None = None,
    merge_overlap_ratio: float | None = None,
    merge_iou: float | None = None,
    source_name: str = "image",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recipe = get_recipe(recipe_id)
    prepared, crop_chain, origin = _prepare_image(image, bbox_px, scale)
    min_area = int(min_area_px if min_area_px is not None else recipe["min_cc_area_px"])
    mask = _mask_clean_vector(prepared, recipe)
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    raw_boxes: list[dict[str, Any]] = []
    for comp_id in range(1, count + 1):
        ys, xs = np.nonzero(labels == comp_id)
        area = int(len(xs))
        if area < min_area:
            continue
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        width, height = x1 - x0, y1 - y0
        aspect = width / height if height else float("inf")
        if min_width_px is not None and width < min_width_px:
            continue
        if min_height_px is not None and height < min_height_px:
            continue
        if max_width_px is not None and width > max_width_px:
            continue
        if max_height_px is not None and height > max_height_px:
            continue
        if min_aspect is not None and aspect < min_aspect:
            continue
        if max_aspect is not None and aspect > max_aspect:
            continue
        raw_boxes.append(
            {
                "bbox": [float(x0), float(y0), float(x1), float(y1)],
                "source_component_ids": [comp_id],
                "area_px": area,
                "centroid": [float(xs.mean()), float(ys.mean())],
                "merge_reason": "single",
            }
        )

    merged = _merge_components(
        raw_boxes,
        merge_gap_px=float(merge_gap_px if merge_gap_px is not None else recipe["merge_gap_px"]),
        merge_overlap_ratio=float(merge_overlap_ratio if merge_overlap_ratio is not None else recipe["merge_overlap_ratio"]),
        merge_iou=float(merge_iou if merge_iou is not None else recipe["merge_iou"]),
    )
    results: list[dict[str, Any]] = []
    for idx, box in enumerate(merged, start=1):
        source_bbox = _source_bbox(tuple(box["bbox"]), origin)
        source_centroid = [
            _source_coord(box["centroid"][0], origin[0], origin[2]),
            _source_coord(box["centroid"][1], origin[1], origin[2]),
        ]
        result = _base_result(
            candidate_id=f"{Path(source_name).stem}:window_cc_detector:{idx:03d}:bbox",
            candidate_kind="window_rect",
            anchor_px={"kind": "bbox", "value": {"bbox_px": source_bbox}},
            tool="window_cc_detector",
            recipe=recipe,
            visual_confidence="medium",
            metric_confidence="medium",
            metric={"raw_segments": [{"bbox_px": source_bbox}]},
        )
        result.update(
            {
                "bbox_px": source_bbox,
                "local_bbox_px": box["bbox"],
                "source_component_ids": box["source_component_ids"],
                "area_px": int(box["area_px"]),
                "centroid_px": source_centroid,
                "local_centroid_px": box["centroid"],
                "merge_reason": box["merge_reason"],
                "geometry": {"kind": "bbox", "bbox_px": source_bbox},
            }
        )
        results.append(result)
    params = {
        "bbox_px": list(bbox_px) if bbox_px is not None else None,
        "scale": scale,
        "min_area_px": min_area,
        "merge_gap_px": float(merge_gap_px if merge_gap_px is not None else recipe["merge_gap_px"]),
        "merge_overlap_ratio": float(merge_overlap_ratio if merge_overlap_ratio is not None else recipe["merge_overlap_ratio"]),
        "merge_iou": float(merge_iou if merge_iou is not None else recipe["merge_iou"]),
    }
    diagnostics = {
        "image_size_px": list(prepared.size),
        "mask_pixels": int(mask.sum()),
        "raw_component_count": int(count),
        "filtered_component_count": len(raw_boxes),
        "merged_count": len(results),
        "crop_chain": crop_chain,
    }
    return _make_payload("window_cc_detector", recipe, params, results, diagnostics), crop_chain


def _candidate_geometry(candidate: dict[str, Any]) -> dict[str, Any] | None:
    if "geometry" in candidate:
        return candidate["geometry"]
    anchor = candidate.get("anchor_px") or {}
    value = anchor.get("value", {})
    if anchor.get("kind") == "bbox" and "bbox_px" in value:
        return {"kind": "bbox", "bbox_px": value["bbox_px"]}
    if anchor.get("kind") == "line":
        return {"kind": "line", **value}
    if anchor.get("kind") == "point" and "point_px" in value:
        return {"kind": "point", "point_px": value["point_px"]}
    return None


def overlay_logger(
    image: str | Path | Image.Image,
    candidates: list[dict[str, Any]],
    *,
    output_path: str | Path,
    recipe_id: str = "clean_vector_v1",
    source_name: str = "image",
) -> dict[str, Any]:
    recipe = get_recipe(recipe_id)
    # F-58: validate the candidates SHAPE before iterating. A caller who wraps
    # the candidate list in an envelope object (e.g. {"results": [...]}
    # instead of the documented bare [...]) used to iterate the dict's keys
    # (plain strings), and the first `.get()` call on a string then raised a
    # bare AttributeError ("'str' object has no attribute 'get'") pointing at
    # this function's internals rather than at the caller's mistake. A reading
    # agent that mis-learns the shape from the docs gets a stack trace instead
    # of guidance. These two checks turn both failure shapes -- the whole
    # argument being the wrong type, and an individual element inside an
    # otherwise-correct list being the wrong type -- into one clean,
    # actionable ValueError naming what was actually received.
    if not isinstance(candidates, list):
        raise ValueError(
            "overlay_logger candidates must be a JSON array of candidate "
            'objects, e.g. [{"candidate_id":"c1","status":"accepted",'
            '"reason":"...","geometry":{...}}], not a '
            f"{type(candidates).__name__}"
        )
    img = _load_rgb(image)
    draw = ImageDraw.Draw(img)
    colors = {"accepted": "lime", "rejected": "red", "undecided": "orange"}
    decisions = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError(
                "each overlay candidate must be a JSON object with "
                '"candidate_id"/"status"/"reason"/"geometry" keys, not a '
                f"{type(candidate).__name__}: {candidate!r}"
            )
        status = candidate.get("status", "undecided")
        if status not in colors:
            raise ValueError("candidate status must be accepted, rejected, or undecided")
        if "candidate_id" not in candidate or "reason" not in candidate:
            raise ValueError("overlay candidates must include candidate_id and reason")
        color = colors[status]
        geometry = _candidate_geometry(candidate)
        if geometry is None:
            raise ValueError("overlay candidate must carry drawable geometry (geometry or anchor_px)")
        if geometry:
            kind = geometry.get("kind")
            if kind == "bbox":
                x0, y0, x1, y1 = geometry["bbox_px"]
                draw.rectangle((x0, y0, x1, y1), outline=color, width=2)
            elif kind == "line":
                if geometry.get("axis") == "row" or "y_px" in geometry:
                    y = geometry.get("y_px", geometry.get("position_px"))
                    draw.line((0, y, img.width, y), fill=color, width=2)
                else:
                    x = geometry.get("x_px", geometry.get("position_px"))
                    draw.line((x, 0, x, img.height), fill=color, width=2)
            elif kind == "point":
                x, y = geometry["point_px"]
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=color, width=2)
        decisions.append(
            {
                "candidate_id": candidate["candidate_id"],
                "status": status,
                "reason": candidate["reason"],
                "geometry": geometry,
            }
        )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    result = _base_result(
        candidate_id=f"{Path(source_name).stem}:overlay_logger:001:overlay",
        candidate_kind="overlay",
        anchor_px={"kind": "bbox", "value": {"bbox_px": [0, 0, img.width, img.height]}},
        tool="overlay_logger",
        recipe=recipe,
        visual_confidence="high",
        metric_confidence="low",
    )
    result.update({"overlay_path": str(out), "decisions": decisions})
    return _make_payload(
        "overlay_logger",
        recipe,
        {"output_path": str(out), "candidate_count": len(candidates)},
        [result],
        {"decisions": decisions},
    )
