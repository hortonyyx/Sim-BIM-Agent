"""Judge-side scoring assembly for aggregate reading-stage products.

The adapter in :mod:`reading_typed_adapter` owns product normalization and has
no answer-schema dependency.  This module is the deliberate join point: it
registers certified observations against typed GT, constructs the
answer-derived denominator certificate, and emits source/channel score rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Iterable

from src.agent.judge.opening_claim_score import (
    OpeningAssignment,
    OpeningObservation,
    _app_ref,
    assign_openings,
    derive_reference_ledger,
    summarize_claim_rows,
)
from src.agent.judge.reading_typed_adapter import (
    derive_reading_denominator_v1,
    normalize_reading_attempt,
)
from src.agent.judge.score_inputs import (
    build_reading_score_manifest,
    source_view_to_gt_view_ids,
)
from src.agent.judge.score_schema import (
    CLAIM_ORDER,
    C2ScoredPayloadV9,
    ClaimOutcomeSliceV8,
    ClaimScoreRowV8,
    ClaimValueErrorV8,
    ClosedIntervalV1,
    ExtraObservationV8,
    IntervalV1,
    NotApplicablePayloadV9,
    OpeningSourceScoreRowV1,
    ReadingAmbiguityWitnessV1,
    ReadingChannelSummaryV1,
    ReadingComponentApplicabilityV1,
    ReadingElevationOpeningAuditV1,
    ReadingPlanOpeningAuditV1,
    ReadingPlanSegmentAuditV1,
    ReadingSegmentScoreRowV1,
    ReadingVisibilityCountsV1,
    ScoreCertificatesV1,
    ScoreCriterionV9,
    ScoreIdentityV9,
    SourceApplicabilityCertificateV1,
    canonical_sha256,
    count_unmeasurable_reading_observations,
)
from src.agent.judge.segment_score import (
    PlanSegment,
    extract_gt_plan_segments,
    match_plan_segments,
    plan_segment_support_ambiguities,
)


@dataclass(frozen=True)
class ReadingScoreAssembly:
    identity: ScoreIdentityV9
    payload: C2ScoredPayloadV9 | NotApplicablePayloadV9
    certificates: ScoreCertificatesV1
    ledger_counts: tuple[int, int, int]


ELEVATION_MIRROR_CRITERION_ID = "reading.elevation_mirror_visible"


def elevation_mirror_flip_witnesses(
    *,
    gt,
    score_bindings,
    observations: tuple[OpeningObservation, ...],
    center_tol_m: float,
) -> tuple[dict[str, object], ...]:
    """Dispatch 2026-08-22 lock 3 — the mirror-visibility gate.

    The ratified drawing convention is "each facade drawn as seen from outside"
    (user, 2026-08-22).  If reflecting a whole facade's observed window
    positions about the GT facade's along-wall center makes them fit GT
    STRICTLY better (by more than the judge's own opening center tolerance)
    than the declared frame does, the product or the binding disagrees with the
    mirror convention.  That must surface as a FAIL criterion, never as a
    silently-scored low result.

    Cost = sum over this input's elevation window observations of the distance
    from the observation center to the nearest GT window center on the same
    facade family and floors.  The reflection center comes from GT segment
    extents only — it is independent of both the product and the binding's
    ``along_origin``.  A symmetric window layout reflects onto itself, the two
    costs tie, and the gate correctly stays silent: symmetry carries no
    direction evidence either way.
    """
    from src.agent.judge.score_schema import ElevationScoreViewBindingV1

    segment_families = {
        segment.id: segment.facade_family
        for floor in gt.floors
        for segment in floor.boundary_segments
    }
    witnesses: list[dict[str, object]] = []
    for binding in score_bindings.bindings:
        if not isinstance(binding, ElevationScoreViewBindingV1):
            continue
        local = tuple(
            item
            for item in observations
            if item.channel == "elevation"
            and item.source_view_id == binding.input_id
        )
        if not local:
            continue
        targets = tuple(
            opening
            for opening in gt.openings
            if opening.kind == "window"
            and opening.floor_id in set(binding.floor_ids)
            and segment_families.get(opening.boundary_segment_id)
            == binding.resolved_building_direction
        )
        if not targets:
            continue
        extents = [
            segment.world_along_interval
            for floor in gt.floors
            if floor.id in set(binding.floor_ids)
            for segment in floor.boundary_segments
            if segment.facade_family == binding.resolved_building_direction
        ]
        center = (
            min(interval.lo for interval in extents)
            + max(interval.hi for interval in extents)
        ) / 2.0
        target_centers = tuple(
            (item.world_along_interval.lo + item.world_along_interval.hi) / 2.0
            for item in targets
        )
        as_is_cost = sum(
            min(abs(obs_center - t) for t in target_centers)
            for obs_center in (
                (item.world_along_interval[0] + item.world_along_interval[1]) / 2.0
                for item in local
            )
        )
        flipped_cost = sum(
            min(abs(2.0 * center - obs_center - t) for t in target_centers)
            for obs_center in (
                (item.world_along_interval[0] + item.world_along_interval[1]) / 2.0
                for item in local
            )
        )
        if flipped_cost <= as_is_cost - center_tol_m:
            witnesses.append(
                {
                    "input_id": binding.input_id,
                    "observation_count": len(local),
                    "as_is_center_cost_m": as_is_cost,
                    "flipped_center_cost_m": flipped_cost,
                    "reflection_center_m": center,
                }
            )
    return tuple(witnesses)


def _visibility_counts(certificate) -> ReadingVisibilityCountsV1:
    return ReadingVisibilityCountsV1(
        nonzero_plan_origins=sum(
            item.nonzero_origin for item in certificate.plan_frames
        ),
        project_convention_vertical_datums=sum(
            item.source == "project_convention_2026_07_25"
            for item in certificate.vertical_datums
        ),
        multiple_plan_view_floor_components=sum(
            "multiple_plan_views_per_floor_unsupported" in item.reasons
            for item in certificate.component_applicability
        ),
        elevation_local_x_sense_disagreements=len(
            certificate.elevation_frame_disagreements
        ),
        scorer_internal_failures=0,
    )


def _channel_summaries(
    components: Iterable[ReadingComponentApplicabilityV1],
) -> tuple[ReadingChannelSummaryV1, ...]:
    rows = tuple(components)
    output: list[ReadingChannelSummaryV1] = []
    for channel in ("plan", "elevation"):
        # Channel status describes only components whose denominator disposition
        # is ``score``.  Trusted-input ``filter`` rows were never read in this
        # exam; other dispositions remain visible in the component certificate
        # but do not redefine the measured channel summary.
        local = tuple(
            item
            for item in rows
            if item.channel == channel
            and item.denominator_disposition == "score"
        )
        applicable = tuple(
            sorted({item.component for item in local if item.status == "applicable"})
        )
        unavailable = tuple(
            sorted(
                {
                    item.component
                    for item in local
                    if item.status == "not_applicable"
                }
            )
        )
        if not local or not applicable:
            status = "not_applicable"
        elif unavailable:
            status = "partially_applicable"
        else:
            status = "applicable"
        output.append(
            ReadingChannelSummaryV1(
                channel=channel,
                status=status,
                source_input_ids=tuple(
                    sorted({item.source_input_id for item in local})
                ),
                applicable_components=applicable,
                not_applicable_components=unavailable,
                reasons=tuple(
                    sorted(
                        {
                            reason
                            for item in local
                            for reason in item.reasons
                        }
                    )
                ),
            )
        )
    return tuple(output)


def _component_map(
    components: Iterable[ReadingComponentApplicabilityV1],
) -> dict[tuple[str, str], ReadingComponentApplicabilityV1]:
    return {
        (item.source_input_id, item.component): item for item in components
    }


def _na_component(
    item: ReadingComponentApplicabilityV1,
    *,
    reason: str,
) -> ReadingComponentApplicabilityV1:
    return ReadingComponentApplicabilityV1(
        source_input_id=item.source_input_id,
        channel=item.channel,
        component=item.component,
        floor_ids=item.floor_ids,
        status="not_applicable",
        reasons=(reason,),
        cause_class="judge_ambiguity",
        denominator_disposition="retain_as_miss",
        observation_count=0,
        transform_sha256=None,
    )


def _point_line_distance(point, p1, p2) -> float:
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = hypot(dx, dy)
    if length == 0.0:
        return hypot(point[0] - p1[0], point[1] - p1[1])
    return abs(
        dy * point[0]
        - dx * point[1]
        + p2[0] * p1[1]
        - p2[1] * p1[0]
    ) / length


def _interval_overlap_or_point(
    left: tuple[float, float],
    right: tuple[float, float],
) -> bool:
    return max(left[0], right[0]) <= min(left[1], right[1])


def _plan_opening_candidates(
    observation: ReadingPlanOpeningAuditV1,
    *,
    boundaries,
    position_tolerance: float,
) -> tuple[tuple[object, tuple[float, float]], ...]:
    vertices = tuple((item.x, item.y) for item in observation.world_vertices)
    xs = tuple(item[0] for item in vertices)
    ys = tuple(item[1] for item in vertices)
    candidates: list[tuple[object, tuple[float, float]]] = []
    for segment in boundaries:
        if segment.floor_id != observation.floor_id:
            continue
        p1 = tuple(segment.p1)
        p2 = tuple(segment.p2)
        if segment.facade_family in {"North", "South"}:
            projected = (min(xs), max(xs))
            support_coordinate = p1[1]
            rectangle_intersects = (
                min(ys) - position_tolerance
                <= support_coordinate
                <= max(ys) + position_tolerance
                and _interval_overlap_or_point(
                    projected,
                    (min(p1[0], p2[0]), max(p1[0], p2[0])),
                )
            )
        else:
            projected = (min(ys), max(ys))
            support_coordinate = p1[0]
            rectangle_intersects = (
                min(xs) - position_tolerance
                <= support_coordinate
                <= max(xs) + position_tolerance
                and _interval_overlap_or_point(
                    projected,
                    (min(p1[1], p2[1]), max(p1[1], p2[1])),
                )
            )
        if observation.geometry_kind == "rect":
            on_support = rectangle_intersects
        else:
            on_support = all(
                _point_line_distance(point, p1, p2)
                <= position_tolerance
                for point in vertices
            )
        target_span = (
            segment.world_along_interval.lo,
            segment.world_along_interval.hi,
        )
        if on_support and _interval_overlap_or_point(projected, target_span):
            candidates.append((segment, projected))
    return tuple(sorted(candidates, key=lambda item: item[0].id))


def _elevation_segment_candidates(observation, boundaries):
    span = (
        observation.world_along_interval.lo,
        observation.world_along_interval.hi,
    )
    return tuple(
        sorted(
            (
                segment
                for segment in boundaries
                if segment.floor_id == observation.floor_id
                and segment.facade_family == observation.facade_family
                and _interval_overlap_or_point(
                    span,
                    (
                        segment.world_along_interval.lo,
                        segment.world_along_interval.hi,
                    ),
                )
            ),
            key=lambda item: item.id,
        )
    )


def _opening_observations(
    *,
    gt,
    certificate,
    score_bindings,
    config,
) -> tuple[
    tuple[OpeningObservation, ...],
    OpeningAssignment,
    tuple[ExtraObservationV8, ...],
    tuple[ReadingComponentApplicabilityV1, ...],
    tuple[ReadingAmbiguityWitnessV1, ...],
]:
    components = _component_map(certificate.component_applicability)
    boundaries = tuple(
        segment
        for floor in gt.floors
        for segment in floor.boundary_segments
    )
    values: list[OpeningObservation] = []
    extras: list[ExtraObservationV8] = []
    witnesses: list[ReadingAmbiguityWitnessV1] = []

    for audit in certificate.observations:
        if isinstance(audit, ReadingPlanOpeningAuditV1):
            component = components[(audit.source_input_id, "plan_openings")]
            if component.status != "applicable":
                continue
            candidates = _plan_opening_candidates(
                audit,
                boundaries=boundaries,
                position_tolerance=config.plan_position_tol_m,
            )
            if len(candidates) > 1:
                witnesses.append(
                    ReadingAmbiguityWitnessV1(
                        source_input_id=audit.source_input_id,
                        component="plan_openings",
                        floor_ids=(audit.floor_id,),
                        observation_ids=(audit.observation_id,),
                        candidate_target_ids=tuple(
                            item[0].id for item in candidates
                        ),
                        objective_preimage_sha256=canonical_sha256(
                            {
                                "observation_id": audit.observation_id,
                                "candidate_segment_ids": tuple(
                                    item[0].id for item in candidates
                                ),
                                "position_tolerance": (
                                    config.plan_position_tol_m
                                ),
                            }
                        ),
                        reason="multiple_support_lines",
                    )
                )
                continue
            if not candidates:
                extras.append(
                    ExtraObservationV8(
                        observation_id=audit.observation_id,
                        result="not_applicable",
                        reason="unresolved_absence_coverage",
                    )
                )
                continue
            segment, span = candidates[0]
            values.append(
                OpeningObservation(
                    id=audit.observation_id,
                    floor_id=audit.floor_id,
                    kind="window",
                    facade_segment_id=segment.id,
                    world_along_interval=span,
                    source_view_id=audit.source_input_id,
                    z_interval=None,
                    channel="plan",
                )
            )
        elif isinstance(audit, ReadingElevationOpeningAuditV1):
            component = components[
                (audit.source_input_id, "elevation_opening_xy")
            ]
            if component.status != "applicable":
                continue
            candidates = _elevation_segment_candidates(audit, boundaries)
            if len(candidates) > 1:
                witnesses.append(
                    ReadingAmbiguityWitnessV1(
                        source_input_id=audit.source_input_id,
                        component="elevation_opening_xy",
                        floor_ids=(audit.floor_id,),
                        observation_ids=(audit.observation_id,),
                        candidate_target_ids=tuple(
                            item.id for item in candidates
                        ),
                        objective_preimage_sha256=canonical_sha256(
                            {
                                "observation_id": audit.observation_id,
                                "candidate_segment_ids": tuple(
                                    item.id for item in candidates
                                ),
                            }
                        ),
                        reason="multiple_support_lines",
                    )
                )
                continue
            if not candidates:
                extras.append(
                    ExtraObservationV8(
                        observation_id=audit.observation_id,
                        result="not_applicable",
                        reason="unresolved_absence_coverage",
                    )
                )
                continue
            values.append(
                OpeningObservation(
                    id=audit.observation_id,
                    floor_id=audit.floor_id,
                    kind="window",
                    facade_segment_id=candidates[0].id,
                    world_along_interval=(
                        audit.world_along_interval.lo,
                        audit.world_along_interval.hi,
                    ),
                    source_view_id=audit.source_input_id,
                    z_interval=(
                        None
                        if audit.z_interval is None
                        else (audit.z_interval.lo, audit.z_interval.hi)
                    ),
                    channel="elevation",
                )
            )

    mapping = source_view_to_gt_view_ids(score_bindings)
    product_to_gt = {
        segment.id: segment.id for segment in boundaries
    }
    matched = []
    unmatched = []
    by_source: dict[str, list[OpeningObservation]] = {}
    for item in values:
        by_source.setdefault(item.source_view_id, []).append(item)
    for source, local in sorted(by_source.items()):
        try:
            result = assign_openings(
                targets=tuple(
                    item for item in gt.openings if item.kind == "window"
                ),
                observations=tuple(local),
                config=config,
                product_to_gt_segment=product_to_gt,
                source_view_to_gt_view_ids=mapping,
            )
        except Exception as exc:
            from src.agent.judge.score_schema import ScoreContractError

            if not isinstance(exc, ScoreContractError) or (
                exc.code != "score_match_ambiguous"
            ):
                raise
            channel = local[0].channel
            affected = (
                ("plan_openings",)
                if channel == "plan"
                else ("elevation_opening_xy", "elevation_opening_z")
            )
            for component_name in affected:
                key = (source, component_name)
                components[key] = _na_component(
                    components[key],
                    reason="judge_opening_assignment_ambiguous",
                )
            witnesses.append(
                ReadingAmbiguityWitnessV1(
                    source_input_id=source,
                    component=affected[0],
                    floor_ids=tuple(
                        sorted({item.floor_id for item in local})
                    ),
                    observation_ids=tuple(sorted(item.id for item in local)),
                    candidate_target_ids=tuple(
                        sorted(
                            {
                                target.id
                                for target in gt.openings
                                if target.kind == "window"
                                and target.floor_id
                                in {item.floor_id for item in local}
                            }
                        )
                    ),
                    objective_preimage_sha256=canonical_sha256(exc.context),
                    reason="multiple_equal_opening_assignments",
                )
            )
            continue
        matched.extend(result.matched)
        unmatched.extend(result.unmatched_observations)
    extras.extend(
        ExtraObservationV8(
            observation_id=item.id,
            result="not_applicable",
            reason="unresolved_absence_coverage",
        )
        for item in unmatched
    )
    assignment = OpeningAssignment(
        matched=tuple(matched),
        unmatched_targets=(),
        unmatched_observations=tuple(unmatched),
    )
    return (
        tuple(values),
        assignment,
        tuple(sorted(extras, key=lambda item: item.observation_id)),
        tuple(
            sorted(
                components.values(),
                key=lambda item: (
                    item.source_input_id,
                    item.component,
                    item.floor_ids,
                ),
            )
        ),
        tuple(
            sorted(
                witnesses,
                key=lambda item: (
                    item.source_input_id,
                    item.component,
                    item.observation_ids,
                ),
            )
        ),
    )


def _segment_rows(
    *,
    gt,
    certificate,
    components: tuple[ReadingComponentApplicabilityV1, ...],
    denominator_atoms,
    config,
) -> tuple[
    tuple[ReadingSegmentScoreRowV1, ...],
    tuple[ReadingComponentApplicabilityV1, ...],
    tuple[ReadingAmbiguityWitnessV1, ...],
]:
    component_by_key = _component_map(components)
    targets = extract_gt_plan_segments(gt)
    denominator_target_ids = {
        item.target_id
        for item in denominator_atoms
        if item.component == "plan_segments"
    }
    scored_targets = tuple(
        item for item in targets if item.key in denominator_target_ids
    )
    observations = tuple(
        PlanSegment(
            key=item.observation_id,
            floor_id=item.floor_id,
            p1=(item.world_p1.x, item.world_p1.y),
            p2=(item.world_p2.x, item.world_p2.y),
            exterior=False,
        )
        for item in certificate.observations
        if isinstance(item, ReadingPlanSegmentAuditV1)
        and component_by_key[
            (item.source_input_id, "plan_segments")
        ].status
        == "applicable"
    )
    rows = []
    witnesses: list[ReadingAmbiguityWitnessV1] = []
    for floor_id in sorted({item.floor_id for item in targets}):
        floor_targets = tuple(
            item for item in scored_targets if item.floor_id == floor_id
        )
        floor_observations = tuple(
            item for item in observations if item.floor_id == floor_id
        )
        ambiguities = plan_segment_support_ambiguities(
            targets=floor_targets,
            observations=floor_observations,
            config=config,
        )
        ambiguous_ids = {item.observation_key for item in ambiguities}
        observations_by_id = {item.key: item for item in floor_observations}
        for ambiguity in ambiguities:
            observation = observations_by_id[ambiguity.observation_key]
            witnesses.append(
                ReadingAmbiguityWitnessV1(
                    source_input_id=next(
                        item.source_input_id
                        for item in certificate.observations
                        if isinstance(item, ReadingPlanSegmentAuditV1)
                        and item.observation_id == observation.key
                    ),
                    component="plan_segments",
                    floor_ids=(floor_id,),
                    observation_ids=(observation.key,),
                    candidate_target_ids=ambiguity.candidate_target_keys,
                    objective_preimage_sha256=canonical_sha256(
                        {
                            "observation_id": observation.key,
                            "candidate_target_ids": (
                                ambiguity.candidate_target_keys
                            ),
                            "support_lines": ambiguity.support_lines,
                        }
                    ),
                    reason="multiple_support_lines",
                )
            )
        local_rows, _mapping = match_plan_segments(
            targets=floor_targets,
            observations=tuple(
                item
                for item in floor_observations
                if item.key not in ambiguous_ids
            ),
            config=config,
        )
        rows.extend(local_rows)

    serial = [
        ReadingSegmentScoreRowV1(
            row_contract="reading_segment_v1",
            target_id=None if row.target is None else row.target.key,
            observation_id=(
                None if row.observation is None else row.observation.key
            ),
            floor_id=(row.target or row.observation).floor_id,
            target_exterior=(
                None if row.target is None else row.target.exterior
            ),
            status=row.status,
            eligible_units=row.eligible_units,
            axis_alignment_error_m=row.axis_alignment_error_m,
            position_error_m=row.position_error_m,
            extent_symmetric_difference_m=(
                row.extent_symmetric_difference_m
            ),
            na_reason=None,
        )
        for row in rows
    ]
    # Trusted-input-filtered targets remain explicitly visible as zero-unit NA
    # audit rows but never enter a red denominator.
    for target in targets:
        if target.key in denominator_target_ids:
            continue
        local_components = tuple(
            item
            for item in component_by_key.values()
            if item.component == "plan_segments"
            and target.floor_id in item.floor_ids
            and item.denominator_disposition == "filter"
        )
        if not local_components:
            continue
        serial.append(
            ReadingSegmentScoreRowV1(
                row_contract="reading_segment_v1",
                target_id=target.key,
                observation_id=None,
                floor_id=target.floor_id,
                target_exterior=target.exterior,
                status="not_applicable",
                eligible_units=0.0,
                axis_alignment_error_m=None,
                position_error_m=None,
                extent_symmetric_difference_m=None,
                na_reason=local_components[0].reasons[0],
            )
        )
    return (
        tuple(
            sorted(
                serial,
                key=lambda item: (
                    item.floor_id,
                    item.target_id or "",
                    item.observation_id or "",
                    item.status,
                ),
            )
        ),
        tuple(
            sorted(
                component_by_key.values(),
                key=lambda item: (
                    item.source_input_id,
                    item.component,
                    item.floor_ids,
                ),
            )
        ),
        tuple(witnesses),
    )


def _source_certificate(
    *,
    normalization,
    gt,
    score_manifest,
    basis,
    atoms,
    basis_sha256,
    denominator_sha256,
    components,
    ambiguity_witnesses,
) -> SourceApplicabilityCertificateV1:
    raw = {
        "schema_version": "1",
        "helper_version": "reading_source_applicability_v2",
        "normalization_sha256": normalization.content_sha256,
        "gt_content_sha256": gt.content_sha256,
        "score_manifest_sha256": score_manifest.content_sha256,
        "denominator_basis": basis.model_dump(mode="json"),
        "denominator_atoms": tuple(
            item.model_dump(mode="json") for item in atoms
        ),
        "denominator_basis_sha256": basis_sha256,
        "denominator_sha256": denominator_sha256,
        "component_applicability": tuple(
            item.model_dump(mode="json") for item in components
        ),
        "ambiguity_witnesses": tuple(
            item.model_dump(mode="json") for item in ambiguity_witnesses
        ),
    }
    return SourceApplicabilityCertificateV1(
        schema_version="1",
        helper_version="reading_source_applicability_v2",
        normalization_sha256=normalization.content_sha256,
        gt_content_sha256=gt.content_sha256,
        score_manifest_sha256=score_manifest.content_sha256,
        denominator_basis=basis,
        denominator_atoms=tuple(atoms),
        denominator_basis_sha256=basis_sha256,
        denominator_sha256=denominator_sha256,
        component_applicability=tuple(components),
        ambiguity_witnesses=tuple(ambiguity_witnesses),
        content_sha256=canonical_sha256(raw),
    )


def _score_certificates(
    normalization,
    source_applicability,
) -> ScoreCertificatesV1:
    raw = {
        "reading_normalization": normalization.content_sha256,
        "source_applicability": source_applicability.content_sha256,
    }
    return ScoreCertificatesV1(
        reading_normalization=normalization,
        source_applicability=source_applicability,
        aggregate_sha256=canonical_sha256(raw),
    )


def _source_result(
    *,
    target,
    claim: str,
    observation: OpeningObservation | None,
    config,
) -> tuple[str, str, float | None, float]:
    if claim == "existence":
        metric = "binary"
        tolerance = 0.0
        error = (
            None
            if observation is None
            else (
                0.0
                if _interval_overlap_or_point(
                    observation.world_along_interval,
                    (
                        target.world_along_interval.lo,
                        target.world_along_interval.hi,
                    ),
                )
                else 1.0
            )
        )
    elif claim == "along":
        metric = "endpoint_max_abs"
        tolerance = config.along_claim_tol_m
        error = (
            None
            if observation is None
            else max(
                abs(
                    observation.world_along_interval[0]
                    - target.world_along_interval.lo
                ),
                abs(
                    observation.world_along_interval[1]
                    - target.world_along_interval.hi
                ),
            )
        )
    elif claim == "width":
        metric = "length_abs"
        tolerance = config.width_claim_tol_m
        error = (
            None
            if observation is None
            else abs(
                (
                    observation.world_along_interval[1]
                    - observation.world_along_interval[0]
                )
                - (
                    target.world_along_interval.hi
                    - target.world_along_interval.lo
                )
            )
        )
    else:
        metric = "scalar_abs"
        tolerance = (
            config.sill_claim_tol_m
            if claim == "sill"
            else config.head_claim_tol_m
        )
        if observation is None or observation.z_interval is None:
            error = None
        else:
            observed = (
                observation.z_interval[0]
                if claim == "sill"
                else observation.z_interval[1]
            )
            expected = (
                target.z_interval.lo
                if claim == "sill"
                else target.z_interval.hi
            )
            error = abs(observed - expected)
    if error is None:
        result = "miss"
    elif error <= config.claim_complete_epsilon_m:
        result = "complete"
    elif error <= tolerance:
        result = "within_tolerance"
    else:
        result = "miss"
    return result, metric, error, tolerance


def _opening_source_rows(
    *,
    gt,
    atoms,
    components,
    assignment: OpeningAssignment,
    applicability_sha256: str,
    config,
) -> tuple[OpeningSourceScoreRowV1, ...]:
    targets = {item.id: item for item in gt.openings}
    component_by_key = _component_map(components)
    matched = {
        (target.id, observation.source_view_id): observation
        for target, observation in assignment.matched
    }
    rows: list[OpeningSourceScoreRowV1] = []
    for atom in atoms:
        if atom.target_kind != "window" or atom.claim is None:
            continue
        target = targets[atom.target_id]
        for source in atom.source_input_ids:
            component = component_by_key[(source, atom.component)]
            observation = (
                None
                if component.status == "not_applicable"
                else matched.get((target.id, source))
            )
            result, metric, error, tolerance = _source_result(
                target=target,
                claim=atom.claim,
                observation=observation,
                config=config,
            )
            expected_span = ClosedIntervalV1(
                lo=target.world_along_interval.lo,
                hi=target.world_along_interval.hi,
            )
            z_claim = atom.claim in {"sill", "head"}
            expected_scalar = (
                (
                    target.z_interval.lo
                    if atom.claim == "sill"
                    else target.z_interval.hi
                )
                if z_claim and target.z_interval is not None
                else None
            )
            observed_scalar = (
                (
                    observation.z_interval[0]
                    if atom.claim == "sill"
                    else observation.z_interval[1]
                )
                if z_claim
                and observation is not None
                and observation.z_interval is not None
                else None
            )
            rows.append(
                OpeningSourceScoreRowV1(
                    target_id=target.id,
                    target_kind=target.kind,
                    claim=atom.claim,
                    source_input_id=source,
                    channel=component.channel,
                    eligible_units=atom.eligible_units,
                    result=result,
                    na_reason=None,
                    matched_observation_ids=(
                        () if observation is None else (observation.id,)
                    ),
                    expected_intervals=(
                        () if z_claim else (expected_span,)
                    ),
                    observed_interval=(
                        None
                        if z_claim or observation is None
                        else ClosedIntervalV1(
                            lo=observation.world_along_interval[0],
                            hi=observation.world_along_interval[1],
                        )
                    ),
                    expected_scalar=expected_scalar,
                    observed_scalar=observed_scalar,
                    error_metric=metric,
                    error_value=error,
                    tolerance=tolerance,
                    source_applicability_sha256=applicability_sha256,
                )
            )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.target_id,
                item.claim,
                item.source_input_id,
            ),
        )
    )


def _fused_result(rows: tuple[OpeningSourceScoreRowV1, ...]) -> str:
    results = tuple(item.result for item in rows)
    passing = any(
        item in {"complete", "within_tolerance"} for item in results
    )
    failing = any(item in {"miss", "conflict"} for item in results)
    if "conflict" in results or (passing and failing):
        return "conflict"
    if "complete" in results:
        return "complete"
    if "within_tolerance" in results:
        return "within_tolerance"
    return "miss"


def _claim_rows(
    *,
    gt,
    reference,
    source_rows: tuple[OpeningSourceScoreRowV1, ...],
    components,
    score_bindings,
) -> tuple[ClaimScoreRowV8, ...]:
    by_source_key: dict[
        tuple[str, str], list[OpeningSourceScoreRowV1]
    ] = {}
    for row in source_rows:
        by_source_key.setdefault((row.target_id, row.claim), []).append(row)
    reference_openings = {
        item.opening_id: item for item in reference.openings
    }
    component_rows = tuple(components)
    bindings_by_input = {
        item.input_id: item for item in score_bindings.bindings
    }
    component_claims = {
        "plan_openings": frozenset({"existence", "along", "width"}),
        "elevation_opening_xy": frozenset(
            {"existence", "along", "width"}
        ),
        "elevation_opening_z": frozenset({"sill", "head"}),
    }
    output: list[ClaimScoreRowV8] = []
    for target in gt.openings:
        reference_opening = reference_openings[target.id]
        reference_claims = {
            item.claim: item for item in reference_opening.claims
        }
        for claim_name in CLAIM_ORDER:
            claim = reference_claims[claim_name]
            rows = tuple(by_source_key.get((target.id, claim_name), ()))
            if target.kind != "window":
                reason = "unsupported_target_kind"
            elif claim_name == "host":
                reason = "reading_topology_unavailable"
            elif claim_name == "appearance":
                reason = "reference_value_unavailable"
            elif claim_name in {"sill", "head"} and target.z_interval is None:
                reason = "reference_value_unavailable"
            elif not rows:
                target_source_views = {
                    item.view_id for item in target.source_refs
                }
                relevant = tuple(
                    item
                    for item in component_rows
                    if item.denominator_disposition == "filter"
                    and claim_name
                    in component_claims.get(item.component, ())
                    and target.floor_id in item.floor_ids
                    and target_source_views.intersection(
                        bindings_by_input[
                            item.source_input_id
                        ].gt_source_view_ids
                    )
                )
                reason = (
                    relevant[0].reasons[0] if relevant else "unobserved"
                )
            else:
                reason = None
            if reason is not None:
                result = "not_applicable"
                units = 0.0
                slices = ()
            else:
                result = _fused_result(rows)
                units = 1.0
                target_interval = IntervalV1(
                    lo=target.world_along_interval.lo,
                    hi=target.world_along_interval.hi,
                )
                representative = next(
                    (
                        item
                        for item in rows
                        if item.result == result
                        and item.error_value is not None
                    ),
                    next(
                        (
                            item
                            for item in rows
                            if item.error_value is not None
                        ),
                        rows[0],
                    ),
                )
                slices = (
                    ClaimOutcomeSliceV8(
                        slice_id=f"reading:{target.id}:{claim_name}",
                        applicable_intervals=(target_interval,),
                        units=1.0,
                        result=result,
                        error=ClaimValueErrorV8(
                            metric=(
                                "binary"
                                if claim_name == "existence"
                                else (
                                    "scalar_absolute"
                                    if claim_name in {"sill", "head"}
                                    else "masked_interval_length"
                                )
                            ),
                            value=(
                                None
                                if result == "conflict"
                                else representative.error_value
                            ),
                            tolerance=representative.tolerance,
                        ),
                        evidence_source_ids=tuple(
                            sorted({item.source_input_id for item in rows})
                        ),
                    ),
                )
            output.append(
                ClaimScoreRowV8(
                    target_id=target.id,
                    target_kind=target.kind,
                    claim=claim_name,
                    applicability=_app_ref(reference, target.id, claim),
                    eligible_units=units,
                    result=result,
                    na_reason=reason,
                    outcome_slices=slices,
                    matched_observation_ids=tuple(
                        sorted(
                            {
                                observation_id
                                for item in rows
                                for observation_id in (
                                    item.matched_observation_ids
                                )
                            }
                        )
                    ),
                    evidence_source_ids=tuple(
                        sorted({item.source_input_id for item in rows})
                    ),
                    product_provenance=(),
                )
            )
    return tuple(output)


def assemble_reading_score(
    *,
    gt_identity,
    gt,
    product_payload,
    product_identity,
    base_view_manifest,
    effective_view_manifest,
    score_bindings,
    c2_config,
    capability,
    tolerance,
    manifest_identity,
    helpers,
    reading_exam_scope_input_ids: set[str] | None = None,
    reading_exam_scope_source: str | None = None,
) -> ReadingScoreAssembly:
    """Normalize, certify, and score one recognized reading envelope."""
    normalization_outcome = normalize_reading_attempt(
        raw=product_payload,
        source_output_sha256=product_identity.output_sha256,
        base_manifest=base_view_manifest,
        score_bindings=score_bindings,
    )
    normalization = normalization_outcome.certificate
    (
        denominator_basis,
        denominator_atoms,
        denominator_basis_sha256,
        denominator_sha256,
    ) = derive_reading_denominator_v1(
        gt,
        base_view_manifest,
        score_bindings,
        normalization_outcome.trusted_capability_dispositions,
    )
    score_manifest = build_reading_score_manifest(
        effective=effective_view_manifest,
        trusted_capability_dispositions=(
            normalization_outcome.trusted_capability_dispositions
        ),
        input_ids=reading_exam_scope_input_ids,
    )
    (
        _observations,
        assignment,
        extras,
        components,
        opening_ambiguities,
    ) = _opening_observations(
        gt=gt,
        certificate=normalization,
        score_bindings=score_bindings,
        config=c2_config,
    )
    segment_rows, components, segment_ambiguities = _segment_rows(
        gt=gt,
        certificate=normalization,
        components=components,
        denominator_atoms=denominator_atoms,
        config=c2_config,
    )
    ambiguities = tuple(
        sorted(
            opening_ambiguities + segment_ambiguities,
            key=lambda item: (
                item.source_input_id,
                item.component,
                item.observation_ids,
            ),
        )
    )
    source_applicability = _source_certificate(
        normalization=normalization,
        gt=gt,
        score_manifest=score_manifest,
        basis=denominator_basis,
        atoms=denominator_atoms,
        basis_sha256=denominator_basis_sha256,
        denominator_sha256=denominator_sha256,
        components=components,
        ambiguity_witnesses=ambiguities,
    )
    certificates = _score_certificates(
        normalization,
        source_applicability,
    )
    channels = _channel_summaries(components)
    counts = _visibility_counts(normalization)
    common_identity = dict(
        gt=gt_identity,
        product=product_identity,
        manifest=manifest_identity,
        helpers=helpers,
        capability=capability,
        tolerances=tolerance,
        reading_normalization_sha256=normalization.content_sha256,
        source_applicability_sha256=source_applicability.content_sha256,
        score_manifest_sha256=score_manifest.content_sha256,
        denominator_basis_sha256=denominator_basis_sha256,
        denominator_sha256=denominator_sha256,
    )
    if not denominator_atoms:
        identity = ScoreIdentityV9(
            **common_identity,
            reference_applicability_sha256=None,
            product_applicability_sha256=None,
            absence_applicability_sha256=None,
        )
        payload = NotApplicablePayloadV9(
            kind="not_applicable",
            reason="no_scorable_reading_channel",
            detail="no_scorable_reading_channel",
            channel_applicability=channels,
            unmeasurable_observations=count_unmeasurable_reading_observations(
                normalization, source_applicability
            ),
            visibility_counts=counts,
            score_criteria=(),
        )
        return ReadingScoreAssembly(
            identity=identity,
            payload=payload,
            certificates=certificates,
            ledger_counts=(0, 0, 0),
        )

    reference = derive_reference_ledger(
        gt=gt,
        bindings=score_bindings,
        effective_manifest=score_manifest,
        input_ids=reading_exam_scope_input_ids,
        reading_exam_scope_source=reading_exam_scope_source,
    )
    source_rows = _opening_source_rows(
        gt=gt,
        atoms=denominator_atoms,
        components=components,
        assignment=assignment,
        applicability_sha256=source_applicability.content_sha256,
        config=c2_config,
    )
    claim_rows = _claim_rows(
        gt=gt,
        reference=reference,
        source_rows=source_rows,
        components=components,
        score_bindings=score_bindings,
    )
    summaries = summarize_claim_rows(claim_rows)
    from src.agent.judge.score_policy import c2_v3_score_policy

    policy = c2_v3_score_policy(
        claim_rows=claim_rows,
        segment_rows=segment_rows,
        opening_source_rows=source_rows,
    )
    # 2026-08-20 — the silent-zero guard. A plan whose ``scale_origin`` is null
    # is a LEGAL product (guide.md §1: "leave null rather than guess"), but the
    # frame-less plan channel then scores every target as a miss — a structural
    # zero whose criteria read exactly like bad tracing. Surface the structural
    # cause as a first-class FAIL criterion so score consumers see "frame was
    # never declared" next to the miss rows. eligible=True and denominator 1.0
    # deliberately: the channel stays scored (retain_as_miss semantics are
    # untouched — this is NOT a filter escape hatch); strict-profile callers
    # fail closed on it via score_service.strict_payload_violation_reason.
    plan_frame_na = sum(
        1
        for item in components
        if item.channel == "plan"
        and "plan_frame_unavailable" in item.reasons
    )
    structural_criteria: tuple[ScoreCriterionV9, ...] = ()
    if plan_frame_na:
        structural_criteria = (
            ScoreCriterionV9(
                criterion_id="reading.plan_frame_declared",
                eligible=True,
                denominator_units=1.0,
                passing_units=0.0,
                failing_units=1.0,
                na_reasons={"plan_frame_unavailable": plan_frame_na},
                verdict="fail",
            ),
        )
    # Dispatch 2026-08-22 lock 3: a whole-facade reflection fitting GT better
    # than the declared frame is a mirror-convention failure and must be a
    # visible FAIL, never a silently-scored low result (strict callers fail
    # closed on it via score_service.strict_payload_violation_reason).
    mirror_witnesses = elevation_mirror_flip_witnesses(
        gt=gt,
        score_bindings=score_bindings,
        observations=_observations,
        center_tol_m=c2_config.opening_match_center_tol_m,
    )
    if mirror_witnesses:
        structural_criteria += (
            ScoreCriterionV9(
                criterion_id=ELEVATION_MIRROR_CRITERION_ID,
                eligible=True,
                denominator_units=float(len(mirror_witnesses)),
                passing_units=0.0,
                failing_units=float(len(mirror_witnesses)),
                na_reasons={
                    witness["input_id"]: witness["observation_count"]  # type: ignore[index]
                    for witness in mirror_witnesses
                },
                verdict="fail",
            ),
        )
    identity = ScoreIdentityV9(
        **common_identity,
        reference_applicability_sha256=reference.content_sha256,
        product_applicability_sha256=None,
        absence_applicability_sha256=None,
    )
    payload = C2ScoredPayloadV9(
        kind="c2_scored",
        channel_applicability=channels,
        unmeasurable_observations=count_unmeasurable_reading_observations(
            normalization, source_applicability
        ),
        visibility_counts=counts,
        segment_rows=segment_rows,
        segment_extras=(),
        opening_source_rows=source_rows,
        claim_rows=claim_rows,
        claim_summaries=summaries,
        extras=extras,
        score_criteria=tuple(
            ScoreCriterionV9.model_validate(item.model_dump(mode="json"))
            for item in policy.criteria
        )
        + structural_criteria,
        reference_ledger_sha256=reference.content_sha256,
        product_ledger_sha256=None,
        absence_ledger_sha256=None,
    )
    return ReadingScoreAssembly(
        identity=identity,
        payload=payload,
        certificates=certificates,
        ledger_counts=(len(reference.openings), 0, 0),
    )
