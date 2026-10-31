"""Align an approximate reader plan to nearby drawing ink.

This module is deliberately limited to the role-division trial path.  It does
not identify new walls or openings: it only moves an already declared,
orthogonal item when a nearby, sufficiently long ink run supports that move.
Missing support is a recorded non-action.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Point, Polygon

from src.agent.correction.config import load_core_tolerances
from src.agent.geometry.plan_drawing_differences import _ink, _runs


SCHEMA_VERSION = "plan_reading_ink_alignment_v1"
DEFAULT_SEARCH_WORLD_M = 0.30
MIN_SUPPORT_FRACTION = 0.20
MIN_OPENING_EDGE_SUPPORT = 0.50
MIN_OPENING_SIDE_CONTINUITY = 0.80
MAX_OPENING_GAP_SUPPORT = 0.20
WALL_FACE_RANGE_M = (0.05, 0.45)
MIN_JUNCTION_TOLERANCE_M = 0.05
MAX_JUNCTION_TOLERANCE_M = 0.30


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _axis_scale(plan: Mapping[str, Any], axis: str) -> float:
    anchors = plan.get(f"{axis}_anchors")
    if (not isinstance(anchors, list) or len(anchors) != 2
            or any(not isinstance(row, list) or len(row) != 2 for row in anchors)):
        raise ValueError(f"numeric plan.{axis}_anchors are required for ink alignment")
    (p0, w0), (p1, w1) = anchors
    if not all(_finite(value) for value in (p0, w0, p1, w1)) or p0 == p1 or w0 == w1:
        raise ValueError(f"distinct numeric plan.{axis}_anchors are required for ink alignment")
    return abs((float(w1) - float(w0)) / (float(p1) - float(p0)))


def _junction_tolerance_pixels(metres_per_pixel: float) -> float:
    """Match the precompile join tolerance without applying its edits early."""

    return (min(MAX_JUNCTION_TOLERANCE_M,
                max(MIN_JUNCTION_TOLERANCE_M, 3.0 * metres_per_pixel))
            / metres_per_pixel)


def _segments(points: list[list[float]], *, closed: bool = False):
    pairs = list(zip(points, points[1:]))
    if closed and len(points) > 2:
        pairs.append((points[-1], points[0]))
    for index, (first, second) in enumerate(pairs):
        if first[0] == second[0]:
            yield index, "y", "x", float(first[0]), sorted((float(first[1]), float(second[1])))
        elif first[1] == second[1]:
            yield index, "x", "y", float(first[1]), sorted((float(first[0]), float(second[0])))


def _line_scores(mask: np.ndarray, *, along_axis: str, cross_at: float,
                 span: list[float], radius_px: int) -> tuple[list[int], list[float]]:
    height, width = mask.shape
    lo = max(0, int(math.ceil(span[0])))
    hi_limit = width - 1 if along_axis == "x" else height - 1
    hi = min(hi_limit, int(math.floor(span[1])))
    if hi - lo < 2:
        return [], []
    trim = min(max(1, int((hi - lo + 1) * 0.03)), max(1, (hi - lo) // 4))
    lo, hi = lo + trim, hi - trim
    cross_limit = height - 1 if along_axis == "x" else width - 1
    start = max(0, int(math.floor(cross_at)) - radius_px)
    stop = min(cross_limit, int(math.ceil(cross_at)) + radius_px)
    coordinates = list(range(start, stop + 1))
    scores = []
    for coordinate in coordinates:
        line = mask[coordinate, lo:hi + 1] if along_axis == "x" else mask[lo:hi + 1, coordinate]
        scores.append(float(line.mean()) if line.size else 0.0)
    return coordinates, scores


def _supported_runs(coordinates: list[int], scores: list[float], threshold: float) -> list[dict[str, float]]:
    if not coordinates:
        return []
    flags = np.asarray(scores) >= threshold
    result = []
    for start, end in _runs(flags):
        part = scores[start:end + 1]
        weights = [max(score, 1e-6) for score in part]
        centre = sum(coordinates[start + offset] * weight for offset, weight in enumerate(weights)) / sum(weights)
        result.append({
            "first": float(coordinates[start]),
            "last": float(coordinates[end]),
            "centre": float(centre),
            "support": float(max(part)),
        })
    return result


def _choose_wall_line(mask: np.ndarray, *, along_axis: str, cross_at: float,
                      span: list[float], radius_px: int, cross_mpp: float,
                      convention: str, max_distance_px: float,
                      outward: int = 0) -> tuple[float | None, dict[str, Any]]:
    coordinates, scores = _line_scores(
        mask, along_axis=along_axis, cross_at=cross_at, span=span, radius_px=radius_px,
    )
    best = max(scores, default=0.0)
    threshold = max(MIN_SUPPORT_FRACTION, best * 0.55)
    runs = _supported_runs(coordinates, scores, threshold)
    detail: dict[str, Any] = {
        "declared_pixel": round(cross_at, 4),
        "search_radius_pixels": radius_px,
        "best_support_fraction": round(best, 4),
        "candidate_count": len(runs),
    }
    if not runs or best < MIN_SUPPORT_FRACTION:
        detail["reason"] = "no sufficiently long nearby ink run"
        return None, detail
    if convention == "outer_face":
        near = [
            row for row in runs
            if abs((row["first"] if outward < 0 else row["last"]) - cross_at)
            <= max_distance_px + 1e-9
        ]
        if not near:
            detail["reason"] = "nearby ink has no outer face within the physical search radius"
            return None, detail
        chosen = (min(near, key=lambda row: row["centre"]) if outward < 0
                  else max(near, key=lambda row: row["centre"]))
        target = chosen["first"] if outward < 0 else chosen["last"]
        detail.update(method="outermost supported face in the outward direction",
                      selected_support_fraction=round(chosen["support"], 4))
        return float(target), detail

    face_pairs: list[tuple[float, float, str, float]] = []
    for index, first in enumerate(runs):
        for second in runs[index + 1:]:
            spacing_m = abs(second["centre"] - first["centre"]) * cross_mpp
            if WALL_FACE_RANGE_M[0] <= spacing_m <= WALL_FACE_RANGE_M[1]:
                centre = (first["centre"] + second["centre"]) / 2.0
                face_pairs.append((abs(centre - cross_at), centre, "midline of supported wall faces",
                                  min(first["support"], second["support"])))
    candidates = face_pairs or [
        (abs(row["centre"] - cross_at), row["centre"], "ink band centre", row["support"])
        for row in runs
    ]
    candidates = [row for row in candidates if row[0] <= max_distance_px + 1e-9]
    if not candidates:
        detail["reason"] = "nearby ink has no centreline within the physical search radius"
        return None, detail
    _, target, method, support = min(candidates, key=lambda row: (row[0], -row[3], row[1]))
    detail.update(method=method, selected_support_fraction=round(support, 4))
    return float(target), detail


def _same_line(point: list[float], *, cross_index: int, cross_at: float, tolerance: float = 1e-6) -> bool:
    return len(point) == 2 and _finite(point[cross_index]) and abs(float(point[cross_index]) - cross_at) <= tolerance


def _outward_sign(ring: list[list[float]], edge_index: int, cross_axis: str) -> int:
    """Return the exterior-normal sign for a possibly concave polygon edge."""

    signed_twice_area = sum(
        float(first[0]) * float(second[1]) - float(second[0]) * float(first[1])
        for first, second in zip(ring, [*ring[1:], ring[0]])
    )
    if signed_twice_area == 0:
        raise ValueError("footprint ring has zero signed area")
    first, second = ring[edge_index], ring[(edge_index + 1) % len(ring)]
    dx, dy = float(second[0]) - float(first[0]), float(second[1]) - float(first[1])
    # A CCW ring has interior on the left; its exterior is the right normal
    # (dy, -dx).  A CW ring uses the opposite normal.
    orientation = 1 if signed_twice_area > 0 else -1
    component = orientation * (dy if cross_axis == "x" else -dx)
    return -1 if component < 0 else 1


def _orthogonal_ids(plan: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    partitions = set()
    openings = set()
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        pairs = list(zip(points, points[1:])) if isinstance(points, list) else []
        if pairs and all(first[0] == second[0] or first[1] == second[1]
                         for first, second in pairs):
            partitions.add(str(partition.get("id", "?")))
    for opening in plan.get("openings", []):
        first, second = opening.get("p1"), opening.get("p2")
        if (isinstance(first, list) and isinstance(second, list)
                and len(first) == len(second) == 2
                and (first[0] == second[0] or first[1] == second[1])):
            openings.add(str(opening.get("id", "?")))
    return partitions, openings


def _footprint_edge_directions(plan: Mapping[str, Any]) -> dict[int, tuple[str, int, float]]:
    """Record every nonzero orthogonal perimeter edge's axis and direction."""

    ring = plan.get("footprint_pixels", [])
    if not isinstance(ring, list) or len(ring) < 3:
        return {}
    result: dict[int, tuple[str, int, float]] = {}
    for index, (first, second) in enumerate(zip(ring, [*ring[1:], ring[0]])):
        dx = float(second[0]) - float(first[0])
        dy = float(second[1]) - float(first[1])
        if abs(dx) <= 1e-9 and abs(dy) > 1e-9:
            result[index] = ("y", 1 if dy > 0 else -1, abs(dy))
        elif abs(dy) <= 1e-9 and abs(dx) > 1e-9:
            result[index] = ("x", 1 if dx > 0 else -1, abs(dx))
    return result


