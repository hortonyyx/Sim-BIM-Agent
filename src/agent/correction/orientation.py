"""E4 orientation-enrichment: the single deterministic path that augments an
accepted v3/Vg correction with a `NorthAxisEvidence` — including the
zero-evidence `prior_fill` default-0 mechanical producer.

Authority: ``AI_agent/archive/2026-09-08_management_rebuild/original/AI_agent/proposals/c2_e4_output_contract_spec.md`` v2 §3.2bis.
This module does not draw geometry, does not re-run the LLM, and does not
implement priority/sanity merging of multiple raw orientation observations
(that policy is explicitly out of scope for this batch, spec §0.3) — it only
consumes an already-resolved, hash-bound "orientation evidence set" (an empty
one in the overwhelmingly common case: no total-plan/north-arrow evidence
producer exists anywhere in this codebase yet) and either (a) accepts the
single merged candidate, (b) mechanically manufactures the frozen assumed-0
record, or (c) reports that a human decision is needed / that the evidence is
contradictory — it never guesses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.correction.feature_state import (
    FeatureStateClaimsV1,
    derive_feature_state_claims,
)
from src.agent.correction.finalize import FinalizeResult
from src.agent.correction.parse import CorrectionTarget, ensure_corrected_geometry
from src.agent.correction.schema import CorrectedGeometryV3, NorthAxisEvidence
from src.agent.output_coordinates import VerifiedAcceptedCorrection, sha256_bytes

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FiniteFloat = Annotated[float, AllowInfNan(False)]

# spec §3.2bis: the one machine literal for the zero-evidence prior_fill method.
PRIOR_FILL_METHOD = "prior_fill_default_zero_v1"
POLICY_REF_DEFAULT_ZERO = "c2_e4.north_axis.default_zero.v1"


class OrientationNeedsInputError(Exception):
    """`completion_mode == "interactive"` and zero accepted candidates: no
    attempt may be produced (the caller must stop and surface NEEDS_INPUT —
    this is distinct from every other failure mode below, which is a hard
    BLOCK)."""


# --------------------------------------------------------------------------- #
# strict wire types
# --------------------------------------------------------------------------- #
class OrientationConflictV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conflict_id: Hex64
    reason: str
    source_ids: tuple[str, ...]


class OrientationEvidenceSetV1(BaseModel):
    """The canonical, content-addressed input this batch actually consumes
    (spec §3.2bis: "受信 evidence set 必须...写成 canonical、content-addressed
    artifact"). Already carries the POST priority/sanity-merge result — this
    batch does not implement that merge; an empty set (the default for every
    case with no total-plan/north-arrow evidence today) has its own canonical
    hash, distinct from a missing file."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    accepted_candidates: tuple[NorthAxisEvidence, ...] = ()
    conflicts: tuple[OrientationConflictV1, ...] = ()


class OrientationRunConfigV1(BaseModel):
    """Minimal hash-bound completion-mode carrier this batch verifies against
    (spec §3.2bis "hash-bound RunConfig"). Deliberately distinct from
    ``src.agent.execution.run_config.RunConfig`` (the judge/review/grade
    ``run_config.yaml`` loader) — that type has no orientation concept and
    extending its unrelated domain was judged higher-risk than a small,
    dedicated, content-addressed record. See execution brief review-ask."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    completion_mode: Literal["interactive", "prior_fill"]


class OrientationResolutionInputV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    base_correction_sha256: Hex64
    evidence_set_sha256: Hex64
    accepted_candidates: tuple[NorthAxisEvidence, ...]
    conflicts: tuple[OrientationConflictV1, ...]
    completion_mode: Literal["interactive", "prior_fill"]


class OrientationEnrichmentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    base_correction_sha256: Hex64
    orientation_resolution_sha256: Hex64
    resolution_kind: Literal["accepted_evidence", "prior_fill_assumed_zero"]
    north_axis: NorthAxisEvidence


class NorthAxisAssumptionAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["north_axis_assumption"] = "north_axis_assumption"
    value_deg: FiniteFloat
    completion_mode: Literal["prior_fill"] = "prior_fill"
    evidence_candidate_count: Literal[0] = 0
    policy_ref: Literal["c2_e4.north_axis.default_zero.v1"] = POLICY_REF_DEFAULT_ZERO
    knowledge_ref: None = None
    knowledge_ref_na_reason: Literal["policy_default_not_knowledge_lookup"] = (
        "policy_default_not_knowledge_lookup"
    )

    @model_validator(mode="after")
    def _value_is_zero(self) -> "NorthAxisAssumptionAuditV1":
        if self.value_deg != 0.0:
            raise ValueError("NorthAxisAssumptionAuditV1.value_deg must be exactly 0.0")
        return self


class NorthAxisAcceptedEvidenceAuditV1(BaseModel):
    """Symmetric (non-frozen-by-spec, but kept strict/typed for consistency)
    audit record for the `accepted_evidence` resolution kind — spec §3.2bis
    only pins the assumed-0 wire verbatim; this mirrors its shape for the
    other resolution kind so `audit.json` has one uniform vocabulary rather
    than a typed record for one branch and a bare dict for the other."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["north_axis_accepted_evidence"] = "north_axis_accepted_evidence"
    resolution_kind: Literal["accepted_evidence"] = "accepted_evidence"
    north_axis: NorthAxisEvidence


@dataclass(frozen=True)
class OrientationEnrichmentResult(FinalizeResult):
    """Distinguishes an orientation-enrichment write from a B2/Vg draw write
    at the ``StageRunner.record`` boundary — same three fields as
    ``FinalizeResult`` (geom/audit_payload/feature_state_claims), but a
    different ``isinstance`` so the writer picks ``artifact_contract=
    'correction_e4_orientation_v1'`` instead of ``'correction_b2_v1'``."""


@dataclass(frozen=True)
class VerifiedOrientationResolution:
    """Only constructed after the three raw artifacts' hashes and cross-field
    equality have been proven — never a bare parsed model a caller could have
    mutated."""

    raw_resolution_input_bytes: bytes
    raw_evidence_set_bytes: bytes
    raw_run_config_bytes: bytes


def verify_orientation_resolution(
    *, raw_resolution_input_bytes: bytes, raw_evidence_set_bytes: bytes, raw_run_config_bytes: bytes,
) -> VerifiedOrientationResolution:
    """Hash-chain verifier for the three orientation-resolution inputs. Proves:
    (1) the resolution input's `evidence_set_sha256` matches the raw
    evidence-set bytes; (2) the resolution input's `accepted_candidates`/
    `conflicts` are a field-for-field copy of the evidence-set artifact (the
    evidence-set is the single source of truth for those two fields — the
    resolution input may not silently disagree with it); (3) the resolution
    input's `completion_mode` matches the hash-bound run config."""
    resolution_input = OrientationResolutionInputV1.model_validate_json(
        raw_resolution_input_bytes.decode("utf-8")
    )
    evidence_set = OrientationEvidenceSetV1.model_validate_json(raw_evidence_set_bytes.decode("utf-8"))
    run_config = OrientationRunConfigV1.model_validate_json(raw_run_config_bytes.decode("utf-8"))

    evidence_hash = sha256_bytes(raw_evidence_set_bytes)
    if resolution_input.evidence_set_sha256 != evidence_hash:
        raise ValueError(
            "orientation resolution input evidence_set_sha256 does not match the raw "
            "evidence-set artifact bytes"
        )
    if resolution_input.accepted_candidates != evidence_set.accepted_candidates:
        raise ValueError(
            "orientation resolution input accepted_candidates disagrees with the "
            "evidence-set artifact"
        )
    if resolution_input.conflicts != evidence_set.conflicts:
        raise ValueError("orientation resolution input conflicts disagrees with the evidence-set artifact")
    if resolution_input.completion_mode != run_config.completion_mode:
        raise ValueError(
            "orientation resolution input completion_mode disagrees with the hash-bound run config"
        )

    return VerifiedOrientationResolution(
        raw_resolution_input_bytes=raw_resolution_input_bytes,
        raw_evidence_set_bytes=raw_evidence_set_bytes,
        raw_run_config_bytes=raw_run_config_bytes,
    )


def build_orientation_resolution_input(
    *, base_correction_sha256: str, evidence_set: OrientationEvidenceSetV1, completion_mode: str,
) -> tuple[OrientationResolutionInputV1, bytes]:
    """Convenience builder for the common (empty-evidence) case: derive the
    resolution-input record straight from a (possibly empty) evidence set so
    callers never hand-assemble candidates/conflicts by hand. Returns the
    model plus its canonical bytes (== what must be written to
    `orientation_evidence_sets/<sha256>.json` and hashed for
    `evidence_set_sha256`)."""
    evidence_bytes = evidence_set.model_dump_json(indent=2).encode("utf-8")
    resolution_input = OrientationResolutionInputV1(
        base_correction_sha256=base_correction_sha256,
        evidence_set_sha256=sha256_bytes(evidence_bytes),
        accepted_candidates=evidence_set.accepted_candidates,
        conflicts=evidence_set.conflicts,
        completion_mode=completion_mode,  # type: ignore[arg-type]
    )
    return resolution_input, evidence_bytes


# --------------------------------------------------------------------------- #
# BO-CR3: on-disk artifact plumbing — the single owner of the content-
# addressed evidence-set / run-config / resolution artifacts under
# `<run>/1_correction/`. "缺文件 ≠ 空集": the verifier below only ever reads
# REAL bytes off disk; an absent artifact is a hard failure, and the canonical
# empty set exists only because this producer explicitly wrote it.
# --------------------------------------------------------------------------- #
EVIDENCE_SETS_DIRNAME = "orientation_evidence_sets"
ORIENTATION_RUN_CONFIG_NAME = "orientation_run_config.json"
ORIENTATION_RESOLUTION_NAME = "orientation_resolution_input.json"


@dataclass(frozen=True)
class OrientationInputArtifacts:
    """Paths + raw bytes of the three hash-bound orientation inputs."""

    evidence_set_path: object
    raw_evidence_set_bytes: bytes
    run_config_path: object
    raw_run_config_bytes: bytes
    resolution_path: object
    raw_resolution_input_bytes: bytes


def materialize_orientation_inputs(
    *, correction_dir, base_correction_sha256: str, completion_mode: str,
) -> OrientationInputArtifacts:
    """Producer half: materialize (or reuse) the three orientation-input
    artifacts for a run whose 1_correction stage dir is ``correction_dir``.

    Evidence-set policy for this batch (spec §3.2bis / §0.3: no total-plan /
    north-arrow evidence producer exists yet):

    - ``orientation_evidence_sets/`` holds exactly ONE artifact → use it (the
      filename must equal the sha256 of its bytes, else hard fail);
    - the directory is empty/absent → write the canonical EMPTY-set artifact
      (the explicit policy-default production, not a silent fallback);
    - more than one artifact → hard fail (ambiguous evidence identity; a
      future producer must bind the chosen set explicitly).

    ``completion_mode`` must come from the run's REAL configuration (the
    caller loads it via ``load_run_config``); this function persists it as the
    hash-bound ``orientation_run_config.json`` artifact so the resolution
    input's mode is verifiable, never a pipeline literal (BO-CR3).
    """
    from pathlib import Path

    correction_dir = Path(correction_dir)
    es_dir = correction_dir / EVIDENCE_SETS_DIRNAME
    es_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(p for p in es_dir.iterdir() if p.is_file() and p.suffix == ".json")
    if len(existing) > 1:
        raise ValueError(
            f"{es_dir} holds {len(existing)} evidence-set artifacts — ambiguous orientation "
            "evidence identity; exactly one content-addressed set may be present"
        )
    if existing:
        evidence_set_path = existing[0]
        raw_evidence_set_bytes = evidence_set_path.read_bytes()
        if evidence_set_path.stem != sha256_bytes(raw_evidence_set_bytes):
            raise ValueError(
                f"evidence-set artifact {evidence_set_path.name} does not hash to its own "
                "filename — corrupted or tampered content-addressed artifact"
            )
        evidence_set = OrientationEvidenceSetV1.model_validate_json(
            raw_evidence_set_bytes.decode("utf-8"))
    else:
        evidence_set = OrientationEvidenceSetV1()
        raw_evidence_set_bytes = evidence_set.model_dump_json(indent=2).encode("utf-8")
        evidence_set_path = es_dir / f"{sha256_bytes(raw_evidence_set_bytes)}.json"
        evidence_set_path.write_bytes(raw_evidence_set_bytes)

    run_config = OrientationRunConfigV1(completion_mode=completion_mode)  # type: ignore[arg-type]
    raw_run_config_bytes = run_config.model_dump_json(indent=2).encode("utf-8")
    run_config_path = correction_dir / ORIENTATION_RUN_CONFIG_NAME
    run_config_path.write_bytes(raw_run_config_bytes)

    resolution_input = OrientationResolutionInputV1(
        base_correction_sha256=base_correction_sha256,
        evidence_set_sha256=sha256_bytes(raw_evidence_set_bytes),
        accepted_candidates=evidence_set.accepted_candidates,
        conflicts=evidence_set.conflicts,
        completion_mode=completion_mode,  # type: ignore[arg-type]
    )
    raw_resolution_input_bytes = resolution_input.model_dump_json(indent=2).encode("utf-8")
    resolution_path = correction_dir / ORIENTATION_RESOLUTION_NAME
    resolution_path.write_bytes(raw_resolution_input_bytes)

    return OrientationInputArtifacts(
        evidence_set_path, raw_evidence_set_bytes,
        run_config_path, raw_run_config_bytes,
        resolution_path, raw_resolution_input_bytes,
    )


def load_orientation_inputs(
    *, correction_dir, evidence_set_sha256: str,
) -> OrientationInputArtifacts:
    """Verifier half: read the three artifacts back from disk by identity.
    A missing evidence-set file for the referenced hash is a HARD failure —
    it is never interpreted as an empty set (BO-CR3)."""
    from pathlib import Path

    correction_dir = Path(correction_dir)
    evidence_set_path = correction_dir / EVIDENCE_SETS_DIRNAME / f"{evidence_set_sha256}.json"
    if not evidence_set_path.is_file():
        raise ValueError(
            f"orientation evidence-set artifact {evidence_set_path} is MISSING — an absent "
            "artifact is not an empty evidence set; the resolution cannot be verified"
        )
    raw_evidence_set_bytes = evidence_set_path.read_bytes()
    if sha256_bytes(raw_evidence_set_bytes) != evidence_set_sha256:
        raise ValueError("orientation evidence-set artifact bytes do not hash to their filename")

    run_config_path = correction_dir / ORIENTATION_RUN_CONFIG_NAME
    if not run_config_path.is_file():
        raise ValueError(f"orientation run-config artifact {run_config_path} is missing")
    resolution_path = correction_dir / ORIENTATION_RESOLUTION_NAME
    if not resolution_path.is_file():
        raise ValueError(f"orientation resolution-input artifact {resolution_path} is missing")

    return OrientationInputArtifacts(
        evidence_set_path, raw_evidence_set_bytes,
        run_config_path, run_config_path.read_bytes(),
        resolution_path, resolution_path.read_bytes(),
    )


def resolve_orientation_from_run_dir(
    *, correction_dir, base, completion_mode: str, capability_profile: str = "orthogonal_polygon",
):
    """One-call orchestration used by integrated pipeline AND stepwise flow
    (single code path, BO-CR1/CR3): materialize the three input artifacts,
    re-load + verify them from disk, and run the finalize transaction.
    Returns ``(OrientationEnrichmentResult, OrientationInputArtifacts)``."""
    artifacts = materialize_orientation_inputs(
        correction_dir=correction_dir,
        base_correction_sha256=base.ref.output_sha256,
        completion_mode=completion_mode,
    )
    resolution_input = OrientationResolutionInputV1.model_validate_json(
        artifacts.raw_resolution_input_bytes.decode("utf-8"))
    reloaded = load_orientation_inputs(
        correction_dir=correction_dir,
        evidence_set_sha256=resolution_input.evidence_set_sha256,
    )
    verified_resolution = verify_orientation_resolution(
        raw_resolution_input_bytes=reloaded.raw_resolution_input_bytes,
        raw_evidence_set_bytes=reloaded.raw_evidence_set_bytes,
        raw_run_config_bytes=reloaded.raw_run_config_bytes,
    )
    result = finalize_orientation_enrichment(
        base, verified_resolution, capability_profile=capability_profile,
    )
    return result, reloaded


# --------------------------------------------------------------------------- #
# the one finalize transaction
# --------------------------------------------------------------------------- #
def finalize_orientation_enrichment(
    base: VerifiedAcceptedCorrection,
    resolution: VerifiedOrientationResolution,
    *,
    capability_profile: str = "orthogonal_polygon",
) -> FinalizeResult:
    """Parse (fresh, from raw bytes only) → resolve → rebuild → strict
    validate. Never touches attempt-directory I/O (matches
    ``finalize_correction_draw``'s division of labor — the common writer,
    ``StageRunner.record``, owns archive/hash/accepted-pointer promotion).

    Raises ``OrientationNeedsInputError`` for the one non-BLOCK, no-attempt
    outcome (`completion_mode == "interactive"` with zero candidates); raises
    ``ValueError`` for every hard-BLOCK condition (conflicts, multiple
    accepted candidates, a base that is not an accepted v3 `correction_b2_v1`
    Vg-release attempt, or a base that already carries a populated
    `north_axis`).
    """
    accepted_base_contracts = {"correction_b2_v1", "correction_b5_v1"}
    if base.ref.schema_version != "3" or base.ref.artifact_contract not in accepted_base_contracts:
        raise ValueError(
            "finalize_orientation_enrichment requires an accepted v3 base correction "
            f"(Vg-release) base; got schema_version={base.ref.schema_version!r} "
            f"artifact_contract={base.ref.artifact_contract!r}"
        )
    base_geom = ensure_corrected_geometry(json.loads(base.raw_output_bytes.decode("utf-8")))
    if not isinstance(base_geom, CorrectedGeometryV3):
        raise ValueError("finalize_orientation_enrichment: base geom is not CorrectedGeometryV3")
    if base_geom.north_axis is not None:
        raise ValueError(
            "finalize_orientation_enrichment: base correction already has a populated "
            "north_axis — this batch only augments a Vg release whose north_axis is "
            "still declared_unpopulated"
        )
    if not base_geom.facade_segments:
        raise ValueError(
            "finalize_orientation_enrichment: base correction has empty facade_segments — "
            "not a genuine Vg-release attempt"
        )
    base_correction_hash = sha256_bytes(base.raw_output_bytes)
    if base_correction_hash != base.ref.output_sha256:
        raise ValueError("finalize_orientation_enrichment: base raw bytes do not match ref.output_sha256")

    resolution_input = OrientationResolutionInputV1.model_validate_json(
        resolution.raw_resolution_input_bytes.decode("utf-8")
    )
    if resolution_input.base_correction_sha256 != base_correction_hash:
        raise ValueError(
            "finalize_orientation_enrichment: orientation resolution input is bound to a "
            "different base correction than the one supplied"
        )

    if resolution_input.conflicts:
        raise ValueError(
            f"finalize_orientation_enrichment: {len(resolution_input.conflicts)} unresolved "
            "orientation conflict(s) — BLOCK, no attempt produced"
        )
    n_candidates = len(resolution_input.accepted_candidates)
    if n_candidates > 1:
        raise ValueError(
            f"finalize_orientation_enrichment: {n_candidates} accepted orientation "
            "candidates — multiple accepted candidates is a BLOCK, not a merge decision "
            "this function makes"
        )

    orientation_resolution_hash = sha256_bytes(resolution.raw_resolution_input_bytes)

    if n_candidates == 1:
        evidence = resolution_input.accepted_candidates[0]
        resolution_kind: Literal["accepted_evidence", "prior_fill_assumed_zero"] = "accepted_evidence"
        orientation_audit = NorthAxisAcceptedEvidenceAuditV1(north_axis=evidence).model_dump()
    else:  # n_candidates == 0
        if resolution_input.completion_mode == "interactive":
            raise OrientationNeedsInputError(
                "finalize_orientation_enrichment: zero accepted candidates under "
                "completion_mode='interactive' — NEEDS_INPUT, no attempt produced"
            )
        # completion_mode == "prior_fill": the ONLY mechanical zero-evidence
        # producer this batch adds (spec §3.2bis).
        evidence = NorthAxisEvidence(
            value_deg=0.0, provenance="assumed", source_ids=[],
            uncertainty_deg=None, method=PRIOR_FILL_METHOD, frame_transform_hash=None,
        )
        resolution_kind = "prior_fill_assumed_zero"
        orientation_audit = NorthAxisAssumptionAuditV1(value_deg=0.0).model_dump()

    enrichment = OrientationEnrichmentV1(
        base_correction_sha256=base_correction_hash,
        orientation_resolution_sha256=orientation_resolution_hash,
        resolution_kind=resolution_kind,
        north_axis=evidence,
    )

    before_dump = base_geom.model_dump(exclude={"north_axis"})
    new_geom = CorrectedGeometryV3.model_validate({
        **base_geom.model_dump(), "north_axis": evidence.model_dump(),
    })
    after_dump = new_geom.model_dump(exclude={"north_axis"})
    if before_dump != after_dump:
        raise ValueError(
            "finalize_orientation_enrichment invariant: fields other than north_axis "
            "changed during rebuild"
        )

    target = CorrectionTarget(
        schema_version="3", schema_model=CorrectedGeometryV3,
        capability_profile=capability_profile, phase_contract="e4_orientation",
    )
    feature_state_claims: FeatureStateClaimsV1 = derive_feature_state_claims(target, new_geom)

    audit_payload = {
        "corrections": list(new_geom.corrections),
        "conflicts": list(new_geom.conflicts),
        "unsupported": list(new_geom.unsupported),
        "orientation": {
            **enrichment.model_dump(),
            "assumption_audit" if resolution_kind == "prior_fill_assumed_zero" else "evidence_audit": orientation_audit,
        },
    }
    if base.ref.artifact_contract == "correction_b5_v1":
        import hashlib

        from src.agent.correction.artifact_serialization import (
            serialize_correction_output,
            serialize_feature_states,
        )
        from src.agent.correction.config import load_core_tolerances
        from src.agent.correction.feature_state import FeatureStatesArtifactV1
        from src.agent.correction.finalize import PreparedCandidateIdentity
        from src.agent.correction.window_host import (
            WindowHostsArtifactV1,
            derive_window_evidence_ledger,
            recompute_window_host_claims,
        )
        from src.agent.geometry.build import _resolver_inputs_from_verified_proof

        if (
            base.window_host_proof is None
            or base.raw_window_resolver_inputs_bytes is None
            or base.raw_window_hosts_bytes is None
        ):
            raise ValueError("B5 orientation enrichment requires a verified six-artifact base")
        verified_inputs = _resolver_inputs_from_verified_proof(base.window_host_proof)
        base_hosts = WindowHostsArtifactV1.model_validate_json(base.raw_window_hosts_bytes)
        tol = load_core_tolerances()
        host_claims = recompute_window_host_claims(
            new_geom,
            verified_inputs=verified_inputs,
            tolerances=tol,
        )
        if host_claims != base_hosts.claims:
            raise ValueError("B5 orientation enrichment changed the host relationship")
        output_bytes = serialize_correction_output(new_geom)
        output_sha = hashlib.sha256(output_bytes).hexdigest()
        feature_states = FeatureStatesArtifactV1(
            output_sha256=output_sha,
            claims=feature_state_claims,
        )
        feature_bytes = serialize_feature_states(feature_states)
        prepared = PreparedCandidateIdentity(
            output_bytes=output_bytes,
            output_sha256=output_sha,
            feature_states_bytes=feature_bytes,
            feature_states_sha256=hashlib.sha256(feature_bytes).hexdigest(),
        )
        evidence = derive_window_evidence_ledger(
            new_geom,
            host_claims=host_claims,
            verified_inputs=verified_inputs,
            candidate_identity=prepared,
            tolerances=tol,
        )
        return OrientationEnrichmentResult(
            geom=new_geom,
            audit_payload=audit_payload,
            feature_state_claims=feature_state_claims,
            window_host_claims=host_claims,
            window_evidence_ledger=evidence,
            verified_window_resolver_inputs=verified_inputs,
            prepared_candidate_identity=prepared,
        )

    # Historical pre-B5 replay remains readable but is deliberately not
    # labelled B5-ready.
    return OrientationEnrichmentResult(
        geom=new_geom,
        audit_payload=audit_payload,
        feature_state_claims=feature_state_claims,
    )
