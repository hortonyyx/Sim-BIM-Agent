"""E4 output-coordinate contract: internal ``OutputCoordinateContract`` type,
hash-chain verifiers, pure derivers, sidecar persistence, and the loader that
gives graph/CLI entry points a single object to carry EP-frame decisions.

Authority: ``AI_agent/archive/2026-09-08_management_rebuild/original/AI_agent/proposals/c2_e4_output_contract_spec.md`` v2, §3–§8.
This module owns the type/derive/apply surface; the building-bound object
registry + gate lives in ``src/validator/output_coordinates.py`` (validator
layer must not import agent graph code).

Nothing in this module rotates or translates a single vertex. It only
declares — as a strict, hash-bound, content-addressed artifact — which of two
already-true situations applies to a run's accepted geometry:

  * ``world_legacy``   — v1/v2 (or an untraceable historical IntakeOutput):
    ``GlobalGeometryRules.Coordinate System = World``, Zone frame fields keep
    whatever they already were, ``Building.North Axis = 0`` placeholder.
  * ``relative_north_axis`` — v3 with an orientation-enriched accepted
    correction (E4): ``Coordinate System = Relative``, every Zone frame field
    forced to exactly ``0.0``, ``Building.North Axis = θ`` from the accepted
    correction's ``NorthAxisEvidence``.

Dispatch is explicit-only (§5.3, §1.2 invariant 4): every branch below reads a
verified ``mode``/``schema_version``/``artifact_contract`` triple — never a θ
value, never truthiness, never provenance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import CorrectedGeometryV3, NorthAxisEvidence

if TYPE_CHECKING:
    from src.agent.correction.feature_state import FeatureStatesArtifactV1
    from src.agent.geometry.modelling import BuildingGeometry
    from src.mcp.state import ConfigState
    from src.validator.checks.schema import CheckReport

Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FiniteFloat = Annotated[float, AllowInfNan(False)]

COORDINATE_REGISTRY_VERSION = "ep25.1-v1"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(model: BaseModel) -> bytes:
    """The one serialization this module hashes/writes — matches the repo-wide
    ``model_dump_json(indent=2)`` convention (manifest.py, stage_runner.py), so
    the bytes hashed here are byte-identical to the bytes a caller writes to
    disk."""
    return model.model_dump_json(indent=2).encode("utf-8")


# --------------------------------------------------------------------------- #
# §3.1 — strict types
# --------------------------------------------------------------------------- #
class AcceptedCorrectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    binding_kind: Literal["accepted_correction"] = "accepted_correction"
    schema_version: Literal["1", "2", "3"]
    output_sha256: Hex64
    acceptance: Literal["manifest", "integrated_gate1"]
    run_id: Hex32 | None = None
    accepted_attempt: Annotated[int, Field(ge=1)] | None = None
    artifact_contract: Literal[
        "correction_b2_v1", "correction_e4_orientation_v1",
        "correction_b5_v1", "correction_b5_orientation_v1",
        "base_v2", "migrated_v1",
    ] | None = None

    @model_validator(mode="after")
    def _acceptance_shape(self) -> "AcceptedCorrectionRef":
        if self.acceptance == "manifest":
            if self.run_id is None or self.accepted_attempt is None or self.artifact_contract is None:
                raise ValueError(
                    "manifest acceptance requires run_id, accepted_attempt and artifact_contract"
                )
        elif self.acceptance == "integrated_gate1":
            if self.run_id is not None or self.accepted_attempt is not None:
                raise ValueError("integrated_gate1 acceptance must not carry run_id/accepted_attempt")
            if self.artifact_contract is None:
                raise ValueError("integrated_gate1 acceptance still requires artifact_contract")
        return self


class LegacyStandaloneIntakeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    binding_kind: Literal["legacy_standalone_intake"] = "legacy_standalone_intake"
    intake_output_sha256: Hex64
    inferred_schema_family: Literal["unversioned_v1_v2"] = "unversioned_v1_v2"


SourceBinding = Annotated[
    AcceptedCorrectionRef | LegacyStandaloneIntakeRef,
    Field(discriminator="binding_kind"),
]


@dataclass(frozen=True)
class VerifiedAcceptedCorrection:
    """Only constructed after a hash-chain verifier passes; not a persisted
    wire type. Holds only immutable raw bytes — never a mutable parsed model —
    so a derive call cannot accidentally reuse a caller-mutated object."""

    ref: AcceptedCorrectionRef
    raw_output_bytes: bytes
    raw_feature_states_bytes: bytes | None
    raw_window_resolver_inputs_bytes: bytes | None = None
    raw_window_hosts_bytes: bytes | None = None
    window_host_proof: object | None = None


class OutputCoordinateContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_schema_version: Literal["1"] = "1"
    mode: Literal["world_legacy", "relative_north_axis"]
    source: SourceBinding

    geometry_frame: Literal["building_axis_absolute_values"]
    global_geometry_coordinate_system: Literal["World", "Relative"]
    daylighting_reference_point_coordinate_system: Literal["World", "Relative"]
    rectangular_surface_coordinate_system: Literal["World", "Relative"]
    zone_origin_policy: Literal["preserve_legacy", "all_zero"]
    zone_direction_policy: Literal["preserve_legacy", "all_zero"]

    north_axis_owner: Literal["legacy_mep_placeholder", "accepted_correction_orientation"]
    north_axis_deg: Annotated[float, Field(ge=0, lt=360, allow_inf_nan=False)]
    orientation_provenance: Literal["observed", "derived", "assumed"] | None = None
    orientation_source_ids: tuple[str, ...] = ()
    orientation_uncertainty_deg: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    orientation_method: str | None = None
    frame_transform_hash: Hex64 | None = None
    geometry_snapshot_sha256: Hex64 | None = None
    coordinate_registry_version: Literal["ep25.1-v1"] = COORDINATE_REGISTRY_VERSION

    # --- explicit-dispatch invariant (spec §1.2 #4): the combination below is
    # the ONLY place `mode` gets tied to a fixed constant bundle. Every other
    # module in the codebase must consume `contract.mode` as already-decided —
    # none of them may re-derive it from theta/provenance/GGR values.
    @model_validator(mode="after")
    def _mode_locked_combo(self) -> "OutputCoordinateContract":
        if self.mode == "world_legacy":
            self._check_world_legacy_source()
            self._check_field_bundle(_WORLD_LEGACY_FIXED_FIELDS)
            if (self.orientation_provenance is not None or self.orientation_source_ids
                    or self.orientation_uncertainty_deg is not None
                    or self.orientation_method is not None or self.frame_transform_hash is not None):
                raise ValueError("world_legacy contract must carry no orientation metadata")
        elif self.mode == "relative_north_axis":
            if not isinstance(self.source, AcceptedCorrectionRef):
                raise ValueError("relative_north_axis requires an accepted_correction source")
            if (
                self.source.schema_version != "3"
                or self.source.artifact_contract not in {
                    "correction_e4_orientation_v1",
                    "correction_b5_orientation_v1",
                }
            ):
                raise ValueError(
                    "relative_north_axis requires schema_version=='3' and "
                    "an orientation artifact contract"
                )
            if self.geometry_snapshot_sha256 is None:
                raise ValueError("relative_north_axis contract requires geometry_snapshot_sha256")
            self._check_field_bundle(_RELATIVE_FIXED_FIELDS)
            if self.orientation_provenance is None:
                raise ValueError("relative_north_axis contract requires orientation_provenance")
        else:  # pragma: no cover - Literal already restricts this
            raise ValueError(f"unknown contract mode {self.mode!r}")
        return self

    def _check_world_legacy_source(self) -> None:
        if isinstance(self.source, AcceptedCorrectionRef):
            if self.source.schema_version not in ("1", "2"):
                raise ValueError(
                    "world_legacy from an accepted_correction source requires "
                    "schema_version in {'1','2'}"
                )
            if self.geometry_snapshot_sha256 is None:
                raise ValueError(
                    "world_legacy accepted-correction contract requires geometry_snapshot_sha256"
                )
        elif isinstance(self.source, LegacyStandaloneIntakeRef):
            if self.geometry_snapshot_sha256 is not None:
                raise ValueError(
                    "legacy standalone contract must not carry geometry_snapshot_sha256"
                )
        else:  # pragma: no cover - discriminated union already restricts this
            raise ValueError("unknown source binding for world_legacy")

    def _check_field_bundle(self, expected: dict[str, object]) -> None:
        for field_name, value in expected.items():
            if getattr(self, field_name) != value:
                raise ValueError(
                    f"contract field {field_name!r} must be {value!r} under mode={self.mode!r}"
                )


_WORLD_LEGACY_FIXED_FIELDS: dict[str, object] = {
    "geometry_frame": "building_axis_absolute_values",
    "global_geometry_coordinate_system": "World",
    "daylighting_reference_point_coordinate_system": "Relative",
    "rectangular_surface_coordinate_system": "Relative",
    "zone_origin_policy": "preserve_legacy",
    "zone_direction_policy": "preserve_legacy",
    "north_axis_owner": "legacy_mep_placeholder",
    "north_axis_deg": 0.0,
}

_RELATIVE_FIXED_FIELDS: dict[str, object] = {
    "geometry_frame": "building_axis_absolute_values",
    "global_geometry_coordinate_system": "Relative",
    "daylighting_reference_point_coordinate_system": "Relative",
    "rectangular_surface_coordinate_system": "Relative",
    "zone_origin_policy": "all_zero",
    "zone_direction_policy": "all_zero",
    "north_axis_owner": "accepted_correction_orientation",
}


# --------------------------------------------------------------------------- #
# §3.2 — hash-chain verifiers + pure derivation
# --------------------------------------------------------------------------- #
def _fresh_expected_claims(geom):
    """BO-CR4 core: re-derive the ONLY legitimate feature-state claims for a
    freshly parsed correction geom. The phase is determined from the geom
    itself (a populated `north_axis` on a v3 geom can only have been written
    by `finalize_orientation_enrichment`; a B2/Vg finalize leaves it None) —
    never from a caller-supplied claims object. Same B-M CR-01 / Vg CR1
    pattern: the verifier recomputes, it does not trust."""
    from src.agent.correction.feature_state import derive_feature_state_claims
    from src.agent.correction.parse import CorrectionTarget

    schema_version = str(getattr(geom, "schema_version", "1") or "1")
    if schema_version == "3":
        phase = "e4_orientation" if geom.north_axis is not None else "b2"
        capability = "orthogonal_polygon"
    else:
        phase = "b2"
        capability = "rectangular"
    target = CorrectionTarget(schema_version, type(geom), capability, phase)
    return derive_feature_state_claims(target, geom)


def _require_exact_claims(feature_states: "FeatureStatesArtifactV1", geom) -> None:
    """Reject any feature-state sidecar whose claims are not FIELD-FOR-FIELD
    the fresh re-derivation (helper tuple, all four states, phase, schema)."""
    expected = _fresh_expected_claims(geom)
    if feature_states.claims != expected:
        raise ValueError(
            "feature-state claims do not match the fresh re-derivation from the "
            f"correction bytes (claimed {feature_states.claims!r}, expected {expected!r}) "
            "— forged or stale claims cannot enter the output-coordinate chain"
        )


def _contract_for_expected_claims(geom) -> str:
    """The one artifact-contract inference, driven ONLY by the fresh
    re-derived claims/geom (BO-CR4: never by caller-supplied claim strings)."""
    schema_version = str(getattr(geom, "schema_version", "1") or "1")
    if schema_version != "3":
        return "base_v2"
    return "correction_e4_orientation_v1" if geom.north_axis is not None else "correction_b2_v1"


def _verify_b5_bundle(
    *,
    raw_output_bytes: bytes,
    raw_feature_states_bytes: bytes,
    raw_window_resolver_inputs_bytes: bytes,
    raw_window_hosts_bytes: bytes,
):
    """Strictly verify the complete B5 output/feature/resolver/host chain."""
    from src.agent.correction.artifact_serialization import (
        serialize_correction_output,
        serialize_feature_states,
    )
    from src.agent.correction.config import load_core_tolerances
    from src.agent.correction.facade_visibility import (
        VisibilityTolerances,
        validate_materialized_facade_segments,
    )
    from src.agent.correction.feature_state import FeatureStatesArtifactV1
    from src.agent.correction.finalize import PreparedCandidateIdentity
    from src.agent.correction.schema import CorrectedGeometryV3
    from src.agent.correction.window_host import (
        WindowHostsArtifactV1,
        derive_window_evidence_ledger,
        recompute_window_host_claims,
    )
    from src.agent.correction.window_sources import (
        verify_window_resolver_inputs_artifact,
    )
    from src.agent.geometry.build import (
        _issue_verified_window_host_proof,
        _resolver_inputs_from_verified_proof,
    )

    output_hash = sha256_bytes(raw_output_bytes)
    geom = CorrectedGeometryV3.model_validate_json(raw_output_bytes)
    if raw_output_bytes != serialize_correction_output(geom):
        raise ValueError("B5 correction output artifact bytes are not canonical")
    verified_source_inputs = verify_window_resolver_inputs_artifact(
        raw_window_resolver_inputs_bytes
    )
    inputs = verified_source_inputs.inputs
    hosts = WindowHostsArtifactV1.model_validate_json(raw_window_hosts_bytes)
    if hosts.output_sha256 != output_hash:
        raise ValueError("window_hosts.output_sha256 does not bind output.json")
    if hosts.claims.resolver_inputs_sha256 != inputs.content_sha256:
        raise ValueError("window host resolver-input identity mismatch")

    tol = load_core_tolerances()
    validate_materialized_facade_segments(
        geom,
        tolerances=VisibilityTolerances(
            depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
            endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
        ),
    )
    feature_states = FeatureStatesArtifactV1.model_validate_json(raw_feature_states_bytes)
    if feature_states.output_sha256 != output_hash:
        raise ValueError("feature-state sidecar does not bind B5 output bytes")
    _require_exact_claims(feature_states, geom)
    if raw_feature_states_bytes != serialize_feature_states(feature_states):
        raise ValueError("feature-state artifact bytes are not canonical")

    proof = _issue_verified_window_host_proof(
        raw_resolver_inputs_bytes=raw_window_resolver_inputs_bytes,
        raw_window_hosts_bytes=raw_window_hosts_bytes,
        raw_output_bytes=raw_output_bytes,
    )
    verified_inputs = _resolver_inputs_from_verified_proof(proof)
    recomputed_claims = recompute_window_host_claims(
        geom,
        verified_inputs=verified_inputs,
        tolerances=tol,
    )
    if recomputed_claims != hosts.claims:
        raise ValueError("window host claims disagree with accepted output")
    identity = PreparedCandidateIdentity(
        output_bytes=raw_output_bytes,
        output_sha256=output_hash,
        feature_states_bytes=raw_feature_states_bytes,
        feature_states_sha256=sha256_bytes(raw_feature_states_bytes),
    )
    recomputed_evidence = derive_window_evidence_ledger(
        geom,
        host_claims=recomputed_claims,
        verified_inputs=verified_inputs,
        candidate_identity=identity,
        tolerances=tol,
    )
    if recomputed_evidence != hosts.evidence:
        raise ValueError("window evidence disagrees with accepted artifacts")
    return proof


def load_verified_accepted_correction(*, run_dir: Path, manifest) -> VerifiedAcceptedCorrection:
    """Stepwise/manifest boundary: read ONLY the accepted `1_correction`
    attempt, cross-checking every artifact hash the manifest claims for it
    (output + checks + audit + feature_states — BO-CR4 full chain) and fresh
    re-deriving the feature-state claims from the output bytes. Never reads a
    stage-root convenience copy (`correction_geometry_snapped.json`)."""
    from src.agent.correction.feature_state import FeatureStatesArtifactV1
    from src.agent.execution.manifest import RunManifestV2, hash_file

    if not isinstance(manifest, RunManifestV2):
        raise ValueError("output-coordinate contract requires a RunManifestV2 (v1 runs are legacy-only)")
    record = manifest.accepted("1_correction")
    if record is None:
        raise ValueError("no accepted 1_correction stage record in manifest")
    if not hasattr(record, "artifact_contract"):
        raise ValueError("1_correction manifest record is not a StageRecordV2 (no artifact_contract)")

    attempt_dir = Path(run_dir) / "1_correction" / "attempts" / f"{record.accepted_attempt:03d}"
    output_path = attempt_dir / "output.json"
    if not output_path.is_file():
        raise ValueError(f"accepted 1_correction attempt output.json missing: {output_path}")
    output_hash = hash_file(output_path)
    if output_hash != record.output_hash or output_hash != record.artifact_hashes.get("output"):
        raise ValueError("accepted 1_correction output.json hash does not match manifest record")
    raw_output_bytes = output_path.read_bytes()

    artifact_files = (
        ("output", "output.json"),
        ("checks", "checks.json"), ("audit", "audit.json"),
        ("feature_states", "feature_states.json"),
        ("window_resolver_inputs", "window_resolver_inputs.json"),
        ("window_hosts", "window_hosts.json"),
    )
    # Every claimed artifact must exist and hash-match.  Contract validation on
    # StageRecordV2 has already enforced the exact allowed/required key set.
    for key, filename in artifact_files:
        claimed = record.artifact_hashes.get(key)
        if claimed is None:
            continue
        path = attempt_dir / filename
        if not path.is_file() or hash_file(path) != claimed:
            raise ValueError(
                f"accepted 1_correction {filename} is missing or does not match the "
                f"manifest's artifact_hashes[{key!r}]"
            )

    geom = ensure_corrected_geometry(json.loads(raw_output_bytes.decode("utf-8")))
    schema_version = str(getattr(geom, "schema_version", "1") or "1")
    if schema_version not in ("1", "2", "3"):
        raise ValueError(f"unsupported accepted correction schema_version {schema_version!r}")

    raw_feature_states_bytes: bytes | None = None
    raw_window_resolver_inputs_bytes: bytes | None = None
    raw_window_hosts_bytes: bytes | None = None
    window_host_proof = None
    feature_contracts = {
        "correction_b2_v1",
        "correction_e4_orientation_v1",
        "correction_b5_v1",
        "correction_b5_orientation_v1",
    }
    b5_contracts = {"correction_b5_v1", "correction_b5_orientation_v1"}
    if record.artifact_contract in feature_contracts:
        fs_path = attempt_dir / "feature_states.json"
        if not fs_path.is_file() or "feature_states" not in record.artifact_hashes:
            raise ValueError(f"{record.artifact_contract} accepted attempt is missing feature_states.json")
        raw_feature_states_bytes = fs_path.read_bytes()
        feature_states = FeatureStatesArtifactV1.model_validate_json(
            raw_feature_states_bytes.decode("utf-8"))
        if feature_states.output_sha256 != output_hash:
            raise ValueError("feature-state sidecar does not bind the accepted output bytes")
        _require_exact_claims(feature_states, geom)
        # the record's claimed contract must agree with what the geom itself proves
        expected_contracts = (
            {"correction_b5_orientation_v1"}
            if geom.north_axis is not None
            else {"correction_b5_v1"}
        )
        if schema_version == "3" and record.artifact_contract not in expected_contracts:
            raise ValueError(
                f"manifest artifact_contract {record.artifact_contract!r} disagrees with the "
                "v3 B5 proof requirement "
                f"(expected one of {sorted(expected_contracts)!r})"
            )
    elif record.artifact_contract not in ("base_v2", "migrated_v1"):
        raise ValueError(f"unrecognized 1_correction artifact_contract {record.artifact_contract!r}")
    elif schema_version == "3":
        raise ValueError(
            "a v3 accepted correction may not travel under a "
            f"{record.artifact_contract!r} record — feature-state sidecar is mandatory for v3"
        )

    if record.artifact_contract in b5_contracts:
        resolver_path = attempt_dir / "window_resolver_inputs.json"
        hosts_path = attempt_dir / "window_hosts.json"
        raw_window_resolver_inputs_bytes = resolver_path.read_bytes()
        raw_window_hosts_bytes = hosts_path.read_bytes()
        from src.agent.correction.window_sources import (
            WindowResolverInputsArtifactV1,
            verify_window_resolver_inputs_against_raw_artifacts,
        )
        from src.agent.execution.run_meta import run_meta_path
        from src.agent.execution.view_manifest import VIEW_MANIFEST_NAME

        persisted_artifact = WindowResolverInputsArtifactV1.model_validate_json(
            raw_window_resolver_inputs_bytes
        )
        persisted_inputs = persisted_artifact.inputs
        if persisted_inputs.view_manifest.content_sha256 != manifest.run_inputs.view_manifest_sha256:
            raise ValueError("B5 resolver manifest identity disagrees with RunManifestV2.run_inputs")
        raw_manifest_path = run_meta_path(Path(run_dir), VIEW_MANIFEST_NAME)
        if not raw_manifest_path.is_file():
            raise ValueError("B5 accepted loader requires the run-bound view_manifest.json")
        raw_readings: dict[str, bytes] = {}
        for identity in persisted_inputs.reading_artifacts:
            reading_path = Path(run_dir) / "0_reading" / f"{identity.expected_output_id}.json"
            if not reading_path.is_file():
                raise ValueError(f"B5 accepted loader missing raw reading artifact: {reading_path}")
            raw_readings[identity.input_id] = reading_path.read_bytes()
        verify_window_resolver_inputs_against_raw_artifacts(
            persisted_inputs,
            raw_view_manifest_bytes=raw_manifest_path.read_bytes(),
            raw_reading_artifacts=raw_readings,
        )
        assert raw_feature_states_bytes is not None
        window_host_proof = _verify_b5_bundle(
            raw_output_bytes=raw_output_bytes,
            raw_feature_states_bytes=raw_feature_states_bytes,
            raw_window_resolver_inputs_bytes=raw_window_resolver_inputs_bytes,
            raw_window_hosts_bytes=raw_window_hosts_bytes,
        )

    ref = AcceptedCorrectionRef(
        schema_version=schema_version,  # type: ignore[arg-type]
        output_sha256=output_hash,
        acceptance="manifest",
        run_id=manifest.run_id,
        accepted_attempt=record.accepted_attempt,
        artifact_contract=record.artifact_contract,
    )
    return VerifiedAcceptedCorrection(
        ref=ref,
        raw_output_bytes=raw_output_bytes,
        raw_feature_states_bytes=raw_feature_states_bytes,
        raw_window_resolver_inputs_bytes=raw_window_resolver_inputs_bytes,
        raw_window_hosts_bytes=raw_window_hosts_bytes,
        window_host_proof=window_host_proof,
    )


def verify_integrated_gate1_correction(
    *, raw_output_bytes: bytes, correction_report: "CheckReport",
    feature_states: "FeatureStatesArtifactV1",
    raw_window_resolver_inputs_bytes: bytes | None = None,
    raw_window_hosts_bytes: bytes | None = None,
) -> VerifiedAcceptedCorrection:
    """Integrated boundary (``run_pipeline``): construct a verified bundle only
    after gate① reports no blocking result on the just-serialized bytes.

    BO-CR4: the caller-supplied ``feature_states`` is NEVER trusted for the
    artifact-contract decision — the claims are fresh re-derived from the
    parsed bytes via ``derive_feature_state_claims()`` and the sidecar must be
    field-for-field identical (exact helper tuple + all four states + phase),
    otherwise the bundle is rejected outright."""
    if correction_report.blocking():
        raise ValueError(
            "cannot verify an integrated accepted correction: gate① reports "
            f"{len(correction_report.blocking())} blocking result(s)"
        )
    output_hash = sha256_bytes(raw_output_bytes)
    if feature_states.output_sha256 != output_hash:
        raise ValueError("feature-state artifact does not bind the just-serialized correction bytes")

    geom = ensure_corrected_geometry(json.loads(raw_output_bytes.decode("utf-8")))
    schema_version = str(getattr(geom, "schema_version", "1") or "1")
    if schema_version not in ("1", "2", "3"):
        raise ValueError(f"unsupported accepted correction schema_version {schema_version!r}")

    raw_feature_states_bytes: bytes | None = None
    if schema_version == "3":
        _require_exact_claims(feature_states, geom)
        raw_feature_states_bytes = canonical_json_bytes(feature_states)
    if (raw_window_resolver_inputs_bytes is None) != (raw_window_hosts_bytes is None):
        raise ValueError("integrated B5 verification requires both window sidecars")
    if schema_version == "3" and raw_window_resolver_inputs_bytes is None:
        raise ValueError(
            "integrated v3 correction requires the complete B5 resolver/host proof bundle"
        )
    window_host_proof = None
    if schema_version == "3" and raw_window_resolver_inputs_bytes is not None:
        assert raw_window_hosts_bytes is not None and raw_feature_states_bytes is not None
        window_host_proof = _verify_b5_bundle(
            raw_output_bytes=raw_output_bytes,
            raw_feature_states_bytes=raw_feature_states_bytes,
            raw_window_resolver_inputs_bytes=raw_window_resolver_inputs_bytes,
            raw_window_hosts_bytes=raw_window_hosts_bytes,
        )
        artifact_contract = (
            "correction_b5_orientation_v1"
            if geom.north_axis is not None
            else "correction_b5_v1"
        )
    else:
        artifact_contract = _contract_for_expected_claims(geom)

    ref = AcceptedCorrectionRef(
        schema_version=schema_version,  # type: ignore[arg-type]
        output_sha256=output_hash,
        acceptance="integrated_gate1",
        run_id=None,
        accepted_attempt=None,
        artifact_contract=artifact_contract,  # type: ignore[arg-type]
    )
    return VerifiedAcceptedCorrection(
        ref=ref,
        raw_output_bytes=raw_output_bytes,
        raw_feature_states_bytes=raw_feature_states_bytes,
        raw_window_resolver_inputs_bytes=raw_window_resolver_inputs_bytes,
        raw_window_hosts_bytes=raw_window_hosts_bytes,
        window_host_proof=window_host_proof,
    )


def derive_output_coordinate_contract(
    verified: VerifiedAcceptedCorrection, *, geometry_snapshot_sha256: Hex64,
) -> OutputCoordinateContract:
    """Pure function: every call re-hashes the bytes it was given and does a
    FRESH strict parse — it never trusts a caller's already-parsed, possibly
    mutated model."""
    from src.agent.correction.feature_state import FeatureStatesArtifactV1

    ref = verified.ref
    recomputed_hash = sha256_bytes(verified.raw_output_bytes)
    if recomputed_hash != ref.output_sha256:
        raise ValueError("accepted correction raw bytes do not match the ref's output_sha256")
    geom = ensure_corrected_geometry(json.loads(verified.raw_output_bytes.decode("utf-8")))
    geom_schema_version = str(getattr(geom, "schema_version", "1") or "1")
    if geom_schema_version != ref.schema_version:
        raise ValueError("accepted correction schema_version does not match ref.schema_version")

    if ref.schema_version in ("1", "2"):
        return OutputCoordinateContract(
            mode="world_legacy",
            source=ref,
            **_WORLD_LEGACY_FIXED_FIELDS,
            geometry_snapshot_sha256=geometry_snapshot_sha256,
        )

    if ref.schema_version != "3":
        raise ValueError(f"unknown accepted correction schema_version {ref.schema_version!r}")
    if ref.artifact_contract not in {
        "correction_e4_orientation_v1",
        "correction_b5_orientation_v1",
    }:
        raise ValueError(
            "v3 accepted correction cannot derive relative_north_axis: artifact_contract is "
            f"{ref.artifact_contract!r}, not an orientation contract (a base artifact "
            "cannot masquerade as E4-ready)"
        )
    if (
        ref.artifact_contract == "correction_b5_orientation_v1"
        and (
            verified.raw_window_resolver_inputs_bytes is None
            or verified.raw_window_hosts_bytes is None
            or verified.window_host_proof is None
        )
    ):
        raise ValueError("B5 output-coordinate derivation requires the verified six-artifact chain")
    if verified.raw_feature_states_bytes is None:
        raise ValueError("relative_north_axis derivation requires feature-state bytes")
    feature_states = FeatureStatesArtifactV1.model_validate_json(
        verified.raw_feature_states_bytes.decode("utf-8")
    )
    if feature_states.output_sha256 != recomputed_hash:
        raise ValueError("feature-state bytes do not bind the accepted correction bytes")
    if not isinstance(geom, CorrectedGeometryV3) or geom.north_axis is None:
        raise ValueError("v3 correction has no populated north_axis — cannot derive relative_north_axis")
    # BO-CR4: field-for-field fresh re-derivation, not a single-state peek.
    _require_exact_claims(feature_states, geom)
    evidence: NorthAxisEvidence = geom.north_axis

    return OutputCoordinateContract(
        mode="relative_north_axis",
        source=ref,
        **_RELATIVE_FIXED_FIELDS,
        north_axis_deg=evidence.value_deg,
        orientation_provenance=evidence.provenance,
        orientation_source_ids=tuple(evidence.source_ids),
        orientation_uncertainty_deg=evidence.uncertainty_deg,
        orientation_method=evidence.method,
        frame_transform_hash=evidence.frame_transform_hash,
        geometry_snapshot_sha256=geometry_snapshot_sha256,
    )


def legacy_contract_for_unversioned_intake(*, intake_output_sha256: Hex64) -> OutputCoordinateContract:
    """The single absence-to-legacy compatibility factory (spec §3.4 item 4):
    a historical 11-field IntakeOutput with no correction/run metadata at all.
    Always World; never upgradeable to Relative."""
    ref = LegacyStandaloneIntakeRef(intake_output_sha256=intake_output_sha256)
    return OutputCoordinateContract(
        mode="world_legacy",
        source=ref,
        **_WORLD_LEGACY_FIXED_FIELDS,
        geometry_snapshot_sha256=None,
    )


# --------------------------------------------------------------------------- #
# §3.4 — coordinate snapshot (companion baseline proving no E4 vertex drift)
# --------------------------------------------------------------------------- #
class CoordinateRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    object_type: Literal["BuildingSurface:Detailed", "FenestrationSurface:Detailed"]
    name: str
    zone_or_parent: str
    vertices: tuple[tuple[FiniteFloat, FiniteFloat, FiniteFloat], ...]


class OutputCoordinateSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_schema_version: Literal["1"] = "1"
    quantization: Literal["geometry_specs_2dp"] = "geometry_specs_2dp"
    zone_names: tuple[str, ...]
    records: tuple[CoordinateRecordV1, ...]


def build_output_coordinate_snapshot(bg: "BuildingGeometry") -> OutputCoordinateSnapshotV1:
    """Same-source-as-serializer snapshot: 2dp-quantized vertices for every
    surface/fenestration, name-sorted. Used as the pre/post-E4 baseline that
    proves vertex values were never touched by the coordinate-frame switch."""
    rows: list[tuple[str, str, str, tuple[tuple[float, float, float], ...]]] = []
    for s in bg.surfaces:
        rows.append((
            "BuildingSurface:Detailed", s.name, s.zone,
            tuple((round(v[0], 2), round(v[1], 2), round(v[2], 2)) for v in s.verts),
        ))
    for w in bg.windows:
        rows.append((
            "FenestrationSurface:Detailed", w.name, w.parent,
            tuple((round(v[0], 2), round(v[1], 2), round(v[2], 2)) for v in w.verts),
        ))
    rows.sort(key=lambda r: (r[0], r[1]))
    records = tuple(
        CoordinateRecordV1(object_type=t, name=n, zone_or_parent=z, vertices=v)
        for t, n, z, v in rows
    )
    zone_names = tuple(sorted(dict.fromkeys(bg.zones)))
    return OutputCoordinateSnapshotV1(zone_names=zone_names, records=records)


# --------------------------------------------------------------------------- #
# §5.2 — single apply function
# --------------------------------------------------------------------------- #
def apply_output_coordinate_contract(config: "ConfigState", contract: OutputCoordinateContract) -> "ConfigState":
    """Deep-copy `config`, set GGR A3/A4/A5 + Building North Axis, and (for
    ``relative_north_axis``) force every existing Zone's four frame fields to
    exactly 0.0. Returns a fresh validated ConfigState; never mutates the
    input. Legacy mode leaves Zone frame fields exactly as they already are —
    it does not zero them (that would be a v1/v2 behavior change, out of
    scope per spec §0.3)."""
    from src.validator.data_model import GlobalGeometryRulesSchema

    new_config = config.model_copy(deep=True)
    if new_config.building is None:
        raise ValueError("apply_output_coordinate_contract requires ConfigState.building to be seeded first")

    new_config.building = new_config.building.model_validate(
        {**new_config.building.model_dump(by_alias=True), "North Axis": contract.north_axis_deg},
    )
    new_config.global_geometry_rules = GlobalGeometryRulesSchema.model_validate({
        "Starting Vertex Position": new_config.global_geometry_rules.starting_vertex_position,
        "Vertex Entry Direction": new_config.global_geometry_rules.vertex_entry_direction,
        "Coordinate System": contract.global_geometry_coordinate_system,
        "Daylighting Reference Point Coordinate System": contract.daylighting_reference_point_coordinate_system,
        "Rectangular Surface Coordinate System": contract.rectangular_surface_coordinate_system,
    })
    if contract.zone_origin_policy == "all_zero":
        new_config.zones = [
            zone.model_validate({
                **zone.model_dump(by_alias=True),
                "X Origin": 0.0, "Y Origin": 0.0, "Z Origin": 0.0,
                "Direction of Relative North": 0.0,
            })
            for zone in new_config.zones
        ]
    return new_config


# --------------------------------------------------------------------------- #
# §6 — Zone Origin/Direction zeroing (with audit, spec §6.2/§7.4)
# --------------------------------------------------------------------------- #
class ZoneFrameNormalizationEntryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    zone_name: str
    before: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
    after: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]

    @model_validator(mode="after")
    def _after_is_zero(self) -> "ZoneFrameNormalizationEntryV1":
        if self.after != (0.0, 0.0, 0.0, 0.0):
            raise ValueError("ZoneFrameNormalizationEntryV1.after must be exactly (0.0, 0.0, 0.0, 0.0)")
        return self


def zero_zone_frames_with_audit(
    config: "ConfigState", contract: OutputCoordinateContract,
) -> tuple["ConfigState", tuple[ZoneFrameNormalizationEntryV1, ...]]:
    """`zone_agent`'s tail-of-stage normalizer (spec §6.2 item 4): overwrite
    every zone's four frame fields to exactly 0.0 under an all_zero policy and
    return an immutable, name-sorted audit trail of what actually changed. A
    `preserve_legacy` policy (world_legacy contract) is a no-op — legacy
    v1/v2 Zone-frame semantics are explicitly out of scope (spec §0.3/§6.3)."""
    if contract.zone_origin_policy != "all_zero":
        return config, ()
    new_config = config.model_copy(deep=True)
    entries: list[ZoneFrameNormalizationEntryV1] = []
    new_zones = []
    for zone in new_config.zones:
        before = (
            float(zone.x_origin), float(zone.y_origin), float(zone.z_origin),
            float(zone.direction_of_relative_north or 0.0),
        )
        if before != (0.0, 0.0, 0.0, 0.0):
            entries.append(ZoneFrameNormalizationEntryV1(zone_name=zone.name, before=before, after=(0.0, 0.0, 0.0, 0.0)))
        new_zones.append(zone.model_validate({
            **zone.model_dump(by_alias=True),
            "X Origin": 0.0, "Y Origin": 0.0, "Z Origin": 0.0, "Direction of Relative North": 0.0,
        }))
    new_config.zones = new_zones
    entries.sort(key=lambda e: e.zone_name)
    return new_config, tuple(entries)


def validate_zone_frames_all_zero(config: "ConfigState") -> list[str]:
    """Iterate ALL zones (not just the first / not an average — spec §6.2 item
    6) and return the sorted list of offending zone names whose frame is not
    exactly (0,0,0,0)."""
    offenders = []
    for zone in config.zones:
        vals = (zone.x_origin, zone.y_origin, zone.z_origin, zone.direction_of_relative_north or 0.0)
        if vals != (0.0, 0.0, 0.0, 0.0):
            offenders.append(zone.name)
    return sorted(offenders)


# --------------------------------------------------------------------------- #
# §7.4 — assembly/export coordinate audits
# --------------------------------------------------------------------------- #
class AssemblyCoordinateAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    correction_output_sha256: Hex64
    contract_sha256: Hex64
    snapshot_sha256: Hex64
    mep_placeholder_north_axis: FiniteFloat
    final_building_north_axis: FiniteFloat
    zone_origin_policy: Literal["preserve_legacy", "all_zero"]

    @model_validator(mode="after")
    def _placeholder_is_zero(self) -> "AssemblyCoordinateAuditV1":
        if self.mep_placeholder_north_axis != 0.0:
            raise ValueError("AssemblyCoordinateAuditV1.mep_placeholder_north_axis must be 0.0")
        return self


# BO-CR9: `ExportCoordinateAuditV1` now lives in
# `src.validator.output_coordinates` (validator already imports this module,
# so the strict `offenders: tuple[OutputCoordinateIssue, ...]` typing is only
# expressible there without an import cycle).


@dataclass(frozen=True)
class AssemblyE4Write:
    """Marker type consumed by ``StageRunner.record`` (§3.4): the `5_intakeoutput`
    accepted-attempt payload for the ``assembly_e4_v1`` artifact contract —
    the final IntakeOutput plus its two output-coordinate sidecars plus the
    assembly audit, all written into ONE attempt directory by the shared
    writer (not three separate `record()` calls, which would break the
    single-attempt-directory/single-hash-set guarantee).

    ``input_hashes``: the S5 identity bindings (at least
    ``{"1_correction": <accepted correction output hash>}`` — spec §3.5). The
    writer merges them into the manifest record so orchestration layers that
    call ``record()`` without explicit input hashes (the flow loop) still
    produce a fully bound S5 record."""

    intake: object  # IntakeOutput
    contract: OutputCoordinateContract
    snapshot: OutputCoordinateSnapshotV1
    audit: AssemblyCoordinateAuditV1
    input_hashes: tuple[tuple[str, str], ...] = ()


def build_assembly_coordinate_audit(
    *, verified: VerifiedAcceptedCorrection, contract: OutputCoordinateContract,
    snapshot_bytes: bytes, mep_placeholder_north_axis: float, final_building_north_axis: float,
) -> AssemblyCoordinateAuditV1:
    return AssemblyCoordinateAuditV1(
        correction_output_sha256=verified.ref.output_sha256,
        contract_sha256=sha256_bytes(canonical_json_bytes(contract)),
        snapshot_sha256=sha256_bytes(snapshot_bytes),
        mep_placeholder_north_axis=mep_placeholder_north_axis,
        final_building_north_axis=final_building_north_axis,
        zone_origin_policy=contract.zone_origin_policy,
    )


# --------------------------------------------------------------------------- #
# §8.1 — validation context (raw bytes only; never a bare "verified" bool)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutputCoordinateValidationContext:
    raw_intake_output_bytes: bytes
    verified_correction: VerifiedAcceptedCorrection | None
    raw_snapshot_bytes: bytes | None


def context_from_verified(
    *, raw_intake_output_bytes: bytes,
    verified_correction: VerifiedAcceptedCorrection | None,
    coordinate_snapshot: OutputCoordinateSnapshotV1 | None,
) -> OutputCoordinateValidationContext:
    raw_snapshot_bytes = (
        canonical_json_bytes(coordinate_snapshot) if coordinate_snapshot is not None else None
    )
    return OutputCoordinateValidationContext(
        raw_intake_output_bytes=raw_intake_output_bytes,
        verified_correction=verified_correction,
        raw_snapshot_bytes=raw_snapshot_bytes,
    )


# --------------------------------------------------------------------------- #
# §3.4 / §3.5 — bundle + assembly + loader
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IntakeArtifactBundle:
    intake: object  # IntakeOutput; typed loosely to avoid an agent.state<->this-module import cycle at class-def time
    output_coordinates: OutputCoordinateContract
    coordinate_snapshot: OutputCoordinateSnapshotV1 | None
    validation_context: OutputCoordinateValidationContext


def assemble_intake_artifacts(
    *, zone_specs: str, surface_specs: str, fenestration_specs: str,
    mep, correction: VerifiedAcceptedCorrection, coordinate_snapshot: OutputCoordinateSnapshotV1,
) -> IntakeArtifactBundle:
    from src.agent.intakeoutput import assemble_intake_output

    snapshot_bytes = canonical_json_bytes(coordinate_snapshot)
    contract = derive_output_coordinate_contract(
        correction, geometry_snapshot_sha256=sha256_bytes(snapshot_bytes),
    )
    intake = assemble_intake_output(
        zone_specs=zone_specs, surface_specs=surface_specs, fenestration_specs=fenestration_specs,
        mep=mep, output_coordinates=contract,
    )
    raw_intake_bytes = intake.model_dump_json(indent=2).encode("utf-8")
    context = context_from_verified(
        raw_intake_output_bytes=raw_intake_bytes,
        verified_correction=correction,
        coordinate_snapshot=coordinate_snapshot,
    )
    return IntakeArtifactBundle(
        intake=intake, output_coordinates=contract,
        coordinate_snapshot=coordinate_snapshot, validation_context=context,
    )


def coordinate_semantic_projection(contract: OutputCoordinateContract) -> dict:
    """Parity helper (spec §3.5): compare two contracts' semantic content while
    excluding the `source` acceptance-proof envelope, which legitimately
    differs between integrated_gate1 and manifest acceptance for the same
    underlying correction bytes."""
    return contract.model_dump(exclude={"source"})


def resolve_run_dir_for_intake(intake_path: Path, *, default: Path | None = None) -> Path | None:
    """BO-CR2: resolve the run-identity directory for an IntakeOutput file
    from its OWN location. Walks upward from the file's parent looking for a
    directory that holds a run manifest (`<dir>/_run/run_manifest.json`), so a
    nested stepwise layout (`<case>/<run>/5_intakeoutput/intake_output.json`)
    binds to `<run>`'s manifest instead of a caller-guessed case root. Falls
    back to ``default`` when no manifest is found on the walk."""
    from src.agent.execution.manifest import MANIFEST_NAME
    from src.agent.execution.run_meta import run_meta_path

    intake_path = Path(intake_path)
    current = intake_path.parent
    for candidate in (current, *current.parents):
        if run_meta_path(candidate, MANIFEST_NAME).is_file():
            return candidate
        if default is not None and candidate == Path(default):
            break
    return default


def load_intake_bundle(intake_path: Path, *, run_dir: Path | None = None) -> IntakeArtifactBundle:
    """Priority order per spec §3.4:

    1. V2 manifest with an ``assembly_e4_v1`` accepted S5 attempt: read both
       sidecars from that attempt, verify every hash.
    2. integrated/flat: same-directory sidecars next to ``intake_path``.
    3. no sidecar but visible v3/correction/run metadata: hard fail (never
       silently downgrade to legacy).
    4. no sidecar and no correction/v3/run identity at all: the single
       absence-to-legacy compatibility path.
    """
    from src.agent.execution.manifest import RunManifestV2, hash_file, load_run_manifest
    from src.agent.state import IntakeOutput

    intake_path = Path(intake_path)
    raw_intake_bytes = intake_path.read_bytes()
    intake = IntakeOutput.model_validate_json(raw_intake_bytes.decode("utf-8"))

    manifest = load_run_manifest(run_dir) if run_dir is not None else None
    if isinstance(manifest, RunManifestV2):
        record = manifest.accepted("5_intakeoutput")
        if record is not None and getattr(record, "artifact_contract", None) == "assembly_e4_v1":
            attempt_dir = Path(run_dir) / "5_intakeoutput" / "attempts" / f"{record.accepted_attempt:03d}"
            contract_path = attempt_dir / "output_coordinate_contract.json"
            snapshot_path = attempt_dir / "output_coordinate_snapshot.json"
            output_path = attempt_dir / "output.json"
            checks_path = attempt_dir / "checks.json"
            audit_path = attempt_dir / "audit.json"
            for p in (contract_path, snapshot_path, output_path, checks_path, audit_path):
                if not p.is_file():
                    raise ValueError(f"assembly_e4_v1 accepted attempt is missing required artifact: {p}")
            # BO-CR4: ALL five artifact hashes, not just three.
            for key, path in (("output", output_path), ("checks", checks_path),
                               ("audit", audit_path),
                               ("output_coordinate_contract", contract_path),
                               ("output_coordinate_snapshot", snapshot_path)):
                if hash_file(path) != record.artifact_hashes.get(key):
                    raise ValueError(f"assembly_e4_v1 accepted attempt hash mismatch for {key!r}")
            contract = OutputCoordinateContract.model_validate_json(contract_path.read_text(encoding="utf-8"))
            raw_snapshot_bytes = snapshot_path.read_bytes()
            snapshot = OutputCoordinateSnapshotV1.model_validate_json(raw_snapshot_bytes.decode("utf-8"))
            attempt_intake_bytes = output_path.read_bytes()
            verified_correction = load_verified_accepted_correction(run_dir=Path(run_dir), manifest=manifest)

            # BO-CR4: the contract's source identity must equal the verified
            # manifest identity FIELD-FOR-FIELD, not merely parse.
            correction_record = manifest.accepted("1_correction")
            src = contract.source
            if not isinstance(src, AcceptedCorrectionRef):
                raise ValueError("assembly_e4_v1 contract sidecar does not carry an accepted_correction source")
            if (src.acceptance != "manifest"
                    or src.run_id != manifest.run_id
                    or src.accepted_attempt != correction_record.accepted_attempt
                    or src.output_sha256 != verified_correction.ref.output_sha256
                    or src.schema_version != verified_correction.ref.schema_version
                    or src.artifact_contract != correction_record.artifact_contract):
                raise ValueError(
                    "assembly_e4_v1 contract source identity does not match the run's "
                    "verified accepted correction (run_id/attempt/hash/contract/schema)"
                )
            # snapshot binding
            if contract.geometry_snapshot_sha256 != sha256_bytes(raw_snapshot_bytes):
                raise ValueError("contract.geometry_snapshot_sha256 does not match the snapshot sidecar bytes")
            # BO-CR4 (invalidation-by-verification): S5 must have been
            # assembled AGAINST the currently accepted correction — a
            # correction retry that moved the accepted pointer invalidates
            # this S5 attempt.
            s5_correction_input = record.input_hashes.get("1_correction")
            if s5_correction_input != verified_correction.ref.output_sha256:
                raise ValueError(
                    "accepted 5_intakeoutput input_hashes['1_correction'] does not bind the "
                    "currently accepted correction — the S5 attempt is stale/invalidated"
                )
            # root convenience mirrors, when present, must be byte-identical
            # to the accepted attempt (a polluted mirror is reported, never
            # silently preferred).
            for mirror_name, attempt_file in (
                ("output_coordinate_contract.json", contract_path),
                ("output_coordinate_snapshot.json", snapshot_path),
            ):
                mirror = Path(run_dir) / "5_intakeoutput" / mirror_name
                if mirror.is_file() and mirror.read_bytes() != attempt_file.read_bytes():
                    raise ValueError(
                        f"stage-root mirror {mirror_name} has drifted from the accepted "
                        "attempt sidecar — refusing a polluted convenience copy"
                    )
            context = context_from_verified(
                raw_intake_output_bytes=attempt_intake_bytes,
                verified_correction=verified_correction,
                coordinate_snapshot=snapshot,
            )
            return IntakeArtifactBundle(
                intake=IntakeOutput.model_validate_json(attempt_intake_bytes.decode("utf-8")),
                output_coordinates=contract, coordinate_snapshot=snapshot, validation_context=context,
            )
        if record is not None:
            # BO-CR4 / review-ask #3 REJECT: a run with ANY accepted
            # correction identity must travel through accepted-correction
            # derive — the LegacyStandaloneIntakeRef escape hatch is reserved
            # exclusively for historical 11-field files with NO correction/run
            # metadata whatsoever. A pre-E4 stepwise run (v1/v2 included) must
            # be explicitly migrated (re-run S5 through the E4 assembly to
            # mint an accepted legacy sidecar), never silently impersonated.
            raise ValueError(
                "run has a v2 manifest with correction/run identity but its accepted "
                f"5_intakeoutput record is {getattr(record, 'artifact_contract', None)!r}, "
                "not 'assembly_e4_v1' — re-run the S5 assembly under the E4 contract to "
                "mint the output-coordinate sidecars; refusing to impersonate a "
                "standalone historical IntakeOutput"
            )
        # v2 manifest present but no accepted 5_intakeoutput record yet: fall
        # through to the same-directory sidecar path below (e.g. a flat
        # `--intake-only` write that happens to share a run_dir).

    contract_path = intake_path.with_name("output_coordinate_contract.json")
    snapshot_path = intake_path.with_name("output_coordinate_snapshot.json")
    if contract_path.is_file() and snapshot_path.is_file():
        contract = OutputCoordinateContract.model_validate_json(contract_path.read_text(encoding="utf-8"))
        raw_snapshot_bytes = snapshot_path.read_bytes()
        snapshot = OutputCoordinateSnapshotV1.model_validate_json(raw_snapshot_bytes.decode("utf-8"))
        if contract.geometry_snapshot_sha256 != sha256_bytes(raw_snapshot_bytes):
            raise ValueError(
                "contract.geometry_snapshot_sha256 does not match the sibling snapshot sidecar bytes"
            )
        if isinstance(manifest, RunManifestV2):
            verified_correction = load_verified_accepted_correction(run_dir=Path(run_dir), manifest=manifest)
            if (isinstance(contract.source, AcceptedCorrectionRef)
                    and contract.source.output_sha256 != verified_correction.ref.output_sha256):
                raise ValueError(
                    "sidecar contract source does not bind the run's verified accepted correction"
                )
        else:
            verified_correction = None
        context = context_from_verified(
            raw_intake_output_bytes=raw_intake_bytes,
            verified_correction=verified_correction, coordinate_snapshot=snapshot,
        )
        return IntakeArtifactBundle(
            intake=intake, output_coordinates=contract, coordinate_snapshot=snapshot, validation_context=context,
        )
    if contract_path.is_file() or snapshot_path.is_file():
        raise ValueError(
            f"only one of output_coordinate_contract.json / output_coordinate_snapshot.json "
            f"exists next to {intake_path} — a partial sidecar pair is never valid"
        )

    if isinstance(manifest, RunManifestV2) and manifest.accepted("1_correction") is not None:
        # BO-CR4 / review-ask #3 REJECT: any correction identity forbids the
        # standalone escape hatch — v1/v2 included, not only v3.
        raise ValueError(
            "run has correction identity (a v2 manifest with an accepted 1_correction "
            "record) but no output-coordinate sidecar — refusing to treat this as a "
            "legacy standalone IntakeOutput; re-run the S5 assembly under the E4 "
            "contract to mint the sidecars"
        )

    return IntakeArtifactBundle(
        intake=intake,
        output_coordinates=legacy_contract_for_unversioned_intake(intake_output_sha256=sha256_bytes(raw_intake_bytes)),
        coordinate_snapshot=None,
        validation_context=context_from_verified(
            raw_intake_output_bytes=raw_intake_bytes, verified_correction=None, coordinate_snapshot=None,
        ),
    )
