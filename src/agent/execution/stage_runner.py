"""Stage registry + capability model + the per-stage attempt recorder (M0).

The 0–5 pipeline stages each have a *capability* that decides how a failure is
handled (contracts §0.3 failure classification):

  - ``manual``        — a human produces the artifact (0_reading today). Auto
                        routing can only ask for ``human_redraw_required``.
  - ``stochastic``    — an LLM draw (1_correction, 4_mep). A bad draw is
                        blind-resampled (same input, different sampling).
  - ``deterministic`` — code (core, 2_modelling, 3_split_pairing, 5_intakeoutput).
                        A post-condition failure is fail-closed: a code defect to
                        raise, never an upstream bounce / sample swap.

``StageRunner`` is the thin recorder that ties a produced artifact to an
append-only attempt dir + a CheckReport + the run manifest. It does NOT decide
cross-stage routing or invalidation — that belongs to the orchestrator
(invalidation.py). It deliberately does not reuse the correction-stage
``_make_correction_validator`` as a generic harness (施工 H2): single-stage draw
+ retry is a separate concern (judge/retry.py).
"""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import os
import tempfile

from pydantic import BaseModel, ConfigDict

from src.agent.execution.manifest import (
    RunManifest,
    RunManifestV2,
    StageRecord,
    StageRecordV2,
    hash_obj,
    hash_text,
    new_attempt_dir,
    next_attempt_index,
)
from src.validator.checks.schema import CheckReport


class Capability(str, Enum):
    MANUAL = "manual"
    STOCHASTIC = "stochastic"
    DETERMINISTIC = "deterministic"


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    capability: Capability
    # Stages whose accepted output this stage consumes (for input-hash recording
    # and the invalidation DAG).
    depends_on: tuple[str, ...] = ()


# The canonical 0–5 stage chain (contracts §0). `core` is the deterministic snap
# folded into 1_correction's dir on disk but is a distinct deterministic step.
STAGE_REGISTRY: dict[str, StageSpec] = {
    s.stage: s
    for s in [
        StageSpec(stage="0_reading", capability=Capability.MANUAL),
        StageSpec(stage="1_correction", capability=Capability.STOCHASTIC,
                  depends_on=("0_reading",)),
        StageSpec(stage="2_modelling", capability=Capability.DETERMINISTIC,
                  depends_on=("1_correction",)),
        StageSpec(stage="3_split_pairing", capability=Capability.DETERMINISTIC,
                  depends_on=("2_modelling",)),
        StageSpec(stage="4_mep", capability=Capability.STOCHASTIC,
                  depends_on=("3_split_pairing",)),
        StageSpec(stage="5_intakeoutput", capability=Capability.DETERMINISTIC,
                  depends_on=("3_split_pairing", "4_mep")),
    ]
}

# Stable pipeline order (manifest / DAG iteration).
STAGE_ORDER: list[str] = list(STAGE_REGISTRY.keys())


def stage_spec(stage: str) -> StageSpec:
    try:
        return STAGE_REGISTRY[stage]
    except KeyError as e:
        raise KeyError(
            f"unknown stage '{stage}'; known: {', '.join(STAGE_REGISTRY)}"
        ) from e


class RecordedAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    attempt_index: int
    attempt_dir: str
    output_hash: str
    accepted: bool
    check_passed: bool


