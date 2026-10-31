"""S23 — 2_modelling + 3_split_pairing geometry gate (M2b, gate ①).

The deterministic geometry kernel's target is the *code* (unit tests + invariants),
so per-run there is NO LLM judge here — gate ① is the whole story. Checks
(contracts §1 2/3 ①), all on the built ``BuildingGeometry``:

  - **zone closure** (block): every zone has a floor, a top (ceiling/roof), and a
    perimeter wall ring whose footprint matches the zone polygon.
  - **normals** (block): floors point −z, ceilings/roofs +z, walls horizontal &
    outward (away from the zone centroid).
  - **pairing gate as block** (block): the InterZone pair gate
    (validate_interzone_surface_pairs) promoted from advisory to a blocking gate
    (reciprocity / area / opposed-normal / coplanar / min-edge / illegal OBC).
  - **coverage completeness** (block, RECTANGULAR profile only — B5 generalises):
    derive the *expected* internal interfaces from cell adjacency (shared interior
    walls + stacked floor/ceiling overlaps) and reconcile their total area against
    the realised reciprocal (OBC=Surface) area. Catches the "both sides Outdoors"
    hole the per-pair gate and EP are both blind to.
  - **spec self-consistency** (block): every surface references an existing zone;
    every ``obc_obj`` target exists.

For the non-rectangular profile the coverage check emits ``not_applicable`` (the
policy then does not block on it) — fact vs policy, no special-casing in policy.
"""

from __future__ import annotations

import numpy as np

from src.agent.geometry.adjacency import expected_internal_interface_area
from src.agent.geometry.modelling import BuildingGeometry, Surface
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile
from src.validator.data_model import canonicalize_ring_vertices

_AREA_TOL = 0.10       # m^2 — interface-area reconcile tolerance
_PERIM_TOL = 0.10      # m — perimeter-match tolerance
_MIN_SHARE = 0.05      # m — minimum shared edge to count as an interface


def _newell(verts) -> np.ndarray:
    pts = np.asarray(verts, float)
    n = np.zeros(3)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    return n


def _poly_area_3d(verts) -> float:
    return float(np.linalg.norm(_newell(verts))) / 2.0


def _unit(n: np.ndarray) -> np.ndarray:
    m = np.linalg.norm(n)
    return n / m if m > 1e-9 else n


