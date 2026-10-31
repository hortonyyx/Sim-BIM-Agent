"""Single judge-side dispatch seam used by run-stage and score CLI.

The module intentionally owns dispatch/normalization only.  Legacy policy
continues to be supplied by the preserved legacy scorer, while v3 elevation
observations are projected exclusively through reviewed score bindings.
"""
from __future__ import annotations

import hashlib
import logging
import warnings
from dataclasses import dataclass
from typing import Callable, Literal

from src.agent.correction.claims import CLAIM_HOST
from src.agent.judge.elevation_score import (
    ProjectedElevationObservation,
    TypedElevationObservation,
    project_typed_elevation_observation,
)
from src.agent.judge.score_schema import (
    C2ScoredPayloadV9, C2ToleranceIdentityV8, ExtraObservationV8,
    HelperIdentityV9, ManifestIdentityV8, NotApplicablePayloadV9,
    ProductIdentityV8, RejectedPayloadV9, ScoreContractError,
    ScoreCriterionV9, ScoreIdentityV9, ScorePayloadV9, SegmentScoreRowV8,
    CORRECTION_OPENING_MATCHER_HELPER_VERSION,
    READING_OPENING_MATCHER_HELPER_VERSION, SEGMENT_SCORER_HELPER_VERSION,
    canonical_sha256, decide_score_capability, empty_visibility_counts_v1,
    finalize_score_sidecar_v9,
)
from src.agent.judge.score_schema import ElevationScoreViewBindingV1
from src.agent.judge.identity_provenance import (
    identity_contract_for_segment_scorer,
    raise_identity_conflict,
)

_logger = logging.getLogger(__name__)


def _raise_score_input_contract(
    code: str,
    *,
    reason: str,
    **context: object,
) -> None:
    """Send pure schema/cryptographic identity facts through the arbiter."""
    raise_identity_conflict(
        code,
        predicate="typed_score_input_contract",
        reason=reason,
        side=str(context.pop("side", "product")),
        floor_id=str(context.pop("floor_id", "")),
        _exact_error_context=True,
        **context,
    )


def normalize_typed_elevation_observations(*, payload: object, score_bindings: object) -> tuple[ProjectedElevationObservation, ...]:
    """Normalize the CLI/product boundary before scoring (never trust product mirror)."""
    if not isinstance(payload, dict):
        _raise_score_input_contract(
            "score_product_identity_invalid",
            reason="elevation_payload_not_object",
        )
    raw_items = payload.get("elevation_observations", ())
    if not isinstance(raw_items, list):
        _raise_score_input_contract(
            "score_product_identity_invalid",
            reason="elevation_observations_not_list",
        )
    bindings = getattr(score_bindings, "bindings", ())
    by_input = {item.input_id: item for item in bindings if isinstance(item, ElevationScoreViewBindingV1)}
    projected: list[ProjectedElevationObservation] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="elevation_observation_not_object",
            )
        try:
            local = raw["local_x_interval"]
            z = raw.get("z_interval")
            observation = TypedElevationObservation(
                observation_id=str(raw["observation_id"]), source_input_id=str(raw["source_input_id"]),
                floor_id=str(raw["floor_id"]), kind=raw["kind"], facade_family=raw["facade_family"],
                local_x_interval=(float(local[0]), float(local[1])),
                z_interval=None if z is None else (float(z[0]), float(z[1])),
            )
            binding = by_input[observation.source_input_id]
        except (KeyError, TypeError, ValueError) as exc:
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="invalid_typed_elevation_observation",
                parse_error_type=type(exc).__name__,
            )
        projected.append(project_typed_elevation_observation(
            observation=observation, binding=binding,
            direction_semantics=raw.get("direction_semantics", "building_axis"),
        ))
    return tuple(projected)


@dataclass(frozen=True)
class TypedScoreResult:
    identity: ScoreIdentityV9
    payload: ScorePayloadV9
    sidecar: object
    grade_png: bytes


