"""Small, ordinary-Python construction helpers for the existing BIM formats.

These functions do not select room uses, infer doors, repair geometry, run tools,
or certify a building. Coordinates and distances are in the caller's metre-based
plan frame. Shapely polygons and existing ``{polygon, holes}`` dictionaries can be
composed without introducing another plan language.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, unary_union

from src.agent.sdk import edge_segment


_ARC_SEGMENTS = 32
_COMPONENT_SAMPLE_LIMIT = 16


def _polygon(value, label):
    if isinstance(value, Polygon):
        polygon = value
    elif isinstance(value, Mapping) and "polygon" in value:
        polygon = Polygon(value["polygon"], value.get("holes", []))
    else:
        raise ValueError(f"{label} must be a Shapely Polygon or a polygon/holes mapping")
    rings = [polygon.exterior, *polygon.interiors]
    if (polygon.is_empty or polygon.has_z or not polygon.is_valid
            or not math.isfinite(polygon.area) or polygon.area <= 0
            or any(len(p) != 2 or not all(math.isfinite(x) for x in p)
                   for ring in rings for p in ring.coords)):
        raise ValueError(f"{label} must be a finite valid positive-area 2D polygon; no repair is applied")
    # The existing source format requires holes strictly inside the exterior.
    exterior = Polygon(polygon.exterior)
    holes = [Polygon(ring) for ring in polygon.interiors]
    for i, hole in enumerate(holes):
        if (not exterior.contains(hole) or exterior.boundary.intersects(hole.boundary)
                or any(hole.intersects(other) for other in holes[:i])):
            raise ValueError(f"{label} holes must be disjoint and strictly inside the exterior")
    return polygon


def _fields(polygon):
    polygon = orient(polygon, sign=1)
    return {"polygon": [list(p) for p in list(polygon.exterior.coords)[:-1]],
            "holes": [[list(p) for p in list(ring.coords)[:-1]] for ring in polygon.interiors]}


def region(*parts, subtract=()):
    """Union explicit polygon parts, subtract explicit parts, and return fields.

    Returns existing ``polygon`` and ``holes`` fields for a space or footprint.
    A disconnected result is rejected, never reduced to its largest component.
    No snapping, buffering, gap filling, simplification or validity repair occurs.
    Use separate calls/space identities when the intended result is disconnected.
    """
    if not parts:
        raise ValueError("region needs at least one explicit polygon part")
    result = unary_union([_polygon(p, f"part {i}") for i, p in enumerate(parts)])
    removed = [_polygon(p, f"subtract {i}") for i, p in enumerate(subtract)]
    if removed:
        result = result.difference(unary_union(removed))
    if result.geom_type != "Polygon" or result.is_empty:
        raise ValueError(f"region result is {result.geom_type}, empty={result.is_empty}; "
                         "one nonempty connected Polygon is required, no components are discarded")
    return _fields(_polygon(result, "region result"))


def _lines(geometry):
    if geometry.geom_type in ("LineString", "LinearRing"):
        if geometry.length > 0:
            yield LineString(geometry.coords)
    elif hasattr(geometry, "geoms"):
        for item in geometry.geoms:
            yield from _lines(item)


def _straight(a, b, c):
    ab = (b[0] - a[0], b[1] - a[1])
    bc = (c[0] - b[0], c[1] - b[1])
    # Exact collinearity only: a small real bend is not silently straightened.
    return ab[0] * bc[1] == ab[1] * bc[0] and ab[0] * bc[0] + ab[1] * bc[1] > 0


def _straight_runs(line):
    points = [tuple(p) for p in line.coords]
    runs = []
    start = previous = points[0]
    for point in points[1:]:
        if point == previous:
            continue
        if start != previous and not _straight(start, previous, point):
            runs.append((start, previous))
            start = previous
        previous = point
    if start != previous:
        runs.append((start, previous))
    if (len(runs) > 1 and runs[-1][1] == runs[0][0]
            and _straight(runs[-1][0], runs[0][0], runs[0][1])):
        runs = [(runs[-1][0], runs[0][1]), *runs[1:-1]]
    return runs


def shared_edges(left, right):
    """Return all actual shared straight segments, with deterministic ordering.

    Arguments are Polygon objects or polygon/holes mappings. Interior overlap is
    rejected. Only actual boundary intersections count: no nearest-wall matching
    or tolerance welding. Collinear fragmented segments are joined, but turns and
    disconnected segments remain separate. Each p1/p2 direction and list order is
    lexicographic in the current plan frame; indices refer to these exact inputs.
    The caller must choose the appropriate segment, not assume the longest one.
    Compose shared geometry in one local frame before a common transform when
    possible. Independently transformed/subdivided rings can differ at floating
    precision; such tiny overlaps or broken exact contacts are not repaired here.
    """
    left, right = _polygon(left, "left"), _polygon(right, "right")
    overlap_area = left.intersection(right).area
    if overlap_area > 0:
        raise ValueError(f"space interiors overlap (area_m2={overlap_area}); a shared wall cannot be selected; "
                         "reuse shared construction coordinates, no tolerance repair is applied")
    pieces = list(_lines(left.boundary.intersection(right.boundary)))
    if not pieces:
        return []
    joined = linemerge(pieces) if len(pieces) > 1 else pieces[0]
    segments = {tuple(sorted((a, b))) for line in _lines(joined)
                for a, b in _straight_runs(line)}
    return [{"p1": list(a), "p2": list(b), "length_m": math.dist(a, b)}
            for a, b in sorted(segments)]


def shared_opening(left, right, *, edge, offset, width):
    """Place an explicitly sized opening on an explicitly selected shared edge.

    ``edge`` is an index from ``shared_edges(left, right)``. ``offset`` starts at
    that segment's p1; ``width`` is never shrunk to fit. Returns existing p1/p2
    aperture fields. Supply IDs, spaces, height and evidence in the normal plan.
    The normal source compiler still validates the final adopted geometry.
    """
    edges = shared_edges(left, right)
    if isinstance(edge, bool) or not isinstance(edge, int) or not 0 <= edge < len(edges):
        raise ValueError(f"choose an explicit shared edge index in 0..{len(edges)-1}; found {len(edges)} edges")
    selected = edges[edge]
    try:
        return edge_segment(selected["p1"], selected["p2"], offset=offset, width=width)
    except ValueError as error:
        raise ValueError(f"opening does not fit shared edge {edge}: available_length_m={selected['length_m']}, "
                         f"offset_m={offset}, requested_width_m={width}; no shrinking or clipping") from error


def clearance_report(value, *, width, include_geometry=False):
    """Report a caller-sized circular body's approximate feasible centre region.

    This erodes the polygon by width/2. Components indicate where a disk centre
    could lie and move in this 2D region, not whether doors, stairs, origins and
    destinations connect. No default/legal width and no quality-pass decision is
    supplied. Round arcs use 32 chords per quadrant; near-threshold topology may
    depend on this approximation and floating-point geometry.
    """
    if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or width <= 0:
        raise ValueError("width must be an explicit finite positive diameter in metres")
    if not isinstance(include_geometry, bool):
        raise ValueError("include_geometry must be a boolean")
    polygon = _polygon(value, "circulation region")
    radius = width / 2
    eroded = polygon.buffer(-radius, quad_segs=_ARC_SEGMENTS)
    components = ([] if eroded.is_empty else [eroded] if eroded.geom_type == "Polygon"
                  else list(eroded.geoms))
    components.sort(key=lambda item: (*item.bounds, item.area))
    samples = components if include_geometry else components[:_COMPONENT_SAMPLE_LIMIT]
    return {
        "diagnostic_only": True,
        "method": "approximate Euclidean disk-centre erosion in the supplied 2D plan frame",
        "width_m": float(width), "radius_m": radius,
        "arc_segments_per_quadrant": _ARC_SEGMENTS,
        "max_arc_chord_deviation_m": radius * (1 - math.cos(math.pi / (4 * _ARC_SEGMENTS))),
        "input_area_m2": polygon.area, "input_bounds_xy": list(polygon.bounds),
        "centre_area_m2": eroded.area, "component_count": len(components),
        "components": [{"area_m2": p.area, "bounds_xy": list(p.bounds),
                        "representative_point_xy": list(p.representative_point().coords[0]),
                        **({"geometry": _fields(p)} if include_geometry else {})} for p in samples],
        "components_truncated": len(samples) < len(components),
        "limitations": [
            "No door/entry/destination reachability or building-quality decision; use these facts with the actual plan.",
            "Circular cross-section only; turning shape, stairs, height, wall thickness and regulatory compliance are not evaluated.",
            "Arc chord deviation is a geometric approximation bound, not a certified topology or numerical-error bound.",
        ],
    }
