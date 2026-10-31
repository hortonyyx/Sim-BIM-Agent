"""Conservative ink-line alignment for elevation opening observations.

The reader's legacy ``bbox`` is an evidence crop, not an opening boundary.
This module therefore uses it only as a sampling region for left/right edges.
Opening geometry comes from an explicit ``opening_bbox`` when present, or from
legacy ``x_px`` plus ``sill_m``/``head_m`` when a vertical calibration proves
the pixel-to-height conversion.

The public function returns an alignment envelope and never mutates the input
artifact.  Callers preserve the envelope beside the reader artifact, then apply
``aligned_values`` before Lite-grid adoption. Marked dimensions and mixed
evidence are never replaced by image ink; the later regularization report keeps
these pre-grid readings beside the adopted values.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


INK_ALIGNMENT_SCHEMA_VERSION = "elevation_ink_alignment_v2"
DEFAULT_SEARCH_DISTANCE_M = 0.30
DEFAULT_MIN_SUPPORT_RATIO = 0.55
DEFAULT_MIN_MEAN_INK = 0.30

_EDGES = ("left", "top", "right", "bottom")
_VISIBLE_CONTRAST = 8 / 255


def align_elevation_artifact(
    image: str | Path | Image.Image | np.ndarray,
    artifact: Mapping[str, Any],
    *,
    search_distance_m: float = DEFAULT_SEARCH_DISTANCE_M,
    min_support_ratio: float = DEFAULT_MIN_SUPPORT_RATIO,
    min_mean_ink: float = DEFAULT_MIN_MEAN_INK,
) -> dict[str, Any]:
    """Align approximate opening rectangles to nearby strong facade ink.

    The search radius is ``search_distance_m * pixels_per_metre`` on each
    calibrated axis.  A candidate must cover ``min_support_ratio`` of the
    corresponding opening span and meet ``min_mean_ink``.  Weak or absent
    evidence leaves that edge unchanged and records why.

    Minimal forward contract:

    * ``opening.opening_bbox`` is ``[left, top, right, bottom]`` in original
      image pixels and means the approximate *opening* rectangle.
    * ``artifact.z_calibration`` mirrors ``x_calibration`` with
      ``pixel_start``, ``pixel_end``, ``world_start_m`` and ``world_end_m``.
      Image Y must increase while world Z decreases.

    Legacy artifacts remain useful horizontally through ``x_px``.  Their
    vertical bounds are derived from ``sill_m`` and ``head_m`` only when the
    explicit Z calibration is valid; otherwise top/bottom are not invented.
    """

    if not isinstance(artifact, Mapping):
        raise TypeError("artifact must be a mapping")
    if not math.isfinite(search_distance_m) or search_distance_m <= 0:
        raise ValueError("search_distance_m must be a positive finite number")
    if not 0 < min_support_ratio <= 1:
        raise ValueError("min_support_ratio must be in (0, 1]")
    if not 0 < min_mean_ink <= 1:
        raise ValueError("min_mean_ink must be in (0, 1]")

    ink, background = _background_relative_ink(image)
    height, width = ink.shape
    x_calibration, x_problem = _calibration(
        artifact.get("x_calibration"), require_descending_world=False
    )
    z_calibration, z_problem = _calibration(
        artifact.get("z_calibration"), require_descending_world=True
    )
    x_radius = _search_radius(x_calibration, search_distance_m)
    z_radius = _search_radius(z_calibration, search_distance_m)

    opening_results = []
    for index, original_opening in enumerate(artifact.get("openings", ())):
        if not isinstance(original_opening, Mapping):
            raise TypeError(f"openings[{index}] must be a mapping")
        opening_results.append(
            _align_opening(
                ink,
                original_opening,
                index=index,
                x_calibration=x_calibration,
                z_calibration=z_calibration,
                x_problem=x_problem,
                z_problem=z_problem,
                x_radius=x_radius,
                z_radius=z_radius,
                levels=artifact.get("elevations", ()),
                min_support_ratio=min_support_ratio,
                min_mean_ink=min_mean_ink,
                background=background,
            )
        )

    opening_statuses = Counter(item["status"] for item in opening_results)
    edge_statuses = Counter(
        edge["status"]
        for item in opening_results
        for edge in item["edges"].values()
    )
    return {
        "schema_version": INK_ALIGNMENT_SCHEMA_VERSION,
        "image_size_px": [width, height],
        "search_distance_m": float(search_distance_m),
        "search_radius_px": {"x": x_radius, "z": z_radius},
        "source_image": background,
        "issues": [row["source_image_issue"] for row in opening_results
                   if row["source_image_issue"] is not None],
        "calibration": {
            "x": "available" if x_calibration is not None else x_problem,
            "z": "available" if z_calibration is not None else z_problem,
        },
        "openings": opening_results,
        "summary": {
            "opening_count": len(opening_results),
            "opening_status_counts": dict(sorted(opening_statuses.items())),
            "edge_status_counts": dict(sorted(edge_statuses.items())),
            "candidate_moved_edge_count": edge_statuses["aligned"],
            "moved_edge_count": sum(row["edges"][edge]["status"] == "aligned" and field in row["applied_fields"]
                for row in opening_results for edge, field in (("left", "x_px"), ("right", "x_px"),
                                                               ("top", "head_m"), ("bottom", "sill_m"))),
            "applied_field_count": sum(len(row["applied_fields"]) for row in opening_results),
            "inconsistency_count": sum(len(row["inconsistencies"]) for row in opening_results),
        },
    }


def _align_opening(
    ink: np.ndarray,
    opening: Mapping[str, Any],
    *,
    index: int,
    x_calibration: dict[str, float] | None,
    z_calibration: dict[str, float] | None,
    x_problem: str,
    z_problem: str,
    x_radius: int | None,
    z_radius: int | None,
    levels: Sequence[Mapping[str, Any]],
    min_support_ratio: float,
    min_mean_ink: float,
    background: Mapping[str, Any],
) -> dict[str, Any]:
    opening_id = str(opening.get("id", f"opening-{index + 1}"))
    submitted = {
        field: copy.deepcopy(opening.get(field))
        for field in ("x_px", "width_m", "sill_m", "head_m")
    }
    explicit_box = _box_or_none(opening.get("opening_bbox"))
    x_pair = _pair_or_none(opening.get("x_px"))
    if explicit_box is not None:
        left, top, right, bottom = explicit_box
        geometry_source = "opening_bbox"
    else:
        left, right = x_pair if x_pair is not None else (None, None)
        top = _world_to_pixel(z_calibration, opening.get("head_m"))
        bottom = _world_to_pixel(z_calibration, opening.get("sill_m"))
        geometry_source = "legacy_x_px_and_calibrated_heights"

    original_box: list[float | None] = [left, top, right, bottom]
    if opening.get("evidence_type") == "pixels":
        if submitted["x_px"] is None and left is not None and right is not None:
            submitted["x_px"] = [left, right]
        if submitted["width_m"] is None and x_calibration and left is not None and right is not None:
            submitted["width_m"] = abs((right - left) * x_calibration["world_per_pixel"])
        for field, pixel in (("head_m", top), ("sill_m", bottom)):
            if submitted[field] is None and z_calibration and pixel is not None:
                submitted[field] = _pixel_to_world(z_calibration, pixel)
    aligned_box = list(original_box)
    evidence_box = _box_or_none(opening.get("bbox"))
    sampling_top, sampling_bottom = _sampling_vertical_span(
        top, bottom, evidence_box, ink.shape[0]
    )

    edges: dict[str, dict[str, Any]] = {}
    for edge_name in _EDGES:
        coordinate = original_box[_EDGES.index(edge_name)]
        calibration = x_calibration if edge_name in {"left", "right"} else z_calibration
        calibration_problem = x_problem if edge_name in {"left", "right"} else z_problem
        radius = x_radius if edge_name in {"left", "right"} else z_radius
        if coordinate is None or calibration is None or radius is None:
            edges[edge_name] = _unmoved_edge(
                coordinate, "missing_calibration", calibration_problem
            )
            continue

        if edge_name in {"left", "right"}:
            span = (sampling_top, sampling_bottom)
            axis_length = ink.shape[1]
        else:
            if left is None or right is None:
                edges[edge_name] = _unmoved_edge(
                    coordinate, "missing_geometry", "horizontal opening span is unavailable"
                )
                continue
            span = _trimmed_span(left, right, ink.shape[1])
            axis_length = ink.shape[0]

        candidates = _supported_lines(
            ink,
            coordinate=coordinate,
            radius=radius,
            span=span,
            vertical=edge_name in {"left", "right"},
            min_support_ratio=min_support_ratio,
            min_mean_ink=min_mean_ink,
        )
        visible_support = _visible_span_support(
            ink, coordinate=coordinate, radius=radius, span=span,
            vertical=edge_name in {"left", "right"})
        if len(candidates) != 1:
            edges[edge_name] = _unmoved_edge(
                coordinate, "ambiguous_lines" if candidates else "no_strong_line",
                "multiple supported ink strokes" if candidates else "no supported ink line in the calibrated search range"
            )
            edges[edge_name]["candidates"] = candidates
            edges[edge_name]["max_visible_support_ratio"] = visible_support
            continue
        candidate = candidates[0]
        candidate_px, support_ratio, mean_ink = candidate["pixel"], candidate["support_ratio"], candidate["mean_ink"]
        candidate_px = float(max(0, min(axis_length - 1, candidate_px)))
        # Sub-pixel stroke centroids are not meaningful BIM changes.  Treat a
        # line within one source pixel as confirmation and retain the reader's
        # coordinate, avoiding tiny drift on thick or antialiased ink.
        if math.isclose(candidate_px, coordinate, abs_tol=1.0):
            candidate_px = float(coordinate)
            status = "confirmed"
        else:
            status = "aligned"
        edges[edge_name] = {
            "status": status,
            "original_px": float(coordinate),
            "aligned_px": candidate_px,
            "shift_px": candidate_px - float(coordinate),
            "support_ratio": support_ratio,
            "mean_ink": mean_ink,
            "search_radius_px": radius,
            "candidates": candidates,
            "max_visible_support_ratio": visible_support,
        }
        aligned_box[_EDGES.index(edge_name)] = candidate_px

    _reject_inverted_pair(edges, aligned_box, "left", "right")
    _reject_inverted_pair(edges, aligned_box, "top", "bottom")

    policy = _apply_evidence_policy(opening, submitted, aligned_box, edges,
                                    x_calibration, z_calibration, levels)

    statuses = [edge["status"] for edge in edges.values()]
    moved = statuses.count("aligned")
    supported = moved + statuses.count("confirmed")
    if supported == 4:
        status = "aligned" if moved else "confirmed"
    elif supported:
        status = "partial"
    else:
        status = "unchanged"

    # A bounding box does not assert four straight aperture edges. Weak linear
    # coverage is only a prompt to inspect: curved/diagonal strokes may touch a
    # side at one point. Only complete, untrimmed search bands with no visible
    # source pixels at all support a hard absence finding.
    absent = [name for name, edge in edges.items()
              if edge.get("max_visible_support_ratio") is not None
              and edge["max_visible_support_ratio"] <= 0.10]
    issue = None
    full_band_pixels = (_full_band_visible_pixels(ink, explicit_box, x_radius, z_radius)
                        if explicit_box is not None else None)
    empty_bands = (full_band_pixels is not None
                   and all(count == 0 for count in full_band_pixels.values()))
    if explicit_box is not None and background["confident"] and (empty_bands or len(absent) >= 3):
        issue = {"type": ("opening_geometry_without_image_support" if empty_bands
                          else "opening_geometry_weak_linear_support"), "opening_id": opening_id,
                 "bbox": list(explicit_box), "near_empty_edges": absent,
                 "look_box": [max(0, math.floor(explicit_box[0]) - (x_radius or 0)),
                              max(0, math.floor(explicit_box[1]) - (z_radius or 0)),
                              min(ink.shape[1], math.ceil(explicit_box[2]) + (x_radius or 0) + 1),
                              min(ink.shape[0], math.ceil(explicit_box[3]) + (z_radius or 0) + 1)],
                 "search_radius_px": {"x": x_radius, "z": z_radius},
                 "full_band_visible_pixels": full_band_pixels,
                 "max_visible_support_ratio": {name: edges[name]["max_visible_support_ratio"]
                                               for name in absent},
                 "reason": (
                     "No visible source pixels occur in any complete bounding-box search band, including endpoints. Inspect look_box in the ORIGINAL image; check original-pixel coordinates, image-view scaling and x/z calibration, then prepare a corrected trial. No coordinate compensation or replacement geometry was inferred."
                     if empty_bands else
                     "At least three bounding-box sides have weak linear stroke coverage, but visible source pixels remain in the complete search bands. Curved, diagonal, faint or broken aperture boundaries may explain this; inspect the original overlay. This is uncertain evidence, not a blocking shape judgment, and no geometry was inferred.")}
    return {
        "id": opening_id,
        "status": "aligned" if policy["applied_fields"] else "inconsistent" if policy["inconsistencies"]
                  else "confirmed" if supported == 4 else "partial" if supported else "unchanged",
        "candidate_status": status,
        "geometry_source": geometry_source,
        "original_bbox_px": original_box,
        "candidate_bbox_px": aligned_box,
        "aligned_bbox_px": policy["effective_bbox_px"],
        "original_values": submitted,
        "source_image_issue": issue,
        **policy,
        "edges": edges,
    }


def _apply_evidence_policy(opening, submitted, candidate_box, edges, xcal, zcal, levels):
    """An opening-level basis cannot authorize field-level mixed-source edits."""
    basis = opening.get("evidence_type", "unspecified")
    pure_pixels = basis == "pixels"
    values = copy.deepcopy(submitted)
    ink_values, inconsistencies, applied, decisions = {}, [], [], {}
    tolerances = {axis: max(0.05, 2 * abs(cal["world_per_pixel"])) if cal else None
                  for axis, cal in (("x", xcal), ("z", zcal))}
    unique = {name: edge["status"] in {"aligned", "confirmed"} for name, edge in edges.items()}
    if unique["left"] and unique["right"]:
        ink_values["x_px"] = [candidate_box[0], candidate_box[2]]
        ink_values["width_m"] = abs((candidate_box[2] - candidate_box[0]) * xcal["world_per_pixel"])
    for edge, field, index in (("top", "head_m", 1), ("bottom", "sill_m", 3)):
        if unique[edge]:
            ink_values[field] = _pixel_to_world(zcal, candidate_box[index])

    # Ground is not assumed to be Z=0 and a raised threshold is not flattened.
    # Only an observed same-floor/ground level already touching the submitted
    # sill (at most one pixel and 5 cm) establishes this protection.
    floor_contact = []
    sill = submitted.get("sill_m")
    if opening.get("kind") == "door" and zcal and isinstance(sill, (int, float)):
        contact_tolerance = min(0.05, abs(zcal["world_per_pixel"]))
        for level in levels:
            if (level.get("evidence_type") not in {"annotation", "annotation_and_pixels", "pixels"}
                    or level.get("kind") not in {"ground", "floor"}
                    or (level.get("floor_id") is not None and level["floor_id"] != opening.get("floor_id"))):
                continue
            z = level.get("value_m")
            if isinstance(z, (int, float)) and abs(sill - z) <= contact_tolerance + 1e-9:
                floor_contact.append({"id": level.get("id"), "value_m": z, "bbox": level.get("bbox"),
                                      "evidence_type": level["evidence_type"]})

    required_edges = {"x_px": ("left", "right"), "width_m": ("left", "right"),
                      "head_m": ("top",), "sill_m": ("bottom",)}
    for field, names in required_edges.items():
        original, ink = submitted.get(field), ink_values.get(field)
        axis = "x" if field in {"x_px", "width_m"} else "z"
        reason = "retained_non_pixel_basis" if not pure_pixels else "no_unique_ink"
        if ink is not None and original is not None:
            delta = (max(abs(a - b) * abs(xcal["world_per_pixel"])
                         for a, b in zip(ink, original)) if field == "x_px" else abs(ink - original))
            if delta > tolerances[axis] + 1e-9:
                inconsistencies.append({"field": field, "reader_value": copy.deepcopy(original),
                    "ink_value": copy.deepcopy(ink), "difference_m": delta,
                    "tolerance_m": tolerances[axis], "evidence_type": basis,
                    "reason": "reader_value_disagrees_with_unique_ink"})
            if pure_pixels:
                if field == "sill_m" and floor_contact:
                    reason = "retained_floor_contact"
                elif any(edges[name]["status"] == "aligned" for name in names):
                    values[field] = copy.deepcopy(ink)
                    applied.append(field)
                    reason = "replaced_unique_pixels"
                else:
                    # A line confirming the existing pixel edge does not
                    # justify re-rounding an independently submitted number.
                    reason = "confirmed_no_movement"
        decisions[field] = reason
    # Actual pixel corrections are recorded as corrections, not unresolved
    # disagreements. All retained numeric/ink disagreements remain reviewable.
    inconsistencies = [row for row in inconsistencies if row["field"] not in applied]
    effective = [None, None, None, None]
    if values["x_px"] is not None:
        effective[0], effective[2] = values["x_px"]
    effective[1] = _world_to_pixel(zcal, values["head_m"])
    effective[3] = _world_to_pixel(zcal, values["sill_m"])
    values["world_span_m"] = ([_pixel_to_world(xcal, x) for x in values["x_px"]]
                              if xcal and values["x_px"] is not None else None)
    for name, edge in edges.items():
        edge["effective_px"] = effective[_EDGES.index(name)]
    return {"aligned_values": values, "effective_bbox_px": effective,
            "ink_values": ink_values, "evidence_type": basis,
            "replacement_policy": "only_pixels_with_unique_ink; all_mixed_or_unspecified_values_retained",
            "tolerance_m": tolerances, "floor_contact": floor_contact,
            "field_decisions": decisions, "applied_fields": applied,
            "inconsistencies": inconsistencies}


def _background_relative_ink(image: str | Path | Image.Image | np.ndarray):
    """Contrast against a sampled drawing background, independent of polarity.

    RGB contrast preserves colored CAD strokes whose luminance is low. When
    transparent pixels dominate, alpha itself is the stroke evidence, so white
    strokes on transparency are not erased by a white compositing background.
    A nonuniform background is recorded as uncertain, never negative evidence.
    """
    if isinstance(image, np.ndarray):
        array = np.asarray(image)
        if not (array.ndim == 2 or array.ndim == 3 and array.shape[2] in {3, 4}):
            raise ValueError("image array must be grayscale, RGB, or RGBA")
        pil = Image.fromarray(array.astype(np.uint8, copy=False))
    else:
        if isinstance(image, Image.Image):
            pil = image
        else:
            with Image.open(image) as source:
                pil = source.copy()
    if min(pil.size) < 2:
        raise ValueError("image must contain at least 2 x 2 pixels")
    rgba = np.asarray(pil.convert("RGBA"), dtype=np.float32)
    alpha = rgba[:, :, 3] / 255
    stride = max(1, math.ceil(max(pil.size) / 256))
    transparent_fraction = float(np.mean(alpha[::stride, ::stride] == 0))
    if transparent_fraction >= 0.80:
        return alpha, {"method": "transparent_background_alpha", "confident": True,
                       "background_fraction": transparent_fraction,
                       "visible_contrast_threshold": _VISIBLE_CONTRAST}
    rgb = rgba[:, :, :3] * alpha[:, :, None] + 255 * (1 - alpha[:, :, None])
    sample = rgb[::stride, ::stride]
    background = np.median(sample, axis=(0, 1))
    background_fraction = float(np.mean(np.max(abs(sample - background), axis=2) <= 8))
    ink = np.max(abs(rgb - background), axis=2) / 255
    return ink, {"method": "background_relative_rgb", "background_rgb": background.tolist(),
                 "confident": background_fraction >= 0.80,
                 "background_fraction": background_fraction,
                 "visible_contrast_threshold": _VISIBLE_CONTRAST}


def _calibration(
    value: Any, *, require_descending_world: bool
) -> tuple[dict[str, float] | None, str]:
    if not isinstance(value, Mapping):
        return None, "missing_calibration"
    required = ("pixel_start", "pixel_end", "world_start_m", "world_end_m")
    try:
        numbers = {name: float(value[name]) for name in required}
    except (KeyError, TypeError, ValueError):
        return None, "invalid_calibration"
    if not all(math.isfinite(number) for number in numbers.values()):
        return None, "invalid_calibration"
    pixel_delta = numbers["pixel_end"] - numbers["pixel_start"]
    world_delta = numbers["world_end_m"] - numbers["world_start_m"]
    if pixel_delta == 0 or world_delta == 0:
        return None, "invalid_calibration"
    if require_descending_world and pixel_delta * world_delta >= 0:
        return None, "invalid_vertical_direction"
    numbers["world_per_pixel"] = world_delta / pixel_delta
    numbers["pixels_per_metre"] = abs(pixel_delta / world_delta)
    return numbers, "available"


def _search_radius(calibration: dict[str, float] | None, distance_m: float) -> int | None:
    if calibration is None:
        return None
    return max(2, int(math.ceil(calibration["pixels_per_metre"] * distance_m)))


def _pixel_to_world(calibration: dict[str, float], pixel: float) -> float:
    return calibration["world_start_m"] + (
        float(pixel) - calibration["pixel_start"]
    ) * calibration["world_per_pixel"]


def _world_to_pixel(calibration: dict[str, float] | None, world: Any) -> float | None:
    if calibration is None:
        return None
    try:
        value = float(world)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return calibration["pixel_start"] + (
        value - calibration["world_start_m"]
    ) / calibration["world_per_pixel"]


def _pair_or_none(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        return None
    try:
        first, second = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (first, second)) or first >= second:
        return None
    return first, second


def _box_or_none(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (left, top, right, bottom)):
        return None
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def _sampling_vertical_span(
    top: float | None,
    bottom: float | None,
    evidence_box: tuple[float, float, float, float] | None,
    image_height: int,
) -> tuple[int, int]:
    if top is not None and bottom is not None and top < bottom:
        return _trimmed_span(top, bottom, image_height)
    if evidence_box is not None:
        # Legacy bbox is safe as a trace sampling region, never as geometry.
        return _trimmed_span(evidence_box[1], evidence_box[3], image_height)
    return 0, image_height


def _trimmed_span(start: float, end: float, limit: int) -> tuple[int, int]:
    low, high = sorted((float(start), float(end)))
    trim = min((high - low) * 0.10, 4.0)
    low = max(0, int(math.floor(low + trim)))
    high = min(limit, int(math.ceil(high - trim)) + 1)
    if high - low < 3:
        low = max(0, int(math.floor(min(start, end))))
        high = min(limit, int(math.ceil(max(start, end))) + 1)
    return low, max(low + 1, high)


def _supported_lines(
    ink: np.ndarray,
    *,
    coordinate: float,
    radius: int,
    span: tuple[int, int],
    vertical: bool,
    min_support_ratio: float,
    min_mean_ink: float,
) -> list[dict[str, float]]:
    axis_length = ink.shape[1] if vertical else ink.shape[0]
    lower = max(0, int(math.floor(coordinate)) - radius)
    upper = min(axis_length - 1, int(math.ceil(coordinate)) + radius)
    supported: list[tuple[int, float, float]] = []
    for candidate in range(lower, upper + 1):
        pixels = (
            ink[span[0] : span[1], candidate]
            if vertical
            else ink[candidate, span[0] : span[1]]
        )
        if pixels.size == 0:
            continue
        pixels = np.clip(pixels, 0.0, 1.0)
        support = float(np.mean(pixels >= min_mean_ink))
        mean_ink = float(np.mean(pixels))
        mask = pixels >= min_mean_ink
        changes = np.diff(np.r_[False, mask, False].astype(np.int8))
        longest = max((np.flatnonzero(changes == -1) - np.flatnonzero(changes == 1)), default=0) / len(mask)
        if support >= min_support_ratio and longest >= min_support_ratio and mean_ink >= min_mean_ink:
            supported.append((candidate, support, mean_ink))
    if not supported:
        return []

    runs: list[list[tuple[int, float, float]]] = []
    for item in supported:
        if not runs or item[0] != runs[-1][-1][0] + 1:
            runs.append([item])
        else:
            runs[-1].append(item)
    output = []
    for run in runs:
        # A flat filled region extending beyond the search band is not a line.
        # Check both flanks in the source image, including just outside the band.
        flanks = (run[0][0] - 1, run[-1][0] + 1)
        if any(0 <= flank < axis_length and float(np.mean((
                ink[span[0]:span[1], flank] if vertical else ink[flank, span[0]:span[1]])
                >= min_mean_ink)) >= min_support_ratio for flank in flanks):
            continue
        weights = np.asarray([item[2] for item in run], dtype=float)
        positions = np.asarray([item[0] for item in run], dtype=float)
        output.append({"pixel": float(np.average(positions, weights=weights)),
                       "support_ratio": max(item[1] for item in run),
                       "mean_ink": max(item[2] for item in run)})
    return output


def _visible_span_support(ink, *, coordinate, radius, span, vertical):
    """Maximum weak-stroke coverage; broken/faint lines count as uncertainty.

    This deliberately does not require a continuous line. It is used only to
    report weak linear coverage, never to reject, choose or move an edge.
    """
    if span[1] - span[0] < 8:
        return None
    limit = ink.shape[1] if vertical else ink.shape[0]
    lower = max(0, math.floor(coordinate) - radius)
    upper = min(limit, math.ceil(coordinate) + radius + 1)
    region = (ink[span[0]:span[1], lower:upper] if vertical
              else ink[lower:upper, span[0]:span[1]].T)
    if region.size == 0:
        return None
    return float(np.max(np.mean(region >= _VISIBLE_CONTRAST, axis=0)))


def _full_band_visible_pixels(ink, box, x_radius, z_radius):
    """Count any visible stroke in complete bands, preserving bbox extrema."""
    if x_radius is None or z_radius is None:
        return None
    left, top, right, bottom = box
    x0, x1 = max(0, math.floor(left) - x_radius), min(ink.shape[1], math.ceil(right) + x_radius + 1)
    y0, y1 = max(0, math.floor(top) - z_radius), min(ink.shape[0], math.ceil(bottom) + z_radius + 1)
    bands = {
        "left": (max(0, math.floor(left) - x_radius), y0,
                 min(ink.shape[1], math.ceil(left) + x_radius + 1), y1),
        "right": (max(0, math.floor(right) - x_radius), y0,
                  min(ink.shape[1], math.ceil(right) + x_radius + 1), y1),
        "top": (x0, max(0, math.floor(top) - z_radius),
                x1, min(ink.shape[0], math.ceil(top) + z_radius + 1)),
        "bottom": (x0, max(0, math.floor(bottom) - z_radius),
                   x1, min(ink.shape[0], math.ceil(bottom) + z_radius + 1)),
    }
    if any(x1 <= x0 or y1 <= y0 for x0, y0, x1, y1 in bands.values()):
        return None
    return {name: int(np.count_nonzero(ink[y0:y1, x0:x1] >= _VISIBLE_CONTRAST))
            for name, (x0, y0, x1, y1) in bands.items()}


def _unmoved_edge(coordinate: float | None, status: str, reason: str) -> dict[str, Any]:
    value = None if coordinate is None else float(coordinate)
    return {
        "status": status,
        "original_px": value,
        "aligned_px": value,
        "shift_px": 0.0,
        "reason": reason,
    }


def _reject_inverted_pair(
    edges: dict[str, dict[str, Any]],
    aligned_box: list[float | None],
    first_name: str,
    second_name: str,
) -> None:
    first_index, second_index = _EDGES.index(first_name), _EDGES.index(second_name)
    first, second = aligned_box[first_index], aligned_box[second_index]
    if first is None or second is None or first < second:
        return
    for name, index in ((first_name, first_index), (second_name, second_index)):
        edge = edges[name]
        if edge["status"] == "aligned":
            aligned_box[index] = edge["original_px"]
            edge.update(
                status="rejected_geometry",
                aligned_px=edge["original_px"],
                shift_px=0.0,
                reason="aligned edges would invert or collapse the opening",
            )
