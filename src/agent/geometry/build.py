"""Deterministic geometry build — orchestrator.

Input  : CorrectedGeometry (room cells per floor, polygon or x/y rect, + windows)
Output : BuildingGeometry (zones + fully-resolved surfaces + windows)

Two stages, split into modules:
  - `modelling`     建模·几何: cells -> zone volumes + face vertex realization
  - `split_pairing` 切配·仿真: which faces exist + 1:1 reciprocal correspondence

This module wires them together and re-exports the output dataclasses so existing
imports (`from src.agent.geometry.build import BuildingGeometry/Surface/Window`)
keep working. Geometry only — no physics.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from src.agent.correction.schema import CorrectedGeometry
from src.agent.geometry.modelling import (
    BuildingGeometry,
    NameRegistry,
    Surface,
    Window,
    _newell,
    attach_windows,
    attach_windows_v3,
    build_zone_volumes,
)
from src.agent.geometry.split_pairing import pair_surfaces
from src.agent.geometry.ring_order import canonicalize_ring_vertices

__all__ = [
    "BuildingGeometry", "Surface", "VerifiedWindowHostProof", "Window",
    "build_geometry",
]


_PROOF_CONSTRUCTION_TOKEN = object()


def _canonicalize_bg_vertices(out: BuildingGeometry) -> None:
    """F-13 (2026-08-06): make the kernel's own vertex output already IDF
    canonical, in place, so gate①'s `GeometrySchema.validate_points_sorting`
    (`src/validator/data_model.py`) becomes an identity transform on it.

    Reuses `canonicalize_ring_vertices` — the SAME function the validator
    itself delegates to — rather than re-deriving a second "should agree"
    algorithm (that divergence is exactly how F-13 happened; see
    AI_agent/logs/reviews/verdict/2026-08-06_f13_orchestrator_lightgate.md).

    The validator derives each surface's outward normal independently (via
    Delaunay triangulation of a Floor's interior points — see
    `GeometrySchema._get_normal_vector`). This kernel does not need to
    replicate that: by the time `pair_surfaces`/`attach_windows*` finish,
    every surface's own vertex order is ALREADY outward-oriented (walls via
    `_local_outward_normal`/owner-polygon containment test, floors/ceilings/
    roofs via the fixed ±Z convention, windows via their parent wall's own
    orientation or an explicit, validated `outward_normal_xy`) — this is the
    kernel's pre-existing, independently-grounded orientation logic
    (`_orient` in `modelling.py`), unchanged by F-13. So the outward normal
    to feed the shared canonicalizer is simply each surface's OWN Newell
    normal (`_newell`): self-consistent, requires no interior-point lookup,
    and only verifies/preserves the ring's boundary adjacency while
    canonicalizing winding + start vertex — it cannot flip a winding that is
    already correct.

    Degenerate (near-zero-area) faces are left untouched — same posture the
    kernel already takes elsewhere (a degenerate face has no well-defined
    normal to canonicalize against; nothing here guesses one).
    """
    for surface in out.surfaces:
        normal = _newell(surface.verts)
        if float(np.linalg.norm(normal)) < 1e-9:
            continue
        canonical = canonicalize_ring_vertices(np.asarray(surface.verts, dtype=float), normal)
        surface.verts = [(float(x), float(y), float(z)) for x, y, z in canonical]
    for window in out.windows:
        normal = _newell(window.verts)
        if float(np.linalg.norm(normal)) < 1e-9:
            continue
        canonical = canonicalize_ring_vertices(np.asarray(window.verts, dtype=float), normal)
        window.verts = [(float(x), float(y), float(z)) for x, y, z in canonical]


@dataclass(frozen=True, init=False)
class VerifiedWindowHostProof:
    raw_resolver_inputs_bytes: bytes
    raw_window_hosts_bytes: bytes
    raw_output_bytes: bytes

    def __init__(
        self,
        *,
        raw_resolver_inputs_bytes: bytes,
        raw_window_hosts_bytes: bytes,
        raw_output_bytes: bytes,
        _token: object,
    ) -> None:
        if _token is not _PROOF_CONSTRUCTION_TOKEN:
            raise TypeError("VerifiedWindowHostProof is issued only by the artifact verifier")
        object.__setattr__(self, "raw_resolver_inputs_bytes", bytes(raw_resolver_inputs_bytes))
        object.__setattr__(self, "raw_window_hosts_bytes", bytes(raw_window_hosts_bytes))
        object.__setattr__(self, "raw_output_bytes", bytes(raw_output_bytes))


@dataclass(frozen=True)
class _ArtifactAuthenticatedResolverInputs:
    """Narrow final-output recheck view issued from a verified proof bundle.

    This is intentionally not ``VerifiedWindowResolverInputs``: the latter
    certifies producer/view/reading raw bytes and must never be forged with
    empty placeholders.  Final-output recomputation needs only the already
    hash-bound persisted resolver wire, so the proof verifier exposes exactly
    that smaller authority.
    """

    inputs: object
    raw_inputs_bytes: bytes


def _proof_parts(proof: VerifiedWindowHostProof):
    from src.agent.correction.schema import CorrectedGeometryV3
    from src.agent.correction.window_host import WindowHostsArtifactV1
    from src.agent.correction.window_sources import (
        verify_window_resolver_inputs_artifact,
    )

    try:
        output = CorrectedGeometryV3.model_validate_json(proof.raw_output_bytes)
        verified = verify_window_resolver_inputs_artifact(proof.raw_resolver_inputs_bytes)
        inputs = verified.inputs
        artifact = WindowHostsArtifactV1.model_validate_json(proof.raw_window_hosts_bytes)
    except ValueError as exc:
        raise ValueError("window_host_proof artifact parsing failed") from exc
    output_sha256 = hashlib.sha256(proof.raw_output_bytes).hexdigest()
    if artifact.output_sha256 != output_sha256:
        raise ValueError("window_host_proof output hash does not bind raw output bytes")
    if artifact.claims.resolver_inputs_sha256 != inputs.content_sha256:
        raise ValueError("window_host_proof resolver input identity mismatch")
    return output, inputs, artifact


def _reverify_window_host_proof(
    proof: VerifiedWindowHostProof,
) -> VerifiedWindowHostProof:
    """Recheck an already-issued marker at each production consumption boundary."""
    from src.agent.correction.config import load_core_tolerances
    from src.agent.correction.window_host import recompute_window_host_claims

    output, inputs, artifact = _proof_parts(proof)
    marker = _ArtifactAuthenticatedResolverInputs(
        inputs=inputs,
        raw_inputs_bytes=proof.raw_resolver_inputs_bytes,
    )
    recomputed = recompute_window_host_claims(
        output,
        verified_inputs=marker,
        tolerances=load_core_tolerances(),
    )
    if recomputed != artifact.claims:
        raise ValueError("window_host_proof claims disagree with fresh output recompute")
    return proof


def _resolver_inputs_from_verified_proof(
    proof: VerifiedWindowHostProof,
) -> _ArtifactAuthenticatedResolverInputs:
    """Return the proof-authenticated narrow resolver-input view."""
    _output, inputs, _artifact = _proof_parts(_reverify_window_host_proof(proof))
    return _ArtifactAuthenticatedResolverInputs(
        inputs=inputs,
        raw_inputs_bytes=proof.raw_resolver_inputs_bytes,
    )


def _issue_verified_window_host_proof(
    *,
    raw_resolver_inputs_bytes: bytes,
    raw_window_hosts_bytes: bytes,
    raw_output_bytes: bytes,
) -> VerifiedWindowHostProof:
    """Accepted/integrated-verifier issuance seam; intentionally not exported.

    Phase D will call this only after its six-artifact/StageRecord verifier.
    Phase-C tests exercise the private seam with frozen in-memory artifacts;
    production consumers can only reverify an already-issued marker.
    """
    proof = VerifiedWindowHostProof(
        raw_resolver_inputs_bytes=raw_resolver_inputs_bytes,
        raw_window_hosts_bytes=raw_window_hosts_bytes,
        raw_output_bytes=raw_output_bytes,
        _token=_PROOF_CONSTRUCTION_TOKEN,
    )
    return _reverify_window_host_proof(proof)


def verify_geometry_input(geom: CorrectedGeometry, window_host_proof=None):
    """Verify source bytes/host evidence independently of EP surface building."""
    is_b5 = str(geom.schema_version) == "3"
    if is_b5 and window_host_proof is None:
        raise ValueError("v3 build requires VerifiedWindowHostProof, including zero-window output")
    if not is_b5 and window_host_proof is not None:
        raise ValueError("legacy build must not receive B5 window host proof")
    proof_artifact = None
    if is_b5:
        assert window_host_proof is not None
        reverified = _reverify_window_host_proof(window_host_proof)
        proof_geom, _inputs, proof_artifact = _proof_parts(reverified)
        if proof_geom.model_dump(mode="json") != geom.model_dump(mode="json"):
            raise ValueError("build geom was mutated after verified output bytes were issued")
        geom = proof_geom

    return geom, proof_artifact


def build_geometry(
    geom: CorrectedGeometry,
    *,
    capability_profile: str = "rectangular",
    window_host_proof: VerifiedWindowHostProof | None = None,
) -> BuildingGeometry:
    if geom.schema_version == "source_polygon_v1":
        raise ValueError("source_polygon_v1 requires the source BIM builder; this geometry-to-EnergyPlus adapter is not supported")
    geom, proof_artifact = verify_geometry_input(geom, window_host_proof)
    is_b5 = str(geom.schema_version) == "3"
    if is_b5:
        from src.agent.correction.config import load_core_tolerances

    out = BuildingGeometry(geometry_contract="c2_b5_v1" if is_b5 else "legacy")

    # 建模·几何: cells -> zone volumes (+ tiling guard notes)
    zvs, overlap_notes = build_zone_volumes(
        geom, capability_profile=capability_profile
    )
    out.zones = [zv.zone for zv in zvs]
    out.zone_volumes = zvs
    out.notes.extend(overlap_notes)

    # 切配·仿真: zone volumes -> cut + paired surfaces
    registry = NameRegistry()
    out.surfaces = pair_surfaces(zvs, registry, capability_profile=capability_profile)

    # windows -> sub-surface on the room's exterior wall on that facade
    zv_by_cell = {zv.cell_id: zv for zv in zvs}
    if is_b5:
        assert proof_artifact is not None
        out.windows = attach_windows_v3(
            geom,
            out.surfaces,
            zv_by_cell,
            registry,
            host_claims=proof_artifact.claims,
            tolerances=load_core_tolerances(),
        )
        win_notes: list[str] = []
    else:
        out.windows, win_notes = attach_windows(geom, out.surfaces, zv_by_cell, registry)
        out.notes.extend(win_notes)

    # Window completeness: every window the correction stage kept must attach.
    # A silent drop (unknown room / no exterior wall on that facade) would flow
    # downstream as "create exactly these windows" minus the lost ones and pass
    # every gate — fail loud instead; the notes name each loss and its reason.
    if len(out.windows) != len(geom.windows):
        raise ValueError(
            f"window attachment lost {len(geom.windows) - len(out.windows)} of "
            f"{len(geom.windows)} window(s): " + "; ".join(win_notes)
        )

    # F-13 (2026-08-06): `bg` (`out`) is now fully resolved — canonicalize
    # every surface/window's vertex order here, once, at the single point
    # both downstream consumers (`serialize_geometry` in specs.py,
    # `build_output_coordinate_snapshot` in output_coordinates.py) read
    # `out.surfaces[*].verts` / `out.windows[*].verts` from.
    _canonicalize_bg_vertices(out)

    from src.agent.geometry.openings import attach_openings

    out.openings = attach_openings(geom, out)

    return out
