"""Judge-only planar segment extraction and deterministic matching (B4b-B).

The module intentionally works with actual polygon edges.  It has no rectangle
or ``W/D`` fallback: callers must give typed GT or typed correction geometry.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import product
import hashlib
import logging
from math import hypot
from typing import Iterable, Mapping, Sequence

from src.agent.correction.orthogonality import (
    ORTHOGONALITY_EPSILON,
    classify_edge_orthogonality,
)
from src.agent.correction.schema import CorrectedGeometryV3
from src.agent.judge.gt_schema import GroundTruthV3
from src.agent.judge.interval_ledger import (
    CanonicalCutRegistry,
    CoverageClaim,
    ObservationLedger,
    TargetLedger,
    build_coverage_claim,
    build_observation_ledger,
    build_target_ledger,
    claims_by_key,
    exact_float,
)
from src.agent.judge.certifier import (
    AnalysisCollector,
    CapabilityEnvelope,
    ConflictWitness,
    DEFAULT_EVALUATOR_REGISTRY,
    FactNode,
    FiniteFactGraph,
    JudgeDiagnostic,
    canonical_diagnostic_id,
    certify_and_arbitrate_request,
    collecting_into,
    is_collected_identity_abort,
)
from src.agent.judge.identity_provenance import (
    AliasCertificate,
    CoordinateOccurrence,
    CoordinateSourceKey,
    IdentityInputEnvelope,
    OwnerIdentity,
    SourceGeometryDocument,
    SourceRing,
    SourceSegment,
    SourceTopologyIndex,
    SourceVertex,
    adapt_correction_floor,
    adapt_gt_floor,
    adapt_reading_floor,
    certify_alias,
    identity_error_context,
    raise_identity_conflict,
    validate_occurrence_shape,
)
from src.agent.judge.score_schema import JudgeScoreConfigV1, ScoreContractError


Point = tuple[float, float]

_logger = logging.getLogger(__name__)


# W1 coordinate identity (representation-layer, NOT a geometric tolerance).
# Coordinates are NOT snapped to a global grid: any total discretization has a
# boundary, and a 1-ulp pair straddling it silently splits -- exactly the false
# red this batch removes (the r2 quantum-boundary pair).  Instead, the values
# actually appearing on each (side, floor, axis) are single-link clustered with
# a guard band and a diameter guard, so a same-intent seam (8.059999999999999 vs
# 8.06; 0.1+0.2 vs 0.3; a 1-ulp straddling pair) collapses to ONE atomic
# representative, and an unresolvable gap or a chain bridge is a LOUD reject.
# Pooling scope is per (side, floor, axis): GT and product pools never mix, so
# the answer's atoms are a pure function of the answer bytes (C-1').  Cross-
# document comparison happens only at the judge tolerance layer (plan_*_tol_m),
# product -> answer one-way (never reverse).
#
# Thresholds were MEASURED then chosen, not chosen then justified (R-2):
#   - signed-off sm24 GT: ZERO intra-document drift (50 vertices, no near pair);
#   - largest measured drift: 1 ulp at 8 m = 1.776357e-15 (13.0-4.94 vs 8.06);
#   - binary64 single-ulp bound across 1..20 m: 3.552714e-15.
# So merge sits 281x above the single-ulp bound (absorbs any arithmetic drift),
# split sits 100x below the 1e-9 minimum red gap (a 1e-9 seam stays split), and
# the open (merge, split) band is an explicit reject zone a grid cannot have.
# See AI_agent/logs/reviews/execution/2026-07-27_judge_identity_metric_glm.md
# for the full measurement table and two-sided headroom proof.
_COORDINATE_MERGE_THRESHOLD = 1e-12     # gap below this: same intent -> merge.
_COORDINATE_SPLIT_THRESHOLD = 1e-11     # gap above this: distinct intent -> split.
# §2.1 (controller ruling): the diameter cap MUST be <= the merge threshold.  A
# cap larger than merge (the prior 1e-11) lets a single-link chain of sub-merge
# gaps grow a cluster's diameter past the "same intent allowed spread" itself --
# three adjacent gaps each < merge can total 1.8e-12 > merge and still weld into
# one atom silently (contract ① violated, GREEN).  At diam = merge = 1e-12 a
# cluster may never span more than the very spread that defines "same intent".
_COORDINATE_DIAMETER_THRESHOLD = 1e-12  # cluster diameter cap (chain-bridge guard).

# Historical scalar-helper compatibility only.  Production matching no longer
# calls this tolerance or either scalar conservation helper: Slice 3 proves
# conservation from the exact atom partitions in ``interval_ledger``.
_SUBINTERVAL_SUM_TOL = 1e-9


@dataclass(frozen=True)
class _AxisIdentity:
    """One axis of one (side, floor): source slot -> atomic representative.

    The representative is the cluster minimum, which is deterministic once the
    occurrence set is fixed and never depends on input ordering.
    """
    side: str
    floor_id: str
    axis: str
    rep: Mapping[CoordinateSourceKey, float]
    certificates: tuple[AliasCertificate, ...] = ()


def _source_key_audit(key: CoordinateSourceKey) -> tuple[object, ...]:
    return key.audit_tuple()


def _cluster_occurrences(
    occurrences: tuple[CoordinateOccurrence, ...],
    *,
    side: str,
    floor_id: str,
    axis: str,
    topology: SourceTopologyIndex,
) -> _AxisIdentity:
    """C-1..C-3: source consistency, numeric proposals, structural proof."""
    grouped: dict[CoordinateSourceKey, list[CoordinateOccurrence]] = {}
    for occurrence in occurrences:
        key = occurrence.source_key
        if key.axis != axis or key.side != side or key.floor_id != floor_id:
            raise_identity_conflict(
                "score_identity_contract_mismatch",
                predicate="source_key_scope",
                source_vertex_ids=(_source_key_audit(key),),
                reason="identity_source_key_scope_mismatch",
                side=side,
                floor_id=floor_id,
                axis=axis,
            )
        value = occurrence.value
        if value != value or value == float("inf") or value == float("-inf"):
            raise_identity_conflict(
                "score_identity_non_finite",
                predicate="finite_coordinate",
                source_vertex_ids=(_source_key_audit(key),),
                reason="identity_non_finite_value",
                side=side,
                floor_id=floor_id,
                axis=axis,
                hex=value.hex(),
            )
        grouped.setdefault(key, []).append(occurrence)

    source_values: list[tuple[float, CoordinateSourceKey]] = []
    for key, samples in sorted(grouped.items()):
        values = sorted(sample.value for sample in samples)
        diameter = values[-1] - values[0]
        if diameter > _COORDINATE_MERGE_THRESHOLD:
            raise_identity_conflict(
                "score_identity_contract_mismatch",
                predicate="same_source_coordinate_consistency",
                source_vertex_ids=(_source_key_audit(key),),
                reason="same_source_coordinate_spread",
                side=side,
                floor_id=floor_id,
                axis=axis,
                original_hex=tuple(value.hex() for value in values),
                diameter=diameter,
                diameter_hex=diameter.hex(),
                merge=_COORDINATE_MERGE_THRESHOLD,
            )
        source_values.append((values[0], key))

    ordered = sorted(source_values, key=lambda item: (item[0], item[1]))
    if not ordered:
        return _AxisIdentity(side, floor_id, axis, {})

    clusters: list[list[tuple[float, CoordinateSourceKey]]] = [[ordered[0]]]
    for item in ordered[1:]:
        prev = clusters[-1][-1]
        gap = item[0] - prev[0]
        if gap < _COORDINATE_MERGE_THRESHOLD:
            clusters[-1].append(item)
        elif gap > _COORDINATE_SPLIT_THRESHOLD:
            clusters.append([item])
        else:
            raise_identity_conflict(
                "score_identity_guard_band_ambiguity",
                predicate="coordinate_guard_band",
                source_vertex_ids=(
                    _source_key_audit(prev[1]), _source_key_audit(item[1])
                ),
                reason="identity_guard_band_ambiguity",
                side=side,
                floor_id=floor_id,
                axis=axis,
                gap=gap,
                merge=_COORDINATE_MERGE_THRESHOLD,
                split=_COORDINATE_SPLIT_THRESHOLD,
                gap_hex=gap.hex(),
                lo_hex=prev[0].hex(),
                hi_hex=item[0].hex(),
                source_keys=(
                    _source_key_audit(prev[1]), _source_key_audit(item[1])
                ),
            )

    rep: dict[CoordinateSourceKey, float] = {}
    accepted: list[AliasCertificate] = []
    for cluster in clusters:
        lo, hi = cluster[0][0], cluster[-1][0]
        diameter = hi - lo
        if diameter > _COORDINATE_DIAMETER_THRESHOLD:
            raise_identity_conflict(
                "score_identity_chain_bridge",
                predicate="coordinate_cluster_diameter",
                source_vertex_ids=tuple(_source_key_audit(key) for _, key in cluster),
                reason="identity_chain_bridge_over_diameter",
                side=side,
                floor_id=floor_id,
                axis=axis,
                diameter=diameter,
                diameter_threshold=_COORDINATE_DIAMETER_THRESHOLD,
                diameter_hex=diameter.hex(),
                lo_hex=lo.hex(),
                hi_hex=hi.hex(),
            )

        adjacency: dict[CoordinateSourceKey, set[CoordinateSourceKey]] = {
            key: set() for _, key in cluster
        }
        for left_index, (left_value, left_key) in enumerate(cluster):
            for right_value, right_key in cluster[left_index + 1:]:
                if left_value == right_value:
                    adjacency[left_key].add(right_key)
                    adjacency[right_key].add(left_key)
                    continue
                certificate = certify_alias(left_key, right_key, axis, topology)
                if certificate is not None:
                    adjacency[left_key].add(right_key)
                    adjacency[right_key].add(left_key)
                    accepted.append(certificate)

        anchor = min(adjacency)
        reached = {anchor}
        work = [anchor]
        while work:
            current = work.pop()
            for neighbour in sorted(adjacency[current]):
                if neighbour not in reached:
                    reached.add(neighbour)
                    work.append(neighbour)
        if len(reached) != len(cluster):
            unresolved = sorted(set(adjacency).difference(reached))
            candidate_pair = (anchor, unresolved[0])
            values_by_key = {key: value for value, key in cluster}
            for ref in topology.half_edges:
                endpoints = {
                    key.element_index: key
                    for _, key in cluster
                    if key.owner_kind == ref.owner_kind
                    and key.owner_id == ref.owner_id
                    and key.ring_id == ref.ring_id
                    and key.element_index in {
                        ref.start_vertex_index, ref.end_vertex_index
                    }
                }
                if set(endpoints) == {
                    ref.start_vertex_index, ref.end_vertex_index
                }:
                    left = endpoints[ref.start_vertex_index]
                    right = endpoints[ref.end_vertex_index]
                    if right not in adjacency[left]:
                        lo_value, hi_value = sorted(
                            (values_by_key[left], values_by_key[right])
                        )
                        diameter = hi_value - lo_value
                        raise_identity_conflict(
                            "score_identity_merge_collapse",
                            predicate="ring_identity_conflict",
                            owner_ids=(ref.owner_id,),
                            source_edge_ids=(ref.edge_id,),
                            source_vertex_ids=(
                                (left.side, left.floor_id, left.owner_kind,
                                 left.owner_id, left.ring_id, left.endpoint_side,
                                 left.element_index),
                                (right.side, right.floor_id, right.owner_kind,
                                 right.owner_id, right.ring_id, right.endpoint_side,
                                 right.element_index),
                            ),
                            reason="identity_merge_edge_collapse",
                            side=side,
                            floor_id=floor_id,
                            axis=axis,
                            source_keys=(
                                left.audit_tuple(), right.audit_tuple()
                            ),
                            v1_hex=lo_value.hex(),
                            v2_hex=hi_value.hex(),
                            diameter=diameter,
                            diameter_hex=diameter.hex(),
                        )
            raise_identity_conflict(
                "score_identity_contract_mismatch",
                predicate="cross_source_alias",
                owner_ids=tuple(sorted({
                    (key.owner_kind, key.owner_id) for _, key in cluster
                })),
                source_vertex_ids=tuple(_source_key_audit(key) for _, key in cluster),
                reason="unproven_cross_source_alias",
                side=side,
                floor_id=floor_id,
                axis=axis,
                candidate_pair=tuple(_source_key_audit(key) for key in candidate_pair),
                candidate_hex=tuple(value.hex() for value, _ in cluster),
                structural_relation="none",
            )
        for _, key in cluster:
            rep[key] = lo + 0.0
    return _AxisIdentity(
        side, floor_id, axis, rep,
        tuple(sorted(set(accepted), key=lambda cert: cert.certificate_id)),
    )


def _cluster_axis(
    occurrences: Iterable[CoordinateOccurrence],
    *,
    side: str,
    floor_id: str,
    axis: str,
    topology: SourceTopologyIndex,
) -> _AxisIdentity:
    """Cluster source-bearing occurrences under an explicit topology contract."""
    materialized = tuple(occurrences)
    if any(not isinstance(item, CoordinateOccurrence) for item in materialized):
        raise TypeError("identity clustering requires CoordinateOccurrence")
    return _cluster_occurrences(
        materialized, side=side, floor_id=floor_id, axis=axis, topology=topology
    )


def _build_floor_identity(
    envelope: IdentityInputEnvelope,
) -> tuple[_AxisIdentity, _AxisIdentity]:
    """Build source-key identities from one exact-version typed envelope."""
    validate_occurrence_shape(envelope)
    x_occurrences = tuple(
        item for item in envelope.occurrences if item.source_key.axis == "x"
    )
    y_occurrences = tuple(
        item for item in envelope.occurrences if item.source_key.axis == "y"
    )
    x_id = _cluster_axis(
        x_occurrences, side=envelope.side, floor_id=envelope.floor_id, axis="x",
        topology=envelope.topology,
    )
    y_id = _cluster_axis(
        y_occurrences, side=envelope.side, floor_id=envelope.floor_id, axis="y",
        topology=envelope.topology,
    )
    assert isinstance(x_id, _AxisIdentity) and isinstance(y_id, _AxisIdentity)
    return x_id, y_id


def _identify_point(
    point: SourceVertex, x_id: _AxisIdentity, y_id: _AxisIdentity
) -> Point:
    return (
        x_id.rep[point.x_source] + 0.0,
        y_id.rep[point.y_source] + 0.0,
    )


@dataclass(frozen=True)
class PlanSegment:
    """A judge observation/target; ``key`` is audit data, never a tie breaker."""
    key: str
    floor_id: str
    p1: Point
    p2: Point
    zone_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    exterior: bool = True

    @property
    def length(self) -> float:
        return hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1])


@dataclass(frozen=True)
class PlanSupportAmbiguity:
    """One product observation that cannot select a unique GT support line."""
    observation_key: str
    candidate_target_keys: tuple[str, ...]
    support_lines: tuple[tuple[str, str, float], ...]


@dataclass(frozen=True)
class SegmentAssignment:
    matched: tuple[tuple[PlanSegment, PlanSegment], ...]
    unmatched_targets: tuple[PlanSegment, ...]
    unmatched_observations: tuple[PlanSegment, ...]


@dataclass(frozen=True)
class SegmentScore:
    target: PlanSegment | None
    observation: PlanSegment | None
    status: str  # complete | within_tolerance | miss | extra | duplicate
    axis_alignment_error_m: float | None
    position_error_m: float | None
    extent_symmetric_difference_m: float | None
    # W4: length (m) this row contributes to its criterion denominator.  For a
    # matched row, the overlap length on the target's support line; for a miss,
    # the uncovered target length; for an extra, the observation length that
    # covered no target; for a duplicate, the target length covered by >1 obs.
    # Replaces the prior per-segment count of 1 (which over-weighted walls
    # facing many rooms by up to 3.96x on real sm24).
    eligible_units: float = 0.0
    # Exact source for ``eligible_units``.  Production rows set this from the
    # ledger before the one public binary64 rounding; canonical audit must not
    # reconstruct it from the public float.
    eligible_units_exact: Fraction = Fraction(0)


def _exact_orientation(a: Point, b: Point, c: Point) -> int:
    ax, ay = Fraction.from_float(a[0]), Fraction.from_float(a[1])
    bx, by = Fraction.from_float(b[0]), Fraction.from_float(b[1])
    cx, cy = Fraction.from_float(c[0]), Fraction.from_float(c[1])
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return (cross > 0) - (cross < 0)


def _on_exact_segment(a: Point, b: Point, point: Point) -> bool:
    return (
        _exact_orientation(a, b, point) == 0
        and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    oa, ob = _exact_orientation(a, b, c), _exact_orientation(a, b, d)
    oc, od = _exact_orientation(c, d, a), _exact_orientation(c, d, b)
    if oa * ob < 0 and oc * od < 0:
        return True
    return (
        (oa == 0 and _on_exact_segment(a, b, c))
        or (ob == 0 and _on_exact_segment(a, b, d))
        or (oc == 0 and _on_exact_segment(c, d, a))
        or (od == 0 and _on_exact_segment(c, d, b))
    )


def _ring_context(
    ring: SourceRing,
    *,
    predicate: str,
    source_vertex_ids: Iterable[object],
    source_edge_ids: Iterable[object] = (),
    reason: str,
    **extra: object,
) -> dict[str, object]:
    return identity_error_context(
        predicate=predicate,
        owner_ids=(ring.owner_id,),
        source_edge_ids=source_edge_ids,
        source_vertex_ids=source_vertex_ids,
        reason=reason,
        side=ring.vertices[0].x_source.side if ring.vertices else "",
        floor_id=ring.vertices[0].x_source.floor_id if ring.vertices else "",
        owner_identities=(ring.owner,),
        **extra,
    )


def _raise_identity_context(code: str, context: Mapping[str, object]) -> None:
    """Route an already-built C-4 witness through the request arbiter."""
    reserved = {
        "predicate",
        "owner_ids",
        "source_edge_ids",
        "source_vertex_ids",
        "depends_on_capability_ids",
        "authority",
        "proof_status",
        "predicate_schema_version",
    }
    raise_identity_conflict(
        code,
        predicate=str(context["predicate"]),
        owner_ids=context.get("owner_ids", ()),
        source_edge_ids=context.get("source_edge_ids", ()),
        source_vertex_ids=context.get("source_vertex_ids", ()),
        depends_on_capability_ids=context.get(
            "depends_on_capability_ids", ()
        ),
        **{
            key: value
            for key, value in context.items()
            if key not in reserved
        },
    )


def _points(
    ring: SourceRing,
    x_id: _AxisIdentity,
    y_id: _AxisIdentity,
    *,
    identity_code: str,
) -> tuple[Point, ...]:
    """Rebuild a ring by source key, then certify its generic exact topology."""
    source_vertices = ring.vertices[:-1] if ring.explicit_closure else ring.vertices
    out = tuple(_identify_point(point, x_id, y_id) for point in source_vertices)
    raw = tuple(point.raw_point for point in source_vertices)
    if len(out) < 3:
        _raise_identity_context(
            identity_code,
            _ring_context(
                ring, predicate="ring_identity_conflict",
                source_vertex_ids=tuple(vertex.vertex_id for vertex in source_vertices),
                reason="polygon_too_short",
            ),
        )
    ring_n = len(out)
    for idx in range(ring_n):
        next_index = (idx + 1) % ring_n
        if out[idx] == out[next_index]:
            v1, v2 = raw[idx], raw[next_index]
            diameter = hypot(v1[0] - v2[0], v1[1] - v2[1])
            _raise_identity_context(
                "score_identity_merge_collapse",
                _ring_context(
                    ring,
                    predicate="ring_identity_conflict",
                    source_vertex_ids=(
                        source_vertices[idx].vertex_id,
                        source_vertices[next_index].vertex_id,
                    ),
                    source_edge_ids=((ring.owner_kind, ring.owner_id, ring.ring_id, idx),),
                    reason="identity_merge_edge_collapse",
                    axis="xy",
                    v1_hex=(v1[0].hex(), v1[1].hex()),
                    v2_hex=(v2[0].hex(), v2[1].hex()),
                    source_keys=(
                        source_vertices[idx].x_source.audit_tuple(),
                        source_vertices[idx].y_source.audit_tuple(),
                        source_vertices[next_index].x_source.audit_tuple(),
                        source_vertices[next_index].y_source.audit_tuple(),
                    ),
                    diameter=diameter,
                    diameter_hex=diameter.hex(),
                ),
            )

    for first in range(ring_n):
        for second in range(first + 1, ring_n):
            adjacent = second == first + 1 or (first == 0 and second == ring_n - 1)
            if not adjacent and out[first] == out[second]:
                _raise_identity_context(
                    identity_code,
                    _ring_context(
                        ring,
                        predicate="ring_identity_conflict",
                        source_vertex_ids=(
                            source_vertices[first].vertex_id,
                            source_vertices[second].vertex_id,
                        ),
                        reason="ring_nonadjacent_vertex_repeat",
                    ),
                )

    edges = tuple((out[index], out[(index + 1) % ring_n]) for index in range(ring_n))
    for first in range(ring_n):
        for second in range(first + 1, ring_n):
            adjacent = second == first + 1 or (first == 0 and second == ring_n - 1)
            a, b = edges[first]
            c, d = edges[second]
            if adjacent:
                # Adjacent edges may meet only at their declared endpoint; any
                # collinear backtrack creates a positive overlap.
                if _exact_orientation(a, b, c) == _exact_orientation(a, b, d) == 0:
                    common = {a, b}.intersection({c, d})
                    backtracks = False
                    if len(common) == 1:
                        shared = next(iter(common))
                        first_other = a if b == shared else b
                        second_other = c if d == shared else d
                        backtracks = (
                            _on_exact_segment(a, b, second_other)
                            or _on_exact_segment(c, d, first_other)
                        )
                    if len(common) != 1 or backtracks:
                        _raise_identity_context(
                            identity_code,
                            _ring_context(
                                ring, predicate="ring_identity_conflict",
                                source_vertex_ids=tuple(
                                    source_vertices[index].vertex_id
                                    for index in {first, (first + 1) % ring_n,
                                                  second, (second + 1) % ring_n}
                                ),
                                source_edge_ids=(
                                    (ring.owner_kind, ring.owner_id, ring.ring_id, first),
                                    (ring.owner_kind, ring.owner_id, ring.ring_id, second),
                                ),
                                reason="ring_adjacent_edge_backtrack",
                            ),
                        )
                continue
            if _segments_intersect(a, b, c, d):
                _raise_identity_context(
                    identity_code,
                    _ring_context(
                        ring,
                        predicate="ring_identity_conflict",
                        source_vertex_ids=tuple(
                            source_vertices[index].vertex_id
                            for index in {first, (first + 1) % ring_n,
                                          second, (second + 1) % ring_n}
                        ),
                        source_edge_ids=(
                            (ring.owner_kind, ring.owner_id, ring.ring_id, first),
                            (ring.owner_kind, ring.owner_id, ring.ring_id, second),
                        ),
                        reason="ring_nonadjacent_edge_intersection",
                    ),
                )
    return out


def _edges(
    ring: SourceRing,
    x_id: _AxisIdentity,
    y_id: _AxisIdentity,
    *,
    identity_code: str,
) -> tuple[tuple[Point, Point], ...]:
    identified = _points(ring, x_id, y_id, identity_code=identity_code)
    return tuple(
        (identified[index], identified[(index + 1) % len(identified)])
        for index in range(len(identified))
    )


@dataclass(frozen=True)
class _SourceEdgeClaim:
    edge_id: tuple[object, ...]
    owner: OwnerIdentity
    p1: Point
    p2: Point
    source_vertex_ids: tuple[tuple[object, ...], tuple[object, ...]]
    endpoint_sources: tuple[
        tuple[CoordinateSourceKey, CoordinateSourceKey],
        tuple[CoordinateSourceKey, CoordinateSourceKey],
    ]
    previous_edge_id: tuple[object, ...]
    next_edge_id: tuple[object, ...]
    side: str


def _source_edge_claims(
    ring: SourceRing,
    x_id: _AxisIdentity,
    y_id: _AxisIdentity,
    *,
    identity_code: str,
) -> tuple[_SourceEdgeClaim, ...]:
    """Pair identified geometry with the source half-edge that produced it."""
    geometries = _edges(
        ring, x_id, y_id, identity_code=identity_code
    )
    source_vertices = (
        ring.vertices[:-1] if ring.explicit_closure else ring.vertices
    )
    count = len(source_vertices)
    side = source_vertices[0].x_source.side if source_vertices else ""

    def edge_id(index: int) -> tuple[object, ...]:
        return (
            side,
            x_id.floor_id,
            ring.owner_kind,
            ring.owner_id,
            ring.ring_id,
            index,
        )

    return tuple(
        _SourceEdgeClaim(
            edge_id=edge_id(index),
            owner=ring.owner,
            p1=geometry[0],
            p2=geometry[1],
            source_vertex_ids=(
                source_vertices[index].vertex_id,
                source_vertices[(index + 1) % count].vertex_id,
            ),
            endpoint_sources=(
                (
                    source_vertices[index].x_source,
                    source_vertices[index].y_source,
                ),
                (
                    source_vertices[(index + 1) % count].x_source,
                    source_vertices[(index + 1) % count].y_source,
                ),
            ),
            previous_edge_id=edge_id((index - 1) % count),
            next_edge_id=edge_id((index + 1) % count),
            side=side,
        )
        for index, geometry in enumerate(geometries)
    )


def _identify_segment(
    segment: SourceSegment, x_id: _AxisIdentity, y_id: _AxisIdentity
) -> tuple[Point, Point]:
    return (
        _identify_point(segment.p1, x_id, y_id),
        _identify_point(segment.p2, x_id, y_id),
    )


def _edge_key(p1: Point, p2: Point) -> tuple[Point, Point]:
    return p1, p2


def _lies_on_exterior(edge: tuple[Point, Point], exterior: Iterable[tuple[Point, Point]]) -> bool:
    """Exact axis-aligned containment allows a tiled zone's split exterior edge."""
    (a, b) = edge
    for p1, p2 in exterior:
        if p1[0] == p2[0] == a[0] == b[0] and min(p1[1], p2[1]) <= min(a[1], b[1]) and max(a[1], b[1]) <= max(p1[1], p2[1]): return True
        if p1[1] == p2[1] == a[1] == b[1] and min(p1[0], p2[0]) <= min(a[0], b[0]) and max(a[0], b[0]) <= max(p1[0], p2[0]): return True
    return False


