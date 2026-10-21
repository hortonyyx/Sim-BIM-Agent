"""Compare source spaces without treating display/thermal cells as source rooms.

Inputs must already use the same metres, coordinate frame and floor identifiers.
This evaluator compares planar polygons extruded by ``z_floor`` and ``height``;
it does not infer physical wall types, doors, connectivity or false floor slabs.
Tolerance applies to the boundaries of individually matched objects, never to a
buffered union that could erase a narrow room. No input geometry is repaired.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment
from shapely.geometry import Polygon
from shapely.validation import explain_validity

_LENGTH_EPS = 1e-9
_REL_AREA_EPS = 1e-10


@dataclass(frozen=True)
class _Space:
    id: str
    floor_id: str
    polygon: Polygon
    z_floor: float
    height: float


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    reference_ids: list[str] | None = None,
    candidate_ids: list[str] | None = None,
    **evidence: Any,
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "reference_ids": reference_ids or [],
        "candidate_ids": candidate_ids or [],
        "message": message,
        **evidence,
    }


def _validate(spaces: list[dict], side: str) -> tuple[list[_Space], list[dict]]:
    parsed, findings = [], []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(spaces):
        identity = raw.get("id") if isinstance(raw, dict) else None
        ids = {f"{side}_ids": [identity] if isinstance(identity, str) else []}
        try:
            if not isinstance(raw, dict):
                raise ValueError("space must be a mapping")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("id must be a nonempty string")
            floor_id = raw["floor_id"]
            if not isinstance(floor_id, str) or not floor_id.strip():
                raise ValueError("floor_id must be a nonempty string")
            z_floor, height = float(raw["z_floor"]), float(raw["height"])
            if (
                not isfinite(z_floor)
                or not isfinite(height)
                or not isfinite(z_floor + height)
                or height <= 0
            ):
                raise ValueError("z_floor and height must be finite; height must be positive")
            ring = raw["polygon"]
            if not isinstance(ring, (list, tuple)) or len(ring) < 3:
                raise ValueError("polygon requires at least three XY points")
            coordinates = []
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("polygon vertices must be XY pairs")
                xy = tuple(float(value) for value in point)
                if not all(isfinite(value) for value in xy):
                    raise ValueError("polygon coordinates must be finite")
                coordinates.append(xy)
            holes = raw.get('holes', [])
            for hole in holes:
                if (not isinstance(hole, (list, tuple)) or len(hole) < 3
                        or any(not isinstance(p, (list, tuple)) or len(p) != 2
                               or not all(isfinite(float(v)) for v in p) for p in hole)):
                    raise ValueError('holes require finite XY rings')
            polygon = Polygon(coordinates, holes)
            if (
                polygon.is_empty
                or not polygon.is_valid
                or not isfinite(polygon.area)
                or polygon.area <= 0
            ):
                raise ValueError(f"invalid polygon: {explain_validity(polygon)}")
            key = (floor_id, identity)
            if key in seen:
                raise ValueError("duplicate space id on the same floor")
            seen.add(key)
            parsed.append(_Space(identity, floor_id, polygon, z_floor, height))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            findings.append(
                _finding(
                    f"invalid_{side}_space",
                    "severe",
                    str(exc),
                    side=side,
                    input_index=index,
                    **ids,
                )
            )
    return parsed, findings


def _area_epsilon(first: Polygon, second: Polygon) -> float:
    # Relative to the smaller object: a genuinely narrow room is never removed
    # by a fixed minimum area or by an inward buffer.
    return min(first.area, second.area) * _REL_AREA_EPS


def _within_boundary_tolerance(first: Polygon, second: Polygon, tolerance: float) -> bool:
    if first.equals(second):
        return True
    if tolerance == 0:
        return False
    # Buffer boundary lines only to check distances, retaining both source
    # polygons. Unlike a discrete Hausdorff check, this checks entire edges.
    return bool(
        first.boundary.difference(second.boundary.buffer(tolerance + _LENGTH_EPS)).length
        <= _LENGTH_EPS
        and second.boundary.difference(first.boundary.buffer(tolerance + _LENGTH_EPS)).length
        <= _LENGTH_EPS
    )


def _overlaps(spaces: list[_Space], side: str, tolerance: float) -> list[dict]:
    findings = []
    for i, first in enumerate(spaces):
        for second in spaces[i + 1 :]:
            if first.floor_id != second.floor_id:
                continue
            vertical_overlap = min(
                first.z_floor + first.height, second.z_floor + second.height
            ) - max(first.z_floor, second.z_floor)
            if vertical_overlap <= _LENGTH_EPS:
                continue
            overlap = first.polygon.intersection(second.polygon)
            epsilon = _area_epsilon(first.polygon, second.polygon)
            if overlap.area <= epsilon:
                continue
            fraction = overlap.area / min(first.polygon.area, second.polygon.area)
            # A duplicate/contained thin space is severe even when all of its
            # area happens to lie in another object's tolerance strip.
            substantial = fraction >= 0.5 or (
                overlap.difference(first.polygon.boundary.buffer(tolerance)).area > epsilon
                and overlap.difference(second.polygon.boundary.buffer(tolerance)).area > epsilon
            )
            findings.append(
                _finding(
                    f"{side}_spaces_overlap",
                    "severe" if substantial else "minor",
                    "Source spaces overlap in volume."
                    if substantial
                    else "Source spaces have a boundary overlap within tolerance.",
                    **{f"{side}_ids": [first.id, second.id]},
                    floor_id=first.floor_id,
                    overlap_area_m2=float(overlap.area),
                    smaller_space_overlap_fraction=float(fraction),
                    vertical_overlap_m=float(vertical_overlap),
                )
            )
    return findings


def compare_partitions(
    reference: list[dict], candidate: list[dict], *, tolerance_m: float = 0.02
) -> dict:
    """Return geometric correspondence, defects and explicitly untested scope.

    Exact geometry (including reordered/subdivided edges and renamed room IDs)
    passes. Each object must have a one-to-one geometric counterpart; boundary
    or vertical changes within ``tolerance_m`` are minor. Larger changes,
    missing/extra objects and source splits/merges are severe. Floor identifiers
    are assumed to have been aligned by the caller, not matched by their names.

    An absent/invalid reference is ``not_evaluated``. Invalid candidate data is
    severe with a usable reference. Overlapping reference spaces that cannot be
    explained by the boundary tolerance also prevent a comparative verdict.
    Raw intersections support split/merge evidence; total count/area alone are
    never used to establish that the partition is correct.
    """
    tolerance_m = float(tolerance_m)
    if not isfinite(tolerance_m) or tolerance_m < 0:
        raise ValueError("tolerance_m must be finite and nonnegative")
    refs, ref_findings = _validate(reference, "reference")
    cands, cand_findings = _validate(candidate, "candidate")
    ref_findings.extend(_overlaps(refs, "reference", tolerance_m))
    cand_findings.extend(_overlaps(cands, "candidate", tolerance_m))
    findings = ref_findings + cand_findings
    result = {
        "schema_version": 1,
        "status": "not_evaluated",
        "tolerance_m": tolerance_m,
        "reference_count": len(reference),
        "candidate_count": len(candidate),
        "matched_count": 0,
        "matches": [],
        "findings": findings,
        "evaluated": [],
        "not_evaluated": [
            "doors", "openings", "connectivity", "false_slabs", "physical_wall_types"
        ],
    }
    if not reference or any(item["severity"] == "severe" for item in ref_findings):
        findings.append(
            _finding(
                "reference_unavailable" if not reference else "reference_unusable",
                "info",
                "No usable reference partition; candidate correctness is not established.",
            )
        )
        return result
    result["evaluated"] = [
        "source_space_correspondence",
        "partition_geometry",
        "floor_assignment",
        "vertical_extent",
    ]

    intersections = np.zeros((len(refs), len(cands)), dtype=float)
    scores = np.zeros_like(intersections)
    near = np.zeros_like(intersections, dtype=bool)
    for i, ref in enumerate(refs):
        for j, cand in enumerate(cands):
            intersection = float(ref.polygon.intersection(cand.polygon).area)
            intersections[i, j] = intersection
            near[i, j] = _within_boundary_tolerance(ref.polygon, cand.polygon, tolerance_m)
            if intersection <= _area_epsilon(ref.polygon, cand.polygon) and not near[i, j]:
                continue
            iou = intersection / (ref.polygon.area + cand.polygon.area - intersection)
            # Geometry dominates identity; the same floor/vertical position
            # breaks ties for rooms stacked above one another. IDs are unused.
            scores[i, j] = iou + 4 * near[i, j] + float(ref.floor_id == cand.floor_id)
            scores[i, j] += 0.25 / (1 + abs(ref.z_floor - cand.z_floor))

    ref_matches, cand_matches = {}, {}
    near_pairs: set[tuple[int, int]] = set()
    rows, columns = linear_sum_assignment(scores, maximize=True)
    for row, column in zip(rows, columns, strict=True):
        i, j = int(row), int(column)
        if scores[i, j] <= 0:
            continue
        ref, cand = refs[i], cands[j]
        ref_matches[i], cand_matches[j] = j, i
        intersection = float(intersections[i, j])
        floor_changed = ref.floor_id != cand.floor_id
        z_delta = abs(ref.z_floor - cand.z_floor)
        height_delta = abs(ref.height - cand.height)
        # Compare top elevations as well: two individually small deviations
        # can otherwise combine into a materially shifted upper boundary.
        top_delta = abs(ref.z_floor + ref.height - cand.z_floor - cand.height)
        vertical_changed = max(z_delta, height_delta, top_delta) > tolerance_m + _LENGTH_EPS
        if near[i, j] and not floor_changed and not vertical_changed:
            near_pairs.add((i, j))
        exact = ref.polygon.equals(cand.polygon) and max(z_delta, height_delta) <= _LENGTH_EPS
        if floor_changed or vertical_changed or not near[i, j]:
            status = "severe"
        else:
            status = "pass" if exact else "minor"
        match = {
            "reference_id": ref.id,
            "candidate_id": cand.id,
            "reference_floor_id": ref.floor_id,
            "candidate_floor_id": cand.floor_id,
            "status": status,
            "intersection_area_m2": intersection,
            "reference_coverage_fraction": intersection / ref.polygon.area,
            "candidate_coverage_fraction": intersection / cand.polygon.area,
            "iou": intersection / (ref.polygon.area + cand.polygon.area - intersection),
            "symmetric_difference_area_m2": float(
                ref.polygon.symmetric_difference(cand.polygon).area
            ),
            "boundary_hausdorff_m": float(
                ref.polygon.boundary.hausdorff_distance(cand.polygon.boundary)
            ),
            "boundary_within_tolerance": bool(near[i, j]),
            "z_floor_delta_m": z_delta,
            "height_delta_m": height_delta,
            "z_top_delta_m": top_delta,
        }
        result["matches"].append(match)
        evidence = {"reference_ids": [ref.id], "candidate_ids": [cand.id], "match": match}
        if floor_changed:
            findings.append(
                _finding(
                    "floor_assignment_changed", "severe",
                    "Matched space moved to a different floor identifier.",
                    **evidence,
                )
            )
        if vertical_changed:
            findings.append(
                _finding(
                    "vertical_extent_changed", "severe",
                    "Matched space has a vertical extent change beyond tolerance.",
                    **evidence,
                )
            )
        if not near[i, j]:
            findings.append(
                _finding(
                    "partition_boundary_changed", "severe",
                    "Matched source space boundaries differ beyond tolerance.",
                    **evidence,
                )
            )
        elif status == "minor":
            findings.append(
                _finding(
                    "space_geometry_within_tolerance", "minor",
                    "Matched source space differs within the declared tolerance.",
                    **evidence,
                )
            )

    for i, ref in enumerate(refs):
        if i not in ref_matches:
            findings.append(
                _finding(
                    "missing_source_space", "severe",
                    "Reference space has no one-to-one candidate counterpart.",
                    reference_ids=[ref.id], floor_id=ref.floor_id,
                    area_m2=float(ref.polygon.area),
                )
            )
    for j, cand in enumerate(cands):
        if j not in cand_matches:
            findings.append(
                _finding(
                    "extra_source_space", "severe",
                    "Candidate space has no one-to-one reference counterpart.",
                    candidate_ids=[cand.id], floor_id=cand.floor_id,
                    area_m2=float(cand.polygon.area),
                )
            )

    # Splits need substantial coverage of each candidate fragment; merges need
    # substantial coverage of each reference room. Relative fractions preserve
    # even a millimetre-wide fragment while rejecting ordinary boundary slivers.
    # Skip cross-links between independently matched near-equivalent objects.
    def relation_allowed(i: int, j: int) -> bool:
        if refs[i].floor_id != cands[j].floor_id:
            return False
        if intersections[i, j] <= _area_epsilon(refs[i].polygon, cands[j].polygon):
            return False
        return not (
            (i, ref_matches.get(i)) in near_pairs
            and (cand_matches.get(j), j) in near_pairs
            and ref_matches.get(i) != j
        )

    for i, ref in enumerate(refs):
        fragments = [
            j for j, cand in enumerate(cands)
            if relation_allowed(i, j)
            and intersections[i, j] / cand.polygon.area >= 0.5
        ]
        if len(fragments) > 1:
            findings.append(
                _finding(
                    "source_space_split", "severe",
                    "Multiple candidate source spaces substantially cover one reference space.",
                    reference_ids=[ref.id],
                    candidate_ids=[cands[j].id for j in fragments],
                    floor_id=ref.floor_id,
                    overlaps=[
                        {
                            "candidate_id": cands[j].id,
                            "intersection_area_m2": float(intersections[i, j]),
                            "candidate_coverage_fraction": float(
                                intersections[i, j] / cands[j].polygon.area
                            ),
                        }
                        for j in fragments
                    ],
                )
            )
    for j, cand in enumerate(cands):
        rooms = [
            i for i, ref in enumerate(refs)
            if relation_allowed(i, j)
            and intersections[i, j] / ref.polygon.area >= 0.5
        ]
        if len(rooms) > 1:
            findings.append(
                _finding(
                    "source_spaces_merged", "severe",
                    "One candidate source space substantially covers multiple reference spaces.",
                    reference_ids=[refs[i].id for i in rooms],
                    candidate_ids=[cand.id], floor_id=cand.floor_id,
                    overlaps=[
                        {
                            "reference_id": refs[i].id,
                            "intersection_area_m2": float(intersections[i, j]),
                            "reference_coverage_fraction": float(
                                intersections[i, j] / refs[i].polygon.area
                            ),
                        }
                        for i in rooms
                    ],
                )
            )

    result["matched_count"] = len(result["matches"])
    severities = {item["severity"] for item in findings}
    result["status"] = (
        "severe" if "severe" in severities else "minor" if "minor" in severities else "pass"
    )
    return result
