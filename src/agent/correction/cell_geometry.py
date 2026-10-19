"""Shared polygon-first geometry helpers for corrected cells.

Schema v2 keeps ``Cell.x``/``Cell.y`` as required bbox projections for legacy
consumers. Any geometric consumer should use this module so explicit polygons
are validated and used consistently.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from shapely.geometry import Polygon

# W5 (R-4): production and the judge share ONE orthogonality yardstick through
# the production/judge orthogonality module, so the two can never disagree about
# a near-axis edge (the structural root of the false-red rounds: the judge used
# its own exactness ceiling to convict legal upstream geometry).  The legitimacy
# gate below CALLS the shared predicate -- it does not re-derive the condition --
# so tightening the epsilon is one edit in one place (R-4 two-stage gating).
from src.agent.correction.orthogonality import ORTHOGONALITY_EPSILON as _EPS, edge_is_axis_aligned
_BBOX_TOL = 1e-6


def cell_has_polygon(cell: Any) -> bool:
    return _raw_polygon(cell) is not None


def _raw_polygon(cell: Any):
    poly = getattr(cell, "polygon", None)
    if poly is None:
        poly = (getattr(cell, "__pydantic_extra__", None) or {}).get("polygon")
    return poly


def _as_pair(raw: Any, cell_id: str, idx: int) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not [x, y]")
    try:
        x = float(raw[0])
        y = float(raw[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not numeric") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not finite")
    return x, y


def _cell_id(cell: Any) -> str:
    return str(getattr(cell, "id", "<unknown>"))


def _is_ccw(coords: Iterable[tuple[float, float]]) -> bool:
    pts = list(coords)
    s = 0.0
    for a, b in zip(pts, pts[1:]):
        s += (b[0] - a[0]) * (b[1] + a[1])
    return s < 0


def cell_polygon_vertices(
    cell: Any, *, allow_closed: bool = False, minimum_vertices: int = 4
) -> list[tuple[float, float]] | None:
    raw = _raw_polygon(cell)
    if raw is None:
        return None
    cid = _cell_id(cell)
    if not isinstance(raw, list):
        raise ValueError(f"cell {cid}: polygon must be a list of [x, y] vertices")
    if len(raw) < minimum_vertices:
        raise ValueError(f"cell {cid}: polygon must contain at least {minimum_vertices} vertices")
    pts = [_as_pair(p, cid, i) for i, p in enumerate(raw)]
    if pts[0] == pts[-1]:
        if not allow_closed:
            raise ValueError(
                f"cell {cid}: polygon must not repeat the first vertex at the end"
            )
        pts = pts[:-1]
        if len(pts) < minimum_vertices:
            raise ValueError(f"cell {cid}: polygon must contain at least {minimum_vertices} vertices")
    return pts


def _rect_vertices(cell: Any) -> list[tuple[float, float]]:
    cid = _cell_id(cell)
    if len(cell.x) != 2 or len(cell.y) != 2:
        raise ValueError(f"cell {cid}: x and y must be [min, max] pairs")
    x0, x1 = float(cell.x[0]), float(cell.x[1])
    y0, y1 = float(cell.y[0]), float(cell.y[1])
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def cell_bbox(cell: Any, *, validate_polygon: bool = True) -> tuple[float, float, float, float]:
    if cell_has_polygon(cell):
        poly = cell_polygon(cell) if validate_polygon else Polygon(cell_polygon_vertices(cell))
        minx, miny, maxx, maxy = poly.bounds
        return float(minx), float(miny), float(maxx), float(maxy)
    if len(cell.x) != 2 or len(cell.y) != 2:
        raise ValueError(f"cell {_cell_id(cell)}: x and y must be [min, max] pairs")
    return min(cell.x), min(cell.y), max(cell.x), max(cell.y)


def validate_cell_polygon(
    cell: Any,
    *,
    min_edge_length_m: float | None = None,
    require_bbox_match: bool = True,
    allow_cw: bool = False,
    allow_closed: bool = False,
) -> None:
    if not cell_has_polygon(cell):
        return
    cell_polygon(
        cell,
        min_edge_length_m=min_edge_length_m,
        require_bbox_match=require_bbox_match,
        allow_cw=allow_cw,
        allow_closed=allow_closed,
    )


def normalized_ccw_polygon(cell: Any) -> list[list[float]] | None:
    """Return an encoding-canonicalized rewrite of the polygon ring, or None.

    Ring encoding is not geometry: a CW ring or an explicitly-closed ring
    (first vertex repeated at the end) describes the same room, and LLM
    producers routinely emit both. The deterministic core canonicalizes
    through this helper (and logs the rewrite) instead of failing the draw:
    - explicit closure → the duplicate closing vertex is stripped;
    - CW winding → reversed to CCW (start vertex preserved).
    Returns None when the cell has no polygon or the encoding is already
    canonical (open ring, CCW).
    """
    raw = _raw_polygon(cell)
    if raw is None:
        return None
    pts = cell_polygon_vertices(cell, allow_closed=True)
    was_closed = len(pts) != len(raw)
    was_cw = not _is_ccw(pts + [pts[0]])
    if not was_closed and not was_cw:
        return None
    if was_cw:
        pts = [pts[0]] + pts[1:][::-1]
    return [[float(x), float(y)] for x, y in pts]


def cell_polygon(
    cell: Any,
    *,
    min_edge_length_m: float | None = None,
    require_bbox_match: bool = True,
    allow_cw: bool = False,
    allow_closed: bool = False,
    require_orthogonal: bool = True,
) -> Polygon:
    pts = cell_polygon_vertices(cell, allow_closed=allow_closed, minimum_vertices=4 if require_orthogonal else 3)
    if pts is None:
        pts = _rect_vertices(cell)
    cid = _cell_id(cell)
    closed = pts + [pts[0]]

    for i, (a, b) in enumerate(zip(closed, closed[1:])):
        dx = abs(b[0] - a[0])
        dy = abs(b[1] - a[1])
        length = math.hypot(dx, dy)
        if length <= _EPS:
            raise ValueError(f"cell {cid}: polygon edge {i} is degenerate")
        # W5 (R-4): the legitimacy question is answered by the SHARED predicate
        # (edge_is_axis_aligned), not a re-derived local condition -- the judge
        # calls classify_edge_orthogonality on the same epsilon, so an edge this
        # gate admits can never be branded a topology break downstream.
        if require_orthogonal and not edge_is_axis_aligned(dx, dy):
            raise ValueError(f"cell {cid}: polygon edge {i} is not orthogonal")
        if min_edge_length_m is not None and length < min_edge_length_m - _EPS:
            raise ValueError(
                f"cell {cid}: polygon edge {i} length {length:.6f} m is below "
                f"min_edge_length_m {min_edge_length_m:.6f} m"
            )

    if not allow_cw and not _is_ccw(closed):
        raise ValueError(f"cell {cid}: polygon exterior ring must be CCW")

    poly = Polygon(pts)
    if poly.is_empty or poly.area <= _EPS:
        raise ValueError(f"cell {cid}: polygon is degenerate")
    if not poly.is_valid:
        raise ValueError(f"cell {cid}: polygon is invalid/self-intersecting")

    if require_bbox_match and cell_has_polygon(cell):
        minx, miny, maxx, maxy = poly.bounds
        expected_x = [float(min(cell.x)), float(max(cell.x))]
        expected_y = [float(min(cell.y)), float(max(cell.y))]
        actual_x = [float(minx), float(maxx)]
        actual_y = [float(miny), float(maxy)]
        if (
            abs(expected_x[0] - actual_x[0]) > _BBOX_TOL
            or abs(expected_x[1] - actual_x[1]) > _BBOX_TOL
            or abs(expected_y[0] - actual_y[0]) > _BBOX_TOL
            or abs(expected_y[1] - actual_y[1]) > _BBOX_TOL
        ):
            raise ValueError(
                f"cell {cid}: x/y bbox {expected_x}/{expected_y} does not match "
                f"polygon bbox {actual_x}/{actual_y}"
            )
    return poly


def cell_axis_values(cell: Any, axis: str) -> list[float]:
    pts = cell_polygon_vertices(cell)
    if pts is None:
        values = cell.x if axis == "x" else cell.y
        return [float(values[0]), float(values[1])]
    idx = 0 if axis == "x" else 1
    return [float(p[idx]) for p in pts]


def set_cell_polygon_vertices(cell: Any, vertices: list[tuple[float, float]]) -> None:
    cell.polygon = [[float(x), float(y)] for x, y in vertices]
    minx = min(x for x, _ in vertices)
    maxx = max(x for x, _ in vertices)
    miny = min(y for _, y in vertices)
    maxy = max(y for _, y in vertices)
    cell.x = [minx, maxx]
    cell.y = [miny, maxy]


def cell_facade_span(cell: Any, facade: str) -> tuple[float, float]:
    """Return the along-facade span of a cell's bbox projection.

    This is intentionally a coarse containment span for current v2 window checks;
    parent-wall selection in the kernel uses actual polygon wall segments.
    """
    minx, miny, maxx, maxy = cell_bbox(cell)
    if facade in ("North", "South"):
        return minx, maxx
    return miny, maxy
