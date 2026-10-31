"""Compile the signed facts layer into the judge's derived answer exits.

The facts trio is the only maintained answer.  This module is a pure,
versioned derivation from ``as_signed`` plus its revisions ledger:

* ``form_a_axis`` projects every resolvable wall to its axis;
* ``form_b_exterior_skin`` projects exterior walls to their outer skin and
  inter-zone walls to their axis;
* :meth:`AnswerCompiler.reading_exam` derives the reading denominator from
  the same frozen facts instead of re-running the converter;
* :meth:`AnswerCompiler.clear_span_table` exposes usable cavity areas as a
  derived quantity.  Clear span is deliberately not an output profile.

The compiler never reads a stored ``basis``.  For form B it independently
classifies each cavity-side wall span by walking through the complete wall
band and testing the exit point against the facts-layer footprint and the
other measured cavities.  The evidence written on a projected edge names the
outward normal and, for an exterior edge, the exact footprint-ring edge that
the ray crossed.

Dependency closure follows sol B6.  A bad segment or junction invalidates the
whole incident ring; a bad view transform invalidates coordinate metrics for
the view; an ambiguous opening remains local to opening metrics; boundary
classification is profile-sensitive; and metric coverage always retains the
declared denominator.  An unprojectable ring contains no edges or vertices,
so partially calculated coordinates cannot leak through an NA result.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .as_measured import (INGEST_RESOLUTION_UNITS, UNITS_PER_METRE,
                          AsMeasuredBoundaryEdgeV1,
                          AsMeasuredOpeningV1, AsMeasuredV1,
                          AsMeasuredViewV1, AsMeasuredWallV1,
                          BoundaryConditionEvidenceV1,
                          snap_to_ingest_resolution)
from .gt_revisions import (AsSignedV1, RevisionsLedgerV1,
                           revisions_content_sha256,
                           verify_as_signed_reproduction)
from .gt_schema import REPO_ROOT
from .tarch_converter_schema import (ConversionReportV1,
                                     TarchConversionRequestV1, _StrictModel)

__all__ = [
    "ANSWER_COMPILER_VERSION", "DEPENDENCY_CLOSURE_VERSION",
    "OutputProfile", "AnswerCompiler", "AnswerCompilerInputError",
    "BoundaryConditionMismatchError", "BoundaryBasisMismatchError",
    "BoundaryBasisAuditV1", "BoundaryBasisExclusionV1", "BoundaryBasisRowV1",
    "BoundaryPairingProofV1", "BOUNDARY_PAIRING_MAX_RESIDUAL_UNITS",
    "NaRecordV1", "CompiledZoneEdgeV1", "CompiledZoneV1",
    "CompiledOpeningV1", "MetricResultV1", "CompiledViewV1",
    "CompiledAnswerV1", "read_facts_for_compilation",
    "reconcile_boundary_basis",
]

ANSWER_COMPILER_VERSION = 2
DEPENDENCY_CLOSURE_VERSION = 1
BOUNDARY_PAIRING_MAX_RESIDUAL_UNITS = 5_000


class AnswerCompilerInputError(ValueError):
    """The compiler inputs do not identify the same facts derivation."""


class BoundaryConditionMismatchError(AnswerCompilerInputError):
    """A stored boundary fact disagrees with the compiler's independent ray."""

    def __init__(self, mismatches: list[dict[str, Any]]) -> None:
        self.mismatches = mismatches
        details = "; ".join(
            f"{row['facts_edge_id']} stored={row['stored']} "
            f"recomputed={row['recomputed']}"
            for row in mismatches)
        super().__init__(f"answer_compiler_boundary_condition_mismatch:{details}")


class BoundaryBasisMismatchError(AnswerCompilerInputError):
    """The independent converter-basis reconciliation gate is red."""

    def __init__(self, audit: "BoundaryBasisAuditV1") -> None:
        self.audit = audit
        named = [f"{row.facts_edge_id}:{row.facts_boundary_condition}->"
                 f"{row.converter_basis}" for row in audit.mismatches]
        super().__init__(
            "boundary_basis_reconciliation_failed:"
            f"mismatches={len(audit.mismatches)}[{','.join(named)}] "
            f"structural={audit.structural_failures}")


class OutputProfile(str, Enum):
    """The only two output forms approved by the 2026-08-29 ruling."""

    FORM_A_AXIS = "form_a_axis"
    FORM_B_EXTERIOR_SKIN = "form_b_exterior_skin"


class NaRecordV1(_StrictModel):
    """One coordinate-free step in the invalidation closure."""

    rule: Literal[
        "segment_dependency", "junction_dependency", "view_coordinate_source",
        "opening_local", "profile_dependency", "metric_requirement",
        "unsigned_revision",
    ]
    component_kind: str
    component_id: str
    reason_code: str
    affected_metrics: list[str] = Field(default_factory=list)
    propagated_from: list[str] = Field(default_factory=list)


class ProjectionEvidenceV1(BoundaryConditionEvidenceV1):
    """Independent boundary-classification evidence for one valid edge."""

    boundary_condition: Literal["exterior", "interzone", "unclaimed_void", "unknown"]
    facts_boundary_edge_id: str | None = None


class BoundaryPairingHypothesisV1(_StrictModel):
    direction: Literal["forward", "reverse"]
    rotation: int
    converter_edge_indices: list[int]
    max_residual_units: float
    total_residual_units: float
    source_handle_matches: int


class BoundaryPairingProofV1(_StrictModel):
    view_id: str
    cavity_id: str
    converter_zone_id: str
    facts_edge_ids: list[str]
    geometric_converter_edge_indices: list[int]
    ancestry_converter_edge_indices: list[int]
    selected_direction: Literal["forward", "reverse"]
    selected_rotation: int
    selected_max_residual_units: float
    residual_hard_limit_units: int
    alternative_min_residual_units: float
    all_alternatives_strictly_worse: bool
    geometry_and_ancestry_pairing_identical: bool
    hypotheses: list[BoundaryPairingHypothesisV1]


class BoundaryBasisRowV1(_StrictModel):
    view_id: str
    cavity_id: str
    facts_edge_id: str
    converter_zone_id: str
    converter_edge_index: int
    facts_boundary_condition: Literal[
        "exterior", "interzone", "unclaimed_void", "unknown"]
    expected_converter_basis: Literal["wall_axis", "outer_skin"] | None
    converter_basis: Literal["wall_axis", "outer_skin"]
    matches: bool


class BoundaryBasisExclusionV1(_StrictModel):
    """A converter zone accounted for without pretending edges were paired.

    ②-1d rework3: an exclusion is only ever licensed by
    ``below_request_area_threshold`` -- an INDEPENDENTLY provable, by-design drop
    (the gate recomputes the raw cavity area itself and the threshold comes from
    the request, a different author, so a producer cannot forge it).  A
    producer-authored ``registered_ring_loss`` is ⛔ no longer a licence: it is
    fail-loud in ``reconcile_boundary_basis`` (a named structural failure),
    because the hash only stops a third party tampering with the ledger, not the
    producer itself writing a loss for every cavity it failed to ring
    ([[gate-measures-right-but-carrier-gets-swapped]]).  The gate still never
    re-derives the ring, so a co-cause failure cannot manufacture a silent NA
    container either.
    """

    view_id: str
    facts_cavity_id: str
    converter_zone_id: str
    reason: Literal["facts_cavity_has_no_logical_boundary_ring"]
    #: The only licence an exclusion may carry: a by-design sub-threshold drop,
    #: independently provable and by design unlimited.  ⛔ ``registered_ring_loss``
    #: is deliberately absent -- a producer-written loss is fail-loud, never an
    #: exclusion.
    evidence: Literal["below_request_area_threshold"]


class BoundaryBasisAuditV1(_StrictModel):
    passed: bool
    paired_edges: int
    converter_zones: int
    accounted_converter_zones: int
    rows: list[BoundaryBasisRowV1]
    mismatches: list[BoundaryBasisRowV1]
    pairings: list[BoundaryPairingProofV1]
    exclusions: list[BoundaryBasisExclusionV1]
    structural_failures: list[str]

    def assert_consistent(self) -> None:
        if not self.passed:
            raise BoundaryBasisMismatchError(self)


class CompiledZoneEdgeV1(_StrictModel):
    """A projected support-line span.  Only valid rings carry these."""

    component_id: str
    axis: Literal["x", "y"]
    span_lo: int
    span_hi: int
    support_const: int
    basis: Literal["wall_axis", "outer_skin"]
    wall_ids: list[str]
    face_line_handles: list[str]
    evidence: ProjectionEvidenceV1


