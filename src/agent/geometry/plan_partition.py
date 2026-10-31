"""Compile declared plan linework into a deterministic source proposal.

The compiler is deliberately literal: it polygonizes a simple orthogonal footprint
and the supplied physical partition representatives without snapping,
extending, clipping, or inventing linework. Concave footprints use the existing
per-floor ring supported by the standalone source exporter; their bounding-box
gaps never become rooms. Cells may be arbitrary orthogonal polygons.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Iterable
from typing import Any

from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import polygonize_full, unary_union

from src.agent.geometry.source_image_overlay import _axis_anchors
from src.agent.geometry.input_scale import check_planar_scale


_PLAN_FIELDS = frozenset({
    "floor_id", "z_floor", "ceiling_height", "x_anchors", "y_anchors",
    "basis", "footprint_pixels", "partitions", "openings", "space_seeds",
    "assumptions", "unresolved", "regularization_inputs", "regularization",
    "reading_type", "wall_metadata",
})
_REQUIRED_PLAN_FIELDS = _PLAN_FIELDS - {
    "space_seeds", "regularization_inputs", "regularization", "reading_type", "wall_metadata",
}
_REQUIRED_PLAN_READING_FIELDS = _REQUIRED_PLAN_FIELDS - {"z_floor", "ceiling_height"}
_PARTITION_FIELDS = frozenset({"id", "points", "source_refs"})
_OPENING_FIELDS = frozenset({
    "id", "kind", "p1", "p2", "z", "source_refs", "state", "assumptions",
})
_SEED_FIELDS = frozenset({"id", "point", "role", "source_refs", "role_evidence"})


def _required_opening_fields(plan: dict) -> frozenset[str]:
    required = frozenset({"id", "kind", "p1", "p2", "source_refs"})
    return required - {"source_refs"} if plan.get("reading_type") == "plan" else required | {"z"}


def _required_partition_fields(plan: dict) -> frozenset[str]:
    return _PARTITION_FIELDS - {"source_refs"} if plan.get("reading_type") == "plan" else _PARTITION_FIELDS


class OpeningHostError(ValueError):
    """A full declared opening has no valid host; keep exact pixel evidence."""

    def __init__(self, message: str, *, opening_id: str,
                 p1_pixel: tuple[float, float], p2_pixel: tuple[float, float]):
        super().__init__(message)
        self.opening_id = opening_id
        self.p1_pixel = list(p1_pixel)
        self.p2_pixel = list(p2_pixel)


def _number(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{path} must be a finite number")
    return parsed


def _text(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    return value


def _fields(value: object, *, path: str, allowed: frozenset[str], required: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{path} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"{path} fields: " + "; ".join(details))
    return value


def _strings(value: object, *, path: str, require_one: bool = False) -> list[str]:
    if not isinstance(value, list) or (require_one and not value):
        suffix = " with at least one item" if require_one else ""
        raise TypeError(f"{path} must be a list of strings{suffix}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{path} entries must be nonempty strings")
    return list(value)


def _seed_role(item: dict, *, seed_id: str) -> tuple[str, dict | None]:
    """Validate optional use evidence without inferring it from a bare label."""
    from src.agent.correction.schema import RoomRoleEvidence
    from src.agent.roles import require_role

    evidence = (RoomRoleEvidence.model_validate(item["role_evidence"]).model_dump(mode="json")
                if "role_evidence" in item else None)
    role = _text(item.get("role", evidence["role"] if evidence else "unknown"),
                 path=f"space seed {seed_id}.role")
    if evidence is not None:
        if require_role(role) != evidence["role"]:
            raise ValueError(f"space seed {seed_id}.role does not match role_evidence.role")
        role = evidence["role"]
    return role, evidence


def _point(value: object, *, path: str, width: int, height: int) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{path} must be [pixel_x, pixel_y]")
    point = (_number(value[0], path=f"{path}[0]"), _number(value[1], path=f"{path}[1]"))
    if not (0 <= point[0] < width and 0 <= point[1] < height):
        raise ValueError(f"{path} {list(point)} is outside image bounds {width}x{height}")
    return point


def _orthogonal(points: list[tuple[float, float]], *, path: str, closed: bool) -> None:
    pairs = list(zip(points, points[1:]))
    if closed:
        pairs.append((points[-1], points[0]))
    for index, (first, second) in enumerate(pairs):
        if first == second:
            raise ValueError(f"{path} segment {index} is degenerate at pixel {list(first)}")
        if first[0] != second[0] and first[1] != second[1]:
            raise ValueError(
                f"{path} segment {index} is not orthogonal: {list(first)} -> {list(second)}"
            )


def _parts(geometry: Any) -> Iterable[Any]:
    if geometry.is_empty:
        return
    if geometry.geom_type in {"Point", "LineString"}:
        yield geometry
        return
    for part in geometry.geoms:
        yield from _parts(part)


def _rounded(value: float) -> float:
    result = round(float(value), 6)
    return 0.0 if result == 0 else result


def _canonical_ring(polygon: Polygon) -> list[list[float]]:
    oriented = orient(polygon, sign=1.0)
    points = [(_rounded(x), _rounded(y)) for x, y in list(oriented.exterior.coords)[:-1]]
    changed = True
    while changed and len(points) > 3:
        changed = False
        for index, current in enumerate(points):
            previous = points[index - 1]
            following = points[(index + 1) % len(points)]
            if ((previous[0] == current[0] == following[0])
                    or (previous[1] == current[1] == following[1])):
                points.pop(index)
                changed = True
                break
    rounded = Polygon(points)
    if not rounded.is_valid or rounded.area <= 0 or rounded.interiors:
        raise ValueError("rounding world polygon to six decimals produced invalid geometry")
    if not rounded.exterior.is_ccw:
        points.reverse()
    start = min(range(len(points)), key=lambda index: points[index])
    points = points[start:] + points[:start]
    return [[x, y] for x, y in points]


def _describe_residual(kind: str, geometry: Any, partitions: list[dict], footprint: Any) -> str:
    details = []
    for part in _parts(geometry):
        if part.geom_type != "LineString":
            continue
        ids = []
        for partition in partitions:
            overlap = part.intersection(partition["line"])
            if getattr(overlap, "length", 0.0) > 0:
                ids.append(partition["id"])
        details.append({
            "partition_ids": sorted(ids) or ["footprint boundary"],
            "pixel_points": [[_rounded(x), _rounded(y)] for x, y in part.coords],
        })
    # Diagnostics only: nearest declared line to each disconnected endpoint.
    # Never change the linework supplied to the polygonizer.
    boundary = list(footprint.exterior.coords)
    lines = [(f"footprint[{i}]", LineString([a, b]))
             for i, (a, b) in enumerate(zip(boundary, boundary[1:]))]
    lines += [(row["id"], row["line"]) for row in partitions]
    nearest = []
    for row in partitions:
        if geometry.intersection(row["line"]).is_empty:
            continue
        for endpoint in (row["points"][0], row["points"][-1]):
            point = Point(endpoint)
            others = [(point.distance(line), identity, line) for identity, line in lines
                      if line is not row["line"]]
            if not others or any(distance == 0 for distance, _, _ in others):
                continue
            distance, identity, line = min(others, key=lambda item: (item[0], item[1]))
            projected = line.interpolate(line.project(point))
            nearest.append(dict(partition_id=row["id"], endpoint_pixel=list(endpoint),
                nearest_line=identity, distance_pixels=distance,
                nearest_point_pixel=[projected.x, projected.y]))
    return (f"polygonize produced {kind}: {details}; nearest disconnected endpoints: {nearest}; "
            "coordinates unchanged; no automatic snapping")


def compile_plan_partition(
    plan: dict,
    *,
    image_size: tuple[int, int],
    image_name: str,
    include_audit_metadata: bool = True,
) -> tuple[dict, dict]:
    """Return compiled plan geometry and pixel/world mapping metadata.

    ``reading_type='plan'`` marks a 2D reading, where floor and opening heights
    may be absent. Known heights are still validated, and absent heights stay
    absent in the result. Unmarked legacy plans must supply all heights and
    continue to produce an executable geometry-v2 proposal.

    Input linework must already meet exactly.  Exact orthogonal intersections
    are deterministically noded by ``unary_union``; coordinates are never
    snapped or extended to create a junction.  The function never reads image
    pixels and never uses ground truth.  A partition residual, overlap,
    unsupported footprint, or ambiguous opening host raises instead of
    dropping or modifying the declared evidence.

    Internal geometric preflights can omit inert audit metadata they discard.
    Public saves retain the default complete, independently copied metadata.
    """
    from src.agent.geometry.plan_input import normalize_plan_fields
    if not include_audit_metadata and isinstance(plan, dict):
        # These two fields only enter the returned metadata below. Exclude them
        # before alias normalization copies the plan; retain all other keys so
        # unknown-field checks and every geometric check still run unchanged.
        plan = {key: value for key, value in plan.items()
                if key not in {"regularization", "regularization_inputs"}}
    plan, field_aliases = normalize_plan_fields(plan)
    reading_2d = isinstance(plan, dict) and plan.get("reading_type") == "plan"
    plan = _fields(
        plan, path="plan", allowed=_PLAN_FIELDS,
        required=_REQUIRED_PLAN_READING_FIELDS if reading_2d else _REQUIRED_PLAN_FIELDS,
    )
    if "reading_type" in plan and not reading_2d:
        raise ValueError("plan.reading_type must be 'plan' when supplied")
    if (not isinstance(image_size, tuple) or len(image_size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                   for value in image_size)):
        raise ValueError("image_size must be a (positive_width, positive_height) tuple")
    width, height = image_size
    image_name = _text(image_name, path="image_name")
    floor_id = _text(plan["floor_id"], path="plan.floor_id")
    basis = _text(plan["basis"], path="plan.basis")
    z_floor = _number(plan["z_floor"], path="plan.z_floor") if "z_floor" in plan else None
    ceiling_height = (_number(plan["ceiling_height"], path="plan.ceiling_height")
                      if "ceiling_height" in plan else None)
    if ceiling_height is not None and ceiling_height <= 0:
        raise ValueError("plan.ceiling_height must be positive")
    assumptions = _strings(plan["assumptions"], path="plan.assumptions")
    unresolved = _strings(plan["unresolved"], path="plan.unresolved")

    x_slope, x_offset, parsed_x = _axis_anchors(plan["x_anchors"], axis="x", size=width)
    y_slope, y_offset, parsed_y = _axis_anchors(plan["y_anchors"], axis="y", size=height)

    def world(pixel: tuple[float, float]) -> tuple[float, float]:
        return (x_slope * pixel[0] + x_offset, y_slope * pixel[1] + y_offset)

    raw_footprint = plan["footprint_pixels"]
    if not isinstance(raw_footprint, list) or len(raw_footprint) < 4:
        raise ValueError("plan.footprint_pixels must contain at least four points")
    footprint_points = [
        _point(value, path=f"plan.footprint_pixels[{index}]", width=width, height=height)
        for index, value in enumerate(raw_footprint)
    ]
    if footprint_points[0] == footprint_points[-1]:
        footprint_points.pop()
    if len(footprint_points) < 4:
        raise ValueError("plan.footprint_pixels must contain at least four non-closing points")
    check_planar_scale(
        [(f"plan.{axis}_anchors (world values {[p[1] for p in anchors]})",
          abs(slope) * (max(p[i] for p in footprint_points) - min(p[i] for p in footprint_points)))
         for i, (axis, slope, anchors) in enumerate((("x", x_slope, parsed_x), ("y", y_slope, parsed_y)))],
        [("plan.ceiling_height", ceiling_height)],
    )
    _orthogonal(footprint_points, path="plan.footprint_pixels", closed=True)
    footprint = Polygon(footprint_points)
    if not footprint.is_valid or footprint.area <= 0 or footprint.interiors:
        raise ValueError("plan.footprint_pixels must be one valid simple outer ring without holes")
    min_px, min_py, max_px, max_py = footprint.bounds
    rectangular_footprint = footprint.equals(box(min_px, min_py, max_px, max_py))

    raw_partitions = plan["partitions"]
    if not isinstance(raw_partitions, list):
        raise TypeError("plan.partitions must be a list")
    partitions = []
    partition_ids = set()
    for index, raw in enumerate(raw_partitions):
        item = _fields(
            raw, path=f"plan.partitions[{index}]", allowed=_PARTITION_FIELDS,
            required=_required_partition_fields(plan),
        )
        partition_id = _text(item["id"], path=f"plan.partitions[{index}].id")
        if partition_id in partition_ids:
            raise ValueError(f"duplicate partition id {partition_id!r}")
        partition_ids.add(partition_id)
        raw_points = item["points"]
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise ValueError(f"partition {partition_id}: points must contain at least two points")
        points = [
            _point(value, path=f"partition {partition_id}.points[{point_index}]",
                   width=width, height=height)
            for point_index, value in enumerate(raw_points)
        ]
        _orthogonal(points, path=f"partition {partition_id}", closed=False)
        line = LineString(points)
        if not line.is_simple:
            raise ValueError(f"partition {partition_id}: polyline self-intersects at {points}")
        if not footprint.covers(line):
            outside = line.difference(footprint)
            raise ValueError(
                f"partition {partition_id}: line is outside footprint at {outside.wkt}"
            )
        overlap = line.intersection(footprint.boundary)
        if getattr(overlap, "length", 0.0) > 0:
            raise ValueError(
                f"partition {partition_id}: physical partition overlaps footprint boundary at {overlap.wkt}"
            )
        partitions.append({
            "id": partition_id,
            "points": points,
            "line": line,
            "source_refs": _strings(
                item.get("source_refs", [f"image:{image_name}#{partition_id}"]),
                path=f"partition {partition_id}.source_refs", require_one=True,
            ),
        })

    for index, first in enumerate(partitions):
        for second in partitions[index + 1:]:
            intersection = first["line"].intersection(second["line"])
            if getattr(intersection, "length", 0.0) > 0:
                raise ValueError(
                    f"partitions {first['id']} and {second['id']} overlap at {intersection.wkt}"
                )

    linework = unary_union([footprint.boundary, *[row["line"] for row in partitions]])
    polygons, cuts, dangles, invalid = polygonize_full(linework)
    for kind, residual in (("cut edges", cuts), ("dangles", dangles), ("invalid rings", invalid)):
        if not residual.is_empty:
            raise ValueError(_describe_residual(kind, residual, partitions, footprint))
    pixel_polygons = list(polygons.geoms)
    if not pixel_polygons:
        raise ValueError("polygonize produced no source spaces")
    polygons_with_holes = [poly for poly in pixel_polygons if poly.interiors]
    if polygons_with_holes:
        rings = []
        for poly in polygons_with_holes:
            for ring in poly.interiors:
                line = LineString(ring.coords)
                rings.append({
                    "partition_ids": sorted(
                        row["id"] for row in partitions
                        if line.intersection(row["line"]).length > 0
                    ),
                    "pixel_points": [
                        [_rounded(x), _rounded(y)] for x, y in list(ring.coords)[:-1]
                    ],
                })
        raise ValueError(
            "polygonize produced an unsupported space with an inner ring; "
            f"source partition evidence: {rings}"
        )
    covered = unary_union(pixel_polygons)
    if not covered.equals(footprint):
        raise ValueError(
            "polygonized spaces do not exactly cover the declared footprint: "
            f"missing={footprint.difference(covered).wkt}; outside={covered.difference(footprint).wkt}"
        )
    pixel_polygons.sort(key=lambda poly: (
        poly.bounds[1], poly.bounds[0], poly.bounds[3], poly.bounds[2],
        tuple(_canonical_ring(poly)),
    ))

    raw_seeds = plan.get("space_seeds", [])
    if not isinstance(raw_seeds, list):
        raise TypeError("plan.space_seeds must be a list")
    seed_by_polygon: dict[int, dict] = {}
    seed_ids = set()
    for index, raw in enumerate(raw_seeds):
        item = _fields(
            raw, path=f"plan.space_seeds[{index}]", allowed=_SEED_FIELDS,
            required=frozenset({"id", "point"}),
        )
        seed_id = _text(item["id"], path=f"plan.space_seeds[{index}].id")
        if seed_id in seed_ids:
            raise ValueError(f"duplicate space seed id {seed_id!r}")
        seed_ids.add(seed_id)
        point = _point(item["point"], path=f"space seed {seed_id}.point", width=width, height=height)
        matches = [poly_index for poly_index, poly in enumerate(pixel_polygons) if poly.contains(Point(point))]
        if len(matches) != 1:
            raise ValueError(
                f"space seed {seed_id} at pixel {list(point)} must be strictly inside one unique space; "
                f"found {len(matches)}"
            )
        polygon_index = matches[0]
        if polygon_index in seed_by_polygon:
            raise ValueError(
                f"space seeds {seed_by_polygon[polygon_index]['id']} and {seed_id} occupy the same space"
            )
        role, role_evidence = _seed_role(item, seed_id=seed_id)
        refs = _strings(item.get("source_refs", []), path=f"space seed {seed_id}.source_refs")
        seed_by_polygon[polygon_index] = {
            "id": seed_id, "point": point, "role": role, "source_refs": refs,
            "role_evidence": role_evidence,
        }

    cells = []
    space_mapping = []
    used_space_ids = set()
    polygons_by_id: dict[str, Polygon] = {}
    for index, pixel_polygon in enumerate(pixel_polygons):
        seed = seed_by_polygon.get(index)
        space_id = seed["id"] if seed else f"{floor_id}_S{index + 1:02d}"
        if space_id in used_space_ids:
            raise ValueError(f"space id {space_id!r} is duplicated by automatic naming and seeds")
        used_space_ids.add(space_id)
        world_polygon = Polygon([world(point) for point in list(pixel_polygon.exterior.coords)[:-1]])
        ring = _canonical_ring(world_polygon)
        canonical_world = Polygon(ring)
        min_x, min_y, max_x, max_y = canonical_world.bounds
        refs = [f"image:{image_name}"]
        touching_partitions = []
        for partition in partitions:
            if pixel_polygon.boundary.intersection(partition["line"]).length > 0:
                touching_partitions.append(partition["id"])
                refs.extend(partition["source_refs"])
        if seed:
            refs.extend(seed["source_refs"])
        refs = list(dict.fromkeys(refs))
        cell = {
            "id": space_id,
            "role": seed["role"] if seed else "unknown",
            "x": [_rounded(min_x), _rounded(max_x)],
            "y": [_rounded(min_y), _rounded(max_y)],
            "polygon": ring,
            "source_refs": refs,
        }
        if seed and seed["role_evidence"] is not None:
            cell["role_evidence"] = copy.deepcopy(seed["role_evidence"])
        cells.append(cell)
        polygons_by_id[space_id] = pixel_polygon
        space_mapping.append({
            "space_id": space_id,
            "role": cell["role"],
            "pixel_polygon": _canonical_ring(pixel_polygon),
            "world_polygon_m": ring,
            "bbox_m": {"x": cell["x"], "y": cell["y"]},
            "partition_ids": sorted(touching_partitions),
            "source_refs": refs,
            "seed": None if seed is None else {
                "id": seed["id"], "point_pixel": list(seed["point"]),
                "source_refs": seed["source_refs"],
            },
        })

    raw_openings = plan["openings"]
    if not isinstance(raw_openings, list):
        raise TypeError("plan.openings must be a list")
    windows = []
    wall_openings = []
    opening_hosts = []
    opening_ids = set()
    footprint_world = Polygon([world(point) for point in footprint_points])
    world_footprint_ring = _canonical_ring(footprint_world)
    for index, raw in enumerate(raw_openings):
        item = _fields(
            raw, path=f"plan.openings[{index}]", allowed=_OPENING_FIELDS,
            required=_required_opening_fields(plan),
        )
        opening_id = _text(item["id"], path=f"plan.openings[{index}].id")
        if opening_id in opening_ids:
            raise ValueError(f"duplicate opening id {opening_id!r}")
        opening_ids.add(opening_id)
        kind = item["kind"]
        if kind not in {"window", "door", "open"}:
            raise ValueError(f"opening {opening_id}: kind must be window, door, or open")
        p1_pixel = _point(item["p1"], path=f"opening {opening_id}.p1", width=width, height=height)
        p2_pixel = _point(item["p2"], path=f"opening {opening_id}.p2", width=width, height=height)
        _orthogonal([p1_pixel, p2_pixel], path=f"opening {opening_id}", closed=False)
        pixel_line = LineString([p1_pixel, p2_pixel])
        host_ids = [space_id for space_id, poly in polygons_by_id.items() if poly.boundary.covers(pixel_line)]
        host_ids.sort()
        if len(host_ids) not in {1, 2}:
            raise OpeningHostError(
                f"opening {opening_id} at pixels {list(p1_pixel)} -> {list(p2_pixel)} "
                f"requires one exterior or two interior full-boundary hosts; found {host_ids}",
                opening_id=opening_id, p1_pixel=p1_pixel, p2_pixel=p2_pixel,
            )
        exterior = len(host_ids) == 1
        if exterior and not footprint.boundary.covers(pixel_line):
            raise OpeningHostError(
                f"opening {opening_id} at pixels {list(p1_pixel)} -> {list(p2_pixel)} has only "
                "one full space host but is not wholly on the footprint boundary; a T-junction "
                "or partial host cannot be treated as outdoors",
                opening_id=opening_id, p1_pixel=p1_pixel, p2_pixel=p2_pixel,
            )
        z_values = None
        if "z" in item:
            z = item["z"]
            if not isinstance(z, list) or len(z) != 2:
                raise ValueError(f"opening {opening_id}.z must be [absolute_bottom_m, absolute_top_m]")
            z_values = [_number(value, path=f"opening {opening_id}.z[{z_index}]")
                        for z_index, value in enumerate(z)]
            if not z_values[0] < z_values[1]:
                raise ValueError(f"opening {opening_id}.z must have positive height")
            z_top = (z_floor + ceiling_height
                     if z_floor is not None and ceiling_height is not None else None)
            if ((z_floor is not None and z_values[0] < z_floor)
                    or (z_top is not None and z_values[1] > z_top)):
                raise ValueError(
                    f"opening {opening_id}.z {z_values} is outside floor vertical bounds "
                    f"[{z_floor}, {z_top if z_top is not None else 'unknown'}]"
                )
        refs = _strings(item.get("source_refs", [f"image:{image_name}#{opening_id}"]),
                        path=f"opening {opening_id}.source_refs", require_one=True)
        opening_assumptions = _strings(
            item.get("assumptions", []), path=f"opening {opening_id}.assumptions",
        )
        state = item.get("state")
        if state is not None and state not in {"unknown", "open", "closed"}:
            raise ValueError(f"opening {opening_id}.state must be unknown, open, or closed")
        if kind == "window" and state is not None:
            raise ValueError(
                f"opening {opening_id}: explicit window.state is not supported by the "
                "current source-BIM window contract and would be lost"
            )
        if kind == "open" and state not in {None, "open"}:
            raise ValueError(f"opening {opening_id}: an open passage state must be open or omitted")
        p1_world = [_rounded(value) for value in world(p1_pixel)]
        p2_world = [_rounded(value) for value in world(p2_pixel)]
        width_m = _rounded(math.dist(p1_world, p2_world))
        if width_m <= 0:
            raise ValueError(f"opening {opening_id}: six-decimal world coordinates collapse its width")
        facade = None
        if kind == "window":
            if not exterior:
                raise ValueError(f"opening {opening_id}: interior windows are not supported")
            # The CCW ring has its interior on the left. Classify the actual
            # outward edge, including a recessed facade, in the calibrated frame.
            segment = LineString([p1_world, p2_world])
            edges = [(a, b) for a, b in zip(
                world_footprint_ring, world_footprint_ring[1:] + world_footprint_ring[:1])
                if LineString([a, b]).covers(segment)]
            if len(edges) != 1:
                raise ValueError(
                    f"opening {opening_id}: rounded window must lie on one complete footprint edge"
                )
            a, b = edges[0]
            if a[1] == b[1]:
                facade = "South" if b[0] > a[0] else "North"
            else:
                facade = "East" if b[1] > a[1] else "West"
            along = [p1_world[0], p2_world[0]] if facade in {"North", "South"} else [p1_world[1], p2_world[1]]
            window = {
                "id": opening_id, "kind": "window", "floor": floor_id,
                "facade": facade, "span": sorted(along),
                "room": host_ids[0], "p1": p1_world, "p2": p2_world,
                "width_m": width_m, "source_refs": refs,
                "assumptions": opening_assumptions,
            }
            if z_values is not None:
                window["z"] = z_values
            windows.append(window)
        else:
            opening = {
                "id": opening_id, "kind": kind, "space_id": host_ids[0],
                "other_space_id": None if exterior else host_ids[1],
                "p1": p1_world, "p2": p2_world,
                "source_refs": refs, "assumptions": opening_assumptions,
            }
            if z_values is not None:
                opening["z"] = z_values
            if state is not None:
                opening["state"] = state
            wall_openings.append(opening)
        host = {
            "opening_id": opening_id, "kind": kind, "space_ids": host_ids,
            "exterior": exterior, "facade": facade,
            "p1_pixel": list(p1_pixel), "p2_pixel": list(p2_pixel),
            "p1_world_m": p1_world, "p2_world_m": p2_world,
            "width_m": width_m,
            "source_refs": refs,
        }
        if z_values is not None:
            host["z_absolute_m"] = z_values
        opening_hosts.append(host)

    world_footprint = Polygon(world_footprint_ring)
    min_x, min_y, max_x, max_y = world_footprint.bounds
    # Machine method/calibration facts live in metadata and geometry.notes.
    # They are not authored building assumptions or tasks for the model to erase.
    proposal = {
        "geometry": {
            "schema_version": "2",
            "footprint_x": [_rounded(min_x), _rounded(max_x)],
            "footprint_y": [_rounded(min_y), _rounded(max_y)],
            "floors": [{"name": floor_id, "cells": cells}],
            "windows": windows,
            "openings": wall_openings,
            "notes": (
                "Deterministic plan partition compilation from explicit original-image pixels. "
                "Caller-supplied calibration and drawing fidelity remain independently unverified."
            ),
        },
        "assumptions": assumptions,
        "unresolved": unresolved,
    }
    floor = proposal["geometry"]["floors"][0]
    if z_floor is not None:
        floor["z_floor"] = z_floor
    if ceiling_height is not None:
        floor["ceiling_height"] = ceiling_height
    if reading_2d:
        proposal["geometry"]["reading_type"] = "plan"
    if not rectangular_footprint:
        # Same explicit floor ring as the parametric source-proposal adapter.
        # Keep rectangular proposals unchanged for existing replay consumers.
        proposal["geometry"]["floors"][0]["footprint"] = {"vertices": world_footprint_ring}
    metadata = {
        "mode": "deterministic_plan_partition",
        "image_name": image_name,
        "image_size": [width, height],
        "floor_id": floor_id,
        "basis": basis,
        "space_count": len(cells),
        "space_mapping": space_mapping,
        "partition_mapping": [{
            "partition_id": row["id"],
            "pixel_points": [list(point) for point in row["points"]],
            "world_points_m": [[_rounded(value) for value in world(point)] for point in row["points"]],
            "source_refs": row["source_refs"],
        } for row in partitions],
        "opening_hosts": opening_hosts,
        "footprint": {
            "pixel_polygon": _canonical_ring(footprint),
            "world_polygon_m": world_footprint_ring,
        },
        "calibration": {
            "x_anchors": parsed_x, "y_anchors": parsed_y,
            "world_metres_per_pixel": {"x": x_slope, "y": y_slope},
            "basis": basis,
            "status": "caller_supplied_not_independently_verified",
        },
        "method": {
            "polygonizer": "shapely.unary_union+polygonize_full",
            "snap": False, "extend": False, "clip": False,
            "inferred_partitions": False,
        },
    }
    if field_aliases:
        metadata["field_aliases"] = field_aliases
    if reading_2d:
        metadata["reading_type"] = "plan"
    # These records are inert here.  They are persisted for audit/replay; the
    # literal compiler never interprets them as permission to snap geometry.
    if "regularization_inputs" in plan:
        metadata["regularization_inputs"] = copy.deepcopy(plan["regularization_inputs"])
    if "regularization" in plan:
        metadata["regularization"] = copy.deepcopy(plan["regularization"])
    if "wall_metadata" in plan:
        metadata["wall_metadata"] = copy.deepcopy(plan["wall_metadata"])
    return proposal, metadata
