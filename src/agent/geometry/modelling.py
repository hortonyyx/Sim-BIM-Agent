"""建模·几何 — geometry realization vocabulary (no topology decisions).

This module owns the *primitives* for turning corrected room cells into oriented
geometry: the output dataclasses, the per-cell zone volumes, and the functions
that synthesize a face's vertices (walls, floor/ceiling rings, window rects) with
correct outward orientation. It makes **no** decision about which faces exist or
how they correspond — that topology lives in `split_pairing` (which imports from
here, never the reverse).

Polygon-native (shapely): a rectangle is the simple case, so non-orthogonal /
L-shaped rooms need no rewrite.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from shapely.geometry import Point
from shapely.geometry import LineString, MultiLineString, Polygon

from src.agent.correction.cell_geometry import (
    cell_has_polygon,
    cell_polygon as _validated_cell_polygon,
)
from src.agent.correction.config import load_core_tolerances
from src.agent.correction.schema import Cell, CorrectedGeometry
from src.agent.correction.footprint import footprint_bbox
from src.agent.geometry.capability import FEATURE_CELL_POLYGON, require_supported_geometry_contract, schema_supports

# Shared tolerances (single source; split_pairing imports these).
_Z_TOL = 0.02         # m — floors stack when |lower ceiling z - upper floor z| <= this
_AREA_MIN = 0.05      # m^2 — ignore overlaps/segments smaller than this
_NAME_QUANT = 6       # shared precision for deterministic public-name keys


def _min_edge() -> float:
    """The degenerate-sliver floor (m), read from the ACTIVE correction config.

    F-133 R2 (2026-08-28): this used to be a second, independent literal
    (`_MIN_EDGE = 0.10`) sitting next to `correction.yaml`'s
    `min_edge_length_m: 0.100`. The two were not merely the same number by
    coincidence — they are handed to the *same* validator
    (`cell_geometry.cell_polygon`) on two different call paths: the correction
    path (`deterministic.py`, `envelope_transform.py`, `parse.py`,
    `pipeline.py`) passed `tol.min_edge_length_m`, while `_cell_polygon` /
    `_iter_segments` here passed the literal. Measured before this change with
    an override config at `min_edge_length_m: 0.200`: a 0.15 m cell edge was
    REJECTED on the correction path and ACCEPTED here — one validator, two
    answers, and nothing in the tree could see the drift.

    Resolved lazily (not bound at import) because the active config path comes
    from `$EP_AGENT_CORRECTION_CONFIG`, which a caller may set after this
    module is imported; an import-time binding would re-open the same drift it
    is meant to close. `load_core_tolerances` is lru_cached per resolved path,
    so this is a dict lookup.

    ⛔ The VALUE is unchanged (0.100 m under the shipped config). Whether this
    floor should instead track EnergyPlus' own very-small-vertex threshold is a
    separate question and deliberately NOT decided here — see the dispatch's
    §五.2: the geometry kernel's sliver floor (polygon edges / face segments)
    and correction's axis-merge parameter are different quantities that happen
    to share a source today.
    """
    return load_core_tolerances().min_edge_length_m


# --------------------------------------------------------------------------- #
# output dataclasses (re-exported by build.py for back-compat)
# --------------------------------------------------------------------------- #
@dataclass
class Surface:
    name: str
    zone: str
    stype: str              # Wall | Floor | Ceiling | Roof
    verts: list[tuple[float, float, float]]   # CCW from outside
    obc: str                # Outdoors | Ground | Surface | Adiabatic
    obc_obj: str = ""       # reciprocal target surface name (Surface only)


@dataclass
class Window:
    name: str
    parent: str             # parent wall surface name
    verts: list[tuple[float, float, float]]
    source_window_id: str | None = None
    facade_segment_id: str | None = None
    host_resolution_sha256: str | None = None


@dataclass
class Opening:
    """One derived side/piece of an identified source door or empty opening."""

    name: str
    parent: str
    verts: list[tuple[float, float, float]]
    source_opening_id: str
    kind: Literal["door", "open"]
    space_id: str
    other_space_id: str | None
    state: Literal["unknown", "open", "closed"]
    partner: str = ""


@dataclass
class ZoneVolume:
    """One zone's volume: footprint polygon + z range. The leg-agnostic unit
    `split_pairing` consumes — any zonification (faithful rooms / re-topologized
    thermal blocks) produces these; only the count/granularity differs."""

    zone: str               # EP-safe zone name
    cell_id: str            # original cell id (for window room lookup)
    polygon: Polygon
    zf: float
    zt: float
    fi: int                 # floor membership (every zonification has floors);
                            # actual face pairing is z + polygon driven, not fi
    role: str = "office"    # semantic role (for zone_specs serialization)
    handle: str = ""        # short deterministic public handle, e.g. Z01


@dataclass
class BuildingGeometry:
    zones: list[str] = field(default_factory=list)
    surfaces: list[Surface] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    zone_volumes: list[ZoneVolume] = field(default_factory=list)  # for serialization
    geometry_contract: Literal["legacy", "c2_b5_v1"] = "legacy"
    openings: list[Opening] = field(default_factory=list)


@dataclass(frozen=True)
class SegmentLine2D:
    """A finite, directed 2-D host line.

    The direction is part of the contract: callers must preserve ``p1 -> p2``
    when materialising parameter intervals, including negative world axes.
    """

    p1: tuple[float, float]
    p2: tuple[float, float]

    def __post_init__(self) -> None:
        values = (*self.p1, *self.p2)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("host line coordinates must be finite")
        if self.p1 == self.p2:
            raise ValueError("host line must have positive length")

    def point_at(self, t: float) -> tuple[float, float]:
        if not math.isfinite(float(t)):
            raise ValueError("host line parameter must be finite")
        return (
            float(self.p1[0]) + (float(self.p2[0]) - float(self.p1[0])) * float(t),
            float(self.p1[1]) + (float(self.p2[1]) - float(self.p1[1])) * float(t),
        )

    def parameter_of(self, point: tuple[float, float]) -> float:
        if len(point) != 2 or not all(math.isfinite(float(value)) for value in point):
            raise ValueError("host line point must contain two finite coordinates")
        dx = float(self.p2[0]) - float(self.p1[0])
        dy = float(self.p2[1]) - float(self.p1[1])
        denom = dx * dx + dy * dy
        t = (
            (float(point[0]) - float(self.p1[0])) * dx
            + (float(point[1]) - float(self.p1[1])) * dy
        ) / denom
        qx, qy = self.point_at(t)
        if abs(qx - float(point[0])) > 1e-12 or abs(qy - float(point[1])) > 1e-12:
            raise ValueError("point is not on host line")
        return t


class NameRegistry:
    """Hands out unique EP-safe surface/window names across the whole build."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def uname(self, base: str) -> str:
        nm = _safe(base)
        i = 1
        cand = nm
        while cand in self.used:
            i += 1
            cand = f"{nm}_{i}"
        self.used.add(cand)
        return cand


