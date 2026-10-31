"""Judge-only B4b opening adapter, matching, applicability and denominator.

All applicability decisions in this file are delegated to Va's public
``derive_opening_claim_applicability``.  The helpers below only adapt typed GT
and score the resulting immutable ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isclose
from typing import Callable, Iterable, Literal

from src.agent.correction.facade_applicability import (
    ApplicabilityIntervalV1, ElevationClaimEvidenceV1, ElevationViewBindingV1,
    FacadeVisibilityLedgerV1, FloorVisibilityLedgerV1, OpeningApplicabilityLedgerV1,
    OpeningClaimTargetV1, OpeningClaimsV1, PlanClaimEvidenceV1,
    derive_opening_claim_applicability,
)
from src.agent.correction.facade_visibility import VisibilityTolerances, vg_for_direction
from src.agent.correction.schema import FacadeSegment, WorldInterval
from src.agent.execution.view_manifest import ViewManifest
from src.agent.judge.gt_schema import GroundTruthV3, GtOpeningV3
from src.agent.judge.score_inputs import materialize_va_elevation_bindings
from src.agent.judge.score_schema import (
    CLAIM_ORDER, ClaimApplicabilityRefV8, ClaimOutcomeSliceV8, ClaimScoreRowV8,
    ClaimSummaryV8, ClaimValueErrorV8, IntervalV1, JudgeScoreConfigV1,
    JudgeScoreViewBindingsV1, ScoreContractError, canonical_sha256, compute_facade_segments_sha256,
)


@dataclass(frozen=True)
class OpeningObservation:
    id: str
    floor_id: str
    kind: Literal["window", "door"]
    facade_segment_id: str
    world_along_interval: tuple[float, float]
    source_view_id: str
    room_id: str | None = None
    z_interval: tuple[float, float] | None = None
    channel: Literal["plan", "elevation"] = "plan"


@dataclass(frozen=True)
class OpeningAssignment:
    matched: tuple[tuple[GtOpeningV3, OpeningObservation], ...]
    unmatched_targets: tuple[GtOpeningV3, ...]
    unmatched_observations: tuple[OpeningObservation, ...]


def bind_correction_window_segment(
    *,
    window,
    segments: Iterable[FacadeSegment],
    allow_temporary_binding: bool = True,
) -> tuple[FacadeSegment, str]:
    """Validate an explicit segment id or make the permitted temporary span binding.

    This is scoring-only evidence.  It never writes a canonical host back into
    the correction output (B5 remains the host resolver owner).
    """
    candidates = tuple(segment for segment in segments if segment.floor_id == window.floor_id
                       and segment.facade_family == window.facade
                       and segment.world_along_interval.lo <= window.span[0]
                       and window.span[1] <= segment.world_along_interval.hi)
    if window.facade_segment_id is not None:
        explicit = tuple(segment for segment in candidates if segment.id == window.facade_segment_id)
        if len(explicit) == 1:
            return explicit[0], "declared_segment_binding"
    elif allow_temporary_binding and len(candidates) == 1:
        return candidates[0], "temporary_unique_span_binding"
    raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"window_id": window.id})


def resolve_correction_window_host(*, geometry, window, product_segment: FacadeSegment,
                                   gt_segment_id: str, gt_zone_id: str,
                                   product_to_gt_segment: dict[str, str],
                                   product_to_gt_zone: dict[str, str]) -> Literal["complete", "miss"]:
    """Judge-only plan host relationship resolution from exact cell boundaries."""
    floor = next((item for item in geometry.floors if item.id == window.floor_id), None)
    if floor is None or window.room is not None and not any(cell.id == window.room for cell in floor.cells):
        raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"window_id": window.id})
    if window.room is None:
        return "miss"
    span = tuple(window.span)
    adjacent: set[str] = set()
    for cell in floor.cells:
        ring = cell.polygon if cell.polygon is not None else ((cell.x[0], cell.y[0]), (cell.x[1], cell.y[0]), (cell.x[1], cell.y[1]), (cell.x[0], cell.y[1]))
        for p1, p2 in zip(ring, tuple(ring[1:]) + (ring[0],)):
            same_line = ((p1[0] == p2[0] == product_segment.p1[0] == product_segment.p2[0]) or
                         (p1[1] == p2[1] == product_segment.p1[1] == product_segment.p2[1]))
            axis = 1 if p1[0] == p2[0] else 0
            dx, dy = float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1])
            length = abs(dx) + abs(dy)
            outward = None if length == 0 else (int(dy / length), int(-dx / length))
            if (same_line and outward == tuple(product_segment.outward_normal)
                    and min(p1[axis], p2[axis]) <= span[0]
                    and span[1] <= max(p1[axis], p2[axis])):
                adjacent.add(cell.id)
    if len(adjacent) != 1:
        raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"window_id": window.id})
    adjacent_room = next(iter(adjacent))
    return "complete" if window.room == adjacent_room and product_to_gt_segment.get(product_segment.id) == gt_segment_id and product_to_gt_zone.get(adjacent_room) == gt_zone_id else "miss"


def build_correction_host_resolver(*, geometry, product_to_gt_segment: dict[str, str],
                                   product_to_gt_zone: dict[str, str],
                                   allow_temporary_binding: bool = True) -> Callable[[GtOpeningV3, OpeningObservation], Literal["complete", "miss"]]:
    """Connect the judge-only §8.4.1 resolver to ``score_plan_claims``."""
    windows = {window.id: window for window in geometry.windows}
    segments = tuple(geometry.facade_segments)
    def resolve(target: GtOpeningV3, observation: OpeningObservation) -> Literal["complete", "miss"]:
        window = windows.get(observation.id)
        if window is None:
            return "miss"
        product_segment, _ = bind_correction_window_segment(
            window=window,
            segments=segments,
            allow_temporary_binding=allow_temporary_binding,
        )
        if target.host_zone_id is None:
            return "miss"
        return resolve_correction_window_host(geometry=geometry, window=window, product_segment=product_segment,
            gt_segment_id=target.boundary_segment_id, gt_zone_id=target.host_zone_id,
            product_to_gt_segment=product_to_gt_segment, product_to_gt_zone=product_to_gt_zone)
    return resolve


def map_product_cells_to_gt_zones(*, geometry, gt: GroundTruthV3,
                                  product_floor_to_gt_floor: dict[str, str] | None = None) -> dict[str, str]:
    """Map zones only by full polygon equality; never by center or id.

    F-90 (2026-08-25 dispatch): `floor.id` here is the PRODUCT's own floor
    namespace ("floor_1"/"floor_2" for sm25); `gt_by_floor` is keyed by the
    GT's ("F1"/"F2"). The two were never guaranteed to line up (they are two
    independent producers' choices), so `product_floor_to_gt_floor` (the same
    window-provenance-derived bridge the caller already built for the plan
    source and facade-span lookups) translates the product floor before the
    GT-side dict is consulted. Without it, a product floor absent from the
    map (e.g. `None`, the caller's back-compat default) resolves no zones for
    that floor -- every window's host-zone claim then fails closed as a
    miss, never a silent guess.
    """
    from shapely.geometry import Polygon
    from src.agent.correction.cell_geometry import cell_polygon

    gt_by_floor = {
        floor.id: tuple(
            (zone.id, Polygon(zone.polygon.exterior.vertices)) for zone in floor.zones
        )
        for floor in gt.floors
    }
    bridge = product_floor_to_gt_floor or {}
    mapping: dict[str, str] = {}
    for floor in geometry.floors:
        gt_floor_id = bridge.get(floor.id, floor.id)
        targets = gt_by_floor.get(gt_floor_id, ())
        for cell in floor.cells:
            polygon = cell_polygon(cell)
            candidates = [zone_id for zone_id, target in targets if polygon.equals(target)]
            if len(candidates) == 1:
                mapping[cell.id] = candidates[0]
    return mapping


def _interval(raw) -> ApplicabilityIntervalV1:
    return ApplicabilityIntervalV1(lo=float(raw[0]), hi=float(raw[1]))


def _overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _merge(intervals: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    output: list[list[float]] = []
    for lo, hi in sorted(intervals):
        if lo >= hi:
            raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability", context={"reason": "invalid_interval"})
        if output and lo <= output[-1][1]: output[-1][1] = max(output[-1][1], hi)
        else: output.append([lo, hi])
    return tuple((lo, hi) for lo, hi in output)


def _length(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(hi - lo for lo, hi in intervals)


def gt_to_va_visibility(gt: GroundTruthV3) -> FacadeVisibilityLedgerV1:
    """Re-run public Vg and replace only its temporary IDs with typed GT IDs."""
    tol = VisibilityTolerances(gt.generator.tolerances.vg_depth_epsilon_m, gt.generator.tolerances.vg_endpoint_epsilon_m)
    all_segments: list[FacadeSegment] = []; floors: list[FloorVisibilityLedgerV1] = []
    direction = {"North": (0, 1), "South": (0, -1), "East": (1, 0), "West": (-1, 0)}
    for floor in gt.floors:
        derived = [item for family in ("North", "South", "East", "West")
                   for item in vg_for_direction(floor.footprint.exterior.vertices, direction[family], tolerances=tol)]
        expected_rows = [(item.frame.facade_family, item.frame.p1, item.frame.p2, item.frame.world_along_interval, item) for item in derived]
        declared_rows = [(item.facade_family, tuple(item.p1), tuple(item.p2), (item.world_along_interval.lo, item.world_along_interval.hi), item) for item in floor.boundary_segments]
        expected_keys = [row[:4] for row in expected_rows]; declared_keys = [row[:4] for row in declared_rows]
        if len(set(expected_keys)) != len(expected_keys) or len(set(declared_keys)) != len(declared_keys) or set(expected_keys) != set(declared_keys):
            raise ScoreContractError("score_visibility_adapter_mismatch", "scoring.applicability", context={"floor_id": floor.id})
        expected = {row[:4]: row[4] for row in expected_rows}; declared = {row[:4]: row[4] for row in declared_rows}
        materialized: list[FacadeSegment] = []
        for key, vg in expected.items():
            segment = declared[key]
            materialized.append(FacadeSegment(id=segment.id, floor_id=floor.id, facade_family=segment.facade_family,
                p1=tuple(segment.p1), p2=tuple(segment.p2), outward_normal=tuple(segment.outward_normal),
                world_along_interval=WorldInterval(lo=segment.world_along_interval.lo, hi=segment.world_along_interval.hi),
                depth=segment.depth, visible_intervals=[WorldInterval(lo=lo, hi=hi) for lo, hi in vg.visible_intervals],
                source_footprint_fingerprint=segment.source_footprint_fingerprint))
        materialized.sort(key=lambda s: (s.facade_family, s.world_along_interval.lo, s.world_along_interval.hi, s.depth, s.id))
        all_segments.extend(materialized)
        floors.append(FloorVisibilityLedgerV1(floor_id=floor.id, source_footprint_fingerprint=floor.footprint_fingerprint,
                                               segments=tuple(materialized)))
    return FacadeVisibilityLedgerV1(source_kind="judge_gt", source_schema_version="3", source_output_sha256=gt.content_sha256,
        facade_segments_sha256=compute_facade_segments_sha256(all_segments), feature_states_sha256=None,
        helper_versions=("facade_visibility_v1", "b4b_gt_to_va_v1"), floors=tuple(floors))


def gt_openings_to_va_claims(*, gt: GroundTruthV3, bindings: JudgeScoreViewBindingsV1,
                             effective_manifest: ViewManifest,
                             input_ids: set[str] | None = None) -> tuple[OpeningClaimsV1, ...]:
    """Build exact seven-claim GT positive evidence for one Va reference call."""
    by_gt_view = {view: binding for binding in bindings.bindings for view in binding.gt_source_view_ids}
    observable_by_input = {
        entry.input_id: frozenset(
            getattr(
                getattr(entry, "opening_evidence", None),
                "potentially_observable_claims",
                (),
            )
        )
        for entry in effective_manifest.required_entries()
    }
    segments = {segment.id: segment for floor in gt.floors for segment in floor.boundary_segments}
    floor_order = {floor.id: index + 1 for index, floor in enumerate(gt.floors)}
    rows: list[OpeningClaimsV1] = []
    plan_claims = {"existence", "host", "along", "width"}
    elevation_claims = {"existence", "along", "width", "sill", "head", "appearance"}
    for opening in gt.openings:
        segment = segments.get(opening.boundary_segment_id)
        if segment is None or opening.floor_id not in floor_order:
            raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability", context={"opening_id": opening.id})
        target = _interval((opening.world_along_interval.lo, opening.world_along_interval.hi))
        evidence: dict[str, list] = {claim: [] for claim in CLAIM_ORDER}
        seen: set[tuple[str, str]] = set()
        for ref in opening.source_refs:
            binding = by_gt_view.get(ref.view_id)
            if binding is None:
                if input_ids is not None:
                    # The frozen scope is the reader's entire exam.  A GT ref
                    # outside it is neither a missing binding nor a miss.
                    continue
                raise ScoreContractError("score_view_binding_invalid", "scoring.view_bindings", context={"opening_id": opening.id, "view_id": ref.view_id})
            marker = (ref.view_id, binding.input_id)
            if marker in seen: continue
            seen.add(marker)
            if binding.kind == "plan":
                for claim in sorted(
                    plan_claims.intersection(
                        observable_by_input.get(binding.input_id, ())
                    )
                ):
                    evidence[claim].append(PlanClaimEvidenceV1(source_input_id=binding.input_id, world_interval=target))
            else:
                u0 = (target.lo - binding.along_origin) / binding.sign
                u1 = (target.hi - binding.along_origin) / binding.sign
                local = ApplicabilityIntervalV1(lo=min(u0, u1), hi=max(u0, u1))
                for claim in sorted(
                    elevation_claims.intersection(
                        observable_by_input.get(binding.input_id, ())
                    )
                ):
                    evidence[claim].append(ElevationClaimEvidenceV1(source_input_id=binding.input_id, local_interval=local))
        rows.append(OpeningClaimsV1(opening_id=opening.id, floor_id=opening.floor_id, floor_ref=floor_order[opening.floor_id],
            facade_segment_id=segment.id, facade_family=segment.facade_family,
            claims=tuple(OpeningClaimTargetV1(claim=claim, target_world_interval=target,
                positive_evidence=tuple(evidence[claim])) for claim in CLAIM_ORDER)))
    return tuple(rows)


def derive_reference_ledger(*, gt: GroundTruthV3, bindings: JudgeScoreViewBindingsV1,
                            effective_manifest: ViewManifest,
                            input_ids: set[str] | None = None,
                            reading_exam_scope_source: str | None = None) -> OpeningApplicabilityLedgerV1:
    visibility = gt_to_va_visibility(gt)
    return derive_opening_claim_applicability(visibility=visibility, manifest=effective_manifest,
        elevation_views=materialize_va_elevation_bindings(score_bindings=bindings, effective_manifest=effective_manifest),
        openings=gt_openings_to_va_claims(gt=gt, bindings=bindings, effective_manifest=effective_manifest,
                                         input_ids=input_ids),
        reading_exam_scope_source=reading_exam_scope_source)


def derive_product_ledger(*, visibility: FacadeVisibilityLedgerV1, manifest: ViewManifest,
                          elevation_views: tuple[ElevationViewBindingV1, ...], openings: tuple[OpeningClaimsV1, ...]) -> OpeningApplicabilityLedgerV1:
    """Product declarations have their own Va call and never alter reference units."""
    return derive_opening_claim_applicability(visibility=visibility, manifest=manifest, elevation_views=elevation_views, openings=openings)


def derive_absence_ledger(*, visibility: FacadeVisibilityLedgerV1, manifest: ViewManifest,
                          elevation_views: tuple[ElevationViewBindingV1, ...], openings: tuple[OpeningClaimsV1, ...]) -> OpeningApplicabilityLedgerV1:
    """Absence queries are deliberately also a Va call (no local visibility test)."""
    return derive_opening_claim_applicability(visibility=visibility, manifest=manifest, elevation_views=elevation_views, openings=openings)


def absence_opening_id(*, output_sha256: str, observation: OpeningObservation, gt_segment_id: str) -> str:
    span = _interval(observation.world_along_interval)
    digest = canonical_sha256({"output_sha256": output_sha256, "observation_id": observation.id,
                              "segment_id": gt_segment_id, "span": [span.lo, span.hi]})[:24]
    return "absence:" + digest


def build_absence_opening_claims(*, observations: Iterable[OpeningObservation], floor_refs: dict[str, int],
                                segment_families: dict[str, str], output_sha256: str,
                                product_to_gt_segment: dict[str, str],
                                trusted_source_views: dict[str, tuple[str, ...]] | None = None) -> tuple[OpeningClaimsV1, ...]:
    """Build §7.5's collision-checked, positive-evidence-free Va queries."""
    rows = []
    for observation in observations:
        gt_segment_id = product_to_gt_segment.get(observation.facade_segment_id)
        family = segment_families.get(gt_segment_id)
        floor_ref = floor_refs.get(observation.floor_id)
        allowed_views = None if trusted_source_views is None else trusted_source_views.get(gt_segment_id)
        if family is None or floor_ref is None or allowed_views is not None and observation.source_view_id not in allowed_views:
            raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"observation_id": observation.id})
        span = _interval(observation.world_along_interval)
        rows.append(OpeningClaimsV1(opening_id=absence_opening_id(output_sha256=output_sha256, observation=observation, gt_segment_id=gt_segment_id), floor_id=observation.floor_id, floor_ref=floor_ref,
            facade_segment_id=gt_segment_id, facade_family=family,
            claims=tuple(OpeningClaimTargetV1(claim=claim, target_world_interval=span, positive_evidence=()) for claim in CLAIM_ORDER)))
    if len({item.opening_id for item in rows}) != len(rows):
        raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability", context={"reason": "absence_id_collision"})
    return tuple(rows)


