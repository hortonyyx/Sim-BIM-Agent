"""B5 source-aware host resolution and evidence derivation.

The geometry resolver is pure.  The only mutating operation is the explicit
fresh-copy commit in :func:`apply_window_host_resolutions`; Va evidence remains
an output-bound sidecar and can never rewrite the accepted geometry.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal, Mapping

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

from src.agent.correction.artifact_serialization import serialize_correction_output
from src.agent.correction.cell_geometry import cell_polygon_vertices
from src.agent.correction.facade import ViewProjectionFrame
from src.agent.correction.facade_applicability import (
    CLAIM_ORDER,
    ApplicabilityIntervalV1,
    ElevationClaimEvidenceV1,
    FacadeApplicabilityInvariantError,
    FacadeVisibilityLedgerV1,
    FloorVisibilityLedgerV1,
    OpeningClaimsV1,
    OpeningClaimTargetV1,
    PlanClaimEvidenceV1,
    derive_opening_claim_applicability,
)
from src.agent.correction.facade_visibility import VisibilityTolerances
from src.agent.correction.footprint import floor_footprint_fingerprint
from src.agent.correction.schema import CorrectedGeometryV3
from src.agent.correction.window_sources import (
    ElevationSourceWindowV1,
    PlanSourceWindowV1,
    SourceIntervalV1,
    VerifiedWindowResolverInputs,
    WindowDirectionBindingError,
    WindowSourceErrorCategory,
    canonical_sha256,
    materialize_current_ring_va_elevation_bindings,
)
from src.agent.geometry.modelling import SegmentLine2D, window_verts_on_line

if TYPE_CHECKING:
    from src.agent.correction.finalize import PreparedCandidateIdentity

WINDOW_HOST_SCHEMA_VERSION = "1"
WINDOW_HOST_HELPER_VERSION = "window_host_resolver_v1"
WINDOW_HOST_ARTIFACT_VERSION = "window_hosts_v1"

FiniteFloat = Annotated[float, Strict(), AllowInfNan(False)]
StableId = Annotated[str, Strict(), StringConstraints(min_length=1)]
Hex64 = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceLocator = Annotated[str, Strict(), StringConstraints(pattern=r"^src:[0-9a-f]{64}$")]
_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)


def _payload_without(model: BaseModel, field: str) -> dict:
    value = model.model_dump(mode="json")
    value.pop(field, None)
    return value


class Point2V1(BaseModel):
    model_config = _CFG
    x: FiniteFloat
    y: FiniteFloat


class Point3V1(BaseModel):
    model_config = _CFG
    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat


class ParamIntervalV1(BaseModel):
    model_config = _CFG
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if not (0 <= self.lo < self.hi <= 1):
            raise ValueError("parameter interval requires 0 <= lo < hi <= 1")
        return self


class NegativeEvidenceDecisionV1(BaseModel):
    model_config = _CFG
    source_input_id: StableId
    channel: Literal["plan", "elevation"]
    claim: Literal["existence"]
    positive_evidence_declared: Annotated[bool, Strict()]
    negative_evidence_capable: Annotated[bool, Strict()]
    completeness_assertion_id: StableId | None
    coverage_frame: Literal["plan_floor_region", "elevation_local_along"] | None
    coverage_region: Literal["full_floor", "full_facade"] | None
    negative_intervals: tuple[SourceIntervalV1, ...]
    covers_complete_target: Annotated[bool, Strict()]
    outcome: Literal["positive", "trusted_negative_conflict", "uncorroborated"]

    @model_validator(mode="after")
    def _decision_shape(self):
        for left, right in zip(self.negative_intervals, self.negative_intervals[1:]):
            if right.lo <= left.hi:
                raise ValueError("negative intervals must be a canonical union")
        if self.outcome == "positive" and not self.positive_evidence_declared:
            raise ValueError("positive outcome requires declared positive evidence")
        if self.outcome == "trusted_negative_conflict" and (
            self.positive_evidence_declared
            or not self.negative_evidence_capable
            or self.completeness_assertion_id is None
            or self.coverage_frame is None
            or self.coverage_region is None
            or not self.covers_complete_target
        ):
            raise ValueError("trusted negative requires complete, non-positive manifest evidence")
        return self


class WindowHostResolutionV1(BaseModel):
    model_config = _CFG
    host_schema_version: Literal["1"]
    helper_version: Literal["window_host_resolver_v1"]
    window_id: StableId
    floor_id: StableId
    room_id: StableId
    branch: Literal["plan", "elevation"]
    facade_family: Literal["North", "South", "East", "West"]
    facade_segment_id: StableId
    segment_p1: Point2V1
    segment_p2: Point2V1
    segment_outward_normal: tuple[Literal[-1, 0, 1], Literal[-1, 0, 1]]
    room_boundary_interval: SourceIntervalV1
    clamped_span: SourceIntervalV1
    segment_parameter_interval: ParamIntervalV1
    clamped_plan_endpoints_p1_to_p2: tuple[Point2V1, Point2V1]
    z_interval: SourceIntervalV1
    clamped_vertices: tuple[Point3V1, Point3V1, Point3V1, Point3V1]
    source_locators: tuple[SourceLocator, ...]
    visible_overlap_intervals: tuple[SourceIntervalV1, ...]
    resolution_sha256: Hex64

    @model_validator(mode="after")
    def _shape_and_hash(self):
        if self.segment_outward_normal not in {(0, 1), (0, -1), (1, 0), (-1, 0)}:
            raise ValueError("C2 host normal must be one cardinal unit vector")
        expected_normal = {
            "North": (0, 1), "South": (0, -1),
            "East": (1, 0), "West": (-1, 0),
        }[self.facade_family]
        if self.segment_outward_normal != expected_normal:
            raise ValueError("segment normal disagrees with facade family")
        if self.branch == "plan" and self.visible_overlap_intervals:
            raise ValueError("plan branch visible_overlap_intervals must be empty")
        if self.branch == "elevation" and not self.visible_overlap_intervals:
            raise ValueError("elevation branch requires visible overlap")
        for left, right in zip(self.visible_overlap_intervals, self.visible_overlap_intervals[1:]):
            if right.lo <= left.hi:
                raise ValueError("visible overlap intervals must be canonical union")
        if self.resolution_sha256 != canonical_sha256(_payload_without(self, "resolution_sha256")):
            raise ValueError("resolution_sha256 does not match canonical host resolution")
        return self


class WindowHostClaimsV1(BaseModel):
    model_config = _CFG
    schema_version: Literal["1"]
    helper_version: Literal["window_host_resolver_v1"]
    resolver_inputs_sha256: Hex64
    view_manifest_sha256: Hex64
    facade_segments_sha256: Hex64
    resolutions: tuple[WindowHostResolutionV1, ...]
    aggregate_sha256: Hex64

    @model_validator(mode="after")
    def _aggregate(self):
        if tuple((item.floor_id, item.facade_segment_id, item.clamped_span.lo, item.clamped_span.hi, item.window_id) for item in self.resolutions) != tuple(sorted((item.floor_id, item.facade_segment_id, item.clamped_span.lo, item.clamped_span.hi, item.window_id) for item in self.resolutions)):
            raise ValueError("resolutions must be canonical sorted")
        expected = canonical_sha256([item.resolution_sha256 for item in self.resolutions])
        if self.aggregate_sha256 != expected:
            raise ValueError("aggregate_sha256 does not match ordered resolution hashes")
        if len({item.window_id for item in self.resolutions}) != len(self.resolutions):
            raise ValueError("host resolutions must contain unique window ids")
        return self


class WindowEvidenceDecisionV1(BaseModel):
    model_config = _CFG
    window_id: StableId
    source_locators: tuple[SourceLocator, ...]
    negative_evidence: tuple[NegativeEvidenceDecisionV1, ...]
    corroboration_status: Literal["supported", "uncorroborated"]
    evidence_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if self.source_locators != tuple(sorted(set(self.source_locators))):
            raise ValueError("evidence source locators must be canonical and unique")
        keys = tuple((item.channel, item.source_input_id) for item in self.negative_evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("negative evidence decisions must be canonical and unique")
        if self.evidence_sha256 != canonical_sha256(_payload_without(self, "evidence_sha256")):
            raise ValueError("evidence_sha256 does not match canonical evidence decision")
        return self


class WindowEvidenceLedgerV1(BaseModel):
    model_config = _CFG
    schema_version: Literal["1"]
    helper_version: Literal["window_evidence_ledger_v1"]
    direction_helper_version: Literal["window_direction_frame_v1"]
    output_sha256: Hex64
    feature_states_sha256: Hex64
    resolver_inputs_sha256: Hex64
    view_manifest_sha256: Hex64
    facade_segments_sha256: Hex64
    direction_bindings_sha256: Hex64
    va_ledger_content_sha256: Hex64
    decisions: tuple[WindowEvidenceDecisionV1, ...]
    aggregate_sha256: Hex64
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hashes(self):
        ids = tuple(item.window_id for item in self.decisions)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("evidence decisions must be sorted and unique by window id")
        if any(row.outcome == "trusted_negative_conflict"
               for decision in self.decisions for row in decision.negative_evidence):
            raise ValueError("accepted evidence ledger cannot contain a trusted-negative conflict")
        if self.aggregate_sha256 != canonical_sha256([item.evidence_sha256 for item in self.decisions]):
            raise ValueError("aggregate_sha256 does not match ordered evidence hashes")
        if self.content_sha256 != canonical_sha256(_payload_without(self, "content_sha256")):
            raise ValueError("content_sha256 does not match canonical evidence ledger")
        return self


class WindowHostsArtifactV1(BaseModel):
    model_config = _CFG
    artifact_version: Literal["window_hosts_v1"]
    output_sha256: Hex64
    claims: WindowHostClaimsV1
    evidence: WindowEvidenceLedgerV1
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash(self):
        if self.output_sha256 != self.evidence.output_sha256:
            raise ValueError("window-host artifact output identity mismatch")
        claim_ids = tuple(sorted(row.window_id for row in self.claims.resolutions))
        evidence_ids = tuple(row.window_id for row in self.evidence.decisions)
        if claim_ids != evidence_ids:
            raise ValueError("host claims and evidence window ids must be exactly equal")
        if (
            self.claims.resolver_inputs_sha256 != self.evidence.resolver_inputs_sha256
            or self.claims.view_manifest_sha256 != self.evidence.view_manifest_sha256
            or self.claims.facade_segments_sha256 != self.evidence.facade_segments_sha256
        ):
            raise ValueError("host claims and evidence identities disagree")
        if self.content_sha256 != canonical_sha256(_payload_without(self, "content_sha256")):
            raise ValueError("content_sha256 does not match canonical window hosts artifact")
        return self


def build_window_hosts_artifact(
    *,
    output_sha256: str,
    claims: WindowHostClaimsV1,
    evidence: WindowEvidenceLedgerV1,
) -> WindowHostsArtifactV1:
    """Construct the sole output-bound B5 host artifact projection."""
    payload = {
        "artifact_version": WINDOW_HOST_ARTIFACT_VERSION,
        "output_sha256": output_sha256,
        "claims": claims.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
    }
    return WindowHostsArtifactV1(
        artifact_version=WINDOW_HOST_ARTIFACT_VERSION,
        output_sha256=output_sha256,
        claims=claims,
        evidence=evidence,
        content_sha256=canonical_sha256(payload),
    )


HostConflictReason = Literal[
    "source_identity_invalid", "source_channel_missing", "source_geometry_mismatch", "claim_evidence_invalid",
    "direction_binding_invalid", "va_identity_invalid", "cross_segment_boundary", "segment_endpoint_overrun",
    "invalid_window_span", "zero_segment_candidates", "multiple_segment_candidates", "elevation_segment_not_visible",
    "zero_room_interval_candidates", "multiple_room_interval_candidates", "cross_room_boundary", "room_mismatch",
    "floor_mismatch", "facade_mismatch", "trusted_negative_conflict", "parent_wall_zero_candidates",
    "parent_wall_multiple_candidates", "invalid_host_line", "resolver_output_tampered",
]


class WindowHostConflictV1(BaseModel):
    model_config = _CFG
    kind: Literal["window_host_conflict"]
    conflict_schema_version: Literal["1"]
    window_id: StableId
    floor_id: StableId | None
    branch: Literal["plan", "elevation", "unresolved"]
    reason_code: HostConflictReason
    raw_span: SourceIntervalV1 | None
    source_input_ids: tuple[StableId, ...]
    candidate_segment_ids: tuple[StableId, ...]
    candidate_room_ids: tuple[StableId, ...]
    crossed_segment_ids: tuple[StableId, ...]
    trusted_negative: tuple[NegativeEvidenceDecisionV1, ...]
    upstream_error_code: Literal[
        "va_claim_ledger_invalid", "va_projection_frame_invalid", "va_direction_unresolved",
        "va_identity_mismatch", "va_visibility_ledger_invalid", "direction_binding_ring_invalid",
        "direction_binding_ring_incompatible",
    ] | None
    failed_gate_id: Literal["correction.window_host_resolution"]
    fallback_action: Literal["needs_input_no_geometry_commit", "invariant_no_geometry_commit"]
    blocking: Literal[True]

    @model_validator(mode="after")
    def _upstream_shape(self):
        if self.upstream_error_code is not None and self.reason_code not in {
            "claim_evidence_invalid", "direction_binding_invalid", "va_identity_invalid",
        }:
            raise ValueError("Va upstream code requires a Va-mapped reason")
        if self.reason_code == "va_identity_invalid" and self.fallback_action != "invariant_no_geometry_commit":
            raise ValueError("Va identity failure is invariant")
        if self.upstream_error_code in {"direction_binding_ring_invalid", "direction_binding_ring_incompatible"} and (
                self.reason_code != "direction_binding_invalid" or self.fallback_action != "invariant_no_geometry_commit"):
            raise ValueError("ring binding failure must be a direction invariant")
        return self


class WindowHostResolutionAuditV1(BaseModel):
    model_config = _CFG
    kind: Literal["window_host_resolution"]
    rule_id: Literal["deterministic_core.window_host_resolver_v1"]
    stage: Literal["core"]
    claim_type: Literal["topology_identity"]
    window_id: StableId
    floor_id: StableId
    original_room_id: StableId | None
    resolved_room_id: StableId
    original_facade_segment_id: None
    resolved_facade_segment_id: StableId
    original_span: SourceIntervalV1
    resolved_span: SourceIntervalV1
    source_ids: tuple[SourceLocator, ...]
    branch: Literal["plan", "elevation"]
    resolution_sha256: Hex64
    tolerance_names: tuple[
        Literal["WINDOW_SEGMENT_ENDPOINT_CLAMP_TOL"],
        Literal["WINDOW_HOST_SPAN_EPSILON"],
        Literal["WINDOW_HOST_PLANE_EPSILON"],
    ]
    changes_topology: Literal[True]


def map_direction_binding_error(exc: WindowDirectionBindingError, *, geom, verified_inputs: VerifiedWindowResolverInputs,
                                phase: Literal["dry_pre_transform", "dry_post_transform", "final"]) -> tuple[WindowHostConflictV1, ...]:
    """Map only the two current-ring errors to blocking typed conflicts.

    Phase B records ``phase`` in rejected-attempt check evidence; this pure
    Phase-A mapper intentionally does not mutate geometry/audit.
    """
    affected_floors = set()
    if isinstance(exc.context.get("floor_id"), str):
        affected_floors.add(exc.context["floor_id"])
    windows = [window for window in geom.windows if not affected_floors or window.floor_id in affected_floors]
    return tuple(WindowHostConflictV1(kind="window_host_conflict", conflict_schema_version="1", window_id=window.id,
        floor_id=window.floor_id, branch="unresolved", reason_code="direction_binding_invalid", raw_span=None,
        source_input_ids=(), candidate_segment_ids=(), candidate_room_ids=(), crossed_segment_ids=(), trusted_negative=(),
        upstream_error_code=exc.code, failed_gate_id="correction.window_host_resolution",
        fallback_action="invariant_no_geometry_commit", blocking=True) for window in sorted(windows, key=lambda item: item.id))


class WindowHostResolutionError(ValueError):
    def __init__(
        self,
        conflicts: tuple[WindowHostConflictV1, ...],
        *,
        phase: Literal["dry_pre_transform", "dry_post_transform", "final"] | None = None,
        context: Mapping[str, object] | None = None,
    ):
        if not conflicts:
            raise ValueError("INVARIANT: WindowHostResolutionError requires typed conflicts")
        self.conflicts = conflicts
        # Companion evidence for checks.json/rejected-attempt diagnostics.  It
        # deliberately stays outside WindowHostConflictV1 so the conflict wire
        # remains frozen and phase-independent.
        self.phase = phase
        self.context = dict(context or {})
        super().__init__("window host resolution rejected")

    @property
    def category(self) -> WindowSourceErrorCategory:
        """F-9 (2026-08-06): fault classification for F-7's `run_stage.py`
        "model_draw_error → file as failed attempt, blind-resample" vs
        "input_integrity_error → re-raise, hard-crash" routing (mirrors
        `WindowResolverInputError.category`).

        Derived from each conflict's OWN `fallback_action` — the field every
        throw site inside this module already sets to decide whether a
        conflict may ever be silently folded into a softer outcome
        (`invariant_no_geometry_commit`) or not
        (`needs_input_no_geometry_commit`; see `envelope_transform.py`'s own
        pre-/post-transform handling, which uses this exact same field for
        the same reason). This is a throw-site judgment, never inferred from
        `str(self)` / message text, and never defaulted: any conflict marked
        `invariant_no_geometry_commit` means at least one throw site judged
        the state unsafe to ever resample past, so the WHOLE exception is
        `input_integrity_error`. Only when every conflict is the softer
        `needs_input_no_geometry_commit` — i.e. every throw site judged this
        a correction-draw claim that failed verification, not a broken
        invariant — is the exception `model_draw_error`."""
        if any(row.fallback_action == "invariant_no_geometry_commit" for row in self.conflicts):
            return "input_integrity_error"
        return "model_draw_error"


def _conflict(window, reason: HostConflictReason, *, branch="unresolved", candidates=(), rooms=(), crossed=(),
              raw_span=None, source_input_ids=(), trusted_negative=(), upstream_error_code=None,
              fallback_action="needs_input_no_geometry_commit"):
    return WindowHostConflictV1(kind="window_host_conflict", conflict_schema_version="1", window_id=window.id,
        floor_id=window.floor_id, branch=branch, reason_code=reason, raw_span=raw_span,
        source_input_ids=tuple(sorted(set(source_input_ids))), candidate_segment_ids=tuple(sorted(set(candidates))),
        candidate_room_ids=tuple(sorted(set(rooms))), crossed_segment_ids=tuple(sorted(set(crossed))),
        trusted_negative=tuple(trusted_negative), upstream_error_code=upstream_error_code,
        failed_gate_id="correction.window_host_resolution", fallback_action=fallback_action, blocking=True)


def _cell_edges(cell):
    points = cell_polygon_vertices(cell)
    if points is None:
        x0, x1 = cell.x; y0, y1 = cell.y
        points = [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
    return tuple(zip(points, points[1:] + points[:1]))


def _room_intervals(
    geom: CorrectedGeometryV3,
    segment,
    *,
    plane_eps: float,
    span_eps: float,
):
    """Room-owned maximal exterior edge intervals; never merge across rooms."""
    out: list[tuple[str, float, float]] = []
    for floor in geom.floors:
        if floor.id != segment.floor_id:
            continue
        for cell in floor.cells:
            for a, b in _cell_edges(cell):
                dx, dy = b[0] - a[0], b[1] - a[1]
                length = abs(dx) + abs(dy)
                if not length:
                    continue
                normal = (int(dy / length), int(-dx / length))
                if normal != segment.outward_normal:
                    continue
                if (dx and abs(a[1] - segment.p1[1]) <= plane_eps
                        and abs(b[1] - segment.p2[1]) <= plane_eps):
                    lo, hi = sorted((a[0], b[0]))
                elif (dy and abs(a[0] - segment.p1[0]) <= plane_eps
                      and abs(b[0] - segment.p2[0]) <= plane_eps):
                    lo, hi = sorted((a[1], b[1]))
                else:
                    continue
                lo = max(lo, segment.world_along_interval.lo)
                hi = min(hi, segment.world_along_interval.hi)
                if hi - lo > span_eps:
                    out.append((cell.id, lo, hi))
    merged: list[list[object]] = []
    for room_id, lo, hi in sorted(out, key=lambda row: (row[0], row[1], row[2])):
        if (merged and merged[-1][0] == room_id
                and lo <= float(merged[-1][2]) + span_eps):
            merged[-1][2] = max(float(merged[-1][2]), hi)
        else:
            merged.append([room_id, lo, hi])
    return tuple((str(room_id), float(lo), float(hi)) for room_id, lo, hi in merged)


def _same_resolution_representation_close(a: tuple[float, ...], b: tuple[float, ...], eps: tuple[float, ...]) -> bool:
    """True when every coordinate of ``a``/``b`` agrees within its own ``eps``.

    F-18 (2026-08-09): used only to re-compare two independently-computed
    binary64 representations of the SAME already-resolved geometric fact —
    e.g. a stored ``clamped_span`` against a fresh ``line.point_at(t)``
    reprojection of that SAME resolution's own stored parameter interval.
    An exact ``!=`` on floats there flags 1-4 ULP round-trip noise
    (<= 2e-15 m on real production geometry) as tampering even though both
    sides trace the same wall through different, equally-valid arithmetic.
    This helper is never a measurement/matching tolerance between two
    DIFFERENT candidates — every other comparison in this module (segment
    selection, room binding, etc.) stays exact or uses its own documented
    physical tolerance.
    """
    return len(a) == len(b) == len(eps) and all(
        abs(av - bv) <= e for av, bv, e in zip(a, b, eps)
    )


def window_host_claim_issues(
    geom: CorrectedGeometryV3,
    *,
    claims: WindowHostClaimsV1,
    tolerances,
) -> list[dict[str, object]]:
    """Freshly audit final geometry against host records without source trust.

    This is shared by the correction/build/kernel boundaries.  It never feeds
    record vertices back into the line helper as an input.
    """
    issues: list[dict[str, object]] = []
    source_ids = [window.id for window in geom.windows]
    resolution_ids = [row.window_id for row in claims.resolutions]
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(resolution_ids):
        issues.append({
            "reason": "window_resolution_totality",
            "source_window_ids": sorted(source_ids),
            "resolution_ids": sorted(resolution_ids),
        })
    if claims.facade_segments_sha256 != _facade_segments_sha256(geom):
        issues.append({"reason": "facade_segments_sha256"})
    windows = {window.id: window for window in geom.windows}
    segments = {segment.id: segment for segment in geom.facade_segments}
    floors = {floor.id: floor for floor in geom.floors}
    for resolution in claims.resolutions:
        window = windows.get(resolution.window_id)
        segment = segments.get(resolution.facade_segment_id)
        floor = floors.get(resolution.floor_id)
        prefix = {"window_id": resolution.window_id}
        if window is None:
            continue
        if (
            window.floor_id != resolution.floor_id
            or window.room != resolution.room_id
            or window.facade != resolution.facade_family
            or window.facade_segment_id != resolution.facade_segment_id
            or tuple(float(value) for value in window.span)
            != (resolution.clamped_span.lo, resolution.clamped_span.hi)
            or tuple(float(value) for value in window.z)
            != (resolution.z_interval.lo, resolution.z_interval.hi)
        ):
            issues.append({**prefix, "reason": "final_window_identity"})
        # F-16 follow-up (2026-08-08, §6 摊一 Step 2): unlike the
        # `floor_mismatch` check in `resolve_window_hosts` (below in this
        # file), this one is NOT provably tautological after the `floor`
        # derivation and is intentionally left unchanged. `floor` here is
        # looked up via `resolution.floor_id` — a value from `claims`, an
        # UNTRUSTED, potentially-persisted/forged audit payload this
        # function's own docstring says to audit "without trusting record
        # vertices" — not via `window.floor_id`. If `resolution.floor_id`
        # lies about which floor a window is on, `window.floor` (correctly
        # schema-derived from the window's OWN, real `floor_id`) generally
        # will not equal the LYING floor's name, so this still catches a
        # real, externally-observable discrepancy; it is not simply
        # implied by the `final_window_identity` check above; that check
        # already flags `window.floor_id != resolution.floor_id` directly.
        if floor is None or floor.name != window.floor:
            issues.append({**prefix, "reason": "floor_identity"})
        if segment is None:
            issues.append({**prefix, "reason": "segment_missing"})
            continue
        if (
            tuple(float(value) for value in segment.p1)
            != (resolution.segment_p1.x, resolution.segment_p1.y)
            or tuple(float(value) for value in segment.p2)
            != (resolution.segment_p2.x, resolution.segment_p2.y)
            or tuple(segment.outward_normal) != tuple(resolution.segment_outward_normal)
            or segment.facade_family != resolution.facade_family
        ):
            issues.append({**prefix, "reason": "segment_geometry"})
            continue
        intervals = _room_intervals(
            geom,
            segment,
            plane_eps=tolerances.window_host_plane_epsilon_m,
            span_eps=tolerances.window_host_span_epsilon_m,
        )
        complete = [
            row for row in intervals
            if resolution.clamped_span.lo >= row[1] - tolerances.window_host_span_epsilon_m
            and resolution.clamped_span.hi <= row[2] + tolerances.window_host_span_epsilon_m
        ]
        if complete != [(
            resolution.room_id,
            resolution.room_boundary_interval.lo,
            resolution.room_boundary_interval.hi,
        )]:
            issues.append({**prefix, "reason": "room_boundary_interval"})
        try:
            line = SegmentLine2D(
                (resolution.segment_p1.x, resolution.segment_p1.y),
                (resolution.segment_p2.x, resolution.segment_p2.y),
            )
            dx = resolution.segment_p2.x - resolution.segment_p1.x
            dy = resolution.segment_p2.y - resolution.segment_p1.y
            if (dx == 0) == (dy == 0):
                raise ValueError("C2 host line must be axis-aligned")
            t = (
                resolution.segment_parameter_interval.lo,
                resolution.segment_parameter_interval.hi,
            )
            q0 = line.point_at(t[0])
            q1 = line.point_at(t[1])
            # F-18: the host line is axis-aligned (guarded above), so one plan
            # coordinate runs ALONG the facade and the other is the plane
            # offset.  Each gets B5's own designated epsilon -- these two
            # deliberately carry no dataclass default precisely so this
            # resolver can never silently borrow a Vg/legacy window tolerance.
            eps_xy = (
                (tolerances.window_host_span_epsilon_m, tolerances.window_host_plane_epsilon_m)
                if dy == 0
                else (tolerances.window_host_plane_epsilon_m, tolerances.window_host_span_epsilon_m)
            )
            declared_endpoints = tuple(
                (point.x, point.y)
                for point in resolution.clamped_plan_endpoints_p1_to_p2
            )
            if len(declared_endpoints) != 2 or not all(
                _same_resolution_representation_close(declared, fresh, eps_xy)
                for declared, fresh in zip(declared_endpoints, (q0, q1))
            ):
                raise ValueError("p1->p2 endpoints")
            projected = (q0[0], q1[0]) if dy == 0 else (q0[1], q1[1])
            lo = projected[0] if projected[0] < projected[1] else projected[1]
            hi = projected[1] if projected[0] < projected[1] else projected[0]
            if not _same_resolution_representation_close(
                (lo, hi),
                (resolution.clamped_span.lo, resolution.clamped_span.hi),
                (tolerances.window_host_span_epsilon_m,) * 2,
            ):
                raise ValueError("world span")
            fresh_vertices = window_verts_on_line(
                host_line=line,
                parameter_interval=t,
                z_interval=(resolution.z_interval.lo, resolution.z_interval.hi),
                outward_normal_xy=tuple(float(value) for value in resolution.segment_outward_normal),
            )
            declared_vertices = [
                (point.x, point.y, point.z) for point in resolution.clamped_vertices
            ]
            # z stays EXACT (sol cross-review 2026-08-09, MINOR-2): the earlier
            # "reuse plane epsilon or invent a constant" framing was a false
            # dichotomy.  `window_verts_on_line` copies `z_interval` straight
            # into each vertex (modelling.py:412-420) -- z never travels the
            # world -> parameter -> world round trip that produces F-18's ULP
            # noise, so it needs no tolerance at all.  Borrowing the XY plane
            # epsilon would also be semantically wrong: that one guards planar
            # collinearity, and downstream z-extent checks use the SPAN epsilon
            # (modelling.py:837-840).  A 0.0 epsilon here IS exact comparison.
            eps_xyz = (*eps_xy, 0.0)
            if len(fresh_vertices) != len(declared_vertices) or not all(
                _same_resolution_representation_close(fresh, declared, eps_xyz)
                for fresh, declared in zip(fresh_vertices, declared_vertices)
            ):
                raise ValueError("vertices")
        except ValueError as exc:
            issues.append({**prefix, "reason": "line_geometry", "detail": str(exc)})
    audit_rows: list[WindowHostResolutionAuditV1] = []
    for raw in geom.corrections:
        if isinstance(raw, dict) and raw.get("kind") == "window_host_resolution":
            try:
                audit_rows.append(WindowHostResolutionAuditV1.model_validate_json(
                    json.dumps(raw, separators=(",", ":"), ensure_ascii=False),
                ))
            except ValueError as exc:
                issues.append({"reason": "audit_wire", "detail": str(exc)})
    audit_by_window: dict[str, list[WindowHostResolutionAuditV1]] = {}
    for row in audit_rows:
        audit_by_window.setdefault(row.window_id, []).append(row)
    for resolution in claims.resolutions:
        rows = audit_by_window.get(resolution.window_id, [])
        if len(rows) != 1:
            issues.append({"window_id": resolution.window_id, "reason": "audit_totality"})
            continue
        row = rows[0]
        if (
            row.floor_id != resolution.floor_id
            or row.resolved_room_id != resolution.room_id
            or row.resolved_facade_segment_id != resolution.facade_segment_id
            or (row.resolved_span.lo, row.resolved_span.hi)
            != (resolution.clamped_span.lo, resolution.clamped_span.hi)
            or row.source_ids != resolution.source_locators
            or row.branch != resolution.branch
            or row.resolution_sha256 != resolution.resolution_sha256
        ):
            issues.append({"window_id": resolution.window_id, "reason": "audit_relation"})
    extra_audit = set(audit_by_window) - set(resolution_ids)
    if extra_audit:
        issues.append({"reason": "audit_extra", "window_ids": sorted(extra_audit)})
    return issues


def _visibility_tolerances(tolerances) -> VisibilityTolerances:
    return VisibilityTolerances(
        depth_epsilon_m=tolerances.facade_visibility_depth_epsilon_m,
        endpoint_epsilon_m=tolerances.facade_visibility_endpoint_epsilon_m,
    )


def _facade_segments_sha256(geom: CorrectedGeometryV3) -> str:
    rank = {"North": 0, "South": 1, "East": 2, "West": 3}
    rows = sorted(
        geom.facade_segments,
        key=lambda segment: (
            segment.floor_id,
            rank[segment.facade_family],
            segment.world_along_interval.lo,
            segment.world_along_interval.hi,
            segment.depth,
            segment.id,
        ),
    )
    return canonical_sha256([segment.model_dump(mode="json") for segment in rows])


def _source_world_interval(source, window, bindings) -> SourceIntervalV1:
    if isinstance(source, PlanSourceWindowV1):
        interval = source.world_x_interval if window.facade in {"North", "South"} else source.world_y_interval
        return SourceIntervalV1(lo=float(interval.lo), hi=float(interval.hi))
    binding = bindings.get(source.source_input_id)
    if binding is None or binding.resolved_building_direction != window.facade:
        raise KeyError(source.source_input_id)
    frame = ViewProjectionFrame(
        facade_family=binding.resolved_building_direction,
        world_axis=binding.world_axis,
        sign=binding.sign,
        along_origin=binding.along_origin,
        mirrored=binding.mirrored,
        local_x_positive=binding.local_x_positive,
    )
    a = frame.to_world_along(source.local_along_interval.lo)
    b = frame.to_world_along(source.local_along_interval.hi)
    return SourceIntervalV1(lo=float(min(a, b)), hi=float(max(a, b)))


def _plan_source_matches_plane(source: PlanSourceWindowV1, window, segment, eps: float) -> bool:
    plane_interval = source.world_y_interval if window.facade in {"North", "South"} else source.world_x_interval
    plane = segment.p1[1] if window.facade in {"North", "South"} else segment.p1[0]
    return plane_interval.lo - eps <= plane <= plane_interval.hi + eps


def resolve_window_hosts(geom: CorrectedGeometryV3, *, verified_inputs: VerifiedWindowResolverInputs,
                         tolerances, commit: Literal[False] = False) -> WindowHostClaimsV1:
    """Pure B5 host resolver.  It deliberately has no center/nearest fallback."""
    if commit is not False:
        raise ValueError("resolve_window_hosts is pure; use apply_window_host_resolutions")
    if any(window.facade_segment_id is not None for window in geom.windows):
        raise WindowHostResolutionError(tuple(
            _conflict(window, "source_identity_invalid", fallback_action="invariant_no_geometry_commit")
            for window in sorted(geom.windows, key=lambda item: item.id)
            if window.facade_segment_id is not None
        ))
    visibility_tolerances = _visibility_tolerances(tolerances)
    bindings_tuple = materialize_current_ring_va_elevation_bindings(
        geom=geom,
        manifest=verified_inputs.inputs.view_manifest,
        direction_facts=verified_inputs.inputs.elevation_direction_facts,
        visibility_tolerances=visibility_tolerances,
    ) if geom.windows else ()
    bindings = {binding.input_id: binding for binding in bindings_tuple}
    links = {w.id: [] for w in geom.windows}
    sources = {s.source_locator: s for s in verified_inputs.inputs.source_windows}
    for link in verified_inputs.inputs.claim_links:
        if link.claim == "existence" and link.source_locator in sources:
            links.setdefault(link.window_id, []).append(sources[link.source_locator])
    records = []
    conflicts = []
    floors = {floor.id: floor for floor in geom.floors}
    for window in sorted(geom.windows, key=lambda w: w.id):
        existence = links.get(window.id, [])
        plan = [source for source in existence if isinstance(source, PlanSourceWindowV1)]
        elev = [source for source in existence if isinstance(source, ElevationSourceWindowV1)]
        if not plan and not elev:
            conflicts.append(_conflict(window, "source_channel_missing"))
            continue
        branch = "plan" if plan else "elevation"
        source_inputs = [source.source_input_id for source in existence]
        floor = floors.get(window.floor_id)
        if floor is None:
            conflicts.append(_conflict(window, "floor_mismatch", branch=branch, source_input_ids=source_inputs))
            continue
        # F-16 follow-up (2026-08-08, §6 摊一 Step 2): `floor.name !=
        # window.floor` used to be part of the condition above (a second,
        # independent way to reach the same "floor_mismatch" conflict) —
        # now provably unreachable, not merely redundant: `window.floor` is
        # schema-derived from `by_id[window.floor_id].name`
        # (CorrectedGeometryV3._v3_integrity), and `floor` here is looked
        # up via that SAME `window.floor_id`, off the SAME `geom` the
        # window came from — so `floor.name == window.floor` holds for
        # every window that reaches this line, by construction, not by
        # coincidence. Kept as a loud assertion (not silently dropped)
        # because a validated CorrectedGeometryV3 instance's fields CAN
        # still be mutated after construction by non-schema code (pydantic
        # does not freeze these models); if that ever desyncs the two, this
        # is the earliest point that would notice.
        assert floor.name == window.floor, (
            f"window {window.id}: floor/floor_id desync despite schema derivation "
            f"(floor.name={floor.name!r}, window.floor={window.floor!r}) — "
            "geom was mutated after construction without going through "
            "CorrectedGeometryV3 validation again"
        )
        z0, z1 = map(float, window.z)
        if not (floor.z_floor <= z0 < z1 <= floor.z_floor + floor.ceiling_height):
            conflicts.append(_conflict(window, "invalid_window_span", branch=branch, source_input_ids=source_inputs))
            continue
        if window.span[0] >= window.span[1]:
            conflicts.append(_conflict(window, "invalid_window_span", branch=branch, source_input_ids=source_inputs))
            continue
        if branch == "plan" and not window.room:
            conflicts.append(_conflict(window, "room_mismatch", branch=branch, source_input_ids=source_inputs))
            continue
        raw = SourceIntervalV1(lo=float(window.span[0]), hi=float(window.span[1]))
        if (raw.hi - raw.lo < tolerances.min_edge_length_m
                or z1 - z0 < tolerances.min_edge_length_m):
            conflicts.append(_conflict(window, "invalid_window_span", branch=branch, raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        try:
            mapped_sources = tuple((source, _source_world_interval(source, window, bindings)) for source in existence)
        except KeyError:
            conflicts.append(_conflict(window, "facade_mismatch", branch=branch, raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        if any(min(raw.hi, interval.hi) - max(raw.lo, interval.lo) <= tolerances.window_host_span_epsilon_m
               for _, interval in mapped_sources):
            conflicts.append(_conflict(window, "source_geometry_mismatch", branch=branch, raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        segs = [segment for segment in geom.facade_segments
                if segment.floor_id == window.floor_id and segment.facade_family == window.facade]
        if branch == "plan":
            segs = [segment for segment in segs
                    if all(_plan_source_matches_plane(source, window, segment, tolerances.window_host_plane_epsilon_m)
                           for source in plan)]
        overlapping = [segment for segment in segs
                       if min(raw.hi, segment.world_along_interval.hi)
                       - max(raw.lo, segment.world_along_interval.lo) > tolerances.window_host_span_epsilon_m]
        fully_containing = [segment for segment in overlapping
                            if max(0.0, segment.world_along_interval.lo - raw.lo)
                            <= tolerances.window_segment_endpoint_clamp_tol_m
                            and max(0.0, raw.hi - segment.world_along_interval.hi)
                            <= tolerances.window_segment_endpoint_clamp_tol_m]
        # Adjacent partial overlaps mean the raw span crosses a segment seam.
        # Two or more full containers are instead an overlapping/ambiguous
        # topology and must reach the distinct multiple-candidate diagnostic.
        crossed = [segment.id for segment in overlapping]
        if len(crossed) >= 2 and len(fully_containing) < 2:
            conflicts.append(_conflict(window, "cross_segment_boundary", branch=branch, crossed=crossed,
                                       raw_span=raw, source_input_ids=source_inputs))
            continue
        candidates = []
        overruns = []
        hidden = []
        for segment in segs:
            left_over = max(0.0, segment.world_along_interval.lo - raw.lo)
            right_over = max(0.0, raw.hi - segment.world_along_interval.hi)
            overlap = min(raw.hi, segment.world_along_interval.hi) - max(raw.lo, segment.world_along_interval.lo)
            if overlap > tolerances.window_host_span_epsilon_m and (
                left_over > tolerances.window_segment_endpoint_clamp_tol_m
                or right_over > tolerances.window_segment_endpoint_clamp_tol_m
            ):
                overruns.append(segment.id)
                continue
            if left_over > tolerances.window_segment_endpoint_clamp_tol_m or right_over > tolerances.window_segment_endpoint_clamp_tol_m:
                continue
            lo = max(raw.lo, segment.world_along_interval.lo)
            hi = min(raw.hi, segment.world_along_interval.hi)
            if hi - lo < tolerances.min_edge_length_m:
                continue
            visible_intersections = tuple(
                SourceIntervalV1(lo=float(max(lo, interval.lo)), hi=float(min(hi, interval.hi)))
                for interval in segment.visible_intervals
                if min(hi, interval.hi) - max(lo, interval.lo) > tolerances.window_host_span_epsilon_m
            )
            if branch == "elevation" and not visible_intersections:
                hidden.append(segment.id)
                continue
            visible = visible_intersections if branch == "elevation" else ()
            candidates.append((segment, SourceIntervalV1(lo=float(lo), hi=float(hi)), visible))
        if not candidates and overruns:
            conflicts.append(_conflict(window, "segment_endpoint_overrun", branch=branch, candidates=overruns,
                                       raw_span=raw, source_input_ids=source_inputs))
            continue
        if len(candidates) != 1:
            reason = ("elevation_segment_not_visible" if branch == "elevation" and hidden and not candidates
                      else "zero_segment_candidates" if not candidates else "multiple_segment_candidates")
            conflicts.append(_conflict(window, reason, branch=branch, candidates=[item[0].id for item in candidates],
                                       raw_span=raw, source_input_ids=source_inputs))
            continue
        segment, span, visible = candidates[0]
        intervals = _room_intervals(
            geom,
            segment,
            plane_eps=tolerances.window_host_plane_epsilon_m,
            span_eps=tolerances.window_host_span_epsilon_m,
        )
        complete = [row for row in intervals
                    if span.lo >= row[1] - tolerances.window_host_span_epsilon_m
                    and span.hi <= row[2] + tolerances.window_host_span_epsilon_m]
        overlaps = [row for row in intervals
                    if min(span.hi, row[2]) - max(span.lo, row[1]) > tolerances.window_host_span_epsilon_m]
        overlap_rooms = {row[0] for row in overlaps}
        if len(overlap_rooms) >= 2 and not complete:
            conflicts.append(_conflict(window, "cross_room_boundary", branch=branch, rooms=overlap_rooms,
                                       raw_span=raw, source_input_ids=source_inputs))
            continue
        if len(complete) > 1:
            conflicts.append(_conflict(window, "multiple_room_interval_candidates", branch=branch,
                                       rooms=[row[0] for row in complete], raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        if not complete:
            conflicts.append(_conflict(window, "zero_room_interval_candidates", branch=branch,
                                       rooms=[row[0] for row in overlaps], raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        room = complete[0]
        if window.room is not None and window.room != room[0]:
            conflicts.append(_conflict(window, "room_mismatch", branch=branch, rooms=[room[0]], raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        if (span.lo <= room[1] + tolerances.window_host_span_epsilon_m
                and span.hi >= room[2] - tolerances.window_host_span_epsilon_m
                and z0 <= floor.z_floor + tolerances.window_host_span_epsilon_m
                and z1 >= floor.z_floor + floor.ceiling_height - tolerances.window_host_span_epsilon_m):
            conflicts.append(_conflict(window, "invalid_window_span", branch=branch,
                                       candidates=[segment.id], rooms=[room[0]], raw_span=raw,
                                       source_input_ids=source_inputs))
            continue
        p1, p2 = segment.p1, segment.p2
        try:
            host_line = SegmentLine2D(tuple(p1), tuple(p2))
            if window.facade in {"North", "South"}:
                world_lo_point = (span.lo, float(p1[1]))
                world_hi_point = (span.hi, float(p1[1]))
            else:
                world_lo_point = (float(p1[0]), span.lo)
                world_hi_point = (float(p1[0]), span.hi)
            world_lo_t = host_line.parameter_of(world_lo_point)
            world_hi_t = host_line.parameter_of(world_hi_point)
            if world_lo_t < world_hi_t:
                tlo, thi = world_lo_t, world_hi_t
            else:
                tlo, thi = world_hi_t, world_lo_t
            if not (0 <= tlo < thi <= 1):
                raise ValueError("parameter interval lies outside host line")
            q0 = host_line.point_at(tlo)
            q1 = host_line.point_at(thi)
            fresh_vertices = window_verts_on_line(
                host_line=host_line,
                parameter_interval=(tlo, thi),
                z_interval=(z0, z1),
                outward_normal_xy=tuple(float(value) for value in segment.outward_normal),
            )
        except ValueError:
            conflicts.append(_conflict(window, "invalid_host_line", branch=branch, candidates=[segment.id],
                                       raw_span=raw, source_input_ids=source_inputs))
            continue
        verts = tuple(Point3V1(x=x, y=y, z=z) for x, y, z in fresh_vertices)
        payload = dict(
            host_schema_version="1", helper_version=WINDOW_HOST_HELPER_VERSION, window_id=window.id,
            floor_id=window.floor_id, room_id=room[0], branch=branch, facade_family=window.facade,
            facade_segment_id=segment.id, segment_p1=Point2V1(x=p1[0], y=p1[1]),
            segment_p2=Point2V1(x=p2[0], y=p2[1]), segment_outward_normal=segment.outward_normal,
            room_boundary_interval=SourceIntervalV1(lo=room[1], hi=room[2]), clamped_span=span,
            segment_parameter_interval=ParamIntervalV1(lo=tlo, hi=thi),
            clamped_plan_endpoints_p1_to_p2=(Point2V1(x=q0[0], y=q0[1]), Point2V1(x=q1[0], y=q1[1])),
            z_interval=SourceIntervalV1(lo=z0, hi=z1), clamped_vertices=verts,
            source_locators=tuple(sorted(source.source_locator for source in existence)),
            visible_overlap_intervals=visible,
        )
        records.append(WindowHostResolutionV1(
            **payload, resolution_sha256=canonical_sha256({
                key: value.model_dump(mode="json") if isinstance(value, BaseModel)
                else [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
                if isinstance(value, tuple) else value
                for key, value in payload.items()
            }),
        ))
    if conflicts:
        raise WindowHostResolutionError(tuple(conflicts))
    ordered = tuple(sorted(records, key=lambda row: (
        row.floor_id, row.facade_segment_id, row.clamped_span.lo, row.clamped_span.hi, row.window_id,
    )))
    return WindowHostClaimsV1(
        schema_version="1", helper_version=WINDOW_HOST_HELPER_VERSION,
        resolver_inputs_sha256=verified_inputs.inputs.content_sha256,
        view_manifest_sha256=verified_inputs.inputs.view_manifest.content_sha256,
        facade_segments_sha256=_facade_segments_sha256(geom), resolutions=ordered,
        aggregate_sha256=canonical_sha256([row.resolution_sha256 for row in ordered]),
    )


def apply_window_host_resolutions(
    geom: CorrectedGeometryV3,
    *,
    claims: WindowHostClaimsV1,
    verified_inputs: VerifiedWindowResolverInputs,
    tolerances,
) -> CorrectedGeometryV3:
    """Recompute the pure claims, then atomically commit refs, spans and audit."""
    recomputed = resolve_window_hosts(
        geom, verified_inputs=verified_inputs, tolerances=tolerances, commit=False,
    )
    if recomputed != claims:
        windows = {window.id: window for window in geom.windows}
        raise WindowHostResolutionError(tuple(
            _conflict(
                windows[window_id], "resolver_output_tampered",
                fallback_action="invariant_no_geometry_commit",
            )
            for window_id in sorted(windows)
        ))
    candidate = geom.model_copy(deep=True)
    windows = {window.id: window for window in candidate.windows}
    audit_rows = []
    for resolution in claims.resolutions:
        window = windows[resolution.window_id]
        original_room = window.room
        # ``original_span`` is the resolver-entry/post-snap value.  The raw
        # producer value remains authenticated in VerifiedWindowResolverInputs
        # and Phase-D verification must replay deterministic snap before
        # comparing this audit field.
        original_span = SourceIntervalV1(lo=float(window.span[0]), hi=float(window.span[1]))
        if window.facade_segment_id is not None:
            raise WindowHostResolutionError((_conflict(
                window, "source_identity_invalid", branch=resolution.branch,
                fallback_action="invariant_no_geometry_commit",
            ),))
        window.room = resolution.room_id
        window.facade_segment_id = resolution.facade_segment_id
        window.span = [resolution.clamped_span.lo, resolution.clamped_span.hi]
        audit = WindowHostResolutionAuditV1(
            kind="window_host_resolution",
            rule_id="deterministic_core.window_host_resolver_v1",
            stage="core",
            claim_type="topology_identity",
            window_id=window.id,
            floor_id=window.floor_id,
            original_room_id=original_room,
            resolved_room_id=resolution.room_id,
            original_facade_segment_id=None,
            resolved_facade_segment_id=resolution.facade_segment_id,
            original_span=original_span,
            resolved_span=resolution.clamped_span,
            source_ids=resolution.source_locators,
            branch=resolution.branch,
            resolution_sha256=resolution.resolution_sha256,
            tolerance_names=(
                "WINDOW_SEGMENT_ENDPOINT_CLAMP_TOL",
                "WINDOW_HOST_SPAN_EPSILON",
                "WINDOW_HOST_PLANE_EPSILON",
            ),
            changes_topology=True,
        )
        audit_rows.append(audit.model_dump())
    candidate.corrections = [*candidate.corrections, *audit_rows]
    return CorrectedGeometryV3.model_validate(candidate.model_dump())


def recompute_window_host_claims(
    geom: CorrectedGeometryV3,
    *,
    verified_inputs: VerifiedWindowResolverInputs,
    tolerances,
) -> WindowHostClaimsV1:
    """Fresh-recompute final B5 claims without trusting persisted refs/vertices."""
    candidate = geom.model_copy(deep=True)
    for window in candidate.windows:
        window.facade_segment_id = None
    recomputed = resolve_window_hosts(
        candidate,
        verified_inputs=verified_inputs,
        tolerances=tolerances,
        commit=False,
    )
    issues = window_host_claim_issues(geom, claims=recomputed, tolerances=tolerances)
    if issues:
        windows = {window.id: window for window in geom.windows}
        conflicts = tuple(
            _conflict(
                windows[window_id],
                "resolver_output_tampered",
                fallback_action="invariant_no_geometry_commit",
            )
            for window_id in sorted(windows)
        )
        if conflicts:
            raise WindowHostResolutionError(conflicts, context={"issues": issues})
        raise RuntimeError(f"INVARIANT: empty-window host claims are inconsistent: {issues}")
    return recomputed


def map_va_applicability_error(
    exc: FacadeApplicabilityInvariantError,
    *,
    host_claims: WindowHostClaimsV1,
    verified_inputs: VerifiedWindowResolverInputs,
) -> tuple[WindowHostConflictV1, ...]:
    """Map only Va's frozen error vocabulary; unknown codes remain invariants."""
    if exc.code == "va_claim_ledger_invalid":
        if exc.context.get("claim") == "existence":
            reason = "va_identity_invalid"
            fallback = "invariant_no_geometry_commit"
        elif exc.context.get("claim") in set(CLAIM_ORDER) - {"existence"}:
            reason = "claim_evidence_invalid"
            fallback = "needs_input_no_geometry_commit"
        else:
            reason = "va_identity_invalid"
            fallback = "invariant_no_geometry_commit"
    elif exc.code in {"va_projection_frame_invalid", "va_direction_unresolved"}:
        reason = "direction_binding_invalid"
        fallback = "invariant_no_geometry_commit"
    elif exc.code in {"va_identity_mismatch", "va_visibility_ledger_invalid"}:
        reason = "va_identity_invalid"
        fallback = "invariant_no_geometry_commit"
    else:
        raise RuntimeError(f"INVARIANT: unknown facade applicability error code {exc.code!r}") from exc
    source_input = exc.context.get("input_id")
    opening_id = exc.context.get("opening_id")
    source_by_locator = {source.source_locator: source for source in verified_inputs.inputs.source_windows}
    source_links: dict[str, set[str]] = {}
    for link in verified_inputs.inputs.claim_links:
        source_links.setdefault(link.window_id, set()).add(source_by_locator[link.source_locator].source_input_id)
    rows = []
    for resolution in host_claims.resolutions:
        if isinstance(opening_id, str) and resolution.window_id != opening_id:
            continue
        if isinstance(source_input, str) and source_input not in source_links.get(resolution.window_id, set()):
            continue
        rows.append(WindowHostConflictV1(
            kind="window_host_conflict", conflict_schema_version="1", window_id=resolution.window_id,
            floor_id=resolution.floor_id, branch=resolution.branch, reason_code=reason,
            raw_span=resolution.clamped_span,
            source_input_ids=(source_input,) if isinstance(source_input, str) else (),
            candidate_segment_ids=(resolution.facade_segment_id,), candidate_room_ids=(resolution.room_id,),
            crossed_segment_ids=(), trusted_negative=(), upstream_error_code=exc.code,
            failed_gate_id="correction.window_host_resolution", fallback_action=fallback, blocking=True,
        ))
    if not rows and not host_claims.resolutions:
        raise RuntimeError(
            "INVARIANT: facade applicability error has no host resolution for a typed conflict"
        ) from exc
    if not rows:
        rows = [WindowHostConflictV1(
            kind="window_host_conflict", conflict_schema_version="1", window_id=resolution.window_id,
            floor_id=resolution.floor_id, branch=resolution.branch, reason_code=reason,
            raw_span=resolution.clamped_span,
            source_input_ids=(source_input,) if isinstance(source_input, str) else (),
            candidate_segment_ids=(resolution.facade_segment_id,), candidate_room_ids=(resolution.room_id,),
            crossed_segment_ids=(), trusted_negative=(), upstream_error_code=exc.code,
            failed_gate_id="correction.window_host_resolution", fallback_action=fallback, blocking=True,
        ) for resolution in host_claims.resolutions]
    return tuple(sorted(rows, key=lambda row: (row.window_id, row.reason_code)))


