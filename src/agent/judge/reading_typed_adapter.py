"""Judge-only adapter contracts for aggregate reading-stage products."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from src.agent.execution.view_manifest import RequiredViewEntry
from src.agent.judge.affine_space import require_affine_spaces
from src.agent.judge.elevation_score import (
    TypedElevationObservation,
    project_typed_elevation_observation,
)
from src.agent.judge.score_schema import (
    Affine2DV1,
    READING_ADAPTER_VERSION,
    READING_DENOMINATOR_VERSION,
    ClosedIntervalV1,
    ElevationScoreViewBindingV1,
    PlanScoreViewBindingV1,
    PlanFrameCertificateV1,
    Point2V1,
    ReadingComponentApplicabilityV1,
    ReadingDenominatorAtomV1,
    ReadingDenominatorBasisV1,
    ReadingElevationOpeningAuditV1,
    ReadingFilteredComponentBasisV1,
    ReadingMetadataFindingV1,
    ReadingNormalizationCertificateV1,
    ReadingPlanOpeningAuditV1,
    ReadingPlanSegmentAuditV1,
    UnmeasurableObservationWitnessV1,
    VerticalDatumCertificateV1,
    canonical_sha256,
)
from src.agent.reading import ReadingView
# The reading-product contract shape detector + constants are owned by the
# reading package (src.agent.reading.contract).  Imported here so this adapter
# (and its existing call sites) bind the canonical single object — there is no
# second detector.  See test_f2c_single_contract_detector_is_canonical.
from src.agent.reading.contract import (
    READING_CONTRACT_DETECTOR_VERSION,
    READING_PRODUCT_CONTRACT,
    identify_reading_contract,
)


@dataclass(frozen=True)
class ReadingNormalizationOutcome:
    certificate: ReadingNormalizationCertificateV1
    trusted_capability_dispositions: tuple[
        ReadingFilteredComponentBasisV1, ...
    ]


_ELEVATION_COMPONENTS = (
    "elevation_opening_xy",
    "elevation_opening_z",
)
_PLAN_COMPONENTS = ("plan_segments", "plan_openings")
_MISSING = object()


def _strict_finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("strict finite number required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("strict finite number required")
    return result


def _strict_point(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("point requires two coordinates")
    return _strict_finite(value[0]), _strict_finite(value[1])


def apply_affine_2d(
    frame: Affine2DV1,
    point: tuple[float, float],
) -> tuple[float, float]:
    """Map one reading-local plan point (metres) into world metres.

    The space check is the point: the caller hands in local drawing metres and
    gets world metres back, and any affine that does not declare exactly that
    pair -- a manifest ``world_from_source_m`` for instance -- is refused by
    name instead of being read coefficient-by-coefficient into a wrong answer.
    """
    require_affine_spaces(
        frame,
        domain="reading_plan_local_metre",
        codomain="world_metre",
        context="apply_affine_2d",
    )
    return (
        frame.xx * point[0] + frame.xy * point[1] + frame.x0,
        frame.yx * point[0] + frame.yy * point[1] + frame.y0,
    )


def _audit_sha(value: object) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError):
        return canonical_sha256(
            {
                "unsupported_json_value": type(value).__name__,
            }
        )


def _component(
    *,
    source_input_id: str,
    channel: Literal["plan", "elevation"],
    component: str,
    floor_ids: tuple[str, ...],
    status: Literal["applicable", "not_applicable"],
    reasons: tuple[str, ...] = (),
    cause_class: str = "none",
    denominator_disposition: str = "score",
    observation_count: int = 0,
    transform_sha256: str | None = None,
) -> ReadingComponentApplicabilityV1:
    return ReadingComponentApplicabilityV1(
        source_input_id=source_input_id,
        channel=channel,
        component=component,
        floor_ids=tuple(sorted(floor_ids)),
        status=status,
        reasons=tuple(sorted(reasons)),
        cause_class=cause_class,
        denominator_disposition=denominator_disposition,
        observation_count=observation_count,
        transform_sha256=transform_sha256,
    )


def _na_components(
    *,
    source_input_id: str,
    channel: Literal["plan", "elevation"],
    components: tuple[str, ...],
    floor_ids: tuple[str, ...],
    reason: str,
    cause_class: str,
    denominator_disposition: str,
) -> tuple[ReadingComponentApplicabilityV1, ...]:
    return tuple(
        _component(
            source_input_id=source_input_id,
            channel=channel,
            component=component,
            floor_ids=floor_ids,
            status="not_applicable",
            reasons=(reason,),
            cause_class=cause_class,
            denominator_disposition=denominator_disposition,
        )
        for component in components
    )


def _normalized_observation_id(
    *,
    output_sha256: str,
    input_id: str,
    stroke_id: str,
    component: str,
    primitive_index: int,
) -> str:
    return "reading:" + canonical_sha256(
        (
            output_sha256,
            input_id,
            stroke_id,
            component,
            primitive_index,
        )
    )


def _vertical_datum(
    *,
    input_id: str,
    floor_ids: tuple[str, ...],
    source: Literal[
        "product_declared",
        "project_convention_2026_07_25",
        "multi_floor_unavailable",
    ],
    z_origin: float | None,
) -> VerticalDatumCertificateV1:
    if source == "multi_floor_unavailable":
        raw = {
            "input_id": input_id,
            "floor_ids": tuple(sorted(floor_ids)),
            "status": "not_applicable",
            "source": source,
            "units": "metre",
            "local_axis": "drawing_up",
            "z_sign": None,
            "z_origin": None,
            "authority": "reviewed_binding_multiple_floors",
            "reason": "elevation_floor_partition_unresolved",
        }
    else:
        raw = {
            "input_id": input_id,
            "floor_ids": tuple(sorted(floor_ids)),
            "status": "applicable",
            "source": source,
            "units": "metre",
            "local_axis": "drawing_up",
            "z_sign": 1,
            "z_origin": z_origin,
            "authority": (
                "reading_scale_origin_world_z_m"
                if source == "product_declared"
                else "user_ruling_grade_line_equals_interior_floor_zero"
            ),
            "reason": None,
        }
    return VerticalDatumCertificateV1(
        **raw,
        preimage_sha256=canonical_sha256(raw),
    )


def _plan_frame(
    *,
    input_id: str,
    floor_id: str,
    x0: float,
    y0: float,
) -> PlanFrameCertificateV1:
    affine = Affine2DV1(
        xx=1.0,
        xy=0.0,
        x0=x0,
        yx=0.0,
        yy=1.0,
        y0=y0,
    )
    raw = {
        "input_id": input_id,
        "floor_id": floor_id,
        "source": "reading_scale_origin_v1",
        "units": "metre",
        "local_axes": "drawing_right_up",
        "affine": affine.model_dump(mode="json"),
        "nonzero_origin": x0 != 0.0 or y0 != 0.0,
    }
    return PlanFrameCertificateV1(
        input_id=input_id,
        floor_id=floor_id,
        source="reading_scale_origin_v1",
        units="metre",
        local_axes="drawing_right_up",
        affine=affine,
        nonzero_origin=raw["nonzero_origin"],
        preimage_sha256=canonical_sha256(raw),
    )


def _elevation_bounds(
    geometry: object,
) -> tuple[str, tuple[float, float], tuple[float, float]]:
    if not isinstance(geometry, dict):
        raise ValueError("geometry must be an object")
    kind = geometry.get("kind")
    if kind == "rect":
        raw_x = geometry.get("x_range_m")
        raw_y = geometry.get("y_range_m")
        if (
            not isinstance(raw_x, list)
            or len(raw_x) != 2
            or not isinstance(raw_y, list)
            or len(raw_y) != 2
        ):
            raise ValueError("rect ranges invalid")
        xs = (_strict_finite(raw_x[0]), _strict_finite(raw_x[1]))
        ys = (_strict_finite(raw_y[0]), _strict_finite(raw_y[1]))
    elif kind == "line":
        points = (_strict_point(geometry.get("p1")), _strict_point(geometry.get("p2")))
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
    elif kind == "polyline":
        raw_points = geometry.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            raise ValueError("polyline points invalid")
        points = tuple(_strict_point(point) for point in raw_points)
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
    else:
        raise ValueError("unsupported elevation geometry")
    return str(kind), (min(xs), max(xs)), (min(ys), max(ys))


def _plan_wall_edges(
    geometry: object,
) -> tuple[str, tuple[tuple[tuple[float, float], tuple[float, float]], ...]]:
    if not isinstance(geometry, dict):
        raise ValueError("geometry must be an object")
    kind = geometry.get("kind")
    if kind == "line":
        return "line", (
            (
                _strict_point(geometry.get("p1")),
                _strict_point(geometry.get("p2")),
            ),
        )
    if kind == "polyline":
        raw_points = geometry.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise ValueError("wall polyline requires at least two points")
        points = tuple(_strict_point(point) for point in raw_points)
        edges = list(zip(points, points[1:]))
        if geometry.get("closed") is True:
            edges.append((points[-1], points[0]))
        return "polyline", tuple(edges)
    raise ValueError("unsupported wall geometry")


def _plan_opening_vertices(
    geometry: object,
) -> tuple[str, tuple[tuple[float, float], ...]]:
    if not isinstance(geometry, dict):
        raise ValueError("geometry must be an object")
    kind = geometry.get("kind")
    if kind == "line":
        return "line", (
            _strict_point(geometry.get("p1")),
            _strict_point(geometry.get("p2")),
        )
    if kind == "rect":
        raw_x = geometry.get("x_range_m")
        raw_y = geometry.get("y_range_m")
        if (
            not isinstance(raw_x, list)
            or len(raw_x) != 2
            or not isinstance(raw_y, list)
            or len(raw_y) != 2
        ):
            raise ValueError("rect ranges invalid")
        x0, x1 = sorted(
            (_strict_finite(raw_x[0]), _strict_finite(raw_x[1]))
        )
        y0, y1 = sorted(
            (_strict_finite(raw_y[0]), _strict_finite(raw_y[1]))
        )
        return "rect", ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    if kind == "polyline":
        raw_points = geometry.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            raise ValueError("opening polyline requires a point")
        return "polyline", tuple(_strict_point(point) for point in raw_points)
    raise ValueError("unsupported opening geometry")


def _metadata_hash(value: object) -> str:
    return canonical_sha256("missing" if value is _MISSING else value)


def _plan_result(
    *,
    entry: RequiredViewEntry,
    binding: PlanScoreViewBindingV1,
    raw_view: object,
    source_output_sha256: str,
) -> tuple[
    tuple[ReadingComponentApplicabilityV1, ...],
    tuple[ReadingPlanSegmentAuditV1 | ReadingPlanOpeningAuditV1, ...],
    tuple[PlanFrameCertificateV1, ...],
    tuple[UnmeasurableObservationWitnessV1, ...],
    tuple[ReadingMetadataFindingV1, ...],
]:
    floor_ids = (binding.floor_id,)
    if raw_view is _MISSING:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_missing",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
        )
    if not isinstance(raw_view, dict):
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
        )
    try:
        ReadingView.model_validate(raw_view)
    except ValidationError:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
        )
    findings: list[ReadingMetadataFindingV1] = []
    if raw_view.get("image_kind", _MISSING) != "plan":
        findings.append(
            ReadingMetadataFindingV1(
                source_input_id=binding.input_id,
                code="image_kind_declaration_mismatch",
                declared_sha256=_metadata_hash(
                    raw_view.get("image_kind", _MISSING)
                ),
                trusted_sha256=_metadata_hash("plan"),
            )
        )
    origin = raw_view.get("scale_origin")
    try:
        if not isinstance(origin, dict):
            raise ValueError("plan scale origin missing")
        x0 = _strict_finite(origin.get("world_x_m", _MISSING))
        y0 = _strict_finite(origin.get("world_y_m", _MISSING))
        frame = _plan_frame(
            input_id=binding.input_id,
            floor_id=binding.floor_id,
            x0=x0,
            y0=y0,
        )
    except ValueError:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=floor_ids,
                reason="plan_frame_unavailable",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            tuple(findings),
        )
    raw_strokes = raw_view.get("strokes")
    if not isinstance(raw_strokes, list):
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (frame,),
            (),
            tuple(findings),
        )

    segment_observations: list[ReadingPlanSegmentAuditV1] = []
    opening_observations: list[ReadingPlanOpeningAuditV1] = []
    witnesses: list[UnmeasurableObservationWitnessV1] = []
    malformed_segments = False
    malformed_openings = False
    for raw_stroke in raw_strokes:
        if not isinstance(raw_stroke, dict):
            continue
        if raw_stroke.get("visibility") == "hidden" or raw_stroke.get(
            "line_style"
        ) in {"dashed", "dash_dot"}:
            continue
        pen = raw_stroke.get("pen")
        if pen not in {"wall", "window"}:
            continue
        stroke_id = raw_stroke.get("id")
        if not isinstance(stroke_id, str) or not stroke_id:
            stroke_id = "invalid-stroke-id"
        geometry = raw_stroke.get("geometry", _MISSING)
        if (
            pen == "wall"
            and isinstance(geometry, dict)
            and geometry.get("kind") == "rect"
        ):
            witnesses.append(
                UnmeasurableObservationWitnessV1(
                    source_input_id=binding.input_id,
                    source_stroke_id=stroke_id,
                    component="plan_segments",
                    reason="plan_wall_rect_has_no_centerline_contract",
                    cause_class="product_content",
                    source_geometry_sha256=_audit_sha(geometry),
                )
            )
            continue
        if pen == "wall":
            try:
                _geometry_kind, edges = _plan_wall_edges(geometry)
            except ValueError:
                malformed_segments = True
                witnesses.append(
                    UnmeasurableObservationWitnessV1(
                        source_input_id=binding.input_id,
                        source_stroke_id=stroke_id,
                        component="plan_segments",
                        reason="consumed_geometry_malformed",
                        cause_class="product_content",
                        source_geometry_sha256=_audit_sha(geometry),
                    )
                )
                continue
            for primitive_index, (local_p1, local_p2) in enumerate(edges):
                world_p1 = apply_affine_2d(frame.affine, local_p1)
                world_p2 = apply_affine_2d(frame.affine, local_p2)
                segment_observations.append(
                    ReadingPlanSegmentAuditV1(
                        kind="plan_segment",
                        observation_id=_normalized_observation_id(
                            output_sha256=source_output_sha256,
                            input_id=binding.input_id,
                            stroke_id=stroke_id,
                            component="plan_segments",
                            primitive_index=primitive_index,
                        ),
                        source_input_id=binding.input_id,
                        source_stroke_id=stroke_id,
                        primitive_index=primitive_index,
                        floor_id=binding.floor_id,
                        local_p1=Point2V1(x=local_p1[0], y=local_p1[1]),
                        local_p2=Point2V1(x=local_p2[0], y=local_p2[1]),
                        world_p1=Point2V1(x=world_p1[0], y=world_p1[1]),
                        world_p2=Point2V1(x=world_p2[0], y=world_p2[1]),
                        source_geometry_sha256=_audit_sha(geometry),
                        transform_sha256=frame.preimage_sha256,
                        topology="unknown",
                    )
                )
            continue
        try:
            geometry_kind, local_vertices = _plan_opening_vertices(geometry)
        except ValueError:
            malformed_openings = True
            witnesses.append(
                UnmeasurableObservationWitnessV1(
                    source_input_id=binding.input_id,
                    source_stroke_id=stroke_id,
                    component="plan_openings",
                    reason="consumed_geometry_malformed",
                    cause_class="product_content",
                    source_geometry_sha256=_audit_sha(geometry),
                )
            )
            continue
        world_vertices = tuple(
            apply_affine_2d(frame.affine, point) for point in local_vertices
        )
        opening_observations.append(
            ReadingPlanOpeningAuditV1(
                kind="plan_opening",
                observation_id=_normalized_observation_id(
                    output_sha256=source_output_sha256,
                    input_id=binding.input_id,
                    stroke_id=stroke_id,
                    component="plan_openings",
                    primitive_index=0,
                ),
                source_input_id=binding.input_id,
                source_stroke_id=stroke_id,
                floor_id=binding.floor_id,
                geometry_kind=geometry_kind,
                local_vertices=tuple(
                    Point2V1(x=point[0], y=point[1])
                    for point in local_vertices
                ),
                world_vertices=tuple(
                    Point2V1(x=point[0], y=point[1])
                    for point in world_vertices
                ),
                source_geometry_sha256=_audit_sha(geometry),
                transform_sha256=frame.preimage_sha256,
            )
        )
    if malformed_segments:
        segment_component = _component(
            source_input_id=binding.input_id,
            channel="plan",
            component="plan_segments",
            floor_ids=floor_ids,
            status="not_applicable",
            reasons=("plan_geometry_unsupported",),
            cause_class="product_content",
            denominator_disposition="retain_as_miss",
        )
        segment_observations = []
    else:
        segment_component = _component(
            source_input_id=binding.input_id,
            channel="plan",
            component="plan_segments",
            floor_ids=floor_ids,
            status="applicable",
            observation_count=len(segment_observations),
            transform_sha256=frame.preimage_sha256,
        )
    if malformed_openings:
        opening_component = _component(
            source_input_id=binding.input_id,
            channel="plan",
            component="plan_openings",
            floor_ids=floor_ids,
            status="not_applicable",
            reasons=("plan_opening_geometry_unsupported",),
            cause_class="product_content",
            denominator_disposition="retain_as_miss",
        )
        opening_observations = []
    else:
        opening_component = _component(
            source_input_id=binding.input_id,
            channel="plan",
            component="plan_openings",
            floor_ids=floor_ids,
            status="applicable",
            observation_count=len(opening_observations),
            transform_sha256=frame.preimage_sha256,
        )
    return (
        (segment_component, opening_component),
        tuple(segment_observations) + tuple(opening_observations),
        (frame,),
        tuple(witnesses),
        tuple(findings),
    )


def _elevation_result(
    *,
    entry: RequiredViewEntry,
    binding: ElevationScoreViewBindingV1,
    raw_view: object,
    source_output_sha256: str,
) -> tuple[
    tuple[ReadingComponentApplicabilityV1, ...],
    tuple[ReadingElevationOpeningAuditV1, ...],
    tuple[VerticalDatumCertificateV1, ...],
    tuple[UnmeasurableObservationWitnessV1, ...],
    tuple[ReadingMetadataFindingV1, ...],
    tuple[ReadingFilteredComponentBasisV1, ...],
]:
    floor_ids = tuple(binding.floor_ids)
    if len(floor_ids) != 1:
        datum = _vertical_datum(
            input_id=binding.input_id,
            floor_ids=floor_ids,
            source="multi_floor_unavailable",
            z_origin=None,
        )
        components = _na_components(
            source_input_id=binding.input_id,
            channel="elevation",
            components=_ELEVATION_COMPONENTS,
            floor_ids=floor_ids,
            reason="elevation_floor_partition_unresolved",
            cause_class="trusted_input",
            denominator_disposition="filter",
        )
        exclusions = tuple(
            ReadingFilteredComponentBasisV1(
                source_input_id=binding.input_id,
                component=component,
                floor_ids=tuple(sorted(floor_ids)),
                cause_class="trusted_input",
                reasons=("elevation_floor_partition_unresolved",),
            )
            for component in _ELEVATION_COMPONENTS
        )
        return components, (), (datum,), (), (), exclusions
    if raw_view is _MISSING:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="elevation",
                components=_ELEVATION_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_missing",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
            (),
        )
    if not isinstance(raw_view, dict):
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="elevation",
                components=_ELEVATION_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
            (),
        )
    try:
        parsed = ReadingView.model_validate(raw_view)
    except ValidationError:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="elevation",
                components=_ELEVATION_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            (),
            (),
            (),
            (),
        )

    findings: list[ReadingMetadataFindingV1] = []
    if raw_view.get("image_kind", "missing") != "elevation":
        findings.append(
            ReadingMetadataFindingV1(
                source_input_id=binding.input_id,
                code="image_kind_declaration_mismatch",
                declared_sha256=_metadata_hash(
                    raw_view.get("image_kind", "missing")
                ),
                trusted_sha256=_metadata_hash("elevation"),
            )
        )
    raw_facade = raw_view.get("facade")
    if (
        isinstance(raw_facade, dict)
        and raw_facade.get("view_facade", "missing")
        != binding.facade_family
    ):
        findings.append(
            ReadingMetadataFindingV1(
                source_input_id=binding.input_id,
                code="orientation_declaration_mismatch",
                declared_sha256=_metadata_hash(
                    raw_facade.get("view_facade", "missing")
                ),
                trusted_sha256=_metadata_hash(binding.facade_family),
            )
        )

    scale_origin = raw_view.get("scale_origin")
    raw_z = (
        scale_origin.get("world_z_m", _MISSING)
        if isinstance(scale_origin, dict)
        else _MISSING
    )
    datum: VerticalDatumCertificateV1 | None
    z_origin: float | None
    invalid_z = False
    if raw_z is _MISSING or raw_z is None:
        z_origin = 0.0
        datum = _vertical_datum(
            input_id=binding.input_id,
            floor_ids=floor_ids,
            source="project_convention_2026_07_25",
            z_origin=z_origin,
        )
    else:
        try:
            z_origin = _strict_finite(raw_z)
        except ValueError:
            invalid_z = True
            z_origin = None
            datum = None
        else:
            datum = _vertical_datum(
                input_id=binding.input_id,
                floor_ids=floor_ids,
                source="product_declared",
                z_origin=z_origin,
            )

    # Facade direction declarations are non-load-bearing audit data.  Reading
    # geometry always starts at the image's left edge and world placement is
    # entirely the reviewed binding's responsibility.

    raw_strokes = raw_view.get("strokes")
    if not isinstance(raw_strokes, list):
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="elevation",
                components=_ELEVATION_COMPONENTS,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            () if datum is None else (datum,),
            (),
            tuple(findings),
            (),
        )
    observations: list[ReadingElevationOpeningAuditV1] = []
    witnesses: list[UnmeasurableObservationWitnessV1] = []
    malformed = False
    direction_semantics = entry.direction_semantics
    for raw_stroke in raw_strokes:
        if not isinstance(raw_stroke, dict) or raw_stroke.get("pen") != "window":
            continue
        if raw_stroke.get("visibility") == "hidden" or raw_stroke.get(
            "line_style"
        ) in {"dashed", "dash_dot"}:
            continue
        stroke_id = raw_stroke.get("id")
        if not isinstance(stroke_id, str) or not stroke_id:
            malformed = True
            stroke_id = "invalid-stroke-id"
        geometry = raw_stroke.get("geometry", "missing")
        try:
            geometry_kind, local_x, local_y = _elevation_bounds(geometry)
        except ValueError:
            malformed = True
            witnesses.append(
                UnmeasurableObservationWitnessV1(
                    source_input_id=binding.input_id,
                    source_stroke_id=stroke_id,
                    component="elevation_opening_xy",
                    reason="consumed_geometry_malformed",
                    cause_class="product_content",
                    source_geometry_sha256=_audit_sha(geometry),
                )
            )
            continue
        observation_id = _normalized_observation_id(
            output_sha256=source_output_sha256,
            input_id=binding.input_id,
            stroke_id=stroke_id,
            component="elevation_opening_xy",
            primitive_index=0,
        )
        projected = project_typed_elevation_observation(
            observation=TypedElevationObservation(
                observation_id=observation_id,
                source_input_id=binding.input_id,
                floor_id=floor_ids[0],
                kind="window",
                facade_family=binding.facade_family,
                local_x_interval=local_x,
                z_interval=(
                    None
                    if invalid_z
                    else (
                        local_y[0] + z_origin,
                        local_y[1] + z_origin,
                    )
                ),
            ),
            binding=binding,
            direction_semantics=direction_semantics,
        )
        observations.append(
            ReadingElevationOpeningAuditV1(
                kind="elevation_opening",
                observation_id=observation_id,
                source_input_id=binding.input_id,
                source_stroke_id=stroke_id,
                floor_id=floor_ids[0],
                facade_family=binding.facade_family,
                geometry_kind=geometry_kind,
                local_x_interval=ClosedIntervalV1(
                    lo=local_x[0],
                    hi=local_x[1],
                ),
                local_y_interval=ClosedIntervalV1(
                    lo=local_y[0],
                    hi=local_y[1],
                ),
                world_along_interval=ClosedIntervalV1(
                    lo=projected.world_along_interval[0],
                    hi=projected.world_along_interval[1],
                ),
                z_interval=(
                    None
                    if projected.z_interval is None
                    else ClosedIntervalV1(
                        lo=projected.z_interval[0],
                        hi=projected.z_interval[1],
                    )
                ),
                source_geometry_sha256=_audit_sha(geometry),
                horizontal_transform_sha256=binding.frame_transform_sha256,
                vertical_transform_sha256=(
                    None if datum is None else datum.preimage_sha256
                ),
            )
        )
    if malformed:
        return (
            _na_components(
                source_input_id=binding.input_id,
                channel="elevation",
                components=_ELEVATION_COMPONENTS,
                floor_ids=floor_ids,
                reason="elevation_opening_geometry_unsupported",
                cause_class="product_content",
                denominator_disposition="retain_as_miss",
            ),
            (),
            () if datum is None else (datum,),
            tuple(witnesses),
            tuple(findings),
            (),
        )
    xy = _component(
        source_input_id=binding.input_id,
        channel="elevation",
        component="elevation_opening_xy",
        floor_ids=floor_ids,
        status="applicable",
        observation_count=len(observations),
        transform_sha256=binding.frame_transform_sha256,
    )
    if invalid_z:
        z = _component(
            source_input_id=binding.input_id,
            channel="elevation",
            component="elevation_opening_z",
            floor_ids=floor_ids,
            status="not_applicable",
            reasons=("elevation_vertical_datum_unsupported",),
            cause_class="product_content",
            denominator_disposition="retain_as_miss",
        )
    else:
        assert datum is not None
        z = _component(
            source_input_id=binding.input_id,
            channel="elevation",
            component="elevation_opening_z",
            floor_ids=floor_ids,
            status="applicable",
            observation_count=len(observations),
            transform_sha256=datum.preimage_sha256,
        )
    return (
        (xy, z),
        tuple(observations),
        () if datum is None else (datum,),
        tuple(witnesses),
        tuple(findings),
        (),
    )


def normalize_reading_attempt(
    *,
    raw: object,
    source_output_sha256: str,
    base_manifest,
    score_bindings,
) -> ReadingNormalizationOutcome:
    """Normalize product evidence through trusted manifest/binding frames."""
    decision = identify_reading_contract(raw)
    if decision.contract_id != READING_PRODUCT_CONTRACT:
        raise ValueError("normalize_reading_attempt requires reading_views_v2")
    assert isinstance(raw, dict)
    views = raw["views"]
    assert isinstance(views, dict)
    entries = {
        item.input_id: item
        for item in base_manifest.required_entries()
        if item.view_type in {"plan", "elevation"}
    }
    bindings = {item.input_id: item for item in score_bindings.bindings}
    component_rows: list[ReadingComponentApplicabilityV1] = []
    normalized_observations: list[
        ReadingPlanSegmentAuditV1
        | ReadingPlanOpeningAuditV1
        | ReadingElevationOpeningAuditV1
    ] = []
    plan_frames: list[PlanFrameCertificateV1] = []
    vertical_datums: list[VerticalDatumCertificateV1] = []
    unmeasurable: list[UnmeasurableObservationWitnessV1] = []
    findings: list[ReadingMetadataFindingV1] = []
    exclusions: list[ReadingFilteredComponentBasisV1] = []
    plan_inputs_by_floor: dict[str, list[str]] = {}
    for input_id, entry in entries.items():
        binding = bindings.get(input_id)
        if entry.view_type == "plan" and isinstance(
            binding, PlanScoreViewBindingV1
        ):
            plan_inputs_by_floor.setdefault(binding.floor_id, []).append(
                input_id
            )
    duplicate_plan_inputs = {
        input_id
        for input_ids in plan_inputs_by_floor.values()
        if len(input_ids) > 1
        for input_id in input_ids
    }

    for input_id, entry in sorted(entries.items()):
        binding = bindings.get(input_id)
        raw_view = views.get(entry.expected_output_id, _MISSING)
        if input_id in duplicate_plan_inputs and isinstance(
            binding, PlanScoreViewBindingV1
        ):
            components = _na_components(
                source_input_id=input_id,
                channel="plan",
                components=_PLAN_COMPONENTS,
                floor_ids=(binding.floor_id,),
                reason="multiple_plan_views_per_floor_unsupported",
                cause_class="trusted_input",
                denominator_disposition="filter",
            )
            component_rows.extend(components)
            exclusions.extend(
                ReadingFilteredComponentBasisV1(
                    source_input_id=input_id,
                    component=component,
                    floor_ids=(binding.floor_id,),
                    cause_class="trusted_input",
                    reasons=("multiple_plan_views_per_floor_unsupported",),
                )
                for component in _PLAN_COMPONENTS
            )
            continue
        if entry.view_type == "elevation" and isinstance(
            binding, ElevationScoreViewBindingV1
        ):
            (
                components,
                observations,
                datums,
                witnesses,
                metadata,
                trusted_exclusions,
            ) = _elevation_result(
                entry=entry,
                binding=binding,
                raw_view=raw_view,
                source_output_sha256=source_output_sha256,
            )
            component_rows.extend(components)
            normalized_observations.extend(observations)
            vertical_datums.extend(datums)
            unmeasurable.extend(witnesses)
            findings.extend(metadata)
            exclusions.extend(trusted_exclusions)
            continue
        if entry.view_type == "plan" and isinstance(
            binding, PlanScoreViewBindingV1
        ):
            (
                components,
                observations,
                frames,
                witnesses,
                metadata,
            ) = _plan_result(
                entry=entry,
                binding=binding,
                raw_view=raw_view,
                source_output_sha256=source_output_sha256,
            )
            component_rows.extend(components)
            normalized_observations.extend(observations)
            plan_frames.extend(frames)
            unmeasurable.extend(witnesses)
            findings.extend(metadata)
            continue
        floor_ids = (
            (binding.floor_id,)
            if isinstance(binding, PlanScoreViewBindingV1)
            else tuple(getattr(binding, "floor_ids", ()))
        )
        components = (
            _PLAN_COMPONENTS
            if entry.view_type == "plan"
            else _ELEVATION_COMPONENTS
        )
        component_rows.extend(
            _na_components(
                source_input_id=input_id,
                channel=entry.view_type,
                components=components,
                floor_ids=floor_ids,
                reason="reading_view_schema_unsupported",
                cause_class="trusted_input",
                denominator_disposition="filter",
            )
        )
        exclusions.extend(
            ReadingFilteredComponentBasisV1(
                source_input_id=input_id,
                component=component,
                floor_ids=tuple(sorted(floor_ids)),
                cause_class="trusted_input",
                reasons=("reading_view_schema_unsupported",),
            )
            for component in components
        )

    expected_output_ids = {
        entry.expected_output_id for entry in entries.values()
    }
    for output_id in sorted(set(views) - expected_output_ids):
        findings.append(
            ReadingMetadataFindingV1(
                source_input_id=output_id,
                code="unbound_reading_view",
                declared_sha256=_audit_sha(views[output_id]),
                trusted_sha256=canonical_sha256("unbound"),
            )
        )

    component_rows.sort(
        key=lambda item: (
            item.source_input_id,
            item.component,
            item.floor_ids,
        )
    )
    observations = tuple(
        sorted(
            normalized_observations,
            key=lambda item: (
                item.source_input_id,
                item.source_stroke_id,
                item.kind,
                0,
            ),
        )
    )
    plan_frames.sort(key=lambda item: (item.input_id, item.floor_id))
    vertical_datums.sort(key=lambda item: (item.input_id, item.floor_ids))
    unmeasurable.sort(
        key=lambda item: (
            item.source_input_id,
            item.source_stroke_id,
            item.component,
        )
    )
    findings.sort(key=lambda item: (item.source_input_id, item.code))
    exclusions.sort(
        key=lambda item: (
            item.source_input_id,
            item.component,
            item.floor_ids,
            item.reasons,
        )
    )
    certificate_raw = {
        "schema_version": "1",
        "helper_version": READING_ADAPTER_VERSION,
        "contract_detector_version": READING_CONTRACT_DETECTOR_VERSION,
        "source_output_sha256": source_output_sha256,
        "product_contract": READING_PRODUCT_CONTRACT,
        "base_view_manifest_sha256": base_manifest.content_sha256,
        "score_view_bindings_sha256": score_bindings.content_sha256,
        "plan_frames": tuple(
            item.model_dump(mode="json") for item in plan_frames
        ),
        "vertical_datums": tuple(
            item.model_dump(mode="json") for item in vertical_datums
        ),
        "component_applicability": tuple(
            item.model_dump(mode="json") for item in component_rows
        ),
        "observations": tuple(
            item.model_dump(mode="json") for item in observations
        ),
        "unmeasurable_observation_witnesses": tuple(
            item.model_dump(mode="json") for item in unmeasurable
        ),
        "elevation_frame_disagreements": (),
        "metadata_findings": tuple(
            item.model_dump(mode="json") for item in findings
        ),
    }
    certificate = ReadingNormalizationCertificateV1(
        schema_version="1",
        helper_version=READING_ADAPTER_VERSION,
        contract_detector_version=READING_CONTRACT_DETECTOR_VERSION,
        source_output_sha256=source_output_sha256,
        product_contract=READING_PRODUCT_CONTRACT,
        base_view_manifest_sha256=base_manifest.content_sha256,
        score_view_bindings_sha256=score_bindings.content_sha256,
        plan_frames=tuple(plan_frames),
        vertical_datums=tuple(vertical_datums),
        component_applicability=tuple(component_rows),
        observations=observations,
        unmeasurable_observation_witnesses=tuple(unmeasurable),
        elevation_frame_disagreements=(),
        metadata_findings=tuple(findings),
        content_sha256=canonical_sha256(certificate_raw),
    )
    return ReadingNormalizationOutcome(
        certificate=certificate,
        trusted_capability_dispositions=tuple(exclusions),
    )


def _filtered(
    exclusions: tuple[ReadingFilteredComponentBasisV1, ...],
    *,
    source_input_id: str,
    component: str,
    floor_id: str,
) -> bool:
    return any(
        item.source_input_id == source_input_id
        and item.component == component
        and floor_id in item.floor_ids
        for item in exclusions
    )


def _atom(
    *,
    target_id: str,
    target_kind: Literal["plan_segment", "window"],
    component: str,
    claim: str | None,
    floor_id: str,
    source_input_ids: tuple[str, ...],
    eligible_units: float,
) -> ReadingDenominatorAtomV1:
    raw = {
        "target_id": target_id,
        "target_kind": target_kind,
        "component": component,
        "claim": claim,
        "floor_id": floor_id,
        "source_input_ids": source_input_ids,
        "eligible_units": eligible_units,
    }
    return ReadingDenominatorAtomV1(
        atom_id=f"reading-denominator:{canonical_sha256(raw)}",
        **raw,
    )


def derive_reading_denominator_v1(
    gt,
    base_manifest,
    bindings,
    trusted_capability_dispositions,
) -> tuple[
    ReadingDenominatorBasisV1,
    tuple[ReadingDenominatorAtomV1, ...],
    str,
    str,
]:
    """Derive answer atoms from trusted inputs only.

    The signature deliberately has no product parameter.  The fourth argument
    validates through a wire whose cause literal is only ``trusted_input``.
    """
    exclusions = tuple(
        item
        if isinstance(item, ReadingFilteredComponentBasisV1)
        else ReadingFilteredComponentBasisV1.model_validate(item)
        for item in trusted_capability_dispositions
    )
    exclusions = tuple(
        sorted(
            exclusions,
            key=lambda item: (
                item.source_input_id,
                item.component,
                item.floor_ids,
                item.reasons,
            ),
        )
    )
    basis_preimage = {
        "helper_version": READING_DENOMINATOR_VERSION,
        "gt_content_sha256": gt.content_sha256,
        "base_view_manifest_sha256": base_manifest.content_sha256,
        "score_view_bindings_sha256": bindings.content_sha256,
        "filtered_components": tuple(
            item.model_dump(mode="json") for item in exclusions
        ),
    }
    basis = ReadingDenominatorBasisV1(
        helper_version=READING_DENOMINATOR_VERSION,
        gt_content_sha256=gt.content_sha256,
        base_view_manifest_sha256=base_manifest.content_sha256,
        score_view_bindings_sha256=bindings.content_sha256,
        filtered_components=exclusions,
        content_sha256=canonical_sha256(basis_preimage),
    )

    plan_bindings = tuple(
        item
        for item in bindings.bindings
        if isinstance(item, PlanScoreViewBindingV1)
    )
    elevation_bindings = tuple(
        item
        for item in bindings.bindings
        if isinstance(item, ElevationScoreViewBindingV1)
    )
    atoms: list[ReadingDenominatorAtomV1] = []
    # Import inside the pure helper so this adapter does not acquire a second
    # GT wire dependency.  The extractor is the scorer's one authoritative
    # boundary+interior target constructor; deriving atoms from
    # ``floor.boundary_segments`` alone would silently omit every interior-wall
    # denominator.
    from src.agent.judge.segment_score import extract_gt_plan_segments

    for segment in extract_gt_plan_segments(gt):
        source_views = set(segment.source_ids)
        source_ids = tuple(
            sorted(
                item.input_id
                for item in plan_bindings
                if item.floor_id == segment.floor_id
                and (
                    not source_views
                    or source_views.intersection(item.gt_source_view_ids)
                )
                and not _filtered(
                    exclusions,
                    source_input_id=item.input_id,
                    component="plan_segments",
                    floor_id=segment.floor_id,
                )
            )
        )
        if not source_ids:
            continue
        atoms.append(
            _atom(
                target_id=segment.key,
                target_kind="plan_segment",
                component="plan_segments",
                claim=None,
                floor_id=segment.floor_id,
                source_input_ids=source_ids,
                eligible_units=segment.length,
            )
        )

    component_claims = {
        "plan_openings": ("existence", "along", "width"),
        "elevation_opening_xy": ("existence", "along", "width"),
        "elevation_opening_z": ("sill", "head"),
    }
    for opening in gt.openings:
        if opening.kind != "window":
            continue
        source_views = {item.view_id for item in opening.source_refs}
        for component, claims in component_claims.items():
            candidates = plan_bindings if component == "plan_openings" else elevation_bindings
            source_ids = tuple(
                sorted(
                    item.input_id
                    for item in candidates
                    if (
                        (
                            item.floor_id == opening.floor_id
                            if isinstance(item, PlanScoreViewBindingV1)
                            else opening.floor_id in item.floor_ids
                        )
                        and source_views.intersection(item.gt_source_view_ids)
                        and not _filtered(
                            exclusions,
                            source_input_id=item.input_id,
                            component=component,
                            floor_id=opening.floor_id,
                        )
                    )
                )
            )
            if not source_ids:
                continue
            for claim in claims:
                if claim in {"sill", "head"} and opening.z_interval is None:
                    continue
                atoms.append(
                    _atom(
                        target_id=opening.id,
                        target_kind="window",
                        component=component,
                        claim=claim,
                        floor_id=opening.floor_id,
                        source_input_ids=source_ids,
                        eligible_units=1.0,
                    )
                )

    ordered = tuple(
        sorted(
            atoms,
            key=lambda item: (
                item.target_id,
                item.component,
                item.claim or "",
                item.source_input_ids,
            ),
        )
    )
    denominator_sha256 = canonical_sha256(
        [item.model_dump(mode="json") for item in ordered]
    )
    return basis, ordered, basis.content_sha256, denominator_sha256
