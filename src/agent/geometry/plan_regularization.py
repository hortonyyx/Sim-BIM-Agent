"""Deterministic plan-draft regularisation before strict compilation.

This module edits plan drafts, never compiled/source-BIM polygons.  The strict
``compile_plan_partition`` function remains literal.  Callers opt new drafts
into these rules, persist the returned plan and report, then compile it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from src.agent.geometry.source_image_overlay import _axis_anchors
from src.agent.geometry.wall_merge_policy import (
    decorate_segments, within_wall_tolerance, remember_merge,
    retain_remainder_metadata, prune_removed_metadata,
)


RULE_VERSION = "plan_regularization_v1"
ALIGNMENT_THRESHOLD_M = 0.30
MIN_SPACE_WIDTH_M = 0.60
PIXEL_DECIMALS = 9
NUMERIC_EQUALITY_TOLERANCE_M = 1e-7
CROSS_STOREY_FAILURE_POLICIES = {"reject_assembly", "skip_failed_pair"}
_EPS = 1e-8


def _strictly_under(value: float, threshold: float) -> bool:
    """Numerically stable implementation of the product's strict '<' rule."""
    return value > NUMERIC_EQUALITY_TOLERANCE_M and value < threshold - _EPS


class PlanRegularizationError(ValueError):
    """A draft cannot be regularised without violating a hard BIM rule."""

    def __init__(self, message: str, *, report: dict):
        super().__init__(message)
        self.report = report


def _plan_digest(plan: dict) -> str:
    # Serialization is read-only; do not copy the large audit only to omit it.
    payload = {key: value for key, value in plan.items() if key != "regularization"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _calibration(plan: dict, image_size: tuple[int, int]) -> dict:
    if (not isinstance(image_size, tuple) or len(image_size) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in image_size)):
        raise ValueError("image_size must be a (positive_width, positive_height) tuple")
    width, height = image_size
    x_slope, x_offset, _ = _axis_anchors(plan["x_anchors"], axis="x", size=width)
    y_slope, y_offset, _ = _axis_anchors(plan["y_anchors"], axis="y", size=height)
    return {
        "x": {"slope": x_slope, "offset": x_offset, "size": width},
        "y": {"slope": y_slope, "offset": y_offset, "size": height},
    }


def _world(calibration: dict, point: list[float] | tuple[float, float]) -> tuple[float, float]:
    return tuple(
        calibration[axis]["slope"] * float(point[index]) + calibration[axis]["offset"]
        for index, axis in enumerate(("x", "y"))
    )


def _pixel_coordinate(calibration: dict, axis: str, value_m: float) -> float:
    row = calibration[axis]
    pixel = (value_m - row["offset"]) / row["slope"]
    nearest_integer = round(pixel)
    if math.isclose(pixel, nearest_integer, rel_tol=0.0, abs_tol=1e-9):
        return float(nearest_integer)
    # Match the precompile pixel precision; inverse world calibration must not
    # reopen an exactly joined endpoint with a neighbouring binary float.
    return round(pixel, PIXEL_DECIMALS)


def _axis_segment(first: list[float], second: list[float], calibration: dict) -> dict | None:
    a, b = _world(calibration, first), _world(calibration, second)
    if math.isclose(a[0], b[0], abs_tol=_EPS) and not math.isclose(a[1], b[1], abs_tol=_EPS):
        return {"axis": "x", "coordinate_m": a[0], "span_m": sorted((a[1], b[1]))}
    if math.isclose(a[1], b[1], abs_tol=_EPS) and not math.isclose(a[0], b[0], abs_tol=_EPS):
        return {"axis": "y", "coordinate_m": a[1], "span_m": sorted((a[0], b[0]))}
    return None


def _segments(plan: dict, calibration: dict) -> list[dict]:
    rows = []
    for partition_index, partition in enumerate(plan.get("partitions", [])):
        for segment_index, (first, second) in enumerate(zip(partition.get("points", []), partition.get("points", [])[1:])):
            line = _axis_segment(first, second, calibration)
            if line is None:
                continue
            rows.append({
                **line,
                "partition_id": partition.get("id", f"partition[{partition_index}]"),
                "partition_index": partition_index,
                "segment_index": segment_index,
                "points_pixel": [list(first), list(second)],
                "length_m": line["span_m"][1] - line["span_m"][0],
            })
    return decorate_segments(plan, rows)


def _footprint_segments(plan: dict, calibration: dict) -> list[dict]:
    points = plan.get("footprint_pixels", [])
    if points and points[0] != points[-1]:
        points = [*points, points[0]]
    rows = []
    for index, (first, second) in enumerate(zip(points, points[1:])):
        line = _axis_segment(first, second, calibration)
        if line is None:
            continue
        rows.append({**line, "partition_id": f"footprint[{index}]",
                     "partition_index": -1, "segment_index": index,
                     "points_pixel": [list(first), list(second)],
                     "length_m": line["span_m"][1] - line["span_m"][0],
                     "fixed_exterior": True})
    return decorate_segments(plan, rows)


def _overlap(first: dict, second: dict) -> float:
    return max(0.0, min(first["span_m"][1], second["span_m"][1])
               - max(first["span_m"][0], second["span_m"][0]))


def _span_gap(first: dict, second: dict) -> float:
    if _overlap(first, second) > _EPS:
        return 0.0
    return max(first["span_m"][0], second["span_m"][0]) - min(
        first["span_m"][1], second["span_m"][1]
    )


