"""Strict, judge-side ground-truth schema contracts (B4a Phase A).

This module deliberately contains no extraction or rendering policy.  Schema v3
is a typed, self-validating candidate format; schema v2 remains a read-only
adapter for the existing human-ratified answer key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import (AllowInfNan, BaseModel, ConfigDict, Field, Strict,
                      StringConstraints, model_validator)

from src.agent.correction.facade_visibility import VisibilityTolerances, vg_for_direction
from src.agent.correction.footprint import footprint_fingerprint

JsonValue: TypeAlias = Any
StrictFiniteFloat = Annotated[float, Strict(), AllowInfNan(False)]
NonNegativeFiniteFloat = Annotated[StrictFiniteFloat, Field(ge=0)]
PositiveFiniteFloat = Annotated[StrictFiniteFloat, Field(gt=0)]
StrictNonNegativeInt = Annotated[int, Strict(), Field(ge=0)]
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
HumanLabel = Annotated[str, StringConstraints(min_length=1, max_length=255)]
DxfHandle = Annotated[str, StringConstraints(pattern=r"^[0-9A-F]+$")]
DateYmd = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
# JSON arrays must remain acceptable under strict validation; Pydantic strict
# tuples reject JSON lists, so fixed-size wire arrays are represented as lists.
Point2 = Annotated[list[StrictFiniteFloat], Field(min_length=2, max_length=2)]
REPO_ROOT = Path(__file__).resolve().parents[3]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GtWorldIntervalV3(_StrictModel):
    lo: StrictFiniteFloat
    hi: StrictFiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if not self.lo < self.hi:
            raise ValueError("interval lo must be less than hi")
        return self


class GtRingV3(_StrictModel):
    vertices: list[Point2] = Field(min_length=4)


class GtPolygonV3(_StrictModel):
    exterior: GtRingV3
    interior_rings: list[GtRingV3] = Field(default_factory=list)


class GtEntityRefV3(_StrictModel):
    source_id: StableId
    view_id: StableId
    entity_handle: DxfHandle
    subentity_index: StrictNonNegativeInt | None = None
    role: Literal["footprint_boundary", "zone_boundary", "opening_plan", "opening_elevation", "north_axis", "configured_binding"]


class GtSourceViewV3(_StrictModel):
    id: StableId
    kind: Literal["plan", "elevation"]
    floor_ids: list[StableId] = Field(min_length=1)
    projection_surface_key: StableId | None
    facade_family: Literal["North", "South", "East", "West"] | None
    view_kind: Literal["full", "partial"] | None
    world_along_coverage: GtWorldIntervalV3 | None
    direction_semantics: Literal["building_axis", "true_azimuth"] | None
    azimuth_deg: Annotated[StrictFiniteFloat, Field(ge=0, lt=360)] | None

    @model_validator(mode="after")
    def _kind_contract(self):
        if self.kind == "plan":
            if len(self.floor_ids) != 1 or any(v is not None for v in (
                self.projection_surface_key, self.facade_family, self.view_kind,
                self.world_along_coverage, self.direction_semantics, self.azimuth_deg,
            )):
                raise ValueError("plan view has elevation-only fields")
        elif any(v is None for v in (self.projection_surface_key, self.facade_family,
                                     self.view_kind, self.direction_semantics)):
            raise ValueError("elevation view requires surface, family, kind and semantics")
        return self


class GtSourceDocumentV3(_StrictModel):
    id: StableId
    kind: Literal["dxf"]
    label: HumanLabel
    content_sha256: Hex64
    native_units: Literal["m", "mm", "cm", "in", "ft", "unitless"]
    metres_per_unit: PositiveFiniteFloat
    views: list[GtSourceViewV3] = Field(min_length=1)


class GtExtractionTolerancesV1(_StrictModel):
    profile_version: Literal[1]
    dxf_node_join_tolerance_m: PositiveFiniteFloat
    dxf_axis_alignment_tolerance_m: PositiveFiniteFloat
    dxf_topology_area_tolerance_m2: PositiveFiniteFloat
    opening_boundary_max_distance_m: PositiveFiniteFloat
    opening_assignment_tie_epsilon_m: PositiveFiniteFloat
    elevation_match_max_distance_m: PositiveFiniteFloat
    elevation_match_tie_epsilon_m: PositiveFiniteFloat


class GtResolvedToolingTolerancesV1(GtExtractionTolerancesV1):
    vg_depth_epsilon_m: PositiveFiniteFloat
    vg_endpoint_epsilon_m: PositiveFiniteFloat

    @model_validator(mode="after")
    def _relationships(self):
        minimum = min(self.dxf_node_join_tolerance_m, self.dxf_axis_alignment_tolerance_m)
        if self.opening_assignment_tie_epsilon_m >= minimum or self.elevation_match_tie_epsilon_m >= minimum:
            raise ValueError("assignment tie epsilon must be below node and axis tolerance")
        if self.vg_depth_epsilon_m >= self.dxf_node_join_tolerance_m or self.vg_endpoint_epsilon_m >= self.dxf_node_join_tolerance_m:
            raise ValueError("Vg epsilon must be below node join tolerance")
        return self


class GtResolvedToolingConfigV1(_StrictModel):
    tolerances: GtResolvedToolingTolerancesV1
    judge_config_sha256: Hex64
    vg_config_sha256: Hex64


class GtImplementationHashesV1(_StrictModel):
    extractor_sha256: Hex64
    validator_sha256: Hex64
    vg_implementation_sha256: Hex64


class GtVerificationV3(_StrictModel):
    status: Literal["candidate", "human_verified"]
    reviewer_id: StableId | None
    reviewed_on: DateYmd | None
    methods: list[Literal["dxf_topology_roundtrip", "direct_gt_render", "overlay_on_original_drawing", "human_source_comparison"]] = Field(default_factory=list)


class GtGeneratorV3(_StrictModel):
    name: Literal["energyplus-agent.gt_from_dxf"]
    contract_version: Literal[1]
    extractor_sha256: Hex64
    validator_sha256: Hex64
    vg_implementation_sha256: Hex64
    manifest_sha256: Hex64
    judge_config_sha256: Hex64
    vg_config_sha256: Hex64
    tolerances: GtResolvedToolingTolerancesV1


class GtZoneV3(_StrictModel):
    id: StableId
    name: HumanLabel
    role: StableId
    polygon: GtPolygonV3
    source_refs: list[GtEntityRefV3] = Field(min_length=1)


class GtBoundarySegmentV3(_StrictModel):
    id: StableId
    floor_id: StableId
    boundary_loop_id: Literal["exterior"]
    facade_family: Literal["North", "South", "East", "West"]
    p1: Point2
    p2: Point2
    outward_normal: Annotated[list[Literal[-1, 0, 1]], Field(min_length=2, max_length=2)]
    world_along_interval: GtWorldIntervalV3
    depth: NonNegativeFiniteFloat
    visible_intervals: list[GtWorldIntervalV3]
    source_footprint_fingerprint: Hex64
    projection_surface_keys: list[StableId] = Field(default_factory=list)
    wall_thickness_m: PositiveFiniteFloat | None
    source_refs: list[GtEntityRefV3] = Field(min_length=1)


class GtFloorV3(_StrictModel):
    id: StableId
    name: HumanLabel
    z_floor_m: StrictFiniteFloat
    ceiling_height_m: PositiveFiniteFloat
    footprint: GtPolygonV3
    footprint_fingerprint: Hex64
    zones: list[GtZoneV3] = Field(min_length=1)
    boundary_segments: list[GtBoundarySegmentV3] = Field(min_length=4)


class GtOpeningV3(_StrictModel):
    id: StableId
    kind: Literal["window", "door"]
    floor_id: StableId
    host_zone_id: StableId | None
    boundary_segment_id: StableId
    world_along_interval: GtWorldIntervalV3
    z_interval: GtWorldIntervalV3 | None
    source_refs: list[GtEntityRefV3] = Field(min_length=1)


class GroundTruthV3(_StrictModel):
    schema_version: Literal[3]
    case: StableId
    geometry_profile: Literal["c2_simple_orthogonal_no_holes"]
    coordinate_frame: Literal["building_axis_world_m"]
    verification: GtVerificationV3
    generator: GtGeneratorV3
    sources: list[GtSourceDocumentV3] = Field(min_length=1)
    north_axis_deg: Annotated[StrictFiniteFloat, Field(ge=0, lt=360)] | None = None
    north_axis_source_refs: list[GtEntityRefV3] = Field(default_factory=list)
    floors: list[GtFloorV3] = Field(min_length=1)
    openings: list[GtOpeningV3] = Field(default_factory=list)
    content_sha256: Hex64


# Legacy wire: it is intentionally not converted to v3.
class LegacyFootprintV2(_StrictModel):
    W_m: PositiveFiniteFloat
    D_m: PositiveFiniteFloat


class LegacyZoneV2(_StrictModel):
    id: str
    role: str
    rect_m: Annotated[list[StrictFiniteFloat], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def _rectangle(self):
        if not self.rect_m[0] < self.rect_m[2] or not self.rect_m[1] < self.rect_m[3]:
            raise ValueError("rect must have positive dimensions")
        return self


class LegacyFloorV2(_StrictModel):
    name: str
    z_floor: StrictFiniteFloat
    ceiling_height: PositiveFiniteFloat
    zone_count: Annotated[int, Strict(), Field(ge=1)]
    zones: list[LegacyZoneV2]


class LegacyOpeningV2(_StrictModel):
    x_m: StrictFiniteFloat
    width_m: PositiveFiniteFloat
    sill_m: StrictFiniteFloat
    head_m: StrictFiniteFloat

    @model_validator(mode="after")
    def _z_order(self):
        if not self.sill_m < self.head_m:
            raise ValueError("sill must be below head")
        return self


class LegacyWindowGroupV2(_StrictModel):
    facade: Literal["North", "South", "East", "West"]
    floor: str
    count: StrictNonNegativeInt
    sill_m: StrictFiniteFloat | None
    head_m: StrictFiniteFloat | None
    openings: list[LegacyOpeningV2]


class LegacyDoorV2(LegacyOpeningV2):
    facade: Literal["North", "South", "East", "West"]
    floor: str


class LegacyGroundTruthV2(_StrictModel):
    case: str
    schema_version: Literal[2]
    source: str | None = Field(default=None, alias="_source")
    cad_file: str | None = Field(default=None, alias="_cad_file")
    cad_sha256: Hex64 | None = Field(default=None, alias="_cad_sha256")
    extractor: str | None = Field(default=None, alias="_extractor")
    note: str | None = Field(default=None, alias="_note")
    verified: bool | None = Field(default=None, alias="_verified")
    verified_by: str | None = Field(default=None, alias="_verified_by")
    verified_on: str | None = Field(default=None, alias="_verified_on")
    verified_method: str | None = Field(default=None, alias="_verified_method")
    wall_thickness_m: PositiveFiniteFloat
    wall_thickness_note: str | None = Field(default=None, alias="_wall_thickness_note")
    footprint: LegacyFootprintV2
    floors: list[LegacyFloorV2]
    windows: list[LegacyWindowGroupV2]
    doors: list[LegacyDoorV2]


GtDocument: TypeAlias = LegacyGroundTruthV2 | GroundTruthV3


@dataclass(frozen=True)
class GtValidationIssue:
    code: str
    pointer: str
    context: Mapping[str, JsonValue]


class GtValidationError(ValueError):
    def __init__(self, issues: list[GtValidationIssue] | tuple[GtValidationIssue, ...]):
        self.issues = tuple(sorted(issues, key=lambda issue: (issue.pointer, issue.code)))
        super().__init__("; ".join(f"{i.code} at {i.pointer}" for i in self.issues))


def _fail(code: str, pointer: str, **context: JsonValue) -> None:
    raise GtValidationError([GtValidationIssue(code, pointer, context)])


def _ordered_rings_equal_within_tolerance(
        reference: tuple[tuple[float, float], ...],
        candidate: tuple[tuple[float, float], ...], tolerance: float) -> bool:
    """Compare corresponding vertices without relaxing ring order or shape."""
    if len(reference) != len(candidate):
        return False
    tolerance_sq = tolerance * tolerance
    return all((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 <= tolerance_sq
               for left, right in zip(reference, candidate))


def _ring_vertices(ring: GtRingV3, pointer: str) -> tuple[tuple[float, float], ...]:
    pts = tuple((float(x), float(y)) for x, y in ring.vertices)
    if pts[0] == pts[-1]:
        _fail("gt_polygon_closed_ring", pointer)
    if len(set(pts)) != len(pts):
        _fail("gt_polygon_repeated_vertex", pointer)
    dirs: list[tuple[int, int]] = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx == 0 and dy == 0:
            _fail("gt_polygon_zero_edge", pointer)
        if dx != 0 and dy != 0:
            _fail("gt_polygon_nonorthogonal", pointer)
        dirs.append((0 if dx == 0 else (1 if dx > 0 else -1), 0 if dy == 0 else (1 if dy > 0 else -1)))
    for previous, current in zip(dirs[-1:] + dirs[:-1], dirs):
        if previous == current:
            _fail("gt_wire_noncanonical_ring", pointer)
        if previous == (-current[0], -current[1]):
            _fail("gt_polygon_backtrack", pointer)
    area2 = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if area2 <= 0:
        _fail("gt_polygon_not_ccw", pointer)
    if pts[0] != min(pts):
        _fail("gt_wire_noncanonical_ring", pointer)
    # Shapely is intentionally a checker, never a repair mechanism.
    from shapely.geometry import Polygon
    polygon = Polygon(pts)
    if not polygon.is_valid:
        _fail("gt_polygon_invalid", pointer)
    return pts


def _reference_key(ref: GtEntityRefV3) -> tuple[Any, ...]:
    return (ref.source_id, ref.view_id, ref.entity_handle, -1 if ref.subentity_index is None else ref.subentity_index, ref.role)


def _intervals_ordered(intervals: list[GtWorldIntervalV3]) -> bool:
    return all((a.lo, a.hi) < (b.lo, b.hi) and a.hi <= b.lo for a, b in zip(intervals, intervals[1:]))


def _family_normal(family: str) -> tuple[int, int]:
    return {"North": (0, 1), "South": (0, -1), "East": (1, 0), "West": (-1, 0)}[family]


def _segment_sort_key(segment: GtBoundarySegmentV3) -> tuple[Any, ...]:
    rank = {"North": 0, "South": 1, "East": 2, "West": 3}[segment.facade_family]
    return (rank, segment.world_along_interval.lo, segment.world_along_interval.hi, segment.depth, segment.id)


def _is_zero_hash(value: str) -> bool:
    return value == "0" * 64


def validate_legacy_v2(doc: LegacyGroundTruthV2, *, expected_case: str | None = None) -> None:
    if expected_case is not None and doc.case != expected_case:
        _fail("gt_legacy_case_mismatch", "/case", expected=expected_case, actual=doc.case)
    if len({floor.name for floor in doc.floors}) != len(doc.floors):
        _fail("gt_legacy_duplicate_floor", "/floors")
    zone_ids: set[str] = set()
    spans = {"North": doc.footprint.W_m, "South": doc.footprint.W_m,
             "East": doc.footprint.D_m, "West": doc.footprint.D_m}
    floor_names = {floor.name for floor in doc.floors}
    from shapely.geometry import box
    from shapely.ops import unary_union
    footprint = box(0, 0, doc.footprint.W_m, doc.footprint.D_m)
    for index, floor in enumerate(doc.floors):
        ptr = f"/floors/{index}"
        if floor.zone_count != len(floor.zones):
            _fail("gt_legacy_zone_count_mismatch", ptr)
        for zone in floor.zones:
            if zone.id in zone_ids:
                _fail("gt_legacy_duplicate_zone", ptr)
            zone_ids.add(zone.id)
            x0, y0, x1, y1 = zone.rect_m
            if x0 < 0 or y0 < 0 or x1 > doc.footprint.W_m or y1 > doc.footprint.D_m:
                _fail("gt_legacy_zone_outside_footprint", ptr)
        zone_polygons = [box(*zone.rect_m) for zone in floor.zones]
        if any(a.intersection(b).area > 0 for n, a in enumerate(zone_polygons) for b in zone_polygons[n + 1:]):
            _fail("gt_legacy_zone_overlap", ptr)
        if unary_union(zone_polygons).symmetric_difference(footprint).area > 0:
            _fail("gt_legacy_zone_tiling_mismatch", ptr)
    for index, group in enumerate(doc.windows):
        ptr = f"/windows/{index}"
        if group.floor not in floor_names or group.count != len(group.openings):
            _fail("gt_legacy_window_group_mismatch", ptr)
        if group.count == 0 and (group.sill_m is not None or group.head_m is not None):
            _fail("gt_legacy_window_empty_z", ptr)
        if group.count and (group.sill_m is None or group.head_m is None):
            _fail("gt_legacy_window_missing_z", ptr)
        floor = next(floor for floor in doc.floors if floor.name == group.floor)
        if any(opening.x_m < 0 or opening.x_m + opening.width_m > spans[group.facade] or
               opening.sill_m < floor.z_floor or opening.head_m > floor.z_floor + floor.ceiling_height
               for opening in group.openings):
            _fail("gt_legacy_opening_outside_span", ptr)
    for index, door in enumerate(doc.doors):
        if door.floor not in floor_names or door.x_m < 0 or door.x_m + door.width_m > spans[door.facade]:
            _fail("gt_legacy_door_invalid", f"/doors/{index}")


def validate_gt_v3(doc: GroundTruthV3, *, tolerances: GtResolvedToolingTolerancesV1, expected_case: str | None = None) -> None:
    """Validate document-intrinsic v3 invariants using only its stored profile."""
    if expected_case is not None and doc.case != expected_case:
        _fail("gt_source_case_mismatch", "/case", expected=expected_case, actual=doc.case)
    if doc.generator.tolerances != tolerances:
        _fail("gt_profile_tolerances_mismatch", "/generator/tolerances")
    if any(_is_zero_hash(value) for value in (
        doc.generator.extractor_sha256, doc.generator.validator_sha256,
        doc.generator.vg_implementation_sha256, doc.generator.manifest_sha256,
        doc.generator.judge_config_sha256, doc.generator.vg_config_sha256,
    )):
        _fail("gt_source_zero_hash", "/generator")
    if doc.north_axis_deg is None:
        if doc.north_axis_source_refs:
            _fail("gt_source_north_refs_without_value", "/north_axis_source_refs")
    elif not doc.north_axis_source_refs or any(ref.role != "north_axis" for ref in doc.north_axis_source_refs):
        _fail("gt_source_north_refs_invalid", "/north_axis_source_refs")
    expected_methods = ["dxf_topology_roundtrip", "direct_gt_render", "overlay_on_original_drawing", "human_source_comparison"]
    if doc.verification.status == "candidate":
        if doc.verification.reviewer_id is not None or doc.verification.reviewed_on is not None or doc.verification.methods:
            _fail("gt_wire_candidate_verification_invalid", "/verification")
    elif (doc.verification.reviewer_id is None or doc.verification.reviewed_on is None or doc.verification.methods != expected_methods):
        _fail("gt_wire_verified_verification_invalid", "/verification")
    if doc.verification.reviewed_on is not None:
        try:
            date.fromisoformat(doc.verification.reviewed_on)
        except ValueError:
            _fail("gt_wire_verified_date_invalid", "/verification/reviewed_on")
    source_ids: set[str] = set()
    view_by_id: dict[str, GtSourceViewV3] = {}
    view_source_by_id: dict[str, str] = {}
    projection_view_by_key: dict[str, GtSourceViewV3] = {}
    for source in doc.sources:
        if source.id in source_ids or "/" in source.label or "\\" in source.label or ".." in source.label or _is_zero_hash(source.content_sha256):
            _fail("gt_source_invalid", "/sources")
        source_ids.add(source.id)
        if [view.id for view in source.views] != sorted(view.id for view in source.views):
            _fail("gt_wire_noncanonical_order", "/sources")
        for view in source.views:
            if view.id in view_by_id:
                _fail("gt_source_duplicate_view", "/sources")
            view_by_id[view.id] = view
            view_source_by_id[view.id] = source.id
            if view.kind == "elevation":
                key = view.projection_surface_key
                if key is None:
                    _fail("gt_source_elevation_surface_missing", "/sources")
                if key in projection_view_by_key:
                    _fail("gt_source_duplicate_projection_surface_key", "/sources")
                projection_view_by_key[key] = view
                if view.floor_ids != sorted(set(view.floor_ids)):
                    _fail("gt_wire_noncanonical_order", "/sources")
                if view.direction_semantics == "building_axis" and view.azimuth_deg is not None:
                    _fail("gt_source_elevation_direction_invalid", "/sources")
                if view.direction_semantics == "true_azimuth":
                    offset = {"North": 0, "East": 90, "South": 180, "West": 270}[view.facade_family]
                    if doc.north_axis_deg is None or view.azimuth_deg is None or view.azimuth_deg != (doc.north_axis_deg + offset) % 360:
                        _fail("gt_source_elevation_direction_invalid", "/sources")
                if (view.view_kind == "full" and view.world_along_coverage is not None) or (view.view_kind == "partial" and view.world_along_coverage is None):
                    _fail("gt_source_elevation_coverage_invalid", "/sources")
    if [source.id for source in doc.sources] != sorted(source.id for source in doc.sources):
        _fail("gt_wire_noncanonical_order", "/sources")

    def check_refs(refs: list[GtEntityRefV3], pointer: str) -> None:
        if refs != sorted(refs, key=_reference_key) or len({_reference_key(ref) for ref in refs}) != len(refs):
            _fail("gt_wire_noncanonical_order", pointer)
        for ref in refs:
            view = view_by_id.get(ref.view_id)
            if ref.source_id not in source_ids or view is None or view_source_by_id[ref.view_id] != ref.source_id:
                _fail("gt_source_reference_missing", pointer)
            if ref.role in {"footprint_boundary", "zone_boundary", "opening_plan"} and view.kind != "plan":
                _fail("gt_source_reference_view_kind", pointer)
            if ref.role == "opening_elevation" and view.kind != "elevation":
                _fail("gt_source_reference_view_kind", pointer)
    check_refs(doc.north_axis_source_refs, "/north_axis_source_refs")
    floor_ids = [floor.id for floor in doc.floors]
    if len(set(floor_ids)) != len(floor_ids) or doc.floors != sorted(doc.floors, key=lambda f: (f.z_floor_m, f.id)):
        _fail("gt_wire_noncanonical_order", "/floors")
    prior_top = -math.inf
    canonical_footprint: tuple[tuple[float, float], ...] | None = None
    all_segments: dict[str, GtBoundarySegmentV3] = {}
    all_zones: dict[str, tuple[str, GtZoneV3]] = {}
    vg_tolerances = VisibilityTolerances(tolerances.vg_depth_epsilon_m, tolerances.vg_endpoint_epsilon_m)
    for f_index, floor in enumerate(doc.floors):
        f_ptr = f"/floors/{f_index}"
        if floor.z_floor_m < prior_top:
            _fail("gt_profile_floor_overlap", f_ptr)
        prior_top = floor.z_floor_m + floor.ceiling_height_m
        ring = _ring_vertices(floor.footprint.exterior, f_ptr + "/footprint/exterior")
        if floor.footprint.interior_rings:
            _fail("gt_profile_holes_unsupported", f_ptr + "/footprint/interior_rings")
        if canonical_footprint is None:
            canonical_footprint = ring
        elif not _ordered_rings_equal_within_tolerance(
                canonical_footprint, ring, tolerances.dxf_node_join_tolerance_m):
            _fail("gt_profile_floor_footprint_mismatch", f_ptr + "/footprint")
        fingerprint = footprint_fingerprint(list(ring))
        if floor.footprint_fingerprint != fingerprint:
            _fail("gt_hash_footprint_mismatch", f_ptr + "/footprint_fingerprint")
        if [zone.id for zone in floor.zones] != sorted(zone.id for zone in floor.zones):
            _fail("gt_wire_noncanonical_order", f_ptr + "/zones")
        for zone in floor.zones:
            if zone.id in all_zones:
                _fail("gt_zone_duplicate_id", f_ptr + "/zones")
            _ring_vertices(zone.polygon.exterior, f_ptr + "/zones")
            if zone.polygon.interior_rings:
                _fail("gt_profile_holes_unsupported", f_ptr + "/zones")
            check_refs(zone.source_refs, f_ptr + "/zones")
            all_zones[zone.id] = (floor.id, zone)
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        footprint_polygon = Polygon(ring)
        if footprint_polygon.area <= tolerances.dxf_topology_area_tolerance_m2:
            _fail("gt_polygon_area_too_small", f_ptr + "/footprint")
        zone_polygons = [Polygon(_ring_vertices(zone.polygon.exterior, f_ptr + "/zones")) for zone in floor.zones]
        if any(polygon.area <= tolerances.dxf_topology_area_tolerance_m2 for polygon in zone_polygons):
            _fail("gt_polygon_area_too_small", f_ptr + "/zones")
        if any(not polygon.within(footprint_polygon) for polygon in zone_polygons):
            _fail("gt_zone_outside_footprint", f_ptr + "/zones")
        if any(a.intersection(b).area > tolerances.dxf_topology_area_tolerance_m2
               for index, a in enumerate(zone_polygons) for b in zone_polygons[index + 1:]):
            _fail("gt_zone_overlap", f_ptr + "/zones")
        if unary_union(zone_polygons).symmetric_difference(footprint_polygon).area > tolerances.dxf_topology_area_tolerance_m2:
            _fail("gt_zone_tiling_mismatch", f_ptr + "/zones")
        if floor.boundary_segments != sorted(floor.boundary_segments, key=_segment_sort_key):
            _fail("gt_wire_noncanonical_order", f_ptr + "/boundary_segments")
        try:
            derived = [item for direction in ((0, 1), (0, -1), (1, 0), (-1, 0))
                       for item in vg_for_direction(list(ring), direction, tolerances=vg_tolerances)]
        except ValueError as exc:
            _fail("gt_boundary_vg_invalid", f_ptr + "/footprint", reason=str(exc))
        if len(derived) != len(floor.boundary_segments):
            _fail("gt_boundary_segments_wire_mismatch", f_ptr + "/boundary_segments")
        observed = {(segment.facade_family, tuple(segment.p1), tuple(segment.p2)): segment for segment in floor.boundary_segments}
        for segment in floor.boundary_segments:
            if segment.id in all_segments or segment.floor_id != floor.id or tuple(segment.outward_normal) != _family_normal(segment.facade_family):
                _fail("gt_boundary_invalid", f_ptr + "/boundary_segments")
            if segment.source_footprint_fingerprint != fingerprint or not _intervals_ordered(segment.visible_intervals):
                _fail("gt_boundary_segments_wire_mismatch", f_ptr + "/boundary_segments")
            p1, p2 = segment.p1, segment.p2
            if p1 == p2 or (p1[0] != p2[0] and p1[1] != p2[1]):
                _fail("gt_boundary_invalid", f_ptr + "/boundary_segments")
            along = sorted((p1[0], p2[0])) if p1[1] == p2[1] else sorted((p1[1], p2[1]))
            if (segment.world_along_interval.lo, segment.world_along_interval.hi) != tuple(along):
                _fail("gt_boundary_invalid", f_ptr + "/boundary_segments")
            if segment.projection_surface_keys != sorted(set(segment.projection_surface_keys)):
                _fail("gt_wire_noncanonical_order", f_ptr + "/boundary_segments")
            check_refs(segment.source_refs, f_ptr + "/boundary_segments")
            for key in segment.projection_surface_keys:
                view = projection_view_by_key.get(key)
                if view is None or view.facade_family != segment.facade_family or floor.id not in view.floor_ids:
                    _fail("gt_boundary_projection_surface_invalid", f_ptr + "/boundary_segments")
            all_segments[segment.id] = segment
        # Vg frames are compared by public geometry, not private helper hash.
        for item in derived:
            frame = item.frame
            key = (frame.facade_family, tuple(frame.p1), tuple(frame.p2))
            segment = observed.get(key)
            if segment is None or segment.depth != frame.depth or [(v.lo, v.hi) for v in segment.visible_intervals] != list(item.visible_intervals):
                _fail("gt_boundary_segments_wire_mismatch", f_ptr + "/boundary_segments")
    if [opening.id for opening in doc.openings] != [opening.id for opening in sorted(doc.openings, key=lambda o: (o.floor_id, o.boundary_segment_id, o.world_along_interval.lo, o.kind, o.id))]:
        _fail("gt_wire_noncanonical_order", "/openings")
    seen_openings: set[str] = set()
    for index, opening in enumerate(doc.openings):
        ptr = f"/openings/{index}"
        segment = all_segments.get(opening.boundary_segment_id)
        if opening.id in seen_openings or segment is None or segment.floor_id != opening.floor_id:
            _fail("gt_opening_invalid_reference", ptr)
        seen_openings.add(opening.id)
        if not (segment.world_along_interval.lo <= opening.world_along_interval.lo < opening.world_along_interval.hi <= segment.world_along_interval.hi):
            _fail("gt_opening_outside_segment", ptr)
        if opening.world_along_interval.hi - opening.world_along_interval.lo <= tolerances.vg_endpoint_epsilon_m:
            _fail("gt_opening_width_too_small", ptr)
        zone_entry = all_zones.get(opening.host_zone_id or "")
        if opening.host_zone_id is None or zone_entry is None or zone_entry[0] != opening.floor_id:
            _fail("gt_opening_invalid_host_zone", ptr)
        check_refs(opening.source_refs, ptr + "/source_refs")
        plan_refs = [ref for ref in opening.source_refs if ref.role == "opening_plan"]
        elevation_refs = [ref for ref in opening.source_refs if ref.role == "opening_elevation"]
        if not plan_refs:
            _fail("gt_opening_plan_source_missing", ptr + "/source_refs")
        floor = next(floor for floor in doc.floors if floor.id == opening.floor_id)
        if opening.z_interval is not None and not (floor.z_floor_m <= opening.z_interval.lo < opening.z_interval.hi <= floor.z_floor_m + floor.ceiling_height_m):
            _fail("gt_opening_z_outside_floor", ptr)
        def overlap(a: GtWorldIntervalV3, b: GtWorldIntervalV3) -> bool:
            return min(a.hi, b.hi) > max(a.lo, b.lo)
        relevant: set[str] = set()
        for key in segment.projection_surface_keys:
            view = projection_view_by_key[key]
            coverage = view.world_along_coverage or segment.world_along_interval
            if any(overlap(opening.world_along_interval, visible) and overlap(visible, coverage)
                   and min(opening.world_along_interval.hi, visible.hi, coverage.hi) > max(opening.world_along_interval.lo, visible.lo, coverage.lo)
                   for visible in segment.visible_intervals):
                relevant.add(view.id)
        observed_elevation_views = {ref.view_id for ref in elevation_refs}
        if relevant:
            if opening.z_interval is None or observed_elevation_views != relevant:
                _fail("gt_opening_elevation_evidence_mismatch", ptr)
        elif opening.z_interval is not None or observed_elevation_views:
            _fail("gt_opening_plan_only_evidence_mismatch", ptr)
        def covers_opening_boundary_span(zone: GtZoneV3) -> bool:
            vertices = zone.polygon.exterior.vertices
            for start, end in zip(vertices, vertices[1:] + vertices[:1]):
                if segment.p1[1] == segment.p2[1] == start[1] == end[1]:
                    lo, hi = sorted((start[0], end[0]))
                    if lo <= opening.world_along_interval.lo and opening.world_along_interval.hi <= hi:
                        return True
                if segment.p1[0] == segment.p2[0] == start[0] == end[0]:
                    lo, hi = sorted((start[1], end[1]))
                    if lo <= opening.world_along_interval.lo and opening.world_along_interval.hi <= hi:
                        return True
            return False
        hosts = [zone_id for zone_id, (zone_floor_id, zone) in all_zones.items()
                 if zone_floor_id == opening.floor_id and covers_opening_boundary_span(zone)]
        if hosts != [opening.host_zone_id]:
            _fail("gt_opening_host_zone_boundary_mismatch", ptr)
    computed = compute_gt_v3_content_sha256(doc)
    if doc.content_sha256 != computed:
        _fail("gt_hash_content_mismatch", "/content_sha256", expected=computed)


def _normalise(value: Any) -> Any:
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    return value


def canonical_gt_v3_payload(doc: GroundTruthV3) -> dict[str, JsonValue]:
    payload = doc.model_dump(mode="json")
    payload["content_sha256"] = "0" * 64
    return _normalise(payload)


def canonical_gt_v3_bytes(doc: GroundTruthV3) -> bytes:
    payload = canonical_gt_v3_payload(doc)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n").hexdigest()
    final = doc.model_dump(mode="json")
    final["content_sha256"] = digest
    return json.dumps(_normalise(final), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def compute_gt_v3_content_sha256(doc: GroundTruthV3) -> str:
    return hashlib.sha256(json.dumps(canonical_gt_v3_payload(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n").hexdigest()


def stable_boundary_segment_id(floor_id: str, facade_family: str, p1: Point2, p2: Point2) -> str:
    payload = {"schema": "gt_boundary_segment_geometry_v1", "floor_id": floor_id,
               "facade_family": facade_family, "p1": list(p1), "p2": list(p2)}
    value = json.dumps(_normalise(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return f"{floor_id}:boundary:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _protected_candidate_path(out: Path) -> bool:
    candidate = Path(out)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    suffix: list[str] = []
    parent = candidate.parent
    while not parent.exists():
        suffix.append(parent.name)
        parent = parent.parent
    resolved = parent.resolve(strict=True).joinpath(*reversed(suffix), candidate.name)
    # Keep the candidate writer aligned with the active default root even in
    # isolated tooling tests that replace it.  The local import avoids the
    # schema/loader import cycle at module import time.
    from .gt import DEFAULT_GT_DIR
    protected = [Path(DEFAULT_GT_DIR), REPO_ROOT / "case_tests/test_baseline/gt_sources"]
    if any(resolved.is_relative_to(path.resolve()) for path in protected):
        return True
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return False
    # This is a path-shape policy, not an inventory of existing case folders:
    # it protects `e2e_tests/<any-case>/case_data/**` before that case exists.
    return (len(relative.parts) >= 4
            and relative.parts[:2] == ("case_tests", "e2e_tests")
            and relative.parts[3] == "case_data")


def write_gt_v3_candidate(doc: GroundTruthV3, out: Path, *, overwrite: Literal[False] = False) -> None:
    if overwrite is not False:
        _fail("gt_candidate_overwrite_forbidden", "/out")
    if doc.verification.status != "candidate":
        _fail("gt_candidate_status_required", "/verification/status")
    out = Path(out)
    if out.exists():
        _fail("gt_candidate_exists", "/out")
    if _protected_candidate_path(out):
        _fail("gt_candidate_protected_path", "/out")
    out.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_gt_v3_bytes(doc)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            # mkstemp already creates a private file on POSIX. Windows uses
            # the destination directory's ACL; fchmod is not available there.
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-read wire/hash before atomic replacement; import locally avoids loader cycle.
        reloaded = GroundTruthV3.model_validate_json(Path(temporary).read_bytes())
        validate_gt_v3(reloaded, tolerances=reloaded.generator.tolerances)
        if compute_gt_v3_content_sha256(reloaded) != reloaded.content_sha256:
            _fail("gt_hash_content_mismatch", "/content_sha256")
        os.replace(temporary, out)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compute_gt_implementation_hashes(repo_root: Path) -> GtImplementationHashesV1:
    root = Path(repo_root).resolve()
    groups = {
        "extractor_sha256": ["src/agent/judge/gt_extraction.py", "src/agent/judge/gt_manifest.py", "scripts/tool_scripts/gt_from_dxf.py"],
        "validator_sha256": ["src/agent/judge/gt_schema.py", "src/agent/judge/gt.py"],
        "vg_implementation_sha256": ["src/agent/correction/facade_visibility.py", "src/agent/correction/facade.py", "src/agent/correction/footprint.py", "src/agent/correction/schema.py"],
    }
    output: dict[str, str] = {}
    for key, paths in groups.items():
        material = bytearray()
        for relative in sorted(paths):
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"gt_implementation_file_missing: {relative}")
            material.extend(relative.encode("utf-8")); material.extend(b"\0")
            material.extend(path.read_bytes()); material.extend(b"\0")
        output[key] = hashlib.sha256(material).hexdigest()
    return GtImplementationHashesV1.model_validate(output)
