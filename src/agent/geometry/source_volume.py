"""Exact horizontal sections of one connected source volume.

The optional sections describe geometry, never extra rooms or storeys.  Legacy
spaces remain one prism; top-level polygon/holes are the full XY projection.
"""
from __future__ import annotations

import math

from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

EPS = 1e-7


def _dict(value):
    return value if isinstance(value, dict) else value.model_dump(mode="json")


def section_polygon(section):
    row = _dict(section)
    return Polygon(row["polygon"], row.get("holes", []))


def validate_section_polygon(section):
    """Reject malformed rings rather than repairing supplied source geometry."""
    row = _dict(section)
    for ring in [row["polygon"], *row.get("holes", [])]:
        if len(ring) < 3 or any(len(p) != 2 or any(not math.isfinite(float(v)) for v in p) for p in ring):
            raise ValueError("vertical section requires finite polygon rings")
        if ring[0] == ring[-1] or any(math.dist(a, b) <= EPS for a, b in zip(ring, ring[1:] + ring[:1])):
            raise ValueError("vertical section rings must be open and have nondegenerate edges")
    poly = section_polygon(row)
    if not poly.is_valid or poly.is_empty or poly.area <= EPS ** 2:
        raise ValueError("vertical section polygon must be valid and nonempty")
    outer = Polygon(row["polygon"])
    if any(not outer.contains(Polygon(h)) or outer.boundary.intersects(Polygon(h).boundary)
           for h in row.get("holes", [])):
        raise ValueError("vertical section holes must be strictly inside its exterior")
    return poly


def validate_vertical_sections(sections, polygon, holes, z_floor, height):
    """Validate ordered, contiguous, connected slabs and their exact projection.

    Returns JSON-compatible records. Numerical seams within EPS are normalized
    to the preceding slab/top-level height so the resulting shell is closed.
    """
    rows = [_dict(row) for row in sections]
    if not rows:
        return []
    if len(rows) > 64:
        raise ValueError("one source space supports at most 64 vertical sections")
    bottom, top = float(z_floor), float(z_floor) + float(height)
    normalized, shapes = [], []
    previous = bottom
    for index, row in enumerate(rows):
        bounds = row.get("z", [])
        if len(bounds) != 2 or any(not math.isfinite(float(v)) for v in bounds):
            raise ValueError("vertical section z must be two finite absolute elevations")
        z0, z1 = map(float, bounds)
        if z1 - z0 <= EPS:
            raise ValueError("vertical section height must be positive")
        if abs(z0 - previous) > EPS:
            raise ValueError("vertical sections must be sorted and contiguous across the full space height")
        z0 = previous
        if index == len(rows) - 1:
            if abs(z1 - top) > EPS:
                raise ValueError("vertical sections must cover the full source floor height")
            z1 = top
        shape = validate_section_polygon(row)
        if shapes and shapes[-1].intersection(shape).area <= EPS ** 2:
            raise ValueError("adjacent vertical sections must share positive area for one connected space")
        normalized.append({"z": [z0, z1], "polygon": [list(p) for p in row["polygon"]],
                           "holes": [[list(p) for p in ring] for ring in row.get("holes", [])]})
        shapes.append(shape)
        previous = z1
    projection = Polygon(polygon, holes)
    actual = unary_union(shapes)
    if (not projection.is_valid or projection.is_empty
            or actual.difference(projection.buffer(EPS)).area > EPS ** 2
            or projection.difference(actual.buffer(EPS)).area > EPS ** 2):
        raise ValueError("vertical sections projection must equal the source space polygon and holes")
    return normalized


def space_sections(space):
    """Return slab records for a SourceSpace dict/model, including old prisms."""
    row = _dict(space)
    if row.get("vertical_sections"):
        return [_dict(section) for section in row["vertical_sections"]]
    return [{"z": [float(row["z_floor"]), float(row["z_floor"]) + float(row["height"])],
             "polygon": row["polygon"], "holes": row.get("holes", [])}]


def space_polygon_at_z(space, z):
    """Return a cross section; an interface belongs to the slab above it.

    The very top is included for boundary inspection. Other out-of-range
    elevations return an empty Polygon rather than the bounding projection.
    """
    rows = space_sections(space)
    for index, row in enumerate(rows):
        lo, hi = row["z"]
        if lo <= z < hi or (index == len(rows) - 1 and z == hi):
            return section_polygon(row)
    return Polygon()


def space_contains_point(space, point):
    """Whether a 3D point is strictly in the source volume (XY boundary excluded)."""
    return space_polygon_at_z(space, float(point[2])).contains(Point(point[:2]))


def space_volume(space):
    return sum(section_polygon(row).area * (row["z"][1] - row["z"][0]) for row in space_sections(space))