def _point_on_segment(point: list[float], first: list[float], second: list[float],
                      tolerance: float = 1e-5) -> bool:
    if first[0] == second[0]:
        return (abs(float(point[0]) - float(first[0])) <= tolerance
                and min(first[1], second[1]) - tolerance <= point[1]
                <= max(first[1], second[1]) + tolerance)
    if first[1] == second[1]:
        return (abs(float(point[1]) - float(first[1])) <= tolerance
                and min(first[0], second[0]) - tolerance <= point[0]
                <= max(first[0], second[0]) + tolerance)
    return False


def _wall_host_segments(plan: Mapping[str, Any]) -> dict[str, list[tuple[list[float], list[float]]]]:
    ring = plan.get("footprint_pixels", [])
    hosts = {"footprint": list(zip(ring, [*ring[1:], ring[0]])) if len(ring) >= 3 else []}
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        hosts[f"partition:{partition.get('id', '?')}"] = list(zip(points, points[1:]))
    return hosts


def _attachment_requirements(plan: Mapping[str, Any]) -> tuple[
    dict[tuple[str, int], set[str]], dict[str, set[str]],
]:
    hosts = _wall_host_segments(plan)
    partition_ends: dict[tuple[str, int], set[str]] = {}
    for partition in plan.get("partitions", []):
        partition_id = str(partition.get("id", "?"))
        points = partition.get("points", [])
        for endpoint_index in (0, len(points) - 1):
            if len(points) < 2:
                continue
            endpoint = points[endpoint_index]
            attached = {
                host for host, segments in hosts.items()
                if host != f"partition:{partition_id}"
                and any(_point_on_segment(endpoint, first, second) for first, second in segments)
            }
            if attached:
                partition_ends[(partition_id, endpoint_index)] = attached
    opening_hosts: dict[str, set[str]] = {}
    for opening in plan.get("openings", []):
        first, second = opening.get("p1"), opening.get("p2")
        if not (isinstance(first, list) and isinstance(second, list)):
            continue
        attached = {
            host for host, segments in hosts.items()
            if any(_point_on_segment(first, start, end)
                   and _point_on_segment(second, start, end) for start, end in segments)
        }
        if attached:
            opening_hosts[str(opening.get("id", "?"))] = attached
    return partition_ends, opening_hosts