def _canonical_geometry(segment: PlanSegment) -> tuple:
    return (segment.floor_id, min(segment.p1, segment.p2), max(segment.p1, segment.p2), segment.exterior,
            tuple(sorted(segment.zone_ids)))


def _assert_owner_atom(
    left_owner: OwnerIdentity,
    right_owner: OwnerIdentity,
    *,
    floor_id: str,
    source_edge_ids: Iterable[object] = (),
) -> None:
    """A reverse interior atom needs two distinct ``(owner_kind, owner_id)``."""
    if left_owner == right_owner:
        raise_identity_conflict(
            "score_identity_contract_mismatch",
            predicate="owner_atom_multiplicity",
            owner_ids=(left_owner[1],),
            source_edge_ids=source_edge_ids,
            reason="interior_atom_same_owner_reverse",
            side="",
            floor_id=floor_id,
            owner_identities=(left_owner, right_owner),
        )


def _edge_endpoint_fact_ids(
    claim: _SourceEdgeClaim,
) -> tuple[tuple[object, ...], ...]:
    return (
        ("edge_endpoint", claim.edge_id, "start", "x"),
        ("edge_endpoint", claim.edge_id, "start", "y"),
        ("edge_endpoint", claim.edge_id, "end", "x"),
        ("edge_endpoint", claim.edge_id, "end", "y"),
    )