def _regularization_inputs(plan: dict) -> tuple[dict[str, list[dict]], list[dict]]:
    raw = plan.get("regularization_inputs", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {
            "line_references", "coordinate_references", "reading_alignment"}:
        raise ValueError(
            "regularization_inputs allows only line_references, coordinate_references and reading_alignment"
        )
    by_partition: dict[str, list[dict]] = {}
    for index, row in enumerate(raw.get("line_references", [])):
        if (not isinstance(row, dict)
                or set(row) != {"partition_id", "basis", "source_refs"}):
            raise ValueError(
                f"regularization_inputs.line_references[{index}] needs partition_id, basis and source_refs"
            )
        if row["basis"] not in {"dimension", "ink", "measured", "inferred"}:
            raise ValueError(f"line reference {index} has unsupported basis {row['basis']!r}")
        if (not isinstance(row["partition_id"], str) or not row["partition_id"].strip()
                or not isinstance(row["source_refs"], list)
                or any(not isinstance(v, str) or not v.strip() for v in row["source_refs"])):
            raise ValueError(f"line reference {index} has invalid identity or source_refs")
        by_partition.setdefault(row["partition_id"], []).append(copy.deepcopy(row))
    coordinates = []
    allowed = {"axis", "value_m", "basis", "chain_id", "tick_index", "source_refs"}
    required = allowed
    for index, row in enumerate(raw.get("coordinate_references", [])):
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(
                f"regularization_inputs.coordinate_references[{index}] needs "
                "axis, value_m, basis, chain_id, tick_index and source_refs"
            )
        if row["axis"] not in {"x", "y"} or row["basis"] != "dimension":
            raise ValueError(f"coordinate reference {index} must be an x/y dimension reference")
        item = copy.deepcopy(row)
        item["value_m"] = _finite(item["value_m"], f"coordinate reference {index}.value_m")
        if (not isinstance(item["chain_id"], str) or not item["chain_id"].strip()
                or isinstance(item["tick_index"], bool) or not isinstance(item["tick_index"], int)
                or item["tick_index"] < 0
                or not isinstance(item["source_refs"], list)
                or any(not isinstance(v, str) or not v.strip() for v in item["source_refs"])):
            raise ValueError(f"coordinate reference {index} has invalid chain, tick or source_refs")
        coordinates.append(item)
    audit = raw.get("reading_alignment")
    if audit is not None:
        if (not isinstance(audit, dict)
                or set(audit) - {"schema_version", "ink", "dimensions", "lite_bim", "wall_merging"}
                or not {"schema_version", "ink", "dimensions"}.issubset(audit)
                or not isinstance(audit["schema_version"], str)
                or not audit["schema_version"].strip()):
            raise ValueError("reading_alignment needs schema_version, ink and dimensions")
        section_fields = {"summary", "items", "rejections", "search_or_tolerance"}
        for name in ("ink", "dimensions"):
            section = audit[name]
            if not isinstance(section, dict) or set(section) != section_fields:
                raise ValueError(
                    f"reading_alignment.{name} needs summary, items, rejections and search_or_tolerance"
                )
            if (not isinstance(section["summary"], dict)
                    or not isinstance(section["items"], list)
                    or not isinstance(section["rejections"], list)
                    or not isinstance(section["search_or_tolerance"], (dict, str, int, float))):
                raise ValueError(f"reading_alignment.{name} has invalid audit value types")
    return by_partition, coordinates


def _separation_rows(segments: list[dict]) -> dict[tuple, dict]:
    rows = {}
    for index, first in enumerate(segments):
        for second in segments[index + 1:]:
            if first["partition_id"] == second["partition_id"] or first["axis"] != second["axis"]:
                continue
            overlap = _overlap(first, second)
            distance = abs(first["coordinate_m"] - second["coordinate_m"])
            if overlap <= _EPS or distance + _EPS < ALIGNMENT_THRESHOLD_M:
                continue
            first_key = (first["partition_id"], first["segment_index"])
            second_key = (second["partition_id"], second["segment_index"])
            key = tuple(sorted((first_key, second_key)))
            rows[key] = {
                "lines": [_line_public(first), _line_public(second)],
                "distance_m": round(distance, 9), "overlap_m": round(overlap, 9),
            }
    return rows


_BASIS_PRIORITY = {"inferred": 0, "measured": 1, "ink": 2, "dimension": 3}


def _priority(segment: dict, line_refs: dict[str, list[dict]], coordinate_refs: list[dict],
              *, fixed_exterior: bool = False, lower_storey: bool = False,
              coordinate_support: tuple[int, int] = (0, 0)) -> tuple:
    bases = [row["basis"] for row in line_refs.get(segment["partition_id"], [])]
    bases.extend(
        "dimension" for row in coordinate_refs
        if row["axis"] == segment["axis"]
        and math.isclose(row["value_m"], segment["coordinate_m"], abs_tol=1e-6)
    )
    basis = max((_BASIS_PRIORITY[value] for value in bases), default=0)
    return (basis, int(fixed_exterior), *coordinate_support,
            segment.get("length_m", 0.0), int(lower_storey))


def _same_floor_coordinate_support(plan: dict, candidate: dict,
                                   calibration: dict) -> tuple[int, int]:
    """Count actual segments on one exact coordinate, including disjoint pieces."""
    lines = {
        (segment["partition_id"], segment["segment_index"])
        for segment in _segments(plan, calibration)
        if (segment["axis"] == candidate["axis"]
            and math.isclose(
                segment["coordinate_m"], candidate["coordinate_m"], abs_tol=1e-7))
    }
    return (1 if lines else 0), len(lines)


def _line_public(segment: dict) -> dict:
    result = {
        "partition_id": segment["partition_id"],
        "axis": segment["axis"],
        "coordinate_m": round(segment["coordinate_m"], 9),
        "span_m": [round(v, 9) for v in segment["span_m"]],
    }
    if "floor_id" in segment:
        result["floor_id"] = segment["floor_id"]
    if segment.get('thickness_m'):
        result.update(thickness_m=segment['thickness_m'], reference_side=segment['reference_side'])
    return result


def _opening_on_segment(opening: dict, segment: dict, calibration: dict) -> bool:
    p1, p2 = opening.get("p1"), opening.get("p2")
    if not isinstance(p1, list) or not isinstance(p2, list):
        return False
    line = _axis_segment(p1, p2, calibration)
    return bool(line and line["axis"] == segment["axis"]
                and math.isclose(line["coordinate_m"], segment["coordinate_m"], abs_tol=1e-7)
                and _overlap(line, segment) > _EPS)


def _opening_width_m(opening: dict, calibration: dict) -> float:
    return math.dist(_world(calibration, opening["p1"]),
                     _world(calibration, opening["p2"]))


def _structured_gap_support(plan: dict, opening_id: str) -> bool:
    audit = plan.get("regularization_inputs", {}).get("reading_alignment", {})
    ink = audit.get("ink", {}) if isinstance(audit, dict) else {}
    return any(
        isinstance(row, dict)
        and row.get("object") == f"opening:{opening_id}"
        and isinstance(row.get("gap_evidence"), dict)
        and row["gap_evidence"].get("status") == "supported"
        for row in ink.get("items", []) if isinstance(row, dict)
    )


def _has_hard_opening_width(plan: dict, opening_id: str) -> bool:
    audit = plan.get("regularization_inputs", {}).get("reading_alignment", {})
    dimensions = audit.get("dimensions", {}) if isinstance(audit, dict) else {}
    return any(
        isinstance(row, dict)
        and (row.get("object") == f"opening:{opening_id}"
             or row.get("opening_id") == opening_id)
        for row in dimensions.get("items", []) if isinstance(row, dict)
    )


def _complete_open_passage_boundaries(plan: dict, opening: dict, host: dict,
                                      moving: dict, calibration: dict) -> dict | None:
    """Identify the two terminal walls of one evidence-backed open passage.

    This is a narrow compatibility path for saved drafts that encoded a
    complete passage as ``door/state=open``.  It deliberately does not inspect
    IDs or source-ref prose, and ordinary open doors do not qualify merely from
    their state.
    """
    identity = opening.get("id")
    if (opening.get("kind") not in {"door", "open"}
            or opening.get("state") != "open"
            or not isinstance(identity, str)
            or not _structured_gap_support(plan, identity)
            or _has_hard_opening_width(plan, identity)):
        return None
    normal_index = 0 if moving["axis"] == "x" else 1
    host_coordinate = host["coordinate_m"]
    endpoints = {
        key: _world(calibration, opening[key])[normal_index]
        for key in ("p1", "p2")
    }
    candidates = []
    for boundary in _segments(plan, calibration):
        if (boundary["axis"] != moving["axis"]
                or boundary["partition_id"] == host["partition_id"]
                or min(abs(value - host_coordinate)
                       for value in boundary["span_m"])
                > NUMERIC_EQUALITY_TOLERANCE_M):
            continue
        candidates.append(boundary)
    def terminal_direction(boundary):
        other = max(boundary["span_m"]) if math.isclose(
            min(boundary["span_m"]), host_coordinate,
            abs_tol=NUMERIC_EQUALITY_TOLERANCE_M) else min(
                boundary["span_m"])
        return 1 if other > host_coordinate else -1
    assignments = []
    for first in candidates:
        for second in candidates:
            if ((first["partition_id"], first["segment_index"])
                    == (second["partition_id"], second["segment_index"])):
                continue
            if (abs(first["coordinate_m"] - endpoints["p1"])
                    >= ALIGNMENT_THRESHOLD_M
                    or abs(second["coordinate_m"] - endpoints["p2"])
                    >= ALIGNMENT_THRESHOLD_M):
                continue
            opening_lo, opening_hi = sorted(endpoints.values())
            boundary_lo, boundary_hi = sorted(
                (first["coordinate_m"], second["coordinate_m"]))
            if (boundary_lo > opening_lo + NUMERIC_EQUALITY_TOLERANCE_M
                    or boundary_hi < opening_hi - NUMERIC_EQUALITY_TOLERANCE_M):
                continue
            moving_key = (moving["partition_id"], moving["segment_index"])
            if moving_key not in {
                    (first["partition_id"], first["segment_index"]),
                    (second["partition_id"], second["segment_index"])}:
                continue
            if terminal_direction(first) != terminal_direction(second):
                continue
            assignments.append((
                abs(first["coordinate_m"] - endpoints["p1"])
                + abs(second["coordinate_m"] - endpoints["p2"]),
                first, second,
            ))
    if not assignments:
        return None

    # Multiple partition IDs may describe the same collinear terminal wall.
    # Treat those as one geometric interpretation, but do not let proximity or
    # an ID tie-break choose between genuinely different terminal coordinates.
    # A non-unique interpretation falls back to the width-preserving path.
    moving_key = (moving["partition_id"], moving["segment_index"])
    interpretations = []
    for assignment in assignments:
        _, first, second = assignment
        moving_edge = "p1" if (
            first["partition_id"], first["segment_index"]) == moving_key else "p2"
        match = next((group for group in interpretations
                      if group["moving_edge"] == moving_edge
                      and terminal_direction(group["first"]) == terminal_direction(first)
                      and terminal_direction(group["second"]) == terminal_direction(second)
                      and math.isclose(group["first"]["coordinate_m"], first["coordinate_m"],
                                       rel_tol=0.0,
                                       abs_tol=NUMERIC_EQUALITY_TOLERANCE_M)
                      and math.isclose(group["second"]["coordinate_m"], second["coordinate_m"],
                                       rel_tol=0.0,
                                       abs_tol=NUMERIC_EQUALITY_TOLERANCE_M)), None)
        if match is None:
            interpretations.append({
                "moving_edge": moving_edge,
                "first": first,
                "second": second,
                "assignments": [assignment],
            })
        else:
            match["assignments"].append(assignment)
    if len(interpretations) != 1:
        return None
    _, first, second = min(interpretations[0]["assignments"], key=lambda row: (
        row[0], row[1]["partition_id"], row[2]["partition_id"]))
    return {
        "p1": first,
        "p2": second,
        "inference": {
            "basis": "structured_complete_open_passage_compatibility",
            "state": "open",
            "structured_gap_support": True,
            "complete_between_terminal_boundaries": True,
            "hard_width_reference": False,
        },
    }


def _opening_adjustment(opening: dict, before: dict, calibration: dict, *,
                        mode: str, jamb_deltas_m: dict, inference: dict,
                        boundary_ids: dict | None = None,
                        boundary_offsets_before_m: dict | None = None,
                        boundary_offsets_after_m: dict | None = None) -> dict:
    return {
        "opening_id": opening.get("id"),
        "mode": mode,
        "before": {
            "p1": copy.deepcopy(before["p1"]),
            "p2": copy.deepcopy(before["p2"]),
            "world_p1": [round(value, 9) for value in _world(
                calibration, before["p1"])],
            "world_p2": [round(value, 9) for value in _world(
                calibration, before["p2"])],
            "width_m": round(_opening_width_m(before, calibration), 9),
        },
        "after": {
            "p1": copy.deepcopy(opening["p1"]),
            "p2": copy.deepcopy(opening["p2"]),
            "world_p1": [round(value, 9) for value in _world(
                calibration, opening["p1"])],
            "world_p2": [round(value, 9) for value in _world(
                calibration, opening["p2"])],
            "width_m": round(_opening_width_m(opening, calibration), 9),
        },
        "jamb_deltas_m": {
            key: round(value, 9) for key, value in jamb_deltas_m.items()
        },
        "width_change_m": round(
            _opening_width_m(opening, calibration)
            - _opening_width_m(before, calibration), 9),
        "inference": inference,
        **({"boundary_ids": boundary_ids} if boundary_ids else {}),
        **({"boundary_offsets_before_m": {
            key: round(value, 9) for key, value in boundary_offsets_before_m.items()
        }} if boundary_offsets_before_m else {}),
        **({"boundary_offsets_after_m": {
            key: round(value, 9) for key, value in boundary_offsets_after_m.items()
        }} if boundary_offsets_after_m else {}),
    }


def _move_segment(plan: dict, segment: dict, target_coordinate_m: float, calibration: dict,
                  *, reason: str, changes: list[dict], basis: str = "existing_line") -> None:
    axis = segment["axis"]
    normal_index = 0 if axis == "x" else 1
    target_pixel = _pixel_coordinate(calibration, axis, target_coordinate_m)
    partition = plan["partitions"][segment["partition_index"]]
    points = partition["points"]
    old_pixel = points[segment["segment_index"]][normal_index]
    if math.isclose(old_pixel, target_pixel, abs_tol=_EPS):
        return
    old_segment = copy.deepcopy(segment)
    moved_openings = []
    moved_opening_ids = set()
    for opening in plan.get("openings", []):
        if _opening_on_segment(opening, old_segment, calibration):
            opening["p1"][normal_index] = target_pixel
            opening["p2"][normal_index] = target_pixel
            moved_openings.append(opening.get("id"))
            moved_opening_ids.add(opening.get("id"))
    # Endpoints on the moved wall remain connected by extending/retracting their
    # perpendicular segments.  Interior crossing points are left for strict
    # compilation to node; no extra line is invented.
    moved_endpoints = []
    collapsed_partitions = []
    blocked_collapses = []
    along_index = 1 - normal_index
    old_coordinate_pixel = old_segment["points_pixel"][0][normal_index]
    lo_px, hi_px = sorted(p[along_index] for p in old_segment["points_pixel"])
    # A T-junction may move into an opening carried by the perpendicular host
    # wall even though that host is one long segment with no endpoint at the
    # junction.  Move the opening edge facing that junction with it.  This
    # keeps the opening on the same host and on the same side of the junction;
    # the transactional compile/relationship check below still rejects any
    # case where that cannot be preserved.
    crossing_openings = []
    opening_adjustments = []
    target_coordinate_pixel = target_pixel
    sweep_lo, sweep_hi = sorted((old_coordinate_pixel, target_coordinate_pixel))
    for host in _segments(plan, calibration):
        if (host["partition_id"] == old_segment["partition_id"]
                or host["axis"] == axis
                or not (host["span_m"][0] - NUMERIC_EQUALITY_TOLERANCE_M
                        <= old_segment["coordinate_m"]
                        <= host["span_m"][1] + NUMERIC_EQUALITY_TOLERANCE_M)
                or not (old_segment["span_m"][0] - NUMERIC_EQUALITY_TOLERANCE_M
                        <= host["coordinate_m"]
                        <= old_segment["span_m"][1] + NUMERIC_EQUALITY_TOLERANCE_M)):
            continue
        for opening in plan.get("openings", []):
            identity = opening.get("id")
            if identity in moved_opening_ids or not _opening_on_segment(
                    opening, host, calibration):
                continue
            opening_lo, opening_hi = sorted(
                (opening["p1"][normal_index], opening["p2"][normal_index]))
            old_inside = (
                opening_lo - _EPS <= old_coordinate_pixel <= opening_hi + _EPS)
            swept = (
                sweep_lo < opening_hi - _EPS
                and opening_lo + _EPS < sweep_hi)
            if old_inside or not swept:
                continue
            if old_coordinate_pixel < opening_lo:
                edge = (
                    "p1" if opening["p1"][normal_index]
                    <= opening["p2"][normal_index] else "p2")
            else:
                edge = (
                    "p1" if opening["p1"][normal_index]
                    >= opening["p2"][normal_index] else "p2")
            before_opening = copy.deepcopy(opening)
            passage = _complete_open_passage_boundaries(
                plan, opening, host, old_segment, calibration)
            if passage is not None:
                moving_key = (old_segment["partition_id"], old_segment["segment_index"])
                moving_edge = next(key for key in ("p1", "p2") if (
                    passage[key]["partition_id"], passage[key]["segment_index"])
                    == moving_key)
                pixel_delta = target_coordinate_pixel - old_coordinate_pixel
                opening[moving_edge][normal_index] += pixel_delta
                jamb_deltas = {
                    "p1": target_coordinate_m - old_segment["coordinate_m"]
                    if moving_edge == "p1" else 0.0,
                    "p2": target_coordinate_m - old_segment["coordinate_m"]
                    if moving_edge == "p2" else 0.0,
                }
                before_offsets = {
                    key: _world(calibration, before_opening[key])[normal_index]
                    - passage[key]["coordinate_m"]
                    for key in ("p1", "p2")
                }
                after_offsets = {
                    key: _world(calibration, opening[key])[normal_index]
                    - (target_coordinate_m if key == moving_edge
                       else passage[key]["coordinate_m"])
                    for key in ("p1", "p2")
                }
                opening_adjustments.append(_opening_adjustment(
                    opening, before_opening, calibration,
                    mode="open_passage_boundary_follow",
                    jamb_deltas_m=jamb_deltas,
                    inference=passage["inference"],
                    boundary_ids={
                        key: passage[key]["partition_id"] for key in ("p1", "p2")
                    },
                    boundary_offsets_before_m=before_offsets,
                    boundary_offsets_after_m=after_offsets,
                ))
            else:
                pixel_delta = target_coordinate_pixel - opening[edge][normal_index]
                opening["p1"][normal_index] += pixel_delta
                opening["p2"][normal_index] += pixel_delta
                delta_m = (_world(calibration, opening["p1"])[normal_index]
                           - _world(calibration, before_opening["p1"])[normal_index])
                opening_adjustments.append(_opening_adjustment(
                    opening, before_opening, calibration,
                    mode="translate_preserve_width",
                    jamb_deltas_m={"p1": delta_m, "p2": delta_m},
                    inference={"basis": "entity_opening_width_preserved"},
                    boundary_ids={"swept_boundary": old_segment["partition_id"]},
                ))
            moved_openings.append(identity)
            moved_opening_ids.add(identity)
            crossing_openings.append(identity)
    for other in list(plan.get("partitions", [])):
        if other is partition or not other.get("points"):
            continue
        for endpoint_index in (0, len(other["points"]) - 1):
            endpoint = other["points"][endpoint_index]
            if (math.isclose(endpoint[normal_index], old_coordinate_pixel, abs_tol=_EPS)
                    and lo_px - _EPS <= endpoint[along_index] <= hi_px + _EPS):
                neighbor_index = 1 if endpoint_index == 0 else endpoint_index - 1
                neighbor = other["points"][neighbor_index]
                if math.isclose(neighbor[normal_index], endpoint[normal_index], abs_tol=_EPS):
                    continue
                old_other = _axis_segment(endpoint, neighbor, calibration)
                would_collapse = (
                    len(other["points"]) == 2
                    and math.isclose(neighbor[normal_index], target_pixel, abs_tol=_EPS)
                )
                hosted_openings = []
                if would_collapse and old_other is not None:
                    hosted_openings = [
                        opening.get("id") for opening in plan.get("openings", [])
                        if _opening_on_segment(
                            opening,
                            {**old_other, "points_pixel": [list(endpoint), list(neighbor)]},
                            calibration,
                        )
                    ]
                if hosted_openings:
                    blocked_collapses.append({
                        "partition_id": other.get("id"),
                        "opening_ids": hosted_openings,
                    })
                    continue
                other_points_before = copy.deepcopy(other["points"])
                endpoint_before = endpoint[normal_index]
                endpoint_delta = target_pixel - endpoint_before
                if old_other is not None:
                    for opening in plan.get("openings", []):
                        identity = opening.get("id")
                        if (identity in moved_opening_ids
                                or not _opening_on_segment(
                                    opening,
                                    {**old_other,
                                     "points_pixel": [list(endpoint), list(neighbor)]},
                                    calibration)):
                            continue
                        if any(math.isclose(
                                opening[key][normal_index], endpoint_before, abs_tol=_EPS)
                                for key in ("p1", "p2")):
                            opening["p1"][normal_index] += endpoint_delta
                            opening["p2"][normal_index] += endpoint_delta
                            moved_openings.append(identity)
                            moved_opening_ids.add(identity)
                endpoint[normal_index] = target_pixel
                moved_endpoints.append({"partition_id": other.get("id"), "endpoint": endpoint_index})
                if would_collapse:
                    collapsed_partitions.append({
                        "partition_id": other.get("id"),
                        "line_before": old_other,
                        "points_before": other_points_before,
                        "point_after": list(endpoint),
                    })
    collapsed_ids = {row["partition_id"] for row in collapsed_partitions}
    if collapsed_ids:
        plan["partitions"] = [
            row for row in plan.get("partitions", []) if row.get("id") not in collapsed_ids
        ]
        for row in collapsed_partitions:
            collapsed_line = row["line_before"]
            changes.append({
                "type": "remove_collapsed_wall_step",
                "floor_id": segment.get("floor_id", plan.get("floor_id")),
                "axis": collapsed_line["axis"],
                "partition_id": row["partition_id"],
                "object_ids": {"partitions": [row["partition_id"]], "openings": []},
                "from_m": round(collapsed_line["coordinate_m"], 9),
                "to_m": round(collapsed_line["coordinate_m"], 9),
                "span_m": [round(v, 9) for v in collapsed_line["span_m"]],
                "points_before": row["points_before"],
                "point_after": row["point_after"],
                "movement_m": round(
                    collapsed_line["span_m"][1] - collapsed_line["span_m"][0], 9),
                "basis": basis,
                "reason": "the sub-0.30 m connector collapsed to a point while its adjoining wall was aligned",
            })
    points[segment["segment_index"]][normal_index] = target_pixel
    points[segment["segment_index"] + 1][normal_index] = target_pixel
    _simplify_partition(partition)
    changes.append({
        "type": "move_wall_line",
        "floor_id": segment.get("floor_id", plan.get("floor_id")),
        "axis": axis,
        "partition_id": old_segment["partition_id"],
        "object": _line_public(old_segment),
        "object_ids": {"partitions": [old_segment["partition_id"]],
                       "openings": moved_openings},
        "span_m": [round(v, 9) for v in old_segment["span_m"]],
        "from_m": round(old_segment["coordinate_m"], 9),
        "to_m": round(target_coordinate_m, 9),
        "movement_m": round(abs(target_coordinate_m - old_segment["coordinate_m"]), 9),
        "basis": basis, "reason": reason,
        "opening_ids": moved_openings,
        "moved_opening_ids": moved_openings,
        "adjusted_crossing_opening_ids": crossing_openings,
        "opening_adjustments": opening_adjustments,
        "adjusted_endpoint_ids": moved_endpoints,
        "blocked_collapses": blocked_collapses,
    })


def _simplify_partition(partition: dict) -> None:
    points = partition.get("points", [])
    changed = True
    while changed and len(points) > 2:
        changed = False
        for index in range(1, len(points) - 1):
            previous, current, following = points[index - 1:index + 2]
            if current == previous or current == following or (
                    previous[0] == current[0] == following[0]) or (
                    previous[1] == current[1] == following[1]):
                points.pop(index)
                changed = True
                break


def _strip_protection(plan: dict, first: dict, second: dict, calibration: dict) -> dict:
    axis = first["axis"]
    overlap_lo = max(first["span_m"][0], second["span_m"][0])
    overlap_hi = min(first["span_m"][1], second["span_m"][1])
    normal_lo, normal_hi = sorted((first["coordinate_m"], second["coordinate_m"]))
    if axis == "x":
        polygon = Polygon([(normal_lo, overlap_lo), (normal_hi, overlap_lo),
                           (normal_hi, overlap_hi), (normal_lo, overlap_hi)])
    else:
        polygon = Polygon([(overlap_lo, normal_lo), (overlap_hi, normal_lo),
                           (overlap_hi, normal_hi), (overlap_lo, normal_hi)])
    seeds = []
    for seed in plan.get("space_seeds", []):
        point = seed.get("point")
        if isinstance(point, list) and polygon.contains(Point(_world(calibration, point))):
            seeds.append(seed.get("id"))
    openings = []
    for opening in plan.get("openings", []):
        if not isinstance(opening.get("p1"), list) or not isinstance(opening.get("p2"), list):
            continue
        line = LineString([_world(calibration, opening["p1"]), _world(calibration, opening["p2"])])
        if line.intersects(polygon):
            openings.append(opening.get("id"))
    return {"space_seed_ids": seeds, "opening_ids": openings,
            "length_m": round(overlap_hi - overlap_lo, 9)}


def _merge_strip_contents(plan: dict, source: dict, target: dict, calibration: dict,
                          protection: dict, changes: list[dict],
                          rejections: list[dict]) -> bool:
    """Remove a false strip seed and move/deduplicate openings transactionally."""
    axis = source["axis"]
    normal_index = 0 if axis == "x" else 1
    along_index = 1 - normal_index
    target_pixel = _pixel_coordinate(calibration, axis, target["coordinate_m"])
    hosted = []
    hosted_ids = set()
    for index, opening in enumerate(plan.get("openings", [])):
        origins = set()
        if _opening_on_segment(opening, source, calibration):
            origins.add("source")
        if _opening_on_segment(opening, target, calibration):
            origins.add("target")
        if not origins:
            continue
        line = _axis_segment(opening["p1"], opening["p2"], calibration)
        identity = opening.get("id")
        hosted_ids.add(identity)
        hosted.append({"index": index, "opening": opening, "origins": origins,
                       "span_m": list(line["span_m"]), "id": identity})

    for row in hosted:
        if ("source" in row["origins"]
                and (row["span_m"][0] < source["span_m"][0] - _EPS
                     or row["span_m"][1] > source["span_m"][1] + _EPS)):
            rejections.append({
                "type": "narrow_strip_opening_conflict",
                "contradiction_category": "opening_crosses_partial_merge_boundary",
                "lines": [_line_public(source), _line_public(target)],
                "opening_ids": [row["id"]], "opening_span_m": row["span_m"],
                "merge_span_m": [round(v, 9) for v in source["span_m"]],
                "fix": "Split or re-host the opening so it lies wholly inside or outside the duplicate overlap span.",
                "message": "An opening on the removable line crosses the end of a partial wall merge, so moving it would detach its unmerged portion.",
            })
            return False

    unhosted = [identity for identity in protection["opening_ids"]
                if identity not in hosted_ids]
    if unhosted:
        rejections.append({
            "type": "narrow_strip_opening_host_ambiguous",
            "contradiction_category": "opening_not_hosted_on_either_merged_wall",
            "lines": [_line_public(source), _line_public(target)],
            "distance_m": round(abs(source["coordinate_m"] - target["coordinate_m"]), 9),
            "length_m": protection["length_m"], "opening_ids": unhosted,
            "fix": "Attach each listed opening to one of the two duplicate wall lines, then retry the merge.",
            "message": "A narrow-strip opening intersects the strip but is not hosted by either wall line, so its connection cannot be preserved automatically.",
        })
        return False

    # Once both wall representatives become one host, all along-wall overlaps
    # form duplicate groups, including target-line duplicates and transitive
    # bridges.  Compare states by the strict compiler's effective semantics.
    parents = list(range(len(hosted)))

    def effective_state(opening):
        if opening.get("kind") == "door":
            return opening.get("state", "unknown")
        if opening.get("kind") == "open":
            return "open"
        return None

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(first, second):
        a, b = find(first), find(second)
        if a != b:
            parents[b] = a

    for index, first in enumerate(hosted):
        for second_index, second in enumerate(hosted[index + 1:], start=index + 1):
            overlap = max(0.0, min(first["span_m"][1], second["span_m"][1])
                          - max(first["span_m"][0], second["span_m"][0]))
            if overlap <= _EPS:
                continue
            first_kind = first["opening"].get("kind")
            second_kind = second["opening"].get("kind")
            first_state = effective_state(first["opening"])
            second_state = effective_state(second["opening"])
            contradiction = None
            if first_kind != second_kind:
                contradiction = "overlapping_openings_have_incompatible_kinds"
            elif first_state != second_state:
                contradiction = "overlapping_openings_have_incompatible_passage_states"
            if contradiction:
                rejections.append({
                    "type": "narrow_strip_opening_conflict",
                    "contradiction_category": contradiction,
                    "lines": [_line_public(source), _line_public(target)],
                    "distance_m": round(abs(source["coordinate_m"] - target["coordinate_m"]), 9),
                    "length_m": protection["length_m"],
                    "opening_ids": [first["id"], second["id"]],
                    "overlap_m": round(overlap, 9),
                    "opening_values": [{"id": row["id"],
                                        "kind": row["opening"].get("kind"),
                                        "z": row["opening"].get("z"),
                                        "declared_state": row["opening"].get("state"),
                                        "effective_state": effective_state(row["opening"])}
                                       for row in (first, second)],
                    "fix": "Resolve the opening kind or effective passage-state conflict before merging the duplicate walls.",
                    "message": "Overlapping openings on the merged host line disagree on opening kind or effective passage state, so one surviving connection cannot express both.",
                })
                return False
            union(index, second_index)

    groups = defaultdict(list)
    for index, row in enumerate(hosted):
        groups[find(index)].append(row)

    removed_seeds = []
    protected_seed_ids = set(protection["space_seed_ids"])
    kept_seeds = []
    for seed in plan.get("space_seeds", []):
        if seed.get("id") in protected_seed_ids:
            removed_seeds.append(copy.deepcopy(seed))
        else:
            kept_seeds.append(seed)
    if removed_seeds:
        plan["space_seeds"] = kept_seeds
        changes.append({
            "type": "remove_narrow_strip_space_seeds", "floor_id": plan.get("floor_id"),
            "axis": axis, "span_m": [round(max(source["span_m"][0], target["span_m"][0]), 9),
                                      round(min(source["span_m"][1], target["span_m"][1]), 9)],
            "object_ids": {"partitions": [source["partition_id"], target["partition_id"]],
                           "space_seeds": [row.get("id") for row in removed_seeds],
                           "openings": []},
            "removed_space_seeds": removed_seeds,
            "basis": "sub_0_30m_overlapping_duplicate_wall_strip",
            "reason": "the named seed occupied a false narrow strip eliminated by the mandatory wall merge",
        })

    moved_openings = []
    for row in hosted:
        opening = row["opening"]
        before = [copy.deepcopy(opening["p1"]), copy.deepcopy(opening["p2"])]
        opening["p1"][normal_index] = target_pixel
        opening["p2"][normal_index] = target_pixel
        if "source" in row["origins"] and not math.isclose(
                source["coordinate_m"], target["coordinate_m"], abs_tol=_EPS):
            moved_openings.append({"opening_id": row["id"], "from_points": before,
                                   "to_points": [copy.deepcopy(opening["p1"]),
                                                 copy.deepcopy(opening["p2"])],
                                   "from_m": round(source["coordinate_m"], 9),
                                   "to_m": round(target["coordinate_m"], 9)})
    if moved_openings:
        changes.append({
            "type": "move_openings_to_merged_wall", "floor_id": plan.get("floor_id"),
            "axis": axis, "partition_id": source["partition_id"],
            "object_ids": {"partitions": [source["partition_id"], target["partition_id"]],
                           "openings": [row["opening_id"] for row in moved_openings]},
            "from_m": round(source["coordinate_m"], 9),
            "to_m": round(target["coordinate_m"], 9),
            "movement_m": round(abs(source["coordinate_m"] - target["coordinate_m"]), 9),
            "span_m": [round(max(source["span_m"][0], target["span_m"][0]), 9),
                       round(min(source["span_m"][1], target["span_m"][1]), 9)],
            "openings": moved_openings, "basis": "retained_wall_line",
            "reason": "openings on both duplicate lines moved with their host to the retained line",
        })
    retained_target_openings = [
        row["id"] for row in hosted if "target" in row["origins"]
    ]
    if retained_target_openings:
        changes.append({
            "type": "retain_openings_on_merged_wall", "floor_id": plan.get("floor_id"),
            "axis": axis, "partition_id": target["partition_id"],
            "object_ids": {"partitions": [source["partition_id"], target["partition_id"]],
                           "openings": retained_target_openings},
            "opening_ids": retained_target_openings,
            "span_m": [round(v, 9) for v in target["span_m"]],
            "from_m": round(target["coordinate_m"], 9),
            "to_m": round(target["coordinate_m"], 9),
            "movement_m": 0.0,
            "basis": "surviving_merged_wall_host",
            "reason": "openings already on the retained line survived the strip elimination and their before/after hosts remain explicitly mapped",
        })

    removed_indices = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        retained = min(group, key=lambda row: (
            0 if "target" in row["origins"] else 1, str(row["id"])))
        removed = [row for row in group if row is not retained]
        lo = min(row["span_m"][0] for row in group)
        hi = max(row["span_m"][1] for row in group)
        along_axis = "y" if axis == "x" else "x"
        retained["opening"]["p1"][along_index] = _pixel_coordinate(calibration, along_axis, lo)
        retained["opening"]["p2"][along_index] = _pixel_coordinate(calibration, along_axis, hi)
        retained["opening"]["source_refs"] = list(dict.fromkeys(
            ref for row in group for ref in row["opening"].get("source_refs", [])))
        removed_indices.update(row["index"] for row in removed)
        changes.append({
            "type": "merge_overlapping_openings", "floor_id": plan.get("floor_id"),
            "axis": axis, "partition_id": target["partition_id"],
            "object_ids": {"partitions": [source["partition_id"], target["partition_id"]],
                           "openings": [row["id"] for row in group]},
            "retained_opening_id": retained["id"],
            "removed_opening_ids": [row["id"] for row in removed],
            "before_spans_m": [{"opening_id": row["id"], "span_m": [round(v, 9) for v in row["span_m"]]}
                               for row in group],
            "after_span_m": [round(lo, 9), round(hi, 9)],
            "before_z_m": [{"opening_id": row["id"], "z": copy.deepcopy(row["opening"].get("z"))}
                           for row in group],
            "chosen_z_m": copy.deepcopy(retained["opening"].get("z")),
            "effective_state": effective_state(retained["opening"]),
            "semantic_resolution": "retain target-line opening when present, then lexical id; z does not define horizontal host connectivity",
            "basis": "overlapping_openings_on_duplicate_wall_lines",
            "reason": "overlapping semantically identical openings became one opening on the retained wall",
        })
    if removed_indices:
        plan["openings"] = [opening for index, opening in enumerate(plan.get("openings", []))
                            if index not in removed_indices]
    return True


def _clipped_segment(segment: dict, lo: float, hi: float) -> dict:
    result = copy.deepcopy(segment)
    result["span_m"] = [lo, hi]
    result["length_m"] = hi - lo
    return result


def _partition_remainders(plan: dict, segment: dict, overlap_lo: float, overlap_hi: float,
                          calibration: dict) -> list[str]:
    """Remove only an overlapping span and retain real straight remainders."""
    partition = plan["partitions"][segment["partition_index"]]
    span_lo, span_hi = segment["span_m"]
    ranges = []
    if span_lo < overlap_lo - _EPS:
        ranges.append((span_lo, overlap_lo))
    if overlap_hi < span_hi - _EPS:
        ranges.append((overlap_hi, span_hi))
    if not ranges:
        plan["partitions"].remove(partition)
        retain_remainder_metadata(plan, partition['id'], [])
        return []
    axis = segment["axis"]
    normal_axis = axis
    along_axis = "y" if axis == "x" else "x"
    normal_pixel = _pixel_coordinate(calibration, normal_axis, segment["coordinate_m"])

    def points_for(bounds):
        lo_px = _pixel_coordinate(calibration, along_axis, bounds[0])
        hi_px = _pixel_coordinate(calibration, along_axis, bounds[1])
        return ([[normal_pixel, lo_px], [normal_pixel, hi_px]] if axis == "x"
                else [[lo_px, normal_pixel], [hi_px, normal_pixel]])

    partition["points"] = points_for(ranges[0])
    ids = [partition.get("id")]
    existing = {row.get("id") for row in plan["partitions"]}
    for remainder_index, bounds in enumerate(ranges[1:], start=2):
        identity = f"{partition.get('id')}::remainder-{remainder_index}"
        suffix = 2
        while identity in existing:
            identity = f"{partition.get('id')}::remainder-{remainder_index}-{suffix}"
            suffix += 1
        existing.add(identity)
        remainder = copy.deepcopy(partition)
        remainder["id"] = identity
        remainder["points"] = points_for(bounds)
        plan["partitions"].append(remainder)
        ids.append(identity)
    retain_remainder_metadata(plan, partition['id'], ids)
    return ids


def _split_polyline_for_merge(plan: dict, segment: dict,
                              line_refs: dict[str, list[dict]],
                              changes: list[dict]) -> bool:
    """Give every leg of a merge-participating polyline its own stable wall id.

    The split is representational: it preserves every consecutive point pair,
    source reference and line-reference basis.  Keeping the selected leg on the
    original id lets the existing merge audit remain readable, while the other
    legs receive deterministic child ids and remain eligible for later merges.
    """
    partition = plan["partitions"][segment["partition_index"]]
    points = partition.get("points", [])
    if len(points) <= 2:
        return False
    original_id = partition.get("id", segment["partition_id"])
    selected_index = segment["segment_index"]
    existing_ids = {
        row.get("id") for row in plan.get("partitions", []) if row is not partition
    }
    split_partitions = []
    mapping = []
    child_ids = []
    for index, (first, second) in enumerate(zip(points, points[1:])):
        child = copy.deepcopy(partition)
        if index == selected_index:
            child_id = original_id
        else:
            base = f"{original_id}::segment-{index + 1}"
            child_id = base
            suffix = 2
            while child_id in existing_ids:
                child_id = f"{base}-{suffix}"
                suffix += 1
        existing_ids.add(child_id)
        child["id"] = child_id
        child["points"] = [copy.deepcopy(first), copy.deepcopy(second)]
        split_partitions.append(child)
        child_ids.append(child_id)
        mapping.append({
            "source_partition_id": original_id,
            "source_segment_index": index,
            "partition_id": child_id,
            "points": copy.deepcopy(child["points"]),
        })

    partition_index = plan["partitions"].index(partition)
    plan["partitions"][partition_index:partition_index + 1] = split_partitions

    inherited = copy.deepcopy(line_refs.get(original_id, []))
    raw_inputs = plan.get("regularization_inputs")
    for child_id in child_ids:
        if child_id == original_id:
            continue
        if inherited:
            line_refs[child_id] = [
                {**copy.deepcopy(row), "partition_id": child_id} for row in inherited
            ]
            if isinstance(raw_inputs, dict):
                rows = raw_inputs.setdefault("line_references", [])
                rows.extend(copy.deepcopy(line_refs[child_id]))

    changes.append({
        "type": "split_polyline_for_merge",
        "floor_id": plan.get("floor_id"),
        "axis": segment["axis"],
        "partition_id": original_id,
        "object_ids": {"partitions": child_ids, "openings": [], "space_seeds": []},
        "from_m": round(segment["coordinate_m"], 9),
        "to_m": round(segment["coordinate_m"], 9),
        "span_m": [round(value, 9) for value in segment["span_m"]],
        "movement_m": 0.0,
        "basis": "representational_split_before_duplicate_wall_merge",
        "reason": "split a merge-participating polyline into unchanged two-point wall legs so each actual overlap can be evaluated independently",
        "geometry_changed": False,
        "source_refs": copy.deepcopy(partition.get("source_refs", [])),
        "segment_mapping": mapping,
    })
    return True


def _merge_parallel_lines(plan: dict, first: dict, second: dict, calibration: dict,
                          line_refs: dict[str, list[dict]], coordinate_refs: list[dict],
                          changes: list[dict], rejections: list[dict]) -> bool:
    protection = _strip_protection(plan, first, second, calibration)
    distance = abs(first["coordinate_m"] - second["coordinate_m"])
    first_partition = plan["partitions"][first["partition_index"]]
    second_partition = plan["partitions"][second["partition_index"]]
    first_support = _same_floor_coordinate_support(plan, first, calibration)
    second_support = _same_floor_coordinate_support(plan, second, calibration)
    p_first = _priority(
        first, line_refs, coordinate_refs, coordinate_support=first_support)
    p_second = _priority(
        second, line_refs, coordinate_refs, coordinate_support=second_support)
    if p_first == p_second:
        target, source = sorted((first, second), key=lambda row: row["partition_id"])
    else:
        target, source = (first, second) if p_first > p_second else (second, first)
    target_support = first_support if target is first else second_support
    source_support = second_support if source is second else first_support
    target_partition = plan["partitions"][target["partition_index"]]
    source_partition = plan["partitions"][source["partition_index"]]
    overlap_lo = max(target["span_m"][0], source["span_m"][0])
    overlap_hi = min(target["span_m"][1], source["span_m"][1])
    overlap_source = _clipped_segment(source, overlap_lo, overlap_hi)
    overlap_target = _clipped_segment(target, overlap_lo, overlap_hi)
    content_change_start = len(changes)
    if not _merge_strip_contents(plan, overlap_source, overlap_target, calibration,
                                 protection, changes, rejections):
        return False
    # Only the duplicate overlap disappears.  Straight source segments beyond
    # it are retained at their original coordinate as real wall evidence.
    along_index = 1 if target["axis"] == "x" else 0
    # Retarget perpendicular endpoints attached to the removed line.
    normal_index = 0 if source["axis"] == "x" else 1
    source_px = source["points_pixel"][0][normal_index]
    target_px = _pixel_coordinate(calibration, source["axis"], target["coordinate_m"])
    moved_endpoints = []
    for other in plan["partitions"]:
        if other in (source_partition, target_partition) or not other.get("points"):
            continue
        for endpoint_index in (0, len(other["points"]) - 1):
            endpoint = other["points"][endpoint_index]
            endpoint_world = _world(calibration, endpoint)
            if (math.isclose(endpoint[normal_index], source_px, abs_tol=_EPS)
                    and overlap_lo - _EPS <= endpoint_world[along_index] <= overlap_hi + _EPS):
                endpoint[normal_index] = target_px
                moved_endpoints.append({"partition_id": other.get("id"), "endpoint": endpoint_index})
    target_partition["source_refs"] = list(dict.fromkeys(
        [*target_partition.get("source_refs", []), *source_partition.get("source_refs", [])]
    ))
    source_id = source_partition.get("id")
    remember_merge(plan, source, target)
    remainder_ids = _partition_remainders(
        plan, source, overlap_lo, overlap_hi, calibration)
    content_changes = changes[content_change_start:]
    affected_openings = list(dict.fromkeys(
        identity for row in content_changes
        for identity in row.get("object_ids", {}).get("openings", [])))
    changes.append({
        "type": "merge_duplicate_wall_lines",
        "floor_id": plan.get("floor_id"), "axis": source["axis"],
        "partition_id": source_id,
        "object_ids": {"partitions": [source_id, target_partition.get("id")],
                       "openings": affected_openings,
                       "space_seeds": protection["space_seed_ids"]},
        "from_m": round(source["coordinate_m"], 9),
        "to_m": round(target["coordinate_m"], 9),
        "span_m": [round(max(source["span_m"][0], target["span_m"][0]), 9),
                   round(min(source["span_m"][1], target["span_m"][1]), 9)],
        "source": _line_public(source), "target": _line_public(target),
        "movement_m": round(distance, 9), "length_m": protection["length_m"],
        "removed_partition_id": source_id if not remainder_ids else None,
        "retained_remainder_partition_ids": remainder_ids,
        "retained_partition_id": target_partition.get("id"),
        "adjusted_endpoint_ids": moved_endpoints,
        "source_coordinate_support": {
            "floors": source_support[0], "collinear_wall_segments": source_support[1],
        },
        "target_coordinate_support": {
            "floors": target_support[0], "collinear_wall_segments": target_support[1],
        },
        "basis": ("more_collinear_wall_segments_then_line_priority"
                  if target_support > source_support and p_first[:2] == p_second[:2]
                  else "dimension_then_ink_then_existing_line_priority"),
        "reason": "wall-tolerance duplicate overlap merge; strip seeds and openings were migrated with explicit audit",
    })
    return True


def _merge_partition_to_footprint(plan: dict, source: dict, target: dict, calibration: dict,
                                  changes: list[dict], rejections: list[dict]) -> bool:
    protection = _strip_protection(plan, source, target, calibration)
    distance = abs(source["coordinate_m"] - target["coordinate_m"])
    common = {
        "floor_id": plan.get("floor_id"), "axis": source["axis"],
        "lines": [_line_public(source), _line_public(target)],
        "distance_m": round(distance, 9), "length_m": protection["length_m"],
    }
    partition = plan["partitions"][source["partition_index"]]
    if len(partition.get("points", [])) != 2:
        rejections.append({
            **common, "type": "ambiguous_partition_near_fixed_footprint",
            "contradiction_category": "partial_polyline_merge_cannot_preserve_fixed_outline_topology",
            "message": (
                f"Cannot safely remove {source['partition_id']} near fixed {target['partition_id']} "
                f"({distance:.3f} m apart over {protection['length_m']:.3f} m). Redraw the intended "
                "single boundary or keep a real space at least 0.60 m wide."
            ),
        })
        return False
    overlap_lo = max(source["span_m"][0], target["span_m"][0])
    overlap_hi = min(source["span_m"][1], target["span_m"][1])
    overlap_source = _clipped_segment(source, overlap_lo, overlap_hi)
    overlap_target = _clipped_segment(target, overlap_lo, overlap_hi)
    content_change_start = len(changes)
    if not _merge_strip_contents(plan, overlap_source, overlap_target, calibration,
                                 protection, changes, rejections):
        return False
    normal_index = 0 if source["axis"] == "x" else 1
    along_index = 1 - normal_index
    source_px = source["points_pixel"][0][normal_index]
    target_px = _pixel_coordinate(calibration, source["axis"], target["coordinate_m"])
    moved_endpoints = []
    collapsed_partitions = []
    for other in plan["partitions"]:
        if other is partition or not other.get("points"):
            continue
        for endpoint_index in (0, len(other["points"]) - 1):
            endpoint = other["points"][endpoint_index]
            endpoint_world = _world(calibration, endpoint)
            if (math.isclose(endpoint[normal_index], source_px, abs_tol=_EPS)
                    and overlap_lo - _EPS <= endpoint_world[along_index] <= overlap_hi + _EPS):
                points_before = copy.deepcopy(other["points"])
                coordinate_changed = not math.isclose(
                    endpoint[normal_index], target_px, abs_tol=_EPS)
                old_line_before = (
                    _axis_segment(points_before[0], points_before[1], calibration)
                    if len(points_before) == 2 else None
                )
                if not coordinate_changed:
                    continue
                endpoint[normal_index] = target_px
                moved_endpoints.append({"partition_id": other.get("id"), "endpoint": endpoint_index})
                if (len(other["points"]) == 2
                        and old_line_before is not None
                        and all(math.isclose(other["points"][0][axis_index],
                                             other["points"][1][axis_index],
                                             abs_tol=_EPS)
                                for axis_index in (0, 1))):
                    old_line = old_line_before
                    hosted_openings = [
                        opening.get("id") for opening in plan.get("openings", [])
                        if old_line is not None and _opening_on_segment(
                            opening,
                            {**old_line, "points_pixel": copy.deepcopy(points_before)},
                            calibration,
                        )
                    ]
                    if hosted_openings:
                        other["points"] = points_before
                        rejections.append({
                            **common,
                            "type": "footprint_merge_would_collapse_opening_host_wall",
                            "contradiction_category": "opening_host_cannot_collapse_to_point",
                            "partition_id": other.get("id"),
                            "opening_ids": hosted_openings,
                            "points_before": points_before,
                            "message": (
                                f"Merging {source['partition_id']} into the fixed footprint would "
                                f"collapse opening host {other.get('id')} to a point. Move openings "
                                "to an explicit surviving wall or correct the wall junction."
                            ),
                        })
                        return False
                    collapsed_partitions.append({
                        "partition": other,
                        "line_before": old_line,
                        "points_before": points_before,
                        "point_after": copy.deepcopy(other["points"][0]),
                    })
    remember_merge(plan, source, target)
    # Resolve the source remainder while its captured partition_index still
    # refers to the same object.  Removing a collapsed neighbour first can
    # shift list indexes and accidentally trim an unrelated partition.
    remainder_ids = _partition_remainders(plan, source, overlap_lo, overlap_hi, calibration)
    for row in collapsed_partitions:
        if row["partition"] in plan["partitions"]:
            plan["partitions"].remove(row["partition"])
        line = row["line_before"]
        changes.append({
            "type": "remove_collapsed_wall_step",
            "floor_id": plan.get("floor_id"),
            "axis": line["axis"] if line else source["axis"],
            "partition_id": row["partition"].get("id"),
            "object_ids": {
                "partitions": [row["partition"].get("id")], "openings": [],
            },
            "from_m": round(line["coordinate_m"], 9) if line else None,
            "to_m": round(line["coordinate_m"], 9) if line else None,
            "span_m": ([round(value, 9) for value in line["span_m"]]
                       if line else None),
            "points_before": row["points_before"],
            "point_after": row["point_after"],
            "movement_m": (round(line["span_m"][1] - line["span_m"][0], 9)
                           if line else 0.0),
            "basis": "fixed_exterior_footprint",
            "reason": "the endpoint retargeted by this footprint merge exactly collapsed a two-point connector with no hosted opening",
        })
    content_changes = changes[content_change_start:]
    affected_openings = list(dict.fromkeys(
        identity for row in content_changes
        for identity in row.get("object_ids", {}).get("openings", [])))
    changes.append({
        "type": "merge_duplicate_wall_into_fixed_footprint",
        "floor_id": plan.get("floor_id"), "axis": source["axis"],
        "partition_id": source["partition_id"],
        "object_ids": {"partitions": [source["partition_id"], target["partition_id"]],
                       "openings": affected_openings,
                       "space_seeds": protection["space_seed_ids"]},
        "from_m": round(source["coordinate_m"], 9),
        "to_m": round(target["coordinate_m"], 9),
        "span_m": [round(overlap_lo, 9), round(overlap_hi, 9)],
        "source": _line_public(source), "target": _line_public(target),
        "movement_m": round(distance, 9), "length_m": protection["length_m"],
        "removed_partition_id": source["partition_id"] if not remainder_ids else None,
        "retained_remainder_partition_ids": remainder_ids,
        "retained_partition_id": target["partition_id"],
        "adjusted_endpoint_ids": moved_endpoints,
        "basis": "fixed_exterior_footprint",
        "reason": "outer footprint stayed fixed; only the thickness-qualified duplicate overlap was removed and its contents were audited",
    })
    return True


def _close_small_steps(plan: dict, calibration: dict, line_refs: dict[str, list[dict]],
                       coordinate_refs: list[dict], changes: list[dict]) -> bool:
    for partition_index, partition in enumerate(plan.get("partitions", [])):
        points = partition.get("points", [])
        for index in range(len(points) - 3):
            first = _axis_segment(points[index], points[index + 1], calibration)
            connector = _axis_segment(points[index + 1], points[index + 2], calibration)
            second = _axis_segment(points[index + 2], points[index + 3], calibration)
            if (not first or not connector or not second or first["axis"] != second["axis"]
                    or connector["axis"] == first["axis"]):
                continue
            distance = abs(first["coordinate_m"] - second["coordinate_m"])
            if not _strictly_under(distance, ALIGNMENT_THRESHOLD_M):
                continue
            a = {**first, "partition_id": partition.get("id"), "partition_index": partition_index,
                 "segment_index": index, "points_pixel": [points[index], points[index + 1]],
                 "length_m": first["span_m"][1] - first["span_m"][0]}
            b = {**second, "partition_id": partition.get("id"), "partition_index": partition_index,
                 "segment_index": index + 2, "points_pixel": [points[index + 2], points[index + 3]],
                 "length_m": second["span_m"][1] - second["span_m"][0]}
            target, source = (a, b) if _priority(a, line_refs, coordinate_refs) >= _priority(b, line_refs, coordinate_refs) else (b, a)
            _move_segment(plan, source, target["coordinate_m"], calibration,
                          reason="collapse a sub-0.30 m wall step to an existing parallel leg",
                          changes=changes)
            return True
    return False


def _align_head_to_tail(plan: dict, calibration: dict, line_refs: dict[str, list[dict]],
                        coordinate_refs: list[dict], changes: list[dict],
                        *, locked_coordinates: set[tuple[str, float]] | None = None) -> bool:
    segments = [*_segments(plan, calibration), *_footprint_segments(plan, calibration)]
    candidates = []
    for index, first in enumerate(segments):
        for second in segments[index + 1:]:
            if (first["partition_id"] == second["partition_id"]
                    or first["axis"] != second["axis"]
                    or (first.get("fixed_exterior") and second.get("fixed_exterior"))):
                continue
            distance = abs(first["coordinate_m"] - second["coordinate_m"])
            if not within_wall_tolerance(first, second):
                continue
            if _overlap(first, second) > _EPS or _span_gap(first, second) > _EPS:
                continue
            candidates.append((distance, first, second))
    if not candidates:
        return False
    _, first, second = min(candidates, key=lambda row: (row[0], row[1]["partition_id"], row[2]["partition_id"]))
    locked_coordinates = locked_coordinates or set()
    first_locked = any(
        axis == first["axis"]
        and math.isclose(value, first["coordinate_m"], abs_tol=1e-7)
        for axis, value in locked_coordinates
    )
    second_locked = any(
        axis == second["axis"]
        and math.isclose(value, second["coordinate_m"], abs_tol=1e-7)
        for axis, value in locked_coordinates
    )
    if first.get("fixed_exterior") or second.get("fixed_exterior"):
        # The footprint is immutable even when an internal line has stronger
        # dimension/ink evidence.  Evidence chooses among movable lines; it
        # cannot turn a fixed outline segment into the move source.
        target, source = (first, second) if first.get("fixed_exterior") else (second, first)
    elif first_locked != second_locked:
        # A cross-storey move selects one coordinate for the entire connected
        # collinear chain.  As adjacent pieces settle, the old coordinate may
        # temporarily have more/longer pieces; it must not pull an already
        # aligned piece back and create a two-state endpoint cycle.
        target, source = (first, second) if first_locked else (second, first)
    else:
        p_first = _priority(first, line_refs, coordinate_refs)
        p_second = _priority(second, line_refs, coordinate_refs)
        target, source = (first, second) if p_first >= p_second else (second, first)
    _move_segment(plan, source, target["coordinate_m"], calibration,
                  reason="align touching collinear wall ends to an existing line",
                  changes=changes,
                  basis=("fixed_exterior_footprint" if target.get("fixed_exterior")
                         else "locked_cross_storey_target_coordinate"
                         if first_locked != second_locked else "existing_line"))
    remember_merge(plan, source, target, keep_source=True)
    return True


def _attach_suspended_endpoints(plan: dict, calibration: dict, changes: list[dict]) -> bool:
    segments = [*_segments(plan, calibration), *_footprint_segments(plan, calibration)]
    candidates = []
    for partition_index, partition in enumerate(plan.get("partitions", [])):
        points = partition.get("points", [])
        if len(points) < 2:
            continue
        for endpoint_index, neighbor_index in ((0, 1), (len(points) - 1, len(points) - 2)):
            endpoint, neighbor = points[endpoint_index], points[neighbor_index]
            own = _axis_segment(endpoint, neighbor, calibration)
            if own is None:
                continue
            world_endpoint = _world(calibration, endpoint)
            eligible = []
            for wall in segments:
                if wall["partition_index"] == partition_index or wall["axis"] == own["axis"]:
                    continue
                coordinate_index = 0 if wall["axis"] == "x" else 1
                along_index = 1 - coordinate_index
                distance = abs(world_endpoint[coordinate_index] - wall["coordinate_m"])
                if not wall["span_m"][0] - _EPS <= world_endpoint[along_index] <= wall["span_m"][1] + _EPS:
                    continue
                eligible.append((distance, wall, coordinate_index, along_index))
            # Once an endpoint is exactly attached to a perpendicular existing
            # line, do not detach it to chase another nearby line.  This makes
            # the operation monotonic and prevents A<->B oscillation.
            if any(distance <= _EPS for distance, *_ in eligible):
                continue
            for distance, wall, coordinate_index, along_index in eligible:
                if not _strictly_under(distance, ALIGNMENT_THRESHOLD_M):
                    continue
                candidates.append((distance, partition_index, endpoint_index, wall, world_endpoint))
    if not candidates:
        return False
    distance, partition_index, endpoint_index, wall, world_endpoint = min(
        candidates, key=lambda row: (row[0], row[1], row[2], row[3]["partition_id"])
    )
    partition = plan["partitions"][partition_index]
    axis_index = 0 if wall["axis"] == "x" else 1
    before = list(partition["points"][endpoint_index])
    partition["points"][endpoint_index][axis_index] = _pixel_coordinate(
        calibration, wall["axis"], wall["coordinate_m"]
    )
    changes.append({
        "type": "attach_suspended_endpoint",
        "floor_id": plan.get("floor_id"), "axis": wall["axis"],
        "partition_id": partition.get("id"), "endpoint_index": endpoint_index,
        "object_ids": {"partitions": [partition.get("id"), wall["partition_id"]],
                       "openings": []},
        "from_m": round(world_endpoint[axis_index], 9),
        "to_m": round(wall["coordinate_m"], 9),
        "span_m": [round(world_endpoint[1 - axis_index], 9)] * 2,
        "from_pixel": before, "to_pixel": list(partition["points"][endpoint_index]),
        "target": _line_public(wall), "movement_m": round(distance, 9),
        "basis": "perpendicular_existing_wall_line",
        "reason": "extend or retract a suspended endpoint to a perpendicular existing wall line",
    })
    return True


def _settle_endpoints(plan: dict, calibration: dict, line_refs: dict[str, list[dict]],
                      coordinate_refs: list[dict], changes: list[dict],
                      rejections: list[dict], *, phase: str,
                      locked_coordinates: set[tuple[str, float]] | None = None) -> bool:
    """Finish collinear joins/end contacts with a finite, cycle-reported loop."""
    seen: dict[str, int] = {}
    partition_ids = [row.get("id") for row in plan.get("partitions", [])]
    endpoint_count = sum(2 for row in plan.get("partitions", []) if len(row.get("points", [])) >= 2)
    limit = max(16, endpoint_count * 4 + len(plan.get("partitions", [])) * 2)
    for iteration in range(limit):
        state = _plan_digest(plan)
        if state in seen:
            rejections.append({
                "type": "endpoint_regularization_cycle", "floor_id": plan.get("floor_id"),
                "phase": phase, "first_iteration": seen[state], "repeat_iteration": iteration,
                "cycle_length": iteration - seen[state],
                "object_ids": {"partitions": partition_ids, "openings": []},
                "endpoint_count": endpoint_count,
                "alignment_threshold_m": ALIGNMENT_THRESHOLD_M,
                "message": "Endpoint regularisation repeated an earlier plan state. No cyclic result may be saved; review the nearby perpendicular wall choices.",
            })
            return False
        seen[state] = iteration
        changed = _align_head_to_tail(
            plan, calibration, line_refs, coordinate_refs, changes,
            locked_coordinates=locked_coordinates,
        )
        changed = _attach_suspended_endpoints(plan, calibration, changes) or changed
        if not changed:
            return True
    rejections.append({
        "type": "endpoint_regularization_limit", "floor_id": plan.get("floor_id"),
        "phase": phase, "iteration_limit": limit,
        "object_ids": {"partitions": partition_ids, "openings": []},
        "endpoint_count": endpoint_count,
        "alignment_threshold_m": ALIGNMENT_THRESHOLD_M,
        "message": "Endpoint regularisation did not converge within the finite object-based limit. No partial result may be saved; review the reported nearby walls.",
    })
    return False


def _space_scan_width(polygon: Polygon) -> tuple[float, dict]:
    """Return a conservative orthogonal local width, including necks and tongues."""
    if polygon.is_empty or not polygon.is_valid:
        return 0.0, {"axis": "invalid", "coordinate_m": None}
    best = (math.inf, {"axis": "unknown", "coordinate_m": None})
    xs = sorted({float(x) for x, _ in polygon.exterior.coords})
    ys = sorted({float(y) for _, y in polygon.exterior.coords})
    for axis, values, bounds in (("x", xs, polygon.bounds[1::2]), ("y", ys, polygon.bounds[0::2])):
        samples = [(a + b) / 2 for a, b in zip(values, values[1:]) if b - a > _EPS]
        for value in samples:
            if axis == "x":
                probe = LineString([(value, polygon.bounds[1] - 1), (value, polygon.bounds[3] + 1)])
            else:
                probe = LineString([(polygon.bounds[0] - 1, value), (polygon.bounds[2] + 1, value)])
            intersection = polygon.intersection(probe)
            lines = [intersection] if intersection.geom_type == "LineString" else [
                part for part in getattr(intersection, "geoms", []) if part.geom_type == "LineString"
            ]
            for line in lines:
                if _EPS < line.length < best[0]:
                    best = (line.length, {"axis": axis, "coordinate_m": round(value, 9)})
    if math.isinf(best[0]):
        return 0.0, {"axis": "unmeasurable", "coordinate_m": None}
    return best


def minimum_orthogonal_space_width(polygon: Polygon) -> tuple[float, dict]:
    """Public conservative minimum-width probe for orthogonal source spaces."""
    return _space_scan_width(polygon)


def _plan_polygons(plan: dict, calibration: dict) -> list[Polygon]:
    footprint = Polygon([_world(calibration, point) for point in plan["footprint_pixels"]])
    lines = [footprint.boundary]
    for partition in plan.get("partitions", []):
        lines.append(LineString([_world(calibration, point) for point in partition["points"]]))
    return [polygon for polygon in polygonize(unary_union(lines)) if footprint.covers(polygon.representative_point())]


def validate_regularized_plan(plan: dict, *, image_size: tuple[int, int], image_name: str = "plan") -> dict:
    """Return post-regularisation hard-rule violations for one plan draft."""
    del image_name  # Reserved for stable API symmetry and future evidence links.
    calibration = _calibration(plan, image_size)
    violations = []
    segments = [*_segments(plan, calibration), *_footprint_segments(plan, calibration)]
    for index, first in enumerate(segments):
        for second in segments[index + 1:]:
            if first["partition_id"] == second["partition_id"] or first["axis"] != second["axis"]:
                continue
            distance = abs(first["coordinate_m"] - second["coordinate_m"])
            overlap = _overlap(first, second)
            if within_wall_tolerance(first, second) and overlap > _EPS:
                violations.append({
                    "type": "parallel_wall_lines_under_0_30m",
                    "objects": [_line_public(first), _line_public(second)],
                    "distance_m": round(distance, 9), "overlap_m": round(overlap, 9),
                    "fix": "Draw one wall line, or confirm two walls and move them at least 0.30 m apart.",
                })
            elif (within_wall_tolerance(first, second)
                  and overlap <= _EPS and _span_gap(first, second) <= _EPS):
                violations.append({
                    "type": "small_wall_step_under_0_30m",
                    "objects": [_line_public(first), _line_public(second)],
                    "distance_m": round(distance, 9),
                    "touch_coordinate_m": round(
                        max(first["span_m"][0], second["span_m"][0]), 9),
                    "fix": "Align the touching parallel legs to one existing line or make the step at least 0.30 m.",
                })
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        for index in range(len(points) - 3):
            first = _axis_segment(points[index], points[index + 1], calibration)
            connector = _axis_segment(points[index + 1], points[index + 2], calibration)
            second = _axis_segment(points[index + 2], points[index + 3], calibration)
            if first and connector and second and first["axis"] == second["axis"] != connector["axis"]:
                distance = abs(first["coordinate_m"] - second["coordinate_m"])
                if _strictly_under(distance, ALIGNMENT_THRESHOLD_M):
                    violations.append({
                        "type": "small_wall_step_under_0_30m", "partition_id": partition.get("id"),
                        "distance_m": round(distance, 9),
                        "fix": "Align the two parallel legs to one existing line or make the step at least 0.30 m.",
                    })
    try:
        polygons = _plan_polygons(plan, calibration)
    except Exception as exc:  # strict compiler will provide the detailed topology error too
        polygons = []
        violations.append({"type": "unpolygonizable_plan", "error": str(exc),
                           "fix": "Repair the named wall endpoints without changing room or opening relationships."})
    seen_steps = set()
    for space_index, polygon in enumerate(polygons):
        ring = list(polygon.exterior.coords)[:-1]

        def world_axis_line(first, second):
            if math.isclose(first[0], second[0], abs_tol=_EPS) and not math.isclose(
                    first[1], second[1], abs_tol=_EPS):
                return ("x", first[0], *sorted((first[1], second[1])))
            if math.isclose(first[1], second[1], abs_tol=_EPS) and not math.isclose(
                    first[0], second[0], abs_tol=_EPS):
                return ("y", first[1], *sorted((first[0], second[0])))
            return None

        for index in range(len(ring)):
            p, q, r, s = [ring[position % len(ring)]
                          for position in (index - 1, index, index + 1, index + 2)]
            first = world_axis_line(p, q)
            connector = world_axis_line(q, r)
            second = world_axis_line(r, s)
            if (not first or not connector or not second
                    or first[0] != second[0] or first[0] == connector[0]):
                continue
            distance = abs(first[1] - second[1])
            along = 1 if first[0] == "x" else 0
            if (not _strictly_under(distance, ALIGNMENT_THRESHOLD_M)
                    or (q[along] - p[along]) * (s[along] - r[along]) <= 0):
                continue
            key = tuple(sorted((tuple(round(value, 9) for value in q),
                                tuple(round(value, 9) for value in r))))
            if key in seen_steps:
                continue
            seen_steps.add(key)
            violations.append({
                "type": "small_wall_step_under_0_30m", "space_index": space_index,
                "axis": first[0],
                "endpoints_m": [[round(value, 9) for value in q],
                                [round(value, 9) for value in r]],
                "distance_m": round(distance, 9),
                "fix": "Align the two parallel legs to one existing line or make the step at least 0.30 m.",
            })
    seeds = plan.get("space_seeds", [])
    for index, polygon in enumerate(polygons):
        width, location = _space_scan_width(polygon)
        if width < MIN_SPACE_WIDTH_M - _EPS:
            seed_ids = [seed.get("id") for seed in seeds if polygon.contains(
                Point(_world(calibration, seed.get("point", [math.inf, math.inf]))))]
            violations.append({
                "type": "space_width_under_0_60m", "space_index": index,
                "space_seed_ids": seed_ids, "minimum_width_m": round(width, 9),
                "location": location,
                "fix": "Move the responsible existing boundary so every part of the space is at least 0.60 m wide; do not silently delete a named space.",
            })
    return {
        "schema": "plan_hard_constraints_v1", "rule_version": RULE_VERSION,
        "status": "pass" if not violations else "rejected",
        "thresholds": {"alignment_strictly_less_than_m": ALIGNMENT_THRESHOLD_M,
                       "minimum_space_width_m": MIN_SPACE_WIDTH_M},
        "violations": violations,
        "coverage": {"wall_segments": len(segments), "spaces": len(polygons),
                     "width_method": "orthogonal scan strips; conservative for necks, tongues and steps"},
    }


def enforce_regularized_plan(plan: dict, *, image_size: tuple[int, int], image_name: str = "plan") -> dict:
    report = validate_regularized_plan(plan, image_size=image_size, image_name=image_name)
    if report["violations"]:
        first = report["violations"][0]
        raise PlanRegularizationError(
            f"Plan hard constraints rejected {first['type']}: {first.get('fix', 'review the reported object and dimensions')}",
            report=report,
        )
    return report


def _compile(plan: dict, image_size: tuple[int, int], image_name: str) -> tuple[dict | None, str | None]:
    from src.agent.geometry.plan_partition import compile_plan_partition
    try:
        proposal, _ = compile_plan_partition(plan, image_size=image_size, image_name=image_name,
                                             include_audit_metadata=False)
        return proposal, None
    except Exception as exc:
        return None, str(exc)


def _relationship_signature(proposal: dict, *, named_space_ids: set[str] | None = None) -> dict:
    geometry = proposal["geometry"]
    openings = []
    hosts = []
    named_hosts = []
    named_space_ids = named_space_ids or set()
    for row in [*geometry.get("windows", []), *geometry.get("openings", [])]:
        openings.append((row.get("id"), row.get("kind"), row.get("other_space_id") is None,
                         1 + int(row.get("other_space_id") is not None)))
        identities = ([row.get("room")] if row.get("kind") == "window" else
                      [row.get("space_id"), row.get("other_space_id")])
        identities = sorted(value for value in identities if value is not None)
        hosts.append((row.get("id"), tuple(identities)))
        named_hosts.append((row.get("id"), tuple(value for value in identities if value in named_space_ids)))
    space_ids = sorted(
        cell.get("id") for floor in geometry.get("floors", [])
        for cell in floor.get("cells", []) if cell.get("id") is not None)
    return {"opening_relations": sorted(openings),
            "opening_hosts": sorted(hosts), "named_opening_hosts": sorted(named_hosts),
            "space_ids": space_ids,
            "opening_host_map": {identity: list(values) for identity, values in hosts},
            "opening_connection_map": {
                row[0]: {"kind": row[1], "is_exterior": row[2], "connected_space_count": row[3]}
                for row in openings
            },
            "opening_count": len(openings),
            "space_count": sum(len(floor.get("cells", [])) for floor in geometry.get("floors", []))}


def _narrow_strip_semantic_mapping(before: dict, after: dict, changes: list[dict]) -> dict:
    opening_map = {}
    affected = set()
    removed_seeds = []
    for change in changes:
        if change.get("type") == "merge_overlapping_openings":
            retained = change["retained_opening_id"]
            opening_map[retained] = retained
            affected.add(retained)
            for removed in change["removed_opening_ids"]:
                opening_map[removed] = retained
                affected.add(removed)
        elif change.get("type") in {
                "move_openings_to_merged_wall", "retain_openings_on_merged_wall"}:
            for identity in change.get("object_ids", {}).get("openings", []):
                opening_map.setdefault(identity, identity)
                affected.add(identity)
        elif change.get("type") == "remove_narrow_strip_space_seeds":
            removed_seeds.extend(copy.deepcopy(change.get("removed_space_seeds", [])))

    def final_survivor(identity):
        current = identity
        path = []
        positions = {}
        while True:
            if current in positions:
                cycle_values = [*path[positions[current]:], current]
                cycle = " -> ".join(map(str, cycle_values))
                raise RuntimeError(f"opening survivor mapping cycle: {cycle}")
            positions[current] = len(path)
            path.append(current)
            successor = opening_map.get(current, current)
            if successor == current:
                return current
            current = successor

    opening_map = {identity: final_survivor(identity) for identity in opening_map}
    before_hosts = before.get("opening_host_map", {})
    after_hosts = after.get("opening_host_map", {})
    before_connections = before.get("opening_connection_map", {})
    after_connections = after.get("opening_connection_map", {})
    rows = []
    for old_id, new_id in sorted(opening_map.items(), key=lambda row: str(row[0])):
        rows.append({
            "old_opening_id": old_id, "surviving_opening_id": new_id,
            "before_hosts": before_hosts.get(old_id, []),
            "after_hosts": after_hosts.get(new_id, []),
            "before_connection": before_connections.get(old_id),
            "after_connection": after_connections.get(new_id),
        })
    return {"opening_id_map": rows, "removed_space_seeds": removed_seeds,
            "affected_opening_ids": sorted(affected, key=str)}


def _eliminated_strip_spaces(plan: dict, calibration: dict,
                             changes: list[dict]) -> list[dict]:
    rectangles = []
    for change in changes:
        if change.get("type") not in {
                "merge_duplicate_wall_lines", "merge_duplicate_wall_into_fixed_footprint"}:
            continue
        lo, hi = sorted((float(change["from_m"]), float(change["to_m"])))
        span_lo, span_hi = change["span_m"]
        if change["axis"] == "x":
            polygon = Polygon([(lo, span_lo), (hi, span_lo), (hi, span_hi), (lo, span_hi)])
        else:
            polygon = Polygon([(span_lo, lo), (span_hi, lo), (span_hi, hi), (span_lo, hi)])
        rectangles.append((change, polygon))
    if not rectangles:
        return []
    strip_parts = [polygon for _, polygon in rectangles]
    for first_index, (first_change, first) in enumerate(rectangles):
        for second_change, second in rectangles[first_index + 1:]:
            if first_change["axis"] == second_change["axis"] or first.distance(second) > _EPS:
                continue
            vertical = first if first_change["axis"] == "x" else second
            horizontal = second if first_change["axis"] == "x" else first
            x_lo, _, x_hi, _ = vertical.bounds
            _, y_lo, _, y_hi = horizontal.bounds
            if (_strictly_under(x_hi - x_lo, ALIGNMENT_THRESHOLD_M)
                    and _strictly_under(y_hi - y_lo, ALIGNMENT_THRESHOLD_M)):
                strip_parts.append(Polygon([
                    (x_lo, y_lo), (x_hi, y_lo), (x_hi, y_hi), (x_lo, y_hi)
                ]))
    strip_union = unary_union(strip_parts)
    eliminated = []
    for index, polygon in enumerate(_plan_polygons(plan, calibration)):
        if (not strip_union.buffer(_EPS).covers(polygon)
                or polygon.area > strip_union.area + _EPS):
            continue
        involved = [change for change, strip in rectangles
                    if strip.buffer(_EPS).intersects(polygon)
                    and strip.intersection(polygon).area > _EPS]
        eliminated.append({
            "before_space_index": index,
            "area_m2": round(polygon.area, 9),
            "bounds_m": [round(value, 9) for value in polygon.bounds],
            "wall_partition_ids": list(dict.fromkeys(
                identity for change in involved
                for identity in change["object_ids"]["partitions"])),
            "removed_space_seed_ids": list(dict.fromkeys(
                identity for change in involved
                for identity in change["object_ids"].get("space_seeds", []))),
        })
    return eliminated


def _junction_lines(plan: dict) -> list[dict]:
    """Pixel lines referencing the actual mutable endpoint lists."""
    rows = []
    ring = plan["footprint_pixels"]
    groups = [(f"footprint[{i}]", None, [a, b])
              for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1]))]
    groups += [(row["id"], row, row["points"]) for row in plan["partitions"]]
    for identity, partition, points in groups:
        for index, (a, b) in enumerate(zip(points, points[1:])):
            if a == b:
                continue
            axis = 0 if a[0] == b[0] else 1 if a[1] == b[1] else None
            if axis is not None:
                rows.append(dict(id=identity, partition=partition, index=index, a=a, b=b,
                                 axis=axis, at=a[axis], span=sorted((a[1-axis], b[1-axis]))))
    return rows