def _opening_observations(*, payload: dict, score_bindings: object):
    """Normalize plan world observations plus reviewed-frame elevation inputs."""
    from src.agent.judge.opening_claim_score import OpeningObservation
    values: list[OpeningObservation] = []
    for raw in payload.get("openings", ()):
        if not isinstance(raw, dict):
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="opening_observation_not_object",
            )
        try:
            span = raw["world_along_interval"]
            z = raw.get("z_interval")
            values.append(OpeningObservation(
                id=str(raw["observation_id"]), floor_id=str(raw["floor_id"]), kind=raw["kind"],
                facade_segment_id=str(raw["facade_segment_id"]),
                world_along_interval=(float(span[0]), float(span[1])), source_view_id=str(raw["source_input_id"]),
                room_id=raw.get("declared_room_id"), z_interval=None if z is None else (float(z[0]), float(z[1])),
                channel=raw.get("channel", "plan"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="invalid_opening_observation",
                parse_error_type=type(exc).__name__,
            )
    for projected in normalize_typed_elevation_observations(payload=payload, score_bindings=score_bindings):
        # C2 input carries a declared product segment; the projection helper
        # owns coordinates only and intentionally does not infer a host.
        raw = next(item for item in payload["elevation_observations"]
                   if item.get("observation_id") == projected.observation_id)
        values.append(OpeningObservation(
            id=projected.observation_id, floor_id=projected.floor_id, kind=projected.kind,
            facade_segment_id=str(raw["facade_segment_id"]), world_along_interval=projected.world_along_interval,
            source_view_id=projected.source_input_id, room_id=raw.get("declared_room_id"),
            z_interval=projected.z_interval, channel="elevation",
        ))
    return tuple(values)


def _resolve_facade_product_to_gt(*, geometry, gt, product_floor_to_gt_floor: dict[str, str]) -> dict[str, str]:
    """Map product facade spans to GT facade spans (one-way, product -> answer).

    §5-B: a product facade span maps only to the UNIQUE GT facade span that
    contains it (floor + family + along-containment).  A span straddling two GT
    facades has >1 candidate and is deliberately NOT mapped -- "take first" would
    silently mis-bind a window to the wrong wall with no test going red.  No
    mapping makes the window fail closed (unmatched), never silently wrong.

    F-90 (2026-08-25 dispatch): "floor" here means a product `FacadeSegment.
    floor_id` -- a namespace the GT never assigned and has no reason to share
    (real sm25: product "floor_1"/"floor_2" vs GT "F1"/"F2"). Comparing the
    two floor_id strings directly (the prior code) can only match by
    coincidence, so the caller resolves `product_floor_to_gt_floor` from the
    same window-provenance evidence used for the plan-source lookup and hands
    it in here; a product floor absent from that map (no window evidence
    reaches it) contributes no candidates, which is the function's existing
    fail-closed behaviour for an unresolved span, not a new case.
    """
    mapping: dict[str, str] = {}
    gt_facades = tuple(segment for floor in gt.floors for segment in floor.boundary_segments)
    for product_segment in geometry.facade_segments:
        gt_floor_id = product_floor_to_gt_floor.get(product_segment.floor_id)
        if gt_floor_id is None:
            continue
        candidates = tuple(target for target in gt_facades
            if target.floor_id == gt_floor_id
            and target.facade_family == product_segment.facade_family
            and target.world_along_interval.lo <= product_segment.world_along_interval.lo
            and product_segment.world_along_interval.hi <= target.world_along_interval.hi)
        if len(candidates) == 1:
            mapping[product_segment.id] = candidates[0].id
    return mapping


def _derive_window_floor_plan_sources(
    *, geometry, score_bindings, resolver_inputs
) -> dict[str, str]:
    """Derive per-window floor -> plan-input evidence for corroboration.

    The verified resolver-input catalog supplies the total floor bridge,
    including zero-window floors.  This narrower witness independently checks
    every window's declared host source and fails closed on contradictory or
    malformed evidence.
    """
    plan_input_ids = {item.input_id for item in score_bindings.bindings
                      if getattr(item, "kind", None) == "plan"}
    source_by_locator = {
        row.source_locator: row for row in resolver_inputs.source_windows
    }
    host_inputs_by_window: dict[str, set[str]] = {}
    for link in resolver_inputs.claim_links:
        if link.claim != CLAIM_HOST:
            continue
        source = source_by_locator.get(link.source_locator)
        if source is None:
            raise ScoreContractError(
                "score_view_binding_invalid",
                "scoring.view_bindings",
                context={
                    "window_id": link.window_id,
                    "reason": "window_host_claim_ambiguous_source",
                    "candidate_inputs": [],
                },
            )
        host_inputs_by_window.setdefault(link.window_id, set()).add(
            source.source_input_id
        )
    floor_plan_source: dict[str, str] = {}
    for window in geometry.windows:
        provenance = window.provenance or {}
        host = provenance.get(CLAIM_HOST)
        if host is None or not host.source_ids:
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings",
                                     context={"floor_id": window.floor_id, "window_id": window.id,
                                              "reason": "window_host_claim_missing_source_ids"})
        # The proof verifier has already translated both legal producer forms
        # (`src:<sha256>` and `<view>/<observation>`) into canonical claim
        # links.  Consume that authenticated catalog instead of reparsing the
        # raw producer string or treating a locator hash as an input id.
        candidate_inputs = host_inputs_by_window.get(window.id, set())
        if len(candidate_inputs) != 1:
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings",
                                     context={"floor_id": window.floor_id, "window_id": window.id,
                                              "reason": "window_host_claim_ambiguous_source",
                                              "candidate_inputs": sorted(candidate_inputs)})
        (input_id,) = candidate_inputs
        if input_id not in plan_input_ids:
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings",
                                     context={"floor_id": window.floor_id, "window_id": window.id,
                                              "reason": "window_host_source_not_a_registered_plan_input",
                                              "input_id": input_id})
        existing = floor_plan_source.get(window.floor_id)
        if existing is not None and existing != input_id:
            raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings",
                                     context={"floor_id": window.floor_id,
                                              "reason": "floor_id_maps_to_multiple_plan_inputs",
                                              "input_ids": sorted({existing, input_id})})
        floor_plan_source[window.floor_id] = input_id
    return floor_plan_source