def _pair_diagnostic(
    *,
    identity_code: str,
    floor_id: str,
    reason: str,
    predicate: str,
    claims: Iterable[_SourceEdgeClaim],
    locus: tuple[Point, Point],
    caused_by: Iterable[str] = (),
) -> _PairDiagnostic:
    selected = tuple(sorted(claims, key=lambda item: repr(item.edge_id)))
    edge_ids = tuple(item.edge_id for item in selected)
    required = tuple(
        fact
        for item in selected
        for fact in (item.edge_id, *_edge_endpoint_fact_ids(item))
    )
    witness = ConflictWitness(
        predicate=predicate,
        predicate_schema_version="1",
        source_edge_ids=edge_ids,
        source_vertex_ids=tuple(
            vertex for item in selected for vertex in item.source_vertex_ids
        ),
        owner_ids=tuple(item.owner[1] for item in selected),
        locus=locus,
        required_fact_ids=required,
        direction=(
            "forward_reverse"
            if predicate != "owner_multiplicity"
            else "same_direction"
        ),
        expected_reverse_slots=(
            (("reverse_owner", edge_ids[0]),) if edge_ids else ()
        ),
    )
    return _PairDiagnostic(
        category="identity",
        code=identity_code,
        gate_id="scoring.input_identity",
        context={"reason": reason, "floor_id": floor_id},
        witness=witness,
        caused_by=tuple(caused_by),
        side=selected[0].side if selected else "",
    )


def _advisory_capability(
    claim: _SourceEdgeClaim,
    *,
    floor_id: str,
) -> CapabilityEnvelope:
    """Build the finite W5 dependency chain from small-axis source slots."""
    dx = claim.p2[0] - claim.p1[0]
    dy = claim.p2[1] - claim.p1[1]
    abs_dx, abs_dy = abs(dx), abs(dy)
    small_axis = "x" if abs_dx <= abs_dy else "y"
    axis_index = 0 if small_axis == "x" else 1
    complete = not (
        0.0 < abs_dx <= ORTHOGONALITY_EPSILON
        and 0.0 < abs_dy <= ORTHOGONALITY_EPSILON
    )
    seed_keys = (
        claim.endpoint_sources[0][axis_index],
        claim.endpoint_sources[1][axis_index],
    )
    enclosure = tuple(sorted((
        claim.p1[axis_index], claim.p2[axis_index]
    )))
    graph = FiniteFactGraph()
    seed_ids: list[tuple[object, ...]] = []
    for key in seed_keys:
        fact_id = ("source_coordinate", key.audit_tuple())
        graph.add(FactNode(fact_id, "source"))
        seed_ids.append(fact_id)

    endpoint_rows = (
        (
            claim.previous_edge_id,
            "end",
            seed_ids[0],
            claim.p1[axis_index],
        ),
        (
            claim.edge_id,
            "start",
            seed_ids[0],
            claim.p1[axis_index],
        ),
        (
            claim.edge_id,
            "end",
            seed_ids[1],
            claim.p2[axis_index],
        ),
        (
            claim.next_edge_id,
            "start",
            seed_ids[1],
            claim.p2[axis_index],
        ),
    )
    endpoint_ids: list[tuple[object, ...]] = []
    for edge_id, endpoint, seed, value in endpoint_rows:
        fact_id = ("edge_endpoint", edge_id, endpoint, small_axis)
        graph.add(
            FactNode(
                fact_id,
                "edge",
                (seed,),
                enclosure,
            )
        )
        endpoint_ids.append(fact_id)
        # The source edge identity itself is fixed, while its derived endpoint
        # coordinate is dependent.  Keeping the raw id in the closure lets the
        # predicate evaluator select a fixed core from unrelated edges.
        graph.nodes.setdefault(
            edge_id, FactNode(edge_id, "source")
        )

    cut_ids: list[tuple[object, ...]] = []
    atom_ids: list[tuple[object, ...]] = []
    for endpoint_id in (endpoint_ids[0], endpoint_ids[3]):
        cut_id = ("cut_token", endpoint_id)
        graph.add(FactNode(cut_id, "support_cut", (endpoint_id,)))
        atom_id = ("owner_atom", cut_id)
        graph.add(FactNode(atom_id, "atom_owner", (cut_id,)))
        cut_ids.append(cut_id)
        atom_ids.append(atom_id)

    dependent, arcs = graph.dependency_closure(seed_ids)
    # Edge ids are dependency labels as well as immutable identities: an
    # advisory may vary a neighbouring edge's endpoint-derived support, but
    # never its owner/ring identity.
    dependent = tuple(
        sorted(
            set(dependent)
            | {claim.edge_id, claim.previous_edge_id, claim.next_edge_id},
            key=repr,
        )
    )
    payload = repr((claim.edge_id, seed_keys, claim.p1, claim.p2)).encode()
    capability_id = "w5:" + hashlib.sha256(payload).hexdigest()[:20]
    return CapabilityEnvelope(
        capability_id=capability_id,
        kind="near_orthogonal_advisory_unpaired",
        source_edge_id=claim.edge_id,
        source_vertex_ids=claim.source_vertex_ids,
        seed_coordinate_keys=seed_keys,
        dependent_fact_ids=dependent,
        dependency_arcs=arcs,
        fixed_invariants=(
            ("source_edge", claim.edge_id),
            ("owner", claim.owner),
            ("large_axis", "y" if small_axis == "x" else "x"),
            (
                "small_axis_enclosure",
                small_axis,
                enclosure[0].hex(),
                enclosure[1].hex(),
            ),
        ),
        complete=complete,
        side=claim.side,
        floor_id=floor_id,
        edge_hex=(
            claim.p1[0].hex(),
            claim.p1[1].hex(),
            claim.p2[0].hex(),
            claim.p2[1].hex(),
        ),
    )


