"""Manifest-bound, read-only DXF plan extraction for the GT v3 tooling.

This is deliberately only the Phase B plan core.  It creates a strict internal
``PlanExtractionResult``; segment/opening/elevation materialisation remains a
later phase and this module never writes a GT asset.
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import ezdxf
from ezdxf import bbox
from ezdxf.entities import DXFTagStorage
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize_full, unary_union

from src.agent.correction.footprint import footprint_fingerprint

from .gt_manifest import (ElevationViewBindingV1, EntityLocatorV1,
                          EntitySelectorV1, GtExtractionManifestV1,
                          PlanViewBindingV1, validate_manifest_view_clips)
from .gt_schema import (DxfHandle, GtEntityRefV3, GtImplementationHashesV1,
                        GtBoundarySegmentV3, GtFloorV3, GtGeneratorV3, GtZoneV3,
                        GtOpeningV3, GtPolygonV3, GtResolvedToolingConfigV1,
                        GtSourceDocumentV3, GtSourceViewV3, GtRingV3,
                        GtVerificationV3, GtWorldIntervalV3, GroundTruthV3,
                        Hex64, JsonValue, StableId, StrictNonNegativeInt,
                        _ordered_rings_equal_within_tolerance,
                        canonical_gt_v3_bytes, compute_gt_v3_content_sha256,
                        stable_boundary_segment_id, validate_gt_v3)
from src.agent.correction.facade_visibility import VisibilityTolerances, vg_for_direction


class ExtractionError(ValueError):
    """Stable, concise failure code for a fail-closed extraction condition."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DxfInspectionIssueV1(_StrictModel):
    code: str
    severity: Literal["BLOCK", "UNBOUND", "INFO"]
    view_id: StableId | None
    entity_handles: list[DxfHandle] = Field(default_factory=list)
    context: dict[str, JsonValue] = Field(default_factory=dict)


class DxfViewInspectionV1(_StrictModel):
    view_id: StableId
    kind: Literal["plan", "elevation", "unbound_cluster"]
    entity_count_by_type: dict[str, StrictNonNegativeInt]
    entity_count_by_layer: dict[str, StrictNonNegativeInt]
    matched_handles: list[DxfHandle]
    polygon_count: StrictNonNegativeInt
    dangle_count: StrictNonNegativeInt
    cut_count: StrictNonNegativeInt
    invalid_ring_count: StrictNonNegativeInt


class DxfInspectionReportV1(_StrictModel):
    report_version: Literal[1]
    status: Literal["PASS", "UNBOUND", "BLOCKED"]
    source_dxf_sha256: Hex64
    native_units_observed: str
    manifest_sha256: Hex64 | None
    judge_config_sha256: Hex64
    vg_config_sha256: Hex64
    proxy_entity_count: StrictNonNegativeInt
    views: list[DxfViewInspectionV1]
    issues: list[DxfInspectionIssueV1]


@dataclass(frozen=True)
class InspectionInputs:
    dxf_path: Path
    manifest: GtExtractionManifestV1 | None
    tooling: GtResolvedToolingConfigV1
    implementation_hashes: GtImplementationHashesV1


@dataclass(frozen=True)
class ExtractionInputs:
    dxf_path: Path
    manifest: GtExtractionManifestV1
    tooling: GtResolvedToolingConfigV1
    implementation_hashes: GtImplementationHashesV1


@dataclass(frozen=True)
class PlanZoneExtraction:
    id: str
    name: str
    role: str
    polygon: GtPolygonV3
    source_refs: tuple[GtEntityRefV3, ...]


@dataclass(frozen=True)
class PlanFloorExtraction:
    id: str
    name: str
    z_floor_m: float
    ceiling_height_m: float
    footprint: GtPolygonV3
    footprint_fingerprint: str
    zones: tuple[PlanZoneExtraction, ...]
    boundary_edge_sources: Mapping[tuple[tuple[float, float], tuple[float, float]], tuple[GtEntityRefV3, ...]]


@dataclass(frozen=True)
class PlanExtractionResult:
    case: str
    source: GtSourceDocumentV3
    floors: tuple[PlanFloorExtraction, ...]


_UNITS = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
_UNIT_SCALE = {"m": 1.0, "mm": 0.001, "cm": 0.01, "in": 0.0254, "ft": 0.3048}
_SEVERITY = {"BLOCK": 0, "UNBOUND": 1, "INFO": 2}


def _fail(code: str) -> None:
    raise ExtractionError(code)


def _handle(entity) -> str:
    return str(entity.dxf.handle).upper()


def _inside(point: tuple[float, float], view: PlanViewBindingV1) -> bool:
    box = view.clip_box_dxf
    return box.xmin < point[0] < box.xmax and box.ymin < point[1] < box.ymax


def _clip_membership(point: tuple[float, float], view: PlanViewBindingV1) -> Literal["inside", "edge", "outside"]:
    box = view.clip_box_dxf
    if point[0] in (box.xmin, box.xmax) or point[1] in (box.ymin, box.ymax):
        return "edge"
    return "inside" if _inside(point, view) else "outside"


def _entity_points(entity) -> list[tuple[float, float]]:
    kind = entity.dxftype()
    if kind == "LINE":
        return [(float(entity.dxf.start.x), float(entity.dxf.start.y)),
                (float(entity.dxf.end.x), float(entity.dxf.end.y))]
    if kind == "LWPOLYLINE":
        if entity.has_arc:
            _fail("dxf_bulge_unsupported")
        return [(float(x), float(y)) for x, y, *_ in entity.get_points("xy")]
    if kind == "POLYLINE":
        return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
    _fail("dxf_entity_type_unsupported")


def _selector_entities(msp, selector: EntitySelectorV1, view: PlanViewBindingV1) -> list:
    selected = []
    wanted = set(selector.handles)
    for entity in msp:
        if entity.dxftype() not in selector.entity_types or entity.dxf.layer not in selector.layers:
            continue
        if selector.handle_mode == "only_listed" and _handle(entity) not in wanted:
            continue
        points = _entity_points(entity)
        if not points:
            _fail("dxf_empty_entity")
        membership = [_clip_membership(point, view) for point in points]
        if "edge" in membership:
            _fail("dxf_entity_clip_boundary")
        if "inside" in membership and "outside" in membership:
            _fail("dxf_entity_clip_ambiguous")
        if "inside" not in membership:
            continue
        selected.append(entity)
    if selector.handle_mode == "only_listed" and {_handle(e) for e in selected} != wanted:
        _fail("dxf_selector_handle_missing")
    if len(selected) < selector.min_count or (selector.max_count is not None and len(selected) > selector.max_count):
        _fail("dxf_selector_count_mismatch")
    return sorted(selected, key=_handle)