class StageRunner:
    """Files an attempt's artifacts under append-only dirs and updates the
    manifest pointer when the attempt is accepted.

    Usage::

        runner = StageRunner(case_dir, manifest)
        att = runner.record(
            stage="2_modelling",
            stage_dir=case_dir / "2_modelling",
            output_obj=building_geometry_dict,
            report=check_report,
            input_hashes={"1_correction": corr_hash},
        )

    ``record`` writes ``output.json`` + ``checks.json`` into a fresh attempt dir.
    Acceptance follows the failure-classification policy:
      - deterministic / manual: accepted iff the report does not block;
      - stochastic: the caller decides (a retry may supersede), default accept
        iff the report does not block.

    Versioned writer (C2 B-M §5.1, CR-02): the record's wire type follows the
    manifest the runner was constructed with. A :class:`RunManifestV2` gets a
    :class:`StageRecordV2` with ``artifact_contract="base_v2"`` and real,
    recomputed ``artifact_hashes`` for the two files this method just wrote
    (all six stages — this IS the "StageRunner 通用 attempt writer" of the
    contract table). The V1 branch remains only for read-only/legacy paths;
    normal command flows always arrive here holding a V2 manifest because
    run provisioning is V2-by-default and persisted-V1 runs are refused at the
    command entrances (grandfather hard gate).
    """

    def __init__(self, case_dir: Path, manifest: "RunManifest | RunManifestV2") -> None:
        self.case_dir = Path(case_dir)
        self.manifest = manifest

    def record(
        self,
        *,
        stage: str,
        stage_dir: Path,
        output_obj,
        report: CheckReport,
        input_hashes: dict[str, str] | None = None,
        stage_version: str = "1",
        accept: bool | None = None,
    ) -> RecordedAttempt:
        """Archive a checked draw, retaining gate diagnostics if archiving fails.

        W#5: failure diagnostics are append-only under record_failures/NNN.
        They are deliberately outside attempts/: failed independent replay
        must not create a consumable B5 bundle or move an accepted pointer.
        """
        with _ArchiveFailureDiagnostics(Path(stage_dir), stage, report):
            return self._record_checked(
                stage=stage, stage_dir=stage_dir, output_obj=output_obj,
                report=report, input_hashes=input_hashes,
                stage_version=stage_version, accept=accept,
            )

    def _record_checked(
        self,
        *,
        stage: str,
        stage_dir: Path,
        output_obj,
        report: CheckReport,
        input_hashes: dict[str, str] | None = None,
        stage_version: str = "1",
        accept: bool | None = None,
    ) -> RecordedAttempt:
        spec = stage_spec(stage)
        stage_dir = Path(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)

        from src.agent.correction.finalize import FinalizeResult
        from src.agent.correction.orientation import OrientationEnrichmentResult
        from src.agent.correction.feature_state import (
            FeatureStatesArtifactV1,
            correction_stage_version,
            derive_feature_state_claims,
        )
        from src.agent.correction.config import load_core_tolerances
        from src.agent.correction.envelope_transform import annotation_basis_report
        from src.agent.correction.facade_visibility import (
            VisibilityTolerances,
            validate_materialized_facade_segments,
        )
        from src.agent.output_coordinates import AssemblyE4Write

        # E4-output-contract spec v2 §3.2bis writer contract + §3.4: two new
        # marker types distinguish an orientation-enrichment `1_correction`
        # attempt (`OrientationEnrichmentResult`, a `FinalizeResult` subclass
        # — checked FIRST so it is never mistaken for a plain B2/Vg draw) and
        # an `assembly_e4_v1` `5_intakeoutput` attempt (`AssemblyE4Write`,
        # unrelated to `FinalizeResult`) from the existing B2/Vg
        # `FinalizeResult` and the generic `base_v2` catch-all.
        is_orientation_enrichment = isinstance(output_obj, OrientationEnrichmentResult)
        is_b2_correction = isinstance(output_obj, FinalizeResult) and not is_orientation_enrichment
        is_assembly_e4 = isinstance(output_obj, AssemblyE4Write)
        is_correction_write = is_b2_correction or is_orientation_enrichment
        b5_fields = (
            output_obj.window_host_claims,
            output_obj.window_evidence_ledger,
            output_obj.verified_window_resolver_inputs,
            output_obj.prepared_candidate_identity,
        ) if is_correction_write else ()
        is_b5_correction = bool(
            is_correction_write
            and str(output_obj.geom.schema_version) == "3"
            and all(value is not None for value in b5_fields)
        )
        if (
            is_correction_write
            and str(output_obj.geom.schema_version) == "3"
            and not is_b5_correction
        ):
            raise ValueError(
                "v3 FinalizeResult must carry all four B5 trust-root fields, including zero-window output"
            )

        if is_correction_write and stage != "1_correction":
            raise ValueError(
                "FinalizeResult/OrientationEnrichmentResult may only be written by 1_correction"
            )
        if is_assembly_e4 and stage != "5_intakeoutput":
            raise ValueError("AssemblyE4Write may only be written by 5_intakeoutput")

        if is_correction_write:
            from src.agent.correction.artifact_serialization import serialize_correction_output

            out_text = serialize_correction_output(output_obj.geom).decode("utf-8")
        elif is_assembly_e4:
            out_text = output_obj.intake.model_dump_json(indent=2)
        elif isinstance(output_obj, str):
            out_text = output_obj
        else:
            out_text = _to_json(output_obj)
        output_hash = hash_text(out_text)

        # Stamp the report with the hashes it was computed against, then persist.
        report.attempt_hash = output_hash
        if report.artifact_hash is None:
            report.artifact_hash = output_hash
        checks_text = report.model_dump_json(indent=2)
        artifact_hashes = {"output": output_hash, "checks": hash_text(checks_text)}

        expected = None
        extra_artifacts: dict[str, str] = {}
        if is_correction_write:
            # Re-derive at the writer boundary; a frozen dataclass does not make
            # nested caller data trustworthy.
            from src.agent.correction.parse import CorrectionTarget
            phase_contract = "e4_orientation" if is_orientation_enrichment else "b2"
            target = CorrectionTarget(
                output_obj.geom.schema_version, type(output_obj.geom),
                report.capability_profile, phase_contract,
            )
            expected = derive_feature_state_claims(target, output_obj.geom)
            if expected != output_obj.feature_state_claims:
                raise ValueError("INVARIANT: FinalizeResult feature-state claims were altered")
            if output_obj.geom.schema_version == "3":
                # Vg rework CR1 (writer fail-closed, verdict VG-CR1): do not
                # trust that `output_obj.geom.facade_segments` is what
                # `finalize_correction_draw` actually produced — a caller
                # could bypass that function entirely, tamper a real result
                # via `model_copy`, and re-derive `feature_state_claims` from
                # the tampered geom (the claims check above only looks at
                # shape — non-empty, full floor coverage — not values, so it
                # would not catch e.g. a mutated `depth`). Independently
                # recompute every floor's segments straight from the
                # authoritative `floor_footprint` ring and reject on any
                # item-for-item mismatch before this attempt is ever
                # accepted. (Applies equally to an orientation-enrichment
                # write: `finalize_orientation_enrichment` must not have
                # touched facade_segments either.)
                tol = load_core_tolerances()
                visibility_tol = VisibilityTolerances(
                    depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
                    endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
                )
                validate_materialized_facade_segments(output_obj.geom, tolerances=visibility_tol)
            if is_b5_correction:
                import hashlib

                from src.agent.correction.artifact_serialization import serialize_feature_states
                from src.agent.correction import window_host as window_host_module
                from src.agent.correction.schema import CorrectedGeometryV3
                from src.agent.correction.window_host import build_window_hosts_artifact
                from src.agent.correction.window_sources import (
                    canonical_json_bytes,
                    serialize_window_resolver_inputs_artifact,
                    verify_window_resolver_inputs_artifact,
                )

                marker = output_obj.verified_window_resolver_inputs
                prepared = output_obj.prepared_candidate_identity
                assert marker is not None and prepared is not None
                output_bytes = out_text.encode("utf-8")
                if output_bytes != prepared.output_bytes or output_hash != prepared.output_sha256:
                    raise ValueError("candidate_output_identity_drift")

                # Persist a self-contained raw-source artifact.  Base
                # finalization carries the full marker; orientation carries
                # the already-verified artifact bytes through its narrow proof
                # view.  In both cases the writer rebuilds a fresh marker from
                # the embedded producer/manifest/readings and independently
                # re-derives ring-free direction facts.
                if hasattr(marker, "producer_draw_canonical_bytes"):
                    resolver_artifact_bytes = serialize_window_resolver_inputs_artifact(marker)
                else:
                    resolver_artifact_bytes = marker.raw_inputs_bytes
                rebuilt_marker = verify_window_resolver_inputs_artifact(
                    resolver_artifact_bytes
                )
                if rebuilt_marker.inputs != marker.inputs:
                    raise ValueError("resolver_input_identity_drift")

                fresh_geom = CorrectedGeometryV3.model_validate_json(output_bytes)
                # Deliberately module-qualified and imported inside the writer:
                # patching finalize.resolve_window_hosts cannot capture this root.
                recomputed_claims = window_host_module.recompute_window_host_claims(
                    fresh_geom,
                    verified_inputs=rebuilt_marker,
                    tolerances=tol,
                )
                if recomputed_claims != output_obj.window_host_claims:
                    raise ValueError("writer_window_host_claims_drift")

                # Replay producer -> structural/window core from embedded raw
                # sources, then bind every accepted audit row to that pre-host
                # state and to the independently recomputed final claim.
                from src.agent.correction.deterministic import (
                    AS_DRAWN_CHAIN_STAMP_VERSION,
                    DETERMINISTIC_CORE_STAMP_VERSION,
                    DeterministicCoreProofV1,
                    apply_deterministic_core,
                    core_owned_projection_v1,
                )
                from src.agent.correction.envelope import extract_authoritative_envelope
                from src.agent.correction.window_host import WindowHostResolutionAuditV1
                from src.agent.correction.window_sources import SourceIntervalV1

                # W#3 (wallhunt 2026-09-08b / dispatch 2026-09-08c S-A):
                # dispatch the replay BY LEG.  The dispatcher is the
                # candidate's own ``chain_provenance`` carrier — present ⟺
                # ``finalize_as_drawn_chain_geometry`` minted this result with
                # its frozen per-storey compilations (an explicit marker, ⛔
                # never a shape guess off the geometry).  Both legs replay
                # from the marker's embedded bytes through the REAL
                # production functions to a ``replayed`` geometry, which the
                # shared gauntlet below (core-owned projection + corrections
                # prefix + stamp) then compares field-for-field — neither leg
                # skips a check the other runs, and the legacy lock is
                # byte-untouched.  A chain candidate minted WITHOUT the
                # carrier routes to the legacy replay and reds there by
                # construction (the omission is loud, never accepted).
                chain_provenance = getattr(output_obj, "chain_provenance", None)
                if chain_provenance is not None:
                    from src.agent.correction.chain_replay import replay_as_drawn_chain

                    # The chain replay re-drives ladder → per-floor
                    # projection → snap → assembly → producer (byte-compared
                    # against the marker's embedded producer canonical bytes)
                    # → the as_drawn finalize half STOPPED before host
                    # resolution (ruling 2026-09-08f §一), all from the
                    # marker's frozen bytes.  Its result IS the replayed
                    # geometry — the core-prefix state the corrections
                    # prefix/suffix contract below is written against.
                    replayed = replay_as_drawn_chain(
                        rebuilt_marker,
                        chain_provenance,
                        target=target,
                        tol=tol,
                    ).geom
                else:
                    producer = CorrectedGeometryV3.model_validate_json(
                        rebuilt_marker.producer_draw_canonical_bytes
                    )
                    with tempfile.TemporaryDirectory(prefix="b5_writer_replay_") as replay_dir_text:
                        replay_dir = Path(replay_dir_text)
                        reading_by_id = dict(rebuilt_marker.raw_reading_artifacts)
                        for identity in rebuilt_marker.inputs.reading_artifacts:
                            (replay_dir / f"{identity.expected_output_id}.json").write_bytes(
                                reading_by_id[identity.input_id]
                            )
                        envelope = extract_authoritative_envelope(
                            replay_dir,
                            footprint=producer,
                            footprint_tolerance_m=tol.envelope_reconcile_tol_m,
                            tol=tol,
                        )
                        replayed = apply_deterministic_core(
                            producer,
                            tol,
                            authoritative_envelope=envelope,
                            capability_profile=report.capability_profile,
                            verified_window_inputs=rebuilt_marker,
                        )
                # F-22 BLOCKER-1 round 2 (2026-08-13, sol re-review): the
                # per-window audit checks below only ever compared REPLAYED
                # window fields against the candidate's own AUDIT ROWS (a
                # dict the candidate itself carries). Nothing above this line
                # ever compared `replayed`'s footprint / per-floor ring /
                # cells against `fresh_geom`'s -- so a candidate whose
                # producer, floor ring, and cells were tampered together
                # (sol's reproduction: re-signed footprint + rewritten
                # ring/cells + every derived artifact re-materialized FROM
                # the tampered footprint, all internally self-consistent)
                # sailed through unchecked and was accepted. Compare the
                # core-owned projection now, BEFORE any of the (still
                # necessary) per-window audit-row checks below, which only
                # ever verify the FINAL-owned half (host-resolved span/room/
                # facade_segment_id) that this projection deliberately
                # excludes -- see `core_owned_projection_v1`'s docstring.
                replayed_projection = core_owned_projection_v1(replayed)
                candidate_projection = core_owned_projection_v1(fresh_geom)
                # W#3 (ruling 2026-09-08f §二): compare the projections as
                # CANONICAL JSON, never as raw python containers.  The two
                # sides are NOT same-sourced: the candidate side is reloaded
                # from archived bytes (every container a list) while the
                # replay side is in-memory (a pydantic ``model_dump`` keeps
                # tuple-typed fields as tuples) — a raw ``!=`` reported 62
                # phantom container-type diffs on value-identical rows
                # (``source_ids`` / ``tolerance_names``).  Canonical-JSON-ing
                # BOTH sides is the same-ruler fix (both pass through the
                # same JSON encoding before the zero-threshold compare),
                # ⛔ NOT a "list == tuple" tolerance branch: a real drift —
                # different elements, dict-vs-list, changed floats — still
                # reds on the canonical bytes.
                if any(
                    canonical_json_bytes(replayed_projection.get(key))
                    != canonical_json_bytes(candidate_projection.get(key))
                    for key in (replayed_projection.keys() | candidate_projection.keys()) - {"corrections"}
                ):
                    raise ValueError("writer_core_projection_drift")
                # `corrections` is append-only downstream of the core: window
                # host resolution APPENDS `window_host_resolution` rows to it
                # but never rewrites the core's own entries (window_host.py
                # `apply_window_host_resolutions`) -- so the core-replayed
                # list must be an exact PREFIX of the candidate's, not a full
                # match. The suffix's shape (exactly one row per resolved
                # window, all `window_host_resolution`) is checked below once
                # `audit_rows` exists.
                _replayed_corrections = replayed_projection["corrections"]
                # Same canonical-JSON ruler as above (ruling 2026-09-08f §二):
                # the candidate's reload-from-bytes lists vs the replay's
                # in-memory tuples must compare as canonical JSON, and the
                # slice-vs-full check keeps its prefix semantics — a shorter
                # candidate list canonicalizes to different bytes.
                if (
                    canonical_json_bytes(
                        candidate_projection["corrections"][: len(_replayed_corrections)]
                    )
                    != canonical_json_bytes(_replayed_corrections)
                ):
                    raise ValueError("writer_core_projection_drift")
                replayed_stamp_version = getattr(
                    getattr(replayed, "deterministic_core_stamp", None), "version", None
                )
                candidate_stamp_version = getattr(
                    getattr(fresh_geom, "deterministic_core_stamp", None), "version", None
                )
                # W#3: each leg pins BOTH sides to its OWN kernel version
                # (exact equality, ⛔ no cross-leg mixing) — the as_drawn leg
                # stamps under AS_DRAWN_CHAIN_STAMP_VERSION inside its
                # finalize, the legacy leg under DETERMINISTIC_CORE_STAMP_
                # VERSION inside apply_deterministic_core.
                expected_stamp_version = (
                    AS_DRAWN_CHAIN_STAMP_VERSION
                    if chain_provenance is not None
                    else DETERMINISTIC_CORE_STAMP_VERSION
                )
                if (
                    replayed_stamp_version != expected_stamp_version
                    or candidate_stamp_version != expected_stamp_version
                ):
                    raise ValueError("writer_core_projection_drift")
                audit_core = {
                    key: output_obj.audit_payload.get(key)
                    for key in ("corrections", "conflicts", "unsupported")
                }
                geom_core = {
                    "corrections": fresh_geom.corrections,
                    "conflicts": fresh_geom.conflicts,
                    "unsupported": fresh_geom.unsupported,
                }
                if json.loads(_to_json(audit_core)) != json.loads(_to_json(geom_core)):
                    raise ValueError("writer_audit_output_drift")
                audit_rows = {
                    row.window_id: row
                    for row in (
                        WindowHostResolutionAuditV1.model_validate_json(
                            json.dumps(item, separators=(",", ":"), ensure_ascii=False)
                        )
                        for item in fresh_geom.corrections
                        if isinstance(item, dict)
                        and item.get("kind") == "window_host_resolution"
                    )
                }
                _corrections_suffix = candidate_projection["corrections"][len(_replayed_corrections):]
                if len(_corrections_suffix) != len(audit_rows) or any(
                    not isinstance(item, dict) or item.get("kind") != "window_host_resolution"
                    for item in _corrections_suffix
                ):
                    raise ValueError("writer_core_projection_drift")
                replay_windows = {window.id: window for window in replayed.windows}
                claim_rows = {row.window_id: row for row in recomputed_claims.resolutions}
                if set(audit_rows) != set(replay_windows) or set(audit_rows) != set(claim_rows):
                    raise ValueError("writer_window_audit_totality_drift")
                for window_id in sorted(audit_rows):
                    audit = audit_rows[window_id]
                    original = replay_windows[window_id]
                    claim = claim_rows[window_id]
                    expected_original_span = SourceIntervalV1(
                        lo=float(original.span[0]), hi=float(original.span[1])
                    )
                    if (
                        audit.floor_id != original.floor_id
                        or audit.original_room_id != original.room
                        or audit.original_span != expected_original_span
                        or audit.resolved_room_id != claim.room_id
                        or audit.resolved_facade_segment_id != claim.facade_segment_id
                        or audit.resolved_span != claim.clamped_span
                        or audit.source_ids != claim.source_locators
                        or audit.branch != claim.branch
                        or audit.resolution_sha256 != claim.resolution_sha256
                    ):
                        raise ValueError("writer_window_audit_replay_drift")

                # F-22 BLOCKER-1 round 2: the loop above only verified the
                # AUDIT ROW dict's `resolved_*` fields against the
                # independently recomputed claim. Nothing verified that the
                # window's OWN persisted `span`/`room`/`facade_segment_id`
                # (the fields a downstream consumer actually reads) equal
                # what that audit row / claim says was resolved -- the same
                # "self-consistent but untethered from the verified value"
                # shape as the footprint/ring/cells gap fixed above, applied
                # to windows instead. Close it the same way: compare the
                # candidate's own window fields to the independently
                # recomputed claim, not to another field the candidate also
                # controls.
                fresh_windows_by_id = {window.id: window for window in fresh_geom.windows}
                for window_id in sorted(claim_rows):
                    fresh_window = fresh_windows_by_id.get(window_id)
                    claim = claim_rows[window_id]
                    if fresh_window is None or (
                        list(fresh_window.span) != [claim.clamped_span.lo, claim.clamped_span.hi]
                        or fresh_window.room != claim.room_id
                        or fresh_window.facade_segment_id != claim.facade_segment_id
                    ):
                        raise ValueError("writer_window_final_fields_drift")

                states_for_identity = FeatureStatesArtifactV1(
                    output_sha256=output_hash,
                    claims=expected,
                )
                feature_bytes = serialize_feature_states(states_for_identity)
                feature_sha = hashlib.sha256(feature_bytes).hexdigest()
                if (
                    feature_bytes != prepared.feature_states_bytes
                    or feature_sha != prepared.feature_states_sha256
                ):
                    raise ValueError("candidate_feature_states_identity_drift")
                recomputed_evidence = window_host_module.derive_window_evidence_ledger(
                    fresh_geom,
                    host_claims=recomputed_claims,
                    verified_inputs=rebuilt_marker,
                    candidate_identity=prepared,
                    tolerances=tol,
                )
                if recomputed_evidence != output_obj.window_evidence_ledger:
                    raise ValueError("writer_window_evidence_drift")
                host_artifact = build_window_hosts_artifact(
                    output_sha256=output_hash,
                    claims=recomputed_claims,
                    evidence=recomputed_evidence,
                )
                extra_artifacts["window_resolver_inputs.json"] = resolver_artifact_bytes.decode("utf-8")
                extra_artifacts["window_hosts.json"] = host_artifact.model_dump_json(indent=2)
                # F-22 BLOCKER-1 round 2: signed ONLY here, after every
                # replay/projection/audit/claim comparison above has
                # succeeded -- this is the "由落库方在重放成功之后签发" proof the
                # 2026-08-13 dispatch's 修法步骤 2 requires. `input_hash` binds
                # it to the exact raw producer bytes that were replayed.
                #
                # `core_projection_hash` is hashed from `candidate_projection`
                # (the FINAL, accepted geometry's own projection) -- NOT from
                # `replayed_projection` (the pre-host-resolution replay).
                # These are NOT the same bytes whenever the attempt has any
                # window at all: host resolution APPENDS `window_host_
                # resolution` rows to `corrections` after the core runs (see
                # `core_owned_projection_v1`'s docstring on the prefix/suffix
                # split), so `candidate_projection["corrections"]` is
                # genuinely longer than `replayed_projection["corrections"]`
                # for any real B5 write with windows. A judge-side reader can
                # only ever recompute `core_owned_projection_v1` on the FINAL
                # geometry it is scoring (it never has access to the pre-host
                # replay) -- so the proof must commit to THAT projection, or
                # verification would fail on every real write with windows.
                # This does not weaken what was proved above: by this line,
                # `candidate_projection`'s footprint/floors/windows(floor_id,z)/
                # conflicts/unsupported have already been asserted equal to
                # `replayed_projection`'s, and its `corrections` suffix has
                # already been asserted to be exactly the verified window-
                # host-resolution rows -- so committing to `candidate_projection`
                # here binds the proof to a value already proven correct, not
                # to an unverified one.
                core_proof = (
                    # W#3: the proof names the kernel that actually replayed —
                    # and binds to the bytes that replay consumed (the frozen
                    # per-storey compilations on the chain leg, the producer
                    # canonical bytes on the legacy leg).
                    DeterministicCoreProofV1(
                        core_version=AS_DRAWN_CHAIN_STAMP_VERSION,
                        input_hash=chain_provenance.replay_input_hash,
                        core_projection_hash=hash_obj(candidate_projection),
                    )
                    if chain_provenance is not None
                    else DeterministicCoreProofV1(
                        core_version=DETERMINISTIC_CORE_STAMP_VERSION,
                        input_hash=hashlib.sha256(
                            rebuilt_marker.producer_draw_canonical_bytes
                        ).hexdigest(),
                        core_projection_hash=hash_obj(candidate_projection),
                    )
                )
                extra_artifacts["deterministic_core_proof.json"] = core_proof.model_dump_json(indent=2)
                if chain_provenance is not None:
                    extra_artifacts["chain_provenance.json"] = (
                        chain_provenance.model_dump_json(indent=2)
                    )
            audit_text = _to_json(output_obj.audit_payload)
            states = FeatureStatesArtifactV1(output_sha256=output_hash, claims=expected)
            states_text = states.model_dump_json(indent=2)
            artifact_hashes.update({"audit": hash_text(audit_text), "feature_states": hash_text(states_text)})
            if is_b5_correction:
                artifact_hashes.update({
                    "window_resolver_inputs": hash_text(extra_artifacts["window_resolver_inputs.json"]),
                    "window_hosts": hash_text(extra_artifacts["window_hosts.json"]),
                    "deterministic_core_proof": hash_text(extra_artifacts["deterministic_core_proof.json"]),
                })
                if "chain_provenance.json" in extra_artifacts:
                    artifact_hashes["chain_provenance"] = hash_text(
                        extra_artifacts["chain_provenance.json"]
                    )
        elif is_assembly_e4:
            audit_text = output_obj.audit.model_dump_json(indent=2)
            contract_text = output_obj.contract.model_dump_json(indent=2)
            snapshot_text = output_obj.snapshot.model_dump_json(indent=2)
            artifact_hashes.update({
                "audit": hash_text(audit_text),
                "output_coordinate_contract": hash_text(contract_text),
                "output_coordinate_snapshot": hash_text(snapshot_text),
            })

        # No attempt directory exists until every B5 independent recomputation
        # above has succeeded.  The accepted pointer is moved only after the
        # complete bundle is written and re-read below.
        final_attempt_dir = None
        if is_b5_correction:
            attempts_root = stage_dir / "attempts"
            attempts_root.mkdir(parents=True, exist_ok=True)
            attempt_index = next_attempt_index(stage_dir)
            final_attempt_dir = attempts_root / f"{attempt_index:03d}"
            adir = Path(tempfile.mkdtemp(
                prefix=f".{attempt_index:03d}.",
                dir=attempts_root,
            ))
        else:
            adir = new_attempt_dir(stage_dir)
        files = {"output.json": out_text, "checks.json": checks_text}
        if is_correction_write:
            files.update({"audit.json": audit_text, "feature_states.json": states_text})
            files.update(extra_artifacts)
        elif is_assembly_e4:
            files.update({
                "audit.json": audit_text,
                "output_coordinate_contract.json": contract_text,
                "output_coordinate_snapshot.json": snapshot_text,
            })
        for filename, text in files.items():
            # Hashes above bind UTF-8 bytes; Windows newline translation must
            # not change the published bytes after those hashes are computed.
            (adir / filename).write_text(text, encoding="utf-8", newline="\n")

        if is_b5_correction:
            from src.agent.correction.feature_state import FeatureStatesArtifactV1
            from src.agent.correction.schema import CorrectedGeometryV3
            from src.agent.correction.window_host import WindowHostsArtifactV1
            from src.agent.correction.window_sources import WindowResolverInputsArtifactV1
            from src.agent.correction.deterministic import DeterministicCoreProofV1

            CorrectedGeometryV3.model_validate_json((adir / "output.json").read_bytes())
            CheckReport.model_validate_json((adir / "checks.json").read_bytes())
            audit_reload = json.loads((adir / "audit.json").read_text(encoding="utf-8"))
            if not isinstance(audit_reload, dict):
                raise ValueError("B5 audit artifact must be an object")
            FeatureStatesArtifactV1.model_validate_json((adir / "feature_states.json").read_bytes())
            WindowResolverInputsArtifactV1.model_validate_json(
                (adir / "window_resolver_inputs.json").read_bytes()
            )
            WindowHostsArtifactV1.model_validate_json((adir / "window_hosts.json").read_bytes())
            DeterministicCoreProofV1.model_validate_json(
                (adir / "deterministic_core_proof.json").read_bytes()
            )
            if "chain_provenance.json" in files:
                from src.agent.correction.chain_provenance import (
                    AsDrawnChainProvenanceV1,
                )

                AsDrawnChainProvenanceV1.model_validate_json(
                    (adir / "chain_provenance.json").read_bytes()
                )
            assert final_attempt_dir is not None
            os.replace(adir, final_attempt_dir)
            adir = final_attempt_dir

        check_passed = report.passed
        do_accept = check_passed if accept is None else accept
        if is_assembly_e4 and output_obj.input_hashes:
            # E4 (spec §3.5): the S5 record's identity bindings travel with
            # the write payload so orchestration layers that call record()
            # without explicit input hashes still bind the accepted
            # correction; explicit caller-supplied hashes win per key.
            merged = dict(output_obj.input_hashes)
            merged.update(input_hashes or {})
            input_hashes = merged
        rec = RecordedAttempt(
            stage=stage,
            attempt_index=int(adir.name),
            attempt_dir=str(adir),
            output_hash=output_hash,
            accepted=do_accept,
            check_passed=check_passed,
        )
        if do_accept:
            if is_correction_write:
                # Vg rework CR2 (§9.2 central release-map policy): re-derive
                # the wire's stage_version from the same `expected` claims
                # re-derived above for the tamper check — never trust a
                # caller-supplied value, and never hardcode ANY correction
                # stage_version literal here (not 2, not 3, not 4). The
                # release map in feature_state.py is now a single, explicit,
                # fail-closed table over the FULL claims state (schema +
                # helper_versions + all four feature states) — an
                # unregistered combination raises INVARIANT unconditionally.
                stage_version = correction_stage_version(expected)
            common = dict(
                stage=stage,
                accepted_attempt=rec.attempt_index,
                output_hash=output_hash,
                input_hashes=input_hashes or {},
                stage_version=stage_version,
                check_version=report.results[0].check_version
                if report.results
                else "1",
                capability=spec.capability.value,
                check_passed=check_passed,
            )
            if isinstance(self.manifest, RunManifestV2):
                if is_orientation_enrichment and is_b5_correction:
                    artifact_contract = "correction_b5_orientation_v1"
                elif is_orientation_enrichment:
                    artifact_contract = "correction_e4_orientation_v1"
                elif is_b5_correction:
                    artifact_contract = "correction_b5_v1"
                elif is_b2_correction:
                    artifact_contract = "correction_b2_v1"
                elif is_assembly_e4:
                    artifact_contract = "assembly_e4_v1"
                else:
                    artifact_contract = "base_v2"
                    if stage == "1_correction":
                        # F-3b: a 1_correction record may only be accepted under
                        # a correction contract (FinalizeResult /
                        # OrientationEnrichmentResult, all handled above). A raw
                        # v3 CorrectedGeometry reaching this catch-all is an
                        # unfinalized draft (the F-3 root cause: an advisory-debt
                        # early-exit returned raw geom instead of a FinalizeResult);
                        # its ``schema_version="3"`` then collides two stages later
                        # in ``load_verified_accepted_correction``
                        # (output_coordinates.py), which fail-closes with the
                        # opaque "feature-state sidecar is mandatory for v3". Fail
                        # closed HERE, at the write point, with a named error.
                        _draft_sv = str(getattr(output_obj, "schema_version", "") or "")
                        if _draft_sv == "3":
                            raise ValueError(
                                "an unfinalized v3 correction draft (raw "
                                "CorrectedGeometry, not a FinalizeResult) may not "
                                "be accepted under a 'base_v2' record — the "
                                "feature-state sidecar is mandatory for v3 (F-3b)"
                            )
                self.manifest.accept(
                    StageRecordV2(
                        **common,
                        artifact_contract=artifact_contract,
                        artifact_hashes=artifact_hashes,
                    )
                )
                if is_correction_write:
                    # Convenience copies are promoted only after gate acceptance.
                    (stage_dir / "correction_geometry_snapped.json").write_text(out_text, encoding="utf-8", newline="\n")
                    (stage_dir / "corrections.json").write_text(_to_json(output_obj.audit_payload), encoding="utf-8", newline="\n")
                    # 2026-08-12 (摊 C): pure "annotation basis" observation
                    # sidecar -- convenience copy only, like the two writes
                    # above; deliberately NOT added to `artifact_hashes` /
                    # the manifest trust root (it is advisory-only and must
                    # never gate acceptance or participate in the B5 replay
                    # cross-check above). Empty tuple (legacy v1/v2, or a v3
                    # draw whose envelope never accepted an axis) writes
                    # nothing -- consistent with "no intent" staying silent
                    # only where there truly is nothing to report.
                    annotation_basis = getattr(output_obj, "annotation_basis", ())
                    if annotation_basis:
                        (stage_dir / "annotation_basis.json").write_text(
                            _to_json(annotation_basis_report(annotation_basis, load_core_tolerances())),
                            encoding="utf-8", newline="\n",
                        )
                elif is_assembly_e4:
                    (stage_dir / "output_coordinate_contract.json").write_text(contract_text, encoding="utf-8", newline="\n")
                    (stage_dir / "output_coordinate_snapshot.json").write_text(snapshot_text, encoding="utf-8", newline="\n")
            else:
                self.manifest.accept(StageRecord(**common))
        return rec


