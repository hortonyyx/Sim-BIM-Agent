"""A0 §7 deterministic geometry validation on CorrectedGeometry (M2a / S1).

These are the *data-level* geometry invariants 1_correction must satisfy before
the kernel runs — checked on the corrected primitives (cells/windows/z), not on
the built surface graph (that is kernel.py's job). They are deterministic facts;
the check adapter (validator/checks/correction.py) wraps them into a CheckReport.

Invariants (block — A0 §7):
  - **coverage**: per floor, cells tile the footprint with no holes and no
    overlaps (within tolerance). A hole is unmodelled floor area; an overlap is a
    correction defect (e.g. a corridor laid over the rooms it sits between).
  - **closure / non-degenerate**: every cell rectangle has positive extent on
    both axes; the footprint is non-degenerate.
  - **z-stack continuity**: adjacent floors stack with no gap/overlap beyond
    tolerance (a gap would model a mid-building interface as Roof + exposed Floor).

Cross-checks (flag):
  - **zone-count tripwire**: per-floor cell count vs testdata ``thermal_zones``
    (catches the 2f corridor-over-split class of coarse error).
  - **window-on-wall**: every window's along-facade span lies within its room's
    extent on that facade (catches axis-flip placement / floating windows).

Returns plain ``GeometryFinding`` lists; severity→layer mapping is the adapter's.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from shapely.ops import unary_union

from src.agent.correction.cell_geometry import (
    cell_bbox,
    cell_facade_span,
    cell_polygon,
    cell_has_polygon,
    validate_cell_polygon,
)
from src.agent.correction.schema import CorrectedGeometry
from src.agent.correction.config import load_core_tolerances
from src.agent.correction.footprint import floor_footprint
from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.geometry.capability import FEATURE_CELL_POLYGON, schema_supports

_Z_TOL = 0.02         # m — z-stack contiguity tolerance (matches kernel _Z_TOL)
_MIN_EXTENT = 0.05    # m — degenerate cell threshold
_SPAN_TOL = 0.10      # m — window-span-within-room tolerance


@dataclass
class GeometryFinding:
    check_id: str
    ok: bool
    message: str = ""
    evidence: dict = field(default_factory=dict)


def _cell_box(c) -> object:
    return cell_polygon(c)


def check_cell_polygon_contract(geom: CorrectedGeometry) -> GeometryFinding:
    bad = []
    for fl in geom.floors:
        for c in fl.cells:
            try:
                if cell_has_polygon(c) and not schema_supports(geom, FEATURE_CELL_POLYGON):
                    raise ValueError(
                        f"cell {c.id}: polygon requires schema_version '2' or a schema version with feature '{FEATURE_CELL_POLYGON}'"
                    )
                validate_cell_polygon(c, min_edge_length_m=_MIN_EXTENT)
            except ValueError as exc:
                bad.append({"floor": fl.name, "cell": c.id, "reason": str(exc)})
    if bad:
        return GeometryFinding(
            "correction.cell_polygon_contract",
            False,
            f"{len(bad)} invalid polygon cell contract violation(s)",
            {"offenders": bad},
        )
    return GeometryFinding("correction.cell_polygon_contract", True)


def check_coverage(geom: CorrectedGeometry) -> list[GeometryFinding]:
    """B3 v2: cells tile each floor's authoritative footprint, no holes/overlaps.

    ``floor_footprint`` is deliberately the only footprint entry point: legacy
    geometry gets its equivalent bbox ring and v3 gets its per-floor polygon.
    Shapely receives those rings directly, so non-rectangular orthogonal areas
    are measured as polygons rather than approximated by their bounding boxes.

    ``area_delta`` (below) is a recorded conservation *bookkeeping* figure, not
    an independent gate: since ``union_area = covered_in_footprint +
    outside_area`` and ``footprint_area = covered_in_footprint + hole_area``,
    ``area_delta = |union_area - footprint_area| = |outside_area - hole_area|
    <= max(outside_area, hole_area)``. So area_delta can never exceed ``tol``
    unless hole or outside already does — it is always accompanied by one of
    them in ``problems``, never the sole offender. (Overlap area nets out of
    ``union_area`` before this identity, which is why it needs its own guard.)
    The actual conservation enforcers are the three separate guards below:
    hole / outside / overlap.
    """
    findings: list[GeometryFinding] = []
    from shapely.geometry import Polygon
    tol = load_core_tolerances().coverage_area_tol_m2
    for fl in geom.floors:
        try:
            foot = Polygon(floor_footprint(geom, fl))
            if foot.is_empty or not foot.is_valid or foot.area <= 0:
                raise ValueError("floor footprint is invalid or has zero area")
            footprint_area = foot.area
        except ValueError as exc:
            findings.append(GeometryFinding("correction.coverage", False, str(exc), {"floor": fl.name}))
            continue
        try:
            boxes = [_cell_box(c) for c in fl.cells]
        except ValueError as exc:
            findings.append(
                GeometryFinding(
                    "correction.coverage",
                    False,
                    f"floor '{fl.name}' has invalid cell geometry: {exc}",
                    {"floor": fl.name, "reason": str(exc)},
                )
            )
            continue
        if not boxes:
            findings.append(GeometryFinding(
                "correction.coverage", False,
                f"floor '{fl.name}' has no cells",
                {"floor": fl.name}))
            continue
        union = unary_union(boxes)
        union_area = union.area
        sum_areas = sum(b.area for b in boxes)
        overlap_area = sum_areas - union_area         # >0 ⇒ cells overlap
        covered_in_footprint = union.intersection(foot).area
        hole_area = footprint_area - covered_in_footprint
        outside_area = union.difference(foot).area
        area_delta = abs(union_area - footprint_area)
        problems = {}
        # area_delta is recorded for the conservation account (see docstring);
        # it is bounded by max(hole, outside) and so never independently trips
        # this gate — whichever of hole/outside caused it also appears below.
        if area_delta > tol:
            problems["area_delta_m2"] = round(area_delta, 6)
        # Keep the prior gate's separate non-overlap and exact-domain guards.
        # Area conservation alone would not catch equal-area hole/outside swaps
        # or duplicate cells whose union still has the correct total area.
        if overlap_area > tol:
            problems["overlap_m2"] = round(overlap_area, 6)
        if hole_area > tol:
            problems["hole_m2"] = round(hole_area, 6)
        if outside_area > tol:
            problems["outside_footprint_m2"] = round(outside_area, 6)
        evidence = {
            "floor": fl.name,
            "coverage_gate_version": "v2",
            "footprint_source": "floor_footprint",
            "footprint_area_m2": round(footprint_area, 6),
            "cells_union_area_m2": round(union_area, 6),
            "area_delta_m2": round(area_delta, 6),
            "coverage_area_tol_m2": tol,
            "overlap_m2": round(overlap_area, 6),
            "hole_m2": round(hole_area, 6),
            "outside_footprint_m2": round(outside_area, 6),
        }
        if problems:
            findings.append(GeometryFinding(
                "correction.coverage", False,
                f"floor '{fl.name}' violates coverage area conservation",
                {**evidence, **problems}))
        else:
            findings.append(GeometryFinding(
                "correction.coverage", True, "",
                evidence))
    return findings


def check_nondegenerate(geom: CorrectedGeometry) -> GeometryFinding:
    bad = []
    for fl in geom.floors:
        for c in fl.cells:
            try:
                minx, miny, maxx, maxy = cell_bbox(c)
            except ValueError:
                bad.append({"floor": fl.name, "cell": c.id})
                continue
            if abs(maxx - minx) < _MIN_EXTENT or abs(maxy - miny) < _MIN_EXTENT:
                bad.append({"floor": fl.name, "cell": c.id})
    if bad:
        return GeometryFinding(
            "correction.nondegenerate", False,
            f"{len(bad)} degenerate cell(s) (near-zero extent)", {"offenders": bad})
    return GeometryFinding("correction.nondegenerate", True)


def check_zstack(geom: CorrectedGeometry) -> GeometryFinding:
    floors = sorted(geom.floors, key=lambda f: float(f.z_floor))
    breaks = []
    for prev, cur in zip(floors, floors[1:]):
        top = float(prev.z_floor) + float(prev.ceiling_height)
        gap = float(cur.z_floor) - top
        if abs(gap) > _Z_TOL:
            breaks.append({"lower": prev.name, "upper": cur.name,
                           "gap_m": round(gap, 3)})
    if breaks:
        return GeometryFinding(
            "correction.zstack_continuity", False,
            f"{len(breaks)} z-stack discontinuity(ies)", {"breaks": breaks})
    return GeometryFinding("correction.zstack_continuity", True)


def check_zone_count(
    geom: CorrectedGeometry, expected_total: int | None
) -> GeometryFinding:
    """Tripwire: total cell count vs testdata thermal_zones (flag)."""
    total = sum(len(fl.cells) for fl in geom.floors)
    if expected_total is None:
        return GeometryFinding(
            "correction.zone_count_tripwire", True, "no expected count provided",
            {"actual": total, "expected": None})
    ok = total == expected_total
    return GeometryFinding(
        "correction.zone_count_tripwire", ok,
        "" if ok else f"cell count {total} != testdata thermal_zones {expected_total}",
        {"actual": total, "expected": expected_total,
         "per_floor": {fl.name: len(fl.cells) for fl in geom.floors}})


def check_windows_on_wall(geom: CorrectedGeometry) -> GeometryFinding:
    """Each window's along-facade span must fall within its room's extent on that
    facade (flag) — catches axis-flip placement and floating windows."""
    cells = {c.id: c for fl in geom.floors for c in fl.cells}
    bad = []
    for w in geom.windows:
        c = cells.get(w.room)
        if c is None:
            bad.append({"window": w.id, "reason": f"room '{w.room}' not found"})
            continue
        # along-facade axis: N/S → x, E/W → y
        try:
            rng = cell_facade_span(c, w.facade)
        except ValueError as exc:
            bad.append({"window": w.id, "reason": str(exc)})
            continue
        s0, s1 = min(w.span), max(w.span)
        if s0 < rng[0] - _SPAN_TOL or s1 > rng[1] + _SPAN_TOL:
            bad.append({"window": w.id, "facade": w.facade, "span": [s0, s1],
                        "room_range": list(rng)})
    if bad:
        return GeometryFinding(
            "correction.window_on_wall", False,
            f"{len(bad)} window(s) fall outside their room's facade extent",
            {"offenders": bad})
    return GeometryFinding("correction.window_on_wall", True,
                           evidence={"windows": len(geom.windows)})


def check_window_host_resolution(
    geom: CorrectedGeometry,
    *,
    proof=None,
    evidence=None,
) -> GeometryFinding:
    """B5 hard gate; legacy retains the original bbox cross-check unchanged."""
    if str(geom.schema_version) != "3":
        return check_windows_on_wall(geom)
    check_id = "correction.window_host_resolution"
    conflicts = [
        row for row in geom.conflicts
        if isinstance(row, dict) and row.get("kind") == "window_host_conflict"
    ]
    if conflicts:
        return GeometryFinding(
            check_id,
            False,
            "v3 geometry contains blocking window-host conflicts",
            {"conflicts": conflicts},
        )
    if proof is None or evidence is None:
        return GeometryFinding(
            check_id,
            False,
            "v3 window host proof and evidence are both required",
            {"proof_present": proof is not None, "evidence_present": evidence is not None},
        )

    from src.agent.correction.feature_state import (
        FeatureStatesArtifactV1,
        derive_feature_state_claims,
    )
    from src.agent.correction.finalize import PreparedCandidateIdentity
    from src.agent.correction.parse import CorrectionTarget
    from src.agent.correction.schema import CorrectedGeometryV3
    from src.agent.correction.window_host import (
        WindowEvidenceLedgerV1,
        WindowHostClaimsV1,
        WindowHostsArtifactV1,
        derive_window_evidence_ledger,
        window_host_claim_issues,
    )
    from src.agent.geometry.build import (
        VerifiedWindowHostProof,
        _resolver_inputs_from_verified_proof,
    )

    assert isinstance(geom, CorrectedGeometryV3)
    verified_inputs = None
    artifact_evidence = None
    if isinstance(proof, VerifiedWindowHostProof):
        try:
            raw_artifact = WindowHostsArtifactV1.model_validate_json(proof.raw_window_hosts_bytes)
            verified_inputs = _resolver_inputs_from_verified_proof(proof)
        except ValueError as exc:
            return GeometryFinding(check_id, False, "window host proof wire is invalid", {"reason": str(exc)})
        claims = raw_artifact.claims
        artifact_evidence = raw_artifact.evidence
    elif isinstance(proof, WindowHostClaimsV1):
        claims = proof
    else:
        return GeometryFinding(check_id, False, "unsupported window host proof type")
    if not isinstance(evidence, WindowEvidenceLedgerV1):
        return GeometryFinding(check_id, False, "window evidence has the wrong wire type")
    if artifact_evidence is not None and evidence != artifact_evidence:
        return GeometryFinding(check_id, False, "window evidence differs from verified proof artifact")

    issues = window_host_claim_issues(
        geom,
        claims=claims,
        tolerances=load_core_tolerances(),
    )
    claim_ids = tuple(sorted(row.window_id for row in claims.resolutions))
    evidence_ids = tuple(row.window_id for row in evidence.decisions)
    if claim_ids != evidence_ids:
        issues.append({
            "reason": "evidence_totality",
            "claim_ids": list(claim_ids),
            "evidence_ids": list(evidence_ids),
        })
    if (
        claims.resolver_inputs_sha256 != evidence.resolver_inputs_sha256
        or claims.view_manifest_sha256 != evidence.view_manifest_sha256
        or claims.facade_segments_sha256 != evidence.facade_segments_sha256
    ):
        issues.append({"reason": "evidence_host_identity"})

    from src.agent.correction.artifact_serialization import (
        serialize_correction_output,
        serialize_feature_states,
    )

    output_bytes = serialize_correction_output(geom)
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    phase = "e4_orientation" if geom.north_axis is not None else "b2"
    target = CorrectionTarget("3", CorrectedGeometryV3, "orthogonal_polygon", phase)
    feature_claims = derive_feature_state_claims(target, geom)
    feature_bytes = serialize_feature_states(FeatureStatesArtifactV1(
        output_sha256=output_sha256,
        claims=feature_claims,
    ))
    feature_sha256 = hashlib.sha256(feature_bytes).hexdigest()
    if evidence.output_sha256 != output_sha256:
        issues.append({"reason": "evidence_output_identity"})
    if evidence.feature_states_sha256 != feature_sha256:
        issues.append({"reason": "evidence_feature_identity"})
    if verified_inputs is not None and not issues:
        identity = PreparedCandidateIdentity(
            output_bytes=output_bytes,
            output_sha256=output_sha256,
            feature_states_bytes=feature_bytes,
            feature_states_sha256=feature_sha256,
        )
        try:
            recomputed_evidence = derive_window_evidence_ledger(
                geom,
                host_claims=claims,
                verified_inputs=verified_inputs,
                candidate_identity=identity,
                tolerances=load_core_tolerances(),
            )
        except (ValueError, RuntimeError) as exc:
            issues.append({"reason": "evidence_recompute", "detail": str(exc)})
        else:
            if recomputed_evidence != evidence:
                issues.append({"reason": "evidence_relation"})
    if issues:
        return GeometryFinding(
            check_id,
            False,
            f"{len(issues)} window host proof defect(s)",
            {"offenders": issues},
        )
    return GeometryFinding(
        check_id,
        True,
        evidence={"windows": len(geom.windows), "aggregate_sha256": claims.aggregate_sha256},
    )


def validate_corrected_geometry(
    geom: CorrectedGeometry, *, expected_zone_total: int | None = None
) -> list[GeometryFinding]:
    """Run all A0 §7 + cross-check geometry validations."""
    geom = ensure_corrected_geometry(geom)
    findings: list[GeometryFinding] = []
    findings.append(check_cell_polygon_contract(geom))
    findings.extend(check_coverage(geom))
    findings.append(check_nondegenerate(geom))
    findings.append(check_zstack(geom))
    findings.append(check_zone_count(geom, expected_zone_total))
    if str(geom.schema_version) != "3":
        findings.append(check_windows_on_wall(geom))
    return findings