# Multi-plan world-transform exit snap (S1, 2026-08-22 dispatch).
#
# The converter quantises wall geometry on its declared native grid
# (q = tau_node/10, e.g. 0.1 mm for mm-native sources) BEFORE the per-view
# world affine is applied here.  Two plan views that draw the same outline at
# different sheet positions carry different affine offsets (m02/m12), so the
# same mathematical world coordinate is evaluated through two different float
# sums and can differ by pure re-association noise (measured 3.55e-15 m on
# sm25 at |world| <= ~55 m).  Multi-floor consumers compare footprints and
# extents with strict bitwise equality (footprint_fingerprint; Va's per-floor
# along-extent equality), so that noise must not survive the transform.
#
# 1e-9 m reuses the already-signed pairing-noise criterion
# (``tarch_normalize._PAIRING_Z_TOLERANCE_M``) and its argument form: it sits
# 5 orders of magnitude BELOW the declared native quantisation step expressed
# in world metres (0.1 mm = 1e-4 m; 6 orders below the 1 mm node-join
# tolerance), so it can never absorb a real geometric difference the pipeline
# can represent; and ~6 orders of magnitude ABOVE the measured re-association
# noise floor (3.55e-15 m), so it deterministically collapses that noise.
_WORLD_SNAP_GRID_M = 1e-9

# Fresh-process mutation seam (same shape as tarch_normalize.TARCH_NEUTER_GATE):
# unwires the snap so the canonical gate suite can prove locks 1/2 depend on it.
_NEUTER_WORLD_SNAP_ENV = "GT_NEUTER_WORLD_SNAP"


def _plan_world_snap_grid(manifest: GtExtractionManifestV1 | None) -> float | None:
    """Grid for the world-transform exit snap, or None on the frozen single-plan path.

    The cross-affine re-association noise this snap removes exists exactly when
    one case maps geometry into the same world region through two or more plan
    affines; with a single plan view there is no second affine to disagree
    with, so the single-plan path stays byte-frozen (signed single-floor
    answers must rebuild identically).
    """
    if manifest is None or os.environ.get(_NEUTER_WORLD_SNAP_ENV):
        return None
    plan_views = [view for view in manifest.views if isinstance(view, PlanViewBindingV1)]
    return _WORLD_SNAP_GRID_M if len(plan_views) > 1 else None


def _transform(point: tuple[float, float], view: PlanViewBindingV1, metres_per_unit: float,
               snap_grid_m: float | None = None) -> tuple[float, float]:
    x, y = point[0] * metres_per_unit, point[1] * metres_per_unit
    affine = view.world_from_source_m
    world_x = affine.m00 * x + affine.m01 * y + affine.m02
    world_y = affine.m10 * x + affine.m11 * y + affine.m12
    if snap_grid_m is None:
        return (world_x, world_y)
    # Deterministic nearest-lattice rounding.  Grid-quantised inputs sit on
    # integer multiples of 1e5 lattice units (0.1 mm = 1e5 nm), so the quotient
    # is within float error (~1e-5 lattice units) of an integer and never lands
    # on a .5 boundary; both floors evaluate the identical rounding path.
    return (round(world_x / snap_grid_m) * snap_grid_m,
            round(world_y / snap_grid_m) * snap_grid_m)


def _segments(entities, view: PlanViewBindingV1, metres_per_unit: float, role: str,
              snap_grid_m: float | None = None):
    output: list[tuple[tuple[float, float], tuple[float, float], GtEntityRefV3]] = []
    for entity in entities:
        points = [_transform(p, view, metres_per_unit, snap_grid_m) for p in _entity_points(entity)]
        if len(points) < 2:
            _fail("dxf_empty_entity")
        if entity.dxftype() == "LWPOLYLINE" and entity.closed and points[0] != points[-1]:
            points.append(points[0])
        for index, (start, end) in enumerate(zip(points, points[1:])):
            output.append((start, end, GtEntityRefV3(source_id="pending", view_id=view.id,
                entity_handle=_handle(entity), subentity_index=index if len(points) > 2 else None, role=role)))
    return output


def _snap_segments(segments, node_join_tolerance: float, axis_alignment_tolerance: float):
    points = sorted({p for start, end, _ in segments for p in (start, end)})
    groups: list[list[tuple[float, float]]] = []
    for point in points:
        hits = [group for group in groups if any((point[0] - p[0]) ** 2 + (point[1] - p[1]) ** 2 <= node_join_tolerance ** 2 for p in group)]
        if len(hits) > 1:
            merged = [p for group in hits for p in group] + [point]
            for group in hits: groups.remove(group)
            groups.append(merged)
        elif hits: hits[0].append(point)
        else: groups.append([point])
    mapping = {}
    for group in groups:
        if max((a[0]-b[0])**2 + (a[1]-b[1])**2 for a in group for b in group) > node_join_tolerance ** 2:
            _fail("dxf_node_snap_component_too_wide")
        representative = min(group)
        mapping.update({point: representative for point in group})
    snapped = []
    for start, end, ref in segments:
        start, end = mapping[start], mapping[end]
        dx, dy = end[0] - start[0], end[1] - start[1]
        if abs(dx) <= axis_alignment_tolerance < abs(dy): end = (start[0], end[1])
        elif abs(dy) <= axis_alignment_tolerance < abs(dx): end = (end[0], start[1])
        elif abs(dx) <= axis_alignment_tolerance and abs(dy) <= axis_alignment_tolerance: _fail("dxf_short_edge")
        elif abs(dx) > axis_alignment_tolerance and abs(dy) > axis_alignment_tolerance: _fail("dxf_nonorthogonal_edge")
        snapped.append((start, end, ref))
    return snapped