def _footprint_preserves_declarations(
    plan: Mapping[str, Any], *,
    orthogonal_partitions: set[str] | None = None,
    orthogonal_openings: set[str] | None = None,
    partition_attachments: dict[tuple[str, int], set[str]] | None = None,
    opening_hosts: dict[str, set[str]] | None = None,
    footprint_edge_directions: dict[int, tuple[str, int, float]] | None = None,
    footprint_axis_scales: Mapping[str, float] | None = None,
) -> tuple[bool, str | None]:
    ring = plan.get("footprint_pixels", [])
    if footprint_edge_directions:
        pairs = list(zip(ring, [*ring[1:], ring[0]])) if len(ring) >= 3 else []
        minimum_edge_m = load_core_tolerances().min_edge_length_m
        for edge_index, (axis, direction, original_length_px) in footprint_edge_directions.items():
            if edge_index >= len(pairs):
                return False, f"would remove footprint edge {edge_index}"
            first, second = pairs[edge_index]
            delta = (float(second[0]) - float(first[0]) if axis == "x"
                     else float(second[1]) - float(first[1]))
            perpendicular_delta = (float(second[1]) - float(first[1]) if axis == "x"
                                   else float(second[0]) - float(first[0]))
            if abs(perpendicular_delta) > 1e-9:
                return False, f"would make footprint edge {edge_index} non-orthogonal"
            if delta * direction <= 1e-9:
                return False, f"would collapse or reverse footprint edge {edge_index}"
            if footprint_axis_scales is not None:
                scale = float(footprint_axis_scales[axis])
                if (original_length_px * scale >= minimum_edge_m - 1e-9
                        and abs(delta) * scale < minimum_edge_m - 1e-9):
                    return False, (
                        f"would shrink footprint edge {edge_index} below "
                        f"min_edge_length_m {minimum_edge_m:.6f} m"
                    )
    try:
        footprint = Polygon(ring)
    except (TypeError, ValueError) as error:
        return False, f"moved footprint is invalid: {error}"
    if not footprint.is_valid or footprint.area <= 0 or footprint.interiors:
        return False, "moved footprint is not one valid simple outer ring"
    for partition in plan.get("partitions", []):
        partition_id = str(partition.get("id", "?"))
        points = partition.get("points", [])
        if (orthogonal_partitions is not None and partition_id in orthogonal_partitions
                and any(first[0] != second[0] and first[1] != second[1]
                        for first, second in zip(points, points[1:]))):
            return False, f"would make partition {partition_id} non-orthogonal"
        try:
            line = LineString(points)
        except (TypeError, ValueError):
            continue
        if not footprint.covers(line):
            return False, f"would put partition {partition.get('id', '?')} outside the footprint"
    for opening in plan.get("openings", []):
        opening_id = str(opening.get("id", "?"))
        first, second = opening.get("p1"), opening.get("p2")
        if (orthogonal_openings is not None and opening_id in orthogonal_openings
                and isinstance(first, list) and isinstance(second, list)
                and first[0] != second[0] and first[1] != second[1]):
            return False, f"would make opening {opening_id} non-orthogonal"
        try:
            line = LineString([first, second])
        except (TypeError, ValueError):
            continue
        if not footprint.covers(line):
            return False, f"would put opening {opening.get('id', '?')} outside the footprint"
    for seed in plan.get("space_seeds", []):
        try:
            point = Point(seed.get("point"))
        except (TypeError, ValueError):
            continue
        if not footprint.contains(point):
            return False, f"would put named room seed {seed.get('id', '?')} outside the footprint"
    hosts = _wall_host_segments(plan)
    partitions = {str(row.get("id", "?")): row for row in plan.get("partitions", [])}
    for (partition_id, endpoint_index), required_hosts in (partition_attachments or {}).items():
        partition = partitions.get(partition_id)
        if partition is None:
            return False, f"would remove attached partition {partition_id}"
        points = partition.get("points", [])
        if not points:
            return False, f"would remove the attached endpoint of partition {partition_id}"
        endpoint = points[endpoint_index]
        for host in required_hosts:
            if not any(_point_on_segment(endpoint, first, second)
                       for first, second in hosts.get(host, [])):
                return False, f"would detach partition {partition_id} from {host}"
    openings = {str(row.get("id", "?")): row for row in plan.get("openings", [])}
    for opening_id, required_hosts in (opening_hosts or {}).items():
        opening = openings.get(opening_id)
        if opening is None:
            return False, f"would remove hosted opening {opening_id}"
        first, second = opening.get("p1"), opening.get("p2")
        if not any(any(_point_on_segment(first, start, end)
                           and _point_on_segment(second, start, end)
                       for start, end in hosts.get(host, []))
                   for host in required_hosts):
            return False, f"would detach opening {opening_id} from its declared wall"
    return True, None