def _junction_cleanup(plan: dict, changes: list[dict]) -> None:
    """Remove only zero-length and exactly covered linework, never a wall gap."""
    from shapely.ops import linemerge

    covered = Polygon(plan["footprint_pixels"]).boundary
    footprint_edges = [row for row in _junction_lines(plan) if row["partition"] is None]
    kept, replacements = [], {}
    used_ids = {row["id"] for row in plan["partitions"]}
    for row in plan["partitions"]:
        points = row["points"]
        line = unary_union([LineString([a, b]) for a, b in zip(points, points[1:]) if a != b])
        remaining = line.difference(covered)
        if remaining.geom_type == "MultiLineString":
            remaining = linemerge(remaining)
        parts = ([remaining] if remaining.geom_type == "LineString" else
                 list(getattr(remaining, "geoms", [])))
        parts = [p for p in parts if p.geom_type == "LineString" and p.length > 0]
        unchanged = (all(a != b for a, b in zip(points, points[1:]))
                     and LineString(points).is_simple and line.equals(remaining))
        if unchanged:
            kept.append(row)
            covered = unary_union([covered, line])
            continue
        reference_targets = []
        for retained in kept:
            if line.intersection(LineString(retained["points"])).length > 0:
                reference_targets.append(retained["id"])
                retained["source_refs"] = list(dict.fromkeys(
                    [*retained["source_refs"], *row["source_refs"]]))
        reference_targets.extend(
            edge["id"] for edge in footprint_edges
            if line.intersection(LineString([edge["a"], edge["b"]])).length > 0)
        survivors = []
        for index, part in enumerate(sorted(parts, key=lambda p: (p.bounds, p.wkt))):
            identity = row["id"]
            if index:
                suffix = index + 1
                identity = f"{row['id']}__junction_{suffix}"
                while identity in used_ids:
                    suffix += 1
                    identity = f"{row['id']}__junction_{suffix}"
                used_ids.add(identity)
            item = copy.deepcopy(row)
            item.update(id=identity, points=[list(p) for p in part.coords])
            kept.append(item)
            survivors.append(identity)
        replacements[row["id"]] = list(dict.fromkeys([*survivors, *reference_targets]))
        retain_remainder_metadata(plan, row['id'], survivors)
        changes.append(dict(type="remove_redundant_partition_segments", partition_id=row["id"],
                            before=copy.deepcopy(points),
                            after=[copy.deepcopy(p["points"]) for p in kept if p["id"] in survivors],
                            surviving_partition_ids=survivors, source_refs=copy.deepcopy(row["source_refs"]),
                            reference_survivor_ids=replacements[row["id"]],
                            movement_m=0.0, reason="zero-length segments or exact overlap with existing walls/footprint"))
        covered = unary_union([covered, line])
    plan["partitions"] = kept
    inputs = plan.get("regularization_inputs", {})
    if replacements and isinstance(inputs, dict) and "line_references" in inputs:
        inputs["line_references"] = [
            {**row, "partition_id": identity}
            for row in inputs["line_references"]
            for identity in replacements.get(row["partition_id"], [row["partition_id"]])]


