"""Vg: gt-blind pure-geometry facade visibility (C2 §E1', proposals/c2_vg_detail_spec.md v2).

This module derives, per floor, the North/South/East/West boundary-ring
facade segments and each segment's half-open visible interval(s) under a 1D
per-direction skyline (nearer depth occludes farther depth; a same-depth tie
on a positive-width overlap is an INVARIANT, never a silent tiebreak).

Purity contract (spec §4.2): no file/env/network/clock/random/log I/O, no
gt/judge/manifest/LLM/reading import, no `load_core_tolerances()` call inside
any function here (tolerances are resolved by the caller and passed in as an
explicit `VisibilityTolerances`), and no mutation of caller-owned inputs.
Only `schema.py` types, `floor_footprint`/`floor_footprint_fingerprint`,
stdlib math/hashlib/json, and the two frame types from `facade.py` may be
imported.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal, Sequence

from src.agent.correction.facade import FacadeFamily, FacadeSegmentFrame
from src.agent.correction.footprint import floor_footprint, floor_footprint_fingerprint
from src.agent.correction.schema import CorrectedGeometryV3, FacadeSegment, WorldInterval

CardinalDirection = tuple[Literal[-1, 0, 1], Literal[-1, 0, 1]]

Point = tuple[float, float]

# direction -> (facade_family, along-axis)
_FAMILY_BY_DIRECTION: dict[CardinalDirection, FacadeFamily] = {
    (0, -1): "South",
    (0, 1): "North",
    (1, 0): "East",
    (-1, 0): "West",
}
_DIRECTION_BY_FAMILY: dict[FacadeFamily, CardinalDirection] = {
    v: k for k, v in _FAMILY_BY_DIRECTION.items()
}
_AXIS_BY_FAMILY: dict[FacadeFamily, Literal["x", "y"]] = {
    "South": "x", "North": "x", "East": "y", "West": "y",
}
# Whether p1 (the family's standard start point, §3.1) sits at along_lo.
_P1_AT_ALONG_LO: dict[FacadeFamily, bool] = {
    "South": True, "East": True, "North": False, "West": False,
}
_FAMILY_RANK: dict[FacadeFamily, int] = {"North": 0, "South": 1, "East": 2, "West": 3}
_FAMILY_ORDER: tuple[FacadeFamily, ...] = ("North", "South", "East", "West")


class FacadeVisibilityInvariantError(ValueError):
    """Deterministic INVARIANT/BLOCK for Vg (spec §5.2). Never a soft downgrade."""

    def __init__(self, code: str, context: dict):
        self.code = code
        self.context = dict(context)
        super().__init__(f"{code}: {self.context}")


@dataclass(frozen=True)
class VisibilityTolerances:
    depth_epsilon_m: float
    endpoint_epsilon_m: float


@dataclass(frozen=True)
class DerivedVisibleSegment:
    frame: FacadeSegmentFrame
    visible_intervals: tuple[tuple[float, float], ...]
    canonical_edge_key: tuple


@dataclass(frozen=True)
class _Candidate:
    frame: FacadeSegmentFrame
    canonical_edge_key: tuple


def _norm_zero(x: float) -> float:
    """`-0.0 -> 0.0`; leaves every other value (including other zeros) alone."""
    return 0.0 if x == 0.0 else x


# --------------------------------------------------------------------------- #
# §5.1 ring parsing / canonicalization
# --------------------------------------------------------------------------- #
def _parse_points(vertices: Sequence[Sequence[float]]) -> list[Point]:
    pts: list[Point] = []
    for raw in vertices:
        try:
            x_raw, y_raw = raw
        except (TypeError, ValueError):
            raise FacadeVisibilityInvariantError(
                "visibility_non_finite_coordinate", {"reason": "point is not a 2-tuple", "value": repr(raw)}
            )
        for v in (x_raw, y_raw):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise FacadeVisibilityInvariantError(
                    "visibility_non_finite_coordinate", {"reason": "non-numeric or bool coordinate", "value": repr(v)}
                )
            if not math.isfinite(float(v)):
                raise FacadeVisibilityInvariantError(
                    "visibility_non_finite_coordinate", {"reason": "non-finite coordinate", "value": repr(v)}
                )
        pts.append((float(x_raw), float(y_raw)))
    return pts


def _strip_single_closure(pts: list[Point]) -> list[Point]:
    if len(pts) > 1 and pts[0] == pts[-1]:
        return pts[:-1]
    return pts


def _signed_area(pts: list[Point]) -> float:
    n = len(pts)
    return sum(
        pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
        for i in range(n)
    ) / 2.0


def _canonical_rotate(pts: list[Point]) -> list[Point]:
    start = min(range(len(pts)), key=lambda i: pts[i])
    return pts[start:] + pts[:start]


def _reject_zero_or_short_edges(pts: list[Point], endpoint_epsilon_m: float) -> None:
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length <= endpoint_epsilon_m:
            raise FacadeVisibilityInvariantError(
                "visibility_zero_or_short_edge", {"a": a, "b": b, "length": length}
            )


def _reject_repeated_vertices(pts: list[Point]) -> None:
    seen: dict[Point, int] = {}
    for i, p in enumerate(pts):
        if p in seen:
            raise FacadeVisibilityInvariantError(
                "visibility_repeated_vertex", {"point": p, "positions": [seen[p], i]}
            )
        seen[p] = i


def _reject_non_orthogonal_edges(pts: list[Point]) -> None:
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx != 0.0 and dy != 0.0:
            raise FacadeVisibilityInvariantError(
                "visibility_non_orthogonal_edge", {"a": a, "b": b, "dx": dx, "dy": dy}
            )


def _edge_dirs(pts: list[Point]) -> list[tuple[int, int]]:
    n = len(pts)
    dirs = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        dirs.append((0 if dx == 0 else (1 if dx > 0 else -1), 0 if dy == 0 else (1 if dy > 0 else -1)))
    return dirs


def _merge_collinear(pts: list[Point]) -> list[Point]:
    n = len(pts)
    dirs = _edge_dirs(pts)
    keep = [True] * n
    for i in range(n):
        prev_dir = dirs[i - 1]
        this_dir = dirs[i]
        if prev_dir == this_dir:
            keep[i] = False
        elif prev_dir == (-this_dir[0], -this_dir[1]):
            raise FacadeVisibilityInvariantError(
                "visibility_backtrack", {"vertex": pts[i], "incoming": prev_dir, "outgoing": this_dir}
            )
    return [p for p, k in zip(pts, keep) if k]


def _segments_touch_or_cross(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """Orthogonal axis-aligned segment intersection/touch test (closed intervals)."""
    def _is_horizontal(p1, p2):
        return p1[1] == p2[1]

    a_h = _is_horizontal(a1, a2)
    b_h = _is_horizontal(b1, b2)

    def _interval(v1, v2):
        return (min(v1, v2), max(v1, v2))

    if a_h and b_h:
        if a1[1] != b1[1]:
            return False
        lo_a, hi_a = _interval(a1[0], a2[0])
        lo_b, hi_b = _interval(b1[0], b2[0])
        return lo_a <= hi_b and lo_b <= hi_a
    if (not a_h) and (not b_h):
        if a1[0] != b1[0]:
            return False
        lo_a, hi_a = _interval(a1[1], a2[1])
        lo_b, hi_b = _interval(b1[1], b2[1])
        return lo_a <= hi_b and lo_b <= hi_a
    # one horizontal, one vertical -> at most a point intersection.
    h1, h2, v1, v2 = (a1, a2, b1, b2) if a_h else (b1, b2, a1, a2)
    h_y = h1[1]
    h_lo, h_hi = _interval(h1[0], h2[0])
    v_x = v1[0]
    v_lo, v_hi = _interval(v1[1], v2[1])
    return h_lo <= v_x <= h_hi and v_lo <= h_y <= v_hi


def _reject_self_intersection(pts: list[Point]) -> None:
    n = len(pts)
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if adjacent:
                continue
            a1, a2 = edges[i]
            b1, b2 = edges[j]
            if _segments_touch_or_cross(a1, a2, b1, b2):
                raise FacadeVisibilityInvariantError(
                    "visibility_self_intersection", {"edge_a": (a1, a2), "edge_b": (b1, b2)}
                )


def _canonicalize_ring(vertices: Sequence[Sequence[float]], tolerances: VisibilityTolerances) -> tuple[Point, ...]:
    pts = _parse_points(vertices)
    pts = _strip_single_closure(pts)
    if len(pts) < 4 or len(set(pts)) < 4:
        raise FacadeVisibilityInvariantError(
            "visibility_too_few_vertices", {"count": len(pts), "distinct": len(set(pts))}
        )
    _reject_zero_or_short_edges(pts, tolerances.endpoint_epsilon_m)
    _reject_repeated_vertices(pts)
    _reject_non_orthogonal_edges(pts)
    area = _signed_area(pts)
    if area == 0.0:
        raise FacadeVisibilityInvariantError("visibility_zero_area", {"vertex_count": len(pts)})
    if area < 0.0:
        pts = list(reversed(pts))
    pts = _canonical_rotate(pts)
    pts = _merge_collinear(pts)
    if len(pts) < 4:
        raise FacadeVisibilityInvariantError(
            "visibility_too_few_vertices", {"count": len(pts), "reason": "collapsed after collinear merge"}
        )
    pts = _canonical_rotate(pts)
    _reject_self_intersection(pts)
    return tuple(pts)


# --------------------------------------------------------------------------- #
# §5.3 candidate extraction + §3.3 depth
# --------------------------------------------------------------------------- #
def _compute_depth(ring: tuple[Point, ...], direction: CardinalDirection, base_world: float,
                    axis: Literal["x", "y"], tolerances: VisibilityTolerances) -> float:
    nx, ny = direction
    front_support = max(nx * x + ny * y for x, y in ring)
    dot_p = ny * base_world if axis == "x" else nx * base_world
    raw_depth = front_support - dot_p
    if abs(raw_depth) <= tolerances.depth_epsilon_m:
        return 0.0
    if raw_depth < -tolerances.depth_epsilon_m:
        raise FacadeVisibilityInvariantError(
            "visibility_negative_depth", {"raw_depth": raw_depth, "base_world": base_world, "direction": direction}
        )
    return raw_depth


def _standard_p1_p2(family: FacadeFamily, axis: Literal["x", "y"], along_lo: float, along_hi: float,
                     base_world: float) -> tuple[Point, Point]:
    p1_along, p2_along = (along_lo, along_hi) if _P1_AT_ALONG_LO[family] else (along_hi, along_lo)
    if axis == "x":
        return (p1_along, base_world), (p2_along, base_world)
    return (base_world, p1_along), (base_world, p2_along)


def _direction_candidates(ring: tuple[Point, ...], direction: CardinalDirection,
                          tolerances: VisibilityTolerances) -> list[_Candidate]:
    family = _FAMILY_BY_DIRECTION[direction]
    axis = _AXIS_BY_FAMILY[family]
    n = len(ring)
    out: list[_Candidate] = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        normal = (dy / length, -dx / length)
        if normal != direction:
            continue
        along_a = a[0] if axis == "x" else a[1]
        along_b = b[0] if axis == "x" else b[1]
        along_lo, along_hi = (along_a, along_b) if along_a <= along_b else (along_b, along_a)
        base_world = a[1] if axis == "x" else a[0]
        p1, p2 = _standard_p1_p2(family, axis, along_lo, along_hi, base_world)
        depth = _compute_depth(ring, direction, base_world, axis, tolerances)
        frame = FacadeSegmentFrame(
            facade_family=family, p1=p1, p2=p2, base_world=_norm_zero(base_world),
            outward_normal=direction, world_along_interval=(_norm_zero(along_lo), _norm_zero(along_hi)),
            depth=depth,
        )
        key = (family, _norm_zero(along_lo), _norm_zero(along_hi), _norm_zero(base_world))
        out.append(_Candidate(frame=frame, canonical_edge_key=key))
    out.sort(key=lambda c: (c.frame.world_along_interval[0], c.frame.world_along_interval[1],
                            c.frame.depth, c.canonical_edge_key))
    return out


# --------------------------------------------------------------------------- #
# §6 per-direction 1D skyline
# --------------------------------------------------------------------------- #
def _assert_no_depth_tie(contenders: list[_Candidate], depth_epsilon_m: float, atom: tuple[float, float]) -> None:
    for i in range(len(contenders)):
        for j in range(i + 1, len(contenders)):
            if abs(contenders[i].frame.depth - contenders[j].frame.depth) <= depth_epsilon_m:
                raise FacadeVisibilityInvariantError(
                    "visibility_same_depth_overlap",
                    {
                        "atom": atom,
                        "depth_a": contenders[i].frame.depth, "depth_b": contenders[j].frame.depth,
                        "edge_a": contenders[i].canonical_edge_key, "edge_b": contenders[j].canonical_edge_key,
                    },
                )


def _merge_adjacent_atoms(raw_atoms: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    if not raw_atoms:
        return ()
    merged = [[raw_atoms[0][0], raw_atoms[0][1]]]
    for lo, hi in raw_atoms[1:]:
        if merged[-1][1] == lo:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi])
    return tuple((lo, hi) for lo, hi in merged)


def vg_for_direction(
    vertices: Sequence[Sequence[float]],
    direction: tuple[int, int],
    *,
    tolerances: VisibilityTolerances,
) -> tuple[DerivedVisibleSegment, ...]:
    """`Vg(polygon, direction)`: candidate boundary segments + skyline visibility.

    Pure function (spec §4.2): no I/O, no config loading, no mutation of
    `vertices`. Raises `FacadeVisibilityInvariantError` fail-closed for any
    unsupported/degenerate input (spec §5.2); never downgrades to a partial
    or approximate result.
    """
    direction = tuple(direction)
    if direction not in _FAMILY_BY_DIRECTION:
        raise FacadeVisibilityInvariantError("visibility_bad_direction", {"direction": direction})
    ring = _canonicalize_ring(vertices, tolerances)
    candidates = _direction_candidates(ring, direction, tolerances)
    if not candidates:
        # Topologically unreachable for a valid, bounded, positive-area simple
        # orthogonal ring (every axis extreme has a facing edge) — a defensive
        # invariant, not a documented §5.2 rejection code.
        raise FacadeVisibilityInvariantError("visibility_missing_direction_candidates", {"direction": direction})

    events = sorted({c.frame.world_along_interval[0] for c in candidates} |
                    {c.frame.world_along_interval[1] for c in candidates})
    for a, b in zip(events, events[1:]):
        gap = b - a
        if 0.0 < gap <= tolerances.endpoint_epsilon_m:
            raise FacadeVisibilityInvariantError("visibility_endpoint_collision", {"a": a, "b": b, "gap": gap})

    # Keyed by object identity, not value equality: two structurally-identical
    # candidates must never collapse into one visibility bucket.
    visible_by_candidate: dict[int, list[tuple[float, float]]] = {id(c): [] for c in candidates}
    for atom_lo, atom_hi in zip(events, events[1:]):
        contenders = [
            c for c in candidates
            if c.frame.world_along_interval[0] <= atom_lo and c.frame.world_along_interval[1] >= atom_hi
        ]
        if not contenders:
            continue
        _assert_no_depth_tie(contenders, tolerances.depth_epsilon_m, (atom_lo, atom_hi))
        winner = min(contenders, key=lambda c: c.frame.depth)
        visible_by_candidate[id(winner)].append((atom_lo, atom_hi))

    results = [
        DerivedVisibleSegment(
            frame=c.frame,
            visible_intervals=_merge_adjacent_atoms(visible_by_candidate[id(c)]),
            canonical_edge_key=c.canonical_edge_key,
        )
        for c in candidates
    ]
    results.sort(key=lambda r: (r.frame.world_along_interval[0], r.frame.world_along_interval[1],
                                r.frame.depth, r.canonical_edge_key))
    return tuple(results)


# --------------------------------------------------------------------------- #
# §7 strict FacadeSegment materialization
# --------------------------------------------------------------------------- #
def _segment_geometry_sha256(family: FacadeFamily, p1: Point, p2: Point) -> str:
    payload = {
        "schema": "facade_segment_geometry_v1",
        "facade_family": family,
        "p1": [_norm_zero(p1[0]), _norm_zero(p1[1])],
        "p2": [_norm_zero(p2[0]), _norm_zero(p2[1])],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def materialize_floor_facade_segments(
    geom: CorrectedGeometryV3,
    floor,
    *,
    tolerances: VisibilityTolerances,
) -> tuple[FacadeSegment, ...]:
    """All four directions' strict `FacadeSegment`s for one floor, sorted per
    spec §7.3 (family_rank, along_lo, along_hi, depth, canonical_edge_key)."""
    ring = floor_footprint(geom, floor)
    source_fp = floor_footprint_fingerprint(geom, floor)
    entries = []
    for family in _FAMILY_ORDER:
        direction = _DIRECTION_BY_FAMILY[family]
        for derived in vg_for_direction(ring, direction, tolerances=tolerances):
            frame = derived.frame
            geometry_sha = _segment_geometry_sha256(frame.facade_family, frame.p1, frame.p2)
            segment = FacadeSegment(
                id=f"{floor.id}:facade:{geometry_sha}",
                floor_id=floor.id,
                facade_family=frame.facade_family,
                p1=frame.p1,
                p2=frame.p2,
                outward_normal=frame.outward_normal,
                world_along_interval=WorldInterval(lo=frame.world_along_interval[0], hi=frame.world_along_interval[1]),
                depth=frame.depth,
                visible_intervals=[WorldInterval(lo=lo, hi=hi) for lo, hi in derived.visible_intervals],
                source_footprint_fingerprint=source_fp,
            )
            entries.append((
                _FAMILY_RANK[family], frame.world_along_interval[0], frame.world_along_interval[1],
                frame.depth, derived.canonical_edge_key, segment,
            ))
    entries.sort(key=lambda e: e[:5])
    return tuple(e[5] for e in entries)


def materialize_all_facade_segments(
    geom: CorrectedGeometryV3,
    *,
    tolerances: VisibilityTolerances,
) -> tuple[FacadeSegment, ...]:
    """Whole-building strict `FacadeSegment`s, ordered per spec §7.3: floor_id
    (lexical) outermost, then each floor's own (family_rank, along_lo,
    along_hi, depth, canonical_edge_key) order — independent of `geom.floors`
    input order, ring start vertex, or hash-map iteration."""
    segments: list[FacadeSegment] = []
    for floor in sorted(geom.floors, key=lambda fl: fl.id):
        segments.extend(materialize_floor_facade_segments(geom, floor, tolerances=tolerances))
    return tuple(segments)


def validate_materialized_facade_segments(
    geom: CorrectedGeometryV3,
    *,
    tolerances: VisibilityTolerances,
) -> None:
    """Independently recompute every floor's facade segments straight from the
    authoritative `floor_footprint` ring and assert an item-for-item match
    against `geom.facade_segments` (spec §5.2 `visibility_wire_mismatch`)."""
    recomputed = materialize_all_facade_segments(geom, tolerances=tolerances)
    if list(geom.facade_segments) != list(recomputed):
        raise FacadeVisibilityInvariantError(
            "visibility_wire_mismatch",
            {
                "floor_ids": [fl.id for fl in geom.floors],
                "stored_count": len(geom.facade_segments),
                "recomputed_count": len(recomputed),
            },
        )