def _derive_correction_floor_plan_sources(
    *, geometry, score_bindings, resolver_inputs
) -> dict[str, str]:
    """Build the total product-floor -> plan-input bridge from verified data.

    The resolver-input verifier already freezes plan ``floor_ref`` values and
    checks that they form a total 1..N contract with product floors ranked by
    ``z_floor``.  Reconstruct that reviewed mapping here so zero-window floors
    remain scorable.  Per-window host claims are still checked and must agree
    with the catalog; they are corroboration, not the only source of totality.
    """
    plan_entries = tuple(
        entry
        for entry in resolver_inputs.view_manifest.required_entries()
        if entry.view_type == "plan"
    )
    product_floors = tuple(
        sorted(geometry.floors, key=lambda floor: floor.z_floor)
    )
    if len(plan_entries) != len(product_floors):
        raise ScoreContractError(
            "score_view_binding_invalid",
            "scoring.view_bindings",
            context={"reason": "verified_plan_floor_catalog_not_total"},
        )
    plan_by_ref = {entry.floor_ref: entry.input_id for entry in plan_entries}
    registered_plan_inputs = {
        item.input_id
        for item in score_bindings.bindings
        if getattr(item, "kind", None) == "plan"
    }
    floor_plan_source: dict[str, str] = {}
    for floor_ref, floor in enumerate(product_floors, start=1):
        input_id = plan_by_ref.get(floor_ref)
        if input_id is None or input_id not in registered_plan_inputs:
            raise ScoreContractError(
                "score_view_binding_invalid",
                "scoring.view_bindings",
                context={
                    "floor_id": floor.id,
                    "reason": "verified_plan_floor_not_registered_for_scoring",
                    "input_id": input_id,
                },
            )
        floor_plan_source[floor.id] = input_id

    window_sources = _derive_window_floor_plan_sources(
        geometry=geometry,
        score_bindings=score_bindings,
        resolver_inputs=resolver_inputs,
    )
    for floor_id, input_id in window_sources.items():
        if floor_plan_source.get(floor_id) != input_id:
            raise ScoreContractError(
                "score_view_binding_invalid",
                "scoring.view_bindings",
                context={
                    "floor_id": floor_id,
                    "reason": "window_host_disagrees_with_verified_plan_floor_catalog",
                    "input_ids": sorted(
                        {input_id, floor_plan_source.get(floor_id) or "<missing>"}
                    ),
                },
            )
    return floor_plan_source


def _score_helper_identity_v9(
    *,
    stage: Literal["reading", "correction"],
    va_helper: str,
    claims_contract: str,
) -> HelperIdentityV9:
    """Return the stage-specific helper release bound into score caches.

    Correction scoring consumes judge-owned floor/source normalization before
    plan and opening matching.  Its helper release must therefore be
    independent from reading's opening assignment release: otherwise a
    correction normalization change can retain the exact same cache identity
    and silently reuse a pre-change sidecar (F-102).
    """
    opening_matcher = (
        READING_OPENING_MATCHER_HELPER_VERSION
        if stage == "reading"
        else CORRECTION_OPENING_MATCHER_HELPER_VERSION
    )
    return HelperIdentityV9(
        scorer_schema="9",
        segment_scorer=SEGMENT_SCORER_HELPER_VERSION,
        opening_matcher=opening_matcher,
        gt_to_va_adapter="b4b_gt_to_va_v1",
        denominator_helper="b4b_denominator_v1",
        grade_renderer="b4b_grade_png_v2",
        va_helper=va_helper,
        vg_helper="facade_visibility_v1",
        claims_contract=claims_contract,
        reading_contract_detector="reading_contract_detector_v2",
        reading_adapter="reading_typed_adapter_v2",
        reading_source_applicability="reading_source_applicability_v2",
    )


def _normalize_correction_plan_floor_ids(
    *, observations, product_floor_to_gt_floor: dict[str, str]
):
    """Translate product plan segments at the judge normalization boundary.

    `match_plan_segments` deliberately compares exact floor ids inside one
    namespace.  Correction output and GT use independent namespaces, so the
    explicit evidence-derived bridge must be applied before that matcher; the
    matcher itself must never guess by spelling, case, or ordering.
    """
    from src.agent.judge.segment_score import PlanSegment

    normalized: list[PlanSegment] = []
    for observation in observations:
        gt_floor_id = product_floor_to_gt_floor.get(observation.floor_id)
        if gt_floor_id is None:
            raise ScoreContractError(
                "score_view_binding_invalid",
                "scoring.view_bindings",
                context={
                    "floor_id": observation.floor_id,
                    "reason": "product_floor_has_no_explicit_gt_floor_binding",
                },
            )
        normalized.append(
            PlanSegment(
                observation.key,
                gt_floor_id,
                observation.p1,
                observation.p2,
                observation.zone_ids,
                observation.source_ids,
                observation.exterior,
            )
        )
    return tuple(normalized)