def _opening_claims(
    *, geom: CorrectedGeometryV3, host_claims: WindowHostClaimsV1,
    verified_inputs: VerifiedWindowResolverInputs,
) -> tuple[OpeningClaimsV1, ...]:
    source_by_locator = {source.source_locator: source for source in verified_inputs.inputs.source_windows}
    links: dict[tuple[str, str], list] = {}
    for link in verified_inputs.inputs.claim_links:
        links.setdefault((link.window_id, link.claim), []).append(source_by_locator[link.source_locator])
    floor_ref = {
        floor.id: index for index, floor in enumerate(
            sorted(geom.floors, key=lambda item: item.z_floor), start=1,
        )
    }
    rows = []
    for resolution in host_claims.resolutions:
        targets = []
        target = ApplicabilityIntervalV1(lo=resolution.clamped_span.lo, hi=resolution.clamped_span.hi)
        for claim in CLAIM_ORDER:
            evidence = []
            for source in links.get((resolution.window_id, claim), ()):
                if isinstance(source, PlanSourceWindowV1):
                    interval = source.world_x_interval if resolution.facade_family in {"North", "South"} else source.world_y_interval
                    evidence.append(PlanClaimEvidenceV1(
                        source_input_id=source.source_input_id,
                        world_interval=ApplicabilityIntervalV1(lo=interval.lo, hi=interval.hi),
                    ))
                else:
                    evidence.append(ElevationClaimEvidenceV1(
                        source_input_id=source.source_input_id,
                        local_interval=ApplicabilityIntervalV1(
                            lo=source.local_along_interval.lo, hi=source.local_along_interval.hi,
                        ),
                    ))
            targets.append(OpeningClaimTargetV1(
                claim=claim, target_world_interval=target, positive_evidence=tuple(evidence),
            ))
        rows.append(OpeningClaimsV1(
            opening_id=resolution.window_id, floor_id=resolution.floor_id,
            floor_ref=floor_ref[resolution.floor_id], facade_segment_id=resolution.facade_segment_id,
            facade_family=resolution.facade_family, claims=tuple(targets),
        ))
    return tuple(rows)


