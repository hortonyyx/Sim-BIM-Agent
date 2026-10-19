"""Explicit circular-arc approximation for the common polygon source contract.

One shared curve declaration has one deterministic chord sequence. The source
stores those planar chords, their declared geometric tolerance and provenance;
it never claims an analytic curved surface or an error bound to the input mesh.
"""
from __future__ import annotations

import copy
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, field_validator
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

JOIN_TOLERANCE_M = 1e-8
MAX_CURVE_SEGMENTS = 4096
Point = tuple[FiniteFloat, FiniteFloat]


class CurveBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_refs: list[str] = Field(min_length=1)

    @field_validator("source_refs")
    @classmethod
    def nonblank_references(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("curve source_refs must be nonblank")
        return values


class LineCurve(CurveBase):
    kind: Literal["line"]
    p1: Point
    p2: Point


class CircularArcCurve(CurveBase):
    kind: Literal["circular_arc"]
    center: Point
    radius_m: Annotated[FiniteFloat, Field(gt=0)]
    start_degrees: FiniteFloat
    sweep_degrees: Annotated[FiniteFloat, Field(ge=-360, le=360)]
    max_chord_error_m: Annotated[FiniteFloat, Field(gt=0)]


BoundaryCurve = Annotated[LineCurve | CircularArcCurve, Field(discriminator="kind")]


class CurveUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    curve_id: str = Field(min_length=1)
    reverse: bool = False


class CurveSegment(BaseModel):
    """One aperture on one chord, not an aperture bent across several planes."""
    model_config = ConfigDict(extra="forbid")
    curve_id: str = Field(min_length=1)
    segment_index: int = Field(ge=0, strict=True)
    span_fraction: tuple[FiniteFloat, FiniteFloat]


def discretize_curves(raw):
    curves = TypeAdapter(dict[str, BoundaryCurve]).validate_python(raw)
    declarations, sampled = {}, {}
    for identity, curve in curves.items():
        if not identity.strip():
            raise ValueError("boundary curve IDs must be nonblank")
        declaration = curve.model_dump(mode="json")
        if curve.kind == "line":
            if math.dist(curve.p1, curve.p2) <= JOIN_TOLERANCE_M:
                raise ValueError(f"curve {identity}: line is degenerate")
            points, error = [list(curve.p1), list(curve.p2)], 0.0
        else:
            sweep = math.radians(curve.sweep_degrees)
            if abs(sweep) <= 1e-12:
                raise ValueError(f"curve {identity}: arc sweep must be nonzero")
            # Stable for small requested errors; a quarter-circle also limits
            # very coarse declarations to a meaningful, nondegenerate polygon.
            step = min(math.pi / 2, 4 * math.asin(math.sqrt(min(curve.max_chord_error_m / (2 * curve.radius_m), .5))))
            count = math.ceil(abs(sweep) / step) if step else MAX_CURVE_SEGMENTS + 1
            if count > MAX_CURVE_SEGMENTS:
                raise ValueError(f"curve {identity}: tolerance requires more than {MAX_CURVE_SEGMENTS} segments")
            start = math.radians(curve.start_degrees % 360)
            points = [[curve.center[0] + curve.radius_m * math.cos(start + sweep * i / count),
                       curve.center[1] + curve.radius_m * math.sin(start + sweep * i / count)]
                      for i in range(count + 1)]
            if abs(curve.sweep_degrees) == 360:
                points[-1] = list(points[0])
            error = 2 * curve.radius_m * math.sin(abs(sweep) / count / 4) ** 2
        declarations[identity] = declaration
        sampled[identity] = {"kind": curve.kind, "points": points,
            "segment_count": len(points) - 1, "maximum_chord_deviation_m": error,
            "declared_max_chord_error_m": getattr(curve, "max_chord_error_m", 0.0),
            "source_refs": list(curve.source_refs),
            "parameter": "fraction of declared curve from start to end, before path reversal",
            "numerical_join_tolerance_m": JOIN_TOLERANCE_M,
            "scope": "approximation of the declared curve only; mesh/drawing fidelity is not evaluated"}
    return declarations, sampled


def path_vertices(raw_path, sampled):
    uses = TypeAdapter(list[CurveUse]).validate_python(raw_path)
    if not uses:
        raise ValueError("boundary_path must contain at least one curve")
    ring = []
    for use in uses:
        if use.curve_id not in sampled:
            raise ValueError(f"boundary_path references unknown curve {use.curve_id}")
        points = copy.deepcopy(sampled[use.curve_id]["points"])
        if use.reverse:
            points.reverse()
        if ring and math.dist(ring[-1], points[0]) > JOIN_TOLERANCE_M:
            raise ValueError("boundary_path curves must meet end to start; no implicit fitting")
        ring.extend(points if not ring else points[1:])
    if len(ring) < 4 or math.dist(ring[0], ring[-1]) > JOIN_TOLERANCE_M:
        raise ValueError("boundary_path must explicitly close a positive-area ring")
    shape = Polygon(ring[:-1])
    if not shape.is_valid or shape.area <= JOIN_TOLERANCE_M ** 2:
        raise ValueError("boundary_path is self-intersecting or degenerate")
    points = [list(p) for p in list(orient(shape, sign=1).exterior.coords)[:-1]]
    return points, [use.model_dump(mode="json") for use in uses]


def _same_ring(actual, expected, label):
    # Vertex start/winding are encoding; a supplied different shape is not.
    first, second = Polygon(actual), Polygon(expected)
    if (not first.is_valid or len(actual) != len(expected)
            or first.boundary.hausdorff_distance(second.boundary) > JOIN_TOLERANCE_M):
        raise ValueError(f"{label} disagrees with boundary_path; edit the curve declaration")


def _ring(raw, label):
    points = TypeAdapter(list[Point]).validate_python(raw)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        raise ValueError(f"{label} requires at least three points")
    return [list(p) for p in points]


def expand_source_polygon_geometry(raw: dict) -> dict:
    """Expand only declared geometry; works on incomplete template containers.

    Roles, storey identities/heights and spatial coverage are validated by their
    existing owners. This function never partitions a room or infers a curve.
    """
    result = copy.deepcopy(raw)
    if result.get("schema_version") != "source_polygon_v1":
        raise ValueError("curve expansion requires explicit source_polygon_v1")
    declarations, sampled = discretize_curves(result.get("boundary_curves", {}))
    result["boundary_curves"] = declarations
    prior = result.get("curve_discretization")
    if prior is not None and prior != sampled:
        raise ValueError("curve_discretization does not match the declared curves")
    result["curve_discretization"] = sampled
    all_points = []
    for floor in result.get("floors", []):
        footprint = floor.get("footprint")
        if isinstance(footprint, dict):
            if footprint.get("boundary_path") is not None:
                points, uses = path_vertices(footprint["boundary_path"], sampled)
                if footprint.get("vertices") is not None:
                    _same_ring(footprint["vertices"], points, "footprint.vertices")
                footprint.update(vertices=points, boundary_path=uses)
            if footprint.get("vertices") is not None:
                footprint["vertices"] = _ring(footprint["vertices"], "footprint.vertices")
                footprint["holes"] = [_ring(hole, "footprint hole") for hole in footprint.get("holes", [])]
                shape = Polygon(footprint["vertices"], footprint["holes"])
                if not shape.is_valid or shape.area <= JOIN_TOLERANCE_M ** 2:
                    raise ValueError("footprint rings/holes must form one valid positive-area polygon")
                all_points.extend(footprint["vertices"])
        for cell in floor.get("cells", []):
            if cell.get("holes"):
                raise ValueError("single-space holes are not supported; never split a room to imitate a hole")
            if cell.get("boundary_path") is not None:
                points, uses = path_vertices(cell["boundary_path"], sampled)
                if cell.get("polygon") is not None:
                    _same_ring(cell["polygon"], points, "cell.polygon")
                cell.update(polygon=points, boundary_path=uses)
            if cell.get("polygon"):
                points = cell["polygon"]
                for field, axis in (("x", 0), ("y", 1)):
                    derived = [min(p[axis] for p in points), max(p[axis] for p in points)]
                    if field in cell and (len(cell[field]) != 2 or any(
                            abs(a-b) > JOIN_TOLERANCE_M for a, b in zip(cell[field], derived))):
                        raise ValueError(f"cell {field} does not match declared boundary")
                    cell[field] = derived
    for opening in result.get("openings", []):
        if opening.get("curve_segment") is None:
            continue
        use = CurveSegment.model_validate(opening["curve_segment"])
        record = sampled.get(use.curve_id)
        lo, hi = use.span_fraction
        if record is None or use.segment_index >= record["segment_count"] or not 0 <= lo < hi <= 1:
            raise ValueError("curve_segment requires an existing chord and 0 <= span_fraction[0] < span_fraction[1] <= 1")
        a, b = record["points"][use.segment_index:use.segment_index+2]
        for field, fraction in (("p1", lo), ("p2", hi)):
            point = [x + fraction * (y-x) for x, y in zip(a, b)]
            if field in opening and math.dist(opening[field], point) > JOIN_TOLERANCE_M:
                raise ValueError(f"opening {field} disagrees with curve_segment")
            opening[field] = point
        opening["curve_segment"] = use.model_dump(mode="json")
    if all_points:
        result.setdefault("footprint_x", [min(p[0] for p in all_points), max(p[0] for p in all_points)])
        result.setdefault("footprint_y", [min(p[1] for p in all_points), max(p[1] for p in all_points)])
    return result


def source_curve_origin(vertices, uses, sampled):
    """Find the declared chord behind an actual (possibly subdivided) wall."""
    points = [p[:2] for p in vertices[:2]]
    origins = []
    for identity in dict.fromkeys(use.curve_id for use in uses or []):
        record = sampled[identity]
        for index, (a, b) in enumerate(zip(record["points"], record["points"][1:])):
            line = LineString([a, b])
            if line.buffer(JOIN_TOLERANCE_M).covers(LineString(points)):
                from shapely.geometry import Point as ShapelyPoint
                parameters = [(index + line.project(ShapelyPoint(point)) / line.length) / record["segment_count"]
                              for point in points]
                origins.append({"curve_id": identity, "segment_index": index,
                    "curve_parameter": parameters,
                    "maximum_chord_deviation_m": record["maximum_chord_deviation_m"],
                    "source_refs": record["source_refs"]})
    return origins