def _polygonize(segments, tolerance: float, diagnostics: dict[str, int] | None = None) -> list[Polygon]:
    lines = [LineString([start, end]) for start, end, _ in segments]
    polygons, cuts, dangles, invalid = polygonize_full(unary_union(lines))
    if diagnostics is not None:
        diagnostics.update(dangle_count=len(dangles.geoms), cut_count=len(cuts.geoms), invalid_ring_count=len(invalid.geoms))
    if not dangles.is_empty or not cuts.is_empty or not invalid.is_empty:
        _fail("dxf_polygonize_residual")
    result = list(polygons.geoms)
    if any(poly.area <= tolerance for poly in result):
        _fail("dxf_polygonize_sliver")
    return result


def _canonical_polygon(poly: Polygon) -> GtPolygonV3:
    if poly.geom_type != "Polygon" or poly.interiors:
        _fail("dxf_polygon_profile_unsupported")
    coords = [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]
    if not poly.exterior.is_ccw:
        coords.reverse()
    # Remove collinear vertices only; no tolerance rewrite happens here.
    changed = True
    while changed:
        changed = False
        for index, current in enumerate(coords):
            previous, following = coords[index - 1], coords[(index + 1) % len(coords)]
            if (previous[0] == current[0] == following[0]) or (previous[1] == current[1] == following[1]):
                coords.pop(index); changed = True; break
    coords = coords[coords.index(min(coords)):] + coords[:coords.index(min(coords))]
    return GtPolygonV3(exterior=GtRingV3(vertices=[[x, y] for x, y in coords]), interior_rings=[])


def _refs_for_ring(ring: GtPolygonV3, segments, source_id: str, role: str):
    answer = {}
    vertices = [tuple(p) for p in ring.exterior.vertices]
    for start, end in zip(vertices, vertices[1:] + vertices[:1]):
        refs = []
        for a, b, ref in segments:
            horizontal = a[1] == b[1] == start[1] == end[1] and min(max(a[0], b[0]), max(start[0], end[0])) > max(min(a[0], b[0]), min(start[0], end[0]))
            vertical = a[0] == b[0] == start[0] == end[0] and min(max(a[1], b[1]), max(start[1], end[1])) > max(min(a[1], b[1]), min(start[1], end[1]))
            if horizontal or vertical:
                refs.append(ref.model_copy(update={"source_id": source_id, "role": role}))
        if not refs:
            _fail("dxf_boundary_ancestry_missing")
        answer[(start, end)] = tuple(sorted(refs, key=lambda r: (r.entity_handle, r.subentity_index or -1)))
    return answer


def _source(manifest: GtExtractionManifestV1) -> GtSourceDocumentV3:
    views = []
    for view in sorted(manifest.views, key=lambda value: value.id):
        if isinstance(view, PlanViewBindingV1):
            views.append(GtSourceViewV3(id=view.id, kind="plan", floor_ids=[view.floor_id], projection_surface_key=None,
                facade_family=None, view_kind=None, world_along_coverage=None, direction_semantics=None, azimuth_deg=None))
        else:
            views.append(GtSourceViewV3(id=view.id, kind="elevation", floor_ids=list(view.floor_ids),
                projection_surface_key=view.projection_surface_key, facade_family=view.facade_family,
                view_kind=view.view_kind, world_along_coverage=view.world_along_coverage,
                direction_semantics=view.direction_semantics, azimuth_deg=view.azimuth_deg))
    return GtSourceDocumentV3(id=manifest.source_id, kind="dxf", label=manifest.source_dxf_label,
        content_sha256=manifest.source_dxf_sha256, native_units=manifest.native_units,
        metres_per_unit=manifest.metres_per_unit, views=views)


def _check_input(inputs: InspectionInputs):
    path = Path(inputs.dxf_path)
    # Keep the raw source hash as provenance; §5.4 order independence is an
    # extractor property, not a DXF reserialization that changes this hash.
    raw = path.read_bytes()
    doc = ezdxf.readfile(path)
    unit = _UNITS.get(doc.header.get("$INSUNITS", 0), str(doc.header.get("$INSUNITS", 0)))
    if inputs.manifest:
        manifest = inputs.manifest
        if hashlib.sha256(raw).hexdigest() != manifest.source_dxf_sha256: _fail("dxf_source_hash_mismatch")
        if unit != manifest.native_units: _fail("dxf_unit_mismatch")
        if unit != "unitless" and _UNIT_SCALE.get(unit) != manifest.metres_per_unit: _fail("dxf_unit_scale_mismatch")
        try:
            validate_manifest_view_clips(manifest, topology_area_tolerance_m2=inputs.tooling.tolerances.dxf_topology_area_tolerance_m2)
        except ValueError as error:
            _fail(str(error))
        if any(view.boundary_reference != "outer_skin" for view in manifest.views if isinstance(view, PlanViewBindingV1)):
            _fail("dxf_centerline_unsupported_in_phase_b")
    return doc, unit