def move_straight_wall(plan: dict[str, Any], *, collection: str, identity: str,
                       along_axis: str, old_cross: float, new_cross: float,
                       span: list[float],
                       endpoint_tolerance_px: float = 1e-6) -> dict[str, list[str]]:
    """Move one wall line with objects attached or within junction tolerance."""

    # Reuse one float object for every moved coordinate.  Computing attached
    # coordinates as ``old + delta`` can round to a neighbouring binary float,
    # leaving a visually coincident opening or junction unequal to its host.
    new_cross = float(new_cross)
    cross_index = 1 if along_axis == "x" else 0
    along_index = 1 - cross_index
    moved_openings: list[str] = []
    moved_junctions: list[str] = []
    moved_walls: list[str] = []

    # A footprint edge can terminate at a collinear partition which continues
    # inward from the same corner.  Treat touching collinear declarations as
    # one connected wall line: moving only their shared endpoint would turn the
    # continuation diagonal.  Grow the affected span until the whole connected
    # collinear chain is known, then move every point on that chain together.
    effective_span = [float(span[0]), float(span[1])]
    footprint_point_indices: set[int] | None = None
    if collection == "footprint":
        ring = plan["footprint_pixels"]
        edge_index = int(identity)
        footprint_point_indices = {edge_index, (edge_index + 1) % len(ring)}
        # A perimeter side may be split by collinear vertices or repeated
        # points. Move that connected side atomically so changing one segment
        # cannot turn its neighbour diagonal.
        changed = True
        while changed:
            changed = False
            for first_index in range(len(ring)):
                second_index = (first_index + 1) % len(ring)
                if not ({first_index, second_index} & footprint_point_indices):
                    continue
                first, second = ring[first_index], ring[second_index]
                if (_same_line(first, cross_index=cross_index, cross_at=old_cross)
                        and _same_line(second, cross_index=cross_index, cross_at=old_cross)):
                    before = len(footprint_point_indices)
                    footprint_point_indices.update((first_index, second_index))
                    changed = changed or len(footprint_point_indices) != before
        along_values = [float(ring[index][along_index]) for index in footprint_point_indices]
        effective_span = [min(along_values), max(along_values)]
    connected: list[dict[str, Any]] = []
    pending = list(plan.get("partitions", []))
    while True:
        added = False
        for partition in pending:
            if partition in connected:
                continue
            if collection == "partitions" and str(partition.get("id")) == identity:
                continue
            points = partition.get("points", [])
            if (not isinstance(points, list) or len(points) < 2
                    or not all(isinstance(point, list) and len(point) == 2
                               and _finite(point[cross_index]) and _finite(point[along_index])
                               and abs(float(point[cross_index]) - old_cross) <= 1e-6
                               for point in points)):
                continue
            candidate_span = [min(float(point[along_index]) for point in points),
                              max(float(point[along_index]) for point in points)]
            if (candidate_span[1] < effective_span[0] - 1e-6
                    or candidate_span[0] > effective_span[1] + 1e-6):
                continue
            connected.append(partition)
            effective_span[0] = min(effective_span[0], candidate_span[0])
            effective_span[1] = max(effective_span[1], candidate_span[1])
            added = True
        if not added:
            break

    if collection == "partitions":
        row = next(item for item in plan.get("partitions", []) if item.get("id") == identity)
        for point in row["points"]:
            if _same_line(point, cross_index=cross_index, cross_at=old_cross):
                point[cross_index] = new_cross
    elif collection == "footprint":
        ring = plan["footprint_pixels"]
        assert footprint_point_indices is not None
        for point_index in footprint_point_indices:
            if _same_line(ring[point_index], cross_index=cross_index, cross_at=old_cross):
                ring[point_index][cross_index] = new_cross
    else:
        raise ValueError(f"unsupported wall collection {collection!r}")

    for partition in connected:
        for point in partition["points"]:
            point[cross_index] = new_cross
        moved_walls.append(str(partition.get("id", "?")))

    for opening in plan.get("openings", []):
        points = [opening.get("p1"), opening.get("p2")]
        if (all(isinstance(point, list) and _same_line(
                point, cross_index=cross_index, cross_at=old_cross) for point in points)
                and all(effective_span[0] - 1e-6 <= float(point[along_index])
                        <= effective_span[1] + 1e-6 for point in points)):
            opening["p1"][cross_index] = new_cross
            opening["p2"][cross_index] = new_cross
            moved_openings.append(str(opening.get("id", "?")))
        else:
            for field in ("p1", "p2"):
                point = opening.get(field)
                if (isinstance(point, list) and len(point) == 2
                        and _finite(point[cross_index]) and _finite(point[along_index])
                        and abs(float(point[cross_index]) - old_cross) <= 1e-6
                        and effective_span[0] - 1e-6 <= float(point[along_index])
                        <= effective_span[1] + 1e-6):
                    point[cross_index] = new_cross
                    moved_openings.append(f"{opening.get('id', '?')}:{field}")

    for partition in plan.get("partitions", []):
        if (collection == "partitions" and str(partition.get("id")) == identity
                or partition in connected):
            continue
        points = partition.get("points", [])
        for endpoint_index in (0, len(points) - 1):
            if len(points) < 2:
                continue
            point = points[endpoint_index]
            neighbor = points[1 if endpoint_index == 0 else len(points) - 2]
            if (isinstance(point, list) and len(point) == 2
                    and isinstance(neighbor, list) and len(neighbor) == 2
                    and _finite(point[cross_index]) and _finite(point[along_index])
                    and _finite(neighbor[cross_index]) and _finite(neighbor[along_index])
                    and abs(float(point[along_index]) - float(neighbor[along_index])) <= 1e-6
                    and abs(float(point[cross_index]) - float(neighbor[cross_index])) > 1e-6
                    and abs(float(point[cross_index]) - old_cross) <= 1e-6
                    and effective_span[0] - endpoint_tolerance_px <= float(point[along_index])
                    <= effective_span[1] + endpoint_tolerance_px):
                point[cross_index] = new_cross
                moved_junctions.append(f"{partition.get('id', '?')}:{endpoint_index}")
    return {"walls": sorted(set(moved_walls)), "openings": sorted(set(moved_openings)),
            "junctions": sorted(set(moved_junctions))}