def _opening_key(opening: GtOpeningV3) -> tuple:
    return (opening.floor_id, opening.kind, opening.boundary_segment_id, opening.world_along_interval.lo,
            opening.world_along_interval.hi, opening.id)


def _assign_openings_for_source(*, targets: Iterable[GtOpeningV3], observations: Iterable[OpeningObservation],
                                source_view_id: str, config: JudgeScoreConfigV1,
                                product_to_gt_segment: dict[str, str],
                                source_view_to_gt_view_ids: dict[
                                    str, tuple[str, ...]
                                ] | None = None) -> OpeningAssignment:
    """One source-view global assignment; a target can be corroborated elsewhere."""
    ts = tuple(sorted(targets, key=_opening_key)); os = tuple(sorted(observations, key=lambda o: (o.floor_id, o.kind, o.facade_segment_id, o.world_along_interval, o.source_view_id, o.id)))
    choices = []
    for target in ts:
        possible = [None]
        references = getattr(target, "source_refs", ())
        trusted_gt_views = set(
            (source_view_to_gt_view_ids or {}).get(
                source_view_id, (source_view_id,)
            )
        )
        if references and not trusted_gt_views.intersection(
            ref.view_id for ref in references
        ):
            choices.append(possible); continue
        for index, observed in enumerate(os):
            span = (target.world_along_interval.lo, target.world_along_interval.hi)
            if (target.floor_id, target.kind, target.boundary_segment_id) != (observed.floor_id, observed.kind, product_to_gt_segment.get(observed.facade_segment_id)): continue
            overlap = _overlap(span, observed.world_along_interval)
            centre = abs((span[0] + span[1] - observed.world_along_interval[0] - observed.world_along_interval[1]) / 2)
            if overlap > 0 and centre <= config.opening_match_center_tol_m:
                possible.append((index, (overlap, centre, abs((span[1]-span[0]) - (observed.world_along_interval[1]-observed.world_along_interval[0])))))
        choices.append(possible)
    all_solutions = []
    def visit(i, used, selected, metric):
        if i == len(ts): all_solutions.append((tuple(selected), metric)); return
        for item in choices[i]:
            if item is None: visit(i+1, used, selected+[None], metric); continue
            oi, values = item
            if oi not in used: visit(i+1, used|{oi}, selected+[oi], (metric[0]+1, metric[1]+values[0], metric[2]+values[1], metric[3]+values[2]))
    visit(0, frozenset(), [], (0, 0., 0., 0.))
    eps = config.opening_assignment_tie_epsilon
    def better(a, b):
        if a[0] != b[0]: return a[0] > b[0]
        if abs(a[1]-b[1]) > eps: return a[1] > b[1]
        if abs(a[2]-b[2]) > eps: return a[2] < b[2]
        return a[3] < b[3]-eps
    best = all_solutions[0][1]
    for _, metric in all_solutions[1:]:
        if better(metric, best): best = metric
    winners = [picked for picked, metric in all_solutions if metric[0] == best[0] and all(abs(metric[i]-best[i]) <= eps for i in range(1, 4))]
    if len(winners) != 1:
        raise ScoreContractError(
            "score_match_ambiguous",
            "scoring.matching",
            context={
                "kind": "opening",
                "source_view_id": source_view_id,
                "candidate_assignments": len(winners),
            },
        )
    selected = winners[0]; used = {value for value in selected if value is not None}
    return OpeningAssignment(tuple((target, os[index]) for target, index in zip(ts, selected) if index is not None),
        tuple(target for target, index in zip(ts, selected) if index is None), tuple(value for index, value in enumerate(os) if index not in used))