def inspect_extraction_inputs(inputs: InspectionInputs) -> DxfInspectionReportV1:
    issues: list[DxfInspectionIssueV1] = []
    try:
        doc, unit = _check_input(inputs)
    except ExtractionError as error:
        issues.append(DxfInspectionIssueV1(code=str(error), severity="BLOCK", view_id=None))
        doc = ezdxf.readfile(inputs.dxf_path)
        unit = _UNITS.get(doc.header.get("$INSUNITS", 0), "unknown")
    proxy_entities = [entity for entity in doc.modelspace() if isinstance(entity, DXFTagStorage) or entity.dxftype() in {"ACAD_PROXY_ENTITY", "ACAD_PROXY_GRAPHIC"}]
    proxy_count = len(proxy_entities)
    if inputs.manifest is None:
        issues.append(DxfInspectionIssueV1(code="dxf_manifest_required", severity="UNBOUND", view_id=None))
        views = []
    else:
        views = []
        snap_grid_m = _plan_world_snap_grid(inputs.manifest)
        for view in sorted((v for v in inputs.manifest.views if isinstance(v, PlanViewBindingV1)), key=lambda v: v.id):
            diagnostics = {"dangle_count": 0, "cut_count": 0, "invalid_ring_count": 0}
            try:
                footprint = _selector_entities(doc.modelspace(), view.footprint_boundary, view)
                segments = _snap_segments(_segments(footprint, view, inputs.manifest.metres_per_unit, "footprint_boundary", snap_grid_m), inputs.tooling.tolerances.dxf_node_join_tolerance_m, inputs.tooling.tolerances.dxf_axis_alignment_tolerance_m)
                polygons = _polygonize(segments, inputs.tooling.tolerances.dxf_topology_area_tolerance_m2, diagnostics)
                zone = _selector_entities(doc.modelspace(), view.zone_boundaries, view)
                _polygonize(_snap_segments(_segments(footprint + zone, view, inputs.manifest.metres_per_unit, "zone_boundary", snap_grid_m), inputs.tooling.tolerances.dxf_node_join_tolerance_m, inputs.tooling.tolerances.dxf_axis_alignment_tolerance_m), inputs.tooling.tolerances.dxf_topology_area_tolerance_m2, diagnostics)
                counts = Counter(e.dxftype() for e in footprint); layers = Counter(e.dxf.layer for e in footprint)
                views.append(DxfViewInspectionV1(view_id=view.id, kind="plan", entity_count_by_type=dict(counts), entity_count_by_layer=dict(layers), matched_handles=[_handle(e) for e in footprint], polygon_count=len(polygons), **diagnostics))
            except ExtractionError as error:
                issues.append(DxfInspectionIssueV1(code=str(error), severity="BLOCK", view_id=view.id))
                views.append(DxfViewInspectionV1(view_id=view.id, kind="plan", entity_count_by_type={}, entity_count_by_layer={}, matched_handles=[], polygon_count=0, **diagnostics))
            outside_proxy_handles: set[str] = set()
            for entity in proxy_entities:
                try:
                    extent = bbox.extents([entity], fast=True)
                    if not extent.has_data:
                        raise ValueError("proxy extent unavailable")
                    clip = view.clip_box_dxf
                    inside = (extent.extmin.x < clip.xmax and extent.extmax.x > clip.xmin
                              and extent.extmin.y < clip.ymax and extent.extmax.y > clip.ymin)
                except Exception:
                    issues.append(DxfInspectionIssueV1(code="dxf_proxy_extent_unavailable", severity="BLOCK", view_id=view.id, entity_handles=[_handle(entity)]))
                    continue
                if inside:
                    issues.append(DxfInspectionIssueV1(code="dxf_requires_graphics_export", severity="BLOCK", view_id=view.id, entity_handles=[_handle(entity)]))
                else:
                    outside_proxy_handles.add(_handle(entity))
            for handle in sorted(outside_proxy_handles):
                issues.append(DxfInspectionIssueV1(code="dxf_proxy_outside_bound_views", severity="INFO", view_id=view.id, entity_handles=[handle]))
    if proxy_count and inputs.manifest is None:
        issues.append(DxfInspectionIssueV1(code="dxf_requires_graphics_export", severity="INFO", view_id=None))
    issues.sort(key=lambda i: (_SEVERITY[i.severity], i.view_id or "", i.code, i.entity_handles))
    status = "BLOCKED" if any(i.severity == "BLOCK" for i in issues) else ("UNBOUND" if inputs.manifest is None else "PASS")
    return DxfInspectionReportV1(report_version=1, status=status, source_dxf_sha256=hashlib.sha256(Path(inputs.dxf_path).read_bytes()).hexdigest(), native_units_observed=unit, manifest_sha256=inputs.manifest.manifest_sha256 if inputs.manifest else None, judge_config_sha256=inputs.tooling.judge_config_sha256, vg_config_sha256=inputs.tooling.vg_config_sha256, proxy_entity_count=proxy_count, views=views, issues=issues)


def extract_plan_geometry(inputs: ExtractionInputs) -> PlanExtractionResult:
    report = inspect_extraction_inputs(InspectionInputs(inputs.dxf_path, inputs.manifest, inputs.tooling, inputs.implementation_hashes))
    if report.status != "PASS": _fail(f"dxf_inspection_{report.status.lower()}")
    doc, _ = _check_input(inputs)
    floors = []
    reference_ring = None
    floor_info = {floor.id: floor for floor in inputs.manifest.floors}
    snap_grid_m = _plan_world_snap_grid(inputs.manifest)
    for view in sorted((v for v in inputs.manifest.views if isinstance(v, PlanViewBindingV1)), key=lambda v: v.floor_id):
        footprint_segments = _snap_segments(_segments(_selector_entities(doc.modelspace(), view.footprint_boundary, view), view, inputs.manifest.metres_per_unit, "footprint_boundary", snap_grid_m), inputs.tooling.tolerances.dxf_node_join_tolerance_m, inputs.tooling.tolerances.dxf_axis_alignment_tolerance_m)
        footprint_faces = _polygonize(footprint_segments, inputs.tooling.tolerances.dxf_topology_area_tolerance_m2)
        containing = [face for face in footprint_faces if all(face.contains(Point(seed.point_world_m)) for seed in view.zone_seeds)]
        if len(containing) != 1: _fail("dxf_footprint_seed_ambiguous")
        footprint = _canonical_polygon(containing[0])
        ring = tuple(tuple(p) for p in footprint.exterior.vertices)
        if reference_ring is None: reference_ring = ring
        elif not _ordered_rings_equal_within_tolerance(
                reference_ring, ring, inputs.tooling.tolerances.dxf_node_join_tolerance_m):
            _fail("dxf_profile_floor_footprint_mismatch")
        zone_entities = _selector_entities(doc.modelspace(), view.zone_boundaries, view)
        zone_segments = _snap_segments(_segments(_selector_entities(doc.modelspace(), view.footprint_boundary, view) + zone_entities, view, inputs.manifest.metres_per_unit, "zone_boundary", snap_grid_m), inputs.tooling.tolerances.dxf_node_join_tolerance_m, inputs.tooling.tolerances.dxf_axis_alignment_tolerance_m)
        zone_faces = _polygonize(zone_segments, inputs.tooling.tolerances.dxf_topology_area_tolerance_m2)
        zones = []
        for face in zone_faces:
            owners = [seed for seed in view.zone_seeds if face.contains(Point(seed.point_world_m))]
            if len(owners) != 1: _fail("dxf_zone_seed_ambiguous")
            if face.boundary.distance(Point(owners[0].point_world_m)) <= inputs.tooling.tolerances.dxf_node_join_tolerance_m:
                _fail("dxf_zone_seed_near_boundary")
            if not containing[0].covers(face): _fail("dxf_zone_outside_footprint")
            seed = owners[0]; polygon = _canonical_polygon(face)
            refs = tuple(ref for refs in _refs_for_ring(polygon, zone_segments, inputs.manifest.source_id, "zone_boundary").values() for ref in refs)
            unique_refs = {(ref.source_id, ref.view_id, ref.entity_handle, ref.subentity_index, ref.role): ref for ref in refs}
            zones.append(PlanZoneExtraction(seed.zone_id, seed.name, seed.role, polygon, tuple(sorted(unique_refs.values(), key=lambda r: (r.entity_handle, r.subentity_index or -1)))))
        if {zone.id for zone in zones} != {seed.zone_id for seed in view.zone_seeds}: _fail("dxf_zone_seed_missing")
        union = unary_union([Polygon(zone.polygon.exterior.vertices) for zone in zones])
        if union.symmetric_difference(containing[0]).area > inputs.tooling.tolerances.dxf_topology_area_tolerance_m2: _fail("dxf_zone_tiling_mismatch")
        info = floor_info[view.floor_id]
        floors.append(PlanFloorExtraction(info.id, info.name, info.z_floor_m, info.ceiling_height_m, footprint,
            footprint_fingerprint(footprint.exterior.vertices), tuple(sorted(zones, key=lambda z: z.id)),
            _refs_for_ring(footprint, footprint_segments, inputs.manifest.source_id, "footprint_boundary")))
    return PlanExtractionResult(inputs.manifest.case, _source(inputs.manifest), tuple(floors))