class CompiledZoneV1(_StrictModel):
    component_id: str
    zone_id: str | None = None
    name: str | None = None
    floor_id: str
    profile: str
    vertices: list[list[int]] | None = None
    edges: list[CompiledZoneEdgeV1] = Field(default_factory=list)
    clear_span_area_units2: int | None = None
    na: list[NaRecordV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ring_is_a_transaction(self):
        if self.vertices is None and self.edges:
            raise ValueError("answer_compiler_na_ring_leaks_projected_edges")
        if self.vertices is not None and len(self.vertices) < 3:
            raise ValueError("answer_compiler_projected_ring_has_fewer_than_three_vertices")
        return self


class CompiledOpeningV1(_StrictModel):
    component_id: str
    opening_id: str
    kind: Literal["window", "door"]
    status: Literal["available", "na"]
    host_wall_ids: list[str] = Field(default_factory=list)
    axis: Literal["x", "y"] | None = None
    along_interval: list[int] | None = None
    cross_interval: list[int] | None = None
    na: list[NaRecordV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _na_opening_has_no_coordinates(self):
        if self.status == "na" and any(
                value is not None
                for value in (self.axis, self.along_interval, self.cross_interval)):
            raise ValueError("answer_compiler_na_opening_leaks_coordinates")
        return self


class MetricResultV1(_StrictModel):
    """Metric requirement declaration plus non-shrinking coverage."""

    metric: str
    profile: str
    status: Literal["available", "partial", "na"]
    required_components: list[str]
    coverage_expected: int
    coverage_available: int
    coverage_na: int
    value: int | float | None = None
    na: list[NaRecordV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coverage_keeps_the_denominator(self):
        if self.coverage_expected < 0 or self.coverage_available < 0 or self.coverage_na < 0:
            raise ValueError("answer_compiler_metric_coverage_negative")
        if self.coverage_available + self.coverage_na != self.coverage_expected:
            raise ValueError("answer_compiler_metric_coverage_shrank_denominator")
        expected_status = (
            "available" if self.coverage_na == 0
            else "na" if self.coverage_available == 0
            else "partial")
        if self.status != expected_status:
            raise ValueError(
                f"answer_compiler_metric_status_disagrees_with_coverage:{self.metric}")
        return self


class CompiledViewV1(_StrictModel):
    view_id: str
    floor_id: str
    profile: str
    zones: list[CompiledZoneV1]
    openings: list[CompiledOpeningV1]
    metrics: list[MetricResultV1]
    counts: dict[str, int]
    na: list[NaRecordV1] = Field(default_factory=list)


class CompiledAnswerV1(_StrictModel):
    case: str
    profile: str
    compiler_version: int
    dependency_closure_version: int
    derivation: dict[str, str | int]
    unresolved_revisions: list[NaRecordV1]
    views: list[CompiledViewV1]


@dataclass(frozen=True)
class _WallGroup:
    axis: Literal["x", "y"]
    face_lo: int
    face_hi: int
    runs: tuple[AsMeasuredWallV1, ...]
    openings: tuple[AsMeasuredOpeningV1, ...]
    handles: frozenset[str]
    component_id: str

    @property
    def key(self) -> tuple[str, int, int]:
        return self.axis, self.face_lo, self.face_hi

    @property
    def wall_ids(self) -> list[str]:
        return sorted(w.id for w in self.runs)

    def coverage(self) -> list[tuple[int, int]]:
        return ([(w.along_min, w.along_max) for w in self.runs]
                + [(o.along_min, o.along_max) for o in self.openings])

    def raw_face(self, side: Literal["lo", "hi"], face_by_id: dict[str, Any]) -> int:
        handles = [handle for wall in self.runs
                   for handle in (wall.face_line_ids_lo if side == "lo"
                                  else wall.face_line_ids_hi)]
        if not handles:
            raise AnswerCompilerInputError(
                f"answer_compiler_wall_group_has_no_{side}_face:{self.component_id}")
        return round(sum(face_by_id[handle].const for handle in handles) / len(handles))


@dataclass
class _Span:
    axis: Literal["x", "y"]
    cavity_const: int
    lo: int
    hi: int
    side: int
    group: _WallGroup | None
    component_id: str
    projected: CompiledZoneEdgeV1 | None = None
    na: list[NaRecordV1] = field(default_factory=list)


def _opaque_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:16]}"


class AnswerCompiler:
    """Pure compiler with an explicit, never-evidence-selected profile."""

    def __init__(self, profile: OutputProfile | str):
        self.profile = OutputProfile(profile)

    def compile(self, as_signed: AsSignedV1, revisions: RevisionsLedgerV1,
                request: TarchConversionRequestV1) -> CompiledAnswerV1:
        self._assert_inputs_belong_together(as_signed, revisions, request)
        unresolved = [self._revision_na(record) for record in revisions.revisions
                      if record.verdict in {"unsigned", "producer_defect"}]
        views = [self._compile_view(view, revisions, request)
                 for view in as_signed.views]
        return CompiledAnswerV1(
            case=as_signed.case,
            profile=self.profile.value,
            compiler_version=ANSWER_COMPILER_VERSION,
            dependency_closure_version=DEPENDENCY_CLOSURE_VERSION,
            derivation={
                "as_measured_content_sha256":
                    as_signed.derivation.as_measured_content_sha256,
                "revisions_content_sha256": revisions_content_sha256(revisions),
                "request_sha256": request.request_sha256,
                "compiler_version": ANSWER_COMPILER_VERSION,
                "dependency_closure_version": DEPENDENCY_CLOSURE_VERSION,
                "profile": self.profile.value,
            },
            unresolved_revisions=unresolved,
            views=views)

    def reproject(self, answer: CompiledAnswerV1, as_signed: AsSignedV1,
                  revisions: RevisionsLedgerV1,
                  request: TarchConversionRequestV1, *,
                  to_profile: OutputProfile | str) -> CompiledAnswerV1:
        """Recover either form from the other form *and the facts layer*.

        The prior answer is checked as the product of the supplied facts, then
        the requested form is freshly compiled.  This avoids a tautological
        inverse that merely subtracts offsets previously emitted by this same
        compiler.
        """
        expected = self.compile(as_signed, revisions, request)
        if expected.model_dump(mode="json") != answer.model_dump(mode="json"):
            raise AnswerCompilerInputError(
                "answer_compiler_reproject_source_does_not_match_supplied_facts")
        return AnswerCompiler(to_profile).compile(as_signed, revisions, request)

    def reading_exam(self, as_signed: AsSignedV1,
                     revisions: RevisionsLedgerV1,
                     request: TarchConversionRequestV1, *,
                     merge_m: float | None = None) -> dict[str, Any]:
        """Derive every plan-view reading question book from frozen facts."""
        from .as_drawn.denominator import denominator_from_facts

        self._assert_inputs_belong_together(as_signed, revisions, request)
        kwargs = {} if merge_m is None else {"merge_m": merge_m}
        exams = {view.view_id: denominator_from_facts(view, request, **kwargs)
                 for view in as_signed.views}
        unresolved = [record.id for record in revisions.revisions
                      if record.verdict in {"unsigned", "producer_defect"}]
        return {
            "compiler_version": ANSWER_COMPILER_VERSION,
            "as_measured_content_sha256":
                as_signed.derivation.as_measured_content_sha256,
            "revisions_content_sha256": revisions_content_sha256(revisions),
            "unresolved_revision_ids": sorted(unresolved),
            "views": exams,
        }

    @staticmethod
    def clear_span_table(answer: CompiledAnswerV1) -> dict[str, Any]:
        """Return usable cavity area per zone; never an output profile."""
        views: dict[str, Any] = {}
        for view in answer.views:
            rows = []
            for zone in view.zones:
                area = zone.clear_span_area_units2
                rows.append({
                    "component_id": zone.component_id,
                    "zone_id": zone.zone_id,
                    "status": "available" if area is not None else "na",
                    "clear_span_area_units2": area,
                    "clear_span_area_m2": (
                        round(area / (UNITS_PER_METRE * UNITS_PER_METRE), 6)
                        if area is not None else None),
                })
            views[view.view_id] = rows
        return {"profile_kind": "derived_clear_span_area", "views": views}

    @staticmethod
    def _assert_inputs_belong_together(
            as_signed: AsSignedV1, revisions: RevisionsLedgerV1,
            request: TarchConversionRequestV1) -> None:
        if as_signed.case != revisions.case or as_signed.case != request.case:
            raise AnswerCompilerInputError(
                "answer_compiler_case_mismatch: "
                f"{as_signed.case!r}/{revisions.case!r}/{request.case!r}")
        if (revisions.as_measured_content_sha256
                != as_signed.derivation.as_measured_content_sha256):
            raise AnswerCompilerInputError(
                "answer_compiler_revisions_target_a_different_as_measured")
        ledger_hash = revisions_content_sha256(revisions)
        if ledger_hash != as_signed.derivation.revisions_content_sha256:
            raise AnswerCompilerInputError(
                "answer_compiler_as_signed_names_a_different_revisions_ledger")
        if request.request_sha256 != as_signed.request_sha256:
            raise AnswerCompilerInputError(
                "answer_compiler_request_does_not_match_as_signed")
        if request.source_dxf_sha256 != as_signed.source_dxf_sha256:
            raise AnswerCompilerInputError(
                "answer_compiler_source_dxf_does_not_match_as_signed")

    @staticmethod
    def _revision_na(record: Any) -> NaRecordV1:
        return NaRecordV1(
            rule=("unsigned_revision" if record.verdict == "unsigned"
                  else "segment_dependency"),
            component_kind="revision",
            component_id=record.id,
            reason_code=("revision_has_no_signed_verdict"
                         if record.verdict == "unsigned"
                         else "signed_producer_defect_has_no_geometry_repair"),
            affected_metrics=["zone_geometry", "reading_face_lines"],
            propagated_from=[record.target.handle])

    def _compile_view(self, view: AsMeasuredViewV1,
                      revisions: RevisionsLedgerV1,
                      request: TarchConversionRequestV1) -> CompiledViewV1:
        plan_view = next((item for item in request.plan_views
                          if item.id == view.view_id), None)
        if plan_view is None:
            raise AnswerCompilerInputError(
                f"answer_compiler_view_not_declared_in_request:{view.view_id}")
        expected = plan_view.zone_intent.expected_count
        view_failure = _view_coordinate_failure(view, plan_view)
        if view_failure is not None:
            zones = [_missing_zone(view, self.profile, index, view_failure)
                     for index in range(expected)]
            openings = [_na_opening(opening, view_failure) for opening in view.openings]
            return _compiled_view_result(
                view, self.profile, zones, openings, expected,
                actual_cavities=0, view_na=[view_failure], coordinate_source_ok=False)

        face_by_id = {face.id: face for face in view.face_lines}
        groups = _build_wall_groups(view)
        footprint, ring_records = _footprint_polygon(view)
        wall_region = _wall_region(view)
        cavities = _cavity_faces(footprint, wall_region, request.min_room_area_m2)
        cavity_ids = {id(cavity): _cavity_id(view.view_id, cavity)
                      for cavity in cavities}

        unresolved_handles = {
            record.target.handle
            for record in revisions.revisions
            if (record.target.view_id == view.view_id
                and record.verdict in {"unsigned", "producer_defect"})
        }
        ordered = sorted(cavities, key=_cavity_sort_key)
        bindable = len(ordered) == expected
        zones: list[CompiledZoneV1] = []
        for index, cavity in enumerate(ordered):
            entry = plan_view.zone_intent.entries[index] if bindable else None
            zones.append(self._compile_cavity(
                view, cavity, cavity_ids[id(cavity)],
                entry.zone_id if entry is not None else None,
                entry.name if entry is not None else None,
                groups, face_by_id, footprint, ring_records, wall_region,
                cavities, cavity_ids, unresolved_handles))

        missing = max(0, expected - len(zones))
        for index in range(missing):
            reason = NaRecordV1(
                rule="metric_requirement", component_kind="zone",
                component_id=_opaque_id("missing-zone", view.view_id, index),
                reason_code="declared_zone_has_no_measured_cavity",
                affected_metrics=["zone_geometry", "clear_span_area"],
                propagated_from=[view.view_id])
            zones.append(_missing_zone(view, self.profile, len(ordered) + index, reason))

        openings = _compile_openings(view)
        view_na: list[NaRecordV1] = []
        if not bindable:
            view_na.append(NaRecordV1(
                rule="metric_requirement", component_kind="zone_binding",
                component_id=_opaque_id("zone-binding", view.view_id),
                reason_code="measured_cavity_count_differs_from_declared_zone_count",
                affected_metrics=["zone_identity", "zone_geometry"],
                propagated_from=[view.view_id]))
        return _compiled_view_result(
            view, self.profile, zones, openings, expected,
            actual_cavities=len(ordered), view_na=view_na,
            coordinate_source_ok=True)

    def _compile_cavity(
            self, view: AsMeasuredViewV1, cavity: Polygon, cavity_id: str,
            zone_id: str | None, name: str | None,
            groups: dict[tuple[str, int, int], _WallGroup],
            face_by_id: dict[str, Any], footprint: Polygon,
            ring_records: list[tuple[str, list[list[int]]]],
            wall_region: Any, cavities: list[Polygon],
            cavity_ids: dict[int, str], unresolved_handles: set[str],
            ) -> CompiledZoneV1:
        ring = [(int(round(x)), int(round(y)))
                for x, y in list(cavity.exterior.coords)[:-1]]
        rep = cavity.representative_point()
        spans: list[_Span] = []
        loose_handles = set(
            view.converter_readouts.face_lines_not_paired_into_a_wall)
        loose_incident = []
        for handle in sorted(loose_handles):
            face = face_by_id.get(handle)
            if face is None:
                continue
            if face.axis == "x":
                line = LineString([
                    (face.along_min, face.const), (face.along_max, face.const)])
            else:
                line = LineString([
                    (face.const, face.along_min), (face.const, face.along_max)])
            if cavity.intersects(line):
                loose_incident.append(handle)
        for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            component_id = _opaque_id("segment", cavity_id, index)
            if a[0] == b[0] and a[1] != b[1]:
                axis: Literal["x", "y"] = "y"
                const, lo, hi = a[0], min(a[1], b[1]), max(a[1], b[1])
                side = -1 if rep.x < const else 1
            elif a[1] == b[1] and a[0] != b[0]:
                axis = "x"
                const, lo, hi = a[1], min(a[0], b[0]), max(a[0], b[0])
                side = -1 if rep.y < const else 1
            else:
                span = _Span("x", 0, 0, 1, 1, None, component_id)
                span.na.append(NaRecordV1(
                    rule="junction_dependency", component_kind="junction",
                    component_id=_opaque_id("junction", cavity_id, index),
                    reason_code="cavity_ring_edge_is_not_axis_aligned",
                    affected_metrics=["zone_geometry"],
                    propagated_from=[component_id]))
                spans.append(span)
                continue
            owners = _owning_groups(groups, axis, const, lo, hi)
            group = owners[0] if len(owners) == 1 else None
            span = _Span(axis, const, lo, hi, side, group, component_id)
            if not owners:
                span.na.append(NaRecordV1(
                    rule="segment_dependency", component_kind="wall_segment",
                    component_id=component_id,
                    reason_code="cavity_edge_has_no_wall_face_pair",
                    affected_metrics=["zone_geometry"],
                    propagated_from=[cavity_id]))
            elif len(owners) > 1:
                span.na.append(NaRecordV1(
                    rule="junction_dependency", component_kind="wall_segment",
                    component_id=component_id,
                    reason_code="cavity_edge_has_multiple_wall_owners",
                    affected_metrics=["zone_geometry"],
                    propagated_from=sorted(owner.component_id for owner in owners)))
            elif group is not None:
                unresolved = sorted(group.handles & unresolved_handles)
                if unresolved:
                    span.na.append(NaRecordV1(
                        rule="unsigned_revision", component_kind="wall_segment",
                        component_id=group.component_id,
                        reason_code="wall_depends_on_unresolved_revision",
                        affected_metrics=["zone_geometry", "clear_span_area"],
                        propagated_from=unresolved))
                else:
                    span.projected, projection_na = self._project_span(
                        view, cavity_id, span, group, face_by_id, footprint,
                        ring_records, wall_region, cavities, cavity_ids)
                    span.na.extend(projection_na)
            spans.append(span)

        ring_na = [record for span in spans for record in span.na]
        if loose_incident:
            ring_na.append(NaRecordV1(
                rule="segment_dependency", component_kind="wall_segment",
                component_id=_opaque_id("unpaired-segment", *loose_incident),
                reason_code="unpaired_face_line_intersects_the_zone_component",
                affected_metrics=["zone_geometry"],
                propagated_from=loose_incident))
        if ring_na or any(span.projected is None for span in spans):
            return CompiledZoneV1(
                component_id=cavity_id, zone_id=zone_id, name=name,
                floor_id=view.floor_id, profile=self.profile.value,
                vertices=None, edges=[],
                clear_span_area_units2=int(round(cavity.area)),
                na=_ring_closure(cavity_id, ring_na))

        edges = _merge_projected_spans([span.projected for span in spans
                                        if span.projected is not None])
        vertices, junction_na = _support_vertices(edges, cavity_id)
        if junction_na:
            return CompiledZoneV1(
                component_id=cavity_id, zone_id=zone_id, name=name,
                floor_id=view.floor_id, profile=self.profile.value,
                vertices=None, edges=[],
                clear_span_area_units2=int(round(cavity.area)),
                na=_ring_closure(cavity_id, junction_na))
        return CompiledZoneV1(
            component_id=cavity_id, zone_id=zone_id, name=name,
            floor_id=view.floor_id, profile=self.profile.value,
            vertices=[[x, y] for x, y in vertices], edges=edges,
            clear_span_area_units2=int(round(cavity.area)), na=[])

    def _project_span(
            self, view: AsMeasuredViewV1, cavity_id: str,
            span: _Span, group: _WallGroup,
            face_by_id: dict[str, Any], footprint: Polygon,
            ring_records: list[tuple[str, list[list[int]]]], wall_region: Any,
            cavities: list[Polygon], cavity_ids: dict[int, str],
            ) -> tuple[CompiledZoneEdgeV1 | None, list[NaRecordV1]]:
        near_side: Literal["lo", "hi"] = "lo" if span.side < 0 else "hi"
        far_side: Literal["lo", "hi"] = "hi" if near_side == "lo" else "lo"
        raw_near = group.raw_face(near_side, face_by_id)
        raw_far = group.raw_face(far_side, face_by_id)
        thickness = abs(raw_far - raw_near)
        if thickness <= 0:
            return None, [NaRecordV1(
                rule="segment_dependency", component_kind="wall_segment",
                component_id=group.component_id,
                reason_code="wall_thickness_is_not_positive",
                affected_metrics=["zone_geometry"],
                propagated_from=group.wall_ids)]

        outward = -span.side
        condition, raw_evidence = _classify_boundary(
            span, group, raw_near, raw_far, outward, footprint,
            ring_records, wall_region, cavities, cavity_ids)
        stored = _stored_boundary_for_span(view, cavity_id, span, group)
        if stored is not None:
            recomputed = condition or "unknown"
            if stored.boundary_condition != recomputed:
                raise BoundaryConditionMismatchError([{
                    "view_id": view.view_id,
                    "cavity_id": cavity_id,
                    "span_component_id": span.component_id,
                    "facts_edge_id": stored.id,
                    "stored": stored.boundary_condition,
                    "recomputed": recomputed,
                }])
            # The stored value is the compiler input.  The independently
            # recomputed value above remains the second column and must agree.
            condition = stored.boundary_condition
            raw_evidence["facts_boundary_edge_id"] = stored.id
        if condition not in {"exterior", "interzone"}:
            if self.profile is OutputProfile.FORM_B_EXTERIOR_SKIN:
                return None, [NaRecordV1(
                    rule="profile_dependency", component_kind="wall_segment",
                    component_id=group.component_id,
                    reason_code=f"boundary_condition_{condition or 'unknown'}",
                    affected_metrics=["zone_geometry:form_b_exterior_skin"],
                    propagated_from=[span.component_id])]
            # Form A needs the wall baseline and thickness, not boundary identity.
            raw_evidence = {
                **raw_evidence,
                "boundary_condition": condition or "unknown",
            }

        if (self.profile is OutputProfile.FORM_A_AXIS
                or condition == "interzone"):
            if thickness % 2:
                return None, [NaRecordV1(
                    rule="segment_dependency", component_kind="wall_segment",
                    component_id=group.component_id,
                    reason_code="wall_axis_falls_between_storage_units",
                    affected_metrics=[f"zone_geometry:{self.profile.value}"],
                    propagated_from=[span.component_id])]
            support = raw_near + outward * (thickness // 2)
            basis: Literal["wall_axis", "outer_skin"] = "wall_axis"
        else:
            support = raw_near + outward * thickness
            basis = "outer_skin"

        evidence = ProjectionEvidenceV1.model_validate(raw_evidence)
        return CompiledZoneEdgeV1(
            component_id=span.component_id, axis=span.axis,
            span_lo=span.lo, span_hi=span.hi, support_const=support,
            basis=basis, wall_ids=group.wall_ids,
            face_line_handles=sorted(group.handles), evidence=evidence), []


def _stored_boundary_for_span(
        view: AsMeasuredViewV1, cavity_id: str, span: _Span,
        group: _WallGroup) -> AsMeasuredBoundaryEdgeV1 | None:
    """Pair a raw compiler span to its projection-free logical facts edge."""
    candidates = [edge for edge in view.boundary_edges
                  if (edge.cavity_id == cavity_id
                      and edge.axis == span.axis
                      and edge.cavity_const == span.cavity_const
                      and edge.side == span.side
                      and edge.span_lo <= span.lo
                      and edge.span_hi >= span.hi
                      and edge.wall_ids == group.wall_ids)]
    if len(candidates) > 1:
        raise AnswerCompilerInputError(
            "answer_compiler_boundary_span_has_multiple_fact_matches:"
            f"{view.view_id}:{cavity_id}:{span.component_id}:"
            f"{sorted(edge.id for edge in candidates)}")
    return candidates[0] if candidates else None


def _view_coordinate_failure(view: AsMeasuredViewV1, plan_view: Any) -> NaRecordV1 | None:
    affine = plan_view.world_from_source_m
    bad_affine = (affine.m01 != 0.0 or affine.m10 != 0.0
                  or affine.m00 == 0.0 or affine.m11 == 0.0)
    calibration_blocks = sorted({
        str(diag.get("code")) for diag in view.converter_readouts.diagnostics
        if (diag.get("severity") == "BLOCK"
            and ("affine" in str(diag.get("code", "")).lower()
                 or "calibr" in str(diag.get("code", "")).lower()))
    })
    if not bad_affine and not calibration_blocks:
        return None
    return NaRecordV1(
        rule="view_coordinate_source", component_kind="view",
        component_id=view.view_id,
        reason_code=("plan_affine_is_not_axis_preserving_and_invertible"
                     if bad_affine else "view_calibration_blocked"),
        affected_metrics=["zone_geometry", "clear_span_area", "opening_geometry"],
        propagated_from=calibration_blocks)


def _build_wall_groups(view: AsMeasuredViewV1) -> dict[tuple[str, int, int], _WallGroup]:
    grouped: dict[tuple[str, int, int], list[AsMeasuredWallV1]] = {}
    for wall in view.walls:
        grouped.setdefault((wall.axis, wall.face_lo, wall.face_hi), []).append(wall)
    by_wall_id = {wall.id: wall for wall in view.walls}
    opening_groups: dict[tuple[str, int, int], list[AsMeasuredOpeningV1]] = {}
    for opening in view.openings:
        keys = {
            (by_wall_id[wall_id].axis, by_wall_id[wall_id].face_lo,
             by_wall_id[wall_id].face_hi)
            for wall_id in opening.carrier_wall_ids if wall_id in by_wall_id}
        # Topology does not depend on the opening host judgement: an opening
        # rectangle whose cross-section equals one existing wall group bridges
        # that group's runs even when the opening metric calls its host
        # ambiguous.  This is B6 rule 4's local blast radius.
        geometric_key = (opening.axis, opening.cross_lo, opening.cross_hi)
        if geometric_key in grouped:
            keys.add(geometric_key)
        for key in keys:
            opening_groups.setdefault(key, []).append(opening)

    result: dict[tuple[str, int, int], _WallGroup] = {}
    for key, runs in grouped.items():
        ordered_runs = tuple(sorted(
            runs, key=lambda wall: (wall.along_min, wall.along_max, wall.id)))
        openings = tuple(sorted(
            {opening.id: opening for opening in opening_groups.get(key, [])}.values(),
            key=lambda opening: (opening.along_min, opening.along_max, opening.id)))
        handles = frozenset(
            handle for wall in ordered_runs
            for handle in (*wall.face_line_ids_lo, *wall.face_line_ids_hi))
        result[key] = _WallGroup(
            axis=key[0], face_lo=key[1], face_hi=key[2],
            runs=ordered_runs, openings=openings, handles=handles,
            component_id=_opaque_id("wall-group", view.view_id, *key,
                                    *sorted(wall.id for wall in ordered_runs)))
    return result


def _band_rectangle(axis: str, face_lo: int, face_hi: int,
                    along_lo: int, along_hi: int) -> Polygon:
    if axis == "x":
        return Polygon([(along_lo, face_lo), (along_hi, face_lo),
                        (along_hi, face_hi), (along_lo, face_hi)])
    return Polygon([(face_lo, along_lo), (face_lo, along_hi),
                    (face_hi, along_hi), (face_hi, along_lo)])


def _wall_region(view: AsMeasuredViewV1) -> Any:
    rectangles = [_band_rectangle(
        wall.axis, wall.face_lo, wall.face_hi, wall.along_min, wall.along_max)
        for wall in view.walls]
    rectangles.extend(_band_rectangle(
        opening.axis, opening.cross_lo, opening.cross_hi,
        opening.along_min, opening.along_max) for opening in view.openings)
    return unary_union(rectangles) if rectangles else Polygon()


def _footprint_polygon(
        view: AsMeasuredViewV1) -> tuple[Polygon, list[tuple[str, list[list[int]]]]]:
    exterior = [ring for ring in view.footprint.rings if ring.kind == "exterior"]
    if len(exterior) != 1:
        raise AnswerCompilerInputError(
            f"answer_compiler_requires_one_exterior_ring:{view.view_id}:{len(exterior)}")
    ext = exterior[0]
    holes = [ring.points for ring in view.footprint.rings
             if ring.kind == "interior" and ring.polygon_index == ext.polygon_index]
    polygon = Polygon(ext.points, holes=holes)
    if polygon.is_empty or not polygon.is_valid:
        raise AnswerCompilerInputError(
            f"answer_compiler_footprint_is_not_a_valid_polygon:{view.view_id}")
    records = [(f"footprint:{view.view_id}:ring:{ext.polygon_index}", ext.points)]
    return polygon, records


def _cavity_faces(footprint: Polygon, wall_region: Any,
                  min_room_area_m2: float) -> list[Polygon]:
    geometry = footprint.difference(wall_region)
    parts = list(getattr(geometry, "geoms", [geometry]))
    threshold = float(min_room_area_m2) * UNITS_PER_METRE * UNITS_PER_METRE
    return [part for part in parts
            if part.geom_type == "Polygon" and not part.is_empty and part.area > threshold]


def _cavity_sort_key(cavity: Polygon) -> tuple[float, float, float, float]:
    return tuple(round(value, 6) for value in cavity.bounds)


def _cavity_id(view_id: str, cavity: Polygon) -> str:
    return _opaque_id("cavity", view_id, *_cavity_sort_key(cavity), round(cavity.area, 3))


def _owning_groups(groups: dict[tuple[str, int, int], _WallGroup],
                   axis: str, const: int, lo: int, hi: int) -> list[_WallGroup]:
    found = []
    for group in groups.values():
        if group.axis != axis or const not in (group.face_lo, group.face_hi):
            continue
        if any(min(hi, c_hi) - max(lo, c_lo) > 0
               for c_lo, c_hi in group.coverage()):
            found.append(group)
    return sorted(found, key=lambda group: group.component_id)


def _classify_boundary(
        span: _Span, group: _WallGroup, raw_near: int, raw_far: int,
        outward: int, footprint: Polygon,
        ring_records: list[tuple[str, list[list[int]]]], wall_region: Any,
        cavities: list[Polygon], cavity_ids: dict[int, str],
        ) -> tuple[str | None, dict[str, Any]]:
    """Recompute exterior/interzone identity from facts geometry only."""
    mid_along = (span.lo + span.hi) // 2
    stored_far = group.face_hi if outward > 0 else group.face_lo
    farthest = max(raw_far, stored_far) if outward > 0 else min(raw_far, stored_far)
    exit_const = farthest + outward
    exit_point = ([exit_const, mid_along] if span.axis == "y"
                  else [mid_along, exit_const])
    point = Point(exit_point)
    outside = not footprint.covers(point) and not wall_region.covers(point)

    footprint_edge_id = None
    footprint_edge_points = None
    footprint_ring_id = ring_records[0][0]
    if outside:
        witness = _footprint_ray_witness(
            span.axis, raw_near, exit_const, mid_along, ring_records)
        if witness is None:
            condition: str | None = None
        else:
            footprint_ring_id, footprint_edge_id, footprint_edge_points = witness
            condition = "exterior"
        adjacent = None
    else:
        adjacent_cavities = [cavity for cavity in cavities if cavity.covers(point)]
        if len(adjacent_cavities) == 1:
            condition = "interzone"
            adjacent = cavity_ids[id(adjacent_cavities[0])]
        elif footprint.covers(point) and not wall_region.covers(point):
            condition = "unclaimed_void"
            adjacent = None
        else:
            condition = None
            adjacent = None
    near_side: Literal["lo", "hi"] = "lo" if span.side < 0 else "hi"
    far_side: Literal["lo", "hi"] = "hi" if near_side == "lo" else "lo"
    near_handles = sorted({handle for wall in group.runs
                           for handle in (wall.face_line_ids_lo
                                          if near_side == "lo"
                                          else wall.face_line_ids_hi)})
    far_handles = sorted({handle for wall in group.runs
                          for handle in (wall.face_line_ids_lo
                                         if far_side == "lo"
                                         else wall.face_line_ids_hi)})
    evidence = {
        "boundary_condition": condition or "unknown",
        "raw_face_const": raw_near,
        "opposite_face_const": raw_far,
        "thickness_units": abs(raw_far - raw_near),
        "outward_normal": ([outward, 0] if span.axis == "y" else [0, outward]),
        "exit_point": exit_point,
        "footprint_ring_id": footprint_ring_id,
        "footprint_edge_id": footprint_edge_id,
        "footprint_edge_points": footprint_edge_points,
        "adjacent_cavity_id": adjacent,
        "cavity_side_face_line_ids": near_handles,
        "far_side_face_line_ids": far_handles,
    }
    return condition, evidence


def _footprint_ray_witness(
        axis: str, start_const: int, exit_const: int, mid_along: int,
        ring_records: list[tuple[str, list[list[int]]]],
        ) -> tuple[str, str, list[list[int]]] | None:
    low, high = sorted((start_const, exit_const))
    for ring_id, points in ring_records:
        ring = points[:-1] if len(points) > 1 and points[0] == points[-1] else points
        for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            if axis == "y" and a[0] == b[0]:
                const, along_lo, along_hi = a[0], min(a[1], b[1]), max(a[1], b[1])
            elif axis == "x" and a[1] == b[1]:
                const, along_lo, along_hi = a[1], min(a[0], b[0]), max(a[0], b[0])
            else:
                continue
            if low <= const <= high and along_lo <= mid_along <= along_hi:
                return ring_id, f"{ring_id}:edge:{index}", [list(a), list(b)]
    return None


def _world_point_to_units(point: list[float]) -> tuple[int, int]:
    return (round(float(point[0]) * UNITS_PER_METRE),
            round(float(point[1]) * UNITS_PER_METRE))


def _world_point_to_ingest_grid(point: list[float]) -> tuple[int, int]:
    """⭐ A-11 (user 2026-09-05, 「走乙」): converter-side geometry enters the
    basis reconciliation on the SAME 1 mm ingest grid the facts layer snaps
    to (:func:`src.agent.judge.as_measured._geom_units`).  Without this, the
    zero-threshold identity ``projected facts ring == converter zone`` would
    be polluted by a ±0.1 mm representation band (MEASURED on sm25: two
    zones showed a symmetric difference of exactly 157600 units² -- a
    0.1 mm strip around a ~16 m perimeter -- that vanished the moment both
    sides were on the grid), and a real geometric difference could no longer
    be told apart from storage noise.  ⛔ This does NOT change what the
    converter measured -- it changes which grid the RECONCILIATION compares
    on, so the zero threshold keeps measuring geometry, not representation.
    """
    x, y = _world_point_to_units(point)
    return (snap_to_ingest_resolution(x), snap_to_ingest_resolution(y))


def _undirected_segment_residual(
        facts_edge: AsMeasuredBoundaryEdgeV1,
        converter_points: tuple[tuple[int, int], tuple[int, int]]) -> float:
    fp1, fp2 = tuple(facts_edge.p1), tuple(facts_edge.p2)
    cp1, cp2 = converter_points

    def distance(a: tuple[int, int], b: tuple[int, int]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    direct = max(distance(fp1, cp1), distance(fp2, cp2))
    reversed_ = max(distance(fp1, cp2), distance(fp2, cp1))
    return min(direct, reversed_)


#: The answer side's own per-edge offset rule, mirrored (⛔ not re-invented):
#: ``tarch_normalize._offset_for`` expands an ``outer_skin`` edge by the full
#: measured thickness and a ``wall_axis`` edge by half of it.  The ``// 2`` is
#: EXACT here because odd interzone thickness is declined loudly upstream (see
#: ``_projected_facts_ring``), exactly as the compiler's wall-axis path does at
#: ``_the_compiler`` (``wall_axis_falls_between_storage_units``); mirroring the
#: producer's definition means mirroring that storage-unit discipline, ⛔ not
#: the raw ``/ 2.0`` float helper (F-156 v3 阻断 2).
_PROJECTED_BASIS_OFFSET_UNITS = {
    "exterior": lambda thickness: thickness,
    "interzone": lambda thickness: thickness // 2,
}


def _projected_facts_ring(
        facts_edges: list[AsMeasuredBoundaryEdgeV1],
        ) -> tuple[Polygon | None, str | None]:
    """Carry the stored facts ring out to the basis the answer declares.

    ⛔ The facts ring is CLEAR SPAN and a converter zone is outer-skin /
    wall-axis.  Comparing those two directly measures the basis gap, not
    correctness -- every healthy sm25 room differs by 1.0-3.5 m² that way, so
    no threshold can separate a right answer from a wrong one and any threshold
    that appears to would have been read off the current data.  So each stored
    edge is walked out along its OWN measured evidence to the basis its
    ``boundary_condition`` declares, and only then compared.  The comparison
    that follows needs no threshold at all.

    Consecutive edges that propagate one support line collapse first (a support
    line may carry more than one record because the classifier declines to
    judge junction fragments); the ring is then the intersections of adjacent
    projected support lines, exactly as ``_support_vertices`` builds a compiled
    zone ring.
    """
    supports: list[tuple[str, int]] = []
    for edge in facts_edges:
        offset = _PROJECTED_BASIS_OFFSET_UNITS.get(edge.boundary_condition)
        if offset is None:
            return None, ("boundary_condition_has_no_declared_basis:"
                          f"{edge.boundary_condition}")
        thickness = edge.evidence.thickness_units
        if thickness <= 0:
            return None, "measured_thickness_is_not_positive"
        # ⭐ F-156 v3 阻断 2: mirror the producer's storage-unit definition, ⛔ not
        # its ``/ 2.0`` float helper.  An odd interzone thickness puts the wall
        # axis at a half-unit -- BETWEEN storage units -- so no integer support
        # line can be recomputed and the zero-threshold identity below cannot
        # run.  Decline loudly with the SAME code the compiler's own wall-axis
        # path raises (``_the_compiler``), rather than silently truncating (the
        # old ``// 2``, off by half a unit) or projecting a fractional axis the
        # integer-rounded converter zone could never equal (a false red).
        if edge.boundary_condition == "interzone" and thickness % 2:
            return None, "wall_axis_falls_between_storage_units"
        outward = (edge.evidence.outward_normal[0] if edge.axis == "y"
                   else edge.evidence.outward_normal[1])
        support = (edge.axis,
                   edge.evidence.raw_face_const + outward * offset(thickness))
        if not supports or supports[-1] != support:
            supports.append(support)
    if len(supports) > 1 and supports[0] == supports[-1]:
        supports.pop()
    if len(supports) < 3:
        return None, "fewer_than_three_projected_support_lines"
    vertices: list[tuple[int, int]] = []
    for index, support in enumerate(supports):
        previous = supports[index - 1]
        if previous[0] == support[0]:
            return None, "adjacent_projected_support_lines_are_parallel"
        vertices.append((previous[1], support[1]) if previous[0] == "y"
                        else (support[1], previous[1]))
    polygon = Polygon(vertices)
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        return None, "projected_support_ring_is_invalid"
    return polygon, None


def reconcile_boundary_basis(
        as_signed: AsSignedV1,
        conversion_report: ConversionReportV1,
        *,
        min_room_area_m2: float | None = None) -> BoundaryBasisAuditV1:
    """Pair and compare facts ``boundary_condition`` with converter ``basis``.

    Pairing does not consult the facts classification value.  It independently
    chooses the lowest-residual ring direction/rotation and the highest source-
    ancestry overlap, then requires both choices to identify the same edge map.
    A changed classification therefore reddens exactly its row instead of
    perturbing the pairing radius.

    ②-1d rework3: a converter zone whose facts cavity holds no stored ring is
    accounted for by WHO authored the licence:

    * ``registered_ring_loss`` (``view.boundary_ring_losses``) is
      PRODUCER-authored.  The hash covering the ledger only stops a third party
      tampering with it; it does not stop the producer itself writing a loss for
      every cavity it failed to ring.  So a registered loss can no longer
      silently waive a converter zone -- it is FAIL-LOUD, a named structural
      failure carrying the cavity and the loss's own fingerprint (reason +
      area).  On real sm25 the sole surviving entry is F-153 form B (a wall
      coordinate 0.1 mm off its three siblings that keeps a fully enclosed real
      room from ringing); reddening it is CORRECT, and the red clears itself the
      moment that upstream defect is fixed -- the producer then emits a ring and
      writes no loss, so no code fires and ⛔ no cavity id is baked in
      ([[gate-measures-right-but-carrier-gets-swapped]]: the '哪个方向没有锁'
      answer here is '加了就会红', which is the defect itself blocking the lock).
      ⭐ rework4 T1: the ledger is swept EXHAUSTIVELY from the ledger side, ⛔ not
      only while walking converter zones -- a producer loss on a cavity that NO
      zone consumes is equally fail-loud
      (``producer_ring_loss_unrepresented_by_any_converter_zone``).  The ledger,
      ⛔ not the converter-zone population, is the traversal start.
    * ``below_request_area_threshold`` (when ``min_room_area_m2`` is the
      production threshold) is INDEPENDENTLY provable: the gate recomputes the
      raw cavity area itself and the threshold comes from the request (a
      different author).  A producer cannot forge it, so it silently licenses an
      exclusion and ⛔ never counts toward any flood quota -- honest sub-threshold
      drops, no matter how many, do not redden (rework3 blocker 1).

    Anything else (no ring, no loss, above threshold) is a silent gap and
    reddens.  The gate never re-derives the ring itself, so the exclusion
    decision is not co-caused with the producer.  ``min_room_area_m2`` is
    optional and defaults to ``None`` (no below-threshold amnesty), which is the
    aligned replacement for the old ``derive(0.0)`` re-derivation that raised
    false ``ring_missing`` alarms on below-threshold cavities dropped by design
    (N3').

    The old F-156 v4 per-view ``excluded > paired`` aggregate quota is GONE: the
    flood ('灌证') direction is now caught per-loss (every producer-written loss
    is its own fail-loud red), so there is no aggregate cut left to tune, and an
    aggregate cut would false-red an honest building with many sub-threshold
    shafts (rework3 blocker 1).
    """
    area_threshold_units2 = (
        None if min_room_area_m2 is None
        else float(min_room_area_m2) * UNITS_PER_METRE * UNITS_PER_METRE)
    converter_by_floor: dict[str, list[Any]] = {}
    for zone in conversion_report.zones:
        converter_by_floor.setdefault(zone.floor_id, []).append(zone)

    rows: list[BoundaryBasisRowV1] = []
    pairings: list[BoundaryPairingProofV1] = []
    exclusions: list[BoundaryBasisExclusionV1] = []
    structural: list[str] = []
    accounted_converter_zones: set[tuple[str, str]] = set()
    claimed_converter_zones: dict[tuple[str, str], tuple[str, str]] = {}
    expected_basis = {
        "exterior": "outer_skin",
        "interzone": "wall_axis",
    }

    if not conversion_report.zones:
        structural.append("converter_zones_empty")
    for floor_id, zones in sorted(converter_by_floor.items()):
        counts: dict[str, int] = {}
        for zone in zones:
            counts[zone.zone_id] = counts.get(zone.zone_id, 0) + 1
        for zone_id, count in sorted(counts.items()):
            if count != 1:
                structural.append(
                    f"converter_zone_identity_not_unique:{floor_id}:{zone_id}:"
                    f"count={count}")

    for view in as_signed.views:
        if not view.boundary_edges:
            structural.append(f"facts_boundary_edges_empty:{view.view_id}")
        by_cavity: dict[str, list[AsMeasuredBoundaryEdgeV1]] = {}
        for edge in view.boundary_edges:
            by_cavity.setdefault(edge.cavity_id, []).append(edge)

        # Account for the complete converter-zone population before doing the
        # edge-level comparison.  A raw facts cavity may genuinely have no
        # logical ring.  ⛔ The producer-written ``boundary_ring_losses`` ledger is
        # NO LONGER a licence: every entry is FAIL-LOUD -- the forward pass below
        # names the ones a converter zone consumes, and the reverse sweep above
        # names every remaining ledger entry, so no producer loss silences a zone.
        # The ONLY silent exit is a below-threshold by-design drop, whose area the
        # gate recomputes against a different author's request threshold -- ⛔ never
        # the producer re-deriving its own ring, whose co-cause failure would
        # silently absorb real rooms and hallucinated zones alike
        # ([[gate-measures-right-but-carrier-gets-swapped]]).
        try:
            footprint, _ring_records = _footprint_polygon(view)
        except AnswerCompilerInputError as exc:
            structural.append(
                f"facts_boundary_footprint_unusable:{view.view_id}:{exc}")
            raw_cavities: list[Polygon] = []
        else:
            geometry = footprint.difference(_wall_region(view))
            raw_cavities = sorted([
                part for part in getattr(geometry, "geoms", [geometry])
                if (part.geom_type == "Polygon" and not part.is_empty
                    and part.area > 0)
            ], key=_cavity_sort_key)

        raw_by_id = {
            _cavity_id(view.view_id, cavity): cavity for cavity in raw_cavities}
        # ②-1d rework2: the independent exclusion anchor -- consumed, not derived.
        loss_by_id = {loss.cavity_id: loss for loss in view.boundary_ring_losses}
        # ⭐ rework4 T1 (阻断 2): the cavities whose producer loss the FORWARD zone
        # pass below observes and names.  Everything the ledger holds that this set
        # does not cover is swept fail-loud FROM THE LEDGER SIDE (reverse sweep at
        # the end of this view body).
        losses_reddened_via_zone: set[str] = set()
        excluded_zone_polys: dict[str, list[tuple[str, Polygon]]] = {}
        for zone in sorted(
                converter_by_floor.get(view.floor_id, []),
                key=lambda item: item.zone_id):
            zone_polygon = Polygon([
                _world_point_to_ingest_grid(point)
                for point in zone.polygon_m.exterior.vertices])
            if (zone_polygon.is_empty or not zone_polygon.is_valid
                    or zone_polygon.area <= 0):
                structural.append(
                    f"converter_zone_polygon_invalid:{view.view_id}:{zone.zone_id}")
                continue
            representative = zone_polygon.representative_point()
            cavity_matches = [
                cavity_id for cavity_id, cavity in raw_by_id.items()
                if cavity.covers(representative)]
            if len(cavity_matches) != 1:
                structural.append(
                    f"converter_zone_facts_cavity_pairing_not_unique:"
                    f"{view.view_id}:{zone.zone_id}:{sorted(cavity_matches)}")
                continue
            cavity_id = cavity_matches[0]
            zone_key = (zone.floor_id, zone.zone_id)
            if cavity_id in by_cavity:
                # A stored ring exists -> the zone is edge-paired by the reverse
                # pass below (which also enforces zone/cavity uniqueness).
                accounted_converter_zones.add(zone_key)
                continue
            # No stored ring.  How the zone is accounted for depends on WHO
            # authored the licence (see the function docstring):
            #
            # * a PRODUCER-written ``registered_ring_loss`` is FAIL-LOUD -- a
            #   named structural failure carrying the cavity and the loss's own
            #   fingerprint (reason + area).  ⛔ It may not silently waive the
            #   zone: the hash only stops a third party, not the producer itself,
            #   from writing a loss for every cavity it failed to ring.  On real
            #   sm25 the sole entry is F-153 form B and reddening it is correct;
            #   ⛔ no cavity id is baked in, so the red clears itself when that
            #   upstream defect is fixed and the producer stops writing the loss.
            # * a ``below_request_area_threshold`` drop is INDEPENDENTLY provable
            #   and silently licenses an exclusion (handled below).
            loss = loss_by_id.get(cavity_id)
            if loss is not None:
                structural.append(
                    f"converter_zone_excluded_by_producer_written_ring_loss:"
                    f"{view.view_id}:{cavity_id}:{zone.zone_id}:"
                    f"reason={loss.reason}:area_units2={loss.area_units2}")
                losses_reddened_via_zone.add(cavity_id)
            elif (area_threshold_units2 is not None
                    and raw_by_id[cavity_id].area <= area_threshold_units2):
                exclusions.append(BoundaryBasisExclusionV1(
                    view_id=view.view_id, facts_cavity_id=cavity_id,
                    converter_zone_id=zone.zone_id,
                    reason="facts_cavity_has_no_logical_boundary_ring",
                    evidence="below_request_area_threshold"))
                excluded_zone_polys.setdefault(cavity_id, []).append(
                    (zone.zone_id, zone_polygon))
            else:
                structural.append(
                    f"facts_boundary_ring_missing:{view.view_id}:{cavity_id}:"
                    f"converter={zone.zone_id}")
            accounted_converter_zones.add(zone_key)

        # ②-1d rework2 uniqueness: a single NA cavity may legitimately host more
        # than one real room (adjacent rooms the under-segmented raw cavity did
        # not split -- sm25 z4/z5 share one cavity, disjoint interiors), but two
        # claimed zones ⛔ cannot occupy the SAME space.  A phantom parked on top
        # of a real excluded zone reddens here.
        for shared_cavity_id, members in sorted(excluded_zone_polys.items()):
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    if members[i][1].relate_pattern(members[j][1], "T********"):
                        structural.append(
                            "converter_zones_overlap_in_shared_exclusion_cavity:"
                            f"{view.view_id}:{shared_cavity_id}:"
                            f"{members[i][0]}:{members[j][0]}")

        # ⭐⭐⭐ rework4 T1 (阻断 2): OUTPUT-side exhaustive sweep.  The forward zone
        # pass above only observes a loss when a converter zone happens to land on
        # its ringless cavity, so a producer loss on a cavity NO zone consumes was
        # completely silent -- the ledger was never the traversal START, only the
        # zone was ([[gate-measures-right-but-carrier-gets-swapped]]: the swapped
        # carrier here is the traversal start).  The producer-authored ledger is
        # the authority on what failed to ring, so it must be swept in full: every
        # ``boundary_ring_losses`` entry the forward pass did not already name is
        # a fail-loud red carrying its OWN cavity + reason + area fingerprint, even
        # with no converter zone and no stored ring.  ⛔ No cavity id is baked in;
        # the red clears itself the moment the producer stops writing the loss.
        for loss in view.boundary_ring_losses:
            if loss.cavity_id in losses_reddened_via_zone:
                continue
            structural.append(
                f"producer_ring_loss_unrepresented_by_any_converter_zone:"
                f"{view.view_id}:{loss.cavity_id}:"
                f"reason={loss.reason}:area_units2={loss.area_units2}")

        for cavity_id, unordered in sorted(by_cavity.items()):
            facts_edges = sorted(unordered, key=lambda edge: edge.sequence)
            if [edge.sequence for edge in facts_edges] != list(range(len(facts_edges))):
                structural.append(
                    f"facts_boundary_sequence_not_contiguous:{view.view_id}:{cavity_id}")
                continue
            facts_polygon = Polygon([edge.p1 for edge in facts_edges])
            if (facts_polygon.is_empty or not facts_polygon.is_valid
                    or facts_polygon.area <= 0):
                structural.append(
                    f"facts_boundary_ring_invalid:{view.view_id}:{cavity_id}")
                continue
            representative = facts_polygon.representative_point()
            zone_polygons = [
                (zone, Polygon([_world_point_to_ingest_grid(point)
                                for point in zone.polygon_m.exterior.vertices]))
                for zone in converter_by_floor.get(view.floor_id, [])]
            # ⭐ A cavity that spans MORE than one answer zone is not a room
            # this layer may pair: whichever zone happens to contain the
            # representative point would silently absorb the other one.  ⛔ The
            # point test alone cannot see that -- it always names exactly one.
            occupied = sorted(
                zone.zone_id for zone, polygon in zone_polygons
                if polygon.intersection(facts_polygon).area > 0)
            if len(occupied) != 1:
                structural.append(
                    f"facts_cavity_occupies_multiple_converter_zones:"
                    f"{view.view_id}:{cavity_id}:{occupied}")
                continue
            zone_matches = [zone for zone, polygon in zone_polygons
                            if polygon.covers(representative)]
            if len(zone_matches) != 1:
                structural.append(
                    f"converter_zone_pairing_not_unique:{view.view_id}:{cavity_id}:"
                    f"{[zone.zone_id for zone in zone_matches]}")
                continue
            zone = zone_matches[0]
            zone_polygon = next(polygon for candidate, polygon in zone_polygons
                                if candidate is zone)
            zone_key = (zone.floor_id, zone.zone_id)
            accounted_converter_zones.add(zone_key)
            previous = claimed_converter_zones.get(zone_key)
            if previous is not None:
                structural.append(
                    f"converter_zone_claimed_by_multiple_facts_cavities:"
                    f"{zone.floor_id}:{zone.zone_id}:"
                    f"{previous[0]}:{previous[1]}:{view.view_id}:{cavity_id}")
                continue
            claimed_converter_zones[zone_key] = (view.view_id, cavity_id)
            # ⭐⭐⭐ Zero-threshold identity: carry the facts ring out to the
            # basis the answer declares for each edge, then require the two to
            # be the SAME polygon.  ⛔ No tolerance is read off the data.
            projected, projection_failure = _projected_facts_ring(facts_edges)
            if projected is None:
                structural.append(
                    f"facts_projected_ring_unavailable:{view.view_id}:"
                    f"{cavity_id}:{projection_failure}")
                continue
            projected_symmetric_difference = projected.symmetric_difference(
                zone_polygon).area
            if projected_symmetric_difference != 0:
                structural.append(
                    f"facts_projected_ring_is_not_the_converter_zone:"
                    f"{view.view_id}:{cavity_id}:{zone.zone_id}:"
                    f"symmetric_difference_units2="
                    f"{projected_symmetric_difference:.0f}")
            # ⛔ No early exit: the per-edge pairing below keeps its own
            # resolution (a single wrong ``boundary_condition`` must still
            # redden exactly its own row, not be swallowed by a whole-cavity
            # verdict), so the identity failure is NAMED and the comparison
            # still runs.
            if len(zone.edges) != len(facts_edges):
                structural.append(
                    f"boundary_edge_count_mismatch:{view.view_id}:{cavity_id}:"
                    f"facts={len(facts_edges)} converter={len(zone.edges)}")
                continue

            converter_segments = [
                (_world_point_to_ingest_grid(edge.p1),
                 _world_point_to_ingest_grid(edge.p2))
                for edge in zone.edges]
            hypotheses: list[BoundaryPairingHypothesisV1] = []
            count = len(facts_edges)
            for direction_value, step in (("forward", 1), ("reverse", -1)):
                for rotation in range(count):
                    indices = [(rotation + step * index) % count
                               for index in range(count)]
                    residuals = [
                        _undirected_segment_residual(facts_edges[index],
                                                     converter_segments[converter_index])
                        for index, converter_index in enumerate(indices)]
                    source_matches = sum(bool(
                        set(facts_edges[index].face_line_handles)
                        & set(zone.edges[converter_index].source_handles))
                        for index, converter_index in enumerate(indices))
                    hypotheses.append(BoundaryPairingHypothesisV1(
                        direction=direction_value, rotation=rotation,
                        converter_edge_indices=indices,
                        max_residual_units=max(residuals),
                        total_residual_units=sum(residuals),
                        source_handle_matches=source_matches))

            geometric = min(
                hypotheses,
                key=lambda item: (
                    item.max_residual_units, item.total_residual_units,
                    item.direction, item.rotation))
            ancestry = min(
                hypotheses,
                key=lambda item: (
                    -item.source_handle_matches, item.max_residual_units,
                    item.total_residual_units, item.direction, item.rotation))
            alternatives = [item for item in hypotheses if item is not geometric]
            alternative_min = min(item.max_residual_units for item in alternatives)
            alternatives_worse = all(
                item.max_residual_units > geometric.max_residual_units
                for item in alternatives)
            same_pairing = (geometric.converter_edge_indices
                            == ancestry.converter_edge_indices)
            proof = BoundaryPairingProofV1(
                view_id=view.view_id, cavity_id=cavity_id,
                converter_zone_id=zone.zone_id,
                facts_edge_ids=[edge.id for edge in facts_edges],
                geometric_converter_edge_indices=geometric.converter_edge_indices,
                ancestry_converter_edge_indices=ancestry.converter_edge_indices,
                selected_direction=geometric.direction,
                selected_rotation=geometric.rotation,
                selected_max_residual_units=geometric.max_residual_units,
                residual_hard_limit_units=BOUNDARY_PAIRING_MAX_RESIDUAL_UNITS,
                alternative_min_residual_units=alternative_min,
                all_alternatives_strictly_worse=alternatives_worse,
                geometry_and_ancestry_pairing_identical=same_pairing,
                hypotheses=hypotheses)
            pairings.append(proof)
            if geometric.max_residual_units > BOUNDARY_PAIRING_MAX_RESIDUAL_UNITS:
                structural.append(
                    f"boundary_pairing_residual_exceeds_hard_limit:"
                    f"{view.view_id}:{cavity_id}:{geometric.max_residual_units}")
            if not alternatives_worse:
                structural.append(
                    f"boundary_pairing_direction_not_unique:{view.view_id}:{cavity_id}")
            if not same_pairing:
                structural.append(
                    f"boundary_geometry_and_ancestry_pairing_disagree:"
                    f"{view.view_id}:{cavity_id}")

            for facts_index, converter_index in enumerate(
                    geometric.converter_edge_indices):
                facts_edge = facts_edges[facts_index]
                converter_edge = zone.edges[converter_index]
                expected = expected_basis.get(facts_edge.boundary_condition)
                rows.append(BoundaryBasisRowV1(
                    view_id=view.view_id, cavity_id=cavity_id,
                    facts_edge_id=facts_edge.id,
                    converter_zone_id=zone.zone_id,
                    converter_edge_index=converter_index,
                    facts_boundary_condition=facts_edge.boundary_condition,
                    expected_converter_basis=expected,
                    converter_basis=converter_edge.basis,
                    matches=(expected == converter_edge.basis)))

    # The facts-driven loop above proves every stored cavity has exactly one
    # converter partner.  This reverse pass closes the other half of the set
    # equality: a converter zone must be either edge-paired or explicitly
    # excluded by a named non-logical facts cavity.  Anything else remains an
    # unclaimed converter invention (E2c), while E3/E4 are caught by the
    # derivable-ring completeness check above.
    for floor_id, zones in sorted(converter_by_floor.items()):
        for zone in sorted(zones, key=lambda item: item.zone_id):
            if (floor_id, zone.zone_id) not in accounted_converter_zones:
                structural.append(
                    f"converter_zone_unclaimed_by_facts:{floor_id}:{zone.zone_id}")

    # ⭐ ②-1d rework3: the FLOODING ('灌证') direction is now caught PER-LOSS,
    # not per-view.  A producer-written ``registered_ring_loss`` is fail-loud
    # above -- every one is its own named structural failure -- so a flood of
    # true-looking losses reddens loss-by-loss and there is no aggregate quota
    # left to tune.  ⛔ We deliberately do NOT count exclusions against pairings:
    # the only exclusions that survive are ``below_request_area_threshold`` ones,
    # which are INDEPENDENTLY provable (the gate recomputes the area, the
    # threshold comes from a different author) and by design unlimited, so an
    # aggregate ``excluded > paired`` cut would false-red an honest building with
    # many sub-threshold shafts (rework3 blocker 1;
    # [[gate-teeth-direction-follows-fixture-inventory]]: teeth belong in the
    # direction the fixture actually has stock, and here the flood stock is
    # per-loss, not aggregate).
    mismatches = [row for row in rows if not row.matches]
    return BoundaryBasisAuditV1(
        passed=not structural and not mismatches,
        paired_edges=len(rows), converter_zones=len(conversion_report.zones),
        accounted_converter_zones=len(accounted_converter_zones),
        rows=rows, mismatches=mismatches, pairings=pairings,
        exclusions=exclusions, structural_failures=structural)


def _merge_projected_spans(edges: list[CompiledZoneEdgeV1]) -> list[CompiledZoneEdgeV1]:
    """6a/6c: collapse consecutive pieces that propagate one support line."""
    if not edges:
        return []
    start = 0
    for index, edge in enumerate(edges):
        previous = edges[index - 1]
        if previous.axis != edge.axis or previous.support_const != edge.support_const:
            start = index
            break
    rotated = edges[start:] + edges[:start]
    merged: list[CompiledZoneEdgeV1] = []
    for edge in rotated:
        if (merged and merged[-1].axis == edge.axis
                and merged[-1].support_const == edge.support_const):
            previous = merged[-1]
            merged[-1] = CompiledZoneEdgeV1.model_validate({
                **previous.model_dump(mode="json"),
                "component_id": _opaque_id(
                    "support", previous.component_id, edge.component_id),
                "span_lo": min(previous.span_lo, edge.span_lo),
                "span_hi": max(previous.span_hi, edge.span_hi),
                "wall_ids": sorted(set(previous.wall_ids) | set(edge.wall_ids)),
                "face_line_handles": sorted(
                    set(previous.face_line_handles) | set(edge.face_line_handles)),
            })
        else:
            merged.append(edge)
    return merged


def _support_vertices(
        edges: list[CompiledZoneEdgeV1], cavity_id: str,
        ) -> tuple[list[tuple[int, int]], list[NaRecordV1]]:
    if len(edges) < 3:
        return [], [NaRecordV1(
            rule="junction_dependency", component_kind="zone_ring",
            component_id=cavity_id, reason_code="fewer_than_three_support_lines",
            affected_metrics=["zone_geometry"], propagated_from=[])]
    vertices: list[tuple[int, int]] = []
    failures: list[NaRecordV1] = []
    for index, edge in enumerate(edges):
        previous = edges[index - 1]
        if previous.axis == edge.axis:
            failures.append(NaRecordV1(
                rule="junction_dependency", component_kind="junction",
                component_id=_opaque_id("junction", cavity_id, index),
                reason_code="adjacent_support_lines_do_not_have_one_intersection",
                affected_metrics=["zone_geometry"],
                propagated_from=[previous.component_id, edge.component_id]))
            continue
        if previous.axis == "y":
            vertices.append((previous.support_const, edge.support_const))
        else:
            vertices.append((edge.support_const, previous.support_const))
    if failures:
        return [], failures
    deduped: list[tuple[int, int]] = []
    for vertex in vertices:
        if not deduped or vertex != deduped[-1]:
            deduped.append(vertex)
    if len(deduped) > 1 and deduped[0] == deduped[-1]:
        deduped.pop()
    if len(deduped) < 3:
        return [], [NaRecordV1(
            rule="junction_dependency", component_kind="zone_ring",
            component_id=cavity_id,
            reason_code="support_line_deduplication_collapsed_the_ring",
            affected_metrics=["zone_geometry"],
            propagated_from=[edge.component_id for edge in edges])]
    polygon = Polygon(deduped)
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        return [], [NaRecordV1(
            rule="junction_dependency", component_kind="zone_ring",
            component_id=cavity_id, reason_code="projected_support_ring_is_invalid",
            affected_metrics=["zone_geometry"],
            propagated_from=[edge.component_id for edge in edges])]
    return deduped, []


def _ring_closure(cavity_id: str, failures: list[NaRecordV1]) -> list[NaRecordV1]:
    records = list(failures)
    records.append(NaRecordV1(
        rule="segment_dependency", component_kind="zone_ring",
        component_id=cavity_id,
        reason_code="incident_component_invalidated_the_complete_ring",
        affected_metrics=["zone_geometry"],
        propagated_from=sorted({failure.component_id for failure in failures})))
    return records


def _compile_openings(view: AsMeasuredViewV1) -> list[CompiledOpeningV1]:
    unresolved = {str(item.get("opening_id")): item
                  for item in view.converter_readouts.unresolved_opening_carriers}
    result = []
    for opening in view.openings:
        if opening.id in unresolved or not opening.carrier_wall_ids:
            na = NaRecordV1(
                rule="opening_local", component_kind="opening",
                component_id=opening.id,
                reason_code="opening_host_is_ambiguous",
                affected_metrics=["opening_geometry"],
                propagated_from=sorted(opening.carrier_wall_ids))
            result.append(_na_opening(opening, na))
        else:
            result.append(CompiledOpeningV1(
                component_id=_opaque_id("opening", view.view_id, opening.id),
                opening_id=opening.id, kind=opening.kind, status="available",
                host_wall_ids=sorted(opening.carrier_wall_ids), axis=opening.axis,
                along_interval=[opening.along_min, opening.along_max],
                cross_interval=[opening.cross_lo, opening.cross_hi], na=[]))
    return result


def _na_opening(opening: AsMeasuredOpeningV1, reason: NaRecordV1) -> CompiledOpeningV1:
    return CompiledOpeningV1(
        component_id=_opaque_id("opening", opening.id),
        opening_id=opening.id, kind=opening.kind, status="na",
        host_wall_ids=[], axis=None, along_interval=None, cross_interval=None,
        na=[reason])


def _missing_zone(view: AsMeasuredViewV1, profile: OutputProfile,
                  index: int, reason: NaRecordV1) -> CompiledZoneV1:
    return CompiledZoneV1(
        component_id=_opaque_id("missing-zone", view.view_id, index),
        floor_id=view.floor_id, profile=profile.value,
        vertices=None, edges=[], clear_span_area_units2=None, na=[reason])


def _metric_status(expected: int, available: int) -> Literal["available", "partial", "na"]:
    na_count = expected - available
    return "available" if na_count == 0 else "na" if available == 0 else "partial"


def _compiled_view_result(
        view: AsMeasuredViewV1, profile: OutputProfile,
        zones: list[CompiledZoneV1], openings: list[CompiledOpeningV1],
        expected: int, *, actual_cavities: int, view_na: list[NaRecordV1],
        coordinate_source_ok: bool) -> CompiledViewV1:
    projected = sum(zone.vertices is not None for zone in zones)
    area_available = sum(zone.clear_span_area_units2 is not None for zone in zones)
    projected_for_coverage = min(projected, expected)
    area_for_coverage = min(area_available, expected)
    opening_available = sum(opening.status == "available" for opening in openings)
    zone_na = [record for zone in zones for record in zone.na]
    opening_na = [record for opening in openings for record in opening.na]
    metrics = [
        MetricResultV1(
            metric="zone_count", profile=profile.value,
            status=("available" if coordinate_source_ok else "na"),
            required_components=[f"view:{view.view_id}:coordinate_source"],
            coverage_expected=1,
            coverage_available=1 if coordinate_source_ok else 0,
            coverage_na=0 if coordinate_source_ok else 1,
            value=actual_cavities if coordinate_source_ok else None,
            na=[] if coordinate_source_ok else view_na),
        MetricResultV1(
            metric="zone_geometry", profile=profile.value,
            status=_metric_status(expected, projected_for_coverage),
            required_components=[zone.component_id for zone in zones],
            coverage_expected=expected, coverage_available=projected_for_coverage,
            coverage_na=expected - projected_for_coverage,
            value=None, na=zone_na + view_na),
        MetricResultV1(
            metric="clear_span_area", profile=profile.value,
            status=_metric_status(expected, area_for_coverage),
            required_components=[zone.component_id for zone in zones],
            coverage_expected=expected, coverage_available=area_for_coverage,
            coverage_na=expected - area_for_coverage,
            value=None, na=zone_na + view_na),
        MetricResultV1(
            metric="opening_geometry", profile=profile.value,
            status=_metric_status(len(openings), opening_available),
            required_components=[opening.component_id for opening in openings],
            coverage_expected=len(openings), coverage_available=opening_available,
            coverage_na=len(openings) - opening_available,
            value=None, na=opening_na + view_na),
    ]
    return CompiledViewV1(
        view_id=view.view_id, floor_id=view.floor_id, profile=profile.value,
        zones=zones, openings=openings, metrics=metrics,
        counts={
            "declared_zones": expected,
            "measured_cavities": actual_cavities,
            "projected_zones": projected,
            "na_zones": expected - projected,
            "openings": len(openings),
            "na_openings": len(openings) - opening_available,
        },
        na=view_na)


def read_facts_for_compilation(
        case: str) -> tuple[AsMeasuredV1, RevisionsLedgerV1, AsSignedV1]:
    """Read facts through an exit-side reproduction check.

    If promoted facts exist under the answer root they are parsed and verified
    on every read, irrespective of how the bytes arrived there.  Otherwise the
    already-gated staging reader is used.  A present but incomplete answer-root
    directory fails loudly instead of silently falling back to staging.
    """
    from . import gt_facts_staging

    gt_facts_staging._validate_case_literal(case)
    answer_root = (REPO_ROOT / "case_tests/test_baseline/gt").resolve()
    facts_dir = (answer_root / case / "facts").resolve()
    if facts_dir != answer_root and answer_root not in facts_dir.parents:
        raise gt_facts_staging.FactsStagingCaseError(
            f"answer_facts_case_escapes_root:{case!r}")
    if not facts_dir.exists():
        return gt_facts_staging.read_facts_candidate(case)
    if not facts_dir.is_dir():
        raise AnswerCompilerInputError(
            f"answer_facts_path_is_not_a_directory:{facts_dir}")
    paths = {name: facts_dir / f"{name}.json"
             for name in ("as_measured", "revisions", "as_signed")}
    missing = sorted(name for name, path in paths.items() if not path.is_file())
    if missing:
        raise AnswerCompilerInputError(
            f"answer_facts_trio_is_incomplete:{case}:{missing}")
    as_measured = AsMeasuredV1.model_validate_json(paths["as_measured"].read_bytes())
    revisions = RevisionsLedgerV1.model_validate_json(paths["revisions"].read_bytes())
    as_signed = AsSignedV1.model_validate_json(paths["as_signed"].read_bytes())
    verify_as_signed_reproduction(as_measured, revisions, as_signed)
    return as_measured, revisions, as_signed