def score_typed_attempt(*, gt_identity, gt, stage: Literal["reading", "correction"],
                        product_payload: dict, product_identity: ProductIdentityV8,
                        base_view_manifest, score_bindings, completeness_overlay, c2_config,
                        window_host_proof=None,
                        reading_exam_scope_input_ids: set[str] | None = None,
                        reading_exam_scope_source: str | None = None) -> TypedScoreResult:
    """Assemble the Phase A/B/C engines into the one real C2 score service."""
    from src.agent.correction.facade_applicability import FACADE_APPLICABILITY_HELPER_VERSION
    from src.agent.correction.claims import CLAIMS_VOCAB_VERSION
    from src.agent.judge.opening_claim_score import (
        OpeningObservation, assign_openings, bind_correction_window_segment,
        build_absence_opening_claims, derive_absence_ledger, derive_product_ledger,
        derive_reference_ledger, gt_openings_to_va_claims, gt_to_va_visibility, score_opening_claims_v3,
        summarize_claim_rows,
    )
    from src.agent.judge.score_inputs import (
        build_effective_view_manifest,
        materialize_va_elevation_bindings,
        source_view_to_gt_view_ids,
    )
    from src.agent.judge.score_policy import c2_v3_score_policy
    from src.agent.judge.segment_score import (coerce_plan_observations,
                                               extract_correction_plan_segments, extract_gt_plan_segments,
                                               match_plan_segments)

    if gt is None:
        _raise_score_input_contract(
            "score_gt_identity_invalid",
            reason="missing_ground_truth",
            side="gt",
        )
    effective = build_effective_view_manifest(base=base_view_manifest, overlay=completeness_overlay)
    capability = decide_score_capability(gt_identity=gt_identity, stage=stage,
                                          product_schema=product_identity.output_schema,
                                          view_manifest=effective,
                                          product_artifact_contract=product_identity.artifact_contract,
                                          score_view_bindings_sha256=score_bindings.content_sha256)
    if capability.path != "c2_v3":
        raise ScoreContractError("score_unsupported_combination", "scoring.capability",
                                 context={"reason": capability.reason or capability.path})
    tolerance = C2ToleranceIdentityV8(profile_kind="judge_score_config_v1", values=c2_config,
                                      content_sha256=canonical_sha256(c2_config.model_dump(mode="json")))
    manifest_identity = ManifestIdentityV8(
        base_view_manifest_sha256=base_view_manifest.content_sha256,
        effective_view_manifest_sha256=effective.content_sha256,
        case_metadata_sha256=base_view_manifest.case_metadata_sha256,
        completeness_ruleset=effective.completeness_ruleset_version,
        completeness_overlay_sha256=None if completeness_overlay is None else completeness_overlay.content_sha256,
        score_view_bindings_sha256=score_bindings.content_sha256,
    )
    helpers = _score_helper_identity_v9(
        stage=stage,
        va_helper=FACADE_APPLICABILITY_HELPER_VERSION,
        claims_contract=CLAIMS_VOCAB_VERSION,
    )
    if stage == "reading":
        from src.agent.judge.reading_typed_score import (
            assemble_reading_score,
        )

        assembly = assemble_reading_score(
            gt_identity=gt_identity,
            gt=gt,
            product_payload=product_payload,
            product_identity=product_identity,
            base_view_manifest=base_view_manifest,
            effective_view_manifest=effective,
            score_bindings=score_bindings,
            c2_config=c2_config,
            capability=capability,
            tolerance=tolerance,
            manifest_identity=manifest_identity,
            helpers=helpers,
            reading_exam_scope_input_ids=reading_exam_scope_input_ids,
            reading_exam_scope_source=reading_exam_scope_source,
        )
        # The renderer consumes only typed judge rows and GT geometry.  Product
        # facade declarations and raw stroke coordinates are not reachable.
        import sys
        from pathlib import Path

        scripts = str(
            Path(__file__).resolve().parents[3] / "scripts" / "tool_scripts"
        )
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from render_grade import render_score_grade_png

        png = render_score_grade_png(
            gt=gt,
            identity=assembly.identity,
            payload=assembly.payload,
        )
        sidecar = finalize_score_sidecar_v9(
            identity=assembly.identity,
            payload=assembly.payload,
            grade_png=png,
            certificates=assembly.certificates,
            ledger_counts=assembly.ledger_counts,
        )
        return TypedScoreResult(
            identity=assembly.identity,
            payload=assembly.payload,
            sidecar=sidecar,
            grade_png=png,
        )

    from src.agent.judge.certifier import (
        AnalysisCollector,
        DEFAULT_EVALUATOR_REGISTRY,
        certify_and_arbitrate_request,
    )
    identity_analysis = AnalysisCollector()
    gt_segments = extract_gt_plan_segments(gt, _analysis=identity_analysis)
    geometry = None
    floor_plan_source: dict[str, str] = {}
    product_floor_to_gt_floor: dict[str, str] = {}
    if stage == "correction":
        from src.agent.correction.schema import CorrectedGeometryV3
        from src.agent.geometry.build import (
            VerifiedWindowHostProof,
            _resolver_inputs_from_verified_proof,
            _reverify_window_host_proof,
        )
        if not product_identity.accepted or not isinstance(
            window_host_proof, VerifiedWindowHostProof,
        ):
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="official_b5_requires_verified_six_artifact_input",
            )
        try:
            verified_proof = _reverify_window_host_proof(window_host_proof)
        except ValueError as exc:
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="b5_window_host_proof_invalid",
                proof_error_type=type(exc).__name__,
            )
        if hashlib.sha256(verified_proof.raw_output_bytes).hexdigest() != product_identity.output_sha256:
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="b5_output_hash_mismatch",
            )
        geometry = CorrectedGeometryV3.model_validate_json(verified_proof.raw_output_bytes)
        if geometry.model_dump(mode="json") != CorrectedGeometryV3.model_validate(product_payload).model_dump(mode="json"):
            _raise_score_input_contract(
                "score_product_identity_invalid",
                reason="b5_product_payload_differs_from_verified_output",
            )
        # F-90/F-102 item 2: establish the product-floor -> plan-input ->
        # GT-floor bridge before plan matching, then translate only the
        # judge-owned PlanSegment observations.  Product geometry and the
        # matcher remain untouched.
        resolver_inputs = _resolver_inputs_from_verified_proof(
            verified_proof
        ).inputs
        floor_plan_source = _derive_correction_floor_plan_sources(
            geometry=geometry,
            score_bindings=score_bindings,
            resolver_inputs=resolver_inputs,
        )
        input_to_gt_floor = {
            item.input_id: item.floor_id
            for item in score_bindings.bindings
            if getattr(item, "kind", None) == "plan"
        }
        product_floor_to_gt_floor = {
            product_floor: input_to_gt_floor[input_id]
            for product_floor, input_id in floor_plan_source.items()
        }
        extracted_plan_observations = extract_correction_plan_segments(
            geometry, _analysis=identity_analysis
        )
        plan_observations = _normalize_correction_plan_floor_ids(
            observations=extracted_plan_observations,
            product_floor_to_gt_floor=product_floor_to_gt_floor,
        )
    else:
        plan_observations = coerce_plan_observations(product_payload.get("segments", ()))
    certify_and_arbitrate_request(
        diagnostics=identity_analysis.diagnostics,
        capabilities=identity_analysis.capabilities,
        evaluator_registry=DEFAULT_EVALUATOR_REGISTRY,
        request_key=(
            getattr(gt_identity, "content_sha256", None),
            product_identity.output_sha256,
            SEGMENT_SCORER_HELPER_VERSION,
            identity_contract_for_segment_scorer(SEGMENT_SCORER_HELPER_VERSION),
        ),
        identity_code="score_product_identity_invalid",
    )
    segment_rows, observation_to_targets = match_plan_segments(targets=gt_segments, observations=plan_observations, config=c2_config)
    # §5-B: product_to_gt is rebuilt from the joint-cutpoint multi-cover result,
    # NOT from a one-to-one assignment.  It is consumed only by window host
    # resolution and opening matching, both of which look it up by
    # facade_segment_id (a product facade span, mapped below to the unique GT
    # facade that contains it).  Wall coverage scores through segment_rows
    # (length denominator, W4), never through this map, so a product wall
    # covering several GT walls cannot mis-bind a window -- the two consumers
    # are deliberately separate.  A product segment covering one GT segment maps
    # straight to it; one covering several (a long product wall over several GT
    # walls) maps to its first sorted target -- it carries no window, so the
    # choice is audit-only and pinned by a deterministic sort, never input order.
    product_to_gt = {obs_key: target_keys[0] for obs_key, target_keys in observation_to_targets.items()}
    product_to_gt.update({item.key: item.key for item in plan_observations if item.key in {target.key for target in gt_segments}})
    if geometry is not None:
        # F-90 (2026-08-25 dispatch): `PlanScoreViewBindingV1.floor_id` is the
        # GT-side floor id ("F1"/"F2" for sm25); a product `floor_id`
        # (`WindowV3.floor_id` / `FacadeSegment.floor_id`) is the PRODUCT-side
        # id ("floor_1"/"floor_2"). The two namespaces are independently
        # assigned by two different producers and were never guaranteed to
        # line up -- comparing them directly (the prior code, both for the
        # window plan-source lookup below AND for `_resolve_facade_product_
        # to_gt`'s floor filter) can only match by coincidence, and in real
        # runs (sm25) it always misses, rejecting every attempt before a
        # single criterion is scored.
        #
        # The total bridge above comes from the already-verified resolver-input
        # catalog: product floor z-rank -> manifest floor_ref -> plan input.
        # Per-window host claims independently corroborate that catalog and
        # fail closed on missing/ambiguous/unregistered/contradictory evidence.
        # Joining through score_bindings (input_id -> GT floor_id) feeds both
        # the facade-span resolver below and the window observations without
        # either consumer guessing at floor identity.
        # §5-B: facade spans are resolved by the one-way containment helper;
        # a multi-span straddle is left unmapped (window fails closed), never
        # silently bound to the first candidate.
        product_to_gt.update(_resolve_facade_product_to_gt(
            geometry=geometry, gt=gt, product_floor_to_gt_floor=product_floor_to_gt_floor))

    if geometry is None:
        observations = _opening_observations(payload=product_payload, score_bindings=score_bindings)
    else:
        converted: list[OpeningObservation] = []
        for window in geometry.windows:
            segment, _method = bind_correction_window_segment(
                window=window,
                segments=geometry.facade_segments,
                allow_temporary_binding=False,
            )
            source = floor_plan_source[window.floor_id]
            # F-90: `OpeningObservation.floor_id` is matched against GT
            # openings by direct equality further down (`_assign_openings_
            # for_source` in opening_claim_score.py) and used to key
            # `floor_refs` for absence classification -- both GT-side
            # consumers. Storing the product's own `window.floor_id` there
            # (the prior code) made every v3 correction window unmatched by
            # construction, silently (no exception): a fully correct answer
            # would score every window a miss. Translate to the GT floor_id
            # here, once, at the one place a v3 window crosses from the
            # product namespace into the GT-matching namespace.
            converted.append(OpeningObservation(id=window.id, floor_id=product_floor_to_gt_floor[window.floor_id],
                kind="window", facade_segment_id=segment.id, world_along_interval=tuple(window.span),
                source_view_id=source, room_id=window.room,
                z_interval=None if window.z is None else tuple(window.z), channel="plan"))
        observations = tuple(converted)
    opening_assignment = assign_openings(targets=tuple(gt.openings), observations=observations,
                                         config=c2_config, product_to_gt_segment=product_to_gt,
                                         source_view_to_gt_view_ids=source_view_to_gt_view_ids(score_bindings))
    visibility = gt_to_va_visibility(gt)
    reference = derive_reference_ledger(gt=gt, bindings=score_bindings, effective_manifest=effective)
    # Product declarations are deliberately derived separately.  A source only
    # remains positive when this attempt actually supplied an observation from
    # that reviewed input; reference denominator data never consults this.
    source_ids = {item.source_view_id for item in observations}
    reference_claims = gt_openings_to_va_claims(gt=gt, bindings=score_bindings, effective_manifest=effective)
    product_claims = tuple(row.model_copy(update={"claims": tuple(claim.model_copy(update={
        "positive_evidence": tuple(item for item in claim.positive_evidence if item.source_input_id in source_ids)
    }) for claim in row.claims)}) for row in reference_claims)
    va_bindings = materialize_va_elevation_bindings(score_bindings=score_bindings, effective_manifest=effective)
    product = derive_product_ledger(visibility=visibility, manifest=effective, elevation_views=va_bindings, openings=product_claims)
    host_resolver = None
    if geometry is not None:
        from src.agent.judge.opening_claim_score import (
            build_correction_host_resolver,
            map_product_cells_to_gt_zones,
        )
        host_resolver = build_correction_host_resolver(
            geometry=geometry,
            product_to_gt_segment=product_to_gt,
            product_to_gt_zone=map_product_cells_to_gt_zones(
                geometry=geometry, gt=gt, product_floor_to_gt_floor=product_floor_to_gt_floor),
            allow_temporary_binding=False,
        )
    rows = score_opening_claims_v3(gt=gt, reference_ledger=reference, product_ledger=product,
                                   assignment=opening_assignment, config=c2_config,
                                   host_resolver=host_resolver)
    summaries = summarize_claim_rows(rows)

    # F-90: `observation.floor_id` is the GT-side id for every OpeningObservation
    # (the v3 correction window loop above translates it through
    # `product_floor_to_gt_floor` at construction time; the v1/v2 reading path
    # already emits GT-consistent floor_id), so `floor_refs` below is keyed by
    # GT floor_id directly -- no product-namespace re-keying needed here.
    floors = {floor.id: index + 1 for index, floor in enumerate(gt.floors)}
    families = {segment.id: segment.facade_family for floor in gt.floors for segment in floor.boundary_segments}
    unmatched = opening_assignment.unmatched_observations
    absence = None
    extras: tuple[ExtraObservationV8, ...] = ()
    if unmatched:
        from src.agent.judge.opening_claim_score import classify_extra_observation
        queries = build_absence_opening_claims(observations=unmatched, floor_refs=floors, segment_families=families,
            output_sha256=product_identity.output_sha256, product_to_gt_segment=product_to_gt,
            trusted_source_views={item.facade_segment_id: (item.source_view_id,) for item in unmatched})
        absence = derive_absence_ledger(visibility=visibility, manifest=effective, elevation_views=va_bindings, openings=queries)
        extras = tuple(ExtraObservationV8(observation_id=item.id,
            result=classify_extra_observation(observation=item, absence_ledger=absence,
                output_sha256=product_identity.output_sha256, gt_segment_id=product_to_gt.get(item.facade_segment_id, item.facade_segment_id)),
            reason=None) for item in unmatched)
    policy = c2_v3_score_policy(claim_rows=rows, segment_rows=segment_rows)
    identity = ScoreIdentityV9(gt=gt_identity, product=product_identity, manifest=manifest_identity,
        helpers=helpers, capability=capability, tolerances=tolerance,
        reading_normalization_sha256=None, source_applicability_sha256=None,
        score_manifest_sha256=effective.content_sha256,
        denominator_basis_sha256=None, denominator_sha256=None,
        reference_applicability_sha256=reference.content_sha256, product_applicability_sha256=product.content_sha256,
        absence_applicability_sha256=None if absence is None else absence.content_sha256)
    serial_segments = tuple(SegmentScoreRowV8(
        target_id=None if row.target is None else row.target.key, observation_id=None if row.observation is None else row.observation.key,
        floor_id=(row.target or row.observation).floor_id, exterior=(row.target or row.observation).exterior,
        status=row.status, axis_alignment_error_m=row.axis_alignment_error_m, position_error_m=row.position_error_m,
        extent_symmetric_difference_m=row.extent_symmetric_difference_m) for row in segment_rows)
    payload = C2ScoredPayloadV9(
        kind="c2_scored",
        channel_applicability=(),
        unmeasurable_observations=0,
        visibility_counts=empty_visibility_counts_v1(),
        segment_rows=serial_segments,
        segment_extras=(),
        opening_source_rows=(),
        claim_rows=rows, claim_summaries=summaries, extras=extras,
        score_criteria=tuple(
            ScoreCriterionV9.model_validate(item.model_dump(mode="json"))
            for item in policy.criteria
        ),
        reference_ledger_sha256=reference.content_sha256, product_ledger_sha256=product.content_sha256,
        absence_ledger_sha256=None if absence is None else absence.content_sha256)
    # The renderer is a judge-side script and consumes the typed document; no
    # legacy bbox transform is reachable on this branch.
    import sys
    from pathlib import Path
    scripts = str(Path(__file__).resolve().parents[3] / "scripts" / "tool_scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from render_grade import render_score_grade_png
    png = render_score_grade_png(gt=gt, identity=identity, payload=payload)
    sidecar = finalize_score_sidecar_v9(identity=identity, payload=payload, grade_png=png,
        ledger_counts=(len(reference.openings), len(product.openings), 0 if absence is None else len(absence.openings)))
    return TypedScoreResult(identity=identity, payload=payload, sidecar=sidecar, grade_png=png)


class TopLevelNotApplicableError(RuntimeError):
    """Raised by strict-profile callers only after NA artifacts are committed."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"top_level_not_applicable:{reason}")


def score_criteria_for_payload(payload: ScorePayloadV9) -> tuple[ScoreCriterionV9, ...]:
    values = getattr(payload, "score_criteria", ())
    return tuple(values)


def strict_payload_violation_reason(payload: ScorePayloadV9) -> str | None:
    """The reason a strict profile must refuse this score, or ``None``.

    2026-08-20 — extends the top-level-NA refusal to the structural case: a
    scored payload whose plan channel is not_applicable because the product
    declared no ``scale_origin`` (a legal SHOULD-level omission) still scores
    every plan target as a miss, i.e. a frame-less zero that reads exactly like
    bad tracing. The ``reading.plan_frame_declared`` FAIL criterion emitted by
    the reading score assembly is the single source of that signal; strict
    callers (``run_stage`` grading, the scoring CLI) raise on it AFTER the
    score artifacts are committed, mirroring ``TopLevelNotApplicableError``'s
    commit-then-raise contract.
    """
    if payload.kind == "not_applicable":
        return payload.reason
    for item in score_criteria_for_payload(payload):
        if (
            item.criterion_id == "reading.plan_frame_declared"
            and item.verdict == "fail"
        ):
            return "plan_frame_unavailable"
        if (
            item.criterion_id == "reading.elevation_mirror_visible"
            and item.verdict == "fail"
        ):
            # Dispatch 2026-08-22 lock 3: a whole-facade reflection fitting GT
            # better than the declared frame means the product or the binding
            # disagrees with the ratified mirror convention. Scoring it
            # silently (as a plain miss) would bury the convention failure.
            return "elevation_mirror_disagreement"
    return None


def _failure_identity(*, typed_request: dict, capability) -> ScoreIdentityV9:
    from src.agent.correction.claims import CLAIMS_VOCAB_VERSION
    from src.agent.correction.facade_applicability import (
        FACADE_APPLICABILITY_HELPER_VERSION,
    )
    from src.agent.judge.score_inputs import build_effective_view_manifest

    base = typed_request["base_view_manifest"]
    overlay = typed_request["completeness_overlay"]
    effective = build_effective_view_manifest(base=base, overlay=overlay)
    config = typed_request["c2_config"]
    tolerance = C2ToleranceIdentityV8(
        profile_kind="judge_score_config_v1",
        values=config,
        content_sha256=canonical_sha256(config.model_dump(mode="json")),
    )
    manifest = ManifestIdentityV8(
        base_view_manifest_sha256=base.content_sha256,
        effective_view_manifest_sha256=effective.content_sha256,
        case_metadata_sha256=base.case_metadata_sha256,
        completeness_ruleset=effective.completeness_ruleset_version,
        completeness_overlay_sha256=(
            None if overlay is None else overlay.content_sha256
        ),
        score_view_bindings_sha256=typed_request["score_bindings"].content_sha256,
    )
    helpers = _score_helper_identity_v9(
        stage=typed_request["stage"],
        va_helper=FACADE_APPLICABILITY_HELPER_VERSION,
        claims_contract=CLAIMS_VOCAB_VERSION,
    )
    return ScoreIdentityV9(
        gt=typed_request["gt_identity"],
        product=typed_request["product_identity"],
        manifest=manifest,
        helpers=helpers,
        capability=capability,
        tolerances=tolerance,
        reading_normalization_sha256=None,
        source_applicability_sha256=None,
        score_manifest_sha256=effective.content_sha256,
        denominator_basis_sha256=None,
        denominator_sha256=None,
        reference_applicability_sha256=None,
        product_applicability_sha256=None,
        absence_applicability_sha256=None,
    )


def _total_failure_result(
    *,
    typed_request: dict,
    error: BaseException,
    disposition: Literal["not_applicable", "rejected", "internal"],
) -> TypedScoreResult:
    from src.agent.judge.score_inputs import build_effective_view_manifest

    base = typed_request["base_view_manifest"]
    effective = build_effective_view_manifest(
        base=base,
        overlay=typed_request["completeness_overlay"],
    )
    product = typed_request["product_identity"]
    capability = decide_score_capability(
        gt_identity=typed_request["gt_identity"],
        stage=typed_request["stage"],
        product_schema=product.output_schema,
        view_manifest=effective,
        product_artifact_contract=product.artifact_contract,
        score_view_bindings_sha256=typed_request[
            "score_bindings"
        ].content_sha256,
    )
    identity = _failure_identity(
        typed_request=typed_request,
        capability=capability,
    )
    counts = empty_visibility_counts_v1(
        scorer_internal_failures=1 if disposition == "internal" else 0
    )
    if disposition == "rejected":
        assert isinstance(error, ScoreContractError)
        payload: ScorePayloadV9 = RejectedPayloadV9(
            kind="rejected",
            error_code=error.code,
            cause_code=error.cause_code,
            gate_id=error.gate_id,
            detail=error.code,
            channel_applicability=(),
            unmeasurable_observations=0,
            visibility_counts=counts,
        )
    else:
        if disposition == "internal":
            reason = "scorer_internal_failure"
        elif capability.reason == "unsupported_reading_contract":
            reason = "unsupported_reading_contract"
        elif capability.reason == "unsupported_gt_profile":
            reason = "unsupported_gt_profile"
        else:
            reason = "unsupported_view_contract"
        payload = NotApplicablePayloadV9(
            kind="not_applicable",
            reason=reason,
            # Keep the established coarse `reason` taxonomy for existing
            # consumers, but retain the originating score-contract code so
            # the official flow can distinguish otherwise identical NA
            # outcomes (F-103).
            detail=(error.code if isinstance(error, ScoreContractError) else reason),
            channel_applicability=(),
            unmeasurable_observations=0,
            visibility_counts=counts,
            score_criteria=(),
        )
    import sys
    from pathlib import Path

    scripts = str(Path(__file__).resolve().parents[3] / "scripts" / "tool_scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from render_grade import render_score_grade_png

    png = render_score_grade_png(
        gt=typed_request["gt"],
        identity=identity,
        payload=payload,
    )
    sidecar = finalize_score_sidecar_v9(
        identity=identity,
        payload=payload,
        grade_png=png,
    )
    return TypedScoreResult(
        identity=identity,
        payload=payload,
        sidecar=sidecar,
        grade_png=png,
    )


def _score_contract_disposition(
    error: ScoreContractError,
    *,
    stage: Literal["reading", "correction"],
) -> Literal["not_applicable", "rejected", "internal"]:
    """Classify only frozen codes and structured ownership fields."""
    trusted_rejections = {
        "score_gt_identity_invalid",
        "score_view_manifest_invalid",
        "score_view_binding_invalid",
        "score_direction_unresolved",
        "score_completeness_input_invalid",
        "score_visibility_adapter_mismatch",
        "score_claim_applicability_invalid",
        "score_denominator_nonconserving",
    }
    product_or_capability_na = {
        "score_product_segment_unresolved",
        "score_match_ambiguous",
        "score_unsupported_combination",
    }
    neutral_identity_codes = {
        "score_identity_non_finite",
        "score_identity_guard_band_ambiguity",
        "score_identity_chain_bridge",
        "score_identity_merge_collapse",
        "score_identity_contract_mismatch",
        "score_identity_support_ambiguous",
    }
    if error.code in trusted_rejections:
        return "rejected"
    if error.code == "score_product_identity_invalid":
        return "rejected" if stage == "correction" else "not_applicable"
    if error.code in product_or_capability_na:
        return "not_applicable"
    if error.code in neutral_identity_codes:
        side = error.context.get("side")
        if side == "gt":
            return "rejected"
        if side == "product":
            return "not_applicable"
    return "internal"


def _report_internal_failure(*, run_profile: str) -> None:
    _logger.exception(
        "reading_typed_scorer_internal_failure",
        extra={"event": "reading_typed_scorer_internal_failure"},
    )
    if run_profile not in {"golden", "regression"}:
        warnings.warn(
            "typed scorer internal failure; emitted not_applicable",
            RuntimeWarning,
            stacklevel=3,
        )


def score_typed_attempt_total(
    *,
    run_profile: str = "exploratory",
    **typed_request,
) -> TypedScoreResult:
    """Total typed boundary; profile-specific raising happens after persistence."""
    try:
        return score_typed_attempt(**typed_request)
    except ScoreContractError as exc:
        disposition = _score_contract_disposition(
            exc,
            stage=typed_request["stage"],
        )
        if disposition == "internal":
            _report_internal_failure(run_profile=run_profile)
        return _total_failure_result(
            typed_request=typed_request,
            error=exc,
            disposition=disposition,
        )
    except Exception as exc:  # noqa: BLE001 - explicit total boundary
        _report_internal_failure(run_profile=run_profile)
        return _total_failure_result(
            typed_request=typed_request,
            error=exc,
            disposition="internal",
        )


def score_attempt_service(*, stage: Literal["0_reading", "1_correction"] | None = None, output: dict | None = None,
                          gt: dict | None = None, grade: object | None = None,
                          legacy_evaluator: Callable[..., dict] | None = None,
                          typed_request: dict | None = None):
    """The one service dispatch for legacy attempt scoring.

    The evaluator is injected so this seam neither copies nor subtly changes
    legacy policy.  v3 callers use the typed normalizer above before entering
    the C2 scorer; no raw-dict fallback is available there.
    """
    if typed_request is not None:
        request = dict(typed_request)
        run_profile = str(request.pop("run_profile", "exploratory"))
        return score_typed_attempt_total(
            run_profile=run_profile,
            **request,
        )
    if legacy_evaluator is None or stage is None or output is None or gt is None:
        raise ValueError("score service requires either typed_request or legacy evaluator inputs")
    return legacy_evaluator(stage, output, gt, grade=grade)