# Phase C materialisation deliberately starts at PlanExtractionResult.  The
# helpers below never polygonize a plan or infer zones a second time.
_DIRECTIONS = ((0, 1), (0, -1), (1, 0), (-1, 0))


def _ref_key(ref: GtEntityRefV3) -> tuple[str, str, str, int, str]:
    return (ref.source_id, ref.view_id, ref.entity_handle,
            -1 if ref.subentity_index is None else ref.subentity_index, ref.role)


def _sorted_refs(refs: list[GtEntityRefV3]) -> list[GtEntityRefV3]:
    return sorted({_ref_key(ref): ref for ref in refs}.values(), key=_ref_key)


def _edge_refs(floor: PlanFloorExtraction, p1: tuple[float, float], p2: tuple[float, float]) -> list[GtEntityRefV3]:
    wanted = {p1, p2}
    for (left, right), refs in floor.boundary_edge_sources.items():
        if {left, right} == wanted:
            return _sorted_refs(list(refs))
    _fail("dxf_boundary_ancestry_missing")


def _is_horizontal(p1: tuple[float, float], p2: tuple[float, float]) -> bool:
    return p1[1] == p2[1]


def _normal_distance(span_axis: str, centre: tuple[float, float], segment: GtBoundarySegmentV3) -> float:
    return abs(centre[1] - segment.p1[1]) if span_axis == "x" else abs(centre[0] - segment.p1[0])


def _positive_collinear_overlap(a: tuple[float, float], b: tuple[float, float],
                                p1: tuple[float, float], p2: tuple[float, float]) -> bool:
    if _is_horizontal(a, b) != _is_horizontal(p1, p2):
        return False
    if _is_horizontal(a, b):
        return a[1] == p1[1] and min(max(a[0], b[0]), max(p1[0], p2[0])) > max(min(a[0], b[0]), min(p1[0], p2[0]))
    return a[0] == p1[0] and min(max(a[1], b[1]), max(p1[1], p2[1])) > max(min(a[1], b[1]), min(p1[1], p2[1]))


def _host_zones(floor: PlanFloorExtraction, segment: GtBoundarySegmentV3) -> list[str]:
    hits: list[str] = []
    for zone in floor.zones:
        vertices = [tuple(point) for point in zone.polygon.exterior.vertices]
        if any(_positive_collinear_overlap(a, b, tuple(segment.p1), tuple(segment.p2))
               for a, b in zip(vertices, vertices[1:] + vertices[:1])):
            hits.append(zone.id)
    return sorted(hits)


def _host_zones_for_opening(floor: PlanFloorExtraction, segment: GtBoundarySegmentV3,
                            interval: GtWorldIntervalV3) -> list[str]:
    """Return zones whose exterior edge covers this opening's *entire* span.

    Vg derives a facade segment from a footprint edge, so one long exterior
    segment can legitimately face several zones.  Segment-level host matching is
    therefore only a candidate filter; opening attachment must be decided at the
    opening interval.  No tolerance expansion is applied here: a window touching
    or crossing a zone join has no unique full-span host and must fail closed.
    """
    horizontal = _is_horizontal(tuple(segment.p1), tuple(segment.p2))
    hits: list[str] = []
    for zone in floor.zones:
        vertices = [tuple(point) for point in zone.polygon.exterior.vertices]
        for a, b in zip(vertices, vertices[1:] + vertices[:1]):
            if not _positive_collinear_overlap(a, b, tuple(segment.p1), tuple(segment.p2)):
                continue
            lo, hi = sorted((a[0], b[0]) if horizontal else (a[1], b[1]))
            if lo <= interval.lo and interval.hi <= hi:
                hits.append(zone.id)
                break
    return sorted(hits)


def _entity_by_locator(msp, locator: EntityLocatorV1):
    matches = [entity for entity in msp if _handle(entity) == locator.handle]
    if len(matches) != 1:
        _fail("dxf_locator_missing")
    return matches[0]


def _bbox_points(entity, locator: EntityLocatorV1) -> list[tuple[float, float]]:
    points = _entity_points(entity)
    if locator.subentity_index is not None:
        index = locator.subentity_index
        if index >= len(points) - 1:
            _fail("dxf_locator_subentity_missing")
        return [points[index], points[index + 1]]
    return points


