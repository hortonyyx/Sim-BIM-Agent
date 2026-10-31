"""Materialize a checked 2D plan only after assembly has resolved its levels.

Reader observations stay immutable. Elevation identities use the existing
ordered matcher and its thresholds; unknown heights become explicit assembly
assumptions, never retrospective reading observations.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping

from shapely.geometry import LineString

from src.agent.geometry.plan_partition import compile_plan_partition

from .elevation import (
    _expected_world_sign, _ordered_assignment, _pixel_to_world,
    match_elevation, validate_elevation_artifact,
)
from .levels import LEVEL_TOLERANCE_M, decision


class PlanMaterializationError(ValueError):
    """A located assembly conflict; no partially materialized plan is returned."""

    def __init__(self, message, *, decisions):
        super().__init__(message)
        self.decisions = copy.deepcopy(decisions)


def _facade(host, ring):
    if not host["exterior"]:
        return None
    line = LineString([host["p1_world_m"], host["p2_world_m"]])
    edges = [(a, b) for a, b in zip(ring, ring[1:] + ring[:1])
             if LineString([a, b]).covers(line)]
    if len(edges) != 1:
        raise ValueError(f"opening {host['opening_id']} must lie on one complete footprint edge")
    first, second = edges[0]
    # compile_plan_partition supplies a CCW ring: its interior is on the left.
    if first[1] == second[1]:
        return "South" if second[0] > first[0] else "North"
    return "East" if second[1] > first[1] else "West"


def _heights_agree(intervals):
    # Reuse the existing cross-view vertical-level consistency policy. Retain
    # one actual observation and every citation; never average observed values.
    return all(max(z[axis] for z in intervals) - min(z[axis] for z in intervals)
               <= LEVEL_TOLERANCE_M + 1e-9 for axis in (0, 1))


def materialize_plan_reading(plan, *, elevations: Mapping, image_size, image_name):
    """Return a strict 3D plan copy and located assembly decisions.

    ``plan`` must have ``reading_type='plan'`` plus explicit ``z_floor`` and
    ``ceiling_height`` already resolved by assembly. ``elevations`` maps task
    IDs to accepted elevation artifacts. Existing artifact validation checks
    their integrity again; this function does not accept a replacement image
    or change any XY geometry.
    """
    if not isinstance(plan, dict) or plan.get("reading_type") != "plan":
        raise ValueError("materialize_plan_reading requires reading_type='plan'")
    if any(field not in plan for field in ("z_floor", "ceiling_height")):
        raise ValueError("assembly must resolve explicit z_floor and ceiling_height before materialization")
    if not isinstance(elevations, Mapping):
        raise TypeError("elevations must map accepted task IDs to artifacts")
    if any(not isinstance(task_id, str) or not task_id.strip() for task_id in elevations):
        raise ValueError("elevations task IDs must be nonempty strings")

    result = copy.deepcopy(plan)
    _, metadata = compile_plan_partition(result, image_size=image_size, image_name=image_name)
    floor_id = result["floor_id"]
    z_floor = float(result["z_floor"])
    z_top = z_floor + float(result["ceiling_height"])
    decisions = []

    def reject(reason, **details):
        decisions.append(decision("opening_height_materialization", reason,
            "Resolve the cited assembly conflict before creating source BIM.",
            floor_id=floor_id, status="conflict", **details))
        raise PlanMaterializationError(reason, decisions=decisions)

    # Use the matcher defaults themselves, so this preparatory path cannot
    # silently establish a second position/width acceptance policy.
    tolerances = {field: match_elevation.__kwdefaults__[field]
                  for field in ("position_tolerance_m", "width_tolerance_m")}
    host_by_id = {row["opening_id"]: row for row in metadata["opening_hosts"]}
    facade_by_id = {identity: _facade(host, metadata["footprint"]["world_polygon_m"])
                    for identity, host in host_by_id.items()}
    source_groups = defaultdict(list)
    for identity, host in host_by_id.items():
        facade = facade_by_id[identity]
        if facade is None or host["kind"] not in {"door", "window"}:
            continue
        axis = 0 if facade in {"North", "South"} else 1
        source_groups[(facade, host["kind"])].append({
            "source_id": identity, "width_m": host["width_m"],
            "world_coordinate_m": (host["p1_world_m"][axis] + host["p2_world_m"][axis]) / 2,
        })

    matches = defaultdict(list)
    for task_id, raw in sorted(elevations.items()):
        artifact = validate_elevation_artifact(raw)
        facade = artifact["orientation"]
        sign = _expected_world_sign(facade, artifact["view_direction"])
        observed_groups = defaultdict(list)
        for opening in artifact["openings"]:
            if opening["floor_id"] == floor_id:
                observed_groups[opening["kind"]].append({
                    **opening,
                    "world_coordinate_m": _pixel_to_world(
                        artifact["x_calibration"], sum(opening["x_px"]) / 2),
                })
        for kind in sorted(set(observed_groups) | {
                kind for orientation, kind in source_groups if orientation == facade}):
            left = sorted(observed_groups[kind], key=lambda row: sign * row["world_coordinate_m"])
            right = sorted(source_groups[(facade, kind)],
                           key=lambda row: (sign * row["world_coordinate_m"], row["source_id"]))
            pairs, only_left, only_right, ambiguous = _ordered_assignment(left, right, **tolerances)
            # Coincident projections have no unique source order, including the
            # equal-count fast path in the existing ordered matcher.
            ambiguous |= len({row["world_coordinate_m"] for row in right}) != len(right)
            for i, j in pairs:
                observed, actual = left[i], right[j]
                position_difference = observed["world_coordinate_m"] - actual["world_coordinate_m"]
                width_difference = observed["width_m"] - actual["width_m"]
                reference = {
                    "task_id": task_id, "artifact_sha256": artifact["artifact_sha256"],
                    "image": artifact["image"], "elevation_opening_id": observed["id"],
                    "evidence_type": observed["evidence_type"], "bbox": observed["bbox"],
                    "z_m": [observed["sill_m"], observed["head_m"]],
                    "position_difference_m": position_difference,
                    "width_difference_m": width_difference,
                }
                safe = (not ambiguous
                        and abs(position_difference) <= tolerances["position_tolerance_m"]
                        and abs(width_difference) <= tolerances["width_tolerance_m"])
                if safe:
                    matches[actual["source_id"]].append(reference)
                else:
                    decisions.append(decision("elevation_match_unresolved",
                        "Ambiguous order or position/width disagreement prevents a unique elevation identity.",
                        "Review the corresponding plan and elevation openings.",
                        floor_id=floor_id, opening_id=actual["source_id"], status="unresolved",
                        facade=facade, ambiguous=ambiguous, references=[reference],
                        tolerances_m=tolerances))
            if only_left or only_right:
                decisions.append(decision("elevation_match_unresolved",
                    "The ordered inventories have unmatched openings.",
                    "Review the unmatched openings; any generated height remains an assembly assumption.",
                    floor_id=floor_id, facade=facade, opening_kind=kind, status="unresolved", task_id=task_id,
                    elevation_opening_ids=[left[i]["id"] for i in only_left],
                    plan_opening_ids=[right[j]["source_id"] for j in only_right]))

    # The legacy compiler requires explicit source references. Binding omitted
    # ones locates declarations in their input image without claiming verification.
    for partition in result["partitions"]:
        partition.setdefault("source_refs", [f"image:{image_name}#{partition['id']}"])
    for opening in result["openings"]:
        identity = opening["id"]
        opening.setdefault("source_refs", [f"image:{image_name}#{identity}"])
        references = matches[identity]
        previous_z = copy.deepcopy(opening.get("z"))
        if references:
            z = list(references[0]["z_m"])
            if not _heights_agree([row["z_m"] for row in references]):
                reject(f"opening {identity}: accepted elevation views disagree on absolute height",
                       opening_id=identity, references=references)
            if previous_z is not None and not _heights_agree(
                    [previous_z, *[row["z_m"] for row in references]]):
                reject(f"opening {identity}: observed plan height conflicts with accepted elevation height",
                       opening_id=identity, previous_z_m=previous_z, references=references)
            status = "elevation"
            reason = "A unique ordered identity supplies absolute height from accepted elevation evidence."
            for reference in references:
                opening["source_refs"].extend([
                    f"reader:{reference['task_id']}/opening:{reference['elevation_opening_id']}",
                    f"artifact:{reference['artifact_sha256']}#{reference['elevation_opening_id']}",
                ])
            opening["source_refs"] = list(dict.fromkeys(opening["source_refs"]))
        elif previous_z is not None:
            z = previous_z
            status = "reader_observation"
            reason = "Retain the explicit plan height; no accepted elevation identity replaces it."
        else:
            if opening["kind"] == "door":
                z = [z_floor, z_floor + 2.1]
                rule = "door height 2.1 m above resolved floor"
            elif opening["kind"] == "window":
                z = [z_floor + 0.9, z_floor + 2.4]
                rule = "window sill/head 0.9/2.4 m above resolved floor"
            else:
                z = [z_floor, z_top]
                rule = "open passage spans the resolved floor height"
            status = "assembly_assumption"
            reason = "No explicit plan height or uniquely matched accepted elevation height is available."
            assumption = (f"Assembly assumption for {identity}: {rule}; absolute Z {z} m. "
                          "This is not a plan-reading observation.")
            opening.setdefault("assumptions", []).append(assumption)
            result["assumptions"].append(assumption)
        if not z_floor <= z[0] < z[1] <= z_top:
            reject(f"opening {identity}: proposed height {z} does not fit floor bounds [{z_floor}, {z_top}]; no clipping",
                   opening_id=identity, proposed_z_m=z, height_source=status, references=references)
        opening["z"] = list(z)
        decisions.append(decision("opening_height_materialization", reason,
            "Retain the cited height evidence." if status != "assembly_assumption"
            else "Review this explicit assembly assumption if height evidence becomes available.",
            floor_id=floor_id, opening_id=identity, facade=facade_by_id[identity],
            status=status, z_m=list(z), previous_z_m=previous_z,
            references=references, source_refs=copy.deepcopy(opening["source_refs"])))

    result.pop("reading_type")
    compile_plan_partition(result, image_size=image_size, image_name=image_name)
    return result, decisions
