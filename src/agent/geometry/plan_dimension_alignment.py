"""Validate model-read plan dimension chains and apply annotation-first alignment."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from src.agent.geometry.plan_ink_alignment import (
    _attachment_requirements,
    _append_line_reference,
    _axis_scale,
    _finite,
    _footprint_edge_directions,
    _footprint_preserves_declarations,
    _junction_tolerance_pixels,
    _orthogonal_ids,
    _segments,
    _set_reading_alignment,
    move_straight_wall,
)


SCHEMA_VERSION = "plan_reading_dimension_alignment_v1"
MAX_ALIGNMENT_WORLD_M = 0.30
MAX_CLOSURE_ERROR_MM = 1.0
MAX_CHAIN_CROSS_CHECK_M = MAX_CLOSURE_ERROR_MM / 1000.0
MAX_CHAIN_SCALE_RELATIVE_ERROR = 0.01


def _world(anchors: list[list[float]], pixel: float) -> float:
    (p0, w0), (p1, w1) = anchors
    return float(w0) + (float(pixel) - float(p0)) * (float(w1) - float(w0)) / (float(p1) - float(p0))


def _chain_shape(raw: object, index: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = f"dimension_chains[{index}]"
    if not isinstance(raw, Mapping):
        return None, {"chain_id": f"index:{index}", "reason": f"{path} must be an object"}
    required = {"id", "axis", "segments_mm", "tick_pixels", "source_refs"}
    allowed = required | {"start_world_m", "total_mm"}
    unknown = sorted(set(raw) - allowed)
    missing = sorted(required - set(raw))
    chain_id = raw.get("id", f"index:{index}")
    if unknown or missing:
        return None, {"chain_id": chain_id, "reason": f"fields missing={missing}, unknown={unknown}"}
    if not isinstance(chain_id, str) or not chain_id.strip():
        return None, {"chain_id": f"index:{index}", "reason": "id must be nonempty text"}
    axis = raw.get("axis")
    segments = raw.get("segments_mm")
    ticks = raw.get("tick_pixels")
    total = raw.get("total_mm")
    refs = raw.get("source_refs")
    start_world = raw.get("start_world_m")
    if axis not in {"x", "y"}:
        return None, {"chain_id": chain_id, "reason": "axis must be x or y"}
    # Axis already identifies the measured component. Accept the original [x,y]
    # ticks as well as scalars, preserving raw declarations in the trial audit.
    if isinstance(ticks, list):
        ticks = [value[0 if axis == 'x' else 1]
                 if isinstance(value, (list, tuple)) and len(value) == 2
                 and all(_finite(part) for part in value) else value for value in ticks]
    if (not isinstance(segments, list) or not segments
            or any(not _finite(value) or float(value) <= 0 for value in segments)):
        return None, {"chain_id": chain_id, "reason": "segments_mm must be positive finite millimetres"}
    if "total_mm" not in raw:
        total = math.fsum(segments)
    if (not isinstance(ticks, list) or len(ticks) != len(segments) + 1
            or any(not _finite(value) for value in ticks)):
        return None, {"chain_id": chain_id,
                      "reason": "tick_pixels must contain one numeric axis pixel more than segments_mm"}
    if not _finite(total) or float(total) <= 0:
        return None, {"chain_id": chain_id, "reason": "total_mm must be positive finite millimetres"}
    if not isinstance(refs, list) or not refs or any(not isinstance(value, str) or not value.strip() for value in refs):
        return None, {"chain_id": chain_id, "reason": "source_refs must contain evidence text"}
    if "start_world_m" in raw and not _finite(start_world):
        return None, {"chain_id": chain_id, "reason": "start_world_m must be a finite world coordinate"}
    if any(float(second) <= float(first) for first, second in zip(ticks, ticks[1:])) and any(
            float(second) >= float(first) for first, second in zip(ticks, ticks[1:])):
        return None, {"chain_id": chain_id, "reason": "tick_pixels must be strictly monotonic"}
    chain = {
        "id": chain_id,
        "axis": axis,
        "segments_mm": [float(value) for value in segments],
        "total_mm": float(total),
        "tick_pixels": [float(value) for value in ticks],
        "source_refs": list(refs),
    }
    if "start_world_m" in raw:
        chain["start_world_m"] = float(start_world)
    return chain, None


def _check_chain(chain: Mapping[str, Any], *, closure_tolerance_mm: float = MAX_CLOSURE_ERROR_MM) -> dict[str, Any]:
    segments = chain["segments_mm"]
    ticks = chain["tick_pixels"]
    total = chain["total_mm"]
    segment_sum = math.fsum(segments)
    closure = segment_sum - total
    cumulative = [0.0]
    for value in segments:
        cumulative.append(math.fsum((cumulative[-1], value)))
    pixel_span = ticks[-1] - ticks[0]
    predicted = [ticks[0] + pixel_span * value / total for value in cumulative]
    residual_px = [ticks[index] - predicted[index] for index in range(len(ticks))]
    mm_per_pixel = total / abs(pixel_span) if pixel_span else math.inf
    residual_mm = [value * mm_per_pixel for value in residual_px]
    segment_pixel_mm = [abs(ticks[index + 1] - ticks[index]) * mm_per_pixel
                        for index in range(len(segments))]
    segment_residual_mm = [segment_pixel_mm[index] - segments[index]
                           for index in range(len(segments))]
    pixel_tolerance = max(2.0, 0.01 * abs(pixel_span))
    closure_ok = abs(closure) <= closure_tolerance_mm
    scale_ok = bool(pixel_span) and max(map(abs, residual_px), default=0.0) <= pixel_tolerance
    worst_segment = max(range(len(segments)), key=lambda index: abs(segment_residual_mm[index]))
    return {
        "chain_id": chain["id"],
        "axis": chain["axis"],
        "segment_sum_mm": round(segment_sum, 6),
        "total_mm": total,
        "closure_error_mm": round(closure, 6),
        "pixel_span": round(pixel_span, 6),
        "mm_per_pixel_from_chain": None if not math.isfinite(mm_per_pixel) else round(mm_per_pixel, 6),
        "tick_residual_pixels": [round(value, 6) for value in residual_px],
        "tick_residual_mm": [None if not math.isfinite(value) else round(value, 3) for value in residual_mm],
        "pixel_residual_tolerance": round(pixel_tolerance, 6),
        "segment_pixel_residual_mm": [None if not math.isfinite(value) else round(value, 3)
                                      for value in segment_residual_mm],
        "worst_segment_index": worst_segment,
        "closure_ok": closure_ok,
        "scale_ok": scale_ok,
        "accepted": closure_ok and scale_ok,
    }


def _footprint_span(plan: Mapping[str, Any], axis: str) -> tuple[float, float]:
    index = 0 if axis == "x" else 1
    values = [float(point[index]) for point in plan.get("footprint_pixels", [])
              if isinstance(point, list) and len(point) == 2 and _finite(point[index])]
    if len(values) < 4:
        raise ValueError("dimension alignment needs a numeric footprint_pixels ring")
    return min(values), max(values)


def _overall_direction(chain: Mapping[str, Any], footprint: tuple[float, float],
                       endpoint_tolerance_px: float) -> int | None:
    first, last = chain["tick_pixels"][0], chain["tick_pixels"][-1]
    lo, hi = footprint
    if abs(first - lo) <= endpoint_tolerance_px and abs(last - hi) <= endpoint_tolerance_px:
        return 1
    if abs(first - hi) <= endpoint_tolerance_px and abs(last - lo) <= endpoint_tolerance_px:
        return -1
    return None


def _append_coordinate_references(plan: dict[str, Any], chain: Mapping[str, Any],
                                  coordinates: list[float]) -> None:
    rows = plan.setdefault("regularization_inputs", {}).setdefault("coordinate_references", [])
    for index, coordinate in enumerate(coordinates):
        row = {"axis": chain["axis"], "value_m": round(coordinate, 9), "basis": "dimension",
               "chain_id": chain["id"], "tick_index": index,
               "source_refs": list(chain["source_refs"])}
        if not any(existing.get("axis") == row["axis"]
                   and abs(float(existing.get("value_m", math.inf)) - row["value_m"]) <= 1e-9
                   and existing.get("chain_id") == row["chain_id"]
                   and existing.get("tick_index") == index for existing in rows):
            rows.append(row)


def _associate_tick_wall_coordinates(observed: Mapping[str, Any], *, axis: str,
                                     ticks: list[dict[str, Any]]) -> None:
    """Find each tick's nearest original wall coordinate, not a wall identity.

    Collinear segments share a coordinate. Distinct coordinates compete even
    when their wall spans are far apart: a one-dimensional tick cannot justify
    pulling both onto the same line. Include all partition segments, including
    ones that cannot themselves be moved by the straight-wall operation.
    """
    expected_along = "y" if axis == "x" else "x"
    segments = [*_segments(observed.get("footprint_pixels", []), closed=True),
                *(segment for wall in observed.get("partitions", [])
                  for segment in _segments(wall.get("points", [])))]
    coordinates = sorted({cross for _, along, _, cross, span in segments
                          if along == expected_along and span[1] > span[0]})
    for tick in ticks:
        distance = min((abs(cross - tick["observed_pixel"]) for cross in coordinates),
                       default=math.inf)
        tick["nearest_wall_pixels"] = [
            cross for cross in coordinates
            if abs(abs(cross - tick["observed_pixel"]) - distance) < 1e-7
        ]


def _wall_tick(original_cross: float, ticks: list[dict[str, Any]], mpp: float):
    """Require reciprocal unique proximity in the original observed frame.

    This is a coordinate association, not semantic proof of the measured wall.
    If the nearest tick belongs to another coordinate, do not try a farther
    tick: it may describe a window jamb or a different part of the building.
    """
    nearest = min(ticks, key=lambda row: (abs(row["observed_pixel"] - original_cross),
                                         row["chain"]["id"], row["tick_index"]))
    distance = abs(nearest["observed_pixel"] - original_cross)
    if distance * mpp >= MAX_ALIGNMENT_WORLD_M:
        return None, None
    tied = [row for row in ticks if abs(abs(row["observed_pixel"] - original_cross) - distance) < 1e-7]
    detail = {
        "kind": "wall_tick_association", "action": "not_applied",
        "basis": "observed_nearest_coordinate", "chain_id": nearest["chain"]["id"],
        "tick_index": nearest["tick_index"], "observed_wall_pixel": original_cross,
        "observed_tick_pixel": nearest["observed_pixel"],
        "nearest_wall_pixels": nearest["nearest_wall_pixels"],
    }
    if any(abs(row["target_pixel"] - nearest["target_pixel"]) * mpp > MAX_CHAIN_CROSS_CHECK_M
           for row in tied):
        # Proximity cannot choose between two different annotated wall positions.
        return None, {**detail, "reason": "nearest ticks propose conflicting coordinates"}
    for row in tied:
        owners = row["nearest_wall_pixels"]
        if len(owners) != 1 or abs(owners[0] - original_cross) >= 1e-7:
            return None, {
                **detail, "chain_id": row["chain"]["id"], "tick_index": row["tick_index"],
                "observed_tick_pixel": row["observed_pixel"], "nearest_wall_pixels": owners,
                "reason": ("nearest tick has ambiguous wall coordinates" if len(owners) != 1
                           else "nearest tick is closer to another observed wall coordinate"),
            }
    return nearest, None


def _snap_partitions(plan: dict[str, Any], *, observed: Mapping[str, Any], axis: str,
                     ticks: list[dict[str, Any]],
                     mpp: float) -> tuple[list[dict[str, Any]],
                                        list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    associations_not_applied: list[dict[str, Any]] = []
    # A vertical wall has an x coordinate; a horizontal wall has a y coordinate.
    expected_along = "y" if axis == "x" else "x"
    partition_ids = [str(row.get("id")) for row in plan.get("partitions", [])]
    for partition_id in partition_ids:
        partition = next(row for row in plan.get("partitions", [])
                         if str(row.get("id")) == partition_id)
        segments = list(_segments(partition.get("points", [])))
        if len(segments) != 1:
            continue
        _, along_axis, _, old_cross, span = segments[0]
        if along_axis != expected_along:
            continue
        original = next(row for row in observed.get("partitions", [])
                        if str(row.get("id")) == partition_id)
        original_cross = list(_segments(original["points"]))[0][3]
        tick, association = _wall_tick(original_cross, ticks, mpp)
        if tick is None:
            if association is not None:
                associations_not_applied.append({**association, "object": f"partition:{partition_id}",
                                                 "axis": axis})
            continue
        target, chain = tick["target_pixel"], tick["chain"]
        distance_m = abs(target - old_cross) * mpp
        if distance_m >= MAX_ALIGNMENT_WORLD_M:
            continue
        before_wall = copy.deepcopy(plan)
        orthogonal_partitions, orthogonal_openings = _orthogonal_ids(plan)
        partition_attachments, opening_hosts = _attachment_requirements(plan)
        attached = move_straight_wall(
            plan, collection="partitions", identity=partition_id,
            along_axis=along_axis, old_cross=old_cross, new_cross=target, span=span,
            endpoint_tolerance_px=_junction_tolerance_pixels(_axis_scale(plan, along_axis)),
        )
        base = {
            "object": f"partition:{partition_id}",
            "kind": "partition",
            "axis": axis,
            "from_pixel": round(old_cross, 6),
            "to_pixel": round(target, 6),
            "movement_m": round((target - old_cross) * mpp, 6),
            "chain_id": chain["id"],
            "observed_wall_pixel": original_cross,
            "observed_tick_pixel": tick["observed_pixel"],
            "tick_index": tick["tick_index"],
            "basis": "dimension",
        }
        safe, reason = _footprint_preserves_declarations(
            plan,
            orthogonal_partitions=orthogonal_partitions,
            orthogonal_openings=orthogonal_openings,
            partition_attachments=partition_attachments,
            opening_hosts=opening_hosts,
        )
        if not safe:
            plan.clear()
            plan.update(before_wall)
            rejections.append({**base, "action": "rejected", "reason": reason})
            continue
        _append_line_reference(plan, partition_id=partition_id, basis="dimension",
                               source_refs=[*partition.get("source_refs", []), *chain["source_refs"],
                                            f"dimension_chain:{chain['id']}"])
        changes.append({**base, "moved_with_wall": attached})
    return changes, rejections, associations_not_applied


def _snap_footprint(plan: dict[str, Any], *, observed: Mapping[str, Any], axis: str,
                    ticks: list[dict[str, Any]],
                    mpp: float) -> tuple[list[dict[str, Any]],
                                       list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    associations_not_applied: list[dict[str, Any]] = []
    expected_along = "y" if axis == "x" else "x"
    ring = plan.get("footprint_pixels", [])
    if not isinstance(ring, list) or len(ring) < 4:
        return changes, rejections, associations_not_applied

    # Each edge is considered once against verified ticks on its own axis.
    # A nearby tick on the other axis cannot authorize a move.
    for edge_index in range(len(ring)):
        segment = next((row for row in _segments(plan["footprint_pixels"], closed=True)
                        if row[0] == edge_index), None)
        if segment is None:
            rejections.append({
                "object": f"footprint:{edge_index}", "kind": "perimeter",
                "axis": axis, "basis": "dimension",
                "action": "rejected",
                "reason": "perimeter edge is non-orthogonal before dimension alignment",
            })
            continue
        _, along_axis, _, old_cross, span = segment
        if along_axis != expected_along:
            continue
        original_segment = next(row for row in _segments(observed["footprint_pixels"], closed=True)
                                if row[0] == edge_index)
        original_cross = original_segment[3]
        tick, association = _wall_tick(original_cross, ticks, mpp)
        if tick is None:
            if association is not None:
                associations_not_applied.append({**association, "object": f"footprint:{edge_index}",
                                                 "axis": axis})
            continue
        target, chain = tick["target_pixel"], tick["chain"]
        distance_m = abs(target - old_cross) * mpp
        if distance_m >= MAX_ALIGNMENT_WORLD_M or abs(target - old_cross) <= 1e-9:
            continue

        before_edge = copy.deepcopy(plan)
        edge_directions = _footprint_edge_directions(plan)
        orthogonal_partitions, orthogonal_openings = _orthogonal_ids(plan)
        partition_attachments, opening_hosts = _attachment_requirements(plan)
        attached = move_straight_wall(
            plan, collection="footprint", identity=str(edge_index),
            along_axis=along_axis, old_cross=old_cross, new_cross=target, span=span,
            endpoint_tolerance_px=_junction_tolerance_pixels(_axis_scale(plan, along_axis)),
        )
        safe, reason = _footprint_preserves_declarations(
            plan,
            orthogonal_partitions=orthogonal_partitions,
            orthogonal_openings=orthogonal_openings,
            partition_attachments=partition_attachments,
            opening_hosts=opening_hosts,
            footprint_edge_directions=edge_directions,
            footprint_axis_scales={candidate_axis: _axis_scale(plan, candidate_axis)
                                   for candidate_axis in ("x", "y")},
        )
        base = {
            "object": f"footprint:{edge_index}",
            "kind": "perimeter",
            "axis": axis,
            "from_pixel": round(old_cross, 6),
            "to_pixel": round(target, 6),
            "movement_m": round((target - old_cross) * mpp, 6),
            "chain_id": chain["id"],
            "observed_wall_pixel": original_cross,
            "observed_tick_pixel": tick["observed_pixel"],
            "tick_index": tick["tick_index"],
            "basis": "dimension",
        }
        if not safe:
            plan.clear()
            plan.update(before_edge)
            rejections.append({**base, "action": "rejected", "reason": reason})
            continue

        for partition_id in attached["walls"]:
            partition = next((row for row in plan.get("partitions", [])
                              if str(row.get("id")) == partition_id), None)
            _append_line_reference(
                plan, partition_id=partition_id, basis="dimension",
                source_refs=[*(partition or {}).get("source_refs", []), *chain["source_refs"],
                             f"dimension_chain:{chain['id']}"],
            )
        changes.append({**base, "action": "moved", "moved_with_wall": attached})
    return changes, rejections, associations_not_applied


def align_plan_to_dimensions(plan: Mapping[str, Any], *, grid_step_m: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate dimension chains, then let accepted overall chains set axis frames.

    A chain can replace an axis calibration only when its first and last ticks
    correspond to the two footprint outer faces.  Internal chains are checked
    and reported but never silently promoted to a whole-axis origin/scale.
    """

    if not isinstance(plan, Mapping):
        raise TypeError("plan must be an object")
    result = copy.deepcopy(dict(plan))
    raw_chains = result.pop("dimension_chains", [])
    if raw_chains is None:
        raw_chains = []
    if not isinstance(raw_chains, list):
        raw_chains = [{"invalid": raw_chains}]
    items: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    associations_not_applied: list[dict[str, Any]] = []
    accepted_axes: set[str] = set()
    applied_chains = 0
    supplemental_chains = 0
    grid_nodes: dict[tuple[str, float], float] = {}
    verified_ticks: dict[str, list[dict[str, Any]]] = {"x": [], "y": []}

    def remember_ticks(chain, world_ticks):
        verified_ticks[chain["axis"]].extend(
            {"observed_pixel": pixel, "world_m": world, "chain": chain, "tick_index": i}
            for i, (pixel, world) in enumerate(zip(chain["tick_pixels"], world_ticks)))

    def calibration_first(entry):
        index, raw = entry
        chain, _ = _chain_shape(raw, index)
        if chain is None:
            return 2
        axis = chain["axis"]
        return int(_overall_direction(chain, _footprint_span(plan, axis),
                                     max(2.0, MAX_ALIGNMENT_WORLD_M / _axis_scale(plan, axis))) is None)

    def accept_grid_nodes(chain, coordinates, check):
        keys = [(chain["axis"], round(pixel, 6)) for pixel in chain["tick_pixels"]]
        conflicts = [{"axis": key[0], "tick_pixel": key[1], "accepted_m": grid_nodes[key], "proposed_m": value}
                     for key, value in zip(keys, coordinates)
                     if key in grid_nodes and abs(grid_nodes[key] - value) > 1e-7]
        if conflicts:
            rejections.append({**check, "action": "rejected", "reason": "shared dimension-chain node conflicts",
                               "conflicts": conflicts})
            return False
        grid_nodes.update(zip(keys, coordinates))
        return True

    for index, raw in sorted(enumerate(raw_chains), key=calibration_first):
        chain, error = _chain_shape(raw, index)
        if error is not None:
            rejections.append(error)
            continue
        assert chain is not None
        check = _check_chain(chain, closure_tolerance_mm=(grid_step_m * 500 if grid_step_m else MAX_CLOSURE_ERROR_MM))
        check["original_segments_mm"] = list(chain["segments_mm"])
        check["total_derived"] = "total_mm" not in raw
        if not check["accepted"]:
            check["action"] = "rejected"
            check["reason"] = ("segment sum does not close to total" if not check["closure_ok"]
                               else "tick spacing is inconsistent with the annotated segment ratios")
            rejections.append(check)
            continue

        axis = chain["axis"]
        old_anchors = copy.deepcopy(result[f"{axis}_anchors"])
        mpp_before = _axis_scale(result, axis)
        footprint = _footprint_span(result, axis)
        endpoint_tolerance_px = max(2.0, MAX_ALIGNMENT_WORLD_M / mpp_before)
        direction_px = _overall_direction(chain, footprint, endpoint_tolerance_px)
        if direction_px is None:
            if grid_step_m is not None:
                from src.agent.geometry.lite_bim_regularization import quantize_m
                start = chain.get("start_world_m", _world(old_anchors, chain["tick_pixels"][0]))
                direction = 1 if _world(old_anchors, chain["tick_pixels"][-1]) > _world(old_anchors, chain["tick_pixels"][0]) else -1
                cumulative = [0.0]
                for value in chain["segments_mm"]:
                    cumulative.append(math.fsum((cumulative[-1], value)))
                # A supplied total is authoritative within the grid tolerance;
                # distribute the small closure remainder in code, not by model.
                raw_world = [start + direction * v * chain["total_mm"] / cumulative[-1] / 1000 for v in cumulative]
                world_ticks = [quantize_m(v, grid_step_m) for v in raw_world]
                if any(a == b for a, b in zip(world_ticks, world_ticks[1:])):
                    rejections.append({**check, "action": "rejected", "reason": "grid collapses a dimension-chain segment"})
                    continue
                if not accept_grid_nodes(chain, world_ticks, check):
                    continue
                remember_ticks(chain, world_ticks)
                _append_coordinate_references(result, chain, world_ticks)
                applied_chains += 1
                items.append({**check, "action": "local_ticks_applied", "overall": False,
                              "origin_basis": "dimension_chain" if "start_world_m" in chain else "local_anchor",
                              "original_tick_world_m": raw_world, "adopted_tick_world_m": world_ticks,
                              "adopted_segments_mm": [round(abs(b-a)*1000, 9) for a,b in zip(world_ticks, world_ticks[1:])],
                              "grid_step_m": grid_step_m})
                continue
            items.append({**check, "action": "checked_not_applied", "overall": False,
                          "reason": "first and last ticks do not correspond to both footprint outer faces",
                          "footprint_pixels": [round(value, 6) for value in footprint],
                          "endpoint_tolerance_pixels": round(endpoint_tolerance_px, 6)})
            continue
        start_px = footprint[0] if direction_px == 1 else footprint[1]
        end_px = footprint[1] if direction_px == 1 else footprint[0]
        old_start_world = _world(old_anchors, start_px)
        old_end_world = _world(old_anchors, end_px)
        old_world_direction = 1 if old_end_world > old_start_world else -1
        origin_basis = "dimension_chain" if "start_world_m" in chain else "legacy_anchor"
        start_world = chain.get("start_world_m", old_start_world)
        end_world = start_world + old_world_direction * chain["total_mm"] / 1000.0
        cumulative = [0.0]
        for value in chain["segments_mm"]:
            cumulative.append(math.fsum((cumulative[-1], value)))
        world_ticks = [start_world + old_world_direction * value / 1000.0 for value in cumulative]
        target_pixels = [start_px + (end_px - start_px) * value / chain["total_mm"] for value in cumulative]
        if grid_step_m is not None:
            from src.agent.geometry.lite_bim_regularization import quantize_m
            raw_world = [start_world + old_world_direction * value * chain["total_mm"] / cumulative[-1] / 1000 for value in cumulative]
            world_ticks = [quantize_m(value, grid_step_m) for value in raw_world]
            start_world, end_world = world_ticks[0], world_ticks[-1]
            if start_world == end_world or any(a == b for a, b in zip(world_ticks, world_ticks[1:])):
                rejections.append({**check, "action": "rejected", "reason": "grid collapses a dimension-chain segment"})
                continue
            if not accept_grid_nodes(chain, world_ticks, check):
                continue
            target_pixels = [start_px + (end_px-start_px)*(v-start_world)/(end_world-start_world) for v in world_ticks]
            check.update(original_tick_world_m=raw_world, adopted_tick_world_m=world_ticks,
                         adopted_segments_mm=[round(abs(b-a)*1000,9) for a,b in zip(world_ticks,world_ticks[1:])],
                         grid_step_m=grid_step_m)

        if axis in accepted_axes:
            start_difference = start_world - old_start_world
            end_difference = end_world - old_end_world
            maximum_difference = max(abs(start_difference), abs(end_difference))
            existing_mpp = _axis_scale(result, axis)
            supplemental_mpp = ((chain["total_mm"] / 1000.0)
                                / abs(chain["tick_pixels"][-1] - chain["tick_pixels"][0]))
            scale_relative_difference = abs(supplemental_mpp - existing_mpp) / existing_mpp
            consistent = (maximum_difference <= MAX_CHAIN_CROSS_CHECK_M
                          and scale_relative_difference <= MAX_CHAIN_SCALE_RELATIVE_ERROR)
            cross_check = {
                "existing_start_world_m": round(old_start_world, 9),
                "supplemental_start_world_m": round(start_world, 9),
                "start_difference_m": round(start_difference, 9),
                "existing_end_world_m": round(old_end_world, 9),
                "supplemental_end_world_m": round(end_world, 9),
                "end_difference_m": round(end_difference, 9),
                "maximum_absolute_difference_m": round(maximum_difference, 9),
                "tolerance_m": MAX_CHAIN_CROSS_CHECK_M,
                "existing_metres_per_pixel": round(existing_mpp, 12),
                "supplemental_metres_per_pixel": round(supplemental_mpp, 12),
                "scale_relative_difference": round(scale_relative_difference, 9),
                "scale_relative_tolerance": MAX_CHAIN_SCALE_RELATIVE_ERROR,
                "status": "consistent" if consistent else "conflict",
            }
            if not consistent:
                rejections.append({
                    **check,
                    "action": "rejected",
                    "reason": "supplemental overall chain conflicts with the accepted axis scale or origin",
                    "axis_cross_check": cross_check,
                })
                continue

            _append_coordinate_references(result, chain, world_ticks)
            remember_ticks(chain, world_ticks)
            applied_chains += 1
            supplemental_chains += 1
            items.append({
                **check,
                "action": "supplemental_ticks_applied",
                "overall": True,
                "origin_basis": origin_basis,
                "axis_cross_check": cross_check,
                "exact_tick_world_m": [round(value, 9) for value in world_ticks],
                "exact_tick_pixels": [round(value, 6) for value in target_pixels],
            })
            continue

        result[f"{axis}_anchors"] = [[start_px, start_world], [end_px, end_world]]
        _append_coordinate_references(result, chain, world_ticks)
        remember_ticks(chain, world_ticks)
        accepted_axes.add(axis)
        applied_chains += 1
        start_difference = start_world - old_start_world
        end_difference = end_world - old_end_world
        maximum_difference = max(abs(start_difference), abs(end_difference))
        items.append({**check, "action": "axis_calibration_applied", "overall": True,
                      "origin_basis": origin_basis,
                      "outer_face_start": {"pixel": round(start_px, 6), "world_m": round(start_world, 9)},
                      "outer_face_end": {"pixel": round(end_px, 6), "world_m": round(end_world, 9)},
                      "old_anchors": old_anchors,
                      "new_anchors": copy.deepcopy(result[f"{axis}_anchors"]),
                      "anchor_cross_check": {
                          "origin_basis": origin_basis,
                          "old_start_world_m": round(old_start_world, 9),
                          "dimension_start_world_m": round(start_world, 9),
                          "start_difference_m": round(start_difference, 9),
                          "old_end_world_m": round(old_end_world, 9),
                          "dimension_end_world_m": round(end_world, 9),
                          # Retain the former end-only key for report consumers.
                          "difference_m": round(end_difference, 9),
                          "end_difference_m": round(end_difference, 9),
                          "maximum_absolute_difference_m": round(maximum_difference, 9),
                          "tolerance_m": MAX_ALIGNMENT_WORLD_M,
                          "status": "consistent" if maximum_difference < MAX_ALIGNMENT_WORLD_M else "warning",
                          "message": (
                              "legacy anchors supply the origin and agree with the accepted overall chain"
                              if origin_basis == "legacy_anchor" and maximum_difference < MAX_ALIGNMENT_WORLD_M else
                              "legacy anchors supply the origin but disagree with the accepted overall chain scale"
                              if origin_basis == "legacy_anchor" else
                              "legacy anchors agree with the dimension-chain origin and endpoint"
                              if maximum_difference < MAX_ALIGNMENT_WORLD_M else
                              "legacy anchors disagree with the dimension-chain origin or endpoint; the chain sets the axis"
                          ),
                      },
                      "exact_tick_world_m": [round(value, 9) for value in world_ticks],
                      "exact_tick_pixels": [round(value, 6) for value in target_pixels]})

    # All chains are interpreted before moving geometry. A wall's correspondence
    # comes from reciprocal nearest original wall/tick coordinates across chains;
    # a window-jamb chain cannot capture a wall moved by an earlier chain, and
    # one tick cannot pull multiple distinct wall coordinates onto itself.
    for axis, ticks in verified_ticks.items():
        if not ticks:
            continue
        (p0, w0), (p1, w1) = result[f"{axis}_anchors"]
        for tick in ticks:
            tick["target_pixel"] = round(p0 + (tick["world_m"] - w0) * (p1 - p0) / (w1 - w0), 12)
        _associate_tick_wall_coordinates(plan, axis=axis, ticks=ticks)
        edges, edge_rejected, edge_associations = _snap_footprint(
            result, observed=plan, axis=axis, ticks=ticks, mpp=_axis_scale(result, axis))
        walls, wall_rejected, wall_associations = _snap_partitions(
            result, observed=plan, axis=axis, ticks=ticks, mpp=_axis_scale(result, axis))
        changes.extend([*edges, *walls])
        rejections.extend([*edge_rejected, *wall_rejected])
        associations_not_applied.extend([*edge_associations, *wall_associations])
    for item in items:
        snapped = [row for row in changes if row["chain_id"] == item["chain_id"]]
        item.update(snapped_object_count=len(snapped),
                    snapped_partition_count=sum(row["kind"] == "partition" for row in snapped),
                    snapped_footprint_edge_count=sum(row["kind"] == "perimeter" for row in snapped))

    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "applied" if applied_chains or changes else ("rejected" if rejections else "unchanged"),
        "tolerances": {
            "closure_mm": grid_step_m * 500 if grid_step_m else MAX_CLOSURE_ERROR_MM,
            "tick_residual_pixels": "max(2 pixels, 1% of chain pixel span)",
            "wall_to_tick_m": MAX_ALIGNMENT_WORLD_M,
            "overall_endpoint_m": MAX_ALIGNMENT_WORLD_M,
            "supplemental_chain_cross_check_m": MAX_CHAIN_CROSS_CHECK_M,
            "supplemental_chain_scale_relative": MAX_CHAIN_SCALE_RELATIVE_ERROR,
        },
        "summary": {
            "chains_supplied": len(raw_chains),
            "chains_applied": applied_chains,
            "axes_calibrated": len(accepted_axes),
            "supplemental_chains_applied": supplemental_chains,
            "walls_snapped": len(changes),
            "partitions_snapped": sum(row["kind"] == "partition" for row in changes),
            "footprint_edges_snapped": sum(row["kind"] == "perimeter" for row in changes),
            "wall_associations_not_applied": len(associations_not_applied),
            "rejected": len(rejections),
        },
        "items": [*items, *associations_not_applied],
        "changes": changes,
        "rejections": rejections,
    }
    _set_reading_alignment(result, "dimensions", report)
    return result, report
