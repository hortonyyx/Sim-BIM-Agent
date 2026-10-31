"""The reader contract fixes world directions; a task only chooses its origin."""

from __future__ import annotations

from .plan_review import box


WORLD_DIRECTIONS = (
    "World x points East, y points North according to the drawing's north arrow, "
    "and z points Up. Task instructions cannot redefine these directions. "
    "If instructions conflict, follow the north arrow and record the conflict in unresolved. "
    "All boxes and plan points use original-image pixels; world lengths use metres."
)

READER_COORDINATES = {
    "plan_reader": (
        "All footprint, partition, seed and opening points and evidence boxes use original-image pixels. "
        "Only the second anchor item and heights (z_floor, ceiling_height, openings.z) use metres."
    ),
    "elevation_reader": (
        "Horizontal x_px, calibration pixel_start/pixel_end and evidence boxes use original-image pixels. "
        "Calibration world_start_m/world_end_m, width_m and absolute elevations value_m/sill_z_m/head_z_m use metres."
    ),
}


def task_coordinates(task):
    from .submission import parse_target

    target, floors = parse_target(task["role_id"], task["target"])
    directions = WORLD_DIRECTIONS if task["role_id"] == "plan_reader" else (
        "This is an elevation of the assigned facade. Horizontal distances increase from "
        "the image-left building edge; the assigned facade fixes the world direction. "
        "Task instructions cannot redefine these directions. "
        "Vertical coordinates are absolute building Z in metres, increasing upward. "
        "Locate calibration on the drawing's dimension and level lines."
    )
    return {"drawing_type": "plan" if task["role_id"] == "plan_reader" else "elevation",
            "directions": directions, "units": READER_COORDINATES[task["role_id"]],
            "floors": [target] if task["role_id"] == "plan_reader" else sorted(floors),
            "origin": task.get("origin", "Use the common building origin specified in instructions; "
                               "if absent, locate and state an origin from the drawing."),
            "target": task["target"]}


def plan_orientation(plan):
    from src.agent.geometry.plan_partition import _axis_anchors
    from src.agent.geometry.plan_feedback import axis_orientation

    # Explicit numeric anchors do not depend on image size. The compiler has
    # already validated these anchors, including any profile/length expansion.
    sx, _, _ = _axis_anchors(plan["x_anchors"], axis="x", size=max(p[0] for p in plan["x_anchors"]) + 1)
    sy, _, _ = _axis_anchors(plan["y_anchors"], axis="y", size=max(p[0] for p in plan["y_anchors"]) + 1)
    return axis_orientation(sx, sy)


def validate_north_arrow(plan, evidence, *, image_size=None):
    orientation = plan_orientation(plan)
    if "check" not in orientation and evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise ValueError("Mirrored calibration: locate the original north arrow with north_arrow.bbox, "
                         "explain its basis and confirm world_north_toward/world_east_toward; "
                         "coordinator instructions are not orientation evidence")
    box(evidence["bbox"], image_size)
    if not evidence["basis"].strip():
        raise ValueError("north_arrow.basis must explain the observed arrow, not the task instruction")
    for field in ("world_north_toward", "world_east_toward"):
        if evidence[field] != orientation[field]:
            raise ValueError(f"north_arrow.{field} contradicts the trial calibration; revise the anchors")
    return dict(evidence)