def prepare_plan_junctions(plan: dict, *, image_size: tuple[int, int],
                           image_name: str = "plan") -> tuple[dict, dict]:
    """Repair small pixel joins before literal compilation, or roll back.

    Per-axis tolerance is max(5 cm, three pixels), capped at 30 cm. No seed or
    opening is removed and no wall inferred. Failure or changed relationships
    in a previously valid draft rolls back all edits; Q1 then runs separately.
    """
    from src.agent.geometry.plan_input import normalize_plan_fields
    from src.agent.geometry.plan_partition import (
        _OPENING_FIELDS, _PARTITION_FIELDS, _SEED_FIELDS, _fields, _point,
        _required_opening_fields, _required_partition_fields, _strings, _text,
    )

    original, _ = normalize_plan_fields(plan)
    result = copy.deepcopy(original)
    calibration = _calibration(result, image_size)
    _regularization_inputs(result)  # Validate evidence before cleanup can remap it.
    scales = [abs(calibration[axis]["slope"]) for axis in ("x", "y")]
    tolerances = [min(ALIGNMENT_THRESHOLD_M, max(0.05, 3 * scale)) / scale for scale in scales]
    before, before_error = _compile(original, image_size, image_name)
    changes: list[dict] = []
    for key in ("partitions", "openings", "space_seeds"):
        if not isinstance(result.get(key, []), list):
            raise TypeError(f"plan.{key} must be a list")
    for index, row in enumerate(result["openings"]):
        _fields(row, path=f"plan.openings[{index}]", allowed=_OPENING_FIELDS,
                required=_required_opening_fields(result))
        if result.get("reading_type") == "plan" and "source_refs" not in row:
            identity = _text(row["id"], path=f"plan.openings[{index}].id")
            row["source_refs"] = [f"image:{image_name}#{identity}"]
        _strings(row["source_refs"], path=f"opening {row['id']}.source_refs", require_one=True)
    for index, row in enumerate(result.get("space_seeds", [])):
        _fields(row, path=f"plan.space_seeds[{index}]", allowed=_SEED_FIELDS,
                required=frozenset({"id", "point"}))
    # Validate data which could disappear during deduplication.
    seen = set()
    for index, row in enumerate(result["partitions"]):
        _fields(row, path=f"plan.partitions[{index}]", allowed=_PARTITION_FIELDS,
                required=_required_partition_fields(result))
        identity = _text(row["id"], path=f"plan.partitions[{index}].id")
        if identity in seen:
            raise ValueError(f"duplicate partition id {identity!r}")
        seen.add(identity)
        if result.get("reading_type") == "plan" and "source_refs" not in row:
            row["source_refs"] = [f"image:{image_name}#{identity}"]
        _strings(row["source_refs"], path=f"partition {identity}.source_refs", require_one=True)
        if not isinstance(row["points"], list) or len(row["points"]) < 2:
            raise ValueError(f"partition {identity}: points must contain at least two points")
    positions = [("footprint_pixels", result["footprint_pixels"])]
    positions += [(f"partition {r['id']}", r["points"]) for r in result["partitions"]]
    positions += [(f"opening {r.get('id')}", [r["p1"], r["p2"]]) for r in result["openings"]]
    positions += [(f"space seed {r.get('id')}", [r["point"]]) for r in result.get("space_seeds", [])]
    precision_changes = []
    for identity, points in positions:
        for index, point in enumerate(points):
            _point(point, path=f"{identity}.points[{index}]", width=image_size[0], height=image_size[1])
            rounded = [round(v, PIXEL_DECIMALS) for v in point]
            if rounded != point:
                precision_changes.append(dict(
                    object=identity, point_index=index, before=list(point), after=rounded,
                    movement_m=math.hypot(*[(a-b)*s for a,b,s in zip(point,rounded,scales)])))
                point[:] = rounded
    if precision_changes:
        changes.append(dict(type="pixel_coordinate_precision", points=precision_changes,
                            movement_m=max(row["movement_m"] for row in precision_changes)))
    supported = all(a == b or a[0] == b[0] or a[1] == b[1]
                    for _, points in positions[:1+len(result["partitions"])]
                    for a, b in zip(points, points[1:]))
    footprint = Polygon(result["footprint_pixels"])
    if supported and footprint.is_valid and footprint.area > 0:
        # Fixed outline: move a duplicate and attached endpoints atomically.
        for _ in range(sum(len(r["points"]) for r in result["partitions"]) + 1):
            lines = _junction_lines(result)
            pair = None
            for wall in lines:
                if wall["partition"] is None:
                    continue
                targets = [e for e in lines if e["partition"] is None and e["axis"] == wall["axis"]
                           and 0 < abs(e["at"]-wall["at"]) <= tolerances[wall["axis"]]
                           and e["span"][0] <= wall["span"][0] <= wall["span"][1] <= e["span"][1]]
                if targets:
                    nearest = min(targets, key=lambda e: abs(e["at"]-wall["at"]))
                    tied = [e for e in targets if math.isclose(
                        abs(e["at"]-wall["at"]), abs(nearest["at"]-wall["at"]), abs_tol=1e-9)]
                    if len({edge["at"] for edge in tied}) == 1:
                        pair = wall, nearest
                        break
            if pair is None:
                break
            wall, edge = pair
            axis, along, old = wall["axis"], 1-wall["axis"], wall["at"]
            attached = []
            for identity, points in positions[1:1+len(result["partitions"])]:
                for index, point in enumerate(points):
                    if point[axis] == old and wall["span"][0] <= point[along] <= wall["span"][1]:
                        attached.append(dict(object=identity, point_index=index, before=list(point)))
                        point[axis] = edge["at"]
            for opening in result["openings"]:
                if all(opening[k][axis] == old and wall["span"][0] <= opening[k][along] <= wall["span"][1] for k in ("p1","p2")):
                    for key in ("p1", "p2"):
                        attached.append(dict(object=f"opening {opening['id']}", point=key, before=list(opening[key])))
                        opening[key][axis] = edge["at"]
            changes.append(dict(type="join_partition_to_footprint", partition_id=wall["id"],
                                target=edge["id"], from_pixel=old, to_pixel=edge["at"],
                                movement_m=abs(old-edge["at"])*scales[axis], attached=attached))
        _junction_cleanup(result, changes)
        # Disconnected terminals move only along their own wall. Two short
        # terminals may extend together to their existing lines' intersection.
        for _ in range(max(1, 4*sum(len(r["points"]) for r in result["partitions"]))):
            lines = _junction_lines(result)
            candidates = []
            for row in result["partitions"]:
                for index, neighbor_index in ((0,1),(len(row["points"])-1,len(row["points"])-2)):
                    point, neighbor = row["points"][index], row["points"][neighbor_index]
                    own_axis = 0 if point[0] == neighbor[0] else 1 if point[1] == neighbor[1] else None
                    if own_axis is None or point == neighbor:
                        continue
                    axis = 1-own_axis
                    if any(w["partition"] is not row and point[w["axis"]] == w["at"]
                           and w["span"][0] <= point[1-w["axis"]] <= w["span"][1] for w in lines):
                        continue
                    others = [w for w in lines if w["partition"] is not row and w["axis"] == axis]
                    for wall in others:
                        distance = abs(point[axis]-wall["at"])
                        if (distance > tolerances[axis]
                                or (wall["at"] - neighbor[axis]) * (point[axis] - neighbor[axis]) <= 0):
                            continue
                        reach = max(wall["span"][0]-point[own_axis],point[own_axis]-wall["span"][1],0)
                        if reach > tolerances[own_axis] or (reach and wall["partition"] is None):
                            continue
                        other_endpoint = None
                        if reach:
                            other_endpoint = min((wall["a"],wall["b"]),key=lambda p:abs(p[own_axis]-point[own_axis]))
                            other_points = wall["partition"]["points"]
                            if not (other_endpoint is other_points[0] or other_endpoint is other_points[-1]):
                                continue
                            if any(w["partition"] is not wall["partition"] and w["axis"] == own_axis
                                   and other_endpoint[own_axis] == w["at"]
                                   and w["span"][0] <= other_endpoint[axis] <= w["span"][1] for w in lines):
                                continue
                        movement = math.hypot(distance*scales[axis], reach*scales[own_axis])
                        if 0 < movement <= ALIGNMENT_THRESHOLD_M:
                            candidates.append((movement,
                                               row["id"],index,point,axis,wall,other_endpoint))
            # Equal-distance alternatives on different target coordinates are
            # ambiguous. Keep that endpoint for an explicit reader correction.
            by_endpoint = defaultdict(list)
            for candidate in candidates:
                by_endpoint[candidate[1:3]].append(candidate)
            candidates = []
            for options in by_endpoint.values():
                nearest = min(options, key=lambda c: c[0])
                tied = [c for c in options if math.isclose(c[0], nearest[0], abs_tol=1e-9)]
                if len({(c[5]["at"], tuple(c[6]) if c[6] is not None else None)
                        for c in tied}) == 1:
                    candidates.append(nearest)
            if not candidates:
                break
            distance,identity,index,point,axis,wall,other_endpoint = min(candidates,key=lambda c:c[:3])
            old = list(point)
            other_before = list(other_endpoint) if other_endpoint is not None else None
            point[axis] = wall["at"]
            if other_endpoint is not None:
                other_endpoint[1-axis] = point[1-axis]
            changes.append(dict(type="join_near_endpoint",partition_id=identity,endpoint_index=index,
                                before=old,after=list(point),target=wall["id"],target_endpoint_before=other_before,
                                target_endpoint_after=list(other_endpoint) if other_endpoint is not None else None,
                                movement_m=distance))
        _junction_cleanup(result, changes)
        # Project a whole opening normally onto one fully covering host line.
        # Do not shorten it or bridge a missing piece of wall.
        lines = _junction_lines(result)
        for opening in result["openings"]:
            a, b = opening["p1"], opening["p2"]
            axis = 0 if a[0] == b[0] else 1 if a[1] == b[1] else None
            if axis is None or a == b:
                continue
            candidates = []
            for at in {w["at"] for w in lines if w["axis"] == axis}:
                if abs(at-a[axis]) > tolerances[axis]:
                    continue
                hosts = [w for w in lines if w["axis"] == axis and w["at"] == at]
                projected = [list(a), list(b)]
                for p in projected:
                    p[axis] = at
                if unary_union([LineString([w["a"],w["b"]]) for w in hosts]).covers(LineString(projected)):
                    candidates.append((abs(at-a[axis]),at,projected))
            candidates.sort()
            if not candidates or candidates[0][0] == 0:
                continue
            if len(candidates)>1 and math.isclose(candidates[0][0],candidates[1][0],abs_tol=1e-6):
                continue
            distance, at, projected = candidates[0]
            changes.append(dict(type="project_opening_to_host",opening_id=opening["id"],
                                before=[list(a),list(b)],after=copy.deepcopy(projected),movement_m=distance*scales[axis]))
            a[:], b[:] = projected
    after, after_error = _compile(result, image_size, image_name)
    rejection = None
    if before is not None and after is not None:
        named = {r["id"] for r in original.get("space_seeds", [])}
        if _relationship_signature(before,named_space_ids=named) != _relationship_signature(after,named_space_ids=named):
            rejection = "Junction preparation would change rooms, opening hosts or connections; kept the original draft."
    applied = bool(changes) and after_error is None and rejection is None
    report = dict(schema="plan_junction_preparation_v1",status="applied" if applied else "rolled_back" if changes else "unchanged",
                  floor_id=plan.get("floor_id"),pixel_decimals=PIXEL_DECIMALS,
                  tolerance_m=dict(zip(("x","y"),(t*s for t,s in zip(tolerances,scales)))),
                  tolerance_pixels=dict(zip(("x","y"),tolerances)),
                  changes=changes if applied else [],attempted_changes=changes if not applied else [],
                  compile_error_before=before_error,compile_error=after_error,rejection=rejection,
                  declared_seed_count=len(original.get("space_seeds",[])),opening_count=len(original.get("openings",[])),
                  space_count=sum(len(f["cells"]) for f in after["geometry"]["floors"]) if after is not None else None)
    return (result if applied else original), report