def assign_openings(*, targets: Iterable[GtOpeningV3], observations: Iterable[OpeningObservation],
                    config: JudgeScoreConfigV1, product_to_gt_segment: dict[str, str],
                    source_view_to_gt_view_ids: dict[
                        str, tuple[str, ...]
                    ] | None = None) -> OpeningAssignment:
    """Run §8.4's global objective independently for every source view."""
    ts = tuple(targets); by_source: dict[str, list[OpeningObservation]] = {}
    for observation in observations:
        by_source.setdefault(observation.source_view_id, []).append(observation)
    matched: list[tuple[GtOpeningV3, OpeningObservation]] = []
    unmatched_observations: list[OpeningObservation] = []
    for source, rows in sorted(by_source.items()):
        if any(row.facade_segment_id not in product_to_gt_segment for row in rows):
            raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"source_view_id": source})
        result = _assign_openings_for_source(targets=ts, observations=rows, source_view_id=source, config=config,
                                             product_to_gt_segment=product_to_gt_segment,
                                             source_view_to_gt_view_ids=source_view_to_gt_view_ids)
        matched.extend(result.matched); unmatched_observations.extend(result.unmatched_observations)
    matched_target_ids = {target.id for target, _ in matched}
    return OpeningAssignment(tuple(matched), tuple(target for target in ts if target.id not in matched_target_ids), tuple(unmatched_observations))


