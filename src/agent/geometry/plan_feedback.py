"""Explicit length conversion and feedback on declared plan geometry.

No image recognition, scale guessing, or geometric repair happens here.
"""
from __future__ import annotations

import copy
import math

from src.agent.geometry.source_image_overlay import _axis_anchors


def resolve_plan_lengths(raw: dict) -> tuple[dict, list]:
    """Keep bare numbers in metres; convert only explicitly unit-tagged values."""
    if not isinstance(raw, dict):
        raise ValueError("plan must be an object")
    plan = copy.deepcopy(raw)
    bindings = []

    def resolve(container, key, path):
        value = container[key]
        if not isinstance(value, dict):
            return
        if set(value) != {"value", "unit"} or value["unit"] not in ("m", "cm", "mm"):
            raise ValueError(f"{path} requires a number in metres or {{value: number, unit: m/cm/mm}}")
        number = value["value"]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f"{path}.value must be finite")
        metres = number / {"m": 1, "cm": 100, "mm": 1000}[value["unit"]]
        container[key] = metres
        bindings.append(dict(slot=path, quantity=value, resolved_metres=metres))

    for key in ("z_floor", "ceiling_height"):
        if key in plan:
            resolve(plan, key, key)
    for axis in ("x", "y"):
        anchors = plan.get(f"{axis}_anchors", [])
        for i, anchor in enumerate(anchors if isinstance(anchors, list) else []):
            if isinstance(anchor, list) and len(anchor) == 2:
                resolve(anchor, 1, f"{axis}_anchors[{i}][1]")
    openings = plan.get("openings", [])
    for i, row in enumerate(openings if isinstance(openings, list) else []):
        if isinstance(row, dict) and isinstance(row.get("z"), list):
            for j in range(len(row["z"])):
                resolve(row["z"], j, f"openings[{i}].z[{j}]")
    return plan, bindings


def axis_orientation(metres_per_pixel_x: float, metres_per_pixel_y: float) -> dict:
    """Report which image direction the anchors make east and north.

    World x is east and world y is north. Anchors that grow y toward the image
    bottom (or x toward the image left) mirror the plan unless the drawing itself
    is oriented that way; overlays reuse the same anchors and cannot reveal it.
    """
    east = "image_right" if metres_per_pixel_x > 0 else "image_left"
    north = "image_top" if metres_per_pixel_y < 0 else "image_bottom"
    report = dict(world_east_toward=east, world_north_toward=north)
    mirrored = [axis for axis, usual in (("x", east == "image_right"), ("y", north == "image_top")) if not usual]
    if mirrored:
        report["check"] = (
            f"These anchors put world north toward the {north.split('_')[1]} and east toward the "
            f"{east.split('_')[1]} of the image. Unless this drawing's north arrow or labels show that "
            "orientation, the plan is mirrored; source overlays reuse your anchors and cannot reveal it.")
    return report


def plan_geometry_feedback(plan: dict, image_size) -> dict:
    """Describe the effective dimensions, even when topology compilation fails."""
    sx, ix, _ = _axis_anchors(plan["x_anchors"], axis="x", size=image_size[0])
    sy, iy, _ = _axis_anchors(plan["y_anchors"], axis="y", size=image_size[1])

    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("geometry feedback requires finite numeric coordinates")
        return value

    def point(p):
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError("geometry feedback requires [x,y] points")
        return [number(p[0]) * sx + ix, number(p[1]) * sy + iy]

    footprint = [point(p) for p in plan["footprint_pixels"]]
    bounds = [[min(p[a] for p in footprint), max(p[a] for p in footprint)] for a in (0, 1)]
    openings = []
    for row in plan["openings"]:
        p1, p2 = point(row["p1"]), point(row["p2"])
        z = [number(v) for v in row["z"]]
        if len(z) != 2:
            raise ValueError("opening z must have two values")
        openings.append(dict(id=row["id"], kind=row["kind"], p1_m=p1, p2_m=p2,
            width_m=math.dist(p1, p2), z_m=z, height_m=z[1] - z[0]))
    return dict(unit="m", floor_id=plan["floor_id"],
        metres_per_pixel=dict(x=sx, y=sy), axis_orientation=axis_orientation(sx, sy),
        footprint_bounds_m=bounds,
        footprint_span_m=[b[1] - b[0] for b in bounds],
        z_floor_m=number(plan["z_floor"]), ceiling_height_m=number(plan["ceiling_height"]),
        openings=openings, drawing_fidelity="not_evaluated",
        note="Effective declaration dimensions, not verified drawing dimensions. Bare world values mean metres. Compare with original annotations; no unit or scale is inferred.")


def compact_plan_feedback(report: dict) -> dict:
    rows = report["openings"]
    return {**{k: v for k, v in report.items() if k != "openings"},
        "opening_count": len(rows), "openings_returned": min(24, len(rows)),
        "openings_truncated": len(rows) > 24,
        "opening_dimensions": [{k: r[k] for k in ("id", "width_m", "z_m")} for r in rows[:24]]}


def opening_geometry_changes(before: dict, after: dict) -> dict:
    """Include indirect changes from calibration as well as explicit row edits."""
    old = {r["id"]: r for r in before["openings"]}
    new = {r["id"]: r for r in after["openings"]}
    changed = [dict(id=key, before=old.get(key), after=new.get(key))
               for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]
    return dict(changed_openings=changed,
        unchanged_opening_ids=sorted(key for key in old.keys() & new.keys() if old[key] == new[key]),
        note="Actual resolved declaration dimensions; host/space changes and drawing truth require source/original review.")