def _append_line_reference(plan: dict[str, Any], *, partition_id: str,
                           basis: str, source_refs: list[str]) -> None:
    inputs = plan.setdefault("regularization_inputs", {})
    rows = inputs.setdefault("line_references", [])
    candidate = {"partition_id": partition_id, "basis": basis,
                 "source_refs": list(dict.fromkeys(source_refs))}
    previous = next((row for row in rows if row.get("partition_id") == partition_id), None)
    rank = {"inferred": 0, "measured": 1, "ink": 2, "dimension": 3, "exterior": 4}
    if previous is None:
        rows.append(candidate)
    elif rank.get(basis, -1) >= rank.get(previous.get("basis"), -1):
        previous.update(candidate)


def _set_reading_alignment(plan: dict[str, Any], key: str, report: Mapping[str, Any]) -> None:
    inputs = plan.setdefault("regularization_inputs", {})
    empty = {"summary": {"status": "not_supplied"}, "items": [], "rejections": [],
             "search_or_tolerance": "not applicable"}
    audit = inputs.setdefault("reading_alignment", {
        "schema_version": "plan_reading_alignment_v1",
        "ink": copy.deepcopy(empty),
        "dimensions": copy.deepcopy(empty),
    })
    items = [*report.get("items", []), *report.get("changes", [])]
    audit[key] = {
        "summary": {"status": report.get("status"), **copy.deepcopy(report.get("summary", {}))},
        "items": copy.deepcopy(items),
        "rejections": copy.deepcopy(report.get("rejections", [])),
        "search_or_tolerance": copy.deepcopy(
            report.get("search", report.get("tolerances", "not reported"))
        ),
    }


def _find_opening_edge(mask: np.ndarray, *, along_axis: str, along_at: float,
                       cross_at: float, along_radius: int, cross_radius: int,
                       max_distance_px: float) -> tuple[float | None, float]:
    height, width = mask.shape
    along_limit = width - 1 if along_axis == "x" else height - 1
    cross_limit = height - 1 if along_axis == "x" else width - 1
    cross_lo = max(0, int(round(cross_at)) - cross_radius)
    cross_hi = min(cross_limit, int(round(cross_at)) + cross_radius)
    first = max(0, int(round(along_at)) - along_radius)
    last = min(along_limit, int(round(along_at)) + along_radius)
    candidates = []
    for coordinate in range(first, last + 1):
        if abs(coordinate - along_at) > max_distance_px + 1e-9:
            continue
        line = (mask[cross_lo:cross_hi + 1, coordinate] if along_axis == "x"
                else mask[coordinate, cross_lo:cross_hi + 1])
        score = float(line.mean()) if line.size else 0.0
        if score >= MIN_OPENING_EDGE_SUPPORT:
            candidates.append((abs(coordinate - along_at), -score, coordinate, score))
    if not candidates:
        return None, 0.0
    _, _, coordinate, score = min(candidates)
    return float(coordinate), float(score)