def eligible_units(*, claim, target_kind: str, has_reference_value: bool = True) -> tuple[float, str | None, tuple[tuple[float, float], ...]]:
    """Exact §8.5 unit calculation; no fixed partial denominator exists here."""
    if claim.claim == "appearance": return 0.0, "reference_value_unavailable", ()
    if target_kind != "window": return 0.0, "unsupported_target_kind", ()
    if claim.claim in {"sill", "head"} and not has_reference_value: return 0.0, "reference_value_unavailable", ()
    target = (claim.target_world_interval.lo, claim.target_world_interval.hi)
    raw = _merge((item.lo, item.hi) for item in claim.applicable_intervals)
    if any(lo < target[0] or hi > target[1] for lo, hi in raw):
        raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability", context={"claim": claim.claim, "reason": "outside_target"})
    applicable = _merge((max(lo, target[0]), min(hi, target[1])) for lo, hi in raw
                        if max(lo, target[0]) < min(hi, target[1]))
    if claim.status == "not_applicable": return 0.0, "unobserved", applicable
    if claim.status == "applicable": return 1.0, None, applicable
    if claim.claim == "existence" or not applicable:
        raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability", context={"claim": claim.claim})
    if claim.claim in {"sill", "head"}:
        return 1.0, None, applicable
    ratio = _length(applicable) / (target[1] - target[0])
    if ratio <= 0 or ratio > 1 + 1e-12:
        raise ScoreContractError("score_denominator_nonconserving", "scoring.denominator_totality", context={"claim": claim.claim})
    return ratio, None, applicable