def check_kernel(
    bg: BuildingGeometry,
    *,
    window_host_proof=None,
    capability_profile: str = "rectangular",
    interzone_issues: list[str] | None = None,
    run_profile: RunProfile = "exploratory",
) -> CheckReport:
    rep = CheckReport(
        stage="2_modelling",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    _zone_closure(rep, bg)
    _normals(rep, bg)
    _pairing_gate(rep, bg, interzone_issues)
    _spec_self_consistency(rep, bg)
    _window_parent_binding(rep, bg, window_host_proof)
    _coverage_completeness(rep, bg, capability_profile)
    return rep


def _by_zone(bg: BuildingGeometry) -> dict[str, list[Surface]]:
    out: dict[str, list[Surface]] = {}
    for s in bg.surfaces:
        out.setdefault(s.zone, []).append(s)
    return out


def _wall_base_len(s: Surface) -> float:
    """Horizontal length of a wall face (split-pairing may fragment walls, so we
    reconcile by SUMMED width, not per-face)."""
    xs = [v[0] for v in s.verts]
    ys = [v[1] for v in s.verts]
    return float(np.hypot(max(xs) - min(xs), max(ys) - min(ys)))


def _zone_closure(rep: CheckReport, bg: BuildingGeometry) -> None:
    """Closure by SUMS against each zone's authoritative polygon: split-pairing
    fragments floors/ceilings/walls into sub-faces, so a single face never matches
    the footprint — but the summed floor/ceiling area and wall width must."""
    polys = {zv.zone: zv for zv in bg.zone_volumes}
    by_zone = _by_zone(bg)
    # Iterate the union of declared zones (bg.zones / zone_volumes) and zones that
    # actually own surfaces — so a DECLARED zone with no surfaces at all still gets
    # checked (and fails on the missing Floor/Ceiling/Wall) rather than skipped.
    all_zones = set(dict.fromkeys(bg.zones)) | set(polys) | set(by_zone)
    bad = []
    for zone in sorted(all_zones):
        surfs = by_zone.get(zone, [])
        floors = [s for s in surfs if s.stype == "Floor"]
        tops = [s for s in surfs if s.stype in ("Ceiling", "Roof")]
        walls = [s for s in surfs if s.stype == "Wall"]
        if not floors:
            bad.append({"zone": zone, "missing": "Floor"})
        if not tops:
            bad.append({"zone": zone, "missing": "Ceiling/Roof"})
        if not walls:
            bad.append({"zone": zone, "missing": "Wall"})
        zv = polys.get(zone)
        if zv is None:
            # A surface references a zone with no ZoneVolume → the zone is not
            # declared; block rather than silently skip its closure checks.
            bad.append({"zone": zone, "reason": "no ZoneVolume (zone not declared)"})
            continue
        if not (floors and tops and walls):
            continue
        area = zv.polygon.area
        perim = zv.polygon.length
        floor_area = sum(_poly_area_3d(s.verts) for s in floors)
        top_area = sum(_poly_area_3d(s.verts) for s in tops)
        wall_width = sum(_wall_base_len(s) for s in walls)
        if abs(floor_area - area) > _AREA_TOL:
            bad.append({"zone": zone, "floor_area": round(floor_area, 3),
                        "footprint_area": round(area, 3)})
        if abs(top_area - area) > _AREA_TOL:
            bad.append({"zone": zone, "top_area": round(top_area, 3),
                        "footprint_area": round(area, 3)})
        if abs(wall_width - perim) > _PERIM_TOL:
            bad.append({"zone": zone, "wall_width_sum": round(wall_width, 3),
                        "footprint_perimeter": round(perim, 3)})
    if bad:
        rep.add_fail("kernel.zone_closure", CheckLayer.INVARIANT,
                     f"{len(bad)} zone-closure defect(s)", evidence={"offenders": bad})
    else:
        rep.add_pass("kernel.zone_closure", CheckLayer.INVARIANT,
                     evidence={"zones": len(all_zones)})


def _normals(rep: CheckReport, bg: BuildingGeometry) -> None:
    zone_poly = {zv.zone: zv.polygon for zv in bg.zone_volumes}
    bad = []
    for s in bg.surfaces:
        n = _unit(_newell(s.verts))
        if s.stype == "Floor":
            if n[2] > -0.5:
                bad.append({"surface": s.name, "type": s.stype, "nz": round(float(n[2]), 3)})
        elif s.stype in ("Ceiling", "Roof"):
            if n[2] < 0.5:
                bad.append({"surface": s.name, "type": s.stype, "nz": round(float(n[2]), 3)})
        elif s.stype == "Wall":
            if abs(n[2]) > 0.1:
                bad.append({"surface": s.name, "type": s.stype, "reason": "wall not vertical",
                            "nz": round(float(n[2]), 3)})
                continue
            poly = zone_poly.get(s.zone)
            if poly is None:
                bad.append({"surface": s.name, "type": s.stype, "reason": "zone polygon missing"})
                continue
            pts = []
            for v in s.verts:
                xy = (float(v[0]), float(v[1]))
                if xy not in pts:
                    pts.append(xy)
            if len(pts) < 2:
                bad.append({"surface": s.name, "type": s.stype, "reason": "degenerate wall"})
                continue
            mid = np.asarray([(pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0])
            nxy = np.asarray([float(n[0]), float(n[1])])
            eps = 1e-4
            from shapely.geometry import Point

            inward_probe = Point(*(mid - nxy * eps))
            outward_probe = Point(*(mid + nxy * eps))
            if not poly.covers(inward_probe) or poly.covers(outward_probe):
                bad.append({"surface": s.name, "type": s.stype, "reason": "inward normal"})
    if bad:
        rep.add_fail("kernel.normals", CheckLayer.INVARIANT,
                     f"{len(bad)} surface(s) with wrong normal orientation",
                     evidence={"offenders": bad[:20], "count": len(bad)})
    else:
        rep.add_pass("kernel.normals", CheckLayer.INVARIANT,
                     evidence={"surfaces": len(bg.surfaces)})


def _pairing_gate(
    rep: CheckReport, bg: BuildingGeometry, interzone_issues: list[str] | None
) -> None:
    """Promote the InterZone pair gate from advisory to a blocking gate."""
    if interzone_issues is None:
        try:
            from src.agent.geometry.to_idf import building_to_idf
            from src.validator.interzone import validate_interzone_surface_pairs

            interzone_issues = validate_interzone_surface_pairs(building_to_idf(bg, boundary_validation_only=True))
        except Exception as e:  # noqa: BLE001 — surfaced as a fail-closed error
            rep.add("kernel.pairing_gate", CheckStatus.ERROR, CheckLayer.INVARIANT,
                    message=f"pairing gate could not run: {type(e).__name__}: {e}")
            return
    if interzone_issues:
        rep.add_fail("kernel.pairing_gate", CheckLayer.INVARIANT,
                     f"{len(interzone_issues)} InterZone pairing issue(s)",
                     evidence={"issues": interzone_issues[:20], "count": len(interzone_issues)})
    else:
        rep.add_pass("kernel.pairing_gate", CheckLayer.INVARIANT)


def _spec_self_consistency(rep: CheckReport, bg: BuildingGeometry) -> None:
    # Declared zones come ONLY from the zone declaration (bg.zones / zone_volumes),
    # NOT from the surfaces themselves — otherwise a surface pointing at an
    # undeclared zone would vacuously satisfy `s.zone in zones`.
    zones = set(dict.fromkeys(bg.zones)) | {zv.zone for zv in bg.zone_volumes}
    names = {s.name for s in bg.surfaces}
    bad = []
    for s in bg.surfaces:
        if s.zone not in zones:
            bad.append({"surface": s.name, "reason": f"zone '{s.zone}' undefined"})
        if s.obc == "Surface":
            if not s.obc_obj:
                bad.append({"surface": s.name, "reason": "OBC=Surface with empty obc_obj"})
            elif s.obc_obj not in names:
                bad.append({"surface": s.name, "reason": f"obc_obj '{s.obc_obj}' not found"})
    if bad:
        rep.add_fail("kernel.spec_self_consistency", CheckLayer.INVARIANT,
                     f"{len(bad)} spec reference defect(s)", evidence={"offenders": bad})
    else:
        rep.add_pass("kernel.spec_self_consistency", CheckLayer.INVARIANT)


def _window_parent_binding(rep: CheckReport, bg: BuildingGeometry, proof) -> None:
    check_id = "kernel.window_parent_binding"
    if bg.geometry_contract == "legacy":
        if proof is not None:
            rep.add_fail(
                check_id,
                CheckLayer.INVARIANT,
                "legacy built geometry must not carry B5 proof",
            )
        else:
            rep.add(
                check_id,
                CheckStatus.NOT_APPLICABLE,
                CheckLayer.INVARIANT,
                message="legacy geometry contract",
            )
        return
    if bg.geometry_contract != "c2_b5_v1":
        rep.add_fail(check_id, CheckLayer.INVARIANT, "unknown building geometry contract")
        return
    if proof is None:
        rep.add_fail(check_id, CheckLayer.INVARIANT, "B5 built geometry requires host proof")
        return

    from src.agent.correction.config import load_core_tolerances
    from src.agent.correction.window_host import WindowHostClaimsV1, WindowHostsArtifactV1
    from src.agent.geometry.build import VerifiedWindowHostProof
    from src.agent.geometry.modelling import (
        SegmentLine2D,
        _newell,  # intentionally shadows this module's OWN top-level `_newell`
        # (used elsewhere in this file, e.g. `_normals`) for the rest of this
        # function -- this is the exact `_newell` `_canonicalize_bg_vertices`
        # (build.py) feeds into `canonicalize_ring_vertices`, so using it here
        # keeps "same normal-derivation, same canonicalizer" unambiguous
        # rather than relying on an equivalence argument between two Newell
        # implementations that only agree because canonicalize_ring_vertices
        # re-normalizes internally.
        _v3_parent_candidates,
        window_verts_on_line,
    )

    if isinstance(proof, VerifiedWindowHostProof):
        try:
            claims = WindowHostsArtifactV1.model_validate_json(
                proof.raw_window_hosts_bytes,
            ).claims
        except ValueError as exc:
            rep.add_fail(
                check_id,
                CheckLayer.INVARIANT,
                "B5 proof artifact is invalid",
                evidence={"reason": str(exc)},
            )
            return
    elif isinstance(proof, WindowHostClaimsV1):
        claims = proof
    else:
        rep.add_fail(check_id, CheckLayer.INVARIANT, "unsupported B5 proof type")
        return

    issues: list[dict] = []
    resolution_ids = [row.window_id for row in claims.resolutions]
    built_ids = [window.source_window_id for window in bg.windows]
    if (
        any(value is None for value in built_ids)
        or len(built_ids) != len(set(built_ids))
        or set(built_ids) != set(resolution_ids)
    ):
        issues.append({
            "reason": "window_totality",
            "built_ids": sorted(str(value) for value in built_ids),
            "resolution_ids": sorted(resolution_ids),
        })
    built_by_id = {window.source_window_id: window for window in bg.windows}
    surfaces = {surface.name: surface for surface in bg.surfaces}
    zone_by_cell = {volume.cell_id: volume.zone for volume in bg.zone_volumes}
    tolerances = load_core_tolerances()
    for resolution in claims.resolutions:
        built = built_by_id.get(resolution.window_id)
        if built is None:
            continue
        prefix = {"window_id": resolution.window_id}
        if (
            built.facade_segment_id != resolution.facade_segment_id
            or built.host_resolution_sha256 != resolution.resolution_sha256
        ):
            issues.append({**prefix, "reason": "built_identity"})
        parent = surfaces.get(built.parent)
        if parent is None:
            issues.append({**prefix, "reason": "parent_missing"})
            continue
        zone = zone_by_cell.get(resolution.room_id)
        if zone is None:
            issues.append({**prefix, "reason": "room_zone_missing"})
            continue
        try:
            candidates = _v3_parent_candidates(
                surfaces=bg.surfaces,
                zone=zone,
                resolution=resolution,
                plane_epsilon=tolerances.window_host_plane_epsilon_m,
                span_epsilon=tolerances.window_host_span_epsilon_m,
            )
        except ValueError as exc:
            issues.append({**prefix, "reason": "parent_geometry", "detail": str(exc)})
            continue
        candidate_names = [surface.name for surface, _line in candidates]
        if candidate_names != [built.parent]:
            issues.append({
                **prefix,
                "reason": "parent_unique",
                "declared_parent": built.parent,
                "candidate_parents": sorted(candidate_names),
            })
        try:
            fresh_vertices = window_verts_on_line(
                host_line=SegmentLine2D(
                    (resolution.segment_p1.x, resolution.segment_p1.y),
                    (resolution.segment_p2.x, resolution.segment_p2.y),
                ),
                parameter_interval=(
                    resolution.segment_parameter_interval.lo,
                    resolution.segment_parameter_interval.hi,
                ),
                z_interval=(resolution.z_interval.lo, resolution.z_interval.hi),
                outward_normal_xy=tuple(
                    float(value) for value in resolution.segment_outward_normal
                ),
            )
        except ValueError as exc:
            issues.append({**prefix, "reason": "line_geometry", "detail": str(exc)})
        else:
            # F-19 (2026-08-10): `built.verts` is already IDF-canonical --
            # `build_geometry`'s `_canonicalize_bg_vertices` (build.py:80-85)
            # runs canonicalize_ring_vertices on every window before this
            # check ever sees it. `fresh_vertices` above is the raw
            # `window_verts_on_line` output and was NOT canonicalized, so a
            # correctly-built window whose ring simply started at a different
            # (but equally valid) vertex than this independent recompute
            # failed here on every real B5 case -- see
            # AI_agent/logs/experiments/2026-08-09_f19_window_parent_binding/README.md.
            # Route this side through the exact same shared primitive, using
            # its own self-consistent Newell normal -- the identical pattern
            # `_canonicalize_bg_vertices` already uses -- instead of deriving
            # a second "should agree" algorithm (that divergence is exactly
            # how F-13 happened; see
            # AI_agent/logs/reviews/verdict/2026-08-06_f13_orchestrator_lightgate.md).
            # This does NOT tolerate a genuinely reversed winding (flipped
            # normal -- inside/outside swapped, window on the wrong room):
            # canonicalize_ring_vertices preserves boundary adjacency and
            # only canonicalizes winding + start vertex relative to the
            # normal it is given; it cannot repair a winding that disagrees
            # with the true outward direction (see
            # `_canonicalize_bg_vertices`'s own docstring for why that
            # holds), so a built ring that is actually wound backwards still
            # compares unequal to this correctly-oriented fresh recompute.
            fresh_normal = _newell(fresh_vertices)
            if float(np.linalg.norm(fresh_normal)) >= 1e-9:
                fresh_vertices = [
                    (float(x), float(y), float(z))
                    for x, y, z in canonicalize_ring_vertices(
                        np.asarray(fresh_vertices, dtype=float), fresh_normal
                    )
                ]
            if built.verts != fresh_vertices:
                issues.append({**prefix, "reason": "built_vertices"})
    if issues:
        rep.add_fail(
            check_id,
            CheckLayer.INVARIANT,
            f"{len(issues)} built window parent-binding defect(s)",
            evidence={"offenders": issues},
        )
    else:
        rep.add_pass(
            check_id,
            CheckLayer.INVARIANT,
            evidence={"windows": len(resolution_ids)},
        )


def _coverage_completeness(
    rep: CheckReport, bg: BuildingGeometry, profile: str
) -> None:
    """Expected internal-interface area (from cell adjacency) vs realised
    reciprocal (OBC=Surface) area. Rectangular profile only."""
    if profile != "rectangular":
        rep.add("kernel.coverage_completeness", CheckStatus.NOT_APPLICABLE,
                CheckLayer.INVARIANT, message=f"profile={profile}: B5 generalises non-rect")
        return
    zvs = bg.zone_volumes
    if not zvs:
        rep.add("kernel.coverage_completeness", CheckStatus.NOT_APPLICABLE,
                CheckLayer.INVARIANT, message="no zone_volumes to derive adjacency")
        return

    expected = expected_internal_interface_area(
        zvs, min_share_m=_MIN_SHARE, z_tol_m=0.05
    )

    realised = sum(_poly_area_3d(s.verts) for s in bg.surfaces if s.obc == "Surface")
    realised_half = realised / 2.0  # each interface counted on both sides
    delta = expected - realised_half
    if delta > _AREA_TOL:
        rep.add_fail(
            "kernel.coverage_completeness", CheckLayer.INVARIANT,
            f"internal-interface coverage hole: expected {expected:.2f} m^2 of "
            f"internal interfaces, only {realised_half:.2f} m^2 realised as "
            f"reciprocal pairs (Δ={delta:.2f}) — some internal boundary is modelled "
            f"as Outdoors/Adiabatic",
            evidence={"expected_m2": round(expected, 3),
                      "realised_m2": round(realised_half, 3),
                      "delta_m2": round(delta, 3)})
    else:
        rep.add_pass("kernel.coverage_completeness", CheckLayer.INVARIANT,
                     evidence={"expected_m2": round(expected, 3),
                               "realised_m2": round(realised_half, 3)})