def _opening_gap_evidence(mask: np.ndarray, *, along_axis: str, first_edge: float,
                          second_edge: float, cross_at: float, along_probe: int,
                          cross_radius: int) -> dict[str, Any]:
    """Require one wall-face row to continue on both sides but stop in the opening.

    A vertical jamb stroke or a door swing can look like a valid endpoint by
    itself.  The pair is usable only when the same nearby wall-face row is
    continuous before and after the two jambs and is absent between them.
    """

    height, width = mask.shape
    along_limit = width - 1 if along_axis == "x" else height - 1
    cross_limit = height - 1 if along_axis == "x" else width - 1
    lo, hi = sorted((int(round(first_edge)), int(round(second_edge))))
    probe = max(4, min(int(along_probe), max(4, int((hi - lo) * 0.30))))
    left_lo, left_hi = max(0, lo - probe), max(0, lo - 2)
    right_lo, right_hi = min(along_limit, hi + 2), min(along_limit, hi + probe)
    gap_lo, gap_hi = max(0, lo + 2), min(along_limit, hi - 2)
    if left_hi < left_lo + 2 or right_hi < right_lo + 2 or gap_hi < gap_lo + 2:
        return {"status": "ambiguous", "reason": "not enough pixels to verify wall sides and opening gap",
                "probe_length_pixels": probe}

    rows = []
    cross_lo = max(0, int(round(cross_at)) - int(cross_radius))
    cross_hi = min(cross_limit, int(round(cross_at)) + int(cross_radius))
    for cross in range(cross_lo, cross_hi + 1):
        if along_axis == "x":
            left = mask[cross, left_lo:left_hi + 1]
            right = mask[cross, right_lo:right_hi + 1]
            gap = mask[cross, gap_lo:gap_hi + 1]
        else:
            left = mask[left_lo:left_hi + 1, cross]
            right = mask[right_lo:right_hi + 1, cross]
            gap = mask[gap_lo:gap_hi + 1, cross]
        left_support = float(left.mean())
        right_support = float(right.mean())
        gap_support = float(gap.mean())
        rows.append((min(left_support, right_support), gap_support, cross,
                     left_support, right_support))
    side_support, gap_support, cross, left_support, right_support = max(
        rows, key=lambda row: (row[0], -row[1])
    )
    detail = {
        "status": "supported",
        "wall_face_cross_pixel": cross,
        "left_continuity_fraction": round(left_support, 4),
        "right_continuity_fraction": round(right_support, 4),
        "middle_gap_ink_fraction": round(gap_support, 4),
        "probe_length_pixels": probe,
    }
    if side_support < MIN_OPENING_SIDE_CONTINUITY:
        detail.update(status="ambiguous",
                      reason="no common wall edge is continuous on both sides of the candidate jamb pair")
    elif gap_support > MAX_OPENING_GAP_SUPPORT:
        detail.update(status="ambiguous",
                      reason="wall ink continues through the candidate pair; no clear opening gap")
    return detail


def _opening_host_spans(plan: Mapping[str, Any], *, along_axis: str, cross_at: float,
                        opening_span: tuple[float, float]) -> list[list[float]]:
    spans = []
    for _, candidate_along, _, candidate_cross, span in _segments(
            plan.get("footprint_pixels", []), closed=True):
        if (candidate_along == along_axis and abs(candidate_cross - cross_at) <= 1e-6
                and span[0] - 1e-6 <= opening_span[0] and span[1] + 1e-6 >= opening_span[1]):
            spans.append(span)
    for partition in plan.get("partitions", []):
        for _, candidate_along, _, candidate_cross, span in _segments(partition.get("points", [])):
            if (candidate_along == along_axis and abs(candidate_cross - cross_at) <= 1e-6
                    and span[0] - 1e-6 <= opening_span[0] and span[1] + 1e-6 >= opening_span[1]):
                spans.append(span)
    return spans