def _app_ref(ledger: OpeningApplicabilityLedgerV1, opening_id: str, claim) -> ClaimApplicabilityRefV8:
    return ClaimApplicabilityRefV8(ledger_content_sha256=ledger.content_sha256, opening_id=opening_id, claim=claim.claim,
        target_world_interval=IntervalV1(lo=claim.target_world_interval.lo, hi=claim.target_world_interval.hi), status=claim.status,
        reason=claim.reason, applicable_intervals=tuple(IntervalV1(lo=x.lo, hi=x.hi) for x in claim.applicable_intervals),
        unobserved_intervals=tuple(IntervalV1(lo=x.lo, hi=x.hi) for x in claim.unobserved_intervals),
        considered_source_view_ids=claim.considered_source_view_ids, supporting_source_view_ids=claim.supporting_source_view_ids,
        facade_segment_ids=claim.facade_segment_ids)


def trusted_negative_intervals(*, claim) -> tuple[tuple[float, float], ...]:
    """Return only negative coverage already certified by Va/completeness."""
    return _merge((item.lo, item.hi) for decision in claim.source_evidence
                  for item in decision.negative_evidence_intervals)


def split_applicable_by_trusted_negative(*, applicable: Iterable[tuple[float, float]], claim) -> tuple[tuple[tuple[float, float], bool], ...]:
    """Cut partial units at Va negative endpoints for conflict/miss allocation."""
    negative = trusted_negative_intervals(claim=claim)
    output = []
    for lo, hi in _merge(applicable):
        endpoints = sorted({lo, hi, *(max(lo, min(hi, x)) for pair in negative for x in pair)})
        for start, end in zip(endpoints, endpoints[1:]):
            if start < end:
                output.append(((start, end), any(nlo <= start and end <= nhi for nlo, nhi in negative)))
    return tuple(output)


def fuse_source_results(results: Iterable[Literal["complete", "within_tolerance", "miss", "conflict", "not_applicable"]]) -> Literal["complete", "within_tolerance", "miss", "conflict", "not_applicable"]:
    """Frozen §8.7 precedence; callers supply per-source comparisons only."""
    values = tuple(results)
    visible = tuple(value for value in values if value != "not_applicable")
    if not visible: return "not_applicable"
    if "conflict" in visible: return "conflict"
    if "complete" in visible: return "complete"
    if "within_tolerance" in visible: return "within_tolerance"
    return "miss"