def space_overlap_volume(first, second, *, area_function=None):
    """Intersect actual slabs, optionally using the kernel's seam predicate."""
    area_function = area_function or (lambda a, b: a.intersection(b).area)
    volume = 0.0
    for a in space_sections(first):
        for b in space_sections(second):
            height = min(a["z"][1], b["z"][1]) - max(a["z"][0], b["z"][0])
            if height > EPS:
                volume += area_function(section_polygon(a), section_polygon(b)) * height
    return volume


def _parts(shape):
    if shape.geom_type == "Polygon":
        return [shape] if shape.area > EPS ** 2 else []
    return [part for item in getattr(shape, "geoms", []) for part in _parts(item)]


def _canonical_ring(ring):
    points = [list(point) for point in list(ring.coords)[:-1]]
    start = min(range(len(points)), key=points.__getitem__)
    return points[start:] + points[:start]


def section_shell(sections, *, wall_breakpoints=()):
    """Return the oriented external shell of contiguous slabs as face records.

    Horizontal faces are CCW for the common builder to orient floors downward.
    Walls are oriented outward and begin with a horizontal edge, as required by
    the existing wall projection/opening contract. Coplanar wall union removes
    artificial section seams without changing the physical volume.
    """
    slabs = [(row["z"], orient(section_polygon(row), sign=1)) for row in sections]
    rows, groups = [], []
    for bounds, polygon in slabs:
        z0, z1 = bounds
        for ring in [polygon.exterior, *polygon.interiors]:
            points = list(ring.coords)[:-1]
            for a, b in zip(points, points[1:] + points[:1]):
                length = math.dist(a, b)
                direction = [(b[0] - a[0]) / length, (b[1] - a[1]) / length]
                group = next((g for g in groups
                              if max(abs(g["direction"][i] - direction[i]) for i in (0, 1)) <= EPS
                              and abs((a[0] - g["origin"][0]) * direction[1]
                                      - (a[1] - g["origin"][1]) * direction[0]) <= EPS), None)
                if group is None:
                    group = {"origin": a, "direction": direction, "polygons": []}
                    groups.append(group)
                origin, direction = group["origin"], group["direction"]
                project = lambda p: sum((p[i] - origin[i]) * direction[i] for i in (0, 1))
                lo, hi = sorted((project(a), project(b)))
                group["polygons"].append(box(lo, z0, hi, z1))

    def horizontal(shape, z, kind):
        for polygon in sorted(_parts(shape), key=lambda p: (p.bounds, p.wkb_hex)):
            polygon = orient(polygon.simplify(0), sign=1)
            rows.append({"geometry_type": kind,
                         "vertices": [[*p, z] for p in _canonical_ring(polygon.exterior)],
                         "holes": [[[*p, z] for p in _canonical_ring(h)] for h in polygon.interiors]})

    horizontal(slabs[0][1], slabs[0][0][0], "floor")
    for (lower_z, lower), (_, upper) in zip(slabs, slabs[1:]):
        horizontal(lower.difference(upper), lower_z[1], "ceiling")
        horizontal(upper.difference(lower), lower_z[1], "floor")
    horizontal(slabs[-1][1], slabs[-1][0][1], "ceiling")

    for group in groups:
        origin, direction = group["origin"], group["direction"]
        wall = unary_union(group["polygons"])
        lo, z0, hi, z1 = wall.bounds
        cuts = sorted({sum((p[i] - origin[i]) * direction[i] for i in (0, 1))
                       for p in wall_breakpoints
                       if abs((p[0] - origin[0]) * direction[1] - (p[1] - origin[1]) * direction[0]) <= EPS})
        edges = [lo, *[v for v in cuts if lo + EPS < v < hi - EPS], hi]
        shapes = [wall.intersection(box(a, z0, b, z1)) for a, b in zip(edges, edges[1:])]
        for shape in shapes:
            for polygon in sorted(_parts(shape), key=lambda p: (p.bounds, p.wkb_hex)):
                polygon = orient(polygon.simplify(0), sign=1)
                points = _canonical_ring(polygon.exterior)
                starts = [i for i, (a, b) in enumerate(zip(points, points[1:] + points[:1]))
                          if abs(a[1] - b[1]) <= EPS and abs(a[0] - b[0]) > EPS]
                start = min(starts, key=lambda i: points[i])
                points = points[start:] + points[:start]
                lift = lambda ring: [[origin[0] + u * direction[0], origin[1] + u * direction[1], z] for u, z in ring]
                rows.append({"geometry_type": "wall", "vertices": lift(points),
                             "holes": [lift(_canonical_ring(h)) for h in polygon.interiors]})
    return rows
