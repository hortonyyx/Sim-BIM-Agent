"""Va: pure, gt-blind opening × claim applicability adapter.

This module consumes already materialized Vg segment facts and the trusted
view manifest.  It deliberately neither creates geometry nor reads artifacts.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.correction import facade_convention
from src.agent.correction.claims import (
    CLAIMS_VOCAB_VERSION,
    ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS,
    PLAN_POTENTIALLY_OBSERVABLE_CLAIMS,
)
from src.agent.correction.facade import ViewProjectionFrame
from src.agent.correction.schema import FacadeSegment
from src.agent.execution.view_manifest import RequiredViewEntry, ViewManifest

FACADE_APPLICABILITY_SCHEMA_VERSION = "1"
FACADE_APPLICABILITY_HELPER_VERSION = "facade_applicability_v1"

ClaimName = Literal["existence", "host", "along", "width", "sill", "head", "appearance"]
CLAIM_ORDER: tuple[ClaimName, ...] = (
    "existence", "host", "along", "width", "sill", "head", "appearance",
)
ApplicabilityStatus = Literal["applicable", "partially_applicable", "not_applicable"]
ApplicabilityReason = Literal[
    "full_observable_coverage", "existence_observable_fragment",
    "partial_observable_coverage", "unobserved", "outside_reading_exam_scope",
]
EvidenceChannel = Literal["plan", "elevation"]
VisibilityRule = Literal["plan_visibility_bypass", "elevation_visible_intersection"]
FacadeFamily = Literal["North", "South", "East", "West"]
GeometrySourceKind = Literal["accepted_correction", "judge_gt"]
FiniteFloat = Annotated[float, AllowInfNan(False)]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)
_RANK = {"North": 0, "South": 1, "East": 2, "West": 3}
# F-9 route② S1 (2026-08-11): _AXIS/_BASE_SIGN merged into
# `facade_convention` (single, gt-free source); see that module's docstring.


class FacadeApplicabilityInvariantError(ValueError):
    def __init__(self, code: str, context: dict):
        self.code = code
        self.context = dict(context)
        super().__init__(f"{code}: {self.context}")


def _fail(code: str, **context):
    raise FacadeApplicabilityInvariantError(code, context)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ApplicabilityIntervalV1(BaseModel):
    model_config = _CFG
    lo: FiniteFloat
    hi: FiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if self.lo >= self.hi:
            raise ValueError("applicability interval requires lo < hi")
        return self


class FloorVisibilityLedgerV1(BaseModel):
    model_config = _CFG
    floor_id: str
    source_footprint_fingerprint: Hex64
    segments: tuple[FacadeSegment, ...]

    @model_validator(mode="after")
    def _nonempty_floor_id(self):
        if not self.floor_id:
            raise ValueError("floor_id must be non-empty")
        return self


class FacadeVisibilityLedgerV1(BaseModel):
    model_config = _CFG
    ledger_schema_version: Literal["1"] = "1"
    helper_version: Literal["facade_visibility_v1"] = "facade_visibility_v1"
    source_kind: GeometrySourceKind
    source_schema_version: str
    source_output_sha256: Hex64
    facade_segments_sha256: Hex64
    feature_states_sha256: Hex64 | None
    helper_versions: tuple[str, ...]
    floors: tuple[FloorVisibilityLedgerV1, ...]


DirectionResolutionSource = Literal["manifest_building_axis", "resolved_direction_sidecar"]


class ElevationViewBindingV1(BaseModel):
    model_config = _CFG
    input_id: str
    resolved_building_direction: FacadeFamily
    resolution_source: DirectionResolutionSource
    view_manifest_sha256: Hex64
    orientation_output_hash: Hex64 | None
    adapter_version: str | None
    source_footprint_fingerprint: Hex64
    world_axis: Literal["x", "y"]
    sign: Literal[-1, 1]
    along_origin: FiniteFloat
    mirrored: bool
    local_x_positive: Literal["image_left_to_right", "image_right_to_left"]
    frame_transform_sha256: Hex64

    @model_validator(mode="after")
    def _nonempty_input_id(self):
        if not self.input_id:
            raise ValueError("input_id must be non-empty")
        return self


class PlanClaimEvidenceV1(BaseModel):
    model_config = _CFG
    channel: Literal["plan"] = "plan"
    source_input_id: str
    world_interval: ApplicabilityIntervalV1

    @model_validator(mode="after")
    def _nonempty_source(self):
        if not self.source_input_id:
            raise ValueError("source_input_id must be non-empty")
        return self


class ElevationClaimEvidenceV1(BaseModel):
    model_config = _CFG
    channel: Literal["elevation"] = "elevation"
    source_input_id: str
    local_interval: ApplicabilityIntervalV1

    @model_validator(mode="after")
    def _nonempty_source(self):
        if not self.source_input_id:
            raise ValueError("source_input_id must be non-empty")
        return self


ClaimEvidenceV1 = Annotated[Union[PlanClaimEvidenceV1, ElevationClaimEvidenceV1], Field(discriminator="channel")]


class OpeningClaimTargetV1(BaseModel):
    model_config = _CFG
    claim: ClaimName
    target_world_interval: ApplicabilityIntervalV1
    positive_evidence: tuple[ClaimEvidenceV1, ...] = ()


class OpeningClaimsV1(BaseModel):
    model_config = _CFG
    opening_id: str
    floor_id: str
    floor_ref: int
    facade_segment_id: str
    facade_family: FacadeFamily
    claims: tuple[OpeningClaimTargetV1, ...]

    @model_validator(mode="after")
    def _nonempty_ids(self):
        if not self.opening_id or not self.floor_id or not self.facade_segment_id:
            raise ValueError("opening_id, floor_id, and facade_segment_id must be non-empty")
        return self


class SegmentEvidenceSliceV1(BaseModel):
    model_config = _CFG
    facade_segment_id: str
    intervals: tuple[ApplicabilityIntervalV1, ...]


class SourceEvidenceDecisionV1(BaseModel):
    model_config = _CFG
    source_input_id: str
    channel: EvidenceChannel
    visibility_rule: VisibilityRule
    positive_evidence_declared: bool
    positive_mapped_world_interval: ApplicabilityIntervalV1 | None
    applicable_intervals: tuple[ApplicabilityIntervalV1, ...]
    negative_evidence_intervals: tuple[ApplicabilityIntervalV1, ...]
    segment_slices: tuple[SegmentEvidenceSliceV1, ...]
    negative_evidence_capable: bool
    completeness_assertion_id: str | None


class ClaimApplicabilityV1(BaseModel):
    model_config = _CFG
    claim: ClaimName
    status: ApplicabilityStatus
    reason: ApplicabilityReason
    target_world_interval: ApplicabilityIntervalV1
    applicable_intervals: tuple[ApplicabilityIntervalV1, ...]
    unobserved_intervals: tuple[ApplicabilityIntervalV1, ...]
    considered_source_view_ids: tuple[str, ...]
    supporting_source_view_ids: tuple[str, ...]
    facade_segment_ids: tuple[str, ...]
    source_evidence: tuple[SourceEvidenceDecisionV1, ...]


class OpeningApplicabilityV1(BaseModel):
    model_config = _CFG
    opening_id: str
    floor_id: str
    floor_ref: int
    facade_segment_id: str
    facade_family: FacadeFamily
    claims: tuple[ClaimApplicabilityV1, ...]


class ApplicabilityBindingsV1(BaseModel):
    model_config = _CFG
    geometry_source_kind: GeometrySourceKind
    geometry_source_schema_version: str
    geometry_source_output_sha256: Hex64
    facade_segments_sha256: Hex64
    feature_states_sha256: Hex64 | None
    view_manifest_sha256: Hex64
    direction_bindings_sha256: Hex64


class OpeningApplicabilityLedgerV1(BaseModel):
    model_config = _CFG
    applicability_schema_version: Literal["1"] = "1"
    helper_version: Literal["facade_applicability_v1"] = "facade_applicability_v1"
    claims_vocab_version: Literal["1"] = "1"
    view_manifest_schema_version: Literal["1"] = "1"
    visibility_helper_version: Literal["facade_visibility_v1"] = "facade_visibility_v1"
    bindings: ApplicabilityBindingsV1
    openings: tuple[OpeningApplicabilityV1, ...]
    content_sha256: Hex64

    @model_validator(mode="after")
    def _hash_consistent(self):
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != _canonical_hash(payload):
            raise ValueError("content_sha256 does not match canonical applicability payload")
        return self


def _intervals(raw) -> tuple[ApplicabilityIntervalV1, ...]:
    return tuple(ApplicabilityIntervalV1(lo=x[0], hi=x[1]) for x in raw)


def _merge(intervals):
    ordered = sorted(((x.lo, x.hi) for x in intervals), key=lambda x: (x[0], x[1]))
    out: list[list[float]] = []
    for lo, hi in ordered:
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return _intervals(out)


def _intersect(a: ApplicabilityIntervalV1, b: ApplicabilityIntervalV1):
    lo, hi = max(a.lo, b.lo), min(a.hi, b.hi)
    return ApplicabilityIntervalV1(lo=lo, hi=hi) if lo < hi else None


def _complement(target: ApplicabilityIntervalV1, covered):
    cursor = target.lo
    out = []
    for interval in _merge(covered):
        lo, hi = max(target.lo, interval.lo), min(target.hi, interval.hi)
        if cursor < lo:
            out.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < target.hi:
        out.append((cursor, target.hi))
    return _intervals(out)


def _segment_payload(visibility: FacadeVisibilityLedgerV1):
    rows = []
    for floor in visibility.floors:
        for seg in floor.segments:
            rows.append((floor.floor_id, _RANK[seg.facade_family], seg.world_along_interval.lo,
                         seg.world_along_interval.hi, seg.depth, seg.id, seg.model_dump(mode="json")))
    return [x[-1] for x in sorted(rows)]


def _validate_visibility(visibility: FacadeVisibilityLedgerV1):
    if visibility.source_kind == "accepted_correction":
        if visibility.source_schema_version != "3" or visibility.feature_states_sha256 is None or visibility.helper_versions != ("floor_footprint_v1", "facade_visibility_v1"):
            _fail("va_identity_mismatch", declared=visibility.model_dump(mode="json"))
    elif visibility.feature_states_sha256 is not None:
        _fail("va_identity_mismatch", declared="judge_gt feature_states_sha256")
    floor_ids, seg_ids = set(), set()
    for floor in visibility.floors:
        if not floor.floor_id or not floor.segments or floor.floor_id in floor_ids:
            _fail("va_visibility_ledger_invalid", floor_id=floor.floor_id)
        floor_ids.add(floor.floor_id)
        for seg in floor.segments:
            if seg.floor_id != floor.floor_id or seg.source_footprint_fingerprint != floor.source_footprint_fingerprint or seg.id in seg_ids:
                _fail("va_visibility_ledger_invalid", floor_id=floor.floor_id, facade_segment_id=seg.id)
            seg_ids.add(seg.id)
            prev = None
            for interval in seg.visible_intervals:
                if prev is not None and interval.lo < prev:
                    _fail("va_visibility_ledger_invalid", facade_segment_id=seg.id)
                prev = interval.hi
    if not floor_ids:
        _fail("va_visibility_ledger_invalid", reason="empty floors")
    actual = _canonical_hash(_segment_payload(visibility))
    if actual != visibility.facade_segments_sha256:
        _fail("va_identity_mismatch", declared=visibility.facade_segments_sha256, recomputed=actual)


def _binding_payload(binding: ElevationViewBindingV1):
    return binding.model_dump(mode="json")


def _frame_hash(binding: ElevationViewBindingV1):
    return _canonical_hash({"schema": "view_projection_binding_v1", "input_id": binding.input_id,
        "resolved_building_direction": binding.resolved_building_direction,
        "source_footprint_fingerprint": binding.source_footprint_fingerprint,
        "world_axis": binding.world_axis, "sign": binding.sign, "along_origin": binding.along_origin,
        "mirrored": binding.mirrored, "local_x_positive": binding.local_x_positive})


def _validate_bindings(manifest: ViewManifest, bindings: tuple[ElevationViewBindingV1, ...]):
    required = {e.input_id: e for e in manifest.required_entries() if e.view_type == "elevation"}
    got = {b.input_id: b for b in bindings}
    if len(got) != len(bindings) or set(got) != set(required):
        _fail("va_direction_unresolved", declared=sorted(got), required=sorted(required))
    for input_id, binding in got.items():
        entry = required[input_id]
        if binding.view_manifest_sha256 != manifest.content_sha256:
            _fail("va_identity_mismatch", input_id=input_id, declared=binding.view_manifest_sha256, recomputed=manifest.content_sha256)
        if binding.resolution_source == "manifest_building_axis":
            if entry.direction_semantics != "building_axis" or binding.resolved_building_direction != entry.building_view_direction or binding.orientation_output_hash is not None or binding.adapter_version is not None:
                _fail("va_direction_unresolved", input_id=input_id)
        else:
            if entry.direction_semantics not in ("true_azimuth", "unknown") or not binding.orientation_output_hash or not binding.adapter_version:
                _fail("va_direction_unresolved", input_id=input_id)
        expected_sign = facade_convention.resolve_sign(
            binding.resolved_building_direction, mirrored=binding.mirrored, local_x_positive=binding.local_x_positive,
        )
        if binding.world_axis != facade_convention.world_axis(binding.resolved_building_direction) or binding.sign != expected_sign or binding.frame_transform_sha256 != _frame_hash(binding):
            _fail("va_projection_frame_invalid", input_id=input_id)
    return got


def _entry(manifest: ViewManifest, source: str):
    entry = manifest.entry_by_input_id(source)
    if not isinstance(entry, RequiredViewEntry):
        _fail("va_claim_ledger_invalid", input_id=source)
    return entry


def _negative(entry: RequiredViewEntry, claim: str) -> bool:
    return claim in entry.opening_evidence.negative_evidence_capable_claims


def _relevant_negative(
    entry: RequiredViewEntry,
    opening: OpeningClaimsV1,
    bindings: dict[str, ElevationViewBindingV1],
) -> bool:
    """Whether this trusted completeness source belongs to this opening.

    Elevation relevance is intentionally based on the *view binding's resolved*
    building family, never the opening family echoed through a caller argument.
    """
    return (
        (entry.view_type == "plan" and entry.floor_ref == opening.floor_ref)
        or (
            entry.view_type == "elevation"
            and bindings[entry.input_id].resolved_building_direction == opening.facade_family
        )
    )


def derive_opening_claim_applicability(*, visibility: FacadeVisibilityLedgerV1, manifest: ViewManifest,
        elevation_views: tuple[ElevationViewBindingV1, ...], openings: tuple[OpeningClaimsV1, ...],
        reading_exam_scope_source: str | None = None) -> OpeningApplicabilityLedgerV1:
    """Derive the immutable Va ledger from caller-owned, in-memory facts only."""
    if manifest.view_manifest_schema_version != "1" or manifest.claims_vocab_version != CLAIMS_VOCAB_VERSION or manifest.generator_version != "1" or manifest.completeness_ruleset_version != "1":
        _fail("va_identity_mismatch", declared=manifest.model_dump(mode="json"))
    _validate_visibility(visibility)
    bindings = _validate_bindings(manifest, elevation_views)
    by_segment = {seg.id: (floor, seg) for floor in visibility.floors for seg in floor.segments}
    used_openings = set()
    rendered: list[OpeningApplicabilityV1] = []
    for opening in openings:
        if not opening.opening_id or not opening.floor_id or not opening.facade_segment_id or opening.opening_id in used_openings:
            _fail("va_opening_segment_invalid", opening_id=opening.opening_id)
        used_openings.add(opening.opening_id)
        item = by_segment.get(opening.facade_segment_id)
        if item is None:
            _fail("va_opening_segment_invalid", opening_id=opening.opening_id, facade_segment_id=opening.facade_segment_id)
        floor, segment = item
        if floor.floor_id != opening.floor_id or segment.facade_family != opening.facade_family:
            _fail("va_opening_segment_invalid", opening_id=opening.opening_id, facade_segment_id=segment.id)
        if tuple(x.claim for x in opening.claims) != CLAIM_ORDER:
            _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, declared=[x.claim for x in opening.claims])
        target = opening.claims[0].target_world_interval
        opening_has_scope_evidence = any(
            claim.positive_evidence for claim in opening.claims
        )
        if any(c.target_world_interval != target for c in opening.claims) or not _intersect(target, ApplicabilityIntervalV1(lo=segment.world_along_interval.lo, hi=segment.world_along_interval.hi)) or target.lo < segment.world_along_interval.lo or target.hi > segment.world_along_interval.hi:
            _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, facade_segment_id=segment.id)
        claims_out = []
        for claim_target in opening.claims:
            claim = claim_target.claim
            positives: dict[str, ClaimEvidenceV1] = {}
            for evidence in claim_target.positive_evidence:
                if evidence.source_input_id in positives:
                    _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=evidence.source_input_id)
                entry = _entry(manifest, evidence.source_input_id)
                if claim not in entry.opening_evidence.potentially_observable_claims:
                    _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=evidence.source_input_id)
                if evidence.channel == "plan":
                    if (
                        entry.view_type != "plan"
                        or entry.floor_ref != opening.floor_ref
                        or claim not in PLAN_POTENTIALLY_OBSERVABLE_CLAIMS
                    ):
                        _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=evidence.source_input_id)
                elif (
                    entry.view_type != "elevation"
                    or bindings[evidence.source_input_id].resolved_building_direction != opening.facade_family
                    or claim not in ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS
                ):
                    _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=evidence.source_input_id)
                positives[evidence.source_input_id] = evidence
            negatives = {
                e.input_id: e
                for e in manifest.required_entries()
                if _negative(e, claim) and _relevant_negative(e, opening, bindings)
            }
            decisions = []
            for source in sorted(set(positives) | set(negatives), key=lambda s: (0 if _entry(manifest, s).view_type == "plan" else 1, s)):
                evidence, entry = positives.get(source), _entry(manifest, source)
                neg_capable = source in negatives
                mapped = None
                covered: tuple[ApplicabilityIntervalV1, ...] = ()
                if evidence is not None and evidence.channel == "plan":
                    mapped = evidence.world_interval
                    inter = _intersect(mapped, target)
                    if inter is None:
                        _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=source)
                    covered = (inter,)
                    rule: VisibilityRule = "plan_visibility_bypass"
                elif evidence is not None:
                    binding = bindings[source]
                    if binding.source_footprint_fingerprint != floor.source_footprint_fingerprint:
                        _fail("va_projection_frame_invalid", input_id=source, floor_id=opening.floor_id)
                    extent = (min(s.world_along_interval.lo for s in floor.segments if s.facade_family == opening.facade_family), max(s.world_along_interval.hi for s in floor.segments if s.facade_family == opening.facade_family))
                    expected_origin = extent[0] if binding.sign == 1 else extent[1]
                    if binding.along_origin != expected_origin:
                        _fail("va_projection_frame_invalid", input_id=source, floor_id=opening.floor_id)
                    frame = ViewProjectionFrame(facade_family=binding.resolved_building_direction, world_axis=binding.world_axis, sign=binding.sign, along_origin=binding.along_origin, mirrored=binding.mirrored, local_x_positive=binding.local_x_positive)
                    a, b = frame.to_world_along(evidence.local_interval.lo), frame.to_world_along(evidence.local_interval.hi)
                    mapped = ApplicabilityIntervalV1(lo=min(a, b), hi=max(a, b))
                    candidate = _intersect(mapped, target)
                    if candidate is None:
                        _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=source)
                    covered = _merge(x for x in (_intersect(candidate, ApplicabilityIntervalV1(lo=v.lo, hi=v.hi)) for v in segment.visible_intervals) if x is not None)
                    rule = "elevation_visible_intersection"
                else:
                    rule = "plan_visibility_bypass" if entry.view_type == "plan" else "elevation_visible_intersection"
                negative_intervals: tuple[ApplicabilityIntervalV1, ...] = ()
                assertion_id = None
                if neg_capable:
                    coverage = entry.opening_evidence.coverage
                    assertion = entry.opening_evidence.completeness_assertion
                    expected_coverage = ("plan_floor_region", "full_floor") if entry.view_type == "plan" else ("elevation_local_along", "full_facade")
                    if coverage is None or assertion is None or (coverage.frame, coverage.region) != expected_coverage or not assertion.assertion_id:
                        _fail("va_claim_ledger_invalid", opening_id=opening.opening_id, claim=claim, input_id=source)
                    assertion_id = assertion.assertion_id
                    if entry.view_type == "plan":
                        negative_intervals = (target,)
                    else:
                        negative_intervals = _merge(x for x in (_intersect(target, ApplicabilityIntervalV1(lo=v.lo, hi=v.hi)) for v in segment.visible_intervals) if x is not None)
                slices = (SegmentEvidenceSliceV1(facade_segment_id=segment.id, intervals=covered),) if covered else ()
                decisions.append(SourceEvidenceDecisionV1(source_input_id=source, channel=entry.view_type, visibility_rule=rule,
                    positive_evidence_declared=evidence is not None, positive_mapped_world_interval=mapped,
                    applicable_intervals=covered, negative_evidence_intervals=negative_intervals, segment_slices=slices,
                    negative_evidence_capable=neg_capable, completeness_assertion_id=assertion_id))
            covered = _merge(x for d in decisions for x in d.applicable_intervals)
            unobserved = _complement(target, covered)
            if not covered:
                status, reason = "not_applicable", (
                    "outside_reading_exam_scope"
                    if reading_exam_scope_source is not None
                    and not opening_has_scope_evidence
                    else "unobserved"
                )
            elif not unobserved:
                status, reason = "applicable", "full_observable_coverage"
            elif claim == "existence":
                status, reason = "applicable", "existence_observable_fragment"
            else:
                status, reason = "partially_applicable", "partial_observable_coverage"
            claims_out.append(ClaimApplicabilityV1(claim=claim, status=status, reason=reason, target_world_interval=target,
                applicable_intervals=covered, unobserved_intervals=unobserved,
                considered_source_view_ids=tuple(d.source_input_id for d in decisions),
                supporting_source_view_ids=tuple(d.source_input_id for d in decisions if d.applicable_intervals),
                facade_segment_ids=(segment.id,) if covered else (), source_evidence=tuple(decisions)))
        rendered.append(OpeningApplicabilityV1(opening_id=opening.opening_id, floor_id=opening.floor_id, floor_ref=opening.floor_ref,
            facade_segment_id=opening.facade_segment_id, facade_family=opening.facade_family, claims=tuple(claims_out)))
    rendered.sort(key=lambda x: (x.floor_id, x.facade_segment_id, x.opening_id))
    binding_hash = _canonical_hash([_binding_payload(x) for x in sorted(elevation_views, key=lambda x: x.input_id)])
    output_bindings = ApplicabilityBindingsV1(geometry_source_kind=visibility.source_kind, geometry_source_schema_version=visibility.source_schema_version,
        geometry_source_output_sha256=visibility.source_output_sha256, facade_segments_sha256=visibility.facade_segments_sha256,
        feature_states_sha256=visibility.feature_states_sha256, view_manifest_sha256=manifest.content_sha256, direction_bindings_sha256=binding_hash)
    payload = {"applicability_schema_version": "1", "helper_version": FACADE_APPLICABILITY_HELPER_VERSION, "claims_vocab_version": CLAIMS_VOCAB_VERSION,
        "view_manifest_schema_version": "1", "visibility_helper_version": "facade_visibility_v1", "bindings": output_bindings.model_dump(mode="json"),
        "openings": [x.model_dump(mode="json") for x in rendered]}
    return OpeningApplicabilityLedgerV1(**{**payload, "openings": tuple(rendered)}, content_sha256=_canonical_hash(payload))