def score_plan_claims(*, gt: GroundTruthV3, ledger: OpeningApplicabilityLedgerV1,
                      assignment: OpeningAssignment, config: JudgeScoreConfigV1,
                      host_results: dict[str, Literal["complete", "miss"]] | None = None,
                      host_resolver: Callable[[GtOpeningV3, OpeningObservation], Literal["complete", "miss"]] | None = None,
                      source_view_to_input: dict[str, str] | None = None) -> tuple[ClaimScoreRowV8, ...]:
    """Score Phase-B plan claims; elevation and fusion remain Phase C territory."""
    by_gt = {item.id: item for item in gt.openings}; matches: dict[str, list[OpeningObservation]] = {}
    for target, observation in assignment.matched:
        matches.setdefault(target.id, []).append(observation)
    rows = []
    for entry in ledger.openings:
        target = by_gt.get(entry.opening_id)
        if target is None: raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability")
        observed_rows = tuple(sorted(matches.get(target.id, ()), key=lambda item: (item.source_view_id, item.id)))
        for claim in entry.claims:
            has_z = target.z_interval is not None
            units, na_reason, applicable = eligible_units(claim=claim, target_kind=target.kind, has_reference_value=has_z)
            if claim.claim not in {"existence", "host", "along", "width"}: na_reason = na_reason or "phase_b_plan_only"; units = 0.0
            result = "not_applicable" if units == 0 else "miss"
            error_value = None
            span = (target.world_along_interval.lo, target.world_along_interval.hi)
            tolerance = None
            known_inputs = set(claim.considered_source_view_ids)
            matched_for_claim = tuple(item for item in observed_rows
                                      if (source_view_to_input or {}).get(item.source_view_id, item.source_view_id) in known_inputs)
            source_results: list[str] = []
            for observed in matched_for_claim if units else ():
                pred = observed.world_along_interval
                if claim.claim == "existence": error_value = 0.0 if _overlap(pred, applicable[0] if applicable else span) > 0 else 1.0
                elif claim.claim == "along":
                    error_value = max(abs(pred[0]-span[0]), abs(pred[1]-span[1])) if units == 1 else max(0.0, _length(applicable)-sum(_overlap(pred, part) for part in applicable))
                elif claim.claim == "width": error_value = abs((pred[1]-pred[0])-(span[1]-span[0])) if units == 1 else _length(applicable)-sum(_overlap(pred, part) for part in applicable)
                elif claim.claim == "host":
                    host = host_resolver(target, observed) if host_resolver is not None else (host_results or {}).get(target.id, "miss")
                    error_value = 0.0 if host == "complete" else 1.0
                tolerance = 0.0 if claim.claim in {"existence", "host"} else (config.along_claim_tol_m if claim.claim == "along" else config.width_claim_tol_m)
                source_results.append("complete" if error_value <= config.claim_complete_epsilon_m else ("within_tolerance" if error_value <= tolerance else "miss"))
            positive_result = fuse_source_results(source_results) if units else "not_applicable"
            positive_sources = {(source_view_to_input or {}).get(item.source_view_id, item.source_view_id) for item in matched_for_claim}
            # A reference source that declared this opening positive cannot also
            # be used as an absence witness merely because completeness made Va
            # expose a negative interval.  §8.6.1 item 1 keeps that reference
            # inconsistency out of product miss/conflict accounting.
            negative_sources = {decision.source_input_id for decision in claim.source_evidence
                                if decision.negative_evidence_intervals
                                and not decision.positive_evidence_declared
                                and not decision.applicable_intervals}
            if negative_sources - positive_sources:
                source_results.append("conflict" if positive_sources else "miss")
            result = fuse_source_results(source_results) if units else "not_applicable"
            if result == "conflict": error_value = None
            if claim.status == "partially_applicable" and claim.claim in {"along", "width"}:
                component_rows = split_applicable_by_trusted_negative(applicable=applicable, claim=claim)
            else:
                component_rows = (((applicable[0] if applicable else span), False),)
            slices = () if result == "not_applicable" else tuple(ClaimOutcomeSliceV8(slice_id="%s:%d" % (claim.claim, i),
                applicable_intervals=(IntervalV1(lo=lo, hi=hi),), units=(hi-lo)/(span[1]-span[0]) if claim.status == "partially_applicable" and claim.claim in {"along", "width"} else units,
                result=("conflict" if conflict else ("miss" if positive_result == "not_applicable" else positive_result)), error=ClaimValueErrorV8(metric="binary" if claim.claim in {"existence", "host"} else "masked_interval_length", value=error_value, tolerance=tolerance if matched_for_claim else None),
                evidence_source_ids=claim.supporting_source_view_ids) for i, ((lo, hi), conflict) in enumerate(component_rows))
            rows.append(ClaimScoreRowV8(target_id=target.id, target_kind=target.kind, claim=claim.claim,
                applicability=_app_ref(ledger, target.id, claim), eligible_units=units, result=result, na_reason=na_reason,
                outcome_slices=slices, matched_observation_ids=tuple(item.id for item in observed_rows), evidence_source_ids=claim.supporting_source_view_ids,
                product_provenance=()))
    return tuple(rows)


def _source_channel(observation: OpeningObservation, source_channels: dict[str, Literal["plan", "elevation"]] | None) -> Literal["plan", "elevation"]:
    """Resolve channel only from normalized judge evidence, never product flags."""
    return observation.channel if source_channels is None else source_channels.get(observation.source_view_id, observation.channel)


def _v3_source_result(*, claim_name: str, target: GtOpeningV3, observation: OpeningObservation,
                      intervals: tuple[tuple[float, float], ...], config: JudgeScoreConfigV1,
                      host_result: Literal["complete", "miss"] = "miss") -> tuple[str, float | None, float | None]:
    """Compare one trusted source against the Va intervals it made observable."""
    predicted = observation.world_along_interval
    span = (target.world_along_interval.lo, target.world_along_interval.hi)
    if claim_name == "existence":
        error, tolerance = (0.0 if any(_overlap(predicted, interval) > 0 for interval in intervals) else 1.0), 0.0
    elif claim_name == "host":
        error, tolerance = (0.0 if host_result == "complete" else 1.0), 0.0
    elif claim_name == "along":
        if len(intervals) == 1 and intervals[0] == span:
            error = max(abs(predicted[0] - span[0]), abs(predicted[1] - span[1]))
        else:
            error = max((interval[1] - interval[0]) - _overlap(predicted, interval) for interval in intervals)
        tolerance = config.along_claim_tol_m
    elif claim_name == "width":
        if len(intervals) == 1 and intervals[0] == span:
            error = abs((predicted[1] - predicted[0]) - (span[1] - span[0]))
        else:
            error = _length(intervals) - sum(_overlap(predicted, interval) for interval in intervals)
        tolerance = config.width_claim_tol_m
    elif claim_name in {"sill", "head"}:
        if target.z_interval is None or observation.z_interval is None:
            return "miss", None, config.sill_claim_tol_m if claim_name == "sill" else config.head_claim_tol_m
        expected = target.z_interval.lo if claim_name == "sill" else target.z_interval.hi
        actual = observation.z_interval[0] if claim_name == "sill" else observation.z_interval[1]
        error, tolerance = abs(actual - expected), (config.sill_claim_tol_m if claim_name == "sill" else config.head_claim_tol_m)
    else:
        return "not_applicable", None, None
    return ("complete" if error <= config.claim_complete_epsilon_m else
            ("within_tolerance" if error <= tolerance else "miss"), error, tolerance)


