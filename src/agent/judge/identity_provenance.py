"""Source-preserving identity inputs and structural alias certificates.

This module deliberately knows nothing about coordinate merge/split thresholds.
Adapters describe wire slots and topology; ``certify_alias`` can therefore
answer only whether two slots are structurally the same junction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, Sequence

from src.agent.judge.score_schema import SEGMENT_SCORER_HELPER_VERSION

IDENTITY_CONTRACT_VERSION = "1"
SEGMENT_SCORER_IDENTITY_RELEASE_MAP: Mapping[str, str] = MappingProxyType({
    SEGMENT_SCORER_HELPER_VERSION: IDENTITY_CONTRACT_VERSION,
})


def identity_contract_for_segment_scorer(helper_version: str) -> str:
    """Return the exact identity contract released with a scorer helper."""
    try:
        return SEGMENT_SCORER_IDENTITY_RELEASE_MAP[helper_version]
    except KeyError as exc:
        raise ValueError(f"unknown segment scorer helper: {helper_version!r}") from exc

# Controller-ratified flat fields.  Every certified identity conflict is built
# through ``identity_error_context`` and is checked complete before raising.
IDENTITY_ERROR_CONTEXT_KEYS = frozenset({
    "authority",
    "proof_status",
    "predicate",
    "predicate_schema_version",
    "owner_ids",
    "source_edge_ids",
    "source_vertex_ids",
    "depends_on_capability_ids",
})

Axis = Literal["x", "y"]
OwnerIdentity = tuple[str, str]


@dataclass(frozen=True, order=True)
class CoordinateSourceKey:
    side: str
    floor_id: str
    owner_kind: str
    owner_id: str
    ring_id: str | None
    element_index: int | None
    endpoint_side: str | None
    axis: Axis

    def audit_tuple(self) -> tuple[object, ...]:
        return (
            self.side,
            self.floor_id,
            self.owner_kind,
            self.owner_id,
            self.ring_id,
            self.element_index,
            self.endpoint_side,
            self.axis,
        )


@dataclass(frozen=True)
class CoordinateOccurrence:
    source_key: CoordinateSourceKey
    value: float
    value_hex: str
    use_site: str

    @classmethod
    def make(
        cls, source_key: CoordinateSourceKey, value: float, use_site: str
    ) -> "CoordinateOccurrence":
        binary64 = float(value)
        return cls(source_key, binary64, binary64.hex(), use_site)


@dataclass(frozen=True)
class SourceVertex:
    vertex_id: tuple[object, ...]
    x_source: CoordinateSourceKey
    y_source: CoordinateSourceKey
    raw_point: tuple[float, float]


@dataclass(frozen=True)
class SourceRing:
    owner_kind: str
    owner_id: str
    ring_id: str
    vertices: tuple[SourceVertex, ...]
    explicit_closure: bool = False

    @property
    def owner(self) -> OwnerIdentity:
        return self.owner_kind, self.owner_id


@dataclass(frozen=True)
class SourceSegment:
    segment_id: str
    owner_kind: str
    owner_id: str
    p1: SourceVertex
    p2: SourceVertex

    @property
    def owner(self) -> OwnerIdentity:
        return self.owner_kind, self.owner_id


@dataclass(frozen=True)
class RingHalfEdgeRef:
    side: str
    floor_id: str
    owner_kind: str
    owner_id: str
    ring_id: str
    edge_index: int
    start_vertex_index: int
    end_vertex_index: int
    direction_axis: Axis | None
    direction_sign: int

    @property
    def edge_id(self) -> tuple[object, ...]:
        return (
            self.side,
            self.floor_id,
            self.owner_kind,
            self.owner_id,
            self.ring_id,
            self.edge_index,
        )


@dataclass(frozen=True)
class BoundaryEndpointRef:
    side: str
    floor_id: str
    boundary_loop_id: str
    source_footprint_fingerprint: str
    facade_family: str
    outward_normal: tuple[int, int]
    segment_id: str
    endpoint_side: Literal["p1", "p2"]
    interval_role: Literal["lo", "hi"]
    chain_rank: int


@dataclass(frozen=True)
class AliasCertificate:
    kind: Literal[
        "explicit_ring_closure",
        "paired_edge_endpoint",
        "boundary_chain_endpoint",
        "profile_axis_constraint",
    ]
    left: CoordinateSourceKey
    right: CoordinateSourceKey
    support_edge_ids: tuple[tuple[object, ...], ...] = ()
    support_owner_ids: tuple[OwnerIdentity, ...] = ()
    support_slots: tuple[tuple[object, ...], ...] = ()
    depends_on: tuple[str, ...] = ()

    @property
    def certificate_id(self) -> str:
        ends = sorted((self.left.audit_tuple(), self.right.audit_tuple()))
        return repr((self.kind, ends, self.support_edge_ids, self.support_slots))


def _pair_key(
    left: CoordinateSourceKey, right: CoordinateSourceKey
) -> tuple[CoordinateSourceKey, CoordinateSourceKey]:
    return (left, right) if left <= right else (right, left)


@dataclass(frozen=True)
class SourceTopologyIndex:
    side: str
    floor_id: str
    half_edges: tuple[RingHalfEdgeRef, ...] = ()
    boundary_endpoints: tuple[BoundaryEndpointRef, ...] = ()
    certificates: Mapping[
        tuple[CoordinateSourceKey, CoordinateSourceKey], AliasCertificate
    ] = field(default_factory=dict)

    @classmethod
    def empty(cls, *, side: str, floor_id: str) -> "SourceTopologyIndex":
        return cls(side=side, floor_id=floor_id)


@dataclass(frozen=True)
class IdentityInputEnvelope:
    contract_version: object
    source_schema: str
    side: str
    floor_id: str
    occurrences: tuple[CoordinateOccurrence, ...]
    topology: SourceTopologyIndex


@dataclass(frozen=True)
class SourceGeometryDocument:
    envelope: IdentityInputEnvelope
    rings: tuple[SourceRing, ...] = ()
    segments: tuple[SourceSegment, ...] = ()


def identity_error_context(
    *,
    predicate: str,
    owner_ids: Iterable[object] = (),
    source_edge_ids: Iterable[object] = (),
    source_vertex_ids: Iterable[object] = (),
    depends_on_capability_ids: Iterable[str] = (),
    **extra: object,
) -> dict[str, object]:
    context: dict[str, object] = {
        "authority": "scoring_identity",
        "proof_status": "CERTIFIED_CONFLICT",
        "predicate": predicate,
        "predicate_schema_version": "1",
        "owner_ids": tuple(owner_ids),
        "source_edge_ids": tuple(source_edge_ids),
        "source_vertex_ids": tuple(source_vertex_ids),
        "depends_on_capability_ids": tuple(depends_on_capability_ids),
    }
    context.update(extra)
    missing = IDENTITY_ERROR_CONTEXT_KEYS.difference(context)
    if missing:
        raise AssertionError(f"incomplete identity error context: {sorted(missing)}")
    return context


def raise_identity_conflict(
    code: str,
    *,
    predicate: str,
    owner_ids: Iterable[object] = (),
    source_edge_ids: Iterable[object] = (),
    source_vertex_ids: Iterable[object] = (),
    depends_on_capability_ids: Iterable[str] = (),
    **extra: object,
) -> None:
    exact_error_context = bool(extra.pop("_exact_error_context", False))
    context = identity_error_context(
        predicate=predicate,
        owner_ids=owner_ids,
        source_edge_ids=source_edge_ids,
        source_vertex_ids=source_vertex_ids,
        depends_on_capability_ids=depends_on_capability_ids,
        **extra,
    )
    assert IDENTITY_ERROR_CONTEXT_KEYS.issubset(context)
    # Lazy import keeps the source model independent of the certifier module
    # while ensuring even pure C-0..C-3 contract facts take the one severity
    # route.  These facts are pre-certified because their truth is read directly
    # from the typed input contract and does not depend on a capability domain.
    from src.agent.judge.certifier import (
        ConflictWitness,
        JudgeDiagnostic,
        _with_exact_error_context,
        canonical_diagnostic_id,
        report_identity_diagnostic,
    )

    witness = ConflictWitness(
        predicate=predicate,
        predicate_schema_version="1",
        source_edge_ids=tuple(
            edge if isinstance(edge, tuple) else (edge,)
            for edge in context["source_edge_ids"]
        ),
        source_vertex_ids=tuple(
            vertex if isinstance(vertex, tuple) else (vertex,)
            for vertex in context["source_vertex_ids"]
        ),
        owner_ids=tuple(context["owner_ids"]),
    )
    side = str(extra.get("side", ""))
    floor_id = str(extra.get("floor_id", ""))
    reason = str(extra.get("reason", predicate))
    diagnostic = JudgeDiagnostic(
        diagnostic_id=canonical_diagnostic_id(
            side=side,
            floor_id=floor_id,
            reason=reason,
            witness=witness,
        ),
        requested_code=code,
        gate_id="scoring.input_identity",
        reason=reason,
        floor_id=floor_id,
        witness=witness,
        side=side,
        context=context,
        precertified=True,
    )
    if exact_error_context:
        diagnostic = _with_exact_error_context(diagnostic)
    report_identity_diagnostic(diagnostic)


def certify_alias(
    left: CoordinateSourceKey,
    right: CoordinateSourceKey,
    axis: Axis,
    topology: SourceTopologyIndex,
) -> AliasCertificate | None:
    """Return a wire/topology certificate without seeing coordinate values."""
    if left.axis != axis or right.axis != axis:
        return None
    return topology.certificates.get(_pair_key(left, right))


def _source_key(
    *,
    side: str,
    floor_id: str,
    owner_kind: str,
    owner_id: str,
    ring_id: str | None,
    element_index: int | None,
    endpoint_side: str | None,
    axis: Axis,
) -> CoordinateSourceKey:
    return CoordinateSourceKey(
        side,
        floor_id,
        owner_kind,
        owner_id,
        ring_id,
        element_index,
        endpoint_side,
        axis,
    )


def _vertex(
    *,
    side: str,
    floor_id: str,
    owner_kind: str,
    owner_id: str,
    ring_id: str | None,
    element_index: int | None,
    endpoint_side: str | None,
    point: Sequence[float],
) -> SourceVertex:
    raw = float(point[0]), float(point[1])
    common = dict(
        side=side,
        floor_id=floor_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        ring_id=ring_id,
        element_index=element_index,
        endpoint_side=endpoint_side,
    )
    x_source = _source_key(**common, axis="x")
    y_source = _source_key(**common, axis="y")
    return SourceVertex(
        vertex_id=(
            side,
            floor_id,
            owner_kind,
            owner_id,
            ring_id,
            endpoint_side,
            element_index,
        ),
        x_source=x_source,
        y_source=y_source,
        raw_point=raw,
    )


def _rectangle_ring(*, side: str, floor_id: str, cell: object) -> SourceRing:
    """Legacy rectangle corners reuse the four actual x/y wire slots."""
    owner_id = str(cell.id)
    x_values = tuple(float(value) for value in cell.x)
    y_values = tuple(float(value) for value in cell.y)
    slots = ((0, 0), (1, 0), (1, 1), (0, 1))
    vertices: list[SourceVertex] = []
    for vertex_index, (x_slot, y_slot) in enumerate(slots):
        x_key = _source_key(
            side=side, floor_id=floor_id, owner_kind="cell_rect_v1",
            owner_id=owner_id, ring_id="x", element_index=x_slot,
            endpoint_side=None, axis="x",
        )
        y_key = _source_key(
            side=side, floor_id=floor_id, owner_kind="cell_rect_v1",
            owner_id=owner_id, ring_id="y", element_index=y_slot,
            endpoint_side=None, axis="y",
        )
        vertices.append(SourceVertex(
            vertex_id=(side, floor_id, "cell_rect_v1", owner_id, "exterior", None, vertex_index),
            x_source=x_key,
            y_source=y_key,
            raw_point=(x_values[x_slot], y_values[y_slot]),
        ))
    return SourceRing("cell_rect_v1", owner_id, "exterior", tuple(vertices))


def _ring(
    *,
    side: str,
    floor_id: str,
    owner_kind: str,
    owner_id: str,
    ring_id: str,
    vertices: Sequence[Sequence[float]],
) -> SourceRing:
    materialized = tuple(vertices)
    explicit = len(materialized) > 1 and tuple(materialized[0]) == tuple(materialized[-1])
    return SourceRing(
        owner_kind,
        owner_id,
        ring_id,
        tuple(
            _vertex(
                side=side,
                floor_id=floor_id,
                owner_kind=owner_kind,
                owner_id=owner_id,
                ring_id=ring_id,
                element_index=index,
                endpoint_side=None,
                point=point,
            )
            for index, point in enumerate(materialized)
        ),
        explicit,
    )


def _occurrences(
    rings: Iterable[SourceRing], segments: Iterable[SourceSegment]
) -> tuple[CoordinateOccurrence, ...]:
    output: list[CoordinateOccurrence] = []
    for ring in rings:
        for vertex in ring.vertices:
            output.extend((
                CoordinateOccurrence.make(vertex.x_source, vertex.raw_point[0], repr(vertex.vertex_id)),
                CoordinateOccurrence.make(vertex.y_source, vertex.raw_point[1], repr(vertex.vertex_id)),
            ))
    for segment in segments:
        for vertex in (segment.p1, segment.p2):
            output.extend((
                CoordinateOccurrence.make(vertex.x_source, vertex.raw_point[0], repr(vertex.vertex_id)),
                CoordinateOccurrence.make(vertex.y_source, vertex.raw_point[1], repr(vertex.vertex_id)),
            ))
    return tuple(output)


def _direction(edge_start: SourceVertex, edge_end: SourceVertex) -> tuple[Axis | None, int]:
    """Classify one edge only; never compare constants of different owners."""
    x1, y1 = edge_start.raw_point
    x2, y2 = edge_end.raw_point
    if x1 == x2 and y1 != y2:
        return "y", 1 if y2 > y1 else -1
    if y1 == y2 and x1 != x2:
        return "x", 1 if x2 > x1 else -1
    return None, 0


def _add_certificate(
    certificates: dict[tuple[CoordinateSourceKey, CoordinateSourceKey], AliasCertificate],
    certificate: AliasCertificate,
) -> None:
    key = _pair_key(certificate.left, certificate.right)
    prior = certificates.get(key)
    if prior is None or certificate.certificate_id < prior.certificate_id:
        certificates[key] = certificate


def _build_topology(
    *,
    side: str,
    floor_id: str,
    rings: tuple[SourceRing, ...],
    boundary_specs: tuple[tuple[SourceSegment, object], ...] = (),
) -> SourceTopologyIndex:
    certificates: dict[
        tuple[CoordinateSourceKey, CoordinateSourceKey], AliasCertificate
    ] = {}
    half_edges: list[RingHalfEdgeRef] = []
    edge_rows: list[tuple[SourceRing, int, SourceVertex, SourceVertex, Axis | None, int]] = []

    for ring in rings:
        usable = ring.vertices[:-1] if ring.explicit_closure else ring.vertices
        count = len(usable)
        for index in range(count):
            start, end = usable[index], usable[(index + 1) % count]
            direction_axis, sign = _direction(start, end)
            ref = RingHalfEdgeRef(
                side, floor_id, ring.owner_kind, ring.owner_id, ring.ring_id,
                index, index, (index + 1) % count, direction_axis, sign,
            )
            half_edges.append(ref)
            edge_rows.append((ring, index, start, end, direction_axis, sign))

            # Profile constraint: the other coordinate is constant because this
            # edge has a non-zero exact direction on its own direction axis.
            if direction_axis is not None:
                constrained: Axis = "y" if direction_axis == "x" else "x"
                left = start.x_source if constrained == "x" else start.y_source
                right = end.x_source if constrained == "x" else end.y_source
                _add_certificate(certificates, AliasCertificate(
                    "profile_axis_constraint", left, right,
                    support_edge_ids=(ref.edge_id,),
                    support_owner_ids=(ring.owner,),
                    support_slots=(("c2_simple_orthogonal_no_holes", "1", constrained),),
                ))

        if ring.explicit_closure:
            tail, head = ring.vertices[-1], ring.vertices[0]
            for axis in ("x", "y"):
                left = tail.x_source if axis == "x" else tail.y_source
                right = head.x_source if axis == "x" else head.y_source
                _add_certificate(certificates, AliasCertificate(
                    "explicit_ring_closure", left, right,
                    support_owner_ids=(ring.owner,),
                    support_slots=((ring.ring_id, len(ring.vertices) - 1, 0),),
                ))

    # Paired constant edges are found by exact spans on the independently read
    # direction axis.  No constant-axis value, gap, threshold, or nearest-owner
    # comparison participates.
    for left_index, left_row in enumerate(edge_rows):
        left_ring, li, ls, le, direction_axis, left_sign = left_row
        if direction_axis is None:
            continue
        left_span = sorted((
            ls.raw_point[0 if direction_axis == "x" else 1],
            le.raw_point[0 if direction_axis == "x" else 1],
        ))
        for right_row in edge_rows[left_index + 1:]:
            right_ring, ri, rs, re, right_axis, right_sign = right_row
            if (
                right_axis != direction_axis
                or right_sign != -left_sign
                or right_ring.owner == left_ring.owner
            ):
                continue
            right_span = sorted((
                rs.raw_point[0 if direction_axis == "x" else 1],
                re.raw_point[0 if direction_axis == "x" else 1],
            ))
            lo, hi = max(left_span[0], right_span[0]), min(left_span[1], right_span[1])
            if not lo < hi:
                continue
            constrained: Axis = "y" if direction_axis == "x" else "x"
            left_keys = (
                ls.x_source if constrained == "x" else ls.y_source,
                le.x_source if constrained == "x" else le.y_source,
            )
            right_keys = (
                rs.x_source if constrained == "x" else rs.y_source,
                re.x_source if constrained == "x" else re.y_source,
            )
            left_ref = half_edges[
                next(i for i, ref in enumerate(half_edges)
                     if ref.owner_kind == left_ring.owner_kind
                     and ref.owner_id == left_ring.owner_id
                     and ref.ring_id == left_ring.ring_id and ref.edge_index == li)
            ]
            right_ref = half_edges[
                next(i for i, ref in enumerate(half_edges)
                     if ref.owner_kind == right_ring.owner_kind
                     and ref.owner_id == right_ring.owner_id
                     and ref.ring_id == right_ring.ring_id and ref.edge_index == ri)
            ]
            for left_key in left_keys:
                for right_key in right_keys:
                    _add_certificate(certificates, AliasCertificate(
                        "paired_edge_endpoint", left_key, right_key,
                        support_edge_ids=(left_ref.edge_id, right_ref.edge_id),
                        support_owner_ids=(left_ring.owner, right_ring.owner),
                        support_slots=((direction_axis, lo, hi),),
                    ))

    boundary_refs: list[BoundaryEndpointRef] = []
    by_chain: dict[tuple[str, str, str, tuple[int, int]], list[tuple[object, SourceSegment]]] = {}
    for segment, spec in boundary_specs:
        loop_id = str(spec.boundary_loop_id)
        fingerprint = str(spec.source_footprint_fingerprint)
        family = str(spec.facade_family)
        normal = tuple(int(v) for v in spec.outward_normal)
        interval = spec.world_along_interval
        p1_axis_value = segment.p1.raw_point[0] if segment.p1.raw_point[1] == segment.p2.raw_point[1] else segment.p1.raw_point[1]
        p2_axis_value = segment.p2.raw_point[0] if segment.p1.raw_point[1] == segment.p2.raw_point[1] else segment.p2.raw_point[1]
        roles = {
            "p1": "lo" if p1_axis_value == float(interval.lo) else "hi",
            "p2": "lo" if p2_axis_value == float(interval.lo) else "hi",
        }
        by_chain.setdefault((loop_id, fingerprint, family, normal), []).append((spec, segment))
        for endpoint_side in ("p1", "p2"):
            boundary_refs.append(BoundaryEndpointRef(
                side, floor_id, loop_id, fingerprint, family, normal,
                segment.segment_id, endpoint_side, roles[endpoint_side], -1,
            ))

    for chain, rows in by_chain.items():
        ordered = sorted(rows, key=lambda row: (
            float(row[0].world_along_interval.lo),
            float(row[0].world_along_interval.hi),
            row[1].segment_id,
        ))
        for rank, ((left_spec, left), (right_spec, right)) in enumerate(zip(ordered, ordered[1:])):
            if (
                float(left_spec.world_along_interval.hi)
                > float(right_spec.world_along_interval.lo)
            ):
                continue
            left_vertex = left.p1 if float(left_spec.world_along_interval.hi) == (
                left.p1.raw_point[0] if left.p1.raw_point[1] == left.p2.raw_point[1] else left.p1.raw_point[1]
            ) else left.p2
            right_vertex = right.p1 if float(right_spec.world_along_interval.lo) == (
                right.p1.raw_point[0] if right.p1.raw_point[1] == right.p2.raw_point[1] else right.p1.raw_point[1]
            ) else right.p2
            for axis in ("x", "y"):
                left_key = left_vertex.x_source if axis == "x" else left_vertex.y_source
                right_key = right_vertex.x_source if axis == "x" else right_vertex.y_source
                _add_certificate(certificates, AliasCertificate(
                    "boundary_chain_endpoint", left_key, right_key,
                    support_owner_ids=(left.owner, right.owner),
                    support_slots=((chain, rank, "hi", "lo"),),
                ))

    # A declared exterior boundary segment may share a junction with the unique
    # footprint half-edge selected by facade family and ring order.  Candidate
    # constant values are never compared: the facade/loop selects the edge, and
    # the segment's own exact interval roles select endpoints on the independent
    # direction axis.
    facade_direction = {
        "South": ("x", 1),
        "East": ("y", 1),
        "North": ("x", -1),
        "West": ("y", -1),
    }
    footprint_rows = [
        row for row in edge_rows
        if row[0].owner_kind == "footprint" and row[0].ring_id == "exterior"
    ]
    for segment, spec in boundary_specs:
        desired = facade_direction.get(str(spec.facade_family))
        matches = [
            row for row in footprint_rows
            if (row[4], row[5]) == desired
        ]
        if str(spec.boundary_loop_id) != "exterior" or len(matches) != 1:
            continue
        ring, edge_index, start, end, direction_axis, sign = matches[0]
        assert direction_axis is not None
        direction_slot = 0 if direction_axis == "x" else 1
        ring_lo, ring_hi = sorted((
            start.raw_point[direction_slot], end.raw_point[direction_slot]
        ))
        interval = spec.world_along_interval
        if (
            float(interval.lo) != ring_lo
            or float(interval.hi) != ring_hi
        ):
            continue
        constrained: Axis = "y" if direction_axis == "x" else "x"
        boundary_vertices = (segment.p1, segment.p2)
        ring_vertices = (start, end)
        for boundary_vertex in boundary_vertices:
            boundary_key = (
                boundary_vertex.x_source if constrained == "x"
                else boundary_vertex.y_source
            )
            for ring_vertex in ring_vertices:
                ring_key = (
                    ring_vertex.x_source if constrained == "x"
                    else ring_vertex.y_source
                )
                _add_certificate(certificates, AliasCertificate(
                    "boundary_chain_endpoint", boundary_key, ring_key,
                    support_edge_ids=((
                        side, floor_id, ring.owner_kind, ring.owner_id,
                        ring.ring_id, edge_index,
                    ),),
                    support_owner_ids=(segment.owner, ring.owner),
                    support_slots=((
                        "exterior", str(spec.facade_family), constrained,
                    ),),
                ))
        for role, boundary_vertex in (
            ("lo", next(
                vertex for vertex in boundary_vertices
                if vertex.raw_point[direction_slot] == float(interval.lo)
            )),
            ("hi", next(
                vertex for vertex in boundary_vertices
                if vertex.raw_point[direction_slot] == float(interval.hi)
            )),
        ):
            ring_vertex = (
                start if (role == "lo") == (sign > 0) else end
            )
            boundary_key = (
                boundary_vertex.x_source if direction_axis == "x"
                else boundary_vertex.y_source
            )
            ring_key = (
                ring_vertex.x_source if direction_axis == "x"
                else ring_vertex.y_source
            )
            _add_certificate(certificates, AliasCertificate(
                "boundary_chain_endpoint", boundary_key, ring_key,
                support_edge_ids=((
                    side, floor_id, ring.owner_kind, ring.owner_id,
                    ring.ring_id, edge_index,
                ),),
                support_owner_ids=(segment.owner, ring.owner),
                support_slots=((
                    "exterior", str(spec.facade_family), role,
                ),),
            ))

    return SourceTopologyIndex(
        side=side,
        floor_id=floor_id,
        half_edges=tuple(half_edges),
        boundary_endpoints=tuple(boundary_refs),
        certificates=certificates,
    )


def _document(
    *,
    source_schema: str,
    side: str,
    floor_id: str,
    rings: tuple[SourceRing, ...],
    segments: tuple[SourceSegment, ...],
    boundary_specs: tuple[tuple[SourceSegment, object], ...] = (),
) -> SourceGeometryDocument:
    owners = [ring.owner for ring in rings]
    owners.extend(segment.owner for segment in segments)
    duplicate_owners = sorted({owner for owner in owners if owners.count(owner) > 1})
    # Multiple boundary segments intentionally share owner_kind but have unique
    # segment ids; rings have exactly one owner record each.
    ring_owners = [ring.owner for ring in rings]
    if len(ring_owners) != len(set(ring_owners)):
        raise_identity_conflict(
            "score_identity_contract_mismatch",
            predicate="source_owner_collision",
            owner_ids=duplicate_owners,
            reason="duplicate_owner_identity",
            side=side,
            floor_id=floor_id,
            contract_version=IDENTITY_CONTRACT_VERSION,
        )
    topology = _build_topology(
        side=side, floor_id=floor_id, rings=rings, boundary_specs=boundary_specs
    )
    occurrences = _occurrences(rings, segments)
    envelope = IdentityInputEnvelope(
        contract_version=IDENTITY_CONTRACT_VERSION,
        source_schema=source_schema,
        side=side,
        floor_id=floor_id,
        occurrences=occurrences,
        topology=topology,
    )
    return SourceGeometryDocument(envelope, rings, segments)


def adapt_gt_floor(floor: object) -> SourceGeometryDocument:
    floor_id = str(floor.id)
    rings: list[SourceRing] = [
        _ring(
            side="gt", floor_id=floor_id, owner_kind="footprint",
            owner_id=floor_id, ring_id="exterior",
            vertices=floor.footprint.exterior.vertices,
        )
    ]
    for ring_index, interior in enumerate(getattr(floor.footprint, "interior_rings", ())):
        rings.append(_ring(
            side="gt", floor_id=floor_id, owner_kind="footprint",
            owner_id=floor_id, ring_id=f"interior:{ring_index}",
            vertices=interior.vertices,
        ))
    for zone in floor.zones:
        rings.append(_ring(
            side="gt", floor_id=floor_id, owner_kind="zone",
            owner_id=str(zone.id), ring_id="exterior",
            vertices=zone.polygon.exterior.vertices,
        ))
        for ring_index, interior in enumerate(getattr(zone.polygon, "interior_rings", ())):
            rings.append(_ring(
                side="gt", floor_id=floor_id, owner_kind="zone",
                owner_id=str(zone.id), ring_id=f"interior:{ring_index}",
                vertices=interior.vertices,
            ))
    segments: list[SourceSegment] = []
    boundary_specs: list[tuple[SourceSegment, object]] = []
    for raw in floor.boundary_segments:
        segment_id = str(raw.id)
        p1 = _vertex(
            side="gt", floor_id=floor_id, owner_kind="boundary",
            owner_id=segment_id, ring_id=None, element_index=None,
            endpoint_side="p1", point=raw.p1,
        )
        p2 = _vertex(
            side="gt", floor_id=floor_id, owner_kind="boundary",
            owner_id=segment_id, ring_id=None, element_index=None,
            endpoint_side="p2", point=raw.p2,
        )
        segment = SourceSegment(segment_id, "boundary", segment_id, p1, p2)
        segments.append(segment)
        if all(hasattr(raw, name) for name in (
            "boundary_loop_id", "source_footprint_fingerprint", "facade_family",
            "outward_normal", "world_along_interval",
        )):
            boundary_specs.append((segment, raw))
    return _document(
        source_schema="gt_v3", side="gt", floor_id=floor_id,
        rings=tuple(rings), segments=tuple(segments),
        boundary_specs=tuple(boundary_specs),
    )


def adapt_correction_floor(floor: object) -> SourceGeometryDocument:
    floor_id = str(floor.id)
    rings: list[SourceRing] = [
        _ring(
            side="product", floor_id=floor_id, owner_kind="footprint",
            owner_id=floor_id, ring_id="exterior", vertices=floor.footprint.vertices,
        )
    ]
    for cell in floor.cells:
        if cell.polygon is None:
            rings.append(_rectangle_ring(side="product", floor_id=floor_id, cell=cell))
        else:
            rings.append(_ring(
                side="product", floor_id=floor_id, owner_kind="cell",
                owner_id=str(cell.id), ring_id="exterior", vertices=cell.polygon,
            ))
    return _document(
        source_schema="correction_v3", side="product", floor_id=floor_id,
        rings=tuple(rings), segments=(),
    )


def adapt_reading_floor(
    floor_id: str, rows: Sequence[object]
) -> SourceGeometryDocument:
    ids = [str(row.key) for row in rows]
    if len(ids) != len(set(ids)):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        raise_identity_conflict(
            "score_identity_contract_mismatch",
            predicate="source_owner_collision",
            owner_ids=(("reading", value) for value in duplicates),
            reason="duplicate_reading_id",
            side="product",
            floor_id=floor_id,
            contract_version=IDENTITY_CONTRACT_VERSION,
        )
    segments: list[SourceSegment] = []
    for row in rows:
        owner_id = str(row.key)
        p1 = _vertex(
            side="product", floor_id=floor_id, owner_kind="reading",
            owner_id=owner_id, ring_id=None, element_index=None,
            endpoint_side="p1", point=row.p1,
        )
        p2 = _vertex(
            side="product", floor_id=floor_id, owner_kind="reading",
            owner_id=owner_id, ring_id=None, element_index=None,
            endpoint_side="p2", point=row.p2,
        )
        segments.append(SourceSegment(owner_id, "reading", owner_id, p1, p2))
    return _document(
        source_schema="reading_plan_v1", side="product", floor_id=floor_id,
        rings=(), segments=tuple(segments),
    )


def validate_occurrence_shape(envelope: IdentityInputEnvelope) -> None:
    if (
        type(envelope.contract_version) is not str
        or envelope.contract_version != IDENTITY_CONTRACT_VERSION
    ):
        raise_identity_conflict(
            "score_identity_contract_mismatch",
            predicate="identity_contract_version",
            reason="identity_contract_version_mismatch",
            side=envelope.side,
            floor_id=envelope.floor_id,
            contract_version=IDENTITY_CONTRACT_VERSION,
            expected_contract_version=IDENTITY_CONTRACT_VERSION,
            observed_contract_version=envelope.contract_version,
        )
    if (
        envelope.topology.side != envelope.side
        or envelope.topology.floor_id != envelope.floor_id
    ):
        raise_identity_conflict(
            "score_identity_contract_mismatch",
            predicate="source_topology_scope",
            reason="identity_topology_scope_mismatch",
            side=envelope.side,
            floor_id=envelope.floor_id,
            contract_version=IDENTITY_CONTRACT_VERSION,
        )
    for occurrence in envelope.occurrences:
        if (
            occurrence.source_key.side != envelope.side
            or occurrence.source_key.floor_id != envelope.floor_id
        ):
            raise_identity_conflict(
                "score_identity_contract_mismatch",
                predicate="source_key_scope",
                source_vertex_ids=(occurrence.source_key.audit_tuple(),),
                reason="identity_source_key_scope_mismatch",
                side=envelope.side,
                floor_id=envelope.floor_id,
                contract_version=IDENTITY_CONTRACT_VERSION,
            )
        if not isfinite(occurrence.value):
            raise_identity_conflict(
                "score_identity_non_finite",
                predicate="finite_coordinate",
                source_vertex_ids=(occurrence.source_key.audit_tuple(),),
                reason="identity_non_finite_value",
                side=envelope.side,
                floor_id=envelope.floor_id,
                contract_version=IDENTITY_CONTRACT_VERSION,
                axis=occurrence.source_key.axis,
                hex=occurrence.value_hex,
            )