def align_plan_to_ink(image: Image.Image, plan: Mapping[str, Any], *,
                      search_world_m: float = DEFAULT_SEARCH_WORLD_M) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copy aligned to nearby ink plus a complete action/non-action report."""

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL image")
    if not isinstance(plan, Mapping):
        raise TypeError("plan must be an object")
    if not _finite(search_world_m) or not 0 < float(search_world_m) <= 0.30:
        raise ValueError("search_world_m must be in (0, 0.30]")
    result = copy.deepcopy(dict(plan))
    mpp = {axis: _axis_scale(result, axis) for axis in ("x", "y")}
    radii = {axis: max(2, int(math.ceil(float(search_world_m) / mpp[axis]))) for axis in ("x", "y")}
    max_distance_px = {axis: float(search_world_m) / mpp[axis] for axis in ("x", "y")}
    orthogonal_partitions, orthogonal_openings = _orthogonal_ids(result)
    partition_attachments, opening_hosts = _attachment_requirements(result)
    mask = _ink(image)
    items: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    ring = result.get("footprint_pixels", [])
    if isinstance(ring, list) and len(ring) >= 4 and all(isinstance(point, list) for point in ring):
        # Move each exterior edge at most once.  Later edges see prior corner updates.
        for edge_index in range(len(ring)):
            ring = result["footprint_pixels"]
            segment = next((row for row in _segments(ring, closed=True)
                            if row[0] == edge_index), None)
            if segment is None:
                rejection = {
                    "object": f"footprint:{edge_index}", "kind": "perimeter",
                    "action": "rejected",
                    "reason": "perimeter edge is non-orthogonal before ink alignment",
                }
                items.append(rejection)
                rejections.append(rejection)
                continue
            _, along_axis, cross_axis, cross_at, span = segment
            outward = _outward_sign(ring, edge_index, cross_axis)
            target, detail = _choose_wall_line(
                mask, along_axis=along_axis, cross_at=cross_at, span=span,
                radius_px=radii[cross_axis], cross_mpp=mpp[cross_axis],
                convention="outer_face", max_distance_px=max_distance_px[cross_axis],
                outward=outward,
            )
            base = {"object": f"footprint:{edge_index}", "kind": "perimeter",
                    "axis": cross_axis, "convention": "outer_face", **detail}
            if target is None:
                items.append({**base, "action": "not_moved"})
                continue
            before_edge = copy.deepcopy(result)
            edge_directions = _footprint_edge_directions(result)
            moved = move_straight_wall(result, collection="footprint", identity=str(edge_index),
                                       along_axis=along_axis, old_cross=cross_at,
                                       new_cross=target, span=span,
                                       endpoint_tolerance_px=_junction_tolerance_pixels(
                                           mpp[along_axis]))
            safe, reason = _footprint_preserves_declarations(
                result, orthogonal_partitions=orthogonal_partitions,
                orthogonal_openings=orthogonal_openings,
                partition_attachments=partition_attachments, opening_hosts=opening_hosts,
                footprint_edge_directions=edge_directions,
                footprint_axis_scales=mpp,
            )
            if not safe:
                result = before_edge
                rejection = {**base, "action": "rejected", "candidate_pixel": round(target, 4),
                             "reason": reason}
                items.append(rejection)
                rejections.append(rejection)
                continue
            items.append({**base, "action": "moved", "aligned_pixel": round(target, 4),
                          "movement_pixels": round(target - cross_at, 4),
                          "movement_m": round((target - cross_at) * mpp[cross_axis], 6),
                          "moved_with_wall": moved})

    for partition in result.get("partitions", []):
        points = partition.get("points", []) if isinstance(partition, Mapping) else []
        segments = list(_segments(points)) if isinstance(points, list) else []
        if len(segments) != 1:
            items.append({"object": f"partition:{partition.get('id', '?')}", "kind": "partition",
                          "action": "not_moved", "reason": "ink alignment currently requires one straight segment"})
            continue
        _, along_axis, cross_axis, cross_at, span = segments[0]
        target, detail = _choose_wall_line(
            mask, along_axis=along_axis, cross_at=cross_at, span=span,
            radius_px=radii[cross_axis], cross_mpp=mpp[cross_axis],
            convention="centerline", max_distance_px=max_distance_px[cross_axis],
        )
        base = {"object": f"partition:{partition.get('id', '?')}", "kind": "partition",
                "axis": cross_axis, "convention": "centerline", **detail}
        if target is None:
            items.append({**base, "action": "not_moved"})
            continue
        before_wall = copy.deepcopy(result)
        moved = move_straight_wall(result, collection="partitions", identity=str(partition["id"]),
                                   along_axis=along_axis, old_cross=cross_at,
                                   new_cross=target, span=span,
                                   endpoint_tolerance_px=_junction_tolerance_pixels(
                                       mpp[along_axis]))
        safe, reason = _footprint_preserves_declarations(
            result, orthogonal_partitions=orthogonal_partitions,
            orthogonal_openings=orthogonal_openings,
            partition_attachments=partition_attachments, opening_hosts=opening_hosts,
        )
        if not safe:
            result = before_wall
            rejection = {**base, "action": "rejected", "candidate_pixel": round(target, 4),
                         "reason": reason}
            items.append(rejection)
            rejections.append(rejection)
            continue
        _append_line_reference(result, partition_id=str(partition["id"]), basis="ink",
                               source_refs=[*partition.get("source_refs", []), "deterministic nearby-ink alignment"])
        items.append({**base, "action": "moved", "aligned_pixel": round(target, 4),
                      "movement_pixels": round(target - cross_at, 4),
                      "movement_m": round((target - cross_at) * mpp[cross_axis], 6),
                      "moved_with_wall": moved})

    # Align declared opening endpoints to nearby jamb/edge ink, without inventing a gap.
    for opening in result.get("openings", []):
        p1, p2 = opening.get("p1"), opening.get("p2")
        if not (isinstance(p1, list) and isinstance(p2, list) and len(p1) == len(p2) == 2
                and all(_finite(value) for value in [*p1, *p2])):
            continue
        if p1[1] == p2[1]:
            along_axis, along_index, cross_index = "x", 0, 1
        elif p1[0] == p2[0]:
            along_axis, along_index, cross_index = "y", 1, 0
        else:
            rejections.append({"object": f"opening:{opening.get('id', '?')}",
                               "reason": "opening is not orthogonal"})
            continue
        if opening.get("kind") not in {"door", "window"}:
            items.append({"object": f"opening:{opening.get('id', '?')}", "kind": "opening",
                          "axis": along_axis, "action": "not_moved",
                          "reason": "open passages have no door/window jamb edges to align"})
            continue
        cross_axis = "y" if along_axis == "x" else "x"
        original_along = (float(p1[along_index]), float(p2[along_index]))
        host_spans = _opening_host_spans(
            result, along_axis=along_axis, cross_at=float(p1[cross_index]),
            opening_span=tuple(sorted(original_along)),
        )
        if not host_spans:
            rejection = {"object": f"opening:{opening.get('id', '?')}", "kind": "opening",
                         "axis": along_axis, "action": "not_moved",
                         "reason": "aligned opening has no exact declared host line; endpoint ink was not applied"}
            items.append(rejection)
            rejections.append(rejection)
            continue
        moves = []
        targets: list[float | None] = []
        for field, point in (("p1", p1), ("p2", p2)):
            target, support = _find_opening_edge(
                mask, along_axis=along_axis, along_at=float(point[along_index]),
                cross_at=float(point[cross_index]), along_radius=radii[along_axis],
                cross_radius=max(2, int(math.ceil(0.30 / mpp[cross_axis]))),
                max_distance_px=max_distance_px[along_axis],
            )
            if target is None:
                targets.append(None)
                moves.append({"endpoint": field, "action": "not_moved",
                              "reason": "no nearby jamb or opening-edge ink"})
            elif not any(span[0] - 1e-6 <= target <= span[1] + 1e-6 for span in host_spans):
                targets.append(None)
                moves.append({"endpoint": field, "action": "not_moved",
                              "reason": "candidate jamb ink lies beyond the declared host wall"})
            else:
                before = float(point[along_index])
                targets.append(target)
                moves.append({"endpoint": field, "action": "moved",
                              "from_pixel": round(before, 4), "to_pixel": round(target, 4),
                              "movement_m": round((target - before) * mpp[along_axis], 6),
                              "support_fraction": round(support, 4)})
        pair_reason = None
        gap_evidence: dict[str, Any] | None = None
        if any(target is None for target in targets):
            pair_reason = "both jamb endpoints need supported ink; partial endpoint alignment was rejected"
        else:
            assert len(targets) == 2 and targets[0] is not None and targets[1] is not None
            gap_evidence = _opening_gap_evidence(
                mask, along_axis=along_axis, first_edge=targets[0], second_edge=targets[1],
                cross_at=float(p1[cross_index]), along_probe=radii[along_axis],
                cross_radius=max(2, int(math.ceil(0.30 / mpp[cross_axis]))),
            )
            if gap_evidence["status"] != "supported":
                pair_reason = str(gap_evidence["reason"])
        new_delta = (float(targets[1]) - float(targets[0])
                     if pair_reason is None else original_along[1] - original_along[0])
        old_delta = original_along[1] - original_along[0]
        if pair_reason is None and (new_delta == 0 or new_delta * old_delta <= 0):
            pair_reason = "candidate ink edges would invert or collapse the opening"
        if pair_reason is not None:
            moves = [{**row, "action": "not_moved",
                      "reason": pair_reason}
                     for row in moves]
            rejections.append({"object": f"opening:{opening.get('id', '?')}",
                               "reason": f"{pair_reason}; endpoints retained",
                               "gap_evidence": copy.deepcopy(gap_evidence)})
        else:
            p1[along_index] = float(targets[0])
            p2[along_index] = float(targets[1])
        items.append({"object": f"opening:{opening.get('id', '?')}", "kind": "opening",
                      "axis": along_axis, "action": "aligned" if any(row["action"] == "moved" for row in moves)
                      else "not_moved", "endpoints": moves,
                      "gap_evidence": copy.deepcopy(gap_evidence)})

    moved_count = sum(row.get("action") in {"moved", "aligned"} for row in items)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "applied" if moved_count else "unchanged",
        "search": {
            "world_radius_m": float(search_world_m),
            "metres_per_pixel": {axis: round(value, 9) for axis, value in mpp.items()},
            "radius_pixels": radii,
            "basis": "0.30 m maximum alignment distance converted independently on each drawing axis",
        },
        "summary": {"moved_or_aligned": moved_count,
                    "not_moved": sum(row.get("action") == "not_moved" for row in items),
                    "rejected": len(rejections)},
        "items": items,
        "rejections": rejections,
    }
    _set_reading_alignment(result, "ink", report)
    return result, report