def regularize_plan(plan: dict, *, image_size: tuple[int, int], image_name: str,
                    rule_version: str = RULE_VERSION,
                    grid_step_m: float | None = None,
                    _before_lite_grid: bool = False) -> tuple[dict, dict]:
    """Regularise one floor draft and return it with a persistent full report."""
    if rule_version != RULE_VERSION:
        raise ValueError(f"unsupported regularization rule version {rule_version!r}")
    if not isinstance(plan, dict):
        raise TypeError("plan must be an object")
    from src.agent.geometry.lite_bim_regularization import lite_grid_step, regularize_lite_plan
    # A trusted run policy overrides model-supplied audit metadata. The default
    # retains the existing opt-in contract for historical/offline callers.
    lite_step = grid_step_m if grid_step_m is not None else lite_grid_step(plan)
    if lite_step is not None and not _before_lite_grid:
        result, lite_report = regularize_lite_plan(
            plan, image_size=image_size, image_name=image_name, grid_step_m=lite_step)
        report = {
            "schema": "plan_regularization_report_v1", "rule_version": rule_version,
            "status": "pass", "floor_id": result.get("floor_id"),
            "plan_sha256": _plan_digest(result), "lite_bim": lite_report,
            "changes": lite_report.get("changes", []), "rejections": [], "attempted_changes": [],
            "hard_constraints": lite_report["hard_constraints"],
            "reading_alignment": copy.deepcopy(result["regularization_inputs"]["reading_alignment"]),
            "summary": lite_report["summary"],
            "method": {"edits_plan_draft_only": True, "edits_source_bim_polygons": False,
                       "grid_step_m": lite_step, "audited_strip_simplification_allowed": bool(result.get("wall_metadata"))},
        }
        result["regularization"] = copy.deepcopy(report)
        return result, report
    existing = plan.get("regularization")
    if (isinstance(existing, dict) and existing.get("rule_version") == rule_version
            and not (_before_lite_grid and 'lite_bim' in existing)
            and existing.get("status") == "pass"
            and existing.get("plan_sha256") == _plan_digest(plan)):
        # A plan can carry caller-supplied metadata.  The digest makes replay
        # deterministic, not trusted: always re-run the current hard rules
        # before accepting the cached change list.
        hard = enforce_regularized_plan(plan, image_size=image_size, image_name=image_name)
        replayed_plan, replayed_report = copy.deepcopy(plan), copy.deepcopy(existing)
        replayed_report["hard_constraints"] = hard
        replayed_plan["regularization"] = copy.deepcopy(replayed_report)
        return replayed_plan, replayed_report
    result, preparation = prepare_plan_junctions(plan, image_size=image_size, image_name=image_name)
    result.pop("regularization", None)
    calibration = _calibration(result, image_size)
    line_refs, coordinate_refs = _regularization_inputs(result)
    original_separations = _separation_rows(_segments(result, calibration))
    before, _ = _compile(result, image_size, image_name)
    changes: list[dict] = copy.deepcopy(preparation["changes"])
    rejections: list[dict] = []

    # Close stepped representatives first; the shared connector is extended or
    # retracted with the moved leg.
    for _ in range(1000):
        if not _close_small_steps(result, calibration, line_refs, coordinate_refs, changes):
            break
    else:
        raise RuntimeError("plan regularization exceeded the small-step safety limit")

    # The outer outline is immutable. A sub-0.30 m duplicate inner overlap is
    # merged into the fixed edge; strip seeds and openings are migrated with
    # explicit audit, and only unrepresentable connectivity blocks the merge.
    blocked_footprint_pairs = set()
    for _ in range(1000):
        candidates = []
        for source in _segments(result, calibration):
            for target in _footprint_segments(result, calibration):
                pair = (source["partition_id"], target["partition_id"])
                if pair in blocked_footprint_pairs or source["axis"] != target["axis"]:
                    continue
                distance = abs(source["coordinate_m"] - target["coordinate_m"])
                overlap = _overlap(source, target)
                if (within_wall_tolerance(source, target, allow_equal=True)
                        and overlap > _EPS):
                    candidates.append((distance, -overlap, source, target))
        if not candidates:
            break
        _, _, source, target = min(candidates, key=lambda row: (
            row[0], row[1], row[2]["partition_id"], row[3]["partition_id"]
        ))
        if _split_polyline_for_merge(result, source, line_refs, changes):
            continue
        if not _merge_partition_to_footprint(
                result, source, target, calibration, changes, rejections):
            blocked_footprint_pairs.add((source["partition_id"], target["partition_id"]))
    else:
        raise RuntimeError("plan regularization exceeded the footprint-merge safety limit")

    # Overlapping near-parallel representatives delimit a mandatory sub-0.30 m
    # merge. Named strip seeds are removed and openings are migrated/deduplicated
    # with explicit audit; only unrepresentable connectivity is rejected.
    blocked_pairs = set()
    for _ in range(1000):
        segments = _segments(result, calibration)
        candidates = []
        for index, first in enumerate(segments):
            for second in segments[index + 1:]:
                pair = tuple(sorted((first["partition_id"], second["partition_id"])))
                if pair in blocked_pairs or first["partition_id"] == second["partition_id"] or first["axis"] != second["axis"]:
                    continue
                distance = abs(first["coordinate_m"] - second["coordinate_m"])
                overlap = _overlap(first, second)
                if (within_wall_tolerance(first, second, allow_equal=True)
                        and overlap > _EPS):
                    candidates.append((distance, -overlap, first, second))
        if not candidates:
            break
        _, _, first, second = min(candidates, key=lambda row: (
            row[0], row[1], row[2]["partition_id"], row[3]["partition_id"]
        ))
        split = _split_polyline_for_merge(result, first, line_refs, changes)
        if second["partition_id"] != first["partition_id"]:
            # Recompute the second partition index after the first list splice.
            current_second = next(
                (row for row in _segments(result, calibration)
                 if row["partition_id"] == second["partition_id"]
                 and row["segment_index"] == second["segment_index"]),
                None,
            )
            if current_second is not None:
                split = (_split_polyline_for_merge(
                    result, current_second, line_refs, changes) or split)
        if split:
            continue
        if not _merge_parallel_lines(result, first, second, calibration, line_refs,
                                     coordinate_refs, changes, rejections):
            blocked_pairs.add(tuple(sorted((first["partition_id"], second["partition_id"]))))
    else:
        raise RuntimeError("plan regularization exceeded the wall-merge safety limit")

    endpoint_before = copy.deepcopy(result)
    endpoint_change_start = len(changes)
    if not _settle_endpoints(result, calibration, line_refs, coordinate_refs, changes, rejections,
                             phase="same_floor"):
        attempted = copy.deepcopy(changes[endpoint_change_start:])
        del changes[endpoint_change_start:]
        result = endpoint_before
        rejections[-1]["attempted_changes"] = attempted

    prune_removed_metadata(result)
    after, compile_error = _compile(result, image_size, image_name)
    semantic_mapping = {"opening_id_map": [], "removed_space_seeds": [],
                        "affected_opening_ids": [], "status": "not_applicable"}
    if compile_error:
        rejections.append({
            "type": "strict_compile_failed_after_regularization",
            "contradiction_category": "literal_recompile_failed", "error": compile_error,
            "message": "The adjusted draft did not compile literally. Repair the reported walls; no source-BIM polygon was edited.",
        })
    if before is not None and after is not None:
        named_ids = {row.get("id") for row in plan.get("space_seeds", []) if row.get("id")}
        before_sig = _relationship_signature(before, named_space_ids=named_ids)
        after_sig = _relationship_signature(after, named_space_ids=named_ids)
        semantic_mapping = _narrow_strip_semantic_mapping(before_sig, after_sig, changes)
        eliminated_spaces = _eliminated_strip_spaces(plan, calibration, changes)
        semantic_mapping["eliminated_strip_spaces"] = eliminated_spaces
        semantic_mapping["space_count_before"] = before_sig["space_count"]
        semantic_mapping["space_count_after"] = after_sig["space_count"]
        semantic_mapping["expected_space_count_after"] = (
            before_sig["space_count"] - len(eliminated_spaces))
        has_strip_merge = any(row.get("type") in {
            "merge_duplicate_wall_lines", "merge_duplicate_wall_into_fixed_footprint"}
            for row in changes)
        affected_before = set(semantic_mapping["affected_opening_ids"])
        affected_after = {row["surviving_opening_id"]
                          for row in semantic_mapping["opening_id_map"]}

        def unaffected(rows, identities):
            return [row for row in rows if row[0] not in identities]

        changed = (
            unaffected(before_sig["opening_relations"], affected_before)
            != unaffected(after_sig["opening_relations"], affected_after)
            or unaffected(before_sig["named_opening_hosts"], affected_before)
            != unaffected(after_sig["named_opening_hosts"], affected_after)
            or (not has_strip_merge
                and unaffected(before_sig["opening_hosts"], affected_before)
                != unaffected(after_sig["opening_hosts"], affected_after))
            or any(row["after_connection"] is None
                   for row in semantic_mapping["opening_id_map"])
            or after_sig["space_count"] != semantic_mapping["expected_space_count_after"]
        )
        if changed:
            rejections.append({
                "type": "opening_or_connectivity_changed",
                "contradiction_category": "unauthorised_relationship_change_outside_eliminated_strip",
                "before": before_sig, "after": after_sig,
                "semantic_mapping": semantic_mapping,
                "message": "Regularisation changed a room, opening host or connection outside the explicitly eliminated narrow strip and was rolled back.",
            })
    hard = validate_regularized_plan(result, image_size=image_size, image_name=image_name)
    hard_rejections = copy.deepcopy(hard["violations"])
    for row in hard_rejections:
        row.setdefault("contradiction_category", "post_regularization_hard_constraint")
    rejections.extend(hard_rejections)
    final_separations = _separation_rows(_segments(result, calibration))
    preserved_separations = []
    for key, before_row in original_separations.items():
        after_row = final_separations.get(key)
        if after_row is not None:
            preserved_separations.append({
                "lines": before_row["lines"],
                "before_distance_m": before_row["distance_m"],
                "after_distance_m": after_row["distance_m"],
                "before_overlap_m": before_row["overlap_m"],
                "after_overlap_m": after_row["overlap_m"],
                "unchanged": (before_row["distance_m"] == after_row["distance_m"]
                              and before_row["overlap_m"] == after_row["overlap_m"]),
            })
    status = "pass" if not rejections else "rejected"
    applied_changes = changes if status == "pass" else []
    attempted_changes = [] if status == "pass" else [*preparation["attempted_changes"], *changes]
    semantic_mapping["status"] = "applied" if status == "pass" else "attempted_not_saved"
    report = {
        "schema": "plan_regularization_report_v1", "rule_version": rule_version,
        "junction_preparation": preparation,
        "status": status, "floor_id": result.get("floor_id"),
        "plan_sha256": _plan_digest(result),
        "thresholds": {"alignment_strictly_less_than_m": ALIGNMENT_THRESHOLD_M,
                       "minimum_space_width_m": MIN_SPACE_WIDTH_M},
        "changes": applied_changes, "attempted_changes": attempted_changes,
        "rejections": rejections, "hard_constraints": hard,
        "semantic_mapping": semantic_mapping,
        "preserved_separations": preserved_separations,
        "summary": {"moved_or_merged": len(applied_changes),
                    "attempted_changes": len(attempted_changes),
                    "maximum_movement_m": max((row.get("movement_m", 0.0) for row in applied_changes), default=0.0),
                    "rejected": len(rejections),
                    "change_counts": dict(Counter(row["type"] for row in applied_changes)),
                    "attempted_change_counts": dict(Counter(row["type"] for row in attempted_changes)),
                    "rejection_counts": dict(Counter(row["type"] for row in rejections))},
        "method": {"edits_plan_draft_only": True, "edits_source_bim_polygons": False,
                   "target_is_existing_line": True, "averages_coordinates": False,
                   "strict_compiler_snaps": False},
    }
    reading_alignment = result.get("regularization_inputs", {}).get("reading_alignment")
    if reading_alignment is not None:
        report["reading_alignment"] = copy.deepcopy(reading_alignment)
    if status == "rejected":
        first = rejections[0]
        raise PlanRegularizationError(
            first.get("message", f"Plan regularization rejected {first['type']}: {first.get('fix', 'review the reported objects and dimensions')}"),
            report=report,
        )
    result["regularization"] = copy.deepcopy(report)
    return result, report