def _tile_orthogonal_edges(
    directed: dict[tuple[Point, Point], list[_SourceEdgeClaim]],
    exterior_edges: tuple[tuple[Point, Point], ...],
    floor_id: str,
    *,
    identity_code: str,
    diagnostics: list[_PairDiagnostic],
) -> list[tuple[Point, Point, tuple[OwnerIdentity, ...]]]:
    """Tile axis-aligned shared edges into T-junction sub-intervals (exact coverage).

    Every directed edge on a shared supporting line is tiled at the union of all
    endpoints into elementary sub-intervals; each sub-interval is attributed to
    the single zone on each side and becomes one paired segment.  Coverage must
    be exact -- a gap, an overlap, or an endpoint that does not meet precisely
    is a real topology break, not a T-junction, and is rejected.  Collinearity
    and coverage both use exact equality with no snapping or tolerance, mirroring
    ``_lies_on_exterior``: snapping would turn a nearby wall into a fictional
    shared edge.  Coordinates are already canonicalized at the scorer entry (RW-1).
    """
    # Bucket directed edges by supporting orthogonal line and traversal side.
    # CCW polygons on opposite sides of a wall traverse it in opposite
    # directions, so the two sides land in forward/reverse separately.
    lines: dict[
        tuple[str, float],
        dict[str, list[tuple[float, float, _SourceEdgeClaim]]],
    ] = {}
    for (p1, p2), claims in directed.items():
        if p1[0] == p2[0]:
            axis, const = "V", p1[0]
            lo, hi = (p1[1], p2[1]) if p1[1] <= p2[1] else (p2[1], p1[1])
            forward = p2[1] > p1[1]
        else:
            axis, const = "H", p1[1]
            lo, hi = (p1[0], p2[0]) if p1[0] <= p2[0] else (p2[0], p1[0])
            forward = p2[0] > p1[0]
        side = "fwd" if forward else "rev"
        for claim in claims:
            lines.setdefault((axis, const), {"fwd": [], "rev": []})[side].append((lo, hi, claim))

    pairs: list[tuple[Point, Point, tuple[OwnerIdentity, ...]]] = []
    for (axis, const), group in lines.items():
        cuts = sorted({coord for span in (group["fwd"] + group["rev"]) for coord in span[:2]})
        for lo, hi in zip(cuts, cuts[1:]):
            if lo == hi:
                continue
            geometry = ((const, lo), (const, hi)) if axis == "V" else ((lo, const), (hi, const))
            forward_claims = [claim for left, right, claim in group["fwd"] if left <= lo and hi <= right]
            reverse_claims = [claim for left, right, claim in group["rev"] if left <= lo and hi <= right]
            on_exterior = _lies_on_exterior(geometry, exterior_edges)
            if forward_claims and reverse_claims:
                if on_exterior:
                    diagnostics.append(_pair_diagnostic(
                        identity_code=identity_code, floor_id=floor_id,
                        reason="exterior_interior_topology_conflict",
                        predicate="exterior_interior_conflict",
                        claims=(*forward_claims, *reverse_claims), locus=geometry,
                    ))
                    continue
                if len(forward_claims) != 1 or len(reverse_claims) != 1:
                    diagnostics.append(_pair_diagnostic(
                        identity_code=identity_code, floor_id=floor_id,
                        reason="invalid_interior_edge_pair",
                        predicate="owner_multiplicity",
                        claims=(*forward_claims, *reverse_claims), locus=geometry,
                    ))
                    continue
                _assert_owner_atom(
                    forward_claims[0].owner,
                    reverse_claims[0].owner,
                    floor_id=floor_id,
                    source_edge_ids=(
                        forward_claims[0].edge_id,
                        reverse_claims[0].edge_id,
                    ),
                )
                pairs.append((geometry[0], geometry[1], tuple(sorted((
                    forward_claims[0].owner, reverse_claims[0].owner
                )))))
            elif forward_claims or reverse_claims:
                present = forward_claims or reverse_claims
                if on_exterior:
                    # RW-3: a conforming tiling has exactly one owner per exterior
                    # edge per side.  Two same-direction owners on one
                    # exterior-only sub-interval means a zone is duplicated over
                    # the footprint (the silent-green hole).  This catches that
                    # specific shape -- it is NOT a claim that the helper detects
                    # every overlap; area-level zone overlap is the upstream
                    # coverage validator's job and a different layer.
                    if len(forward_claims) > 1 or len(reverse_claims) > 1:
                        diagnostics.append(_pair_diagnostic(
                            identity_code=identity_code, floor_id=floor_id,
                            reason="exterior_duplicate_owner",
                            predicate="owner_multiplicity",
                            claims=present, locus=geometry,
                        ))
                        continue
                    # single-owner exterior span: a legitimate boundary edge, not a wall
                else:
                    diagnostics.append(_pair_diagnostic(
                        identity_code=identity_code, floor_id=floor_id,
                        reason="invalid_interior_edge_pair",
                        predicate="missing_reverse_owner",
                        claims=present, locus=geometry,
                    ))
                    continue
            # else: open span with no edge on either side (e.g. a corridor mouth)
    return pairs


def _pair_general_edges(
    directed: dict[tuple[Point, Point], list[_SourceEdgeClaim]],
    exterior_edges: tuple[tuple[Point, Point], ...],
    floor_id: str,
    *,
    identity_code: str,
    diagnostics: list[_PairDiagnostic],
) -> list[tuple[Point, Point, tuple[OwnerIdentity, ...]]]:
    """Pair non-axis-aligned edges by exact reverse (RW-2 general-segment seam).

    The scorer must not reinterpret a near-orthogonal edge the upstream validator
    accepted as a topology break: ``cell_geometry`` admits edges with dx<1e-9 as
    orthogonal, so a legal correction can carry a dx=5e-10 exact-reverse seam that
    the GT/exact axis check would otherwise reject.  Such an edge is paired only
    with its exact reverse; a one-sided general edge with no reverse, or a
    same-direction duplicate owner, is a real topology break.  Footprint/exterior
    rings are axis-aligned (validated upstream), so a general edge is never on
    the exterior here -- the exterior/interior conflict check is unreachable on
    this path and intentionally omitted.  C2 enables orthogonal T-junction tiling
    today; a future non-orthogonal profile extends here without rewriting the API
    or the orthogonal path (invariant #6).
    """
    pairs: list[tuple[Point, Point, tuple[OwnerIdentity, ...]]] = []
    consumed: set[tuple[Point, Point]] = set()
    for edge, claims in directed.items():
        if edge in consumed:
            continue
        p1, p2 = edge
        reverse = (p2, p1)
        reverse_claims = directed.get(reverse)
        if reverse_claims is None:
            diagnostics.append(_pair_diagnostic(
                identity_code=identity_code, floor_id=floor_id,
                reason="invalid_interior_edge_pair",
                predicate="missing_reverse_owner",
                claims=claims, locus=(p1, p2),
            ))
            continue
        if len(claims) != 1 or len(reverse_claims) != 1:
            diagnostics.append(_pair_diagnostic(
                identity_code=identity_code, floor_id=floor_id,
                reason="invalid_interior_edge_pair",
                predicate="owner_multiplicity",
                claims=(*claims, *reverse_claims), locus=(p1, p2),
            ))
            consumed.add(edge)
            consumed.add(reverse)
            continue
        _assert_owner_atom(
            claims[0].owner, reverse_claims[0].owner, floor_id=floor_id,
            source_edge_ids=(claims[0].edge_id, reverse_claims[0].edge_id),
        )
        pairs.append((p1, p2, tuple(sorted((
            claims[0].owner, reverse_claims[0].owner
        )))))
        consumed.add(edge)
        consumed.add(reverse)
    return pairs


def _log_advisory_hit(
    floor_id: str,
    p1: Point,
    p2: Point,
    *,
    unpaired: bool = False,
    capability_id: str | None = None,
) -> None:
    """Record a near-orthogonal advisory edge in the runtime artifact (R-4 / §1.3).

    This is the runtime artifact for R-4's two-stage gating: a real run must be
    able to answer "did any near-orthogonal edge fire, and how many" before the
    advisory is allowed to flip to blocking.  A PAIRED advisory edge (exact
    reverse) is recorded as ``near_orthogonal_advisory_hit`` -- this batch only
    ADVISES (the edge is paired and scored normally).  An UNPAIRED advisory edge
    -- the exact shape that resolves as capability NA and that the later flip-to-
    blocking most needs to count -- is recorded as
    ``near_orthogonal_advisory_unpaired``.  Before r3 only the paired hits were
    logged, so the count was blind to precisely the edges that trigger NA.  The
    structured log carries the floor and the binary64 hex of both endpoints for
    reproducibility.
    """
    event = "near_orthogonal_advisory_unpaired" if unpaired else "near_orthogonal_advisory_hit"
    _logger.info(
        event,
        extra={"event": event, "floor_id": floor_id, "unpaired": unpaired,
               "capability_id": capability_id,
               "p1_hex": (float(p1[0]).hex(), float(p1[1]).hex()),
               "p2_hex": (float(p2[0]).hex(), float(p2[1]).hex())},
    )


