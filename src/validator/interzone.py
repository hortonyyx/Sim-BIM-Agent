"""Deterministic InterZone surface-pair validation.

EnergyPlus accepts whatever surface graph it is handed and only catches some
errors late — or, for degenerate sub-tolerance slivers, segfaults in the input
stage before writing `.err` (observed sm21 Sonnet path, exit 139). This module
adds an early, explicit gate run on the *assembled* IDF, before EnergyPlus, so a
missing / non-reciprocal / degenerate InterZone pair fails fast with a precise
message instead of a late EP fatal or a silent wrong-physics "pass".

It operates on the eppy `IDF` object (not on the natural-language `*_specs`), so
it validates what the surface agent actually produced regardless of which prompt
wording it followed — closing the "correctness depends on prompt compliance" gap
flagged by the 2026-05-28 InterZone surface pairing review (finding #1).

Implemented (review #1 + degeneracy):
  - every `Outside Boundary Condition = Surface` target exists
  - the target is itself `Outside Boundary Condition = Surface`
  - the target points back to the source (reciprocity)
  - no surface is the target of more than one incoming pair
  - paired areas match within tolerance
  - paired normals are opposite within tolerance
  - paired horizontal (floor/ceiling) surfaces lie on the same z-plane
  - no surface has an edge below `min_edge` (degenerate sliver — the EP
    input-stage segfault class)

Deferred (review #2): coverage completeness. Failure class this gate canNOT
catch — a region that *should* be an interior boundary (an inter-floor slab, or
a wall between adjacent zones) but where *both* sides were modelled as Outdoors/
Adiabatic, so the region is absent from the pair graph entirely. Every declared
pair is then legal (this gate passes) and EP runs without error, yet the model
has invented an exterior boundary mid-building. Detecting it needs the zone
footprints unioned/intersected to find the area that *must* be tiled, then the
union of interior horizontal-pair polygons subtracted from it — a non-empty
difference is a hole. That requires polygon union/intersection/difference.

Decision (2026-05-29): the long-term implementation will use `shapely` (chosen
over a pure-numpy axis-aligned-rectangle scheme because the latter must be
rewritten for the L-shape / rotated footprints of plan B5). Not urgent — the
"each pair legal but collectively incomplete" risk has not yet bitten a real
case; land it once a case actually exposes the hole, or alongside the B5
non-rectangular work. Tracked in AI_agent/logs/downstream_agent_changes.md.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
from eppy.modeleditor import IDF

from src.utils.logging import get_logger

logger = get_logger(__name__)

_SURFACE_OBJ = "BUILDINGSURFACE:DETAILED"

# Tolerances. Coordinates are in metres; these buildings are modelled at the
# centimetre level so the thresholds sit comfortably above vertex round-off
# while still catching real modelling defects.
_AREA_ABS_TOL = 0.02  # m^2
_AREA_REL_TOL = 0.01  # 1 %
_PLANE_TOL = 0.02  # m — paired surfaces must lie on the same plane (any orientation)
_NORMAL_DOT_TOL = -0.99  # paired unit normals must be ~antiparallel
_MIN_EDGE = 0.10  # m — anything thinner is a degenerate sliver, not a wall

# Outside Boundary Conditions EnergyPlus accepts for our pipeline. Anything else
# (notably `Zone`, removed from modern EP — `invalid Outside Boundary
# Condition="ZONE"` severe) means an interzone surface was encoded wrong; it must
# be `Surface` + a reciprocal object. This closes the gate blind spot where
# OBC=Zone surfaces were silently ignored (only OBC=Surface was validated).
_VALID_OBC = {"Outdoors", "Ground", "Surface", "Adiabatic"}


def _unit_normal(coords: list[tuple[float, float, float]]) -> np.ndarray:
    """Newell's method — robust planar polygon normal, returned as a unit vector."""
    pts = np.asarray(coords, dtype=float)
    n = np.zeros(3)
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    norm = np.linalg.norm(n)
    if norm < 1e-12:
        return n  # degenerate; min-edge check reports it separately
    return n / norm


def _min_edge_length(coords: list[tuple[float, float, float]]) -> float:
    pts = np.asarray(coords, dtype=float)
    if len(pts) < 2:
        return 0.0
    rolled = np.roll(pts, -1, axis=0)
    return float(np.linalg.norm(pts - rolled, axis=1).min())


def _max_point_to_plane(
    src_coords: list[tuple[float, float, float]],
    tgt_coords: list[tuple[float, float, float]],
    src_normal: np.ndarray,
) -> float:
    """Max distance of target vertices from the plane through the source surface.

    Plane = (point `src_coords[0]`, normal `src_normal`). For a correctly paired
    InterZone surface the two faces are coincident, so every target vertex sits
    on the source plane (distance ~0). Returns 0.0 if the source normal is
    degenerate (the min-edge guard reports that surface separately).
    """
    if np.linalg.norm(src_normal) < 1e-9:
        return 0.0
    p0 = np.asarray(src_coords[0], dtype=float)
    tgt = np.asarray(tgt_coords, dtype=float)
    return float(np.abs((tgt - p0) @ src_normal).max())