def _elevation_priority(item: dict) -> int:
    reference = item.get("elevation_reference")
    if reference is None:
        return 0
    if isinstance(reference, bool):
        return 2 if reference else 0
    if (not isinstance(reference, dict)
            or set(reference) != {"basis", "source_refs"}
            or reference["basis"] not in {"elevation", "dimension", "measured", "inferred"}
            or not isinstance(reference["source_refs"], list)):
        raise ValueError("elevation_reference needs basis and source_refs")
    return {"inferred": 0, "measured": 1, "elevation": 2, "dimension": 3}[reference["basis"]]


def _stack_wall_segments(item: dict) -> list[dict]:
    calibration = _calibration(item["plan"], tuple(item["image_size"]))
    return [{**row, "floor_id": item["floor_id"], "item": item,
             "calibration": calibration} for row in _segments(item["plan"], calibration)]


def _stack_coordinate_support(items: list[dict], candidate: dict) -> tuple[int, int]:
    """Count actual co-linear wall evidence before the lower-storey tie-break."""
    floors = set()
    lines = set()
    for item in items:
        for segment in _stack_wall_segments(item):
            if (segment["axis"] != candidate["axis"]
                    or not math.isclose(
                        segment["coordinate_m"], candidate["coordinate_m"], abs_tol=1e-7)
                    or _overlap(segment, candidate) <= _EPS):
                continue
            floors.add(segment["floor_id"])
            lines.add((segment["floor_id"], segment["partition_id"], segment["segment_index"]))
    return len(floors), len(lines)


def _stack_footprint_segments(item: dict) -> list[dict]:
    calibration = _calibration(item["plan"], tuple(item["image_size"]))
    return [{**row, "floor_id": item["floor_id"], "item": item,
             "calibration": calibration} for row in _footprint_segments(item["plan"], calibration)]


def _stack_boundary_segments(item: dict) -> list[dict]:
    """Use the same physical boundary domain as the final source-BIM gate."""
    return [*_stack_wall_segments(item), *_stack_footprint_segments(item)]


def _stack_offset_pairs(lower: dict, upper: dict) -> list[tuple[dict, dict]]:
    lower_lines, upper_lines = _stack_boundary_segments(lower), _stack_boundary_segments(upper)

    def closer_match(line: dict, alternatives: list[dict], distance: float,
                     shared_start: float, shared_end: float) -> bool:
        # A compiled physical wall may consist of several declaration segments.
        # Only exactly collinear fragments form one matching span.  Combining
        # closer fragments on different coordinates would invent a wall that
        # the source gate does not have and hide a real unresolved offset.
        by_coordinate = {}
        for other in alternatives:
            if (other["axis"] == line["axis"]
                    and abs(other["coordinate_m"] - line["coordinate_m"])
                    < distance - NUMERIC_EQUALITY_TOLERANCE_M):
                by_coordinate.setdefault(other["coordinate_m"], []).append((
                    max(shared_start, other["span_m"][0]),
                    min(shared_end, other["span_m"][1])))
        for intervals in by_coordinate.values():
            covered = shared_start
            for start, end in sorted(intervals):
                if start > covered + NUMERIC_EQUALITY_TOLERANCE_M:
                    break
                covered = max(covered, end)
                if covered >= shared_end - NUMERIC_EQUALITY_TOLERANCE_M:
                    return True
        return False

    pairs = []
    for first in lower_lines:
        for second in upper_lines:
            if first["axis"] != second["axis"]:
                continue
            distance = abs(first["coordinate_m"] - second["coordinate_m"])
            if not within_wall_tolerance(first, second) or _overlap(first, second) <= _EPS:
                continue
            start = max(first["span_m"][0], second["span_m"][0])
            end = min(first["span_m"][1], second["span_m"][1])
            if (closer_match(first, upper_lines, distance, start, end)
                    or closer_match(second, lower_lines, distance, start, end)):
                continue
            pairs.append((first, second))
    return pairs


def _connected_footprint_edge(item: dict, segment: dict) -> dict | None:
    candidates = [
        edge for edge in _stack_footprint_segments(item)
        if (edge["axis"] == segment["axis"]
            and math.isclose(
                edge["coordinate_m"], segment["coordinate_m"], abs_tol=1e-7)
            and (_span_gap(edge, segment) <= _EPS or any(
                member["partition_id"] == segment["partition_id"]
                and member["segment_index"] == segment["segment_index"]
                for member in _connected_collinear_wall_chain(
                    item["plan"], edge, edge["calibration"]))))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda edge: (
        -_overlap(edge, segment), edge["segment_index"]))


def _footprint_edge_has_dimension_evidence(item: dict, edge: dict) -> bool:
    line_refs, coordinate_refs = _regularization_inputs(item["plan"])
    line_ids = {edge["partition_id"]}
    if edge.get("fixed_exterior"):
        line_ids.update(member["partition_id"] for member in _connected_collinear_wall_chain(
            item["plan"], edge, edge["calibration"]))
    return (
        any(row.get("basis") == "dimension"
            for identity in line_ids for row in line_refs.get(identity, []))
        or any(
            row["basis"] == "dimension"
            and row["axis"] == edge["axis"]
            and math.isclose(row["value_m"], edge["coordinate_m"], abs_tol=1e-6)
            for row in coordinate_refs
        )
    )