class _ArchiveFailureDiagnostics:
    """Observe exceptional exit; never catch or suppress a writer refusal."""

    def __init__(self, stage_dir: Path, stage: str, report: CheckReport):
        self.stage_dir, self.stage, self.report = stage_dir, stage, report

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            try:
                _record_archive_failure(self.stage_dir, self.stage, self.report, exc)
            except (OSError, ValueError, TypeError) as diagnostic_exc:
                exc.add_note(f"could not persist gate report after archive failure: {diagnostic_exc}")
        return False


def _record_archive_failure(stage_dir: Path, stage: str, report: CheckReport, exc: BaseException) -> None:
    root = stage_dir / "record_failures"
    root.mkdir(parents=True, exist_ok=True)
    indices = [int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()]
    directory = root / f"{max(indices, default=0) + 1:03d}"
    directory.mkdir(exist_ok=False)
    checks = report.model_dump_json(indent=2)
    (directory / "checks.json").write_text(checks, encoding="utf-8", newline="\n")
    (directory / "failure.json").write_text(json.dumps({
        "stage": stage,
        "accepted": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "checks_sha256": hash_text(checks),
        "candidate_output_sha256": report.attempt_hash,
        "note": "gate diagnostics only; candidate was not successfully archived by this call",
    }, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def _to_json(obj) -> str:
    import json

    from src.agent.correction.finalize import FinalizeResult
    if isinstance(obj, FinalizeResult):
        raise TypeError("FinalizeResult requires the explicit correction writer")
    if hasattr(obj, "model_dump_json"):  # pydantic
        return obj.model_dump_json(indent=2)
    return json.dumps(obj, indent=2, ensure_ascii=False)


# Re-export so callers can hash inputs without importing manifest directly.
__all__ = [
    "Capability",
    "StageSpec",
    "STAGE_REGISTRY",
    "STAGE_ORDER",
    "stage_spec",
    "StageRunner",
    "RecordedAttempt",
    "hash_obj",
]