def validate_interzone_surface_pairs(idf: IDF) -> list[str]:
    """Return a list of human-readable InterZone pairing issues (empty = clean).

    The string format mirrors `ConfigState.validate_references()` so callers can
    treat both the same way.
    """
    surfaces = idf.idfobjects[_SURFACE_OBJ]
    by_name = {s.Name: s for s in surfaces}
    issues: list[str] = []

    # Degenerate-sliver guard (applies to every surface, not just InterZone).
    for s in surfaces:
        min_edge = _min_edge_length(s.coords)
        if min_edge < _MIN_EDGE:
            issues.append(
                f"degenerate surface '{s.Name}': shortest edge {min_edge:.4f} m "
                f"< {_MIN_EDGE} m (sub-tolerance sliver — EP may segfault in input "
                f"processing)"
            )

    # Invalid OBC guard — catches OBC=Zone (and any other non-accepted value)
    # that the Surface-only pairing checks below would otherwise ignore.
    for s in surfaces:
        obc = s.Outside_Boundary_Condition
        if obc not in _VALID_OBC:
            issues.append(
                f"surface '{s.Name}' has invalid Outside Boundary Condition "
                f"'{obc}' — EnergyPlus rejects it; an interzone surface must be "
                f"'Surface' with a reciprocal boundary object, not '{obc}'"
            )

    interzone = [s for s in surfaces if s.Outside_Boundary_Condition == "Surface"]

    # 1-1 reciprocity: each target may be claimed by exactly one source.
    target_counts = Counter(s.Outside_Boundary_Condition_Object for s in interzone)

    seen_pairs: set[frozenset[str]] = set()
    for src in interzone:
        tgt_name = src.Outside_Boundary_Condition_Object
        if not tgt_name:
            issues.append(
                f"InterZone surface '{src.Name}' has OBC=Surface but no boundary "
                f"object (target name is empty)"
            )
            continue
        if tgt_name not in by_name:
            issues.append(
                f"InterZone surface '{src.Name}' targets missing surface "
                f"'{tgt_name}'"
            )
            continue
        tgt = by_name[tgt_name]
        if tgt.Outside_Boundary_Condition != "Surface":
            issues.append(
                f"InterZone surface '{src.Name}' targets '{tgt_name}', whose OBC is "
                f"'{tgt.Outside_Boundary_Condition}' (must be 'Surface')"
            )
            continue
        if tgt.Outside_Boundary_Condition_Object != src.Name:
            issues.append(
                f"InterZone surface '{src.Name}' -> '{tgt_name}' is not reciprocal: "
                f"'{tgt_name}' points back to "
                f"'{tgt.Outside_Boundary_Condition_Object or '(empty)'}'"
            )
            continue
        if target_counts[tgt_name] > 1:
            issues.append(
                f"InterZone surface '{tgt_name}' is targeted by "
                f"{target_counts[tgt_name]} sources (must be exactly one)"
            )

        # Geometry agreement — check each unordered pair once.
        key = frozenset((src.Name, tgt_name))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        a_src, a_tgt = src.area, tgt.area
        if abs(a_src - a_tgt) > max(_AREA_ABS_TOL, _AREA_REL_TOL * max(a_src, a_tgt)):
            issues.append(
                f"InterZone pair '{src.Name}' <-> '{tgt_name}' area mismatch: "
                f"{a_src:.4f} vs {a_tgt:.4f} m^2"
            )

        n_src = _unit_normal(src.coords)
        dot = float(np.dot(n_src, _unit_normal(tgt.coords)))
        if dot > _NORMAL_DOT_TOL:
            issues.append(
                f"InterZone pair '{src.Name}' <-> '{tgt_name}' normals not opposite "
                f"(unit-normal dot {dot:.3f}; expected <= {_NORMAL_DOT_TOL})"
            )

        # Plane coincidence (any orientation — walls AND floor/ceiling). Two
        # reciprocal, equal-area, opposite-normal surfaces can still sit on
        # different parallel planes (e.g. a wall pair offset 0.05 m along its
        # normal); they are then not the same physical boundary face. Measure
        # the target vertices' point-to-plane distance from the source plane.
        plane_dist = _max_point_to_plane(src.coords, tgt.coords, n_src)
        if plane_dist > _PLANE_TOL:
            issues.append(
                f"InterZone pair '{src.Name}' <-> '{tgt_name}' not coplanar: "
                f"max point-to-plane distance {plane_dist:.4f} m (> {_PLANE_TOL} m)"
            )

    return issues


def audit_interzone_surface_pairs(
    idf: IDF, *, issues: list[str] | None = None
) -> dict[str, int]:
    """Non-failing summary counts for baseline run notes (review #4).

    Returns total surfaces, counts by outside boundary condition, reciprocal
    InterZone pair count, and the number of pairing issues. Pass `issues` to
    reuse an already-computed result and avoid validating twice.
    """
    surfaces = idf.idfobjects[_SURFACE_OBJ]
    obc_counts = Counter(s.Outside_Boundary_Condition for s in surfaces)
    if issues is None:
        issues = validate_interzone_surface_pairs(idf)

    # Count surfaces that are *actually* mutual references, not Surface//2 (which
    # is wrong when the graph is broken/odd/non-reciprocal).
    by_name = {s.Name: s for s in surfaces}
    mutual: set[frozenset[str]] = set()
    for s in surfaces:
        if s.Outside_Boundary_Condition != "Surface":
            continue
        tgt = by_name.get(s.Outside_Boundary_Condition_Object)
        if (
            tgt is not None
            and tgt.Outside_Boundary_Condition == "Surface"
            and tgt.Outside_Boundary_Condition_Object == s.Name
        ):
            mutual.add(frozenset((s.Name, tgt.Name)))

    return {
        "buildingsurface_total": len(surfaces),
        "obc_outdoors": obc_counts.get("Outdoors", 0),
        "obc_surface": obc_counts.get("Surface", 0),
        "obc_ground": obc_counts.get("Ground", 0),
        "obc_adiabatic": obc_counts.get("Adiabatic", 0),
        "reciprocal_interzone_pairs": len(mutual),
        "pair_issues": len(issues),
    }