@dataclass(frozen=True)
class _PairDiagnostic:
    """One problem found while pairing interior edges, collected not raised.

    R2-B1: pairing problems are collected into a list and arbitrated AFTER all
    three buckets (advisory / orthogonal tile / general) have run, so a real
    topology break can never be masked by a capability NA (the r2 false-green:
    advisory ran first and its NA hid a true seam on the same floor).  The
    ``category`` is "identity" (a real topology break or a zone duplication) or
    "capability" (a shape the judge cannot measure).  ``code`` / ``gate_id`` /
    ``context`` reconstruct the exact ScoreContractError the prior raise would
    have thrown, so the arbitrator's chosen diagnostic is byte-identical to the
    pre-r2 behaviour for whichever diagnostic wins arbitration.
    """
    category: str
    code: str
    gate_id: str
    context: dict
    witness: ConflictWitness | None = None
    caused_by: tuple[str, ...] = ()
    side: str = ""

    def as_judge_diagnostic(self) -> JudgeDiagnostic:
        reason = str(self.context.get("reason", "pairing_diagnostic"))
        floor_id = str(self.context.get("floor_id", ""))
        witness = self.witness
        if witness is None and self.category == "identity":
            predicate = {
                "exterior_duplicate_owner": "owner_multiplicity",
                "exterior_interior_topology_conflict": "exterior_interior_conflict",
                "invalid_interior_edge_pair": "missing_reverse_owner",
            }.get(reason, "missing_reverse_owner")
            edge_count = 2 if predicate in {
                "owner_multiplicity", "exterior_interior_conflict"
            } else 1
            synthetic_edges = tuple(
                ("compat_pairing", floor_id, index)
                for index in range(edge_count)
            )
            witness = ConflictWitness(
                predicate=predicate,
                predicate_schema_version="1",
                source_edge_ids=synthetic_edges,
                source_vertex_ids=(),
                owner_ids=tuple(f"owner-{index}" for index in range(edge_count)),
                locus=((0.0, 0.0), (1.0, 0.0)),
                required_fact_ids=synthetic_edges,
                expected_reverse_slots=(("reverse_owner", synthetic_edges[0]),),
            )
        return JudgeDiagnostic(
            diagnostic_id=canonical_diagnostic_id(
                side=self.side,
                floor_id=floor_id,
                reason=reason,
                witness=witness,
            ),
            requested_code=self.code,
            gate_id=self.gate_id,
            reason=reason,
            floor_id=floor_id,
            witness=witness,
            caused_by=self.caused_by,
            side=self.side,
            context=self.context,
        )


def _pair_advisory_edges(
    advisory: dict[tuple[Point, Point], list[_SourceEdgeClaim]],
    floor_id: str,
    *,
    diagnostics: list[_PairDiagnostic],
    capabilities: list[CapabilityEnvelope],
) -> list[tuple[Point, Point, tuple[OwnerIdentity, ...]]]:
    """Pair near-orthogonal advisory edges by exact reverse (W5 / R-4).

    Production admitted these edges (dx or dy in (0, 1e-9]) as axis-aligned, so
    the judge may NEVER brand them as a topology break -- that was the structural
    root of the false-red rounds (the judge convicting legal upstream geometry
    with its own exactness ceiling).  Each advisory edge pairs only with its
    EXACT reverse; a one-sided advisory edge, or one whose reverse carries a
    different near-axis spelling (5e-10 vs 4e-10), is a shape the judge's exact
    path cannot measure, so it resolves as capability NA
    (``score_unsupported_combination``), never ``score_*_identity_invalid``.  A
    successfully paired advisory edge is recorded (``_log_advisory_hit``) so
    R-4's later flip-to-blocking has a real-run signal to count.  Footprint and
    exterior rings are axis-aligned (validated upstream), so an advisory edge is
    never on the exterior here and no exterior/interior conflict check is needed.
    """
    pairs: list[tuple[Point, Point, tuple[OwnerIdentity, ...]]] = []
    consumed: set[tuple[Point, Point]] = set()
    for edge, claims in advisory.items():
        if edge in consumed:
            continue
        p1, p2 = edge
        reverse = (p2, p1)
        reverse_claims = advisory.get(reverse)
        if reverse_claims is None or len(claims) != 1 or len(reverse_claims) != 1:
            # R2-B1: collect, do not raise.  An unpaired advisory edge resolves
            # as capability NA, but that NA must be arbitrated AFTER the
            # orthogonal/general buckets so it can never mask a real seam on the
            # same floor.  §1.3: an unpaired advisory is ALSO recorded in the
            # runtime artifact -- the paired-hit-only log of r2 was blind to
            # exactly the edges that trigger NA, which are the ones R-4's later
            # flip-to-blocking most needs to count.
            for claim in claims:
                capability = _advisory_capability(
                    claim, floor_id=floor_id
                )
                capabilities.append(capability)
                _log_advisory_hit(
                    floor_id,
                    p1,
                    p2,
                    unpaired=True,
                    capability_id=capability.capability_id,
                )
            continue
        _assert_owner_atom(
            claims[0].owner, reverse_claims[0].owner, floor_id=floor_id,
            source_edge_ids=(claims[0].edge_id, reverse_claims[0].edge_id),
        )
        _log_advisory_hit(floor_id, p1, p2)
        pairs.append((p1, p2, tuple(sorted((
            claims[0].owner, reverse_claims[0].owner
        )))))
        consumed.add(edge)
        consumed.add(reverse)
    return pairs


def _pair_interior_edges(
    directed: dict[tuple[Point, Point], list[_SourceEdgeClaim]],
    exterior_edges: tuple[tuple[Point, Point], ...],
    floor_id: str,
    *,
    identity_code: str,
) -> tuple[
    list[tuple[Point, Point, tuple[OwnerIdentity, ...]]],
    list[_PairDiagnostic],
    list[CapabilityEnvelope],
]:
    """Pair interior edges by exact coverage, T-junction aware (RW-1/2/3).

    Edges are classified on the SHARED orthogonality yardstick (W5 / R-4) so the
    judge and the production validator can never disagree about a near-axis edge:
      * axis_aligned (exact 0) -- tiled at the union of all endpoints into
        elementary sub-intervals (``_tile_orthogonal_edges``).
      * near_orthogonal_advisory (0 < min(dx,dy) <= 1e-9) -- production admitted
        it, so the judge pairs it by exact reverse only; an unpaired one is a
        shape the judge cannot measure and resolves as capability NA, NEVER as a
        topology break (``_pair_advisory_edges``).  Each hit is recorded so a
        real run can answer "did this fire" (R-4 two-stage gating; advisory-only
        now, not yet flipped to blocking).
      * non_orthogonal (both > 1e-9) -- production rejects it, so a general-seam
        failure here IS a real topology break (``_pair_general_edges``).
    All coordinates are canonicalized at the scorer entry (RW-1), so every
    comparison below is exact (``==`` / ``<=``); a gap, an overlap, or an
    imprecise endpoint is a real topology break, not a T-junction, and is rejected.
    """
    ortho: dict[tuple[Point, Point], list[_SourceEdgeClaim]] = {}
    general: dict[tuple[Point, Point], list[_SourceEdgeClaim]] = {}
    advisory: dict[tuple[Point, Point], list[_SourceEdgeClaim]] = {}
    for edge, claims in directed.items():
        p1, p2 = edge
        cls = classify_edge_orthogonality(p2[0] - p1[0], p2[1] - p1[1])
        if cls == "axis_aligned":
            ortho[edge] = claims
        elif cls == "near_orthogonal_advisory":
            advisory[edge] = claims
        else:  # non_orthogonal
            general[edge] = claims
    # R2-B1: COLLECT, then ARBITRATE (controller dead-frame, not a re-ordering).
    # The three buckets run to completion appending diagnostics into one list;
    # the arbitrator decides which diagnostic wins.  This is the only structure
    # that closes both faces of the disease:
    #   * advisory-first (r2) raised capability NA at the first unpaired
    #     advisory edge, MASKING any real seam on the same floor -> false green.
    #   * tile-first (the "obvious" swap) re-raises a DERIVATIVE
    #     exterior_duplicate_owner the advisory lean perturbed onto a neighbour
    #     span ("second face"); same disease, different code.
    # Collect-then-arbitrate lets a real topology break outrank the NA without
    # letting the derivative convict the wrong span.  See
    # ``_arbitrate_pairing_diagnostics`` for the priority rule.
    diagnostics: list[_PairDiagnostic] = []
    capabilities: list[CapabilityEnvelope] = []
    pairs = list(_pair_advisory_edges(
        advisory,
        floor_id,
        diagnostics=diagnostics,
        capabilities=capabilities,
    ))
    pairs.extend(_tile_orthogonal_edges(ortho, exterior_edges, floor_id,
                                        identity_code=identity_code, diagnostics=diagnostics))
    pairs.extend(_pair_general_edges(general, exterior_edges, floor_id,
                                     identity_code=identity_code, diagnostics=diagnostics))
    located = next(
        (
            item.as_judge_diagnostic().diagnostic_id
            for item in diagnostics
            if item.context.get("reason")
            == "exterior_interior_topology_conflict"
        ),
        None,
    )
    if located is not None:
        diagnostics = [
            replace(item, caused_by=(located,))
            if item.context.get("reason") == "invalid_interior_edge_pair"
            else item
            for item in diagnostics
        ]
    return pairs, diagnostics, capabilities


# Reasons that are ALWAYS a real topology break, independent of any advisory
# edge: an interior edge with no facing pair, or an exterior/interior conflict
# on one span.  These outrank capability NA unconditionally (R2-B1 / §1.2).
#
# Order = within-class precision (§1.2 step 3 "closest to root cause"): an
# ``exterior_interior_topology_conflict`` is a STRUCTURAL, located violation --
# a zone crossing the footprint boundary, so two zones claim an interior edge
# that lies ON the exterior.  An ``invalid_interior_edge_pair`` is the generic
# "this interior edge has no facing pair / wrong owner count", which is very
# often a DOWNSTREAM SYMPTOM of that same misplaced zone (its other edges then
# dangle).  When a zone is pushed outside the footprint BOTH fire -- the
# boundary-crossing span raises the conflict, and the zone's dangling edges
# raise invalid_interior_edge_pair -- so the located, structural reason must win
# to report the root cause, not the derivative.  This is consistent with §1.2's
# example (a real seam's invalid_interior_edge_pair outranks an advisory-
# perturbed exterior_duplicate_owner): in both cases the independent root cause
# beats the derivative symptom.  (exterior_duplicate_owner is NOT in this set --
# it is advisory-derivative and routes to NA / step 3.)
_REAL_BREAK_REASONS = ("exterior_interior_topology_conflict", "invalid_interior_edge_pair")


