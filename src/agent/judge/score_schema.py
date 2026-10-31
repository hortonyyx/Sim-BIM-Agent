"""B4b score-contract wire types and canonical identity helpers.

This module is judge-only.  It intentionally contains the Phase A contract and
no geometry scorer so a v3 input can fail closed before legacy scoring is
considered.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, StringConstraints, ValidationError, model_validator

from src.agent.judge.gt import load_gt_file
from src.agent.judge.gt_schema import GtDocument, GroundTruthV3
from src.agent.execution.view_manifest import ViewManifest
# Reading-product contract constants are owned by the reading package
# (src.agent.reading.contract) so non-judge code can recognize the v2 envelope
# without importing judge score code.  Re-exported here for the judge callers
# and validators that historically read them from this module.
from src.agent.reading.contract import (
    READING_CONTRACT_DETECTOR_VERSION,
    READING_PRODUCT_CONTRACT,
)

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(strict=True, ge=0.0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0.0, allow_inf_nan=False)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Hex64 = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[StrictStr, StringConstraints(min_length=1, max_length=256)]
ClaimName = Literal["existence", "host", "along", "width", "sill", "head", "appearance"]
CardinalFamily = Literal["North", "South", "East", "West"]

# NIT-F25 (2026-08-13): the `SCORER_SCHEMA` alias that used to live here was
# a pure duplicate of this constant (both "8") with zero production readers
# of its own (only `SCORE_SIDECAR_SCHEMA` below is ever imported outside a
# test) -- removed rather than kept, per the dispatch's "后者统一使用已有
# `SCORE_SIDECAR_SCHEMA`". Pure rename/removal, no behavior change: this
# typed-contract label was already independent of, and untouched by, the
# `scripts/tool_scripts/run_stage.py` legacy cache label (now
# `LEGACY_SCORE_CACHE_SCHEMA`) it used to share a name with.
SCORE_SIDECAR_SCHEMA = "8"
JUDGE_SCORE_CONFIG_SCHEMA = "1"
JUDGE_SCORE_BINDINGS_SCHEMA = "1"
JUDGE_COMPLETENESS_OVERLAY_SCHEMA = "1"
SEGMENT_SCORER_HELPER_VERSION = "b4b_segment_score_v3_ic1"
READING_OPENING_MATCHER_HELPER_VERSION = "reading_opening_global_assignment_v1"
CORRECTION_OPENING_MATCHER_HELPER_VERSION = "correction_opening_global_assignment_v5"
GT_TO_VA_ADAPTER_VERSION = "b4b_gt_to_va_v1"
DENOMINATOR_HELPER_VERSION = "b4b_denominator_v1"
GRADE_RENDERER_VERSION = "b4b_grade_png_v1"
CLAIM_ORDER: tuple[ClaimName, ...] = ("existence", "host", "along", "width", "sill", "head", "appearance")
STABLE_ERROR_CODES = frozenset({
    "score_gt_identity_invalid", "score_product_identity_invalid", "score_view_manifest_invalid",
    "score_view_binding_invalid", "score_direction_unresolved", "score_completeness_input_invalid",
    "score_visibility_adapter_mismatch", "score_product_segment_unresolved", "score_claim_applicability_invalid",
    "score_match_ambiguous", "score_denominator_nonconserving", "score_sidecar_invalid",
    "score_unsupported_combination", "score_atomic_write_failed",
    # R-5 identity-layer cause codes (which kind of identity failure), distinct
    # from the side codes above.  An identity MERGE failure (non-finite value,
    # guard-band gap, chain bridge, merge-induced collapse, contract mismatch,
    # or a product segment eligible for >=2 answer support lines) raises one of
    # these neutral codes; the side (gt/product) stays in context["side"].  A
    # PAIRING failure (a real topology break such as a 1e-9 seam the production
    # validator would also reject) still raises the side code
    # score_gt_identity_invalid / score_product_identity_invalid, so A2's "1e-9
    # gap stays red, code verbatim" holds.  Keeping the two failure classes on
    # different codes is exactly R-5's "stable top-level cause code, not a
    # context['reason'] string".
    "score_identity_non_finite", "score_identity_guard_band_ambiguity", "score_identity_chain_bridge",
    "score_identity_merge_collapse", "score_identity_contract_mismatch", "score_identity_support_ambiguous",
})
GATE_IDS = frozenset({
    "scoring.input_identity", "scoring.capability", "scoring.view_bindings", "scoring.completeness",
    "scoring.applicability", "scoring.matching", "scoring.denominator_totality", "scoring.sidecar_identity",
    "scoring.render_totality",
})


class ScoreContractError(ValueError):
    """The sole public B4b boundary error; its fields are safe for a sidecar."""

    def __init__(self, code: str, gate_id: str, *, cause_code: str | None = None, context: dict | None = None):
        if code not in STABLE_ERROR_CODES or gate_id not in GATE_IDS:
            raise ValueError("ScoreContractError requires a frozen code and gate id")
        self.code, self.gate_id, self.cause_code = code, gate_id, cause_code
        self.context = context or {}
        super().__init__(f"{code} at {gate_id}")


class StrictWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def canonical_json(value: object) -> bytes:
    """The frozen UTF-8, sorted-key compact JSON byte preimage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def hash_model_without(model: BaseModel, field: str) -> str:
    return canonical_sha256(model.model_dump(mode="json", exclude={field}))


class JudgeScoreConfigV1(StrictWire):
    schema_version: Literal["1"]
    plan_axis_alignment_tol_m: PositiveFloat
    plan_position_tol_m: PositiveFloat
    plan_extent_tol_m: PositiveFloat
    claim_complete_epsilon_m: PositiveFloat
    opening_match_center_tol_m: PositiveFloat
    opening_assignment_tie_epsilon: PositiveFloat
    along_claim_tol_m: PositiveFloat
    width_claim_tol_m: PositiveFloat
    sill_claim_tol_m: PositiveFloat
    head_claim_tol_m: PositiveFloat
    floor_line_tol_m: PositiveFloat

    @model_validator(mode="after")
    def _relationships(self):
        claim_tols = (self.along_claim_tol_m, self.width_claim_tol_m, self.sill_claim_tol_m, self.head_claim_tol_m)
        if self.claim_complete_epsilon_m > min(claim_tols):
            raise ValueError("claim_complete_epsilon_m exceeds a claim tolerance")
        all_tols = (self.plan_axis_alignment_tol_m, self.plan_position_tol_m, self.plan_extent_tol_m,
                    self.opening_match_center_tol_m, *claim_tols, self.floor_line_tol_m)
        if self.opening_assignment_tie_epsilon >= min(all_tols):
            raise ValueError("opening_assignment_tie_epsilon must be below every geometric tolerance")
        return self


class PlanScoreViewBindingV1(StrictWire):
    kind: Literal["plan"]
    input_id: StableId
    floor_id: StableId
    gt_source_view_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def _sources_nonempty(self):
        if not self.gt_source_view_ids:
            raise ValueError("gt_source_view_ids must be non-empty")
        return self


class ElevationScoreViewBindingV1(StrictWire):
    kind: Literal["elevation"]
    input_id: StableId
    floor_ids: tuple[StableId, ...]
    facade_family: CardinalFamily
    gt_source_view_ids: tuple[StableId, ...]
    resolved_building_direction: CardinalFamily
    resolution_source: Literal["manifest_building_axis", "resolved_direction_sidecar"]
    orientation_output_hash: Hex64 | None
    adapter_version: StableId | None
    source_footprint_fingerprint: Hex64
    world_axis: Literal["x", "y"]
    sign: Literal[-1, 1]
    along_origin: FiniteFloat
    mirrored: StrictBool
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"]
    frame_transform_sha256: Hex64

    @model_validator(mode="after")
    def _frame_source_contract(self):
        if not self.floor_ids or not self.gt_source_view_ids:
            raise ValueError("floor_ids and gt_source_view_ids must be non-empty")
        has_orientation = self.orientation_output_hash is not None and self.adapter_version is not None
        if (self.resolution_source == "manifest_building_axis" and has_orientation) or (
            self.resolution_source == "resolved_direction_sidecar" and not has_orientation
        ):
            raise ValueError("resolution source/orientation fields disagree")
        if len(set(self.floor_ids)) != len(self.floor_ids):
            raise ValueError("floor_ids must be unique")
        return self


