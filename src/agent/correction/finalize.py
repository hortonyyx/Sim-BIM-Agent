"""The one correction finalization transaction shared by pipeline and flow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
import hashlib
from pathlib import Path

from src.agent.correction.artifact_serialization import (
    serialize_correction_output,
    serialize_feature_states,
)
from src.agent.correction.config import CoreTolerances, load_core_tolerances
from src.agent.correction.deterministic import apply_deterministic_core
from src.agent.correction.envelope import extract_authoritative_envelope
from src.agent.correction.envelope_transform import EnvelopeAnnotationObservation
from src.agent.correction.facade_visibility import (
    VisibilityTolerances,
    materialize_all_facade_segments,
)
from src.agent.correction.feature_state import (
    FeatureStateClaimsV1,
    FeatureStatesArtifactV1,
    derive_feature_state_claims,
)
from src.agent.correction.parse import CorrectionTarget, ensure_corrected_geometry, parse_correction_draw, validate_final_corrected_geometry
from src.agent.correction.schema import CorrectedGeometry
from src.agent.correction.window_host import (
    WindowEvidenceLedgerV1,
    WindowHostClaimsV1,
    WindowHostResolutionError,
    apply_window_host_resolutions,
    derive_window_evidence_ledger,
    map_direction_binding_error,
    resolve_window_hosts,
)
from src.agent.correction.window_sources import (
    VerifiedWindowResolverInputs,
    WindowDirectionBindingError,
    WindowResolverInputError,
    canonical_json_bytes,
)

if TYPE_CHECKING:  # provenance carrier is a structural field, not a runtime dep
    from src.agent.correction.chain_provenance import AsDrawnChainProvenanceV1


@dataclass(frozen=True)
class PreparedCandidateIdentity:
    output_bytes: bytes
    output_sha256: str
    feature_states_bytes: bytes
    feature_states_sha256: str

    def __post_init__(self) -> None:
        if hashlib.sha256(self.output_bytes).hexdigest() != self.output_sha256:
            raise ValueError("candidate_output_identity_drift")
        if hashlib.sha256(self.feature_states_bytes).hexdigest() != self.feature_states_sha256:
            raise ValueError("candidate_feature_states_identity_drift")
        artifact = FeatureStatesArtifactV1.model_validate_json(self.feature_states_bytes)
        if artifact.output_sha256 != self.output_sha256:
            raise ValueError("candidate_feature_states_output_identity_drift")


@dataclass(frozen=True)
class FinalizeResult:
    geom: CorrectedGeometry
    audit_payload: dict
    feature_state_claims: FeatureStateClaimsV1
    window_host_claims: WindowHostClaimsV1 | None = None
    window_evidence_ledger: WindowEvidenceLedgerV1 | None = None
    verified_window_resolver_inputs: VerifiedWindowResolverInputs | None = None
    prepared_candidate_identity: PreparedCandidateIdentity | None = None
    # 2026-08-12 (摊 C): pure "which annotation basis" observation from the
    # v3 B2b envelope transform, if one ran (empty for legacy v1/v2 -- this
    # batch only names an outer-skin-vs-axis-line basis for the
    # AuthoritativeEnvelope-derived v3 comparison). Never gates, never
    # changes any pre-existing field on this result.
    annotation_basis: tuple[EnvelopeAnnotationObservation, ...] = ()
    # W#3 (wallhunt 2026-09-08b / dispatch 2026-09-08c S-A): the as_drawn
    # chain leg's writer-replay anchor.  Present ⟺ this result came through
    # ``finalize_as_drawn_chain_geometry`` WITH its frozen per-storey
    # compilations — the explicit dispatcher the StageRunner writer reads to
    # route the B5 replay lock onto the chain replay instead of the legacy
    # envelope core (⛔ never a shape guess off the geometry).  A chain result
    # minted WITHOUT it routes to the legacy replay and reds there by
    # construction — the omission is loud, never silently accepted.
    chain_provenance: "AsDrawnChainProvenanceV1 | None" = None


def _identity_snapshot(geom: CorrectedGeometry):
    if geom.schema_version != "3":
        return None
    return (
        tuple(floor.id for floor in geom.floors),
        tuple((window.id, window.floor_id, window.facade_segment_id) for window in geom.windows),
        tuple((segment.id, segment.floor_id) for segment in geom.facade_segments),
    )


def finalize_correction_draw(
    geom_or_payload,
    *,
    vector_dir: Path,
    verified_window_inputs: VerifiedWindowResolverInputs | None = None,
    tol: CoreTolerances | None = None,
    target: CorrectionTarget,
) -> FinalizeResult:
    """Parse/ensure → identity snapshot → envelope/core → final strict artifact.

    The function intentionally has no attempt-directory I/O: the common writer
    owns archive, hashes, and accepted-pointer promotion.
    """
    geom = (parse_correction_draw(geom_or_payload, target)
            if isinstance(geom_or_payload, dict) else ensure_corrected_geometry(geom_or_payload))
    if geom.schema_version != target.schema_version:
        raise ValueError("finalize input does not match correction target")
    if geom.schema_version != "3" and verified_window_inputs is not None:
        raise ValueError("legacy v1/v2 finalize forbids verified_window_inputs")
    before = _identity_snapshot(geom)
    if geom.schema_version == "3" and verified_window_inputs is not None:
        producer_bytes = canonical_json_bytes(geom.model_dump(mode="json"))
        if producer_bytes != verified_window_inputs.producer_draw_canonical_bytes:
            # Caller-side wiring defect (finalize was handed a different geom
            # than the one `verified_window_inputs` was built from) — not a
            # model draw mistake, so no resample can help.
            raise WindowResolverInputError(
                "source_identity_invalid", {"artifact": "producer_draw_canonical_bytes"},
                category="input_integrity_error",
            )
    if geom.schema_version == "3" and verified_window_inputs is None:
        raise WindowResolverInputError(
            "source_identity_invalid", {"artifact": "verified_window_resolver_inputs"},
            category="input_integrity_error",
        )
    tol = tol or load_core_tolerances()
    envelope = extract_authoritative_envelope(
        Path(vector_dir), footprint=geom, footprint_tolerance_m=tol.envelope_reconcile_tol_m, tol=tol,
    )
    # 2026-08-12 (摊 C): opt-in sink, see `_apply_envelope_reconcile`'s
    # docstring for why this is a mutable out-parameter rather than a return
    # value -- `apply_deterministic_core`'s return type is consumed too
    # widely (including the B5 writer's independent replay) to safely widen.
    annotation_basis_sink: list[EnvelopeAnnotationObservation] = []
    geom = apply_deterministic_core(
        geom, tol, authoritative_envelope=envelope, capability_profile=target.capability_profile,
        verified_window_inputs=verified_window_inputs,
        annotation_basis_sink=annotation_basis_sink,
    )
    if before != _identity_snapshot(geom):
        raise ValueError("finalize invariant: v3 floor/reference identities changed during core")
    if geom.schema_version == "3":
        # Vg (C2 §E1'/§9.1): the ONLY writer of `facade_segments`. Runs on the
        # core-final ring (not the producer's pre-core one) and strictly after
        # the identity snapshot compare above — an empty->populated facade
        # list here is the batch's intended write, not an identity drift.
        visibility_tol = VisibilityTolerances(
            depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
            endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
        )
        segments = materialize_all_facade_segments(geom, tolerances=visibility_tol)
        geom = geom.model_copy(update={"facade_segments": list(segments)})
        # The final-ring resolver is the typed validation boundary for every
        # v3 artifact, including an empty but verified window set.
    window_host_claims = None
    if geom.schema_version == "3" and verified_window_inputs is not None:
        try:
            window_host_claims = resolve_window_hosts(
                geom, verified_inputs=verified_window_inputs, tolerances=tol, commit=False,
            )
            geom = apply_window_host_resolutions(
                geom, claims=window_host_claims,
                verified_inputs=verified_window_inputs, tolerances=tol,
            )
        except WindowDirectionBindingError as exc:
            raise WindowHostResolutionError(
                map_direction_binding_error(
                    exc, geom=geom, verified_inputs=verified_window_inputs, phase="final",
                ),
                phase="final",
                context=exc.context,
            ) from exc
    geom = validate_final_corrected_geometry(geom)
    feature_state_claims = derive_feature_state_claims(target, geom)
    prepared_identity = None
    window_evidence = None
    if geom.schema_version == "3" and verified_window_inputs is not None:
        output_bytes = serialize_correction_output(geom)
        output_sha256 = hashlib.sha256(output_bytes).hexdigest()
        feature_states = FeatureStatesArtifactV1(
            output_sha256=output_sha256, claims=feature_state_claims,
        )
        feature_states_bytes = serialize_feature_states(feature_states)
        prepared_identity = PreparedCandidateIdentity(
            output_bytes=output_bytes,
            output_sha256=output_sha256,
            feature_states_bytes=feature_states_bytes,
            feature_states_sha256=hashlib.sha256(feature_states_bytes).hexdigest(),
        )
        try:
            window_evidence = derive_window_evidence_ledger(
                geom, host_claims=window_host_claims,
                verified_inputs=verified_window_inputs,
                candidate_identity=prepared_identity, tolerances=tol,
            )
        except WindowDirectionBindingError as exc:
            raise WindowHostResolutionError(
                map_direction_binding_error(
                    exc, geom=geom, verified_inputs=verified_window_inputs, phase="final",
                ),
                phase="final",
                context=exc.context,
            ) from exc
    return FinalizeResult(
        geom=geom,
        audit_payload={"corrections": geom.corrections, "conflicts": geom.conflicts, "unsupported": geom.unsupported},
        feature_state_claims=feature_state_claims,
        window_host_claims=window_host_claims,
        window_evidence_ledger=window_evidence,
        verified_window_resolver_inputs=verified_window_inputs,
        prepared_candidate_identity=prepared_identity,
        annotation_basis=tuple(annotation_basis_sink),
    )


def _stamp_and_validate_as_drawn_chain(geom: CorrectedGeometry) -> CorrectedGeometry:
    """The as_drawn chain's own unconditional kernel stamp (W#3), then the
    typed validation boundary — the shared tail of BOTH finalize modes
    below (⛔ one implementation, never a second copy of the derivation).

    Same "I ran, version is X" role as ``apply_deterministic_core``'s
    LAST-statement stamp, naming THIS leg's kernel (projection + snap +
    assembly + this finalize half), ⛔ never the legacy "1" this geometry
    did not run through.  Stamped before the final validation boundary, so
    everything downstream (feature claims, output bytes) hashes over the
    stamped body.
    """
    from src.agent.correction.deterministic import AS_DRAWN_CHAIN_STAMP_VERSION
    from src.agent.correction.schema import DeterministicCoreStampV1

    return validate_final_corrected_geometry(geom.model_copy(update={
        "deterministic_core_stamp": DeterministicCoreStampV1(
            version=AS_DRAWN_CHAIN_STAMP_VERSION
        ),
    }))


def finalize_as_drawn_chain_geometry(
    geom: CorrectedGeometry,
    *,
    verified_window_inputs: VerifiedWindowResolverInputs,
    target: CorrectionTarget,
    tol: CoreTolerances | None = None,
    chain_provenance: "AsDrawnChainProvenanceV1 | None" = None,
    stop_before_host_resolution: bool = False,
) -> FinalizeResult:
    """The as_drawn evidence-chain leg's finalize (W-1 T2-⑤ / rework BLK-B).

    ``finalize_correction_draw`` above is the LEGACY leg's transaction and
    stays byte-identical; its first half (``extract_authoritative_envelope``
    over a legacy ``ReadingView`` directory + ``apply_deterministic_core``)
    is semantically inapplicable here — an as_drawn product silently parses
    to an empty ReadingView shell (measured, T1 B2) and the chain's geometry
    has ALREADY been through the projection bridge's own deterministic core.
    What the v3 artifact still needs from the finalize transaction is the
    second half, and this function is exactly that half, shared shape for
    shared shape:

    * Vg: ``materialize_all_facade_segments`` on the chain-final ring (the
      ONLY writer of ``facade_segments`` — the bridge emits ``[]`` and
      ``derive_feature_state_claims`` refuses an unpopulated list);
    * the typed final validation boundary (``validate_final_corrected_
      geometry`` — the same ruler the legacy leg applies, ⛔ not a relaxed
      copy: a chain product whose cells violate it is a chain-quality red,
      not something this finalize hides);
    * the empty-but-verified window accounts: ``resolve_window_hosts`` +
      ``derive_window_evidence_ledger`` over the LEGAL EMPTY SET built by
      ``build_verified_window_inputs_as_drawn`` (producer ``windows=[]`` ⇒
      empty claims/evidence, accounts matching the product — the channel
      split itself is the FILED ``WINDOW_EVIDENCE_ON_CHAIN_NOT_ON_LEDGER``
      debt, ⛔ never a silent emptiness).

    ``stop_before_host_resolution=True`` (W#3, ruling 2026-09-08f §一) returns
    the PRE-host-resolution state — Vg + kernel stamp + typed validation,
    corrections still the core's own rows and every window still carrying
    its ORIGINAL room/span.  The B5 writer's chain replay is the only legal
    caller: the writer's prefix/suffix contract needs the core's prefix
    state, and the candidate itself supplies (and is audited for) the
    host-resolution suffix.
    """
    tol = tol or load_core_tolerances()
    geom = ensure_corrected_geometry(geom)
    if str(geom.schema_version) != "3":
        raise ValueError("as_drawn chain finalize requires a v3 geometry")
    if target.schema_version != "3":
        raise ValueError("as_drawn chain finalize requires a v3 target")
    # Vg (same rule as the legacy transaction): the facade-segment write runs
    # on the final ring, strictly before the typed validation boundary.
    visibility_tol = VisibilityTolerances(
        depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
        endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
    )
    segments = materialize_all_facade_segments(geom, tolerances=visibility_tol)
    geom = geom.model_copy(update={"facade_segments": list(segments)})
    if stop_before_host_resolution:
        # W#3 (ruling 2026-09-08f §一): the B5 writer's replay must receive
        # the state BEFORE host resolution — ``corrections`` is append-only
        # downstream of the chain core (stage_runner.py's prefix/suffix
        # contract), so the replay hands the writer the PREFIX state and the
        # candidate supplies the host-resolution suffix (its 31
        # ``window_host_resolution`` rows).  This is the as_drawn counterpart
        # of the legacy leg replaying ``apply_deterministic_core`` ALONE:
        # same Vg, same kernel stamp, same typed validation boundary —
        # everything except the finalize half that belongs to the candidate.
        # ⛔ This is a stage boundary inside the ONE production derivation,
        # never a relaxed second copy of it.
        geom = _stamp_and_validate_as_drawn_chain(geom)
        return FinalizeResult(
            geom=geom,
            audit_payload={"corrections": geom.corrections, "conflicts": geom.conflicts, "unsupported": geom.unsupported},
            feature_state_claims=derive_feature_state_claims(target, geom),
            verified_window_resolver_inputs=verified_window_inputs,
            chain_provenance=chain_provenance,
        )
    try:
        window_host_claims = resolve_window_hosts(
            geom, verified_inputs=verified_window_inputs, tolerances=tol,
            commit=False,
        )
        geom = apply_window_host_resolutions(
            geom, claims=window_host_claims,
            verified_inputs=verified_window_inputs, tolerances=tol,
        )
    except WindowDirectionBindingError as exc:
        raise WindowHostResolutionError(
            map_direction_binding_error(
                exc, geom=geom, verified_inputs=verified_window_inputs,
                phase="final",
            ),
            phase="final",
            context=exc.context,
        ) from exc
    # The as_drawn chain's own unconditional kernel stamp (W#3): same "I ran,
    # version is X" role as ``apply_deterministic_core``'s LAST-statement
    # stamp, naming THIS leg's kernel (projection + snap + assembly + this
    # finalize half), ⛔ never the legacy "1" this geometry did not run
    # through.  Stamped before the final validation boundary, so everything
    # downstream (feature claims, output bytes) hashes over the stamped body.
    geom = _stamp_and_validate_as_drawn_chain(geom)
    feature_state_claims = derive_feature_state_claims(target, geom)
    output_bytes = serialize_correction_output(geom)
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    feature_states_bytes = serialize_feature_states(FeatureStatesArtifactV1(
        output_sha256=output_sha256, claims=feature_state_claims,
    ))
    prepared_identity = PreparedCandidateIdentity(
        output_bytes=output_bytes,
        output_sha256=output_sha256,
        feature_states_bytes=feature_states_bytes,
        feature_states_sha256=hashlib.sha256(feature_states_bytes).hexdigest(),
    )
    try:
        window_evidence = derive_window_evidence_ledger(
            geom, host_claims=window_host_claims,
            verified_inputs=verified_window_inputs,
            candidate_identity=prepared_identity, tolerances=tol,
        )
    except WindowDirectionBindingError as exc:
        raise WindowHostResolutionError(
            map_direction_binding_error(
                exc, geom=geom, verified_inputs=verified_window_inputs,
                phase="final",
            ),
            phase="final",
            context=exc.context,
        ) from exc
    return FinalizeResult(
        geom=geom,
        audit_payload={"corrections": geom.corrections, "conflicts": geom.conflicts, "unsupported": geom.unsupported},
        feature_state_claims=feature_state_claims,
        window_host_claims=window_host_claims,
        window_evidence_ledger=window_evidence,
        verified_window_resolver_inputs=verified_window_inputs,
        prepared_candidate_identity=prepared_identity,
        chain_provenance=chain_provenance,
    )