def _arbitrate_pairing_diagnostics(diagnostics: list[_PairDiagnostic], *, identity_code: str) -> None:
    """R2-B1 priority rule over collected pairing diagnostics (§1.2 dead-frame).

    Hard rule: a real topology/identity break may NEVER be masked by a capability
    NA -- "the judge cannot measure this" loses to "the geometry is genuinely
    broken" every time.  Concretely:

      1. ANY real-break identity diagnostic (``exterior_interior_topology_
         conflict`` / ``invalid_interior_edge_pair``) wins -> the round ends red
         on the side identity code.  Within this class the located, structural
         reason is preferred over the generic one (``exterior_interior_topology_
         conflict`` -- a zone crossing the footprint boundary -- beats
         ``invalid_interior_edge_pair`` -- a dangling edge that is usually a
         downstream symptom of that same misplaced zone) -- §1.2 step 3.
      2. Else, if a capability NA exists, the round ends NA.  This is the ONLY
         path a capability NA may take: zero real breaks.
      3. Else an ``exterior_duplicate_owner`` with NO advisory present is a real
         zone duplication over the footprint (RW-3 silent-green hole) and stays
         red.

    ``exterior_duplicate_owner`` is deliberately NOT a real-break reason: a near-
    orthogonal advisory edge's lean (sub-1e-9) perturbs the endpoint of a
    neighbour exterior span, and two cells whose shared advisory wall carries
    different lean spellings (5e-10 vs 4e-10) then overlap on that exterior span
    by ~1e-10 -- a derivative of the unpaired advisory, not an independent zone
    duplication.  Routing that derivative to NA (step 2) is what keeps the R-4
    live counter-example (production five-way GREEN, shared advisory wall)
    resolving as capability NA while a TRUE seam plus the same advisory still
    ends red (the real break in step 1 wins).  A genuine zone duplication has no
    unpaired advisory on the floor, so it falls through to step 3.
    """
    claims = tuple(
        item.as_judge_diagnostic()
        for item in diagnostics
        if item.category == "identity"
    )
    capabilities = tuple(
        CapabilityEnvelope(
            capability_id="compat:" + hashlib.sha256(
                repr(item.context).encode()
            ).hexdigest()[:20],
            kind="near_orthogonal_advisory_unpaired",
            source_edge_id=("compat",),
            source_vertex_ids=(),
            seed_coordinate_keys=(),
            dependent_fact_ids=(),
            dependency_arcs=(),
            fixed_invariants=(),
            complete=False,
            floor_id=str(item.context.get("floor_id", "")),
            reason=str(item.context.get(
                "reason", "near_orthogonal_advisory_unpaired"
            )),
        )
        for item in diagnostics
        if item.category == "capability"
    )
    certify_and_arbitrate_request(
        diagnostics=claims,
        capabilities=capabilities,
        evaluator_registry=DEFAULT_EVALUATOR_REGISTRY,
        request_key=("compat", "pairing"),
        identity_code=identity_code,
    )


def extract_gt_plan_segments(
    gt: GroundTruthV3,
    *,
    _analysis: AnalysisCollector | None = None,
) -> tuple[PlanSegment, ...]:
    """Return exterior GT segments and exact-coverage interior zone edges.

    Coordinates are mapped through a per-(side, floor, axis) identity (W1) so a
    same-intent seam shares one atomic representative before any exact
    comparison.  Interior edges pair by exact collinear coverage so a T-junction
    tiles into one segment per facing zone.  See ``_pair_interior_edges``.
    """
    output: list[PlanSegment] = []
    owns_analysis = _analysis is None
    analysis = _analysis if _analysis is not None else AnalysisCollector()
    for floor in gt.floors:
        source_document = adapt_gt_floor(floor)
        x_id, y_id = _build_floor_identity(source_document.envelope)
        rings = {
            (ring.owner_kind, ring.owner_id, ring.ring_id): ring
            for ring in source_document.rings
        }
        source_segments = {
            segment.segment_id: segment for segment in source_document.segments
        }
        boundary_seen: dict[tuple, SourceSegment] = {}
        for segment in floor.boundary_segments:
            source_segment = source_segments[str(segment.id)]
            q1, q2 = _identify_segment(source_segment, x_id, y_id)
            # C-1''(4): a boundary segment whose endpoints weld together is a
            # zero-length exterior edge; two boundary segments that weld to the
            # SAME geometry are a duplicate exterior owner.  Both are loud rejects.
            if q1 == q2:
                diameter = hypot(
                    source_segment.p1.raw_point[0] - source_segment.p2.raw_point[0],
                    source_segment.p1.raw_point[1] - source_segment.p2.raw_point[1],
                )
                raise_identity_conflict(
                    "score_identity_merge_collapse",
                    predicate="segment_identity_conflict",
                    owner_ids=(source_segment.owner_id,),
                    source_vertex_ids=(
                        source_segment.p1.vertex_id, source_segment.p2.vertex_id
                    ),
                    reason="identity_boundary_segment_collapse",
                    side="gt",
                    floor_id=floor.id,
                    segment=segment.id,
                    source_keys=(
                        source_segment.p1.x_source.audit_tuple(),
                        source_segment.p1.y_source.audit_tuple(),
                        source_segment.p2.x_source.audit_tuple(),
                        source_segment.p2.y_source.audit_tuple(),
                    ),
                    v1_hex=tuple(value.hex() for value in source_segment.p1.raw_point),
                    v2_hex=tuple(value.hex() for value in source_segment.p2.raw_point),
                    diameter=diameter,
                    diameter_hex=diameter.hex(),
                )
            geom = (floor.id, min(q1, q2), max(q1, q2))
            if geom in boundary_seen:
                prior = boundary_seen[geom]
                raise_identity_conflict(
                    "score_identity_merge_collapse",
                    predicate="segment_identity_conflict",
                    owner_ids=(prior.owner_id, source_segment.owner_id),
                    source_vertex_ids=(
                        prior.p1.vertex_id, prior.p2.vertex_id,
                        source_segment.p1.vertex_id, source_segment.p2.vertex_id,
                    ),
                    reason="identity_boundary_duplicate_after_merge",
                    side="gt",
                    floor_id=floor.id,
                    segment_ids=(prior.segment_id, source_segment.segment_id),
                    raw_endpoints=(prior.p1.raw_point, prior.p2.raw_point,
                                   source_segment.p1.raw_point, source_segment.p2.raw_point),
                    source_keys=tuple(
                        key.audit_tuple()
                        for vertex in (prior.p1, prior.p2, source_segment.p1, source_segment.p2)
                        for key in (vertex.x_source, vertex.y_source)
                    ),
                    endpoint_hex=tuple(
                        tuple(value.hex() for value in vertex.raw_point)
                        for vertex in (prior.p1, prior.p2, source_segment.p1, source_segment.p2)
                    ),
                )
            boundary_seen[geom] = source_segment
            output.append(PlanSegment(segment.id, floor.id, q1, q2, (),
                tuple(ref.view_id for ref in segment.source_refs), True))
        directed: dict[tuple[Point, Point], list[_SourceEdgeClaim]] = {}
        for zone in floor.zones:
            source_ring = rings[("zone", str(zone.id), "exterior")]
            for claim in _source_edge_claims(
                source_ring, x_id, y_id,
                identity_code="score_gt_identity_invalid",
            ):
                directed.setdefault(
                    _edge_key(claim.p1, claim.p2), []
                ).append(claim)
        exterior_edges = _edges(rings[("footprint", str(floor.id), "exterior")], x_id, y_id,
                                identity_code="score_gt_identity_invalid")
        pairs, diagnostics, capabilities = _pair_interior_edges(
            directed, exterior_edges, floor.id,
            identity_code="score_gt_identity_invalid",
        )
        analysis.extend(
            (item.as_judge_diagnostic() for item in diagnostics),
            capabilities,
        )
        for p1, p2, owners in pairs:
            zones = tuple(owner_id for _, owner_id in owners)
            key = "interior:%s:%s:%s" % (floor.id, min(p1, p2), max(p1, p2))
            output.append(PlanSegment(key, floor.id, p1, p2, zones, (), False))
    if owns_analysis:
        certify_and_arbitrate_request(
            diagnostics=analysis.diagnostics,
            capabilities=analysis.capabilities,
            evaluator_registry=DEFAULT_EVALUATOR_REGISTRY,
            request_key=("compat", "gt"),
            identity_code="score_gt_identity_invalid",
        )
    return tuple(sorted(output, key=_canonical_geometry))


def _cell_polygon(cell) -> Sequence[Sequence[float]]:
    if cell.polygon is not None:
        return cell.polygon
    # This is a legacy field representation of an actual cell, not a floor bbox.
    return ((cell.x[0], cell.y[0]), (cell.x[1], cell.y[0]), (cell.x[1], cell.y[1]), (cell.x[0], cell.y[1]))


def extract_correction_plan_segments(
    geometry: CorrectedGeometryV3,
    *,
    _analysis: AnalysisCollector | None = None,
) -> tuple[PlanSegment, ...]:
    """Extract correction footprint and cell topology without a bbox reduction.

    Coordinates are mapped through a per-(side, floor, axis) identity (W1), the
    same contract as the GT side, so a same-intent seam shares one atomic
    representative.  Interior cell edges pair through the same exact-coverage
    helper (``_pair_interior_edges``); a genuine topology break fails loudly with
    the product identity error rather than discarding observations.
    """
    output: list[PlanSegment] = []
    owns_analysis = _analysis is None
    analysis = _analysis if _analysis is not None else AnalysisCollector()
    for floor in geometry.floors:
        source_document = adapt_correction_floor(floor)
        x_id, y_id = _build_floor_identity(source_document.envelope)
        rings = {
            (ring.owner_kind, ring.owner_id, ring.ring_id): ring
            for ring in source_document.rings
        }
        exterior_edges = _edges(rings[("footprint", str(floor.id), "exterior")], x_id, y_id,
                                identity_code="score_product_identity_invalid")
        for number, (p1, p2) in enumerate(exterior_edges):
            output.append(PlanSegment("%s:footprint:%d" % (floor.id, number), floor.id, p1, p2,
                                      (), ("correction:%s" % floor.id,), True))
        directed: dict[tuple[Point, Point], list[_SourceEdgeClaim]] = {}
        for cell in floor.cells:
            owner_kind = "cell" if cell.polygon is not None else "cell_rect_v1"
            source_ring = rings[(owner_kind, str(cell.id), "exterior")]
            for claim in _source_edge_claims(
                source_ring, x_id, y_id,
                identity_code="score_product_identity_invalid",
            ):
                directed.setdefault((claim.p1, claim.p2), []).append(claim)
        pairs, diagnostics, capabilities = _pair_interior_edges(
            directed, exterior_edges, floor.id,
            identity_code="score_product_identity_invalid",
        )
        analysis.extend(
            (item.as_judge_diagnostic() for item in diagnostics),
            capabilities,
        )
        for p1, p2, owners in pairs:
            zones = tuple(owner_id for _, owner_id in owners)
            output.append(PlanSegment("%s:interior:%s:%s" % (floor.id, min(p1, p2), max(p1, p2)), floor.id,
                                      p1, p2, zones, ("correction:%s" % floor.id,), False))
    if owns_analysis:
        certify_and_arbitrate_request(
            diagnostics=analysis.diagnostics,
            capabilities=analysis.capabilities,
            evaluator_registry=DEFAULT_EVALUATOR_REGISTRY,
            request_key=("compat", "product"),
            identity_code="score_product_identity_invalid",
        )
    return tuple(sorted(output, key=_canonical_geometry))