def _bbox_for_locators(msp, locators: list[EntityLocatorV1], *, geometry_mode: str, role: str,
                       source_id: str, view_id: str, clip_box) -> tuple[tuple[float, float, float, float], list[GtEntityRefV3]]:
    points: list[tuple[float, float]] = []
    refs: list[GtEntityRefV3] = []
    for locator in locators:
        entity = _entity_by_locator(msp, locator)
        if geometry_mode == "virtual_entity_bbox":
            if entity.dxftype() != "INSERT" or len(locators) != 1:
                _fail("dxf_opening_virtual_entity_invalid")
            try:
                extent = bbox.extents([entity])
                if not extent.has_data:
                    raise ValueError("empty")
                entity_points = [(float(extent.extmin.x), float(extent.extmin.y)),
                                 (float(extent.extmax.x), float(extent.extmax.y))]
            except Exception:
                _fail("dxf_opening_virtual_bbox_invalid")
        elif entity.dxftype() not in {"LINE", "LWPOLYLINE", "POLYLINE"}:
            _fail("dxf_opening_entity_unsupported")
        else:
            entity_points = _bbox_points(entity, locator)
        if geometry_mode == "closed_outline_bbox" and (len(locators) != 1 or entity.dxftype() not in {"LWPOLYLINE", "POLYLINE"} or not entity.closed):
            _fail("dxf_opening_closed_outline_invalid")
        if not entity_points:
            _fail("dxf_opening_empty_bbox")
        points.extend(entity_points)
        refs.append(GtEntityRefV3(source_id=source_id, view_id=view_id, entity_handle=locator.handle,
                    subentity_index=locator.subentity_index, role=role))
    if geometry_mode == "grouped_line_bbox":
        lines = [LineString(_bbox_points(_entity_by_locator(msp, locator), locator)) for locator in locators]
        polygons, cuts, dangles, invalid = polygonize_full(unary_union(lines))
        if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty or len(polygons.geoms) != 1:
            _fail("dxf_opening_grouped_line_invalid")
        minx, miny, maxx, maxy = polygons.geoms[0].bounds
        points = [(minx, miny), (maxx, maxy)]
    if not points:
        _fail("dxf_opening_empty_bbox")
    if any(point[0] <= clip_box.xmin or point[0] >= clip_box.xmax or point[1] <= clip_box.ymin or point[1] >= clip_box.ymax for point in points):
        _fail("dxf_opening_clip_invalid")
    xs, ys = zip(*points)
    if min(xs) == max(xs) and min(ys) == max(ys):
        _fail("dxf_opening_empty_bbox")
    return (min(xs), min(ys), max(xs), max(ys)), _sorted_refs(refs)


def _plan_opening_geometry(msp, binding, view: PlanViewBindingV1, manifest: GtExtractionManifestV1) -> tuple[GtWorldIntervalV3, tuple[float, float], list[GtEntityRefV3]]:
    box, refs = _bbox_for_locators(msp, binding.entities, geometry_mode=binding.geometry_mode, role="opening_plan", source_id=manifest.source_id, view_id=view.id, clip_box=view.clip_box_dxf)
    corners = [_transform(point, view, manifest.metres_per_unit, _plan_world_snap_grid(manifest)) for point in ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3]))]
    xs, ys = zip(*corners)
    lo, hi = (min(xs), max(xs)) if binding.span_world_axis == "x" else (min(ys), max(ys))
    if hi - lo <= 0:
        _fail("dxf_opening_span_too_small")
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    return GtWorldIntervalV3(lo=lo, hi=hi), centre, refs


def _elevation_geometry(msp, evidence, view: ElevationViewBindingV1, manifest: GtExtractionManifestV1) -> tuple[GtWorldIntervalV3, GtWorldIntervalV3, list[GtEntityRefV3]]:
    box, refs = _bbox_for_locators(msp, evidence.entities, geometry_mode=evidence.geometry_mode, role="opening_elevation", source_id=manifest.source_id, view_id=view.id, clip_box=view.clip_box_dxf)
    source = {"x": (box[0], box[2]), "y": (box[1], box[3])}
    along_raw = source[view.world_along_from_source_m.source_axis]
    z_raw = source[view.world_z_from_source_m.source_axis]
    along = sorted(value * manifest.metres_per_unit * view.world_along_from_source_m.scale + view.world_along_from_source_m.offset for value in along_raw)
    z = sorted(value * manifest.metres_per_unit * view.world_z_from_source_m.scale + view.world_z_from_source_m.offset for value in z_raw)
    if along[0] == along[1] or z[0] == z[1]:
        _fail("dxf_elevation_opening_degenerate")
    return GtWorldIntervalV3(lo=along[0], hi=along[1]), GtWorldIntervalV3(lo=z[0], hi=z[1]), refs


def _elevation_floor_matches(opening: GtOpeningV3, evidence_floor_id: str) -> bool:
    """Bind vertical containment to assignment without changing along-wall tie rules."""
    return opening.floor_id == evidence_floor_id


def _assign_elevation(evidence: list[tuple], openings: list[GtOpeningV3], view: ElevationViewBindingV1,
                      segments: Mapping[str, GtBoundarySegmentV3], tolerance: float, tie_epsilon: float) -> list[tuple[int, int]]:
    """Return the unique floor-bound global minimum-cost evidence→opening assignment."""
    options: list[list[tuple[int, float]]] = []
    for item, along, _z, _refs, evidence_floor_id in evidence:
        choices = []
        for index, opening in enumerate(openings):
            segment = segments[opening.boundary_segment_id]
            coverage = view.world_along_coverage or segment.world_along_interval
            visible = any(min(opening.world_along_interval.hi, interval.hi, coverage.hi) > max(opening.world_along_interval.lo, interval.lo, coverage.lo)
                          for interval in segment.visible_intervals)
            cost = max(abs(along.lo - opening.world_along_interval.lo), abs(along.hi - opening.world_along_interval.hi))
            # `item` is always a real evidence binding on the live call path
            # (`evidence.append((item, ...))`), so the former `item is None` guard was a
            # permanently-false branch and is removed rather than left as fake defence.
            if (item.kind == opening.kind and _elevation_floor_matches(opening, evidence_floor_id)
                    and opening.floor_id in view.floor_ids and view.projection_surface_key in segment.projection_surface_keys
                    and visible and cost <= tolerance):
                choices.append((index, cost))
        if not choices:
            _fail("elevation_opening_no_candidate")
        options.append(sorted(choices))
    answers: list[tuple[float, list[tuple[int, int]]]] = []
    def visit(evidence_index: int, used: set[int], cost: float, pairs: list[tuple[int, int]]) -> None:
        if evidence_index == len(options):
            answers.append((cost, list(pairs))); return
        for opening_index, item_cost in options[evidence_index]:
            if opening_index not in used:
                used.add(opening_index); pairs.append((evidence_index, opening_index))
                visit(evidence_index + 1, used, cost + item_cost, pairs)
                pairs.pop(); used.remove(opening_index)
    visit(0, set(), 0.0, [])
    if not answers:
        _fail("elevation_opening_assignment_missing")
    answers.sort(key=lambda item: (item[0], item[1]))
    if len(answers) > 1 and abs(answers[1][0] - answers[0][0]) <= tie_epsilon:
        _fail("elevation_opening_assignment_ambiguous")
    return answers[0][1]


