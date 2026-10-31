"""Opt-in Lite BIM precision: shared coordinates, literal compilation, no fallback.

The grid is a modelling policy, not a display format. Original observations stay
in the audit while all adopted boundary and opening coordinates use one grid.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_LITE_GRID_M = 0.1
DEFAULT_GRID_STEP_M = DEFAULT_LITE_GRID_M


def _geometry_digest(plan):
    value = {key: item for key, item in plan.items() if key != "regularization"}
    # Only these containers change in the digest projection. All evidence stays
    # in the plan, and JSON encoding never mutates the referenced field values.
    if "regularization_inputs" in value:
        inputs = value["regularization_inputs"] = dict(value["regularization_inputs"])
        if "reading_alignment" in inputs:
            inputs["reading_alignment"] = {
                key: item for key, item in inputs["reading_alignment"].items() if key != "lite_bim"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quantize_m(value: float, grid_step_m: float = DEFAULT_LITE_GRID_M) -> float:
    """Round absolute world coordinates; ties are symmetric about zero."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError("Lite BIM coordinate must be a finite number")
    if (isinstance(grid_step_m, bool) or not isinstance(grid_step_m, (int, float))
            or not math.isfinite(grid_step_m) or grid_step_m <= 0):
        raise ValueError("Lite BIM grid_step_m must be positive and finite")
    step = Decimal(str(grid_step_m))
    # Calibration arithmetic can leave 3.95 as 3.9499999999999997.
    number = Decimal(str(round(value, 12)))
    return float((number / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def lite_grid_step(plan: dict) -> float | None:
    policy = plan.get("regularization_inputs", {}).get("reading_alignment", {}).get("lite_bim")
    if policy is None:
        return None
    step = policy.get("grid_step_m")
    quantize_m(0, step)
    return float(step)


def compiled_grid_report(proposal: dict, grid_step_m: float = DEFAULT_LITE_GRID_M) -> dict:
    """Check compiled coordinates, leaving unknown 2D-reading heights absent."""
    geometry = proposal["geometry"]
    values = []
    for axis in ("x", "y"):
        values.extend((f"footprint_{axis}[{i}]", v) for i, v in enumerate(geometry.get(f"footprint_{axis}", [])))
    for floor in geometry.get("floors", []):
        name = floor.get("name", "floor")
        if "z_floor" in floor:
            values.append((f"{name}.z_floor", floor["z_floor"]))
            if "ceiling_height" in floor:
                values.append((f"{name}.z_top", floor["z_floor"] + floor["ceiling_height"]))
        elif "ceiling_height" in floor:
            values.append((f"{name}.ceiling_height", floor["ceiling_height"]))
        for cell in floor.get("cells", []):
            for i, point in enumerate(cell.get("polygon", [])):
                values.extend((f"{name}.{cell['id']}.polygon[{i}][{a}]", v) for a, v in enumerate(point))
        for i, point in enumerate(floor.get("footprint", {}).get("vertices", [])):
            values.extend((f"{name}.footprint[{i}][{a}]", v) for a, v in enumerate(point))
    for opening in [*geometry.get("windows", []), *geometry.get("openings", [])]:
        for field in ("p1", "p2", "z"):
            if field == "z" and field not in opening:
                continue
            values.extend((f"{opening['id']}.{field}[{a}]", v)
                          for a, v in enumerate(opening[field]))
    violations = [{"path": path, "adopted_m": value} for path, value in values
                  if abs(value - quantize_m(value, grid_step_m)) > 1e-7]
    return {"status": "pass" if not violations else "rejected", "grid_step_m": grid_step_m,
            "coordinate_count": len(values), "violations": violations}


def regularize_lite_plan(plan: dict, *, image_size: tuple[int, int], image_name: str = "plan",
                        grid_step_m: float = DEFAULT_LITE_GRID_M) -> tuple[dict, dict]:
    """Merge thickness-qualified wall duplicates, then quantize shared geometry.

    Only explicitly audited strips may disappear in the merge transaction.
    The following grid transaction must preserve its resulting relationships.
    """
    from src.agent.geometry.plan_ink_alignment import _set_reading_alignment
    from src.agent.geometry.plan_partition import compile_plan_partition
    from src.agent.geometry.plan_regularization import (
        PlanRegularizationError, _calibration, _pixel_coordinate, _relationship_signature,
        _world, prepare_plan_junctions, validate_regularized_plan, regularize_plan,
    )

    quantize_m(0, grid_step_m)
    merge_report = None
    if plan.get('wall_metadata'):
        from src.agent.geometry.wall_merge_policy import prepare_thickness_plan
        prepared = prepare_thickness_plan(plan)
        result, merge_report = regularize_plan(prepared, image_size=image_size,
                                               image_name=image_name, _before_lite_grid=True)
        preparation = merge_report['junction_preparation']
    else:
        result, preparation = prepare_plan_junctions(copy.deepcopy(plan), image_size=image_size, image_name=image_name)
    result.pop("regularization", None)
    report = {"schema": "lite_bim_regularization_v1", "status": "pending",
              "grid_step_m": grid_step_m,
              "selection_basis": "configured whole-building Lite BIM grid; explicit dimensions may be simplified",
              "rounding": "shared absolute cumulative coordinates; symmetric nearest grid",
              "junction_preparation": preparation, "items": [], "rejections": []}
    if merge_report is not None:
        report['wall_merging'] = merge_report
        report['wall_policy'] = {'thickness_step_m': .01, 'merge_tolerance': '2 * max(thickness)',
                                 'anchor': 'existing evidence-supported line; never average'}

    def reject(message, **details):
        report.update(status="rejected")
        report["rejections"].append({"message": message, **details})
        raise PlanRegularizationError(message, report=report)

    try:
        before, _ = compile_plan_partition(result, image_size=image_size, image_name=image_name,
                                           include_audit_metadata=False)
    except (ValueError, TypeError, KeyError) as error:
        reject(str(error), stage="before_grid_compile")
    calibration = _calibration(result, image_size)

    def record(path, original, adopted):
        report["items"].append({"path": path, "original_m": original, "adopted_m": adopted,
                                "movement_m": round(adopted - original, 12)})

    # Every occurrence of a shared coordinate gets the same inverse pixel. No
    # independent segment-width rounding and no per-object geometry averaging.
    def point(values, path):
        world = _world(calibration, values)
        adopted = []
        for index, axis in enumerate(("x", "y")):
            target = quantize_m(world[index], grid_step_m)
            record(f"{path}.{axis}", world[index], target)
            adopted.append(_pixel_coordinate(calibration, axis, target))
        return adopted

    result["footprint_pixels"] = [point(p, f"footprint:{i}") for i, p in enumerate(result["footprint_pixels"])]
    for partition in result.get("partitions", []):
        partition["points"] = [point(p, f"partitions:{partition['id']}.points[{i}]")
                               for i, p in enumerate(partition["points"])]
    for opening in result.get("openings", []):
        for field in ("p1", "p2"):
            opening[field] = point(opening[field], f"openings:{opening['id']}.{field}")
        for index, value in enumerate(opening.get("z", [])):
            adopted = quantize_m(value, grid_step_m)
            record(f"openings:{opening['id']}.z[{index}]", value, adopted)
            opening["z"][index] = adopted
    if "z_floor" in result:
        z_bottom = result["z_floor"]
        result["z_floor"] = quantize_m(z_bottom, grid_step_m)
        record("z_floor", z_bottom, result["z_floor"])
        if "ceiling_height" in result:
            z_top = z_bottom + result["ceiling_height"]
            result["ceiling_height"] = round(quantize_m(z_top, grid_step_m) - result["z_floor"], 12)
            record("z_top", z_top, result["z_floor"] + result["ceiling_height"])
    elif "ceiling_height" in result:
        original_height = result["ceiling_height"]
        result["ceiling_height"] = quantize_m(original_height, grid_step_m)
        record("ceiling_height", original_height, result["ceiling_height"])
    for i, reference in enumerate(result.get("regularization_inputs", {}).get("coordinate_references", [])):
        original = reference["value_m"]
        reference["value_m"] = quantize_m(original, grid_step_m)
        record(f"coordinate_references[{i}]", original, reference["value_m"])
    try:
        after, _ = compile_plan_partition(result, image_size=image_size, image_name=image_name,
                                          include_audit_metadata=False)
    except (ValueError, TypeError, KeyError) as error:
        reject(str(error), stage="after_grid_compile")
    named = {s["id"] for s in result.get("space_seeds", [])}
    before_sig = json.loads(json.dumps(_relationship_signature(before, named_space_ids=named)))
    after_sig = json.loads(json.dumps(_relationship_signature(after, named_space_ids=named)))
    if before_sig != after_sig:
        reject("Lite BIM grid changed room/opening hosts or connectivity", before=before_sig, after=after_sig)
    hard = validate_regularized_plan(result, image_size=image_size, image_name=image_name)
    report["hard_constraints"] = hard
    if hard["violations"]:
        reject("Lite BIM hard constraint: " + str(hard["violations"][0]), stage="hard_constraints")
    report["compiled_grid"] = compiled_grid_report(after, grid_step_m)
    if report["compiled_grid"]["violations"]:
        reject("Compiled Lite BIM geometry remains outside the requested grid", stage="compiled_grid")
    report["status"] = "pass"
    report["topology"] = {"status": "preserved", "before": before_sig, "after": after_sig}
    report["summary"] = {"checked_coordinates": len(report["items"]),
                         "moved_coordinates": sum(abs(row["movement_m"]) > 1e-9 for row in report["items"])}
    # Operation receipts describe this invocation only. The durable items below
    # may retain older observations for unchanged fields, separately from moves.
    current_items = copy.deepcopy(report["items"])
    if not result.get("regularization_inputs", {}).get("reading_alignment"):
        _set_reading_alignment(result, "dimensions", {"status": "not_supplied"})
    previous = result["regularization_inputs"]["reading_alignment"].get("lite_bim")
    # Replays retain the original evidence instead of replacing it with already
    # snapped values. Rework preserves each untouched field's original reading.
    if isinstance(previous, dict) and previous.get("grid_step_m") == grid_step_m:
        if previous.get("adopted_plan_sha256") == _geometry_digest(result):
            result["regularization_inputs"]["reading_alignment"]["lite_bim"] = copy.deepcopy(previous)
            return result, copy.deepcopy(previous)
        originals = {row["path"]: row for row in previous.get("items", [])}
        for row in report["items"]:
            prior = originals.get(row["path"])
            if prior and abs(prior["adopted_m"] - row["original_m"]) < 1e-7:
                row["original_m"] = prior["original_m"]
                row["movement_m"] = round(row["adopted_m"] - row["original_m"], 12)
    report["changes"] = copy.deepcopy((merge_report or preparation)["changes"])
    world_footprint = [_world(calibration, p) for p in result["footprint_pixels"]]
    for axis in ("x", "y"):
        index, along = (0, 1) if axis == "x" else (1, 0)
        span = [min(p[along] for p in world_footprint) - grid_step_m,
                max(p[along] for p in world_footprint) + grid_step_m]
        moved = {(row["original_m"], row["adopted_m"])
                 for row in current_items if row["path"].endswith("." + axis)
                 and abs(row["movement_m"]) > 1e-9}
        for original, adopted in sorted(moved):
            opening_ids = [opening["id"] for opening in result.get("openings", [])
                           if any(abs(_world(calibration, opening[field])[index] - adopted) < 1e-7
                                  for field in ("p1", "p2"))]
            report["changes"].append({"type": "lite_grid_coordinate", "floor_id": result["floor_id"],
                                      "axis": axis, "from_m": original, "to_m": adopted,
                                      "movement_m": round(adopted-original, 12), "span_m": span,
                                      "grid_step_m": grid_step_m, "opening_ids": opening_ids,
                                      "before_relationships": before_sig, "after_relationships": after_sig})
    for row in current_items:
        if (row["path"] in {"z_floor", "z_top", "ceiling_height"}
                or ".z[" in row["path"]) and abs(row["movement_m"]) > 1e-9:
            report["changes"].append({"type": "lite_grid_height", "floor_id": result["floor_id"],
                                      "path": row["path"], "from_m": row["original_m"], "to_m": row["adopted_m"],
                                      "movement_m": row["movement_m"], "grid_step_m": grid_step_m,
                                      "before_relationships": before_sig, "after_relationships": after_sig})
    from src.agent.geometry.wall_merge_policy import stamp_wall_state
    stamp_wall_state(result)
    report["adopted_plan_sha256"] = _geometry_digest(result)
    result["regularization_inputs"]["reading_alignment"]["lite_bim"] = copy.deepcopy(report)
    return result, report