def _canonical_union(intervals) -> tuple[SourceIntervalV1, ...]:
    merged: list[list[float]] = []
    for interval in sorted(intervals, key=lambda item: (item.lo, item.hi)):
        if merged and interval.lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], interval.hi)
        else:
            merged.append([interval.lo, interval.hi])
    return tuple(SourceIntervalV1(lo=float(lo), hi=float(hi)) for lo, hi in merged)


def _covers(target: SourceIntervalV1, intervals: tuple[SourceIntervalV1, ...], eps: float) -> bool:
    if not intervals or intervals[0].lo > target.lo + eps:
        return False
    cursor = target.lo
    for interval in intervals:
        if interval.lo > cursor + eps:
            return False
        cursor = max(cursor, interval.hi)
    return cursor >= target.hi - eps


def _identity_conflicts(host_claims: WindowHostClaimsV1) -> WindowHostResolutionError:
    return WindowHostResolutionError(tuple(WindowHostConflictV1(
        kind="window_host_conflict", conflict_schema_version="1", window_id=resolution.window_id,
        floor_id=resolution.floor_id, branch=resolution.branch, reason_code="va_identity_invalid",
        raw_span=resolution.clamped_span, source_input_ids=(),
        candidate_segment_ids=(resolution.facade_segment_id,), candidate_room_ids=(resolution.room_id,),
        crossed_segment_ids=(), trusted_negative=(), upstream_error_code="va_identity_mismatch",
        failed_gate_id="correction.window_host_resolution", fallback_action="invariant_no_geometry_commit",
        blocking=True,
    ) for resolution in host_claims.resolutions))