def _decision_intervals(decision) -> tuple[tuple[float, float], ...]:
    return _merge((part.lo, part.hi) for part in decision.applicable_intervals)


def _explicit_product_absence_sources(*, product_ledger: OpeningApplicabilityLedgerV1 | None,
                                      opening_id: str, claim_name: str) -> set[str]:
    """Return only absence declarations explicitly represented by product Va.

    A missing observation is not a declaration that a source saw no opening.
    The caller must supply the independently-derived product ledger to enable
    §8.6.1 item 2; without it v3 deliberately has no trusted-negative conflict.
    """
    if product_ledger is None:
        return set()
    opening = next((item for item in product_ledger.openings if item.opening_id == opening_id), None)
    if opening is None:
        return set()
    claim = next((item for item in opening.claims if item.claim == claim_name), None)
    if claim is None:
        return set()
    return {decision.source_input_id for decision in claim.source_evidence
            if not decision.positive_evidence_declared and decision.negative_evidence_intervals}


def score_opening_claims_v3(*, gt: GroundTruthV3, reference_ledger: OpeningApplicabilityLedgerV1,
                            assignment: OpeningAssignment, config: JudgeScoreConfigV1,
                            source_view_to_input: dict[str, str] | None = None,
                            source_channels: dict[str, Literal["plan", "elevation"]] | None = None,
                            product_ledger: OpeningApplicabilityLedgerV1 | None = None,
                            host_resolver: Callable[[GtOpeningV3, OpeningObservation], Literal["complete", "miss"]] | None = None) -> tuple[ClaimScoreRowV8, ...]:
    """Score C2 claims from the Va reference ledger and normalized evidence.

    This is intentionally a new typed path.  The legacy plan-only function is
    not changed.  Every denominator and per-source observable interval is read
    from ``reference_ledger`` produced by Va; the scorer neither clips with Vg
    nor invents a visibility test.
    """
    by_target = {opening.id: opening for opening in gt.openings}
    matched: dict[str, list[OpeningObservation]] = {}
    for target, observation in assignment.matched:
        matched.setdefault(target.id, []).append(observation)
    plan_claims = {"existence", "host", "along", "width"}
    elevation_claims = {"existence", "along", "width", "sill", "head"}
    output: list[ClaimScoreRowV8] = []
    for entry in reference_ledger.openings:
        target = by_target.get(entry.opening_id)
        if target is None:
            raise ScoreContractError("score_claim_applicability_invalid", "scoring.applicability")
        observations = tuple(matched.get(target.id, ()))
        for claim in entry.claims:
            units, na_reason, applicable = eligible_units(
                claim=claim, target_kind=target.kind, has_reference_value=target.z_interval is not None,
            )
            # Appearance is an explicit capability NA, before Va coverage.
            if claim.claim == "appearance":
                units, na_reason, applicable = 0.0, "reference_value_unavailable", ()
            source_rows: dict[str, list[OpeningObservation]] = {}
            for observation in observations:
                input_id = (source_view_to_input or {}).get(observation.source_view_id, observation.source_view_id)
                if input_id not in claim.considered_source_view_ids:
                    continue
                channel = _source_channel(observation, source_channels)
                if claim.claim not in (plan_claims if channel == "plan" else elevation_claims):
                    continue
                source_rows.setdefault(input_id, []).append(observation)
            decisions = {decision.source_input_id: decision for decision in claim.source_evidence}
            source_results: list[tuple[str, str, float | None, float | None]] = []
            for input_id, observations_for_source in sorted(source_rows.items()):
                decision = decisions.get(input_id)
                if decision is None:
                    continue
                intervals = _decision_intervals(decision)
                if not intervals:
                    continue
                # A source may contain several product observations, but it is
                # one independent witness.  Its best observation is its result.
                candidates = [_v3_source_result(
                    claim_name=claim.claim, target=target, observation=observation,
                    intervals=intervals, config=config,
                    host_result=(host_resolver(target, observation) if host_resolver else "miss"),
                ) for observation in observations_for_source]
                order = {"complete": 3, "within_tolerance": 2, "miss": 1, "not_applicable": 0}
                result, value, tolerance = max(candidates, key=lambda item: order[item[0]])
                source_results.append((input_id, result, value, tolerance))
            if units == 0:
                result, slices = "not_applicable", ()
            else:
                visible_results = [item[1] for item in source_results if item[1] != "not_applicable"]
                # Independent positive sources that disagree beyond tolerance
                # are a conflict, rather than a convenient best-of fusion.
                positive_conflict = (any(item in {"complete", "within_tolerance"} for item in visible_results)
                                     and "miss" in visible_results)
                # Va certified negative coverage only matters when a different
                # source supplied a product positive *and* product Va explicitly
                # declared that source absent.  It never subtracts A, and an
                # omitted product observation is never inferred to be absence.
                positive_inputs = {item[0] for item in source_results}
                declared_absences = _explicit_product_absence_sources(
                    product_ledger=product_ledger, opening_id=target.id, claim_name=claim.claim,
                )
                negative_inputs = {source_id for source_id, decision in decisions.items()
                                   if source_id in declared_absences and source_id not in positive_inputs
                                   and decision.negative_evidence_intervals
                                   and not decision.positive_evidence_declared
                                   and not decision.applicable_intervals}
                negative_intervals = _merge(
                    (part.lo, part.hi) for source_id, decision in decisions.items() if source_id in negative_inputs
                    for part in decision.negative_evidence_intervals
                )
                negative_conflict = bool(source_results) and any(
                    _overlap(applicable_part, negative_part) > 0
                    for applicable_part in applicable for negative_part in negative_intervals
                )
                base_result = ("conflict" if positive_conflict or negative_conflict else
                               (fuse_source_results(visible_results) if visible_results else "miss"))
                span = (target.world_along_interval.lo, target.world_along_interval.hi)
                if claim.claim in {"along", "width"}:
                    pieces = split_applicable_by_trusted_negative(applicable=applicable, claim=claim)
                    # Only negatives belonging to absent product sources cause
                    # conflict; positive declarations in their own source don't.
                    pieces = tuple((piece, covered and any(_overlap(piece, n) > 0 for n in negative_intervals))
                                   for piece, covered in pieces)
                else:
                    pieces = (((applicable[0] if applicable else span), False),)
                error_value = next((item[2] for item in source_results if item[2] is not None), None)
                tolerance = next((item[3] for item in source_results if item[3] is not None), None)
                slices = tuple(ClaimOutcomeSliceV8(
                    slice_id=f"v3:{claim.claim}:{index}",
                    applicable_intervals=(IntervalV1(lo=piece[0], hi=piece[1]),),
                    units=((piece[1] - piece[0]) / (span[1] - span[0])
                           if claim.status == "partially_applicable" and claim.claim in {"along", "width"}
                           else units / len(pieces)),
                    result=("conflict" if conflict and source_results else base_result),
                    error=ClaimValueErrorV8(
                        metric=("binary" if claim.claim in {"existence", "host"} else
                                ("scalar_absolute" if claim.claim in {"sill", "head"} else "masked_interval_length")),
                        value=None if base_result == "conflict" else error_value,
                        tolerance=tolerance,
                    ), evidence_source_ids=claim.supporting_source_view_ids,
                ) for index, (piece, conflict) in enumerate(pieces))
                results = [item.result for item in slices]
                result = "conflict" if "conflict" in results else base_result
            output.append(ClaimScoreRowV8(
                target_id=target.id, target_kind=target.kind, claim=claim.claim,
                applicability=_app_ref(reference_ledger, target.id, claim), eligible_units=units,
                result=result, na_reason=na_reason, outcome_slices=slices,
                matched_observation_ids=tuple(item.id for item in observations),
                evidence_source_ids=claim.supporting_source_view_ids, product_provenance=(),
            ))
    # Totality is a construction gate, not a best-effort sidecar condition.
    summarize_claim_rows(output)
    return tuple(output)


