"""切配·仿真 — split-pairing (which faces exist + 1:1 reciprocal correspondence).

Leg-agnostic: consumes a list of `ZoneVolume` (footprint polygon + z range) and
decides every face's existence and outside-boundary condition. Adjacent faces are
cut at the union of both sides' breaks so each interzone face corresponds 1:1 with
exactly one opposite face, with reciprocal `Outside Boundary Condition = Surface`.
The zonification regime (faithful rooms vs re-topologized thermal blocks) only
changes the input count, never this algorithm.

  - vertical interior walls: per same-floor cell pair, the shared boundary segment
    becomes a reciprocal wall pair; the rest of each cell edge is exterior (Outdoors)
  - horizontal floor/ceiling: a cell's ceiling ∩ each stacked upper-floor cell's
    footprint becomes a reciprocal ceiling/floor pair; uncovered ceiling -> roof
    (Outdoors); bottom floor -> Ground; exposed floor underside -> Outdoors

Tolerances reuse the InterZone gate's thresholds so a clean build passes by
construction. Imports geometry primitives from `modelling` only (one-way dep).
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from src.agent.geometry.adjacency import zone_volumes_by_floor
from src.agent.geometry.modelling import (
    _AREA_MIN,
    _Z_TOL,
    NameRegistry,
    Surface,
    ZoneVolume,
    _canonical_ring,
    _iter_segments,
    _polys,
    _q,
    _qpt,
    _safe,
    _ring_verts,
    _wall_verts,
)
from src.agent.geometry.capability import CAPABILITY_PROFILE_SHAPES


def pair_surfaces(
    zvs: list[ZoneVolume],
    registry: NameRegistry,
    *,
    capability_profile: str = "rectangular",
) -> list[Surface]:
    if capability_profile not in CAPABILITY_PROFILE_SHAPES:
        raise ValueError(f"unknown capability_profile {capability_profile!r}")
    surfaces: list[Surface] = []
    pair_refs: list[tuple[Surface, Surface]] = []

    def add(zone, stype, verts, obc, obc_obj="") -> Surface:
        s = Surface("", zone, stype, verts, obc, obc_obj)
        surfaces.append(s)
        return s

    zv_by_id: dict[str, ZoneVolume] = {zv.cell_id: zv for zv in zvs}
    by_floor = {
        fi: [zv.cell_id for zv in group]
        for fi, group in zone_volumes_by_floor(zvs).items()
    }

    # ---- 1. vertical walls + same-floor interzone pairing ----
    shared_acc: dict[str, list] = {zv.cell_id: [] for zv in zvs}

    for cids in by_floor.values():
        for ai in range(len(cids)):
            for bi in range(ai + 1, len(cids)):
                A, B = cids[ai], cids[bi]
                za, zb = zv_by_id[A], zv_by_id[B]
                inter = za.polygon.boundary.intersection(zb.polygon.boundary)
                for p1, p2 in _iter_segments(inter):
                    # same-floor cells share a z-range, but use each wall's own
                    # zone volume so the code stays correct if that ever changes
                    wa = _wall_verts(
                        p1,
                        p2,
                        za.zf,
                        za.zt,
                        za.polygon.representative_point().coords[0],
                        za.polygon,
                    )
                    wb = _wall_verts(
                        p1,
                        p2,
                        zb.zf,
                        zb.zt,
                        zb.polygon.representative_point().coords[0],
                        zb.polygon,
                    )
                    sa = add(za.zone, "Wall", wa, "Surface")
                    sb = add(zb.zone, "Wall", wb, "Surface")
                    pair_refs.append((sa, sb))
                    shared_acc[A].append(LineString([p1, p2]))
                    shared_acc[B].append(LineString([p1, p2]))

    # exterior wall segments = cell boundary minus all shared segments
    for zv in zvs:
        boundary = zv.polygon.boundary
        if shared_acc[zv.cell_id]:
            ext = boundary.difference(unary_union(shared_acc[zv.cell_id]).buffer(1e-6))
        else:
            ext = boundary
        for p1, p2 in _iter_segments(ext):
            wv = _wall_verts(
                p1,
                p2,
                zv.zf,
                zv.zt,
                zv.polygon.representative_point().coords[0],
                zv.polygon,
            )
            add(zv.zone, "Wall", wv, "Outdoors")

    # ---- 2. horizontal floor/ceiling + cross-floor pairing ----
    for zv in zvs:
        fi, zf, zt, zone, poly = zv.fi, zv.zf, zv.zt, zv.zone, zv.polygon

        # FLOOR
        if fi == 0:
            add(zone, "Floor", _ring_verts(poly, zf, up=False), "Ground")
        else:
            lower = [
                zv_by_id[c]
                for c in by_floor.get(fi - 1, [])
                if abs(zv_by_id[c].zt - zf) <= _Z_TOL
            ]
            covered = []
            for L in lower:
                ov = poly.intersection(L.polygon)
                for piece in _polys(ov):
                    if piece.area < _AREA_MIN:
                        continue
                    fs = add(zone, "Floor", _ring_verts(piece, zf, up=False), "Surface")
                    cs = add(L.zone, "Ceiling", _ring_verts(piece, zf, up=True), "Surface")
                    pair_refs.append((fs, cs))
                    covered.append(piece)
            rem = poly.difference(unary_union(covered)) if covered else poly
            for piece in _polys(rem):
                if piece.area >= _AREA_MIN:
                    add(zone, "Floor", _ring_verts(piece, zf, up=False), "Outdoors")

        # CEILING / ROOF (paired ceilings already created above from the lower side;
        # here handle the uncovered remainder of THIS cell's ceiling = roof)
        upper = [
            zv_by_id[c]
            for c in by_floor.get(fi + 1, [])
            if abs(zv_by_id[c].zf - zt) <= _Z_TOL
        ]
        covered_top = []
        for U in upper:
            ov = poly.intersection(U.polygon)
            for piece in _polys(ov):
                if piece.area >= _AREA_MIN:
                    covered_top.append(piece)
        rem_top = poly.difference(unary_union(covered_top)) if covered_top else (
            None if not upper else poly
        )
        if not upper:
            add(zone, "Roof", _ring_verts(poly, zt, up=True), "Outdoors")
        elif rem_top is not None:
            for piece in _polys(rem_top):
                if piece.area >= _AREA_MIN:
                    add(zone, "Roof", _ring_verts(piece, zt, up=True), "Outdoors")

    zone_by_name = {zv.zone: zv for zv in zvs}
    zone_index = {zv.zone: i for i, zv in enumerate(zvs)}
    pair_partner: dict[int, Surface] = {}
    for a, b in pair_refs:
        pair_partner[id(a)] = b
        pair_partner[id(b)] = a

    def wall_xy_pair(s: Surface) -> tuple[tuple[float, float], tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for v in s.verts:
            p = _qpt((v[0], v[1]))
            if p not in pts:
                pts.append(p)
        if len(pts) < 2:
            raise ValueError(f"cannot name degenerate wall surface in zone {s.zone}")
        a, b = pts[0], pts[1]
        return tuple(sorted((a, b)))  # type: ignore[return-value]

    def ring_position(zv: ZoneVolume, pt: tuple[float, float]) -> float:
        ring = list(_canonical_ring(zv.polygon))
        if len(ring) < 2:
            return 0.0
        best = (float("inf"), 0.0)
        walked = 0.0
        px, py = pt
        for a, b in zip(ring, ring[1:] + ring[:1]):
            ax, ay = a
            bx, by = b
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            seg_len = math.sqrt(seg_len2)
            if seg_len2 <= 1e-18:
                continue
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
            qx, qy = ax + t * dx, ay + t * dy
            dist2 = (px - qx) ** 2 + (py - qy) ** 2
            cand = (dist2, walked + t * seg_len)
            if cand < best:
                best = cand
            walked += seg_len
        return _q(best[1])

    def wall_key(s: Surface) -> tuple:
        zv = zone_by_name[s.zone]
        a, b = wall_xy_pair(s)
        mid = ((_q(a[0] + b[0]) / 2.0), (_q(a[1] + b[1]) / 2.0))
        partner = pair_partner.get(id(s))
        partner_handle = zone_by_name[partner.zone].handle if partner else ""
        length = math.dist(a, b)
        obc_rank = {"Outdoors": 0, "Ground": 1, "Surface": 2, "Adiabatic": 3}.get(s.obc, 9)
        return (ring_position(zv, mid), a, b, _q(length), obc_rank, partner_handle)

    def horizontal_poly(s: Surface):
        poly = Polygon([(v[0], v[1]) for v in s.verts])
        if not poly.is_valid:
            poly = poly.buffer(0)
        return poly

    def horizontal_key(s: Surface) -> tuple:
        poly = horizontal_poly(s)
        c = poly.centroid
        minx, miny, maxx, maxy = poly.bounds
        return (
            _q(-c.y), _q(c.x),
            (_q(minx), _q(miny), _q(maxx), _q(maxy)),
            _q(poly.area),
            tuple(_qpt((v[0], v[1])) for v in s.verts),
        )

    surface_order: dict[int, tuple] = {}
    type_rank = {"Wall": 0, "Floor": 1, "Ceiling": 2, "Roof": 3}
    for zv in zvs:
        group = [s for s in surfaces if s.zone == zv.zone]
        walls = [s for s in group if s.stype == "Wall"]
        keyed_walls = sorted(((wall_key(s), s) for s in walls), key=lambda ks: ks[0])
        for prev, cur in zip(keyed_walls, keyed_walls[1:]):
            if prev[0] == cur[0]:
                raise ValueError(
                    f"ambiguous wall ring naming key in zone {zv.zone}; "
                    f"surface order would depend on geometry iteration"
                )
        for idx, (_key, s) in enumerate(keyed_walls, start=1):
            s.name = f"{zv.handle}_W{idx}"
            surface_order[id(s)] = (zone_index[zv.zone], type_rank[s.stype], idx)

        for stype in ("Floor", "Ceiling", "Roof"):
            items = sorted(((horizontal_key(s), s) for s in group if s.stype == stype), key=lambda ks: ks[0])
            for prev, cur in zip(items, items[1:]):
                if prev[0] == cur[0]:
                    raise ValueError(f"ambiguous {stype.lower()} naming key in zone {zv.zone}")
            multi = len(items) > 1
            for idx, (_key, s) in enumerate(items, start=1):
                suffix = str(idx) if multi else ""
                s.name = f"{zv.handle}_{stype}{suffix}"
                surface_order[id(s)] = (zone_index[zv.zone], type_rank[stype], idx)

    for a, b in pair_refs:
        a.obc_obj = b.name
        b.obc_obj = a.name

    return sorted(
        surfaces,
        key=lambda s: surface_order.get(id(s), (zone_index.get(s.zone, 999999), 9, _safe(s.name))),
    )