def derive_window_evidence_ledger(
    geom: CorrectedGeometryV3,
    *,
    host_claims: WindowHostClaimsV1,
    verified_inputs: VerifiedWindowResolverInputs,
    candidate_identity: PreparedCandidateIdentity,
    tolerances,
) -> WindowEvidenceLedgerV1:
    """Derive Va and trusted-negative decisions from real candidate bytes."""
    output_bytes = serialize_correction_output(geom)
    if (candidate_identity.output_bytes != output_bytes
            or candidate_identity.output_sha256 != hashlib.sha256(output_bytes).hexdigest()
            or candidate_identity.feature_states_sha256
            != hashlib.sha256(candidate_identity.feature_states_bytes).hexdigest()):
        if host_claims.resolutions:
            raise _identity_conflicts(host_claims)
        raise ValueError("INVARIANT: empty-window candidate identity drift")
    if {resolution.window_id for resolution in host_claims.resolutions} != {window.id for window in geom.windows}:
        raise _identity_conflicts(host_claims)
    visibility_tolerances = _visibility_tolerances(tolerances)
    bindings = materialize_current_ring_va_elevation_bindings(
        geom=geom, manifest=verified_inputs.inputs.view_manifest,
        direction_facts=verified_inputs.inputs.elevation_direction_facts,
        visibility_tolerances=visibility_tolerances,
    )
    segment_hash = _facade_segments_sha256(geom)
    visibility = FacadeVisibilityLedgerV1(
        source_kind="accepted_correction", source_schema_version="3",
        source_output_sha256=candidate_identity.output_sha256,
        facade_segments_sha256=segment_hash,
        feature_states_sha256=candidate_identity.feature_states_sha256,
        helper_versions=("floor_footprint_v1", "facade_visibility_v1"),
        floors=tuple(FloorVisibilityLedgerV1(
            floor_id=floor.id, source_footprint_fingerprint=floor_footprint_fingerprint(geom, floor),
            segments=tuple(segment for segment in geom.facade_segments if segment.floor_id == floor.id),
        ) for floor in sorted(geom.floors, key=lambda item: item.id)),
    )
    openings = _opening_claims(
        geom=geom, host_claims=host_claims, verified_inputs=verified_inputs,
    )
    try:
        va_ledger = derive_opening_claim_applicability(
            visibility=visibility, manifest=verified_inputs.inputs.view_manifest,
            elevation_views=bindings, openings=openings,
        )
    except FacadeApplicabilityInvariantError as exc:
        raise WindowHostResolutionError(
            map_va_applicability_error(
                exc, host_claims=host_claims, verified_inputs=verified_inputs,
            ),
            phase="final",
            context=exc.context,
        ) from exc
    resolutions = {resolution.window_id: resolution for resolution in host_claims.resolutions}
    source_by_locator = {source.source_locator: source for source in verified_inputs.inputs.source_windows}
    locators_by_window: dict[str, set[str]] = {window.id: set() for window in geom.windows}
    for link in verified_inputs.inputs.claim_links:
        locators_by_window.setdefault(link.window_id, set()).add(link.source_locator)
    binding_by_input = {binding.input_id: binding for binding in bindings}
    decisions = []
    conflicts = []
    for opening in va_ledger.openings:
        resolution = resolutions[opening.opening_id]
        existence = next(claim for claim in opening.claims if claim.claim == "existence")
        by_input = {decision.source_input_id: decision for decision in existence.source_evidence}
        relevant = []
        for entry in verified_inputs.inputs.view_manifest.required_entries():
            if entry.view_type == "plan" and entry.floor_ref == opening.floor_ref:
                relevant.append(entry)
            elif (entry.view_type == "elevation" and entry.input_id in binding_by_input
                  and binding_by_input[entry.input_id].resolved_building_direction == opening.facade_family):
                relevant.append(entry)
        positive_channels = {
            decision.channel for decision in by_input.values()
            if decision.positive_evidence_declared and decision.applicable_intervals
        }
        negative_rows = []
        for entry in sorted(relevant, key=lambda item: (item.view_type, item.input_id)):
            va_decision = by_input.get(entry.input_id)
            positive = bool(va_decision and va_decision.positive_evidence_declared)
            capable = "existence" in entry.opening_evidence.negative_evidence_capable_claims
            raw_negative = () if va_decision is None else va_decision.negative_evidence_intervals
            negative_intervals = _canonical_union(
                SourceIntervalV1(lo=item.lo, hi=item.hi) for item in raw_negative
            )
            target = SourceIntervalV1(lo=existence.target_world_interval.lo, hi=existence.target_world_interval.hi)
            complete = _covers(target, negative_intervals, tolerances.window_host_span_epsilon_m)
            assertion = entry.opening_evidence.completeness_assertion
            coverage = entry.opening_evidence.coverage
            trusted = (
                not positive and capable and assertion is not None and coverage is not None
                and coverage.completeness_assertion_id == assertion.assertion_id
                and entry.view_type not in positive_channels and bool(positive_channels) and complete
            )
            outcome = "positive" if positive else "trusted_negative_conflict" if trusted else "uncorroborated"
            row = NegativeEvidenceDecisionV1(
                source_input_id=entry.input_id, channel=entry.view_type, claim="existence",
                positive_evidence_declared=positive, negative_evidence_capable=capable,
                completeness_assertion_id=assertion.assertion_id if capable and assertion is not None else None,
                coverage_frame=coverage.frame if capable and coverage is not None else None,
                coverage_region=coverage.region if capable and coverage is not None else None,
                negative_intervals=negative_intervals, covers_complete_target=complete,
                outcome=outcome,
            )
            negative_rows.append(row)
        negative_rows = sorted(negative_rows, key=lambda item: (item.channel, item.source_input_id))
        payload = dict(
            window_id=opening.opening_id,
            source_locators=tuple(sorted(locators_by_window.get(opening.opening_id, set()))),
            negative_evidence=tuple(negative_rows),
            corroboration_status="supported" if len(positive_channels) > 1 else "uncorroborated",
        )
        decision = WindowEvidenceDecisionV1(
            **payload, evidence_sha256=canonical_sha256({
                **payload,
                "negative_evidence": [row.model_dump(mode="json") for row in negative_rows],
                "source_locators": list(payload["source_locators"]),
            }),
        )
        decisions.append(decision)
        trusted = tuple(row for row in negative_rows if row.outcome == "trusted_negative_conflict")
        if trusted:
            window = next(item for item in geom.windows if item.id == opening.opening_id)
            conflicts.append(_conflict(
                window, "trusted_negative_conflict", branch=resolution.branch,
                candidates=[resolution.facade_segment_id], rooms=[resolution.room_id],
                raw_span=resolution.clamped_span,
                source_input_ids=[row.source_input_id for row in trusted], trusted_negative=trusted,
            ))
    if conflicts:
        raise WindowHostResolutionError(tuple(sorted(conflicts, key=lambda row: row.window_id)))
    decisions = sorted(decisions, key=lambda item: item.window_id)
    binding_hash = canonical_sha256([
        binding.model_dump(mode="json") for binding in sorted(bindings, key=lambda item: item.input_id)
    ])
    payload = dict(
        schema_version="1", helper_version="window_evidence_ledger_v1",
        direction_helper_version="window_direction_frame_v1",
        output_sha256=candidate_identity.output_sha256,
        feature_states_sha256=candidate_identity.feature_states_sha256,
        resolver_inputs_sha256=verified_inputs.inputs.content_sha256,
        view_manifest_sha256=verified_inputs.inputs.view_manifest.content_sha256,
        facade_segments_sha256=segment_hash,
        direction_bindings_sha256=binding_hash,
        va_ledger_content_sha256=va_ledger.content_sha256,
        decisions=tuple(decisions),
        aggregate_sha256=canonical_sha256([decision.evidence_sha256 for decision in decisions]),
    )
    return WindowEvidenceLedgerV1(
        **payload,
        content_sha256=canonical_sha256({
            **payload, "decisions": [decision.model_dump(mode="json") for decision in decisions],
        }),
    )


__all__ = [
    "WINDOW_HOST_ARTIFACT_VERSION", "WINDOW_HOST_HELPER_VERSION", "WINDOW_HOST_SCHEMA_VERSION",
    "WindowEvidenceDecisionV1", "WindowEvidenceLedgerV1", "WindowHostClaimsV1", "WindowHostConflictV1",
    "WindowHostResolutionAuditV1", "WindowHostResolutionError", "WindowHostResolutionV1", "WindowHostsArtifactV1",
    "apply_window_host_resolutions", "build_window_hosts_artifact", "derive_window_evidence_ledger", "map_direction_binding_error",
    "map_va_applicability_error", "recompute_window_host_claims", "resolve_window_hosts",
    "window_host_claim_issues",
]
