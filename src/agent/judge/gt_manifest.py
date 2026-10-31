"""Manifest and frozen tooling-profile contracts for GT v3 tooling.

The manifest is deliberately separate from baseline GT.  Phase A supplies the
wire/config contract only; extraction consumes it in a later phase.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field, Strict, model_validator

from .affine_space import (AffineSpace, bind_affine_spaces,
                           require_affine_magnitudes, strip_affine_space_keys)
from .gt_schema import (DateYmd, DxfHandle, GtResolvedToolingConfigV1,
                        GtResolvedToolingTolerancesV1, GtWorldIntervalV3,
                        Hex64, HumanLabel, Point2, PositiveFiniteFloat,
                        REPO_ROOT, StableId, StrictFiniteFloat, StrictNonNegativeInt)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClipBoxDxf(_StrictModel):
    xmin: StrictFiniteFloat
    ymin: StrictFiniteFloat
    xmax: StrictFiniteFloat
    ymax: StrictFiniteFloat

    @model_validator(mode="after")
    def _ordered(self):
        if not self.xmin < self.xmax or not self.ymin < self.ymax:
            raise ValueError("clip box must have positive dimensions")
        return self


class Affine2D(_StrictModel):
    """A 2-D affine plus its two-end space contract.

    The six coefficients alone are ambiguous: this one type carries
    ``pixel -> source_metre`` (``pixel_to_source_m``), ``dxf_native -> world_metre``
    (``PlanViewIntentV1.world_from_source_m``) and ``source_metre -> world_metre``
    (``PlanViewBindingV1.world_from_source_m``) -- the last two share a field name
    and differ by exactly ``metres_per_unit`` (1000x on sm24/sm25).

    ``domain_space``/``codomain_space`` default to ``None`` so every already-signed
    request and manifest still loads; the *owning* model stamps them in its
    validator via :func:`~.affine_space.bind_affine_spaces`, and an affine that
    arrives already declaring a different pair is rejected there.  Consumers read
    the pair through :func:`~.affine_space.affine_spaces`, which treats an
    undeclared affine as an error rather than as "any space".
    """

    m00: StrictFiniteFloat
    m01: StrictFiniteFloat
    m02: StrictFiniteFloat
    m10: StrictFiniteFloat
    m11: StrictFiniteFloat
    m12: StrictFiniteFloat
    domain_space: AffineSpace | None = None
    codomain_space: AffineSpace | None = None

    @model_validator(mode="after")
    def _non_singular(self):
        if self.m00 * self.m11 - self.m01 * self.m10 == 0:
            raise ValueError("affine transform is singular")
        if (self.domain_space is None) != (self.codomain_space is None):
            raise ValueError("affine space contract must declare both ends or neither")
        return self


class Affine1D(_StrictModel):
    """A 1-D affine plus its two-end space contract (B4-(2)a, F-E).

    Same hazard as :class:`Affine2D`, one dimension down and previously
    untreated: ``world_along_from_source_m`` / ``world_z_from_source_m`` appear
    on the request intents (``dxf_native -> world_metre``, ``scale`` is
    ``+/- metres_per_unit``) and on ``ElevationViewBindingV1``
    (``source_metre -> world_metre``, ``scale`` is ``+/- 1``), same names, same
    type, 1000x apart -- and ``tarch_normalize._build_manifest`` bridges them
    with the same hand-written ``/ metres_per_unit`` that the 2-D pair uses.

    The fields default to ``None`` and the *owning* model stamps them, exactly
    as for :class:`Affine2D`; the canonical hash preimages strip both keys, so
    every already-signed request and manifest keeps its digest byte-for-byte.
    """

    source_axis: Literal["x", "y"]
    scale: StrictFiniteFloat
    offset: StrictFiniteFloat
    domain_space: AffineSpace | None = None
    codomain_space: AffineSpace | None = None

    @model_validator(mode="after")
    def _nonzero(self):
        if self.scale == 0:
            raise ValueError("affine scale is zero")
        if (self.domain_space is None) != (self.codomain_space is None):
            raise ValueError("affine space contract must declare both ends or neither")
        return self


class EntitySelectorV1(_StrictModel):
    entity_types: list[Literal["LINE", "LWPOLYLINE", "POLYLINE", "INSERT"]] = Field(min_length=1)
    layers: list[str] = Field(min_length=1)
    handles: list[DxfHandle] = Field(default_factory=list)
    handle_mode: Literal["all_matching", "only_listed"]
    min_count: StrictNonNegativeInt
    max_count: StrictNonNegativeInt | None

    @model_validator(mode="after")
    def _selector_contract(self):
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("selector max is below min")
        if self.entity_types != sorted(set(self.entity_types)) or self.layers != sorted(set(self.layers)) or self.handles != sorted(set(self.handles)):
            raise ValueError("selector lists must be sorted and unique")
        if (self.handle_mode == "all_matching" and self.handles) or (self.handle_mode == "only_listed" and not self.handles):
            raise ValueError("selector handle mode/list mismatch")
        return self


class EntityLocatorV1(_StrictModel):
    handle: DxfHandle
    subentity_index: StrictNonNegativeInt | None = None


class PlanOpeningBindingV1(_StrictModel):
    opening_id: StableId
    kind: Literal["window", "door"]
    geometry_mode: Literal["closed_outline_bbox", "grouped_line_bbox", "virtual_entity_bbox"]
    span_world_axis: Literal["x", "y"]
    entities: list[EntityLocatorV1] = Field(min_length=1)


class ElevationOpeningEvidenceV1(_StrictModel):
    evidence_id: StableId
    kind: Literal["window", "door"]
    geometry_mode: Literal["closed_outline_bbox", "grouped_line_bbox", "virtual_entity_bbox"]
    entities: list[EntityLocatorV1] = Field(min_length=1)


class ZoneSeedV1(_StrictModel):
    zone_id: StableId
    name: HumanLabel
    role: StableId
    point_world_m: Point2


class PlanViewBindingV1(_StrictModel):
    kind: Literal["plan"]
    id: StableId
    floor_id: StableId
    clip_box_dxf: ClipBoxDxf
    world_from_source_m: Affine2D
    footprint_boundary: EntitySelectorV1
    zone_boundaries: EntitySelectorV1
    plan_openings: list[PlanOpeningBindingV1]
    zone_seeds: list[ZoneSeedV1] = Field(min_length=1)
    boundary_reference: Literal["outer_skin", "centerline"]
    default_wall_thickness_m: PositiveFiniteFloat | None

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        # v3 extraction pre-multiplies native by metres_per_unit before applying
        # this affine, so the manifest copy starts in source METRES -- unlike the
        # identically named PlanViewIntentV1 field, which starts in dxf-native.
        bind_affine_spaces(self, "world_from_source_m",
                           domain="source_metre", codomain="world_metre")
        return self


class ElevationViewBindingV1(_StrictModel):
    kind: Literal["elevation"]
    id: StableId
    floor_ids: list[StableId] = Field(min_length=1)
    projection_surface_key: StableId
    facade_family: Literal["North", "South", "East", "West"]
    view_kind: Literal["full", "partial"]
    world_along_coverage: GtWorldIntervalV3 | None
    direction_semantics: Literal["building_axis", "true_azimuth"]
    azimuth_deg: Annotated[StrictFiniteFloat, Field(ge=0, lt=360)] | None
    clip_box_dxf: ClipBoxDxf
    world_along_from_source_m: Affine1D
    world_z_from_source_m: Affine1D
    segment_scope_mode: Literal["all_family_segments", "listed_boundary_entities"]
    boundary_entities: list[EntityLocatorV1] = Field(default_factory=list)
    opening_entities: list[ElevationOpeningEvidenceV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        # v3 extraction pre-multiplies native by metres_per_unit before applying
        # these, so the manifest copies start in source METRES -- unlike the
        # identically named request-intent fields, which start in dxf-native.
        for field in ("world_along_from_source_m", "world_z_from_source_m"):
            bind_affine_spaces(self, field,
                               domain="source_metre", codomain="world_metre")
        return self

    @model_validator(mode="after")
    def _elevation_contract(self):
        if self.floor_ids != sorted(set(self.floor_ids)):
            raise ValueError("elevation floor IDs must be sorted and unique")
        if self.world_along_from_source_m.source_axis == self.world_z_from_source_m.source_axis:
            raise ValueError("elevation source axes must differ")
        if self.direction_semantics == "building_axis" and self.azimuth_deg is not None:
            raise ValueError("building-axis view has azimuth")
        if self.direction_semantics == "true_azimuth" and self.azimuth_deg is None:
            raise ValueError("true-azimuth view requires azimuth")
        if self.view_kind == "full":
            if self.segment_scope_mode != "all_family_segments" or self.world_along_coverage is not None or self.boundary_entities:
                raise ValueError("full elevation scope contract")
        elif self.segment_scope_mode != "listed_boundary_entities" or self.world_along_coverage is None or not self.boundary_entities:
            raise ValueError("partial elevation scope contract")
        return self


class NorthAxisBindingV1(_StrictModel):
    value_deg: Annotated[StrictFiniteFloat, Field(ge=0, lt=360)]
    source_view_id: StableId
    source_entity_handle: DxfHandle


class RasterOverlayBindingV1(_StrictModel):
    id: StableId
    source_label: HumanLabel
    source_sha256: Hex64
    view_id: StableId
    pixel_to_source_m: Affine2D

    @model_validator(mode="after")
    def _bind_affine_spaces(self):
        bind_affine_spaces(self, "pixel_to_source_m",
                           domain="pixel", codomain="source_metre")
        return self


class FloorBindingV1(_StrictModel):
    id: StableId
    name: HumanLabel
    z_floor_m: StrictFiniteFloat
    ceiling_height_m: PositiveFiniteFloat


class GtExtractionManifestV1(_StrictModel):
    manifest_version: Literal[1]
    case: StableId
    source_id: StableId
    source_dxf_label: HumanLabel
    source_dxf_sha256: Hex64
    native_units: Literal["m", "mm", "cm", "in", "ft", "unitless"]
    metres_per_unit: PositiveFiniteFloat
    geometry_profile: Literal["c2_simple_orthogonal_no_holes"]
    floors: list[FloorBindingV1] = Field(min_length=1)
    views: list[PlanViewBindingV1 | ElevationViewBindingV1] = Field(min_length=1)
    north_axis: NorthAxisBindingV1 | None
    raster_overlays: list[RasterOverlayBindingV1] = Field(default_factory=list)
    manifest_sha256: Hex64

    @model_validator(mode="after")
    def _affine_magnitudes(self):
        """Numeric half of the affine contract -- see ``affine_space``.

        The manifest root is the only place that knows ``metres_per_unit``, and
        every affine under it is reachable from here.  This is what catches a
        regression in ``_build_manifest``'s hand-written ``/ metres_per_unit``:
        that function ends in ``GtExtractionManifestV1.model_validate``, so a
        manifest affine left in dxf-native (or divided twice) cannot be built.
        """
        return require_affine_magnitudes(
            self, metres_per_unit=self.metres_per_unit,
            context="GtExtractionManifestV1")

    @model_validator(mode="after")
    def _manifest_contract(self):
        if "/" in self.source_dxf_label or "\\" in self.source_dxf_label or ".." in self.source_dxf_label:
            raise ValueError("source label must be basename")
        ids = [floor.id for floor in self.floors]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate floor")
        view_ids = [view.id for view in self.views]
        if len(view_ids) != len(set(view_ids)):
            raise ValueError("duplicate view")
        floor_set = set(ids)
        plans = [view for view in self.views if isinstance(view, PlanViewBindingV1)]
        if {view.floor_id for view in plans} != floor_set or len(plans) != len(floor_set):
            raise ValueError("each floor requires exactly one plan")
        if any(floor not in floor_set for view in self.views if isinstance(view, ElevationViewBindingV1) for floor in view.floor_ids):
            raise ValueError("elevation references unknown floor")
        if self.north_axis is not None and self.north_axis.source_view_id not in set(view_ids):
            raise ValueError("north axis references unknown view")
        opening_ids = [opening.opening_id for view in plans for opening in view.plan_openings]
        evidence_ids = [evidence.evidence_id for view in self.views if isinstance(view, ElevationViewBindingV1)
                        for evidence in view.opening_entities]
        projection_keys = [view.projection_surface_key for view in self.views if isinstance(view, ElevationViewBindingV1)]
        if len(opening_ids) != len(set(opening_ids)) or len(evidence_ids) != len(set(evidence_ids)) or len(projection_keys) != len(set(projection_keys)):
            raise ValueError("duplicate opening, evidence, or projection key")
        if any(overlay.view_id not in set(view_ids) for overlay in self.raster_overlays):
            raise ValueError("raster overlay references unknown view")
        if self.manifest_sha256 != compute_manifest_sha256(self):
            raise ValueError("manifest hash mismatch")
        return self


#: Manifest versions signed BEFORE the affine two-end space contract existed.
#: Their canonical preimage omits ``domain_space``/``codomain_space`` so already
#: signed manifests keep their hash byte-for-byte.
#:
#: MIGRATION HONESTY: while every legal ``manifest_version`` is listed here, the
#: space contract is deliberately OUTSIDE the signature.  This protects the
#: *code paths* (a mis-spaced affine is rejected by the owning model's
#: validator); it does NOT mean a signed manifest has attested to its spaces.
#: ``test_affine_space_contract.py`` fails the moment a manifest version is
#: added without deciding whether it binds the spaces.
MANIFEST_VERSIONS_WITHOUT_SPACE_BINDING: frozenset[int] = frozenset({1})


def canonical_manifest_payload(manifest: GtExtractionManifestV1) -> dict:
    payload = manifest.model_dump(mode="json")
    payload["manifest_sha256"] = "0" * 64
    if manifest.manifest_version in MANIFEST_VERSIONS_WITHOUT_SPACE_BINDING:
        payload = strip_affine_space_keys(payload)
    return _normalise(payload)


def _normalise(value):
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    return value


def compute_manifest_sha256(manifest: GtExtractionManifestV1) -> str:
    payload = canonical_manifest_payload(manifest)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n").hexdigest()


def validate_manifest_view_clips(manifest: GtExtractionManifestV1, *, topology_area_tolerance_m2: float) -> None:
    """Fail closed when source-space view clips overlap beyond the active profile.

    The manifest wire intentionally carries no tolerance profile; callers at the
    tooling boundary supply the resolved profile rather than hiding a default in
    the manifest model.
    """
    views = sorted(manifest.views, key=lambda view: view.id)
    scale2 = manifest.metres_per_unit ** 2
    for index, left in enumerate(views):
        for right in views[index + 1:]:
            a, b = left.clip_box_dxf, right.clip_box_dxf
            overlap_x = min(a.xmax, b.xmax) - max(a.xmin, b.xmin)
            overlap_y = min(a.ymax, b.ymax) - max(a.ymin, b.ymin)
            if overlap_x > 0 and overlap_y > 0 and overlap_x * overlap_y * scale2 > topology_area_tolerance_m2:
                raise ValueError("dxf_view_clip_overlap")


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_gt_tooling_config(gt_config_path: Path, vg_config_path: Path) -> GtResolvedToolingConfigV1:
    """Resolve the seven judge values plus the two existing Vg values.

    This is explicitly a build/tool boundary.  Typed GT loaders never call it.
    """
    gt_path, vg_path = Path(gt_config_path), Path(vg_config_path)
    if vg_path.resolve() != (REPO_ROOT / "src/configs/correction.yaml").resolve():
        raise ValueError("gt_vg_config_path_forbidden")
    raw = OmegaConf.to_container(OmegaConf.load(gt_path), resolve=True)
    vg = OmegaConf.to_container(OmegaConf.load(vg_path), resolve=True)
    if not isinstance(raw, dict) or not isinstance(vg, dict) or not isinstance(vg.get("correction"), dict):
        raise ValueError("gt_tooling_config_invalid")
    values = dict(raw)
    correction = vg["correction"]
    values["vg_depth_epsilon_m"] = correction.get("facade_visibility_depth_epsilon_m")
    values["vg_endpoint_epsilon_m"] = correction.get("facade_visibility_endpoint_epsilon_m")
    try:
        tooling = GtResolvedToolingTolerancesV1.model_validate(values)
    except Exception as exc:
        raise ValueError("gt_tooling_config_invalid") from exc
    return GtResolvedToolingConfigV1(
        tolerances=tooling,
        judge_config_sha256=_raw_sha256(gt_path),
        vg_config_sha256=_raw_sha256(vg_path),
    )