# --------------------------------------------------------------------------- #
# low-level primitives (all geometric helpers live here; one-way dep)
# --------------------------------------------------------------------------- #
def _safe(name: str) -> str:
    """EnergyPlus-safe identifier: letters/digits/_ only."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))


def _q(value: float) -> float:
    return round(float(value), _NAME_QUANT)


def _qpt(pt) -> tuple[float, float]:
    return (_q(pt[0]), _q(pt[1]))


def _role_token(role: str | None) -> str:
    raw = str(role or "").strip()
    if not raw:
        raw = "office"
    parts = re.findall(r"[A-Za-z0-9]+", raw)
    titled = "_".join(part[:1].upper() + part[1:].lower() for part in parts)
    token = re.sub(r"_+", "_", _safe(titled)).strip("_")
    return token or "Office"


def _canonical_ring(poly: Polygon) -> tuple[tuple[float, float], ...]:
    coords = [_qpt(p) for p in list(poly.exterior.coords)[:-1]]
    if len(coords) < 3:
        return tuple(coords)
    if not _is_ccw(coords + [coords[0]]):
        coords = list(reversed(coords))
    start = min(range(len(coords)), key=lambda i: (coords[i][1], coords[i][0]))
    return tuple(coords[start:] + coords[:start])


def _geometry_fingerprint(poly: Polygon) -> tuple:
    minx, miny, maxx, maxy = poly.bounds
    return (
        (_q(minx), _q(miny), _q(maxx), _q(maxy)),
        _q(poly.area),
        _canonical_ring(poly),
    )


def _zone_centroid_key(poly: Polygon) -> tuple[float, float]:
    c = poly.centroid
    return (_q(-c.y), _q(c.x))


def _zone_quadrant(poly: Polygon, footprint_x: list[float], footprint_y: list[float]) -> str:
    cx = (float(footprint_x[0]) + float(footprint_x[1])) / 2.0
    cy = (float(footprint_y[0]) + float(footprint_y[1])) / 2.0
    half_w = max(abs(float(footprint_x[1]) - float(footprint_x[0])) / 2.0, 1e-9)
    half_h = max(abs(float(footprint_y[1]) - float(footprint_y[0])) / 2.0, 1e-9)
    p = poly.centroid
    dx, dy = p.x - cx, p.y - cy
    tol_x = min(half_w * 0.05, 0.5) + 1e-6
    tol_y = min(half_h * 0.05, 0.5) + 1e-6
    near_x = abs(dx) <= tol_x
    near_y = abs(dy) <= tol_y
    if near_x and near_y:
        return "C"
    ns = "" if near_y else ("N" if dy > 0 else "S")
    ew = "" if near_x else ("E" if dx > 0 else "W")
    return ns + ew


def _cell_polygon(c: Cell) -> Polygon:
    """Cell -> validated CCW shapely Polygon. Prefer explicit polygon."""
    return _validated_cell_polygon(c, min_edge_length_m=_min_edge())


def _is_ccw(coords: list) -> bool:
    s = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i][0], coords[i][1]
        x2, y2 = coords[i + 1][0], coords[i + 1][1]
        s += (x2 - x1) * (y2 + y1)
    return s < 0  # shoelace: negative sum => CCW


def _newell(verts: list[tuple[float, float, float]]) -> np.ndarray:
    pts = np.asarray(verts, float)
    n = np.zeros(3)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    m = np.linalg.norm(n)
    return n / m if m > 1e-12 else n


def _orient(verts: list, desired: np.ndarray) -> list:
    """Reverse vertex order if the polygon normal opposes `desired`."""
    if float(np.dot(_newell(verts), desired)) < 0:
        return verts[::-1]
    return verts


def _iter_segments(geom):
    """Yield straight ((x1,y1),(x2,y2)) segments from a Line/MultiLineString."""
    if geom is None or geom.is_empty:
        return
    lines = []
    if isinstance(geom, LineString):
        lines = [geom]
    elif isinstance(geom, MultiLineString):
        lines = list(geom.geoms)
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            if isinstance(g, (LineString, MultiLineString)):
                yield from _iter_segments(g)
        return
    for ln in lines:
        cs = list(ln.coords)
        for i in range(len(cs) - 1):
            p1, p2 = cs[i], cs[i + 1]
            if LineString([p1, p2]).length >= _min_edge():
                yield (float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))


def _polys(geom):
    """Yield Polygon parts from a possibly-Multi/GeometryCollection result."""
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                yield g


# --------------------------------------------------------------------------- #
# face vertex synthesis
# --------------------------------------------------------------------------- #
def _local_outward_normal(p1, p2, owner_poly: Polygon) -> np.ndarray | None:
    """Return the wall's local outward XY normal for a segment on owner_poly.

    A single zone centroid/representative point is not enough for concave cells:
    a boundary edge in one wing can have the global representative point on the
    other side of the edge. Probe both sides of the segment midpoint and pick
    the side outside the owning polygon.
    """
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = float(np.hypot(dx, dy))
    if length <= 1e-12:
        return None
    mid = np.array([(p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0], dtype=float)
    n1 = np.array([dy / length, -dx / length], dtype=float)
    eps = min(1e-4, max(length / 1000.0, 1e-7))
    inside1 = owner_poly.covers(Point(*(mid + n1 * eps)))
    inside2 = owner_poly.covers(Point(*(mid - n1 * eps)))
    if inside1 and not inside2:
        return np.array([-n1[0], -n1[1], 0.0])
    if inside2 and not inside1:
        return np.array([n1[0], n1[1], 0.0])
    return None


def _wall_verts(p1, p2, zf, zt, interior_pt, owner_poly: Polygon | None = None) -> list:
    """Vertical wall rectangle, oriented so the outward normal points away from
    `interior_pt` (the owning zone's centroid)."""
    v = [
        (p1[0], p1[1], zt),
        (p2[0], p2[1], zt),
        (p2[0], p2[1], zf),
        (p1[0], p1[1], zf),
    ]
    # outward horizontal direction: perpendicular to (p2-p1), away from interior
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    nrm = _local_outward_normal(p1, p2, owner_poly) if owner_poly is not None else None
    if nrm is None:
        nrm = np.array([dy, -dx, 0.0])  # one perpendicular
        inward = np.array([interior_pt[0] - mx, interior_pt[1] - my, 0.0])
        if float(np.dot(nrm, inward)) > 0:  # points toward interior -> flip desired
            nrm = -nrm
    return _orient(v, nrm)


def _ring_verts(poly: Polygon, z: float, up: bool) -> list:
    coords = list(poly.exterior.coords)[:-1]
    v = [(float(x), float(y), z) for x, y in coords]
    return _orient(v, np.array([0.0, 0.0, 1.0 if up else -1.0]))


def _facade_axis(facade: str) -> str:
    return "x" if facade.lower().startswith(("e", "w")) else "y"


# Outward normal (x, y) a wall must have to belong to a given facade.
_FACADE_NORMAL = {
    "north": (0.0, 1.0),
    "south": (0.0, -1.0),
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
}


def _legacy_cardinal_window_verts(w, parent: Surface) -> list:
    """Window rectangle on the parent wall's plane, CCW from outside."""
    span = sorted(float(s) for s in w.span)
    z0, z1 = sorted(float(z) for z in w.z)
    axis = _facade_axis(w.facade)
    if axis == "y":  # N/S: plane y=const
        y = parent.verts[0][1]
        v = [(span[0], y, z0), (span[1], y, z0), (span[1], y, z1), (span[0], y, z1)]
    else:            # E/W: plane x=const
        x = parent.verts[0][0]
        v = [(x, span[0], z0), (x, span[1], z0), (x, span[1], z1), (x, span[0], z1)]
    # orient to match the parent wall's outward normal
    return _orient(v, _newell(parent.verts))


def _validate_window_line_inputs(
    *,
    host_line: SegmentLine2D,
    parameter_interval: tuple[float, float],
    z_interval: tuple[float, float],
    outward_normal_xy: tuple[float, float],
) -> None:
    if len(parameter_interval) != 2:
        raise ValueError("parameter_interval must contain exactly two values")
    if len(z_interval) != 2:
        raise ValueError("z_interval must contain exactly two values")
    if len(outward_normal_xy) != 2:
        raise ValueError("outward_normal_xy must contain exactly two values")
    t0, t1 = (float(value) for value in parameter_interval)
    z0, z1 = (float(value) for value in z_interval)
    nx, ny = (float(value) for value in outward_normal_xy)
    if not all(math.isfinite(value) for value in (t0, t1, z0, z1, nx, ny)):
        raise ValueError("window line inputs must be finite")
    if not 0.0 <= t0 < t1 <= 1.0:
        raise ValueError("parameter_interval requires 0 <= t0 < t1 <= 1")
    if not z0 < z1:
        raise ValueError("z_interval requires z0 < z1")
    normal_length = math.hypot(nx, ny)
    if abs(normal_length - 1.0) > 1e-12:
        raise ValueError("outward_normal_xy must be a unit vector")
    dx = float(host_line.p2[0]) - float(host_line.p1[0])
    dy = float(host_line.p2[1]) - float(host_line.p1[1])
    line_length = math.hypot(dx, dy)
    if abs((dx * nx + dy * ny) / line_length) > 1e-12:
        raise ValueError("outward_normal_xy must be perpendicular to host_line")


def window_verts_on_line(
    *,
    host_line: SegmentLine2D,
    parameter_interval: tuple[float, float],
    z_interval: tuple[float, float],
    outward_normal_xy: tuple[float, float],
) -> list[tuple[float, float, float]]:
    """Materialise a window rectangle from only line, parameters, z and normal."""
    _validate_window_line_inputs(
        host_line=host_line,
        parameter_interval=parameter_interval,
        z_interval=z_interval,
        outward_normal_xy=outward_normal_xy,
    )
    t0, t1 = parameter_interval
    z0, z1 = z_interval
    q0x, q0y = host_line.point_at(t0)
    q1x, q1y = host_line.point_at(t1)
    v = [
        (q0x, q0y, z0),
        (q1x, q1y, z0),
        (q1x, q1y, z1),
        (q0x, q0y, z1),
    ]
    nx, ny = outward_normal_xy
    return _orient(v, np.array([nx, ny, 0.0], dtype=float))


def _window_wall_source_span(
    wall: Surface,
    span_axis: int,
    shared_endpoints: set[tuple[float, float]],
) -> tuple[float, float]:
    """Recover only the endpoint erosion made by ``pair_surfaces``.

    split_pairing subtracts shared walls with ``buffer(1e-6)``. Its original
    endpoint remains on the paired interior wall, so use that evidence instead
    of expanding every exterior wall by a generic window tolerance. Neither
    wall nor window vertices are changed. Near-cardinal, slanted walls do not
    receive this axis-aligned endpoint recovery.
    """
    normal_axis = 1 - span_axis
    along = [v[span_axis] for v in wall.verts]
    across = [v[normal_axis] for v in wall.verts]
    result = [min(along), max(along)]
    plane = across[0]
    plane_eps = 8 * max(math.ulp(value) for value in across)
    if max(across) - min(across) > plane_eps:
        return tuple(result)
    for index, direction in ((0, -1), (1, 1)):
        clipped = result[index]
        for point in sorted(shared_endpoints):
            eps = 8 * max(math.ulp(value) for value in (*point, clipped, 1e-6))
            if (abs(point[normal_axis] - plane) <= plane_eps
                    and abs(point[span_axis] - clipped - direction * 1e-6) <= eps):
                result[index] = point[span_axis]
                break
    return tuple(result)


def _find_parent_wall(surfaces: list[Surface], zone: str, w) -> Surface | None:
    """Pick the zone's exterior wall on the window facade whose XY span covers it.

    The wall's outward normal must point in the facade direction — a span/axis
    match alone is not enough (a full-depth room has constant-y exterior walls
    on BOTH north and south; without the normal check a South window would
    silently attach to whichever matching wall came last). A window may end at
    its unique host's endpoint; touching is not crossing into a second wall.
    """
    span = sorted(float(s) for s in w.span)
    axis = _facade_axis(w.facade)
    want = _FACADE_NORMAL[w.facade.strip().lower()]
    matches: list[Surface] = []
    shared_endpoints = {
        (float(v[0]), float(v[1]))
        for s in surfaces
        if s.zone == zone and s.stype == "Wall" and s.obc == "Surface"
        for v in s.verts
    }
    for s in surfaces:
        if s.zone != zone or s.stype != "Wall" or s.obc != "Outdoors":
            continue
        n = _newell(s.verts)
        if float(n[0] * want[0] + n[1] * want[1]) < 0.9:
            continue  # wall does not face the window's facade
        xs = [v[0] for v in s.verts]
        ys = [v[1] for v in s.verts]
        # wall must be (near) constant in the facade-normal axis and span the window
        if axis == "y":  # N/S facade: wall runs along x, ~constant y
            if max(ys) - min(ys) > 0.05:
                continue
            span_axis = 0
        else:            # E/W facade: wall runs along y, ~constant x
            if max(xs) - min(xs) > 0.05:
                continue
            span_axis = 1
        seg = _window_wall_source_span(s, span_axis, shared_endpoints)
        # Only floating-point representation slack remains after recovering
        # the proven cut endpoint. Real overruns, even sub-micrometre ones,
        # cannot be absorbed by the pairing buffer a second time.
        eps = 8 * max(math.ulp(value) for value in (*span, *seg))
        if seg[0] - eps <= span[0] and span[1] <= seg[1] + eps:
            matches.append(s)
    if len(matches) > 1:
        raise ValueError(
            f"window {w.id}: ambiguous parent wall on {w.facade} for {w.room}; "
            f"window span {span} falls inside {len(matches)} same-orientation "
            f"wall segment spans: {[s.name for s in matches]}"
        )
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# zone volumes + window attachment (geometry realization, not topology)
# --------------------------------------------------------------------------- #
def build_zone_volumes(
    geom: CorrectedGeometry, *, capability_profile: str = "rectangular"
) -> tuple[list[ZoneVolume], list[str]]:
    """Cells -> zone volumes, returned in deterministic public-name order. Also returns tiling
    guard notes: same-floor cells must not overlap (a correction stage defect — e.g. a
    corridor placed over the rooms it should sit between produces same-side walls
    the gate rejects). Flag, don't paper over.

    Hard guards (raise, never silently corrupt — the InterZone gate is blind to
    both failure classes):
      - cell ids must be globally unique across floors (every id-keyed map in
        split_pairing / window attachment is last-wins on duplicates);
      - the z-stack must be contiguous: a gap/overlap beyond the pairing
        tolerance would silently model an internal floor interface as Roof +
        exposed Floor (mid-building outdoors) with zero gate issues.
    """
    require_supported_geometry_contract(geom, capability_profile)

    # ---- source-id uniqueness guard ----
    floor_of_id: dict[str, str] = {}
    for fl in geom.floors:
        for c in fl.cells:
            if cell_has_polygon(c) and not schema_supports(geom, FEATURE_CELL_POLYGON):
                raise ValueError(
                    f"cell {c.id}: polygon requires schema_version '2' or a schema version with feature '{FEATURE_CELL_POLYGON}'"
                )
            if c.id in floor_of_id:
                raise ValueError(
                    f"duplicate cell id '{c.id}' on floors '{floor_of_id[c.id]}' "
                    f"and '{fl.name}' — cell ids must be globally unique across "
                    f"floors (A0 id_uniqueness); duplicates silently merge zones "
                    f"and mis-pair surfaces"
                )
            floor_of_id[c.id] = fl.name

    # ---- z-stack continuity guard ----
    floors_by_z = sorted(enumerate(geom.floors), key=lambda item: float(item[1].z_floor))
    for prev, cur in zip(floors_by_z, floors_by_z[1:]):
        top = float(prev[1].z_floor) + float(prev[1].ceiling_height)
        gap = float(cur[1].z_floor) - top
        if abs(gap) > _Z_TOL:
            raise ValueError(
                f"broken z-stack: floor '{cur[1].name}' starts at z={cur[1].z_floor} "
                f"but floor '{prev[1].name}' tops out at z={top} (gap {gap:+.3f} m "
                f"> {_Z_TOL} m tolerance) — split-pairing would silently model "
                f"this interface as Roof + exposed Floor (mid-building outdoors); "
                f"fix the correction-stage z values"
            )

    zvs: list[ZoneVolume] = []
    notes: list[str] = []
    floor_name_by_rank: dict[int, str] = {}
    for rank, (_orig_i, fl) in enumerate(floors_by_z):
        floor_name_by_rank[rank] = fl.name
        zf = float(fl.z_floor)
        zt = zf + float(fl.ceiling_height)
        for c in fl.cells:
            zvs.append(
                ZoneVolume(
                    "", c.id, _cell_polygon(c), zf, zt, rank,
                    getattr(c, "role", "office") or "office",
                )
            )

    seen_fingerprint: dict[tuple[int, tuple], str] = {}
    for zv in zvs:
        fp = _geometry_fingerprint(zv.polygon)
        key = (zv.fi, fp)
        if key in seen_fingerprint:
            raise ValueError(
                f"duplicate same-floor zone geometry fingerprint for cell ids "
                f"'{seen_fingerprint[key]}' and '{zv.cell_id}' — deterministic "
                f"zone serials require unique geometry"
            )
        seen_fingerprint[key] = zv.cell_id

    zvs.sort(
        key=lambda zv: (
            zv.fi,
            *_zone_centroid_key(zv.polygon),
            _geometry_fingerprint(zv.polygon),
            _safe(zv.cell_id),
        )
    )
    width = max(2, len(str(len(zvs))))
    for idx, zv in enumerate(zvs, start=1):
        zv.handle = f"Z{idx:0{width}d}"
        role = _role_token(zv.role)
        (fx0, fx1), (fy0, fy1) = footprint_bbox(geom, geom.floors[zv.fi])
        quadrant = _zone_quadrant(zv.polygon, [fx0, fx1], [fy0, fy1])
        zv.zone = f"{zv.handle}_F{zv.fi + 1}_{role}_{quadrant}"

    by_fi: dict[int, list[ZoneVolume]] = {}
    for zv in zvs:
        by_fi.setdefault(zv.fi, []).append(zv)
    for fi, group in by_fi.items():
        fname = floor_name_by_rank.get(fi, f"F{fi + 1}")
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ov = group[i].polygon.intersection(group[j].polygon).area
                if ov > _AREA_MIN:
                    notes.append(
                        f"OVERLAP: cells '{group[i].cell_id}' and "
                        f"'{group[j].cell_id}' on floor {fname} overlap by "
                        f"{ov:.2f} m^2 — cells must tile, not overlap "
                        f"(upstream correction stage/correction defect)"
                    )
    return zvs, notes


def attach_windows(
    geom: CorrectedGeometry,
    surfaces: list[Surface],
    zv_by_cell: dict[str, ZoneVolume],
    registry: NameRegistry,
) -> tuple[list[Window], list[str]]:
    """Attach each window to its room's exterior wall on that facade. Runs after
    all walls exist so `_find_parent_wall` sees the finished exterior-wall set."""
    windows: list[Window] = []
    pending: list[tuple[Window, str]] = []
    notes: list[str] = []
    for w in geom.windows:
        if w.room not in zv_by_cell:
            notes.append(f"window {w.id}: room '{w.room}' not found; skipped")
            continue
        zone = zv_by_cell[w.room].zone
        parent = _find_parent_wall(surfaces, zone, w)
        if parent is None:
            notes.append(f"window {w.id}: no exterior wall on {w.facade} for {w.room}")
            continue
        verts = _legacy_cardinal_window_verts(w, parent)
        if verts:
            win = Window("", parent.name, verts, source_window_id=str(w.id))
            windows.append(win)
            pending.append((win, str(w.id)))

    by_parent: dict[str, list[tuple[Window, str]]] = {}
    for win, src in pending:
        by_parent.setdefault(win.parent, []).append((win, str(src)))

    def win_key(item: tuple[Window, str]) -> tuple:
        win, src = item
        xs = [v[0] for v in win.verts]
        ys = [v[1] for v in win.verts]
        zs = [v[2] for v in win.verts]
        if max(xs) - min(xs) >= max(ys) - min(ys):
            a0, a1 = min(xs), max(xs)
        else:
            a0, a1 = min(ys), max(ys)
        return (_q(a0), _q(a1), _q(min(zs)), _q(max(zs)), _safe(src))

    named: list[Window] = []
    for parent in sorted(by_parent):
        items = sorted(by_parent[parent], key=win_key)
        for idx, (win, _src) in enumerate(items, start=1):
            win.name = f"{parent}_Win{idx}"
            named.append(win)
    return named, notes


class WindowParentBindingError(ValueError):
    """Typed, blocking B5 geometry-realisation failure."""

    def __init__(self, code: str, *, window_id: str, context: dict | None = None):
        self.code = code
        self.window_id = window_id
        self.context = dict(context or {})
        super().__init__(f"{code}: window={window_id}: {self.context}")


def _surface_base_line(surface: Surface, *, window_id: str) -> SegmentLine2D:
    points: list[tuple[float, float]] = []
    for vertex in surface.verts:
        point = (float(vertex[0]), float(vertex[1]))
        if point not in points:
            points.append(point)
    if len(points) != 2:
        raise WindowParentBindingError(
            "invalid_host_line",
            window_id=window_id,
            context={"surface": surface.name, "xy_point_count": len(points)},
        )
    try:
        return SegmentLine2D(points[0], points[1])
    except ValueError as exc:
        raise WindowParentBindingError(
            "invalid_host_line",
            window_id=window_id,
            context={"surface": surface.name},
        ) from exc


def _point_line_residual(point: tuple[float, float], line: SegmentLine2D) -> float:
    dx = float(line.p2[0]) - float(line.p1[0])
    dy = float(line.p2[1]) - float(line.p1[1])
    return abs(
        dx * (float(line.p1[1]) - float(point[1]))
        - (float(line.p1[0]) - float(point[0])) * dy
    ) / math.hypot(dx, dy)


def _line_contains_point(
    line: SegmentLine2D,
    point: tuple[float, float],
    *,
    plane_epsilon: float,
    span_epsilon: float,
) -> bool:
    if _point_line_residual(point, line) > plane_epsilon:
        return False
    dx = float(line.p2[0]) - float(line.p1[0])
    dy = float(line.p2[1]) - float(line.p1[1])
    denom = dx * dx + dy * dy
    t = (
        (float(point[0]) - float(line.p1[0])) * dx
        + (float(point[1]) - float(line.p1[1])) * dy
    ) / denom
    parameter_epsilon = span_epsilon / math.sqrt(denom)
    return -parameter_epsilon <= t <= 1.0 + parameter_epsilon


def _polygon_area_3d(verts: list[tuple[float, float, float]]) -> float:
    if len(verts) < 3:
        return 0.0
    origin = np.asarray(verts[0], dtype=float)
    area_vector = np.zeros(3, dtype=float)
    for index in range(1, len(verts) - 1):
        area_vector += np.cross(
            np.asarray(verts[index], dtype=float) - origin,
            np.asarray(verts[index + 1], dtype=float) - origin,
        )
    return float(np.linalg.norm(area_vector)) / 2.0


def _v3_parent_candidates(
    *,
    surfaces: list[Surface],
    zone: str,
    resolution,
    plane_epsilon: float,
    span_epsilon: float,
) -> list[tuple[Surface, SegmentLine2D]]:
    segment_line = SegmentLine2D(
        (resolution.segment_p1.x, resolution.segment_p1.y),
        (resolution.segment_p2.x, resolution.segment_p2.y),
    )
    endpoints = tuple(
        (point.x, point.y)
        for point in resolution.clamped_plan_endpoints_p1_to_p2
    )
    want_nx, want_ny = resolution.segment_outward_normal
    matches: list[tuple[Surface, SegmentLine2D]] = []
    for surface in surfaces:
        if surface.zone != zone or surface.stype != "Wall" or surface.obc != "Outdoors":
            continue
        line = _surface_base_line(surface, window_id=resolution.window_id)
        if (
            _point_line_residual(line.p1, segment_line) > plane_epsilon
            or _point_line_residual(line.p2, segment_line) > plane_epsilon
        ):
            continue
        normal = _newell(surface.verts)
        if (
            abs(float(normal[0]) - float(want_nx)) > plane_epsilon
            or abs(float(normal[1]) - float(want_ny)) > plane_epsilon
            or abs(float(normal[2])) > plane_epsilon
        ):
            continue
        if not all(
            _line_contains_point(
                line,
                endpoint,
                plane_epsilon=plane_epsilon,
                span_epsilon=span_epsilon,
            )
            for endpoint in endpoints
        ):
            continue
        matches.append((surface, line))
    return matches


def attach_windows_v3(
    geom: CorrectedGeometry,
    surfaces: list[Surface],
    zv_by_cell: dict[str, ZoneVolume],
    registry: NameRegistry,
    *,
    host_claims,
    tolerances,
) -> list[Window]:
    """Attach B5 windows from verified resolution records, never facade guesses."""
    source_ids = [str(window.id) for window in geom.windows]
    resolution_ids = [str(row.window_id) for row in host_claims.resolutions]
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(resolution_ids):
        raise WindowParentBindingError(
            "resolver_output_tampered",
            window_id=next(iter(source_ids), "<none>"),
            context={"source_window_ids": sorted(source_ids), "resolution_ids": sorted(resolution_ids)},
        )
    windows_by_id = {str(window.id): window for window in geom.windows}
    pending: list[tuple[Window, str]] = []
    for resolution in host_claims.resolutions:
        source = windows_by_id[resolution.window_id]
        if (
            source.room != resolution.room_id
            or source.facade_segment_id != resolution.facade_segment_id
            or tuple(float(value) for value in source.span)
            != (resolution.clamped_span.lo, resolution.clamped_span.hi)
        ):
            raise WindowParentBindingError(
                "resolver_output_tampered",
                window_id=resolution.window_id,
                context={"reason": "final window identity disagrees with resolution"},
            )
        zone_volume = zv_by_cell.get(resolution.room_id)
        if zone_volume is None:
            raise WindowParentBindingError(
                "parent_wall_zero_candidates",
                window_id=resolution.window_id,
                context={"reason": "resolved room has no built zone"},
            )
        candidates = _v3_parent_candidates(
            surfaces=surfaces,
            zone=zone_volume.zone,
            resolution=resolution,
            plane_epsilon=tolerances.window_host_plane_epsilon_m,
            span_epsilon=tolerances.window_host_span_epsilon_m,
        )
        if len(candidates) != 1:
            raise WindowParentBindingError(
                "parent_wall_zero_candidates" if not candidates else "parent_wall_multiple_candidates",
                window_id=resolution.window_id,
                context={"candidate_surfaces": sorted(surface.name for surface, _line in candidates)},
            )
        parent, _parent_line = candidates[0]
        host_line = SegmentLine2D(
            (resolution.segment_p1.x, resolution.segment_p1.y),
            (resolution.segment_p2.x, resolution.segment_p2.y),
        )
        verts = window_verts_on_line(
            host_line=host_line,
            parameter_interval=(
                resolution.segment_parameter_interval.lo,
                resolution.segment_parameter_interval.hi,
            ),
            z_interval=(resolution.z_interval.lo, resolution.z_interval.hi),
            outward_normal_xy=tuple(float(value) for value in resolution.segment_outward_normal),
        )
        record_verts = [
            (point.x, point.y, point.z) for point in resolution.clamped_vertices
        ]
        if verts != record_verts:
            raise WindowParentBindingError(
                "resolver_output_tampered",
                window_id=resolution.window_id,
                context={"reason": "record vertices disagree with fresh line recompute"},
            )
        parent_z = [float(vertex[2]) for vertex in parent.verts]
        if (
            min(vertex[2] for vertex in verts) < min(parent_z) - tolerances.window_host_span_epsilon_m
            or max(vertex[2] for vertex in verts) > max(parent_z) + tolerances.window_host_span_epsilon_m
        ):
            raise WindowParentBindingError(
                "parent_wall_zero_candidates",
                window_id=resolution.window_id,
                context={"reason": "window z interval lies outside parent"},
            )
        window_area = _polygon_area_3d(verts)
        parent_area = _polygon_area_3d(parent.verts)
        if not 0.0 < window_area < parent_area:
            raise WindowParentBindingError(
                "parent_wall_zero_candidates",
                window_id=resolution.window_id,
                context={"reason": "window area must be positive and smaller than parent"},
            )
        built = Window(
            "",
            parent.name,
            verts,
            source_window_id=resolution.window_id,
            facade_segment_id=resolution.facade_segment_id,
            host_resolution_sha256=resolution.resolution_sha256,
        )
        pending.append((built, resolution.window_id))

    by_parent: dict[str, list[tuple[Window, str]]] = {}
    for built, source_id in pending:
        by_parent.setdefault(built.parent, []).append((built, source_id))

    def key(item: tuple[Window, str]) -> tuple:
        built, source_id = item
        xs = [vertex[0] for vertex in built.verts]
        ys = [vertex[1] for vertex in built.verts]
        zs = [vertex[2] for vertex in built.verts]
        along = (min(xs), max(xs)) if max(xs) - min(xs) >= max(ys) - min(ys) else (min(ys), max(ys))
        return (_q(along[0]), _q(along[1]), _q(min(zs)), _q(max(zs)), _safe(source_id))

    named: list[Window] = []
    for parent in sorted(by_parent):
        for index, (built, _source_id) in enumerate(sorted(by_parent[parent], key=key), start=1):
            built.name = registry.uname(f"{parent}_Win{index}")
            named.append(built)
    return named