def _build_generator(inputs: ExtractionInputs, *, recorded_tolerances) -> GtGeneratorV3:
    """Make the persisted tolerance snapshot independently from build inputs."""
    return GtGeneratorV3(name="energyplus-agent.gt_from_dxf", contract_version=1,
        extractor_sha256=inputs.implementation_hashes.extractor_sha256, validator_sha256=inputs.implementation_hashes.validator_sha256,
        vg_implementation_sha256=inputs.implementation_hashes.vg_implementation_sha256, manifest_sha256=inputs.manifest.manifest_sha256,
        judge_config_sha256=inputs.tooling.judge_config_sha256, vg_config_sha256=inputs.tooling.vg_config_sha256,
        tolerances=recorded_tolerances)


def extract_gt_v3(inputs: ExtractionInputs) -> GroundTruthV3:
    """Build a candidate v3 document after the already-closed Phase B plan pass."""
    # This copied config is the one threaded through every plan/Vg/opening/z
    # calculation below.  The persisted generator value is a separately-built
    # snapshot so the build-end assertion can catch future wiring drift.
    inputs = ExtractionInputs(Path(inputs.dxf_path), inputs.manifest,
                              inputs.tooling.model_copy(deep=True), inputs.implementation_hashes)
    consumed_tolerances = inputs.tooling.tolerances
    plans = extract_plan_geometry(inputs)
    doc_dxf, _unit = _check_input(inputs)
    msp = doc_dxf.modelspace()
    vg_tolerances = VisibilityTolerances(inputs.tooling.tolerances.vg_depth_epsilon_m,
                                         inputs.tooling.tolerances.vg_endpoint_epsilon_m)
    elevation_views = sorted((view for view in inputs.manifest.views if isinstance(view, ElevationViewBindingV1)), key=lambda view: view.id)
    all_segments: dict[str, GtBoundarySegmentV3] = {}
    # Exterior wall thickness is a DECLARED manifest value (evidence-backed upstream by
    # the converter's G7/G8 thickness proofs); the extractor only carries it onto the
    # outer-skin boundary segments and never measures or defaults one itself.  A manifest
    # that declares nothing leaves every segment at None.
    thickness_by_floor = {view.floor_id: view.default_wall_thickness_m
                          for view in inputs.manifest.views if isinstance(view, PlanViewBindingV1)}
    floors: list[GtFloorV3] = []
    for plan_floor in plans.floors:
        materialized: list[GtBoundarySegmentV3] = []
        ring = plan_floor.footprint.exterior.vertices
        for direction in _DIRECTIONS:
            for derived in vg_for_direction(ring, direction, tolerances=vg_tolerances):
                frame = derived.frame
                refs = _edge_refs(plan_floor, tuple(frame.p1), tuple(frame.p2))
                segment = GtBoundarySegmentV3(id=stable_boundary_segment_id(plan_floor.id, frame.facade_family, list(frame.p1), list(frame.p2)),
                    floor_id=plan_floor.id, boundary_loop_id="exterior", facade_family=frame.facade_family,
                    p1=list(frame.p1), p2=list(frame.p2), outward_normal=list(frame.outward_normal),
                    world_along_interval=GtWorldIntervalV3(lo=frame.world_along_interval[0], hi=frame.world_along_interval[1]),
                    depth=frame.depth, visible_intervals=[GtWorldIntervalV3(lo=lo, hi=hi) for lo, hi in derived.visible_intervals],
                    source_footprint_fingerprint=plan_floor.footprint_fingerprint, projection_surface_keys=[],
                    wall_thickness_m=thickness_by_floor.get(plan_floor.id), source_refs=refs)
                if segment.id in all_segments:
                    _fail("gt_boundary_segment_id_collision")
                all_segments[segment.id] = segment; materialized.append(segment)
        # Bind elevation surfaces after all direction candidates are materialised.
        # A listed locator identifies exactly one canonical boundary edge; a
        # handle-only locator on a multi-edge polyline is intentionally not a
        # convenient fan-out shorthand.
        scoped_ids: dict[str, set[str]] = {}
        for view in elevation_views:
            candidates = [segment for segment in materialized
                          if plan_floor.id in view.floor_ids and segment.facade_family == view.facade_family]
            if view.segment_scope_mode == "all_family_segments":
                if not candidates:
                    _fail("dxf_elevation_scope_no_segments")
                scoped_ids[view.id] = {segment.id for segment in candidates}
                continue
            selected: set[str] = set()
            for locator in view.boundary_entities:
                matches = [segment for segment in candidates if any(
                    ref.entity_handle == locator.handle and ref.subentity_index == locator.subentity_index
                    for ref in segment.source_refs)]
                if len(matches) != 1:
                    _fail("dxf_elevation_scope_locator_ambiguous" if len(matches) > 1 else "dxf_elevation_scope_locator_missing")
                selected.add(matches[0].id)
            if not selected:
                _fail("dxf_elevation_scope_no_segments")
            scoped_ids[view.id] = selected
        rebound = []
        for segment in materialized:
            keys: list[str] = []
            for view in elevation_views:
                if plan_floor.id not in view.floor_ids or view.facade_family != segment.facade_family:
                    continue
                if segment.id in scoped_ids[view.id]:
                    keys.append(view.projection_surface_key)
            rebound_segment = segment.model_copy(update={"projection_surface_keys": sorted(set(keys))})
            all_segments[segment.id] = rebound_segment; rebound.append(rebound_segment)
        zones = [GtZoneV3(id=zone.id, name=zone.name, role=zone.role, polygon=zone.polygon, source_refs=list(zone.source_refs)) for zone in plan_floor.zones]
        floors.append(GtFloorV3(id=plan_floor.id, name=plan_floor.name, z_floor_m=plan_floor.z_floor_m,
            ceiling_height_m=plan_floor.ceiling_height_m, footprint=plan_floor.footprint,
            footprint_fingerprint=plan_floor.footprint_fingerprint, zones=zones,
            boundary_segments=sorted(rebound, key=lambda segment: ({"North": 0, "South": 1, "East": 2, "West": 3}[segment.facade_family], segment.world_along_interval.lo, segment.world_along_interval.hi, segment.depth, segment.id))))
    openings: list[GtOpeningV3] = []
    plan_by_floor = {floor.id: floor for floor in plans.floors}
    plan_views = sorted((view for view in inputs.manifest.views if isinstance(view, PlanViewBindingV1)), key=lambda view: view.id)
    for view in plan_views:
        plan_floor = plan_by_floor[view.floor_id]
        for binding in sorted(view.plan_openings, key=lambda item: item.opening_id):
            interval, centre, refs = _plan_opening_geometry(msp, binding, view, inputs.manifest)
            legal: list[tuple[float, float, GtBoundarySegmentV3]] = []
            for segment in (segment for segment in all_segments.values() if segment.floor_id == plan_floor.id):
                parallel = (binding.span_world_axis == "x") == _is_horizontal(tuple(segment.p1), tuple(segment.p2))
                residual = max(max(segment.world_along_interval.lo - interval.lo, 0.0), max(interval.hi - segment.world_along_interval.hi, 0.0))
                distance = _normal_distance(binding.span_world_axis, centre, segment)
                if parallel and residual == 0.0 and distance <= inputs.tooling.tolerances.opening_boundary_max_distance_m and _host_zones(plan_floor, segment):
                    legal.append((distance, residual, segment))
            legal.sort(key=lambda item: (item[0], item[1], item[2].id))
            if not legal:
                _fail("opening_segment_assignment_no_candidate")
            if len(legal) > 1 and abs(legal[1][0] - legal[0][0]) <= inputs.tooling.tolerances.opening_assignment_tie_epsilon_m and abs(legal[1][1] - legal[0][1]) <= inputs.tooling.tolerances.opening_assignment_tie_epsilon_m:
                _fail("opening_segment_assignment_ambiguous")
            hosts = _host_zones_for_opening(plan_floor, legal[0][2], interval)
            if len(hosts) != 1:
                _fail("opening_host_zone_ambiguous")
            openings.append(GtOpeningV3(id=binding.opening_id, kind=binding.kind, floor_id=plan_floor.id,
                host_zone_id=hosts[0], boundary_segment_id=legal[0][2].id, world_along_interval=interval,
                z_interval=None, source_refs=refs))
    openings.sort(key=lambda opening: (opening.floor_id, opening.boundary_segment_id, opening.world_along_interval.lo, opening.kind, opening.id))
    # Assignment is per view, over every evidence group, never greedy per opening.
    z_by_opening: dict[str, GtWorldIntervalV3] = {}
    evidence_refs: dict[str, list[GtEntityRefV3]] = defaultdict(list)
    for view in elevation_views:
        evidence = []
        for item in sorted(view.opening_entities, key=lambda item: item.evidence_id):
            along, z, refs = _elevation_geometry(msp, item, view, inputs.manifest)
            containing = [floor for floor in floors if floor.id in view.floor_ids and floor.z_floor_m <= z.lo < z.hi <= floor.z_floor_m + floor.ceiling_height_m]
            if len(containing) != 1:
                _fail("elevation_opening_floor_ambiguous")
            evidence.append((item, along, z, refs, containing[0].id))
        if evidence:
            pairs = _assign_elevation(evidence, openings, view, all_segments, inputs.tooling.tolerances.elevation_match_max_distance_m, inputs.tooling.tolerances.elevation_match_tie_epsilon_m)
            for evidence_index, opening_index in pairs:
                opening = openings[opening_index]; _item, _along, z, refs, _floor_id = evidence[evidence_index]
                old = z_by_opening.get(opening.id)
                if old is not None and (old.lo, old.hi) != (z.lo, z.hi):
                    _fail("elevation_opening_vertical_disagreement")
                z_by_opening[opening.id] = z; evidence_refs[opening.id].extend(refs)
    openings = [opening.model_copy(update={"z_interval": z_by_opening.get(opening.id), "source_refs": _sorted_refs([*opening.source_refs, *evidence_refs[opening.id]])}) for opening in openings]
    north_refs: list[GtEntityRefV3] = []
    north = inputs.manifest.north_axis
    if north is not None:
        source_view_ids = {view.id for view in inputs.manifest.views}
        if north.source_view_id not in source_view_ids or not any(_handle(entity) == north.source_entity_handle for entity in msp):
            _fail("dxf_north_source_missing")
        north_refs = [GtEntityRefV3(source_id=inputs.manifest.source_id, view_id=north.source_view_id,
                      entity_handle=north.source_entity_handle, subentity_index=None, role="north_axis")]
    generator = _build_generator(inputs, recorded_tolerances=consumed_tolerances.model_copy(deep=True))
    doc = GroundTruthV3(schema_version=3, case=plans.case, geometry_profile="c2_simple_orthogonal_no_holes",
        coordinate_frame="building_axis_world_m", verification=GtVerificationV3(status="candidate", reviewer_id=None, reviewed_on=None, methods=[]),
        generator=generator, sources=[plans.source], north_axis_deg=None if north is None else north.value_deg,
        north_axis_source_refs=north_refs, floors=sorted(floors, key=lambda floor: (floor.z_floor_m, floor.id)), openings=openings,
        content_sha256="0" * 64)
    if doc.generator.tolerances.model_dump(mode="python") != consumed_tolerances.model_dump(mode="python"):
        _fail("gt_build_profile_tolerances_mismatch")
    doc = doc.model_copy(update={"content_sha256": compute_gt_v3_content_sha256(doc)})
    validate_gt_v3(doc, tolerances=doc.generator.tolerances, expected_case=inputs.manifest.case)
    reloaded = GroundTruthV3.model_validate_json(canonical_gt_v3_bytes(doc))
    validate_gt_v3(reloaded, tolerances=reloaded.generator.tolerances, expected_case=inputs.manifest.case)
    if reloaded != doc or canonical_gt_v3_bytes(reloaded) != canonical_gt_v3_bytes(doc):
        _fail("gt_build_canonical_roundtrip_mismatch")
    return doc