def coerce_plan_observations(observations: Iterable[PlanSegment | dict]) -> tuple[PlanSegment, ...]:
    """Typed dispatch adapter for reading observations; no v3 branch in legacy code.

    Observations are parsed with their raw coordinates, then mapped through a
    per-(side, floor, axis) identity (W1) -- the same contract as the GT/
    correction extraction paths -- so a same-intent seam shares one identity.
    """
    raw_rows: list[PlanSegment] = []
    for raw in observations:
        if isinstance(raw, PlanSegment):
            raw_rows.append(PlanSegment(raw.key, raw.floor_id, raw.p1, raw.p2,
                                        raw.zone_ids, raw.source_ids, raw.exterior)); continue
        try:
            raw_rows.append(PlanSegment(key=str(raw["id"]), floor_id=str(raw["floor_id"]),
                p1=(float(raw["p1"][0]), float(raw["p1"][1])), p2=(float(raw["p2"][0]), float(raw["p2"][1])),
                zone_ids=tuple(str(x) for x in raw.get("zone_ids", ())),
                source_ids=tuple(str(x) for x in raw.get("source_ids", ())), exterior=bool(raw.get("exterior", True))))
        except (KeyError, TypeError, ValueError) as exc:
            raise_identity_conflict(
                "score_product_identity_invalid",
                predicate="reading_input_contract",
                reason="invalid_plan_observation",
                side="product",
                floor_id="",
                parse_error_type=type(exc).__name__,
            )
    # W1: build per-floor identities and map every observation through them.
    by_floor: dict[str, list[PlanSegment]] = {}
    for row in raw_rows:
        by_floor.setdefault(row.floor_id, []).append(row)
    output: list[PlanSegment] = []
    for floor_id, group in by_floor.items():
        source_document = adapt_reading_floor(floor_id, group)
        x_id, y_id = _build_floor_identity(source_document.envelope)
        source_segments = {
            segment.segment_id: segment for segment in source_document.segments
        }
        seen_geometry: dict[tuple[Point, Point], SourceSegment] = {}
        for row in group:
            source_segment = source_segments[row.key]
            q1, q2 = _identify_segment(source_segment, x_id, y_id)
            # C-1''(4): identity merge must not collapse a reading observation
            # to a zero-length segment ((0,0)->(5e-13,0) welds to a point).  This
            # is a loud reject, never a silent drop.
            if q1 == q2:
                diameter = hypot(
                    source_segment.p1.raw_point[0] - source_segment.p2.raw_point[0],
                    source_segment.p1.raw_point[1] - source_segment.p2.raw_point[1],
                )
                raise_identity_conflict(
                    "score_identity_merge_collapse",
                    predicate="segment_identity_conflict",
                    owner_ids=(source_segment.owner_id,),
                    source_vertex_ids=(
                        source_segment.p1.vertex_id, source_segment.p2.vertex_id
                    ),
                    reason="identity_reading_segment_collapse",
                    side="product",
                    floor_id=floor_id,
                    observation=row.key,
                    source_keys=(
                        source_segment.p1.x_source.audit_tuple(),
                        source_segment.p1.y_source.audit_tuple(),
                        source_segment.p2.x_source.audit_tuple(),
                        source_segment.p2.y_source.audit_tuple(),
                    ),
                    v1_hex=tuple(value.hex() for value in source_segment.p1.raw_point),
                    v2_hex=tuple(value.hex() for value in source_segment.p2.raw_point),
                    diameter=diameter,
                    diameter_hex=diameter.hex(),
                )
            geometry_key = min(q1, q2), max(q1, q2)
            if geometry_key in seen_geometry:
                prior = seen_geometry[geometry_key]
                raise_identity_conflict(
                    "score_identity_merge_collapse",
                    predicate="segment_identity_conflict",
                    owner_ids=(prior.owner_id, source_segment.owner_id),
                    source_vertex_ids=(
                        prior.p1.vertex_id, prior.p2.vertex_id,
                        source_segment.p1.vertex_id, source_segment.p2.vertex_id,
                    ),
                    reason="identity_reading_duplicate_after_merge",
                    side="product",
                    floor_id=floor_id,
                    segment_ids=(prior.segment_id, source_segment.segment_id),
                )
            seen_geometry[geometry_key] = source_segment
            output.append(PlanSegment(row.key, row.floor_id, q1, q2, row.zone_ids, row.source_ids, row.exterior))
    return tuple(output)


def _candidate(target: PlanSegment, observed: PlanSegment, config: JudgeScoreConfigV1) -> tuple[float, float, float] | None:
    if target.floor_id != observed.floor_id or target.length == 0 or observed.length == 0:
        return None
    dx, dy = target.p2[0] - target.p1[0], target.p2[1] - target.p1[1]
    length = target.length; tx, ty = dx / length, dy / length
    nx, ny = -ty, tx
    qdx, qdy = observed.p2[0] - observed.p1[0], observed.p2[1] - observed.p1[1]
    # This is a perpendicular endpoint displacement in metres, not a
    # dimensionless angle: a long diagonal must not evade the axis tolerance.
    axis_error = abs(qdx * nx + qdy * ny)
    if axis_error > config.plan_axis_alignment_tol_m:
        return None
    tmid = ((target.p1[0] + target.p2[0]) / 2, (target.p1[1] + target.p2[1]) / 2)
    qmid = ((observed.p1[0] + observed.p2[0]) / 2, (observed.p1[1] + observed.p2[1]) / 2)
    position = abs((qmid[0] - tmid[0]) * nx + (qmid[1] - tmid[1]) * ny)
    if position > config.plan_position_tol_m:
        return None
    t0, t1 = sorted((target.p1[0] * tx + target.p1[1] * ty, target.p2[0] * tx + target.p2[1] * ty))
    q0, q1 = sorted((observed.p1[0] * tx + observed.p1[1] * ty, observed.p2[0] * tx + observed.p2[1] * ty))
    overlap = max(0.0, min(t1, q1) - max(t0, q0))
    if overlap <= 0:
        return None
    symmetric_difference = abs(t0 - q0) + abs(t1 - q1)
    return overlap, position, symmetric_difference


def _support_line_key(segment: PlanSegment) -> tuple[str, str, float]:
    """The geometric supporting line of a segment: (floor_id, axis, const).

    axis is "V" (vertical, x = const) when the segment runs more vertically,
    "H" (horizontal, y = const) otherwise.  Coordinates are already clustered to
    atomic representatives, so two targets on the same wall share an exactly
    equal const.  A near-orthogonal edge never reaches this axis-aligned path:
    it is resolved as unsupported (R-4 / W5) before pairing.
    """
    if abs(segment.p2[0] - segment.p1[0]) <= abs(segment.p2[1] - segment.p1[1]):
        return (segment.floor_id, "V", segment.p1[0])
    return (segment.floor_id, "H", segment.p1[1])


def _assert_target_conservation(target_key: str, length: float, matched_cover: float,
                                miss_length: float, duplicate_length: float) -> None:
    """B-1 / R2-M1 #2 per-target conservation hard gate.

    The cut tiling partitions each target's [t0, t1] into matched / miss /
    duplicate sub-intervals, so matched_cover + miss_length + duplicate_length
    must equal the target length exactly in real arithmetic.  A dropped or
    inflated sub-interval -- a cut-logic bug that silently mis-accounts the
    target's length (understating failing, or inventing passing/duplicate) --
    lands here as a loud reject: the denominator may never be quietly reshaped.
    The micro-tolerance absorbs ONLY FP accumulation in the three running sums;
    it is not an over-charge slack (the obs-level gate is strict, no window).
    """
    accounted = matched_cover + miss_length + duplicate_length
    if abs(accounted - length) > _SUBINTERVAL_SUM_TOL:
        raise ScoreContractError("score_denominator_nonconserving", "scoring.denominator_totality",
            context={"reason": "target_subintervals_do_not_tile", "target": target_key,
                     "target_length": length, "accounted_length": accounted, "deficit": length - accounted})


def _assert_obs_conservation(obs_key: str, obs_length: float, covered: float) -> None:
    """B-1 / R2-M1 conservation hard gate: an observation's charged cover may
    never exceed its own length -- STRICT, no tolerance window.

    ``covered`` is a sum of disjoint sub-intervals of the observation's own
    projection onto its registered support line, so in exact arithmetic it can
    never exceed that projection, which is itself <= the obs length.  ``covered
    > obs_length`` is therefore geometrically impossible -- it is the exact
    signature of a product wall charging length on two OVERLAPPING answer spans
    on the same support line (the 4 m wall that earned 6 m / 8 m).  R2-M1 removed
    the prior ``+ tol`` window: that window (1e-9) swallowed a real 5e-10
    over-charge (sol live probe: covered=4.0000000005 on a 4.0 m wall) and the
    negative extra was then silently dropped by ``extra > epsilon``, turning a
    visible false red into a silent false green.  The gate is now strict: any
    excess, however small, raises.  ``covered == obs_length`` (a single obs
    exactly spanning its registered targets) is the geometric equality and does
    not fire.
    """
    if covered > obs_length:
        raise ScoreContractError("score_denominator_nonconserving", "scoring.denominator_totality",
            context={"reason": "observation_cover_exceeds_length", "observation": obs_key,
                         "obs_length": obs_length, "covered": covered, "excess": covered - obs_length})


def _build_target_ledger(
    *,
    target: PlanSegment,
    claims: Iterable[CoverageClaim],
) -> TargetLedger:
    """Production seam: target rows consume the returned exact ledger."""
    return build_target_ledger(
        target_key=target.key,
        domain_exact=exact_float(target.length),
        claims=claims,
    )


def _build_observation_ledger(
    *,
    observation: PlanSegment,
    claims: Iterable[CoverageClaim],
) -> ObservationLedger:
    """Production seam: extra rows and multiplicity consume this exact ledger."""
    return build_observation_ledger(
        observation_key=observation.key,
        domain_exact=exact_float(observation.length),
        claims=claims,
    )


def _canonical_score_row(row: SegmentScore) -> tuple[object, ...]:
    """Canonical audit bytes for deterministic row aggregation."""
    exact = row.eligible_units_exact
    return (
        "" if row.target is None else row.target.floor_id,
        "" if row.target is None else row.target.key,
        "" if row.observation is None else row.observation.key,
        row.status,
        f"{exact.numerator}/{exact.denominator}",
        row.eligible_units.hex(),
    )


