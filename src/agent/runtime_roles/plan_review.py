"""Checks attached to a reader's immutable trial, never to the single-model path."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping


CONTINUOUS_HINT = "one continuous space rather than a wall with an opening"
COLLECTIONS = ("partitions", "space_seeds", "openings")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def box(value, image_size=None):
    if (not isinstance(value, list) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in value)
            or value[0] < 0 or value[1] < 0 or value[2] <= value[0] or value[3] <= value[1]):
        raise ValueError("bbox needs original-image [left,top,right,bottom]. Minimum correct example: [0,0,10,10]")
    if image_size and (value[2] > image_size[0] or value[3] > image_size[1]):
        raise ValueError(f"bbox exceeds original image {image_size}. Minimum correct example: [0,0,10,10]")
    return value


def revise_operations(before, operations, *, image_size=None, allowed_targets=None):
    """Apply the shared ID-based operations; boxes are reader-only provenance."""
    from src.agent.geometry.plan_revision import apply_plan_revision

    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("supply 1 to 100 operations with reason, source_refs and bbox")
    core = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or "bbox" not in operation:
            raise ValueError(f"operations[{index}] needs an original-image bbox")
        box(operation["bbox"], image_size)
        core.append({key: copy.deepcopy(value) for key, value in operation.items() if key != "bbox"})
    try:
        updated, preservation = apply_plan_revision(before, core)
    except (TypeError, KeyError) as error:
        raise ValueError(f"invalid revision operation: {error}") from error
    for row in preservation["changes"]:
        row["item"] = "plan." + row["field"] + (":" + row["id"] if row["id"] is not None else "")
        row["bbox"] = copy.deepcopy(operations[row["operation_index"]]["bbox"])
        if allowed_targets is not None and row["item"] not in allowed_targets:
            raise ValueError("Rework changed items outside the coordinator's pointed issues: " + row["item"])
    preservation["actual_changes"] = [row for row in preservation["changes"] if row["before"] != row["after"]]
    return updated, preservation


def topology_issues(receipts):
    """Include earlier warnings: deleting a bad wall cannot erase its review duty.

    Only from trials that produced geometry: a draft that never compiled (10-06
    probe run2: dividers written in metres) leaves warnings about nothing real.
    """
    found = {}
    for receipt in receipts:
        if receipt.get("reading_geometry_ready", receipt.get("source_geometry_ready")) is False:
            continue
        report = receipt.get("drawing_differences", {})
        for row in report.get("items", []) if isinstance(report, Mapping) else []:
            kind = row.get("type", row.get("kind"))
            if kind != "unsupported_open_separator" and not (
                    kind == "opening_offset_from_gap" and CONTINUOUS_HINT in row.get("check", "")):
                continue
            identity = {key: row[key] for key in ("type", "kind", "divider", "opening", "gap",
                        "x_px", "y_px") if key in row}
            issue_id = "topology-" + _digest(identity)[:16]
            found.setdefault(issue_id, {"issue_id": issue_id, "plan_sha256": receipt["plan_sha256"], **row,
                "divider_points": receipt.get("topology_dividers", {}).get(row.get("divider"))})
    return list(found.values())


def _segments(plan):
    ring = plan["footprint_pixels"]
    for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
        yield f"footprint:{index}", a, b
    for row in plan.get("partitions", []):
        for a, b in zip(row["points"], row["points"][1:]):
            yield row["id"], a, b


def unhosted_openings(plan):
    """Every opening not lying on one declared footprint edge or partition, with the
    nearest parallel line. The compiler stops at the first; a reader then spends one
    request per opening (10-06 probe run3)."""
    from shapely.geometry import LineString

    lines = [(identity, LineString([a, b])) for identity, a, b in _segments(plan)]
    found = []
    for row in plan.get("openings", []):
        opening = LineString([row["p1"], row["p2"]])
        if opening.length <= 1e-6 or any(opening.difference(line).length <= 1e-6 for _, line in lines):
            continue
        vertical = abs(row["p1"][0] - row["p2"][0]) < abs(row["p1"][1] - row["p2"][1])
        axis = 1 if vertical else 0
        low, high = sorted((row["p1"][axis], row["p2"][axis]))
        nearest = None
        for identity, line in lines:
            (x0, y0), (x1, y1) = line.coords
            if (abs(x0 - x1) < abs(y0 - y1)) != vertical:
                continue
            spans = min(high, max(line.coords[0][axis], line.coords[1][axis])) > max(
                low, min(line.coords[0][axis], line.coords[1][axis]))
            offset = (row["p1"][0] - x0) if vertical else (row["p1"][1] - y0)
            key = (not spans, abs(offset))
            if nearest is None or key < nearest[0]:
                nearest = (key, {"line": identity.split(":")[0], "line_at_px": x0 if vertical else y0,
                                 "offset_px": round(offset, 2), "spans_opening": spans})
        found.append({"opening": row["id"], "p1": row["p1"], "p2": row["p2"],
                      "nearest_parallel_line": nearest[1] if nearest else None})
    return found


def loose_partition_ends(plan, tolerance=0.5):
    """Partition ends touching no other declared line, with the nearest line and gap.

    The compiler reports dangling segments only; readers repeatedly stopped dividers
    at the inner face of an exterior wall, 10-20 px short of the footprint line
    (10-06 probe run2, sm24 debug run2).
    """
    from shapely.geometry import LineString, Point

    segments = list(_segments(plan))
    found = []
    for row in plan.get("partitions", []):
        points = row.get("points") or []
        others = [(identity, LineString([a, b])) for identity, a, b in segments if identity != row["id"]]
        own = [LineString([a, b]) for identity, a, b in segments if identity == row["id"]]
        for end in (points[0], points[-1]) if len(points) >= 2 else ():
            point = Point(end)
            if any(line.distance(point) <= tolerance for _, line in others):
                continue
            if any(line.distance(point) <= tolerance and not line.boundary.contains(point) for line in own):
                continue
            identity, line = min(others, key=lambda item: item[1].distance(point), default=(None, None))
            found.append({"partition": row["id"], "end": end,
                          "nearest_line": identity.split(":")[0] if identity else None,
                          "gap_px": round(line.distance(point), 2) if line is not None else None})
    return found


def opening_hosts(plan):
    """Verify both input endpoints share a declared wall; never move them to pass."""
    result = []
    segments = list(_segments(plan))
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    groups = {}
    for identity, a, b in segments:
        groups.setdefault(identity, []).append(LineString([a, b]))
    footprint = unary_union([line for identity, lines in groups.items() if identity.startswith("footprint:") for line in lines])
    for row in plan.get("openings", []):
        opening = LineString([row["p1"], row["p2"]])
        hosts = sorted(identity for identity, lines in groups.items()
                       if opening.length > 1e-6 and opening.difference(unary_union(lines)).length <= 1e-6)
        if not any(identity.startswith("footprint:") for identity in hosts) and opening.length > 1e-6:
            if opening.difference(footprint).length <= 1e-6:
                hosts.append("footprint")
        if len(hosts) != 1:
            raise ValueError(f"plan.openings:{row['id']}: both endpoints must lie on one declared wall line; "
                             f"found {hosts}. Correct the pointed endpoints/wall and trial again; no automatic snapping.")
        result.append({"id": row["id"], "wall": hosts[0], "p1": row["p1"], "p2": row["p2"]})
    return result


def validate_wall_reference(value, *, image_size=None):
    allowed = {"centerline", "inner_face", "outer_face", "explicit_face"}
    if not isinstance(value, Mapping) or set(value) != {"perimeter", "partitions"}:
        raise ValueError("wall_reference needs separate perimeter and partitions declarations")
    for category in ("perimeter", "partitions"):
        row = value[category]
        if not isinstance(row, Mapping) or set(row) != {"convention", "dimension_basis", "basis", "bbox"}:
            raise ValueError(f"wall_reference.{category} needs convention, dimension_basis, basis and bbox")
        if (not isinstance(row["convention"], str) or row["convention"] not in allowed
                or row["dimension_basis"] != row["convention"]):
            raise ValueError(f"wall_reference.{category}: dimension_basis must name the same declared reference line after conversion")
        if not isinstance(row["basis"], str) or not row["basis"].strip():
            raise ValueError(f"wall_reference.{category}.basis must explain the dimension chain and any face-to-line conversion")
        box(row["bbox"], image_size)
    return copy.deepcopy(dict(value))


def validate_topology(issues, decisions, plan, *, image_size=None):
    if not isinstance(decisions, list):
        raise ValueError("topology_decisions must be a list")
    if not issues and decisions:
        raise ValueError("本稿没有需要决定的警告，这一项传空列表：topology_decisions=[]")
    by_id = {row["issue_id"]: row for row in issues}
    supplied = {}
    openings = {row["id"]: row for row in plan.get("openings", [])}
    for row in decisions:
        if not isinstance(row, Mapping) or set(row) != {"issue_id", "decision", "basis", "bbox"}:
            raise ValueError('topology_decisions item needs issue_id, decision, basis, bbox. Minimum correct example: '
                             '{"issue_id":"topology-...","decision":"retain_opening","basis":"same physical wall on both sides","bbox":[0,0,10,10]}')
        identity = row["issue_id"]
        if identity not in by_id or identity in supplied:
            raise ValueError("unknown or duplicate topology issue_id: " + str(identity))
        if not isinstance(row["basis"], str) or not row["basis"].strip():
            raise ValueError("topology decision needs a drawing-based explanation of whether both sides are the same wall")
        box(row["bbox"], image_size)
        issue = by_id[identity]
        look = issue.get("look_box")
        if look and (row["bbox"][2] <= look[0] or row["bbox"][0] >= look[2]
                     or row["bbox"][3] <= look[1] or row["bbox"][1] >= look[3]):
            raise ValueError("topology evidence bbox must overlap the flagged local image region")
        if row["decision"] == "retain_opening":
            if issue.get("opening") not in openings:
                raise ValueError("retain_opening requires the flagged opening in the submitted trial")
            opening = openings[issue["opening"]]
            from shapely.geometry import LineString, box as region_box
            from shapely.ops import unary_union
            line = LineString([opening["p1"], opening["p2"]])
            hosts = [LineString([a, b]) for identity, a, b in _segments(plan) if identity == issue.get("divider")]
            if (not hosts or line.difference(unary_union(hosts)).length > 1e-6
                    or (look and not line.intersects(region_box(*look)))):
                raise ValueError("retain_opening must retain the flagged opening on its flagged divider in the local gap region")
        elif row["decision"] == "continuous_space":
            if issue.get("opening") in openings:
                raise ValueError("continuous_space requires removing the flagged opening as well as the artificial wall")
            region = issue.get("gap", issue)
            xp, yp = region.get("x_px"), region.get("y_px")
            if isinstance(xp, list) and isinstance(yp, (float, int)):
                start, end = [xp[0], yp], [xp[1], yp]
            elif isinstance(yp, list) and isinstance(xp, (float, int)):
                start, end = [xp, yp[0]], [xp, yp[1]]
            else:
                raise ValueError("topology issue has no checkable gap coordinates")
            from shapely.geometry import LineString
            gap = LineString([start, end])
            # Difference reports round pixels. Reuse the exact declared wall
            # coordinate when available so a fractional pixel cannot evade the check.
            original = issue.get("divider_points")
            if original:
                for a, b in zip(original, original[1:]):
                    if start[0] == end[0] and a[0] == b[0] and abs(a[0] - start[0]) <= .51:
                        gap = LineString([[a[0], start[1]], [a[0], end[1]]])
                    elif start[1] == end[1] and a[1] == b[1] and abs(a[1] - start[1]) <= .51:
                        gap = LineString([[start[0], a[1]], [end[0], a[1]]])
            for segment_id, a, b in _segments(plan):
                line = LineString([a, b])
                # Renaming and shifting a separator within this local corridor
                # does not implement a continuous-space decision either.
                if look:
                    if start[0] == end[0] and a[0] == b[0] and look[0] <= a[0] <= look[2]:
                        overlap = max(0, min(max(a[1], b[1]), max(start[1], end[1]))
                                      - max(min(a[1], b[1]), min(start[1], end[1])))
                    elif start[1] == end[1] and a[1] == b[1] and look[1] <= a[1] <= look[3]:
                        overlap = max(0, min(max(a[0], b[0]), max(start[0], end[0]))
                                      - max(min(a[0], b[0]), min(start[0], end[0])))
                    else:
                        overlap = 0
                    if overlap >= .85 * gap.length:
                        raise ValueError("continuous_space still has a declared wall spanning the flagged local gap")
                if segment_id == issue.get("divider"):
                    if original:
                        moved = line.difference(LineString(original)).length > 1e-6
                    else:
                        # Legacy warnings have only the flagged straight line.
                        moved = (abs(a[0] - start[0]) > 1e-6 or abs(b[0] - start[0]) > 1e-6
                                 if start[0] == end[0] else
                                 abs(a[1] - start[1]) > 1e-6 or abs(b[1] - start[1]) > 1e-6)
                    if moved:
                        raise ValueError("continuous_space requires removing the flagged wall segment, not moving its divider")
                if gap.intersection(line).length > 1e-6:
                    raise ValueError("continuous_space still has a declared wall crossing the inkless gap")
        else:
            raise ValueError("decision must be retain_opening or continuous_space")
        supplied[identity] = dict(row)
    if set(by_id) != set(supplied):
        raise ValueError("Every topology warning needs a located retain_opening/continuous_space decision; missing: "
                         + ", ".join(sorted(set(by_id) - set(supplied))))
    return list(supplied.values())