def summarize_claim_rows(rows: Iterable[ClaimScoreRowV8]) -> tuple[ClaimSummaryV8, ...]:
    output = []
    for claim in CLAIM_ORDER:
        group = [row for row in rows if row.claim == claim]; reasons = {}
        for row in group:
            allocated = sum(slice.units for slice in row.outcome_slices)
            if row.result != "not_applicable" and not isclose(allocated, row.eligible_units, abs_tol=1e-9):
                raise ScoreContractError("score_denominator_nonconserving", "scoring.denominator_totality", context={"target_id": row.target_id, "claim": claim})
        for row in group:
            if row.na_reason: reasons[row.na_reason] = reasons.get(row.na_reason, 0) + 1
        amounts = {name: sum(slice.units for row in group for slice in row.outcome_slices if slice.result == name)
                   for name in ("complete", "within_tolerance", "miss", "conflict")}
        if not isclose(sum(amounts.values()), sum(row.eligible_units for row in group), abs_tol=1e-9):
            raise ScoreContractError("score_denominator_nonconserving", "scoring.denominator_totality", context={"claim": claim})
        output.append(ClaimSummaryV8(claim=claim, target_count=len(group), eligible_target_count=sum(row.eligible_units > 0 for row in group),
            partial_target_count=sum(0 < row.eligible_units < 1 for row in group), denominator_units=sum(row.eligible_units for row in group),
            complete_units=amounts["complete"], within_tolerance_units=amounts["within_tolerance"], miss_units=amounts["miss"], conflict_units=amounts["conflict"],
            not_applicable_target_count=sum(row.result == "not_applicable" for row in group), na_reasons=reasons))
    return tuple(output)


def classify_extra_observation(*, observation: OpeningObservation, absence_ledger: OpeningApplicabilityLedgerV1,
                               output_sha256: str, gt_segment_id: str) -> Literal["extra", "not_applicable"]:
    """Only a Va negative-evidence interval can establish an extra opening."""
    absence_id = absence_opening_id(output_sha256=output_sha256, observation=observation, gt_segment_id=gt_segment_id)
    opening = next((item for item in absence_ledger.openings if item.opening_id == absence_id), None)
    if opening is None: raise ScoreContractError("score_product_segment_unresolved", "scoring.matching", context={"observation_id": observation.id})
    span = observation.world_along_interval
    negatives = _merge((part.lo, part.hi) for claim in opening.claims for decision in claim.source_evidence for part in decision.negative_evidence_intervals)
    covered = _merge((max(lo, span[0]), min(hi, span[1])) for lo, hi in negatives
                     if max(lo, span[0]) < min(hi, span[1]))
    return "extra" if isclose(_length(covered), span[1] - span[0], abs_tol=1e-12) else "not_applicable"