def _plan_segment_support_registration(
    target_list: tuple[PlanSegment, ...],
    obs_list: tuple[PlanSegment, ...],
    config: JudgeScoreConfigV1,
) -> tuple[
    dict[str, tuple[str, str, float]],
    tuple[PlanSupportAmbiguity, ...],
]:
    """Register unique supports and return every per-observation ambiguity.

    The ordinary segment scorer rejects if the second return value is nonempty.
    Reading scoring uses the same registration pass to quarantine only those
    observations before measuring the remainder; this keeps the support-line
    predicate single-sourced.
    """
    obs_support: dict[str, tuple[str, str, float]] = {}
    ambiguities: list[PlanSupportAmbiguity] = []
    for obs in obs_list:
        if obs.length == 0:
            continue
        targets_by_line: dict[tuple[str, str, float], set[str]] = {}
        for target in target_list:
            if target.length == 0 or target.floor_id != obs.floor_id:
                continue
            if _candidate(target, obs, config) is None:
                continue
            targets_by_line.setdefault(_support_line_key(target), set()).add(
                target.key
            )
        if not targets_by_line:
            continue
        if len(targets_by_line) >= 2:
            ambiguities.append(
                PlanSupportAmbiguity(
                    observation_key=obs.key,
                    candidate_target_keys=tuple(
                        sorted(
                            target_key
                            for target_keys in targets_by_line.values()
                            for target_key in target_keys
                        )
                    ),
                    support_lines=tuple(sorted(targets_by_line)),
                )
            )
            continue
        obs_support[obs.key] = next(iter(targets_by_line))
    return obs_support, tuple(ambiguities)


def plan_segment_support_ambiguities(
    *,
    targets: Iterable[PlanSegment],
    observations: Iterable[PlanSegment],
    config: JudgeScoreConfigV1,
) -> tuple[PlanSupportAmbiguity, ...]:
    """Expose the scorer's own registration ambiguities for audit isolation."""
    target_list = tuple(sorted(targets, key=_canonical_geometry))
    obs_list = tuple(sorted(observations, key=_canonical_geometry))
    _registered, ambiguities = _plan_segment_support_registration(
        target_list, obs_list, config
    )
    return ambiguities


def match_plan_segments(*, targets: Iterable[PlanSegment], observations: Iterable[PlanSegment],
                        config: JudgeScoreConfigV1) -> tuple[tuple[SegmentScore, ...], dict[str, tuple[str, ...]]]:
    """Joint-cutpoint atomization with one-way support-line registration (W3/B-1).

    Coverage is a set operation with no tie, so ``score_match_ambiguous`` is
    structurally unreachable on this plan-wall path.  Three ordered steps
    (controller dead-frame, not advisory):

    1. ONE-WAY REGISTRATION (product -> answer, never reverse): each observation
       resolves the UNIQUE answer support line it belongs to.  Candidates are
       answer support lines for which ``_candidate`` succeeds (judge tolerance +
       positive projection overlap).  Exactly 1 -> registered.  0 -> the
       observation covers nothing (extra).  >= 2 -> the judge's own position
       tolerance cannot separate two parallel answer walls, so it LOUD-rejects
       (score_identity_support_ambiguous) -- never "take nearest", never "count
       both", never "first sorted": the ruler is being deformed by what it
       measures, and R-4 says say unsupported, not decide for the user.
    2. JOINT CUTPOINT per target (the existing set operation), but only the
       observations registered to that target's support line may participate --
       so one observation can never charge length on two parallel lines.
    3. CONSERVATION hard gates (R2-M1, both strict):
       (a) PER-TARGET: matched + miss + duplicate == target.length (the cut
           tiling partitions the target exactly) -- else raise.
       (b) PER-OBSERVATION: charged cover <= obs length, STRICT with NO
           tolerance window (cover > length is geometrically impossible -- the
           double-charge signature); a negative extra can therefore never again
           be swallowed by ``extra > epsilon`` (the r0 false-green shape).

    Returns (rows, observation_key -> sorted target keys it covers).
    """
    target_list = tuple(sorted(targets, key=_canonical_geometry))
    obs_list = tuple(sorted(observations, key=_canonical_geometry))
    # B-1 step 1: register each observation to its UNIQUE answer support line.
    obs_support, ambiguities = _plan_segment_support_registration(
        target_list, obs_list, config
    )
    if ambiguities:
        ambiguity = ambiguities[0]
        observation = next(
            item for item in obs_list if item.key == ambiguity.observation_key
        )
        raise_identity_conflict(
            "score_identity_support_ambiguous",
            predicate="support_registration_conflict",
            reason="observation_eligible_for_multiple_support_lines",
            observation=observation.key,
            side="product",
            floor_id=observation.floor_id,
            support_lines=list(ambiguity.support_lines),
        )
    # B exact ledgers: construct every dual-domain claim before either ledger.
    # One request-local registry evaluates a shared geometry cut once per
    # observation; target and observation domains then consume the same claim
    # and mapping certificate.
    cut_registry = CanonicalCutRegistry.empty()
    claims: list[CoverageClaim] = []
    for target in target_list:
        if target.length == 0:
            continue
        target_line = _support_line_key(target)
        length = target.length
        dx, dy = target.p2[0] - target.p1[0], target.p2[1] - target.p1[1]
        tx, ty = dx / length, dy / length
        nx, ny = -ty, tx
        for obs in obs_list:
            if obs.length == 0 or obs_support.get(obs.key) != target_line:
                continue
            metric = _candidate(target, obs, config)
            if metric is None:
                continue
            _overlap, position, _extent = metric
            qdx = obs.p2[0] - obs.p1[0]
            qdy = obs.p2[1] - obs.p1[1]
            axis_error = abs(qdx * nx + qdy * ny)
            claim = build_coverage_claim(
                target=target,
                observation=obs,
                axis_error_m=axis_error,
                position_error_m=position,
                cut_registry=cut_registry,
            )
            if claim is not None:
                claims.append(claim)

    canonical_claims = tuple(sorted(
        claims,
        key=lambda claim: claim.mapping_certificate.certificate_id,
    ))
    target_claims = claims_by_key(canonical_claims, "target_key")
    observation_claims = claims_by_key(canonical_claims, "observation_key")
    observation_by_key = {obs.key: obs for obs in obs_list}

    rows: list[SegmentScore] = []
    for target in target_list:
        if target.length == 0:
            continue
        local_claims = target_claims.get(target.key, ())
        ledger = _build_target_ledger(target=target, claims=local_claims)
        matched = dict(ledger.matched_exact)
        for claim in local_claims:
            cover_exact = matched.get(claim.observation_key, Fraction(0))
            if cover_exact <= 0:
                continue
            obs = observation_by_key[claim.observation_key]
            complete = (
                claim.axis_error_m <= config.claim_complete_epsilon_m
                and claim.position_error_m <= config.claim_complete_epsilon_m
            )
            within = (
                claim.axis_error_m <= config.plan_axis_alignment_tol_m
                and claim.position_error_m <= config.plan_position_tol_m
            )
            status = (
                "complete"
                if complete
                else ("within_tolerance" if within else "miss")
            )
            cover = float(cover_exact)
            rows.append(SegmentScore(
                target,
                obs,
                status,
                claim.axis_error_m,
                claim.position_error_m,
                float(ledger.domain_exact - cover_exact),
                eligible_units=cover,
                eligible_units_exact=cover_exact,
            ))
        if ledger.miss_exact > 0:
            rows.append(SegmentScore(
                target,
                None,
                "miss",
                None,
                None,
                None,
                eligible_units=float(ledger.miss_exact),
                eligible_units_exact=ledger.miss_exact,
            ))
        if ledger.duplicate_exact > 0:
            rows.append(SegmentScore(
                target,
                None,
                "duplicate",
                None,
                None,
                None,
                eligible_units=float(ledger.duplicate_exact),
                eligible_units_exact=ledger.duplicate_exact,
            ))

    for obs in obs_list:
        if obs.length == 0:
            continue
        ledger = _build_observation_ledger(
            observation=obs,
            claims=observation_claims.get(obs.key, ()),
        )
        # ``extra_exact`` is a complement atom measure and can never be
        # negative.  The existing epsilon is only the public row-emission
        # policy; it has no role in conservation or multiplicity.
        if float(ledger.extra_exact) > config.claim_complete_epsilon_m:
            rows.append(SegmentScore(
                None,
                obs,
                "extra",
                None,
                None,
                None,
                eligible_units=float(ledger.extra_exact),
                eligible_units_exact=ledger.extra_exact,
            ))

    observation_map = {
        observation_key: tuple(sorted(
            claim.target_key for claim in local_claims
        ))
        for observation_key, local_claims in observation_claims.items()
    }

    return tuple(sorted(rows, key=_canonical_score_row)), observation_map


def assign_plan_segments(*, targets: Iterable[PlanSegment], observations: Iterable[PlanSegment],
                         config: JudgeScoreConfigV1) -> SegmentAssignment:
    """Compatibility view of the joint-cutpoint match (W3).

    Returns matched pairs plus fully-unmatched targets/observations.  A target
    partially covered appears in ``matched`` (its covered pair), not in
    ``unmatched_targets``; only a target with no covering observation is
    unmatched.  ``score_match_ambiguous`` is structurally unreachable here.
    """
    target_list = tuple(sorted(targets, key=_canonical_geometry))
    obs_list = tuple(sorted(observations, key=_canonical_geometry))
    rows, _ = match_plan_segments(targets=target_list, observations=obs_list, config=config)
    matched = tuple(sorted(((row.target, row.observation) for row in rows
                            if row.target is not None and row.observation is not None),
                           key=lambda pair: _canonical_geometry(pair[0])))
    matched_target_keys = {row.target.key for row in rows if row.target is not None and row.observation is not None}
    matched_obs_keys = {row.observation.key for row in rows if row.target is not None and row.observation is not None}
    unmatched_targets = tuple(sorted((target for target in target_list if target.key not in matched_target_keys),
                                     key=_canonical_geometry))
    unmatched_observations = tuple(sorted((obs for obs in obs_list if obs.key not in matched_obs_keys),
                                          key=_canonical_geometry))
    return SegmentAssignment(matched, unmatched_targets, unmatched_observations)


def score_plan_segments(*, targets: Iterable[PlanSegment], observations: Iterable[PlanSegment],
                        config: JudgeScoreConfigV1) -> tuple[SegmentScore, ...]:
    """Materialize complete/within/miss/extra/duplicate rows via joint cutpoints (W3)."""
    rows, _ = match_plan_segments(targets=targets, observations=observations, config=config)
    return rows