def _connected_collinear_wall_chain(plan: dict, edge: dict,
                                    calibration: dict) -> list[dict]:
    """Return same-coordinate partition segments connected to an exterior edge."""
    eligible = [
        segment for segment in _segments(plan, calibration)
        if (segment["axis"] == edge["axis"]
            and math.isclose(
                segment["coordinate_m"], edge["coordinate_m"], abs_tol=1e-7))
    ]
    selected: list[dict] = []
    pending = [segment for segment in eligible if _span_gap(segment, edge) <= _EPS]
    seen = set()
    while pending:
        segment = pending.pop(0)
        key = (segment["partition_id"], segment["segment_index"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(segment)
        pending.extend(
            other for other in eligible
            if (other["partition_id"], other["segment_index"]) not in seen
            and _span_gap(other, segment) <= _EPS
        )
    return sorted(selected, key=lambda row: (
        row["span_m"][0], row["span_m"][1], row["partition_id"], row["segment_index"]))


def _move_footprint_edge(item: dict, source: dict, target: dict,
                         changes: list[dict], *, basis: str) -> None:
    """Move one continuous source footprint edge and its literal dependants."""
    if not target.get("fixed_exterior") or not isinstance(
            target.get("segment_index"), int):
        raise ValueError(
            "Cross-storey footprint edges may move only to an explicit "
            "corresponding footprint edge"
        )
    plan = item["plan"]
    calibration = source["calibration"]
    axis = source["axis"]
    target_coordinate_m = target["coordinate_m"]
    normal_index = 0 if axis == "x" else 1
    along_index = 1 - normal_index
    target_pixel = _pixel_coordinate(calibration, axis, target_coordinate_m)
    old_pixel = source["points_pixel"][0][normal_index]
    chain = _connected_collinear_wall_chain(plan, source, calibration)
    chain_change_start = len(changes)
    for original in chain:
        current = next((
            segment for segment in _segments(plan, calibration)
            if segment["partition_id"] == original["partition_id"]
            and segment["segment_index"] == original["segment_index"]
            and segment["axis"] == original["axis"]
            and math.isclose(
                segment["coordinate_m"], source["coordinate_m"], abs_tol=1e-7)
        ), None)
        if current is None:
            continue
        _move_segment(
            plan, current, target_coordinate_m, calibration,
            reason="move a connected collinear wall-chain segment with its cross-storey footprint edge",
            changes=changes, basis="cross_storey_footprint_collinear_chain",
        )
    chain_changes = changes[chain_change_start:]
    chain_partition_ids = list(dict.fromkeys(
        identity for row in chain_changes
        for identity in [
            row.get("partition_id"),
            *(endpoint.get("partition_id")
              for endpoint in row.get("adjusted_endpoint_ids", [])),
        ]
        if identity is not None))
    chain_opening_ids = list(dict.fromkeys(
        identity for row in chain_changes
        for identity in row.get("object_ids", {}).get("openings", [])))
    footprint = plan["footprint_pixels"]
    edge_index = source["segment_index"]
    next_index = edge_index + 1 if edge_index + 1 < len(footprint) else 0
    vertex_indexes = {edge_index % len(footprint), next_index}
    if len(footprint) > 1 and footprint[0] == footprint[-1]:
        if 0 in vertex_indexes:
            vertex_indexes.add(len(footprint) - 1)
        if len(footprint) - 1 in vertex_indexes:
            vertex_indexes.add(0)
    footprint_before = copy.deepcopy(footprint)
    for index in vertex_indexes:
        footprint[index][normal_index] = target_pixel

    moved_openings = list(chain_opening_ids)
    for opening in plan.get("openings", []):
        if not _opening_on_segment(opening, source, calibration):
            continue
        opening["p1"][normal_index] = target_pixel
        opening["p2"][normal_index] = target_pixel
        if opening.get("id") not in moved_openings:
            moved_openings.append(opening.get("id"))

    adjusted_endpoints = []
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        if len(points) < 2:
            continue
        for endpoint_index in (0, len(points) - 1):
            endpoint = points[endpoint_index]
            world_endpoint = _world(calibration, endpoint)
            if (math.isclose(endpoint[normal_index], old_pixel, abs_tol=_EPS)
                    and source["span_m"][0] - _EPS
                    <= world_endpoint[along_index]
                    <= source["span_m"][1] + _EPS):
                endpoint[normal_index] = target_pixel
                adjusted_endpoints.append({
                    "partition_id": partition.get("id"),
                    "endpoint": endpoint_index,
                })

    changes.append({
        "type": "move_footprint_edge",
        "floor_id": item["floor_id"],
        "source_floor_id": source["floor_id"],
        "target_floor_id": target["floor_id"],
        "axis": axis,
        "partition_id": source["partition_id"],
        "source_edge_index": source["segment_index"],
        "target_edge_index": target["segment_index"],
        "object": _line_public(source),
        "object_ids": {
            "floors": [source["floor_id"], target["floor_id"]],
            "footprint_edges": [source["partition_id"], target["partition_id"]],
            "partitions": list(dict.fromkeys(
                [*chain_partition_ids,
                 *(row["partition_id"] for row in adjusted_endpoints)])),
            "openings": moved_openings,
        },
        "span_m": [round(value, 9) for value in source["span_m"]],
        "from_m": round(source["coordinate_m"], 9),
        "to_m": round(target_coordinate_m, 9),
        "movement_m": round(abs(target_coordinate_m - source["coordinate_m"]), 9),
        "basis": basis,
        "source": _line_public(source),
        "target": _line_public(target),
        "reason": "align one overlapping cross-storey footprint edge to the policy-selected existing edge",
        "footprint_before": footprint_before,
        "footprint_after": copy.deepcopy(footprint),
        "moved_opening_ids": moved_openings,
        "adjusted_endpoint_ids": adjusted_endpoints,
    })


def _validate_stack_items(items: list[dict]) -> None:
    required = {"plan", "image_size", "image_name", "floor_id", "z_floor", "source_ref"}
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a nonempty list")
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or required - set(item):
            raise ValueError(f"items[{index}] needs {sorted(required)}")
        if item["floor_id"] in seen:
            raise ValueError(f"duplicate floor_id {item['floor_id']!r}")
        seen.add(item["floor_id"])
        _finite(item["z_floor"], f"items[{index}].z_floor")


def _stack_separations(items: list[dict]) -> dict[tuple, dict]:
    rows = {}
    ordered = sorted(items, key=lambda row: (float(row["z_floor"]), row["floor_id"]))
    for lower, upper in zip(ordered, ordered[1:]):
        for first in _stack_wall_segments(lower):
            for second in _stack_wall_segments(upper):
                if first["axis"] != second["axis"]:
                    continue
                overlap = _overlap(first, second)
                distance = abs(first["coordinate_m"] - second["coordinate_m"])
                if overlap <= _EPS or distance + _EPS < ALIGNMENT_THRESHOLD_M:
                    continue
                key = (first["floor_id"], first["partition_id"], first["segment_index"],
                       second["floor_id"], second["partition_id"], second["segment_index"])
                rows[key] = {"lines": [_line_public(first), _line_public(second)],
                             "distance_m": round(distance, 9),
                             "overlap_m": round(overlap, 9)}
    return rows


def validate_regularized_plan_stack(items: list[dict], *, rule_version: str = RULE_VERSION) -> dict:
    """Return hard-rule violations across already regularised plan drafts."""
    _validate_stack_items(items)
    violations = []
    per_floor = {}
    for item in items:
        report = validate_regularized_plan(
            item["plan"], image_size=tuple(item["image_size"]), image_name=item["image_name"]
        )
        per_floor[item["floor_id"]] = report
        violations.extend({**row, "floor_id": item["floor_id"]} for row in report["violations"])
    ordered = sorted(items, key=lambda row: (float(row["z_floor"]), row["floor_id"]))
    pairs = []
    for lower, upper in zip(ordered, ordered[1:]):
        pairs.append([lower["floor_id"], upper["floor_id"]])
        lower_height = float(lower.get("height", lower["plan"]["ceiling_height"]))
        separation = float(upper["z_floor"]) - (float(lower["z_floor"]) + lower_height)
        if _strictly_under(abs(separation), ALIGNMENT_THRESHOLD_M):
            violations.append({
                "type": "vertical_gap_or_overlap_under_0_30m",
                "floor_ids": [lower["floor_id"], upper["floor_id"]],
                "distance_m": round(abs(separation), 9), "signed_gap_m": round(separation, 9),
                "fix": "Use the elevation-backed level, or separate the floor and ceiling by at least 0.30 m.",
            })
        for first, second in _stack_offset_pairs(lower, upper):
            both_exterior = first.get("fixed_exterior") and second.get("fixed_exterior")
            violations.append({
                "type": ("fixed_footprint_storey_offset_under_0_30m" if both_exterior
                         else "storey_wall_offset_under_0_30m"),
                "objects": [_line_public(first), _line_public(second)],
                "distance_m": round(abs(first["coordinate_m"] - second["coordinate_m"]), 9),
                "overlap_m": round(_overlap(first, second), 9),
                "fix": "Align the affected physical boundary to an existing evidence-backed line, including its connected footprint/partition chain; recompile and preserve rooms, opening hosts and connections.",
            })
    return {"schema": "plan_stack_hard_constraints_v1", "rule_version": rule_version,
            "status": "pass" if not violations else "rejected",
            "thresholds": {"alignment_strictly_less_than_m": ALIGNMENT_THRESHOLD_M,
                           "minimum_space_width_m": MIN_SPACE_WIDTH_M,
                           "numeric_equality_tolerance_m":
                               NUMERIC_EQUALITY_TOLERANCE_M},
            "violations": violations, "per_floor": per_floor,
            "coverage": {"floors": [item["floor_id"] for item in ordered], "adjacent_pairs": pairs}}


def enforce_regularized_plan_stack(items: list[dict], *, rule_version: str = RULE_VERSION) -> dict:
    report = validate_regularized_plan_stack(items, rule_version=rule_version)
    if report["violations"]:
        first = report["violations"][0]
        raise PlanRegularizationError(
            f"Plan-stack hard constraints rejected {first['type']}: {first.get('fix', 'review the reported objects and dimensions')}",
            report=report,
        )
    return report


def regularize_plan_stack(
        items: list[dict], *, rule_version: str = RULE_VERSION,
        cross_storey_failure_policy: str = "reject_assembly",
        grid_step_m: float | None = None,
) -> tuple[list[dict], dict]:
    """Regularise floor drafts, align adjacent storeys, and recompile each draft."""
    if cross_storey_failure_policy not in CROSS_STOREY_FAILURE_POLICIES:
        raise ValueError(
            "cross_storey_failure_policy must be reject_assembly or "
            "skip_failed_pair")
    _validate_stack_items(items)
    result = copy.deepcopy(items)
    from src.agent.geometry.lite_bim_regularization import lite_grid_step
    grid_steps = ([grid_step_m] * len(result) if grid_step_m is not None else
                  [lite_grid_step(item["plan"]) for item in result])
    if any(step is not None for step in grid_steps) and (
            any(step is None for step in grid_steps) or len(set(grid_steps)) != 1):
        raise PlanRegularizationError(
            "Lite BIM assembly requires every floor to use the same grid_step_m",
            report={"schema": "plan_stack_regularization_report_v1", "rule_version": rule_version,
                    "status": "rejected", "rejections": [{"type": "inconsistent_lite_grid_policy",
                    "floor_steps": dict(zip((i["floor_id"] for i in result), grid_steps))}]})
    if grid_steps and grid_steps[0] is not None:
        from src.agent.geometry.lite_bim_regularization import quantize_m
        invalid_placements = [
            {"floor_id": item["floor_id"], "field": field, "value_m": item[field]}
            for item in result for field in ("z_floor", "height") if field in item
            and abs(item[field] - quantize_m(item[field], grid_steps[0])) > 1e-7
        ]
        if invalid_placements:
            raise PlanRegularizationError(
                "Lite BIM target floor placement/height is off grid; revise the resolved level explicitly",
                report={"schema": "plan_stack_regularization_report_v1", "rule_version": rule_version,
                        "status": "rejected", "grid_step_m": grid_steps[0],
                        "rejections": invalid_placements})
    original_separations = _stack_separations(result)
    floor_reports = {}
    baseline_proposals = {}
    stack_changes = []
    rejected_attempts = []
    rejections = []
    skipped_alignments = []
    for item in result:
        try:
            item["plan"], floor_reports[item["floor_id"]] = regularize_plan(
                item["plan"], image_size=tuple(item["image_size"]), image_name=item["image_name"],
                rule_version=rule_version, grid_step_m=grid_step_m,
            )
        except PlanRegularizationError as exc:
            floor_reports[item["floor_id"]] = exc.report
            rejections.append({"type": "floor_regularization_rejected", "floor_id": item["floor_id"],
                               "report": exc.report})
        else:
            baseline, error = _compile(item["plan"], tuple(item["image_size"]), item["image_name"])
            if error:
                rejections.append({"type": "floor_baseline_compile_failed", "floor_id": item["floor_id"],
                                   "error": error})
            else:
                baseline_proposals[item["floor_id"]] = baseline
    if rejections:
        report = {"schema": "plan_stack_regularization_report_v1", "rule_version": rule_version,
                  "status": "rejected", "floor_reports": floor_reports,
                  "changes": [], "attempted_changes": stack_changes, "rejections": rejections,
                  "hard_constraints": None, "preserved_separations": [],
                  "summary": {"moved_or_merged": 0, "maximum_movement_m": 0.0,
                              "rejected": len(rejections)},
                  "method": {"edits_plan_drafts_then_recompiles": True,
                             "edits_source_bim_polygons": False}}
        raise PlanRegularizationError("One or more floors failed same-floor regularization", report=report)

    ordered = sorted(result, key=lambda row: (float(row["z_floor"]), row["floor_id"]))

    def attempt_footprint_alignment(source: dict, target: dict, *, basis: str) -> bool:
        """Apply one exterior move transactionally; retain a complete failed audit."""
        affected = source["item"]
        plan_before = copy.deepcopy(affected["plan"])
        proposal_before, before_error = _compile(
            affected["plan"], tuple(affected["image_size"]), affected["image_name"])
        change_start = len(stack_changes)
        _move_footprint_edge(affected, source, target, stack_changes, basis=basis)
        proposal_after, after_error = _compile(
            affected["plan"], tuple(affected["image_size"]), affected["image_name"])
        floor_hard = validate_regularized_plan(
            affected["plan"], image_size=tuple(affected["image_size"]),
            image_name=affected["image_name"],
        )
        relationship_changed = False
        before_sig = after_sig = None
        if before_error is None and after_error is None:
            named_ids = {
                row.get("id") for row in affected["plan"].get("space_seeds", [])
                if row.get("id")
            }
            before_sig = _relationship_signature(
                proposal_before, named_space_ids=named_ids)
            after_sig = _relationship_signature(
                proposal_after, named_space_ids=named_ids)
            relationship_changed = (
                before_sig["opening_relations"] != after_sig["opening_relations"]
                or before_sig["opening_hosts"] != after_sig["opening_hosts"]
                or before_sig["named_opening_hosts"] != after_sig["named_opening_hosts"]
                or before_sig["space_ids"] != after_sig["space_ids"]
                or before_sig["space_count"] != after_sig["space_count"]
            )
        if (before_error is not None or after_error is not None
                or floor_hard["violations"] or relationship_changed):
            attempted = copy.deepcopy(stack_changes[change_start:])
            del stack_changes[change_start:]
            rejected_attempts.extend(copy.deepcopy(attempted))
            affected["plan"] = plan_before
            contradiction = (
                "literal_recompile_failed"
                if before_error is not None or after_error is not None else
                "post_alignment_hard_constraint"
                if floor_hard["violations"] else
                "room_opening_host_or_connection_changed"
            )
            failure = {
                "type": "cross_storey_footprint_alignment_rejected",
                "contradiction_category": contradiction,
                "floor_id": affected["floor_id"],
                "source_floor_id": source["floor_id"],
                "target_floor_id": target["floor_id"],
                "source": _line_public(source), "target": _line_public(target),
                "distance_m": round(
                    abs(source["coordinate_m"] - target["coordinate_m"]), 9),
                "overlap_m": round(_overlap(source, target), 9),
                "attempted_changes": attempted,
                "compile_error_before": before_error,
                "compile_error_after": after_error,
                "hard_violations": copy.deepcopy(floor_hard["violations"]),
                "relationship_changed": relationship_changed,
                "before_relationships": before_sig,
                "after_relationships": after_sig,
                "message": (
                    "Cross-storey footprint alignment was rolled back because the changed "
                    "plan did not strictly preserve rooms, openings, hosts, connections and "
                    "same-floor hard constraints."
                ),
            }
            if cross_storey_failure_policy == "skip_failed_pair":
                failure.update({
                    "type": "cross_storey_footprint_alignment_skipped",
                    "failure_policy": cross_storey_failure_policy,
                    "disposition": "original_pair_preserved_and_reported",
                })
                skipped_alignments.append(failure)
            else:
                rejections.append(failure)
            return False
        stack_changes[-1]["before_relationships"] = before_sig
        stack_changes[-1]["after_relationships"] = after_sig
        return True

    propagated_footprint_dimensions: set[tuple[str, str, float]] = set()

    def footprint_dimension(item: dict, edge: dict) -> bool:
        return (_footprint_edge_has_dimension_evidence(item, edge)
                or (edge["floor_id"], edge["axis"], round(edge["coordinate_m"], 9))
                in propagated_footprint_dimensions)

    def propagate_footprint_dimension(source: dict) -> None:
        """Carry a trusted coordinate down already visited boundary chains once."""
        anchor = next((row for row in _stack_footprint_segments(source["item"])
                       if row["segment_index"] == source["segment_index"]), None)
        lower_index = next(index for index, item in enumerate(ordered)
                           if item["floor_id"] == source["floor_id"])
        for candidate_item in reversed(ordered[:lower_index]):
            if anchor is None:
                break
            candidates = []
            for first, second in _stack_offset_pairs(candidate_item, anchor["item"]):
                target_edge = _connected_footprint_edge(anchor["item"], second)
                source_edge = _connected_footprint_edge(candidate_item, first)
                if (source_edge is not None and target_edge is not None
                        and target_edge["segment_index"] == anchor["segment_index"]):
                    candidates.append((abs(first["coordinate_m"] - second["coordinate_m"]),
                                       -_overlap(first, second), source_edge))
            if not candidates:
                break
            candidate = min(candidates, key=lambda row: (
                row[0], row[1], row[2]["segment_index"]))[2]
            # A conflicting lower dimension remains for the structured final
            # rejection.  Never oscillate or silently overwrite its evidence.
            if footprint_dimension(candidate_item, candidate):
                break
            if not attempt_footprint_alignment(
                    candidate, anchor, basis="upper_dimension_reference_propagated"):
                break
            propagated_footprint_dimensions.add((
                candidate["floor_id"], candidate["axis"], round(anchor["coordinate_m"], 9)))
            anchor = next((row for row in _stack_footprint_segments(candidate_item)
                           if row["segment_index"] == candidate["segment_index"]), None)

    for lower, upper in zip(ordered, ordered[1:]):
        lower_height = float(lower.get("height", lower["plan"]["ceiling_height"]))
        separation = float(upper["z_floor"]) - (float(lower["z_floor"]) + lower_height)
        if _strictly_under(abs(separation), ALIGNMENT_THRESHOLD_M):
            lower_priority, upper_priority = _elevation_priority(lower), _elevation_priority(upper)
            if upper_priority > lower_priority or (upper_priority and upper_priority == lower_priority):
                rejections.append({
                    "type": "conflicting_elevation_references",
                    "floor_ids": [lower["floor_id"], upper["floor_id"]],
                    "distance_m": round(abs(separation), 9),
                    "message": "The upper level has equal or stronger elevation evidence. Changing the lower floor height would break the floor-height convention; review the two elevations explicitly.",
                })
            else:
                old_z = float(upper["z_floor"])
                upper["z_floor"] = float(lower["z_floor"]) + lower_height
                stack_changes.append({
                    "type": "align_storey_elevation", "floor_id": upper["floor_id"],
                    "axis": "z", "partition_id": None,
                    "object_ids": {"floors": [lower["floor_id"], upper["floor_id"]],
                                   "partitions": [], "openings": []},
                    "span_m": None,
                    "from_m": old_z, "to_m": upper["z_floor"],
                    "movement_m": round(abs(old_z - upper["z_floor"]), 9),
                    "basis": "trusted_elevation_then_lower_storey_tie_break",
                    "reason": "keep the lower elevation/height and move the whole upper compiled floor; floor height unchanged",
                })
        # Cross-storey exterior policy is directional and independent of wall
        # majority support: normally the upper continuous edge moves to the
        # overlapping lower edge.  Only an upper-only dimension reference
        # reverses that direction.  Same-floor footprint immutability remains
        # unchanged in regularize_plan().
        moved_footprint_sources: set[tuple[str, int]] = set()
        footprint_locked_coordinates: set[tuple[str, str, float]] = set()
        blocked_footprint_pairs: set[tuple] = set()
        while True:
            footprint_candidates = []
            for lower_edge, upper_edge in _stack_offset_pairs(lower, upper):
                if not (lower_edge.get("fixed_exterior") and upper_edge.get("fixed_exterior")):
                    continue
                pair_key = (
                    lower_edge["floor_id"], lower_edge["segment_index"],
                    upper_edge["floor_id"], upper_edge["segment_index"],
                )
                if pair_key in blocked_footprint_pairs:
                    continue
                distance = abs(lower_edge["coordinate_m"] - upper_edge["coordinate_m"])
                footprint_candidates.append(
                    (distance, -_overlap(lower_edge, upper_edge), pair_key, lower_edge, upper_edge))
            if not footprint_candidates:
                break
            distance, neg_overlap, pair_key, lower_edge, upper_edge = min(
                footprint_candidates,
                key=lambda row: (
                    row[0], row[1], row[3]["segment_index"], row[4]["segment_index"]),
            )
            lower_dimension = footprint_dimension(lower, lower_edge)
            upper_dimension = footprint_dimension(upper, upper_edge)
            if upper_dimension and not lower_dimension:
                source, target = lower_edge, upper_edge
                basis = "upper_only_dimension_reference"
            else:
                source, target = upper_edge, lower_edge
                basis = "lower_storey_footprint_default"
            source_key = (source["floor_id"], source["segment_index"])
            if source_key in moved_footprint_sources:
                blocked_footprint_pairs.add(pair_key)
                continue

            if not attempt_footprint_alignment(source, target, basis=basis):
                blocked_footprint_pairs.add(pair_key)
                continue
            remember_merge(source["item"]["plan"], source, target,
                           target_plan=target["item"]["plan"], keep_source=True)
            moved_footprint_sources.add(source_key)
            footprint_locked_coordinates.add((
                source["floor_id"], source["axis"], target["coordinate_m"]))
            target_dimension = upper_dimension if target is upper_edge else lower_dimension
            if target_dimension:
                propagated_footprint_dimensions.add((
                    source["floor_id"], source["axis"],
                    round(target["coordinate_m"], 9),
                ))

            # If an upper-only dimension reverses an adjacent pair after a
            # lower pair was already aligned, carry that trusted coordinate
            # down the already-visited footprint chain.  This is bounded by
            # the number of lower storeys and every move retains the same
            # compile, semantic and hard-rule transaction as the direct move.
            if (target_dimension and source is lower_edge and target is upper_edge):
                propagate_footprint_dimension(source)

        # Recompute candidates after every accepted edit.  Each tentative move
        # gets finite endpoint settling plus an immediate strict compile and
        # relationship check; failed attempts are rolled back and reported.
        moved_sources, blocked = set(), set()
        while True:
            candidates = []
            for first, second in _stack_offset_pairs(lower, upper):
                keys = ((first["floor_id"], first["partition_id"], first["segment_index"]),
                        (second["floor_id"], second["partition_id"], second["segment_index"]))
                pair_key = tuple(sorted(keys))
                if pair_key in blocked:
                    continue
                distance = abs(first["coordinate_m"] - second["coordinate_m"])
                candidates.append((distance, -_overlap(first, second), pair_key, first, second))
            if not candidates:
                break
            distance, neg_overlap, pair_key, first, second = min(
                candidates,
                key=lambda row: (row[0], row[1], row[3]["partition_id"], row[4]["partition_id"]),
            )
            first_refs, first_coords = _regularization_inputs(first["item"]["plan"])
            second_refs, second_coords = _regularization_inputs(second["item"]["plan"])
            first_support = _stack_coordinate_support(ordered, first)
            second_support = _stack_coordinate_support(ordered, second)
            p_first = _priority(
                first, first_refs, first_coords, lower_storey=True,
                coordinate_support=first_support,
            )
            p_second = _priority(
                second, second_refs, second_coords,
                coordinate_support=second_support,
            )
            first_locked = any(
                floor_id == first["floor_id"] and axis == first["axis"]
                and math.isclose(value, first["coordinate_m"], abs_tol=1e-7)
                for floor_id, axis, value in footprint_locked_coordinates)
            second_locked = any(
                floor_id == second["floor_id"] and axis == second["axis"]
                and math.isclose(value, second["coordinate_m"], abs_tol=1e-7)
                for floor_id, axis, value in footprint_locked_coordinates)
            first_exterior = _connected_footprint_edge(first["item"], first)
            second_exterior = _connected_footprint_edge(second["item"], second)
            if second_exterior is not None and first_exterior is not None:
                # The physical boundary may change from exterior to interior
                # between storeys.  An overlapping, connected collinear chain
                # establishes correspondence even if its exterior pieces alone
                # do not overlap.  The transaction below still recompiles the
                # entire changed outline and preserves rooms/hosts/connections.
                upper_dimension = footprint_dimension(
                    second["item"], second_exterior)
                lower_dimension = footprint_dimension(
                    first["item"], first_exterior)
                if upper_dimension and not lower_dimension:
                    target, source = second, first
                else:
                    target, source = first, second
            elif second_exterior is not None:
                # The upper wall is a continuation of a fixed exterior edge,
                # while the lower candidate is an internal wall.  Keep the
                # exterior coordinate and move the ordinary wall to it.  If
                # the internal wall alone carries dimension evidence, neither
                # legal direction can preserve both authorities; leave the
                # pair for the final structured hard-constraint rejection.
                upper_dimension = _footprint_edge_has_dimension_evidence(
                    second["item"], second_exterior)
                lower_dimension = _footprint_edge_has_dimension_evidence(
                    first["item"], first)
                if lower_dimension and not upper_dimension:
                    blocked.add(pair_key)
                    continue
                target, source = second, first
            elif first_exterior is not None:
                upper_dimension = _footprint_edge_has_dimension_evidence(
                    second["item"], second)
                lower_dimension = _footprint_edge_has_dimension_evidence(
                    first["item"], first_exterior)
                if upper_dimension and not lower_dimension:
                    blocked.add(pair_key)
                    continue
                target, source = first, second
            elif first_locked != second_locked:
                target, source = (first, second) if first_locked else (second, first)
            else:
                target, source = (first, second) if p_first >= p_second else (second, first)
            target_support = first_support if target is first else second_support
            source_support = second_support if source is second else first_support
            source_key = (source["floor_id"], source["partition_id"], source["segment_index"])
            if source_key in moved_sources:
                blocked.add(pair_key)
                continue
            affected = source["item"]
            source_exterior = _connected_footprint_edge(affected, source)
            target_exterior = (
                _connected_footprint_edge(target["item"], target)
                if source_exterior is not None else None
            )
            if source_exterior is not None and target_exterior is None:
                blocked.add(pair_key)
                continue
            plan_before = copy.deepcopy(affected["plan"])
            proposal_before, before_error = _compile(
                affected["plan"], tuple(affected["image_size"]), affected["image_name"])
            change_start = len(stack_changes)
            if source_exterior is not None:
                _move_footprint_edge(
                    affected, source_exterior, target_exterior, stack_changes,
                    basis="cross_storey_connected_footprint_chain",
                )
            else:
                _move_segment(
                    affected["plan"], source, target["coordinate_m"], source["calibration"],
                    reason="cross-storey alignment to an existing higher-priority wall line",
                    changes=stack_changes,
                )
            stack_changes[-1]["matched_boundaries"] = [
                _line_public(source), _line_public(target)]
            stack_changes[-1]["source_coordinate_support"] = {
                "floors": source_support[0], "collinear_wall_segments": source_support[1]
            }
            stack_changes[-1]["target_coordinate_support"] = {
                "floors": target_support[0], "collinear_wall_segments": target_support[1]
            }
            if (source_exterior is None
                    and target_support > source_support and p_first[:2] == p_second[:2]):
                stack_changes[-1]["basis"] = "more_storeys_then_collinear_walls"
            if source_exterior is None and first_locked != second_locked:
                stack_changes[-1]["basis"] = "cross_storey_footprint_target_lock"
            local_rejections: list[dict] = []
            source_refs, source_coords = _regularization_inputs(affected["plan"])
            settled = _settle_endpoints(
                affected["plan"], source["calibration"], source_refs, source_coords,
                stack_changes, local_rejections, phase="cross_storey",
                locked_coordinates={
                    (axis, value)
                    for floor_id, axis, value in footprint_locked_coordinates
                    if floor_id == affected["floor_id"]
                } | {(source["axis"], target["coordinate_m"])},
            )
            proposal_after, after_error = _compile(
                affected["plan"], tuple(affected["image_size"]), affected["image_name"])
            floor_hard = validate_regularized_plan(
                affected["plan"], image_size=tuple(affected["image_size"]),
                image_name=affected["image_name"],
            )
            relationship_changed = False
            before_sig = after_sig = None
            if settled and before_error is None and after_error is None:
                named_ids = {row.get("id") for row in affected["plan"].get("space_seeds", [])
                             if row.get("id")}
                before_sig = _relationship_signature(proposal_before, named_space_ids=named_ids)
                after_sig = _relationship_signature(proposal_after, named_space_ids=named_ids)
                relationship_changed = (
                    before_sig["opening_relations"] != after_sig["opening_relations"]
                    or before_sig["opening_hosts"] != after_sig["opening_hosts"]
                    or before_sig["named_opening_hosts"] != after_sig["named_opening_hosts"]
                    or before_sig["space_ids"] != after_sig["space_ids"]
                    or before_sig["space_count"] != after_sig["space_count"]
                )
            if (not settled or before_error is not None or after_error is not None
                    or floor_hard["violations"] or relationship_changed):
                attempted = copy.deepcopy(stack_changes[change_start:])
                del stack_changes[change_start:]
                rejected_attempts.extend(copy.deepcopy(attempted))
                affected["plan"] = plan_before
                rejection = {
                    "type": "cross_storey_alignment_rejected",
                    "contradiction_category": (
                        "endpoint_regularization_failed" if not settled else
                        "literal_recompile_failed" if before_error is not None or after_error is not None else
                        "post_alignment_hard_constraint" if floor_hard["violations"] else
                        "room_opening_host_or_connection_changed"
                    ),
                    "floor_id": affected["floor_id"],
                    "source": _line_public(source), "target": _line_public(target),
                    "distance_m": round(distance, 9), "overlap_m": round(-neg_overlap, 9),
                    "attempted_changes": attempted, "endpoint_rejections": local_rejections,
                    "compile_error_before": before_error, "compile_error_after": after_error,
                    "hard_violations": copy.deepcopy(floor_hard["violations"]),
                    "relationship_changed": relationship_changed,
                    "before_relationships": before_sig, "after_relationships": after_sig,
                    "message": (
                        "Cross-storey alignment was rolled back because the affected current plan did not "
                        "strictly recompile with unchanged rooms, openings, hosts and connections."
                    ),
                }
                if cross_storey_failure_policy == "skip_failed_pair":
                    rejection.update({
                        "type": "cross_storey_alignment_skipped",
                        "failure_policy": cross_storey_failure_policy,
                        "disposition": "original_pair_preserved_and_reported",
                    })
                    skipped_alignments.append(rejection)
                else:
                    rejections.append(rejection)
                blocked.add(pair_key)
                continue
            # A source segment moves at most once in this adjacent-storey pass,
            # while one trusted target line may receive several disjoint source
            # segments.  Locking both sides leaves real residual offsets.
            remember_merge(affected["plan"], source, target,
                           target_plan=target["item"]["plan"], keep_source=True)
            moved_sources.add(source_key)
            if source_exterior is not None:
                footprint_locked_coordinates.add((
                    source["floor_id"], source["axis"], target["coordinate_m"]))
                if footprint_dimension(target["item"], target_exterior):
                    propagated_footprint_dimensions.add((
                        source["floor_id"], source["axis"], round(target["coordinate_m"], 9)))
                    if source is first:
                        propagate_footprint_dimension(source_exterior)

    # Recompile changed plan drafts and refuse any opening/connection change.
    for item in result:
        from src.agent.geometry.lite_bim_regularization import lite_grid_step, regularize_lite_plan
        lite_step = lite_grid_step(item["plan"])
        if lite_step is not None:
            try:
                from src.agent.geometry.wall_merge_policy import stamp_wall_state
                stamp_wall_state(item['plan'])  # Accepted internal moves preserve observed membership.
                before_grid = copy.deepcopy(item["plan"])
                item["plan"], lite_report = regularize_lite_plan(
                    item["plan"], image_size=tuple(item["image_size"]),
                    image_name=item["image_name"], grid_step_m=lite_step)
                fields = ("footprint_pixels", "partitions", "openings", "z_floor", "ceiling_height")
                geometry_changed = any(before_grid.get(key) != item["plan"].get(key) for key in fields)
                if geometry_changed:
                    stack_changes.extend(copy.deepcopy(lite_report.get("changes", [])))
                floor_report = floor_reports[item["floor_id"]]
                floor_report.update(
                    lite_bim=copy.deepcopy(lite_report),
                    hard_constraints=copy.deepcopy(lite_report["hard_constraints"]),
                    reading_alignment=copy.deepcopy(item["plan"]["regularization_inputs"]["reading_alignment"]),
                    plan_sha256=_plan_digest(item["plan"]),
                )
                item["plan"]["regularization"] = copy.deepcopy(floor_report)
            except PlanRegularizationError as exc:
                rejections.append({"type": "lite_grid_rejected", "floor_id": item["floor_id"], "report": exc.report})
                continue
        before = baseline_proposals.get(item["floor_id"])
        proposal, error = _compile(item["plan"], tuple(item["image_size"]), item["image_name"])
        if error:
            rejections.append({"type": "strict_recompile_failed", "floor_id": item["floor_id"],
                               "error": error,
                               "message": "Cross-storey alignment must change the plan draft and recompile; it may not edit source-BIM polygons."})
            continue
        if before is not None:
            named_ids = {row.get("id") for row in item["plan"].get("space_seeds", []) if row.get("id")}
            before_sig = _relationship_signature(before, named_space_ids=named_ids)
            after_sig = _relationship_signature(proposal, named_space_ids=named_ids)
            if (before_sig["opening_relations"] != after_sig["opening_relations"]
                    or before_sig["opening_hosts"] != after_sig["opening_hosts"]
                    or before_sig["named_opening_hosts"] != after_sig["named_opening_hosts"]
                    or before_sig["space_ids"] != after_sig["space_ids"]
                    or before_sig["space_count"] != after_sig["space_count"]):
                rejections.append({"type": "cross_storey_relationship_changed", "floor_id": item["floor_id"],
                                   "before": before_sig, "after": after_sig,
                                   "message": "Cross-storey alignment changed a room, opening host or connection and was rejected."})
        item["proposal"] = proposal
    hard = validate_regularized_plan_stack(result, rule_version=rule_version)
    hard_rejections = copy.deepcopy(hard["violations"])
    if cross_storey_failure_policy == "skip_failed_pair":
        remaining_hard_rejections = []
        for row in hard_rejections:
            if row.get("type") not in {
                    "storey_wall_offset_under_0_30m",
                    "fixed_footprint_storey_offset_under_0_30m"}:
                remaining_hard_rejections.append(row)
                continue
            objects = row.get("objects", [])
            object_key = {
                (obj.get("floor_id"), obj.get("partition_id"), obj.get("axis"))
                for obj in objects
            }
            existing = next((
                skipped for skipped in skipped_alignments
                if object_key == {
                    (obj.get("floor_id"), obj.get("partition_id"), obj.get("axis"))
                    for obj in (skipped.get("source"), skipped.get("target"))
                    if isinstance(obj, dict)
                }
            ), None)
            if existing is None:
                skipped_alignments.append({
                    "type": "cross_storey_alignment_skipped",
                    "failure_policy": cross_storey_failure_policy,
                    "disposition": "original_pair_preserved_and_reported",
                    "contradiction_category": "unresolved_cross_storey_offset",
                    "source": copy.deepcopy(objects[0]) if objects else None,
                    "target": copy.deepcopy(objects[1]) if len(objects) > 1 else None,
                    "distance_m": row.get("distance_m"),
                    "overlap_m": row.get("overlap_m"),
                    "hard_violation": copy.deepcopy(row),
                    "message": (
                        "Cross-storey alignment could not be applied safely; "
                        "the original pair is preserved and listed for delivery."
                    ),
                })
            else:
                existing["hard_violation"] = copy.deepcopy(row)
        hard_rejections = remaining_hard_rejections
        hard = copy.deepcopy(hard)
        hard["violations"] = copy.deepcopy(hard_rejections)
        hard["skipped_violations"] = [
            copy.deepcopy(row.get("hard_violation"))
            for row in skipped_alignments if row.get("hard_violation")
        ]
        hard["status"] = "pass" if not hard_rejections else "rejected"
    for row in hard_rejections:
        row.setdefault("contradiction_category", "post_regularization_hard_constraint")
    rejections.extend(hard_rejections)
    final_separations = _stack_separations(result)
    preserved_separations = []
    for key, before_row in original_separations.items():
        after_row = final_separations.get(key)
        if after_row is not None:
            preserved_separations.append({
                "lines": before_row["lines"],
                "before_distance_m": before_row["distance_m"],
                "after_distance_m": after_row["distance_m"],
                "before_overlap_m": before_row["overlap_m"],
                "after_overlap_m": after_row["overlap_m"],
                "unchanged": (before_row["distance_m"] == after_row["distance_m"]
                              and before_row["overlap_m"] == after_row["overlap_m"]),
            })
    status = "pass" if not rejections else "rejected"
    applied_stack_changes = stack_changes if status == "pass" else []
    attempted_stack_changes = (
        rejected_attempts if status == "pass" else
        [*stack_changes, *rejected_attempts]
    )
    report = {
        "schema": "plan_stack_regularization_report_v1", "rule_version": rule_version,
        "status": status,
        "floor_reports": floor_reports, "changes": applied_stack_changes,
        "attempted_changes": attempted_stack_changes, "rejections": rejections,
        "skipped_alignments": skipped_alignments,
        "preserved_separations": preserved_separations,
        "hard_constraints": hard,
        "thresholds": {
            "alignment_strictly_less_than_m": ALIGNMENT_THRESHOLD_M,
            "minimum_space_width_m": MIN_SPACE_WIDTH_M,
            "numeric_equality_tolerance_m": NUMERIC_EQUALITY_TOLERANCE_M,
        },
        "cross_storey_failure_policy": cross_storey_failure_policy,
        "elevation_references": {
            item["floor_id"]: {
                "declared_trusted": bool(item.get("elevation_reference", False)),
                "priority": _elevation_priority(item),
                "z_floor_m": float(item["z_floor"]),
                "height_m": float(item.get("height", item["plan"]["ceiling_height"])),
            } for item in result
        },
        "summary": {"moved_or_merged": len(applied_stack_changes),
                    "attempted_changes": len(attempted_stack_changes),
                    "maximum_movement_m": max((row.get("movement_m", 0.0) for row in applied_stack_changes), default=0.0),
                    "rejected": len(rejections),
                    "skipped_alignments": len(skipped_alignments),
                    "change_counts": dict(Counter(row["type"] for row in applied_stack_changes)),
                    "attempted_change_counts": dict(Counter(row["type"] for row in attempted_stack_changes)),
                    "rejection_counts": dict(Counter(row["type"] for row in rejections))},
        "method": {"edits_plan_drafts_then_recompiles": True, "edits_source_bim_polygons": False,
                   "floor_height_changed": False, "elevation_priority_preserved": True},
    }
    if rejections:
        first = rejections[0]
        raise PlanRegularizationError(first.get("message", f"Plan-stack regularization rejected {first['type']}"), report=report)
    for item in result:
        item["plan"]["regularization"]["stack"] = {
            "rule_version": rule_version,
            "changes": [row for row in stack_changes if row.get("floor_id") in {None, item["floor_id"]}
                        or row.get("object", {}).get("floor_id") == item["floor_id"]],
            "skipped_alignments": [
                row for row in skipped_alignments
                if item["floor_id"] in {
                    row.get("floor_id"), row.get("source_floor_id"),
                    row.get("target_floor_id"),
                    (row.get("source") or {}).get("floor_id"),
                    (row.get("target") or {}).get("floor_id"),
                }
            ],
            "status": "pass",
        }
        item["plan"]["regularization"]["plan_sha256"] = _plan_digest(item["plan"])
        floor_reports[item["floor_id"]]["plan_sha256"] = _plan_digest(item["plan"])
    return result, report