ScoreViewBindingV1 = Annotated[PlanScoreViewBindingV1 | ElevationScoreViewBindingV1, Field(discriminator="kind")]


class JudgeScoreViewBindingsV1(StrictWire):
    schema_version: Literal["1"]
    case_id: StableId
    gt_content_sha256: Hex64
    case_metadata_sha256: Hex64
    base_view_manifest_sha256: Hex64
    bindings: tuple[ScoreViewBindingV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical(self):
        if not self.bindings or len({b.input_id for b in self.bindings}) != len(self.bindings):
            raise ValueError("bindings must be non-empty and have unique input_id")
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("content_sha256 does not match canonical score bindings payload")
        return self


class PlanFullFloorCoverageV1(StrictWire):
    kind: Literal["full_floor"]
    floor_id: StableId


class ElevationFullFacadeCoverageV1(StrictWire):
    kind: Literal["full_facade"]
    floor_ids: tuple[StableId, ...]
    facade_family: CardinalFamily

    @model_validator(mode="after")
    def _floors(self):
        if not self.floor_ids or len(set(self.floor_ids)) != len(self.floor_ids):
            raise ValueError("floor_ids must be non-empty and unique")
        return self


CompletenessCoverageV1 = Annotated[PlanFullFloorCoverageV1 | ElevationFullFacadeCoverageV1, Field(discriminator="kind")]


class UserDeclarationBodyV1(StrictWire):
    input_id: StableId
    assertion_id: StableId
    negative_claims: tuple[ClaimName, ...]
    coverage: CompletenessCoverageV1
    asserted_by: StableId
    assertion_revision: PositiveInt


class DatasetDeclarationBodyV1(StrictWire):
    input_id: StableId
    assertion_id: StableId
    negative_claims: tuple[ClaimName, ...]
    coverage: CompletenessCoverageV1
    dataset_id: StableId
    dataset_version: StableId
    contract_id: StableId


class UserCompletenessDeclarationV1(StrictWire):
    source: Literal["user"]
    body: UserDeclarationBodyV1
    body_sha256: Hex64

    @model_validator(mode="after")
    def _body_hash(self):
        if self.body_sha256 != canonical_sha256(self.body.model_dump(mode="json")):
            raise ValueError("body_sha256 does not match user declaration body")
        return self


class DatasetCompletenessDeclarationV1(StrictWire):
    source: Literal["dataset_ref"]
    body: DatasetDeclarationBodyV1
    body_sha256: Hex64

    @model_validator(mode="after")
    def _body_hash(self):
        if self.body_sha256 != canonical_sha256(self.body.model_dump(mode="json")):
            raise ValueError("body_sha256 does not match dataset declaration body")
        return self


CompletenessDeclarationV1 = Annotated[UserCompletenessDeclarationV1 | DatasetCompletenessDeclarationV1, Field(discriminator="source")]


class JudgeCompletenessOverlayV1(StrictWire):
    schema_version: Literal["1"]
    case_id: StableId
    gt_content_sha256: Hex64
    base_view_manifest_sha256: Hex64
    declarations: tuple[CompletenessDeclarationV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical(self):
        if len({d.body.input_id for d in self.declarations}) != len(self.declarations):
            raise ValueError("at most one completeness declaration per input")
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("content_sha256 does not match canonical completeness payload")
        return self


class GtIdentityV8(StrictWire):
    path_id: StableId
    file_sha256: Hex64
    content_sha256: Hex64
    schema_version: Literal[2, 3]
    profile: StableId | None
    coordinate_frame: StableId | None
    verification_status: Literal["candidate", "human_verified"] | None
    loader_helper_version: StableId


class ProductIdentityV8(StrictWire):
    stage: Literal["reading", "correction"]
    attempt: NonNegativeInt
    output_sha256: Hex64
    output_schema: StableId
    accepted: StrictBool
    accepted_stage_record_sha256: Hex64 | None
    source: StableId
    artifact_contract: StableId | None = None

    @model_validator(mode="after")
    def _accepted_record(self):
        if self.accepted != (self.accepted_stage_record_sha256 is not None):
            raise ValueError("accepted stage record hash must match accepted state")
        return self


class ManifestIdentityV8(StrictWire):
    base_view_manifest_sha256: Hex64
    effective_view_manifest_sha256: Hex64
    case_metadata_sha256: Hex64
    completeness_ruleset: StableId
    completeness_overlay_sha256: Hex64 | None
    score_view_bindings_sha256: Hex64 | None


class HelperIdentityV8(StrictWire):
    scorer_schema: Literal["8"]
    segment_scorer: Literal["b4b_segment_score_v3_ic1"]
    gt_to_va_adapter: Literal["b4b_gt_to_va_v1"]
    denominator_helper: Literal["b4b_denominator_v1"]
    grade_renderer: Literal["b4b_grade_png_v1"]
    va_helper: StableId
    vg_helper: StableId
    claims_contract: StableId


class CapabilityDecisionV8(StrictWire):
    path: Literal["legacy_v2", "c2_v3", "not_applicable", "rejected"]
    capability_key: tuple[StableId, ...]
    reason: StableId | None
    gate_id: StableId


class C2ToleranceIdentityV8(StrictWire):
    profile_kind: Literal["judge_score_config_v1"]
    values: JudgeScoreConfigV1
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if self.content_sha256 != canonical_sha256(self.values.model_dump(mode="json")):
            raise ValueError("C2 tolerance content_sha256 mismatch")
        return self


class ScoreIdentityV8(StrictWire):
    gt: GtIdentityV8
    product: ProductIdentityV8
    manifest: ManifestIdentityV8
    helpers: HelperIdentityV8
    capability: CapabilityDecisionV8
    tolerances: C2ToleranceIdentityV8
    reference_applicability_sha256: Hex64 | None
    product_applicability_sha256: Hex64 | None
    absence_applicability_sha256: Hex64 | None


# Phase-B per-claim wire.  Kept here with the other public judge wires so a
# later sidecar assembler cannot silently invent a second representation.
class IntervalV1(StrictWire):
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if self.lo >= self.hi:
            raise ValueError("interval requires lo < hi")
        return self


class ClaimApplicabilityRefV8(StrictWire):
    ledger_content_sha256: Hex64
    opening_id: StableId
    claim: ClaimName
    target_world_interval: IntervalV1
    status: Literal["applicable", "partially_applicable", "not_applicable"]
    reason: Literal["full_observable_coverage", "existence_observable_fragment", "partial_observable_coverage", "unobserved", "outside_reading_exam_scope"]
    applicable_intervals: tuple[IntervalV1, ...]
    unobserved_intervals: tuple[IntervalV1, ...]
    considered_source_view_ids: tuple[StableId, ...]
    supporting_source_view_ids: tuple[StableId, ...]
    facade_segment_ids: tuple[StableId, ...]


class ClaimValueErrorV8(StrictWire):
    metric: Literal["binary", "masked_interval_endpoint", "masked_interval_length", "scalar_absolute"]
    value: NonNegativeFloat | None
    tolerance: NonNegativeFloat | None


class ClaimOutcomeSliceV8(StrictWire):
    slice_id: StableId
    applicable_intervals: tuple[IntervalV1, ...]
    units: Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
    result: Literal["complete", "within_tolerance", "miss", "conflict"]
    error: ClaimValueErrorV8
    evidence_source_ids: tuple[StableId, ...]


class KnowledgeRefAuditV8(StrictWire):
    dataset_id: StableId
    dataset_version: StableId
    entry_id: StableId
    candidate_id: StableId
    content_sha256: Hex64


class ClaimProvenanceAuditV8(StrictWire):
    claim: ClaimName
    provenance: Literal["observed", "derived", "assumed"]
    source_ids: tuple[StableId, ...]
    method: StableId | None
    knowledge_ref: KnowledgeRefAuditV8 | None


class ClaimScoreRowV8(StrictWire):
    target_id: StableId
    target_kind: Literal["window", "door"]
    claim: ClaimName
    applicability: ClaimApplicabilityRefV8
    eligible_units: Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
    result: Literal["complete", "within_tolerance", "miss", "conflict", "not_applicable"]
    na_reason: StableId | None
    outcome_slices: tuple[ClaimOutcomeSliceV8, ...]
    matched_observation_ids: tuple[StableId, ...]
    evidence_source_ids: tuple[StableId, ...]
    product_provenance: tuple[ClaimProvenanceAuditV8, ...]


class ClaimSummaryV8(StrictWire):
    claim: ClaimName
    target_count: NonNegativeInt
    eligible_target_count: NonNegativeInt
    partial_target_count: NonNegativeInt
    denominator_units: NonNegativeFloat
    complete_units: NonNegativeFloat
    within_tolerance_units: NonNegativeFloat
    miss_units: NonNegativeFloat
    conflict_units: NonNegativeFloat
    not_applicable_target_count: NonNegativeInt
    na_reasons: dict[StableId, NonNegativeInt]


class SegmentScoreRowV8(StrictWire):
    """Serialized adapter for the Phase-B actual-polygon segment result."""
    target_id: StableId | None
    observation_id: StableId | None
    floor_id: StableId
    exterior: StrictBool
    status: Literal["complete", "within_tolerance", "miss", "extra"]
    axis_alignment_error_m: NonNegativeFloat | None
    position_error_m: NonNegativeFloat | None
    extent_symmetric_difference_m: NonNegativeFloat | None


class SegmentExtraV8(StrictWire):
    observation_id: StableId
    floor_id: StableId
    reason: StableId


class ExtraObservationV8(StrictWire):
    observation_id: StableId
    result: Literal["extra", "not_applicable"]
    reason: StableId | None


class C2ScoredPayloadV8(StrictWire):
    kind: Literal["c2_scored"]
    segment_rows: tuple[SegmentScoreRowV8, ...]
    segment_extras: tuple[SegmentExtraV8, ...]
    claim_rows: tuple[ClaimScoreRowV8, ...]
    claim_summaries: tuple[ClaimSummaryV8, ...]
    extras: tuple[ExtraObservationV8, ...]
    score_criteria: tuple[dict[StableId, object], ...]
    # Ledgers stay their public strict types in the scoring service.  Their
    # canonical digests/counts are repeated in the artifact contract, avoiding
    # a second near-wire in this schema module.
    reference_ledger_sha256: Hex64
    product_ledger_sha256: Hex64 | None
    absence_ledger_sha256: Hex64 | None


class NotApplicablePayloadV8(StrictWire):
    kind: Literal["not_applicable"]
    reason: Literal["unsupported_product_schema", "unsupported_gt_profile", "unsupported_view_contract", "no_supported_targets"]
    detail: StrictStr


class RejectedPayloadV8(StrictWire):
    kind: Literal["rejected"]
    error_code: StableId
    cause_code: StableId | None
    gate_id: StableId
    detail: StrictStr

    @model_validator(mode="after")
    def _stable_error(self):
        if self.error_code not in STABLE_ERROR_CODES or self.gate_id not in GATE_IDS:
            raise ValueError("rejected payload requires a frozen error code and gate id")
        return self


ScorePayloadV8 = Annotated[C2ScoredPayloadV8 | NotApplicablePayloadV8 | RejectedPayloadV8, Field(discriminator="kind")]


class EmbeddedLedgerContractV1(StrictWire):
    ledger_kind: Literal["reference", "product", "absence"]
    ledger_count: NonNegativeInt
    aggregate_sha256: Hex64


class ScoreArtifactContractV1(StrictWire):
    contract_version: Literal["1"]
    output_sha256: Hex64
    sidecar_schema_version: Literal["8"]
    grade_kind: Literal["c2_grade", "not_applicable_board", "rejected_board"]
    grade_png_sha256: Hex64
    embedded_ledgers: tuple[EmbeddedLedgerContractV1, ...]

    @model_validator(mode="after")
    def _ledger_shape(self):
        expected = ("reference", "product", "absence")
        if tuple(item.ledger_kind for item in self.embedded_ledgers) != expected:
            raise ValueError("embedded ledgers must be reference/product/absence in order")
        return self


class ScoreSidecarV8(StrictWire):
    schema_version: Literal["8"]
    identity: ScoreIdentityV8
    artifact_contract: ScoreArtifactContractV1
    payload: ScorePayloadV8
    content_sha256: Hex64

    @model_validator(mode="after")
    def _contract(self):
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("sidecar content_sha256 mismatch")
        expected_kind = {
            "c2_scored": "c2_grade",
            "not_applicable": "not_applicable_board",
            "rejected": "rejected_board",
        }[self.payload.kind]
        if self.artifact_contract.output_sha256 != self.identity.product.output_sha256 or self.artifact_contract.grade_kind != expected_kind:
            raise ValueError("sidecar artifact contract disagrees with identity/payload")
        if self.payload.kind in {"not_applicable", "rejected"}:
            empty = canonical_sha256([])
            if any(item.ledger_count != 0 or item.aggregate_sha256 != empty for item in self.artifact_contract.embedded_ledgers):
                raise ValueError("NA/REJECTED sidecar must contain empty ledgers")
        return self


# Reading typed-score wire.  V8 remains available above for strict legacy
# loading; typed service writers emit V9.
READING_ADAPTER_VERSION = "reading_typed_adapter_v2"
READING_SOURCE_APPLICABILITY_VERSION = "reading_source_applicability_v2"
READING_DENOMINATOR_VERSION = "reading_denominator_v2"

ReadingComponent = Literal[
    "plan_segments",
    "plan_openings",
    "elevation_opening_xy",
    "elevation_opening_z",
]


class Affine2DV1(StrictWire):
    """The reading-side plan frame affine, with its two ends declared.

    Unlike ``gt_manifest.Affine2D`` -- which one type carries across three
    different domain/codomain pairs -- this type has exactly one producer
    (``reading_typed_adapter._plan_frame``) and exactly one application site
    (``reading_typed_adapter.apply_affine_2d``), and both ends are constant:
    reading-local plan metres (``units="metre"``, ``local_axes="drawing_right_up"``
    on the enclosing :class:`PlanFrameCertificateV1`) into world metres.

    Because the pair is a compile-time constant, it is declared as class
    attributes rather than wire fields.  That is deliberate on two counts: a
    constant does not belong on the wire, and keeping it off the wire leaves
    ``PlanFrameCertificateV1.preimage_sha256`` -- which is stamped onto every
    plan observation as ``transform_sha256`` and persisted in every
    ``score_vs_gt.json`` -- byte-identical.  ``affine_space.affine_spaces()``
    reads these class attributes, so this type participates in
    ``compose_affine`` / ``require_affine_spaces`` exactly like the wire-carried
    ``Affine2D`` does.
    """

    DOMAIN_SPACE: ClassVar[str] = "reading_plan_local_metre"
    CODOMAIN_SPACE: ClassVar[str] = "world_metre"

    xx: FiniteFloat
    xy: FiniteFloat
    x0: FiniteFloat
    yx: FiniteFloat
    yy: FiniteFloat
    y0: FiniteFloat

    @model_validator(mode="after")
    def _invertible(self):
        if self.xx * self.yy - self.xy * self.yx == 0.0:
            raise ValueError("affine transform must be invertible")
        return self


class PlanFrameCertificateV1(StrictWire):
    input_id: StableId
    floor_id: StableId
    source: Literal["reading_scale_origin_v1"]
    units: Literal["metre"]
    local_axes: Literal["drawing_right_up"]
    affine: Affine2DV1
    nonzero_origin: StrictBool
    preimage_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if self.nonzero_origin != (
            self.affine.x0 != 0.0 or self.affine.y0 != 0.0
        ):
            raise ValueError("nonzero_origin disagrees with affine translation")
        if self.preimage_sha256 != hash_model_without(self, "preimage_sha256"):
            raise ValueError("plan frame preimage hash mismatch")
        return self


class VerticalDatumCertificateV1(StrictWire):
    input_id: StableId
    floor_ids: tuple[StableId, ...]
    status: Literal["applicable", "not_applicable"]
    source: Literal[
        "product_declared",
        "project_convention_2026_07_25",
        "multi_floor_unavailable",
    ]
    units: Literal["metre"]
    local_axis: Literal["drawing_up"]
    z_sign: Literal[1] | None
    z_origin: FiniteFloat | None
    authority: Literal[
        "reading_scale_origin_world_z_m",
        "user_ruling_grade_line_equals_interior_floor_zero",
        "reviewed_binding_multiple_floors",
    ]
    reason: Literal["elevation_floor_partition_unresolved"] | None
    preimage_sha256: Hex64

    @model_validator(mode="after")
    def _contract(self):
        if not self.floor_ids or tuple(sorted(self.floor_ids)) != self.floor_ids:
            raise ValueError("vertical datum floor_ids must be non-empty and sorted")
        if self.source == "multi_floor_unavailable":
            if (
                len(self.floor_ids) < 2
                or self.status != "not_applicable"
                or self.z_sign is not None
                or self.z_origin is not None
                or self.authority != "reviewed_binding_multiple_floors"
                or self.reason != "elevation_floor_partition_unresolved"
            ):
                raise ValueError("invalid unavailable multi-floor vertical datum")
        else:
            authority = {
                "product_declared": "reading_scale_origin_world_z_m",
                "project_convention_2026_07_25": (
                    "user_ruling_grade_line_equals_interior_floor_zero"
                ),
            }[self.source]
            if (
                len(self.floor_ids) != 1
                or self.status != "applicable"
                or self.z_sign != 1
                or self.z_origin is None
                or self.authority != authority
                or self.reason is not None
            ):
                raise ValueError("invalid applicable vertical datum")
        if self.preimage_sha256 != hash_model_without(self, "preimage_sha256"):
            raise ValueError("vertical datum preimage hash mismatch")
        return self


class ReadingComponentApplicabilityV1(StrictWire):
    source_input_id: StableId
    channel: Literal["plan", "elevation"]
    component: ReadingComponent
    floor_ids: tuple[StableId, ...]
    status: Literal["applicable", "not_applicable"]
    reasons: tuple[StableId, ...]
    cause_class: Literal[
        "none",
        "trusted_input",
        "trusted_frame",
        "product_content",
        "judge_ambiguity",
    ]
    denominator_disposition: Literal["score", "filter", "retain_as_miss"]
    observation_count: NonNegativeInt
    transform_sha256: Hex64 | None

    @model_validator(mode="after")
    def _status_contract(self):
        if tuple(sorted(set(self.floor_ids))) != self.floor_ids:
            raise ValueError("component floor_ids must be unique and sorted")
        if self.status == "applicable":
            if (
                self.reasons
                or self.cause_class != "none"
                or self.denominator_disposition != "score"
            ):
                raise ValueError("applicable component has NA metadata")
            return self
        if not self.reasons or tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("NA reasons must be non-empty, unique, and sorted")
        expected = (
            "filter"
            if self.cause_class == "trusted_input"
            else "retain_as_miss"
        )
        if self.cause_class == "none" or self.denominator_disposition != expected:
            raise ValueError("NA cause and denominator disposition disagree")
        return self


class Point2V1(StrictWire):
    x: FiniteFloat
    y: FiniteFloat


class ClosedIntervalV1(StrictWire):
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if self.lo > self.hi:
            raise ValueError("closed interval requires lo <= hi")
        return self


class ReadingPlanSegmentAuditV1(StrictWire):
    kind: Literal["plan_segment"]
    observation_id: StableId
    source_input_id: StableId
    source_stroke_id: StableId
    primitive_index: NonNegativeInt
    floor_id: StableId
    local_p1: Point2V1
    local_p2: Point2V1
    world_p1: Point2V1
    world_p2: Point2V1
    source_geometry_sha256: Hex64
    transform_sha256: Hex64
    topology: Literal["unknown"]


class ReadingPlanOpeningAuditV1(StrictWire):
    kind: Literal["plan_opening"]
    observation_id: StableId
    source_input_id: StableId
    source_stroke_id: StableId
    floor_id: StableId
    geometry_kind: Literal["line", "rect", "polyline"]
    local_vertices: tuple[Point2V1, ...]
    world_vertices: tuple[Point2V1, ...]
    source_geometry_sha256: Hex64
    transform_sha256: Hex64


class ReadingElevationOpeningAuditV1(StrictWire):
    kind: Literal["elevation_opening"]
    observation_id: StableId
    source_input_id: StableId
    source_stroke_id: StableId
    floor_id: StableId
    facade_family: CardinalFamily
    geometry_kind: Literal["line", "rect", "polyline"]
    local_x_interval: ClosedIntervalV1
    local_y_interval: ClosedIntervalV1
    world_along_interval: ClosedIntervalV1
    z_interval: ClosedIntervalV1 | None
    source_geometry_sha256: Hex64
    horizontal_transform_sha256: Hex64
    vertical_transform_sha256: Hex64 | None

    @model_validator(mode="after")
    def _vertical_pair(self):
        if (self.z_interval is None) != (
            self.vertical_transform_sha256 is None
        ):
            raise ValueError("elevation z interval and transform must pair")
        return self


ReadingObservationAuditV1 = Annotated[
    ReadingPlanSegmentAuditV1
    | ReadingPlanOpeningAuditV1
    | ReadingElevationOpeningAuditV1,
    Field(discriminator="kind"),
]


class ReadingMetadataFindingV1(StrictWire):
    source_input_id: StableId
    code: Literal[
        "image_kind_declaration_mismatch",
        "orientation_declaration_mismatch",
        "unbound_reading_view",
    ]
    declared_sha256: Hex64
    trusted_sha256: Hex64


class ReadingAmbiguityWitnessV1(StrictWire):
    source_input_id: StableId
    component: ReadingComponent
    floor_ids: tuple[StableId, ...]
    observation_ids: tuple[StableId, ...]
    candidate_target_ids: tuple[StableId, ...]
    objective_preimage_sha256: Hex64
    reason: Literal[
        "multiple_support_lines",
        "multiple_equal_opening_assignments",
        "coordinate_identity_unresolved",
    ]


class UnmeasurableObservationWitnessV1(StrictWire):
    source_input_id: StableId
    source_stroke_id: StableId
    component: ReadingComponent
    reason: Literal[
        "plan_wall_rect_has_no_centerline_contract",
        "consumed_geometry_malformed",
    ]
    cause_class: Literal["product_content"]
    source_geometry_sha256: Hex64


class ElevationFrameDisagreementWitnessV1(StrictWire):
    """Retained only to read v1 certificates; v2 never emits this witness."""
    source_input_id: StableId
    binding_local_x_positive: Literal[
        "image_left_to_right", "image_right_to_left"
    ]
    product_local_x_positive_raw: Literal[
        "image_left_to_right", "image_right_to_left", "missing"
    ]
    product_local_x_positive_effective: Literal[
        "image_left_to_right", "image_right_to_left"
    ] | None
    binding_mirrored: StrictBool
    product_mirrored_raw: StrictBool | Literal[
        "true", "false", "unknown", "missing"
    ]
    product_mirrored_effective: StrictBool | None
    binding_frame_transform_sha256: Hex64
    product_facade_sha256: Hex64
    reason: Literal["elevation_local_x_sense_disagreement"]


class ReadingNormalizationCertificateV1(StrictWire):
    schema_version: Literal["1"]
    helper_version: Literal["reading_typed_adapter_v2"]
    contract_detector_version: Literal["reading_contract_detector_v2"]
    source_output_sha256: Hex64
    product_contract: Literal["reading_views_v2"]
    base_view_manifest_sha256: Hex64
    score_view_bindings_sha256: Hex64
    plan_frames: tuple[PlanFrameCertificateV1, ...]
    vertical_datums: tuple[VerticalDatumCertificateV1, ...]
    component_applicability: tuple[ReadingComponentApplicabilityV1, ...]
    observations: tuple[ReadingObservationAuditV1, ...]
    unmeasurable_observation_witnesses: tuple[
        UnmeasurableObservationWitnessV1, ...
    ]
    elevation_frame_disagreements: tuple[
        ElevationFrameDisagreementWitnessV1, ...
    ]
    metadata_findings: tuple[ReadingMetadataFindingV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical(self):
        keys = [
            (item.source_input_id, item.component, item.floor_ids)
            for item in self.component_applicability
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("component applicability must be canonical")
        observation_keys = [
            (
                item.source_input_id,
                item.source_stroke_id,
                item.kind,
                getattr(item, "primitive_index", 0),
            )
            for item in self.observations
        ]
        if observation_keys != sorted(observation_keys):
            raise ValueError("observations must be canonical")
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("normalization certificate hash mismatch")
        return self


class ReadingFilteredComponentBasisV1(StrictWire):
    source_input_id: StableId
    component: ReadingComponent
    floor_ids: tuple[StableId, ...]
    cause_class: Literal["trusted_input"]
    reasons: tuple[StableId, ...]


class ReadingDenominatorBasisV1(StrictWire):
    helper_version: Literal["reading_denominator_v2"]
    gt_content_sha256: Hex64
    base_view_manifest_sha256: Hex64
    score_view_bindings_sha256: Hex64
    filtered_components: tuple[ReadingFilteredComponentBasisV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical(self):
        keys = [
            (item.source_input_id, item.component, item.floor_ids, item.reasons)
            for item in self.filtered_components
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("filtered components must be canonical")
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("denominator basis hash mismatch")
        return self


class ReadingDenominatorAtomV1(StrictWire):
    atom_id: StableId
    target_id: StableId
    target_kind: Literal["plan_segment", "window"]
    component: ReadingComponent
    claim: ClaimName | None
    floor_id: StableId
    source_input_ids: tuple[StableId, ...]
    eligible_units: NonNegativeFloat


class SourceApplicabilityCertificateV1(StrictWire):
    schema_version: Literal["1"]
    helper_version: Literal["reading_source_applicability_v2"]
    normalization_sha256: Hex64
    gt_content_sha256: Hex64
    score_manifest_sha256: Hex64
    denominator_basis: ReadingDenominatorBasisV1
    denominator_atoms: tuple[ReadingDenominatorAtomV1, ...]
    denominator_basis_sha256: Hex64
    denominator_sha256: Hex64
    component_applicability: tuple[ReadingComponentApplicabilityV1, ...]
    ambiguity_witnesses: tuple[ReadingAmbiguityWitnessV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _canonical(self):
        atom_keys = [
            (
                item.target_id,
                item.component,
                item.claim or "",
                item.source_input_ids,
            )
            for item in self.denominator_atoms
        ]
        if atom_keys != sorted(atom_keys) or len(atom_keys) != len(set(atom_keys)):
            raise ValueError("denominator atoms must be canonical")
        if self.denominator_basis_sha256 != self.denominator_basis.content_sha256:
            raise ValueError("denominator basis digest mismatch")
        if self.denominator_sha256 != canonical_sha256(
            [item.model_dump(mode="json") for item in self.denominator_atoms]
        ):
            raise ValueError("denominator digest mismatch")
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("source applicability certificate hash mismatch")
        return self


def count_unmeasurable_reading_observations(
    normalization: ReadingNormalizationCertificateV1 | None,
    applicability: SourceApplicabilityCertificateV1 | None,
) -> int:
    """Count product observations that the reading path could not measure.

    Adapter-level malformed observations already have dedicated witnesses.
    Registration ambiguities arise only after joining the certified product to
    GT, so their exact observation/boundary witness belongs to the source
    applicability certificate.  Count each support-ambiguous observation once
    without changing the GT-derived denominator.
    """
    adapter_count = (
        0
        if normalization is None
        else len(normalization.unmeasurable_observation_witnesses)
    )
    support_ambiguous = {
        (witness.source_input_id, witness.component, observation_id)
        for witness in (
            () if applicability is None else applicability.ambiguity_witnesses
        )
        if witness.reason == "multiple_support_lines"
        for observation_id in witness.observation_ids
    }
    return adapter_count + len(support_ambiguous)


class ReadingChannelSummaryV1(StrictWire):
    channel: Literal["plan", "elevation"]
    status: Literal["applicable", "partially_applicable", "not_applicable"]
    source_input_ids: tuple[StableId, ...]
    applicable_components: tuple[ReadingComponent, ...]
    not_applicable_components: tuple[ReadingComponent, ...]
    reasons: tuple[StableId, ...]


class ReadingVisibilityCountsV1(StrictWire):
    nonzero_plan_origins: NonNegativeInt
    project_convention_vertical_datums: NonNegativeInt
    multiple_plan_view_floor_components: NonNegativeInt
    elevation_local_x_sense_disagreements: NonNegativeInt
    scorer_internal_failures: NonNegativeInt


class HelperIdentityV9(StrictWire):
    scorer_schema: Literal["9"]
    segment_scorer: StableId
    opening_matcher: Literal[
        "reading_opening_global_assignment_v1",
        "correction_opening_global_assignment_v2",
        "correction_opening_global_assignment_v3",
        "correction_opening_global_assignment_v4",
        "correction_opening_global_assignment_v5",
    ]
    gt_to_va_adapter: StableId
    denominator_helper: StableId
    grade_renderer: Literal["b4b_grade_png_v2"]
    va_helper: StableId
    vg_helper: StableId
    claims_contract: StableId
    reading_contract_detector: Literal["reading_contract_detector_v2"]
    reading_adapter: Literal["reading_typed_adapter_v2"]
    reading_source_applicability: Literal["reading_source_applicability_v2"]


class ScoreIdentityV9(StrictWire):
    gt: GtIdentityV8
    product: ProductIdentityV8
    manifest: ManifestIdentityV8
    helpers: HelperIdentityV9
    capability: CapabilityDecisionV8
    tolerances: C2ToleranceIdentityV8
    reading_normalization_sha256: Hex64 | None
    source_applicability_sha256: Hex64 | None
    score_manifest_sha256: Hex64
    denominator_basis_sha256: Hex64 | None
    denominator_sha256: Hex64 | None
    reference_applicability_sha256: Hex64 | None
    product_applicability_sha256: Hex64 | None
    absence_applicability_sha256: Hex64 | None


class ReadingSegmentScoreRowV1(StrictWire):
    row_contract: Literal["reading_segment_v1"]
    target_id: StableId | None
    observation_id: StableId | None
    floor_id: StableId
    target_exterior: StrictBool | None
    status: Literal[
        "complete",
        "within_tolerance",
        "miss",
        "extra",
        "duplicate",
        "not_applicable",
    ]
    eligible_units: NonNegativeFloat
    axis_alignment_error_m: NonNegativeFloat | None
    position_error_m: NonNegativeFloat | None
    extent_symmetric_difference_m: NonNegativeFloat | None
    na_reason: StableId | None

    @model_validator(mode="after")
    def _row_contract(self):
        is_na = self.status == "not_applicable"
        if is_na != (self.na_reason is not None):
            raise ValueError("segment NA status and reason disagree")
        if is_na and self.eligible_units != 0.0:
            raise ValueError("segment NA row must have zero eligible units")
        if (self.target_id is None) != (self.target_exterior is None):
            raise ValueError("segment target identity and topology disagree")
        return self


class OpeningSourceScoreRowV1(StrictWire):
    target_id: StableId
    target_kind: Literal["window", "door"]
    claim: ClaimName
    source_input_id: StableId
    channel: Literal["plan", "elevation"]
    eligible_units: Annotated[
        float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    ]
    result: Literal[
        "complete",
        "within_tolerance",
        "miss",
        "conflict",
        "not_applicable",
    ]
    na_reason: StableId | None
    matched_observation_ids: tuple[StableId, ...]
    expected_intervals: tuple[ClosedIntervalV1, ...]
    observed_interval: ClosedIntervalV1 | None
    expected_scalar: FiniteFloat | None
    observed_scalar: FiniteFloat | None
    error_metric: Literal[
        "binary",
        "endpoint_max_abs",
        "length_abs",
        "masked_interval_length",
        "scalar_abs",
        "not_applicable",
    ]
    error_value: NonNegativeFloat | None
    tolerance: NonNegativeFloat | None
    source_applicability_sha256: Hex64

    @model_validator(mode="after")
    def _row_contract(self):
        is_na = self.result == "not_applicable"
        if is_na:
            if (
                self.eligible_units != 0.0
                or self.na_reason is None
                or self.error_metric != "not_applicable"
                or self.error_value is not None
                or self.tolerance is not None
            ):
                raise ValueError("invalid not-applicable source row")
            return self
        if (
            self.eligible_units == 0.0
            or self.na_reason is not None
            or self.error_metric == "not_applicable"
            or self.tolerance is None
        ):
            raise ValueError("invalid eligible source row")
        if self.error_value is None and self.matched_observation_ids:
            raise ValueError(
                "matched eligible source row requires a numeric error"
            )
        return self


class ScoreCriterionV9(StrictWire):
    criterion_id: StableId
    eligible: StrictBool
    denominator_units: NonNegativeFloat
    passing_units: NonNegativeFloat
    failing_units: NonNegativeFloat
    na_reasons: dict[StableId, NonNegativeInt]
    verdict: Literal[
        "pass", "fail", "not_applicable", "insufficient_evidence"
    ]


class C2ScoredPayloadV9(StrictWire):
    kind: Literal["c2_scored"]
    channel_applicability: tuple[ReadingChannelSummaryV1, ...]
    unmeasurable_observations: NonNegativeInt
    visibility_counts: ReadingVisibilityCountsV1
    segment_rows: tuple[SegmentScoreRowV8 | ReadingSegmentScoreRowV1, ...]
    segment_extras: tuple[SegmentExtraV8, ...]
    opening_source_rows: tuple[OpeningSourceScoreRowV1, ...]
    claim_rows: tuple[ClaimScoreRowV8, ...]
    claim_summaries: tuple[ClaimSummaryV8, ...]
    extras: tuple[ExtraObservationV8, ...]
    score_criteria: tuple[ScoreCriterionV9, ...]
    reference_ledger_sha256: Hex64
    product_ledger_sha256: Hex64 | None
    absence_ledger_sha256: Hex64 | None


class NotApplicablePayloadV9(StrictWire):
    kind: Literal["not_applicable"]
    reason: Literal[
        "unsupported_reading_contract",
        "unsupported_gt_profile",
        "unsupported_view_contract",
        "no_scorable_reading_channel",
        "scorer_internal_failure",
    ]
    detail: StableId
    channel_applicability: tuple[ReadingChannelSummaryV1, ...]
    unmeasurable_observations: NonNegativeInt
    visibility_counts: ReadingVisibilityCountsV1
    score_criteria: tuple[ScoreCriterionV9, ...]


class RejectedPayloadV9(StrictWire):
    kind: Literal["rejected"]
    error_code: StableId
    cause_code: StableId | None
    gate_id: StableId
    detail: StableId
    channel_applicability: tuple[ReadingChannelSummaryV1, ...]
    unmeasurable_observations: NonNegativeInt
    visibility_counts: ReadingVisibilityCountsV1

    @model_validator(mode="after")
    def _stable_error(self):
        if self.error_code not in STABLE_ERROR_CODES or self.gate_id not in GATE_IDS:
            raise ValueError(
                "rejected payload requires a frozen error code and gate id"
            )
        return self


ScorePayloadV9 = Annotated[
    C2ScoredPayloadV9 | NotApplicablePayloadV9 | RejectedPayloadV9,
    Field(discriminator="kind"),
]


class ScoreCertificatesV1(StrictWire):
    reading_normalization: ReadingNormalizationCertificateV1 | None
    source_applicability: SourceApplicabilityCertificateV1 | None
    aggregate_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        expected = canonical_sha256(
            {
                "reading_normalization": (
                    "absent"
                    if self.reading_normalization is None
                    else self.reading_normalization.content_sha256
                ),
                "source_applicability": (
                    "absent"
                    if self.source_applicability is None
                    else self.source_applicability.content_sha256
                ),
            }
        )
        if self.aggregate_sha256 != expected:
            raise ValueError("certificate aggregate digest mismatch")
        return self


class EmbeddedCertificateContractV1(StrictWire):
    certificate_kind: Literal[
        "reading_normalization", "source_applicability"
    ]
    present: StrictBool
    aggregate_sha256: Hex64


class ScoreArtifactContractV2(StrictWire):
    contract_version: Literal["2"]
    output_sha256: Hex64
    sidecar_schema_version: Literal["9"]
    grade_kind: Literal[
        "c2_grade", "not_applicable_board", "rejected_board"
    ]
    grade_png_sha256: Hex64
    embedded_ledgers: tuple[EmbeddedLedgerContractV1, ...]
    embedded_certificates: tuple[EmbeddedCertificateContractV1, ...]

    @model_validator(mode="after")
    def _shape(self):
        if tuple(item.ledger_kind for item in self.embedded_ledgers) != (
            "reference", "product", "absence"
        ):
            raise ValueError("embedded ledgers must have canonical order")
        if tuple(
            item.certificate_kind for item in self.embedded_certificates
        ) != ("reading_normalization", "source_applicability"):
            raise ValueError("embedded certificates must have canonical order")
        return self


class ScoreSidecarV9(StrictWire):
    schema_version: Literal["9"]
    identity: ScoreIdentityV9
    certificates: ScoreCertificatesV1
    artifact_contract: ScoreArtifactContractV2
    payload: ScorePayloadV9
    content_sha256: Hex64

    @model_validator(mode="after")
    def _contract(self):
        if self.content_sha256 != hash_model_without(self, "content_sha256"):
            raise ValueError("sidecar content_sha256 mismatch")
        expected_kind = {
            "c2_scored": "c2_grade",
            "not_applicable": "not_applicable_board",
            "rejected": "rejected_board",
        }[self.payload.kind]
        if (
            self.artifact_contract.output_sha256
            != self.identity.product.output_sha256
            or self.artifact_contract.grade_kind != expected_kind
        ):
            raise ValueError("sidecar artifact contract disagrees with result")
        normalization = self.certificates.reading_normalization
        applicability = self.certificates.source_applicability
        if self.identity.reading_normalization_sha256 != (
            None if normalization is None else normalization.content_sha256
        ):
            raise ValueError("normalization identity mismatch")
        if self.identity.source_applicability_sha256 != (
            None if applicability is None else applicability.content_sha256
        ):
            raise ValueError("source applicability identity mismatch")
        if applicability is not None and (
            self.identity.denominator_basis_sha256
            != applicability.denominator_basis_sha256
            or self.identity.denominator_sha256
            != applicability.denominator_sha256
        ):
            raise ValueError("denominator identity mismatch")
        if applicability is None and (
            self.identity.denominator_basis_sha256 is not None
            or self.identity.denominator_sha256 is not None
        ):
            raise ValueError("absent applicability has denominator identity")
        if self.identity.product.stage == "correction" and (
            normalization is not None
            or applicability is not None
            or self.payload.channel_applicability
            or self.payload.unmeasurable_observations != 0
            or self.payload.visibility_counts
            != empty_visibility_counts_v1()
        ):
            raise ValueError("correction sidecar contains reading-only evidence")
        if self.identity.product.stage == "reading":
            if self.payload.kind == "c2_scored" and (
                normalization is None or applicability is None
            ):
                raise ValueError("scored reading sidecar requires both certificates")
            if (
                self.payload.kind == "not_applicable"
                and self.payload.reason == "no_scorable_reading_channel"
                and (normalization is None or applicability is None)
            ):
                raise ValueError(
                    "normalized channel NA requires both certificates"
                )
        expected_unmeasurable = count_unmeasurable_reading_observations(
            normalization, applicability
        )
        if self.payload.unmeasurable_observations != expected_unmeasurable:
            raise ValueError("unmeasurable observation count mismatch")
        counts = self.payload.visibility_counts
        expected_certificate_counts = {
            "nonzero_plan_origins": (
                0
                if normalization is None
                else sum(item.nonzero_origin for item in normalization.plan_frames)
            ),
            "project_convention_vertical_datums": (
                0
                if normalization is None
                else sum(
                    item.source == "project_convention_2026_07_25"
                    for item in normalization.vertical_datums
                )
            ),
            "multiple_plan_view_floor_components": (
                0
                if normalization is None
                else sum(
                    "multiple_plan_views_per_floor_unsupported" in item.reasons
                    for item in normalization.component_applicability
                )
            ),
            "elevation_local_x_sense_disagreements": (
                0
                if normalization is None
                else len(normalization.elevation_frame_disagreements)
            ),
        }
        for field, expected in expected_certificate_counts.items():
            if getattr(counts, field) != expected:
                raise ValueError(f"{field} count mismatch")
        expected_internal = (
            1
            if (
                self.payload.kind == "not_applicable"
                and self.payload.reason == "scorer_internal_failure"
            )
            else 0
        )
        if counts.scorer_internal_failures != expected_internal:
            raise ValueError("scorer internal failure count mismatch")
        empty = canonical_sha256([])
        embedded_ledgers = self.artifact_contract.embedded_ledgers
        if self.payload.kind in {"not_applicable", "rejected"}:
            if any(
                item.ledger_count != 0 or item.aggregate_sha256 != empty
                for item in embedded_ledgers
            ):
                raise ValueError("NA/REJECTED sidecar must contain empty ledgers")
        else:
            expected_ledger_hashes = (
                self.payload.reference_ledger_sha256,
                self.payload.product_ledger_sha256 or empty,
                self.payload.absence_ledger_sha256 or empty,
            )
            if tuple(
                item.aggregate_sha256 for item in embedded_ledgers
            ) != expected_ledger_hashes:
                raise ValueError("embedded ledger digest mismatch")
        expected_certificate_hashes = (
            "absent" if normalization is None else normalization.content_sha256,
            "absent" if applicability is None else applicability.content_sha256,
        )
        for item, expected in zip(
            self.artifact_contract.embedded_certificates,
            expected_certificate_hashes,
        ):
            if (
                item.present != (expected != "absent")
                or item.aggregate_sha256
                != canonical_sha256(
                    {
                        "certificate_kind": item.certificate_kind,
                        "content_sha256": expected,
                    }
                )
            ):
                raise ValueError("embedded certificate contract mismatch")
        return self


def load_score_gt_identity(path: Path | str) -> tuple[GtIdentityV8, GtDocument | None]:
    """Typed-only GT identity loading; v3 never calls legacy ``load_gt``."""
    source = Path(path)
    try:
        raw = source.read_bytes()
        document = load_gt_file(source)
    except Exception as exc:
        cause = getattr(exc, "issues", None)
        raise ScoreContractError("score_gt_identity_invalid", "scoring.input_identity", cause_code=(cause[0].code if cause else None)) from exc
    file_sha = hashlib.sha256(raw).hexdigest()
    if isinstance(document, GroundTruthV3):
        identity = GtIdentityV8(path_id=source.name, file_sha256=file_sha, content_sha256=document.content_sha256,
            schema_version=3, profile=document.geometry_profile, coordinate_frame=document.coordinate_frame,
            verification_status=document.verification.status, loader_helper_version="gt_typed_loader_v1")
        if document.geometry_profile != "c2_simple_orthogonal_no_holes":
            return identity, None
        return identity, document
    payload = document.model_dump(mode="json", by_alias=True)
    return GtIdentityV8(path_id=source.name, file_sha256=file_sha, content_sha256=canonical_sha256(payload),
        schema_version=2, profile=None, coordinate_frame=None, verification_status=None,
        loader_helper_version="gt_typed_loader_v1"), document


def compute_facade_segments_sha256(segments: tuple[object, ...] | list[object]) -> str:
    """A0's frozen Va preimage, independently of Va's private hash helper."""
    rank = {"North": 0, "South": 1, "East": 2, "West": 3}
    try:
        rows = sorted(
            (segment.floor_id, rank[segment.facade_family], segment.world_along_interval.lo,
             segment.world_along_interval.hi, segment.depth, segment.id, segment.model_dump(mode="json"))
            for segment in segments
        )
    except (AttributeError, KeyError) as exc:
        raise ScoreContractError("score_visibility_adapter_mismatch", "scoring.applicability") from exc
    return canonical_sha256([row[-1] for row in rows])


def decide_score_capability(*, gt_identity: GtIdentityV8, stage: Literal["reading", "correction"],
                            product_schema: str, view_manifest: ViewManifest,
                            product_artifact_contract: str | None = None,
                            reading_contract_detector_version: str = READING_CONTRACT_DETECTOR_VERSION,
                            reading_adapter_version: str = READING_ADAPTER_VERSION,
                            score_view_bindings_sha256: str | None = None) -> CapabilityDecisionV8:
    """Phase-A dispatch only; it makes unsupported inputs explicit, never raw."""
    from src.agent.correction.facade_applicability import FACADE_APPLICABILITY_SCHEMA_VERSION
    segment_geometry_capability = "c2" if gt_identity.profile == "c2_simple_orthogonal_no_holes" else "legacy_rectangular"
    key = (str(gt_identity.schema_version), gt_identity.profile or "legacy", stage, product_schema,
           view_manifest.view_manifest_schema_version, view_manifest.completeness_ruleset_version,
           FACADE_APPLICABILITY_SCHEMA_VERSION, segment_geometry_capability,
           product_artifact_contract or "no_artifact_contract")
    if stage == "reading":
        key += (
            gt_identity.content_sha256,
            view_manifest.content_sha256,
            score_view_bindings_sha256 or "no_score_view_bindings",
            reading_contract_detector_version,
            reading_adapter_version,
        )
    if gt_identity.schema_version == 2:
        return CapabilityDecisionV8(path="legacy_v2", capability_key=key, reason=None, gate_id="scoring.capability")
    if gt_identity.profile != "c2_simple_orthogonal_no_holes":
        return CapabilityDecisionV8(path="not_applicable", capability_key=key, reason="unsupported_gt_profile", gate_id="scoring.capability")
    if stage == "reading" and (
        product_schema != READING_PRODUCT_CONTRACT
        or reading_contract_detector_version
        != READING_CONTRACT_DETECTOR_VERSION
        or reading_adapter_version != READING_ADAPTER_VERSION
    ):
        return CapabilityDecisionV8(
            path="not_applicable",
            capability_key=key,
            reason="unsupported_reading_contract",
            gate_id="scoring.capability",
        )
    if stage == "correction" and product_schema not in {"3", "v3"}:
        return CapabilityDecisionV8(path="not_applicable", capability_key=key, reason="unsupported_product_schema", gate_id="scoring.capability")
    if stage == "correction" and product_artifact_contract not in {
        "correction_b5_v1", "correction_b5_orientation_v1",
    }:
        return CapabilityDecisionV8(
            path="not_applicable",
            capability_key=key,
            reason="unsupported_product_contract",
            gate_id="scoring.capability",
        )
    return CapabilityDecisionV8(path="c2_v3", capability_key=key, reason=None, gate_id="scoring.capability")


def build_product_identity(*, stage: Literal["reading", "correction"], attempt: int, output_sha256: str,
                           output_schema: str, source: str, accepted_stage_record: object | None) -> ProductIdentityV8:
    record_sha = None if accepted_stage_record is None else canonical_sha256(accepted_stage_record.model_dump(mode="json")
        if hasattr(accepted_stage_record, "model_dump") else accepted_stage_record)
    return ProductIdentityV8(stage=stage, attempt=attempt, output_sha256=output_sha256, output_schema=output_schema,
        accepted=accepted_stage_record is not None, accepted_stage_record_sha256=record_sha, source=source,
        artifact_contract=None if accepted_stage_record is None else accepted_stage_record.artifact_contract)


def build_phase_a_sidecar(*, identity: ScoreIdentityV8, payload: ScorePayloadV8, grade_png_sha256: str) -> ScoreSidecarV8:
    if payload.kind == "c2_scored":
        raise ValueError("use finalize_score_sidecar for c2 payload")
    empty = canonical_sha256([])
    artifact = ScoreArtifactContractV1(contract_version="1", output_sha256=identity.product.output_sha256,
        sidecar_schema_version="8", grade_kind=("not_applicable_board" if payload.kind == "not_applicable" else "rejected_board"),
        grade_png_sha256=grade_png_sha256, embedded_ledgers=tuple(
            EmbeddedLedgerContractV1(ledger_kind=kind, ledger_count=0, aggregate_sha256=empty)
            for kind in ("reference", "product", "absence")
        ))
    raw = {"schema_version": "8", "identity": identity.model_dump(mode="json"),
           "artifact_contract": artifact.model_dump(mode="json"), "payload": payload.model_dump(mode="json")}
    return ScoreSidecarV8(schema_version="8", identity=identity, artifact_contract=artifact, payload=payload,
        content_sha256=canonical_sha256(raw))


def finalize_score_sidecar(*, identity: ScoreIdentityV8, payload: ScorePayloadV8,
                           grade_png: bytes, ledger_counts: tuple[int, int, int] = (0, 0, 0)) -> ScoreSidecarV8:
    """Finalize the typed v8 sidecar only after scorer and PNG success."""
    if payload.kind != "c2_scored":
        return build_phase_a_sidecar(identity=identity, payload=payload,
                                     grade_png_sha256=hashlib.sha256(grade_png).hexdigest())
    aggregate = (
        payload.reference_ledger_sha256,
        payload.product_ledger_sha256 or canonical_sha256([]),
        payload.absence_ledger_sha256 or canonical_sha256([]),
    )
    artifact = ScoreArtifactContractV1(
        contract_version="1", output_sha256=identity.product.output_sha256,
        sidecar_schema_version="8", grade_kind="c2_grade",
        grade_png_sha256=hashlib.sha256(grade_png).hexdigest(),
        embedded_ledgers=tuple(EmbeddedLedgerContractV1(ledger_kind=kind, ledger_count=count, aggregate_sha256=digest)
                               for kind, count, digest in zip(("reference", "product", "absence"), ledger_counts, aggregate)),
    )
    raw = {"schema_version": "8", "identity": identity.model_dump(mode="json"),
           "artifact_contract": artifact.model_dump(mode="json"), "payload": payload.model_dump(mode="json")}
    return ScoreSidecarV8(schema_version="8", identity=identity, artifact_contract=artifact,
                           payload=payload, content_sha256=canonical_sha256(raw))


def empty_visibility_counts_v1(
    *, scorer_internal_failures: int = 0
) -> ReadingVisibilityCountsV1:
    return ReadingVisibilityCountsV1(
        nonzero_plan_origins=0,
        project_convention_vertical_datums=0,
        multiple_plan_view_floor_components=0,
        elevation_local_x_sense_disagreements=0,
        scorer_internal_failures=scorer_internal_failures,
    )


def empty_score_certificates_v1() -> ScoreCertificatesV1:
    raw = {
        "reading_normalization": "absent",
        "source_applicability": "absent",
    }
    return ScoreCertificatesV1(
        reading_normalization=None,
        source_applicability=None,
        aggregate_sha256=canonical_sha256(raw),
    )


def finalize_score_sidecar_v9(
    *,
    identity: ScoreIdentityV9,
    payload: ScorePayloadV9,
    grade_png: bytes,
    certificates: ScoreCertificatesV1 | None = None,
    ledger_counts: tuple[int, int, int] = (0, 0, 0),
) -> ScoreSidecarV9:
    """Finalize a strict V9 sidecar while retaining V8 public correction rows."""
    certs = certificates or empty_score_certificates_v1()
    empty = canonical_sha256([])
    if payload.kind == "c2_scored":
        ledger_hashes = (
            payload.reference_ledger_sha256,
            payload.product_ledger_sha256 or empty,
            payload.absence_ledger_sha256 or empty,
        )
    else:
        ledger_counts = (0, 0, 0)
        ledger_hashes = (empty, empty, empty)
    certificate_models = (
        certs.reading_normalization,
        certs.source_applicability,
    )
    artifact = ScoreArtifactContractV2(
        contract_version="2",
        output_sha256=identity.product.output_sha256,
        sidecar_schema_version="9",
        grade_kind={
            "c2_scored": "c2_grade",
            "not_applicable": "not_applicable_board",
            "rejected": "rejected_board",
        }[payload.kind],
        grade_png_sha256=hashlib.sha256(grade_png).hexdigest(),
        embedded_ledgers=tuple(
            EmbeddedLedgerContractV1(
                ledger_kind=kind,
                ledger_count=count,
                aggregate_sha256=digest,
            )
            for kind, count, digest in zip(
                ("reference", "product", "absence"),
                ledger_counts,
                ledger_hashes,
            )
        ),
        embedded_certificates=tuple(
            EmbeddedCertificateContractV1(
                certificate_kind=kind,
                present=model is not None,
                aggregate_sha256=canonical_sha256(
                    {
                        "certificate_kind": kind,
                        "content_sha256": (
                            "absent" if model is None else model.content_sha256
                        ),
                    }
                ),
            )
            for kind, model in zip(
                ("reading_normalization", "source_applicability"),
                certificate_models,
            )
        ),
    )
    raw = {
        "schema_version": "9",
        "identity": identity.model_dump(mode="json"),
        "certificates": certs.model_dump(mode="json"),
        "artifact_contract": artifact.model_dump(mode="json"),
        "payload": payload.model_dump(mode="json"),
    }
    return ScoreSidecarV9(
        schema_version="9",
        identity=identity,
        certificates=certs,
        artifact_contract=artifact,
        payload=payload,
        content_sha256=canonical_sha256(raw),
    )


def load_cached_score(path: Path | str, *, grade_path: Path | str,
                      expected_identity: ScoreIdentityV8 | ScoreIdentityV9
                      ) -> ScoreSidecarV8 | ScoreSidecarV9 | None:
    """Schema 0--7 and every identity mismatch are deliberate cache misses."""
    model = (
        ScoreSidecarV9
        if isinstance(expected_identity, ScoreIdentityV9)
        else ScoreSidecarV8
    )
    try:
        result = model.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return None
    try:
        grade_sha256 = hashlib.sha256(Path(grade_path).read_bytes()).hexdigest()
    except OSError:
        return None
    # Identity is deliberately a complete strict-wire comparison.  Do not
    # replace this with a selected-field comparison: every member represents a
    # score input (including accepted-state and helper versions).
    if result.identity != expected_identity:
        return None
    if result.artifact_contract.output_sha256 != expected_identity.product.output_sha256:
        return None
    if result.artifact_contract.grade_png_sha256 != grade_sha256:
        return None
    return result


def commit_score_artifacts(*, sidecar_path: Path | str, grade_path: Path | str,
                           sidecar: ScoreSidecarV8 | ScoreSidecarV9,
                           grade_png: bytes) -> None:
    """Commit the grade pair with the sidecar as its commit marker.

    Both payloads are fully validated before touching the published names.  A
    controlled replace failure restores the previous complete pair; temporary
    files are in the destination directory and are never cache candidates.
    This intentionally accepts an already-complete old pair and never promotes
    an invalid/candidate sidecar as a cache hit.
    """
    score = Path(sidecar_path)
    grade = Path(grade_path)
    if score.parent != grade.parent:
        raise ScoreContractError("score_atomic_write_failed", "scoring.sidecar_identity",
                                 context={"reason": "artifacts_must_share_directory"})
    if hashlib.sha256(grade_png).hexdigest() != sidecar.artifact_contract.grade_png_sha256:
        raise ScoreContractError("score_sidecar_invalid", "scoring.sidecar_identity",
                                 context={"reason": "grade_digest_mismatch"})
    # A strict round trip catches an accidentally assembled but invalid model
    # before the PNG is made visible.
    try:
        model = ScoreSidecarV9 if isinstance(sidecar, ScoreSidecarV9) else ScoreSidecarV8
        strict_sidecar = model.model_validate_json(sidecar.model_dump_json())
        if strict_sidecar != sidecar:
            raise ValidationError.from_exception_data("ScoreSidecarV8", [])
    except Exception as exc:
        raise ScoreContractError("score_sidecar_invalid", "scoring.sidecar_identity") from exc

    score.parent.mkdir(parents=True, exist_ok=True)
    old_score = score.read_bytes() if score.exists() else None
    old_grade = grade.read_bytes() if grade.exists() else None
    temp_paths: list[Path] = []
    try:
        def _temp(target: Path, data: bytes) -> Path:
            fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            tmp = Path(raw); temp_paths.append(tmp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            return tmp

        png_tmp = _temp(grade, grade_png)
        # Verify the exact bytes can be read before commit.  Pillow is imported
        # lazily to keep schema loading judge-wire-only.
        from PIL import Image
        with Image.open(png_tmp) as probe:
            probe.verify()
        sidecar_tmp = _temp(score, strict_sidecar.model_dump_json().encode("utf-8"))
        model.model_validate_json(sidecar_tmp.read_text(encoding="utf-8"))
        # PNG first; the JSON is the commit marker.  If either replace throws,
        # restore the last complete pair before surfacing the stable error.
        os.replace(png_tmp, grade)
        temp_paths.remove(png_tmp)
        os.replace(sidecar_tmp, score)
        temp_paths.remove(sidecar_tmp)
        # Windows has no POSIX directory fsync. Both files were already
        # flushed before atomic replacement; directory durability is OS-owned.
        if os.name != "nt":
            directory_fd = os.open(score.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as exc:
        try:
            if old_grade is None:
                if grade.exists():
                    grade.unlink()
            else:
                rollback = _temp(grade, old_grade)
                os.replace(rollback, grade)
                temp_paths.remove(rollback)
            if old_score is None:
                if score.exists():
                    score.unlink()
            else:
                rollback = _temp(score, old_score)
                os.replace(rollback, score)
                temp_paths.remove(rollback)
        except Exception:
            # The primary exception remains the stable boundary result; a
            # subsequent cache read rejects any digest mismatch.
            pass
        raise ScoreContractError("score_atomic_write_failed", "scoring.sidecar_identity") from exc
    finally:
        for tmp in temp_paths:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
