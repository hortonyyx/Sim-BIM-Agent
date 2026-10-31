"""Project a source-BIM plan back onto an explicitly calibrated original image.

This is a review aid.  It does not inspect drawing pixels, infer calibration,
or decide whether the source BIM matches the drawing.
"""
from __future__ import annotations

import copy
import math

from PIL import Image, ImageDraw, ImageFont

from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_floor_selection import select_source_floor, select_source_floor_openings
from src.agent.reading.cv_toolbox.tools import _load_rgb, px_m_calibrator


_MAGENTA = (255, 0, 255)
_ORANGE = (255, 165, 0)
_LIME = (0, 255, 0)
_WALL_REFERENCE = (255, 215, 0)
_DIMENSION_START = (255, 70, 70)
_DIMENSION_END = (90, 220, 255)
_LABEL_BACKGROUND = (20, 20, 20)


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _axis_anchors(
    anchors: list[list[float]], *, axis: str, size: int,
) -> tuple[float, float, list[list[float]]]:
    """Return world-per-pixel slope and intercept for one image axis."""
    if not isinstance(anchors, list) or len(anchors) != 2:
        raise ValueError(f"{axis}_anchors requires exactly two [pixel, world_m] anchors")
    parsed: list[list[float]] = []
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, list) or len(anchor) != 2:
            raise ValueError(f"{axis}_anchors[{index}] must be [pixel, world_m]")
        pixel = _number(anchor[0], name=f"{axis}_anchors[{index}][0]")
        world = _number(anchor[1], name=f"{axis}_anchors[{index}][1]")
        if not 0 <= pixel < size:
            raise ValueError(f"{axis}_anchors[{index}] pixel is outside original image bounds")
        parsed.append([pixel, world])
    (p0, w0), (p1, w1) = parsed
    if p0 == p1 or w0 == w1:
        raise ValueError(f"{axis}_anchors require distinct pixel and world values")
    slope = (w1 - w0) / (p1 - p0)
    return slope, w0 - slope * p0, parsed


def _source_hash(source: dict) -> str:
    if not isinstance(source, dict):
        raise TypeError("source must be a source_model.json object")
    declared = source.get("source_model_sha256")
    if not isinstance(declared, str) or not declared:
        raise ValueError("source requires source_model_sha256")
    actual = _digest({key: value for key, value in source.items() if key != "source_model_sha256"})
    if declared != actual:
        raise ValueError("source_model_sha256 does not match source content")
    return actual


def _plan_points(vertices: object, *, name: str) -> list[tuple[float, float]]:
    if not isinstance(vertices, list) or len(vertices) < 2:
        raise ValueError(f"{name} requires at least two plan vertices")
    points = []
    for index, vertex in enumerate(vertices):
        if not isinstance(vertex, (list, tuple)) or len(vertex) < 2:
            raise ValueError(f"{name}[{index}] must contain x and y")
        points.append((_number(vertex[0], name=f"{name}[{index}][0]"),
                       _number(vertex[1], name=f"{name}[{index}][1]")))
    return points


def _unique_endpoints(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    endpoints: list[tuple[float, float]] = []
    for point in points:
        if point not in endpoints:
            endpoints.append(point)
        if len(endpoints) == 2:
            break
    if len(endpoints) != 2:
        raise ValueError("source opening requires two distinct plan endpoints")
    return endpoints


def _overlay_font(image: Image.Image) -> ImageFont.ImageFont:
    """Use readable labels on plans without making small test images unusable."""
    size = 18 if max(image.size) >= 1000 else 12
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:  # Pillow distributions without the bundled/system font.
        return ImageFont.load_default(size=size)


def _label(draw: ImageDraw.ImageDraw, point: tuple[float, float], text: str,
           *, colour: tuple[int, int, int], font: ImageFont.ImageFont,
           offset: tuple[float, float] = (4, -4)) -> None:
    """Draw a compact high-contrast label next to, rather than instead of, a point."""
    x, y = point
    label_xy = (x + offset[0], y + offset[1])
    left, top, right, bottom = draw.textbbox(label_xy, text, font=font)
    draw.rounded_rectangle((left - 2, top - 1, right + 2, bottom + 1), radius=2,
                           fill=_LABEL_BACKGROUND)
    draw.text(label_xy, text, fill=colour, font=font)


def _rounded_point(point: tuple[float, float]) -> list[float]:
    return [round(point[0], 6), round(point[1], 6)]


def _wall_evidence_projection(
    *,
    source: dict,
    image_name: str,
    floor_id: str,
    selected_walls: list[dict],
    project,
    map_pixel,
    outside,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
) -> dict:
    """Render and describe declared wall/endpoint evidence for one original image.

    This deliberately only maps evidence through the caller's transform.  It
    neither samples the drawing nor decides whether the named host is correct.
    """
    walls_by_id = {wall["id"]: wall for wall in selected_walls}
    wall_rows = []
    for number, wall in enumerate(selected_walls, start=1):
        endpoints = _unique_endpoints(_plan_points(
            wall.get("representative_endpoints"), name=f"wall {wall['id']} representative_endpoints"))
        pixels = [project(point) for point in endpoints]
        marker = f"W{number}"
        draw.line(pixels, fill=_WALL_REFERENCE, width=3)
        midpoint = ((pixels[0][0] + pixels[1][0]) / 2, (pixels[0][1] + pixels[1][1]) / 2)
        _label(draw, midpoint, marker, colour=_WALL_REFERENCE, font=font)
        wall_rows.append({
            "marker": marker,
            "wall_id": wall["id"],
            "boundary_ids": copy.deepcopy(wall["boundary_ids"]),
            "evidence_status": wall.get("evidence_status"),
            "world_endpoints_m": [_rounded_point(point) for point in endpoints],
            "pixel_endpoints": [_rounded_point(point) for point in pixels],
            "out_of_image": any(outside(point) for point in pixels),
        })

    endpoint_rows = []
    report = source.get("wall_dimension_report", {})
    dimensions = report.get("dimensions", []) if isinstance(report, dict) else []
    if not isinstance(dimensions, list):
        dimensions = []
    for dimension_number, dimension in enumerate(dimensions, start=1):
        if not isinstance(dimension, dict) or not isinstance(dimension.get("id"), str):
            continue
        for endpoint_name in ("start", "end"):
            endpoint = dimension.get(endpoint_name)
            if not isinstance(endpoint, dict) or endpoint.get("image") != image_name:
                continue
            wall_id = endpoint.get("wall_id")
            wall = walls_by_id.get(wall_id)
            # A dimension may share the image with another floor; its host
            # decides whether this endpoint belongs in the current plan view.
            if wall is None:
                continue
            pixel_value = endpoint.get("pixel")
            if not isinstance(pixel_value, list) or len(pixel_value) != 2:
                continue
            pixel = (_number(pixel_value[0], name="dimension endpoint pixel x"),
                     _number(pixel_value[1], name="dimension endpoint pixel y"))
            world = map_pixel(pixel)
            host = _unique_endpoints(_plan_points(
                wall.get("representative_endpoints"), name=f"wall {wall_id} representative_endpoints"))
            normal_axis = wall.get("axis")
            if normal_axis not in {"x", "y"}:
                continue
            normal_index = 0 if normal_axis == "x" else 1
            tangent_index = 1 - normal_index
            tangent_values = [point[tangent_index] for point in host]
            tangent_range = [min(tangent_values), max(tangent_values)]
            tangent = world[tangent_index]
            outside_distance = max(tangent_range[0] - tangent, 0.0, tangent - tangent_range[1])
            normal_coordinate = host[0][normal_index]
            host_pixels = [project(point) for point in host]
            marker = f"D{dimension_number}:{'S' if endpoint_name == 'start' else 'E'}"
            colour = _DIMENSION_START if endpoint_name == "start" else _DIMENSION_END
            radius = 4
            draw.ellipse((pixel[0] - radius, pixel[1] - radius, pixel[0] + radius, pixel[1] + radius),
                         fill=colour, outline=_LABEL_BACKGROUND, width=1)
            # The source pixel remains the dot.  Opposite offsets let an
            # adjacent S/E pair stay readable without inventing a new point.
            _label(draw, pixel, marker, colour=colour, font=font,
                   offset=(-32, -18) if endpoint_name == "start" else (5, 6))
            endpoint_rows.append({
                "marker": marker,
                "dimension_id": dimension["id"],
                "endpoint": endpoint_name,
                "wall_id": wall_id,
                "boundary_ids": copy.deepcopy(wall["boundary_ids"]),
                "image": image_name,
                # Preserve the evidence coordinates exactly as declared; the
                # mapped point is separate and is never written back to source.
                "pixel": copy.deepcopy(pixel_value),
                "mapped_world_point_m": _rounded_point(world),
                "projected_host_endpoints": [_rounded_point(point) for point in host_pixels],
                "host_segment": {
                    "world_endpoints_m": [_rounded_point(point) for point in host],
                    "normal_axis": normal_axis,
                    "tangential_axis": "y" if normal_axis == "x" else "x",
                    "normal_coordinate_m": round(normal_coordinate, 6),
                },
                "tangential_coordinate_m": round(tangent, 6),
                "tangential_range_m": [round(value, 6) for value in tangent_range],
                "tangential_outside_distance_m": round(outside_distance, 6),
                "normal_displacement_m": round(world[normal_index] - normal_coordinate, 6),
                "out_of_image": outside(pixel),
                "status": "conditional_projection_not_visually_verified",
            })

    return {
        "scope": {
            "image_name": image_name,
            "floor_id": floor_id,
            "endpoint_selection": "exact original image name and linked wall floor",
            "view_status": "not_visually_verified",
        },
        "walls": wall_rows,
        "endpoints": endpoint_rows,
        "limits": [
            "Endpoint pixels are the declared original-image evidence and are drawn without inferred movement.",
            "Tangential range and normal displacement are conditional on the caller-supplied axis-aligned calibration.",
            "A dimension extension-line tick may legitimately lie outside its linked finite wall segment; this is a spatial fact, not an error or correctness verdict.",
            "This view does not inspect pixels, choose a host wall, or mutate source geometry or evidence.",
        ],
    }


def render_source_overlay(
    source: dict,
    image: Image.Image,
    *,
    floor_id: str,
    x_anchors: list[list[float]],
    y_anchors: list[list[float]],
    basis: str,
    image_name: str | None = None,
) -> tuple[Image.Image, dict]:
    """Draw one source floor over an original plan using a caller-supplied affine frame.

    ``x_anchors`` and ``y_anchors`` each contain exactly two ``[pixel, world_m]``
    pairs, matching the MCP ``map_pixels`` format.  The transform is axis-aligned
    only: rotation, perspective, and nonlinear scan distortion are unsupported.
    The returned metadata deliberately marks calibration and drawing fidelity as
    unverified; this function supplies a visual comparison, not an image verdict.
    """
    source_hash = _source_hash(source)
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL.Image.Image")
    if not isinstance(floor_id, str) or not floor_id:
        raise ValueError("floor_id must be a nonempty string")
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("basis must be a nonempty calibration statement")
    if image_name is not None and (not isinstance(image_name, str) or not image_name):
        raise ValueError("image_name must be a nonempty string or null")

    x_slope, x_offset, parsed_x = _axis_anchors(x_anchors, axis="x", size=image.width)
    y_slope, y_offset, parsed_y = _axis_anchors(y_anchors, axis="y", size=image.height)
    calibration = px_m_calibrator([
        {"axis": "x", "px_a": parsed_x[0][0], "px_b": parsed_x[1][0],
         "value_m": abs(parsed_x[1][1] - parsed_x[0][1]), "dimension_ref": "caller_x_anchors"},
        {"axis": "y", "px_a": parsed_y[0][0], "px_b": parsed_y[1][0],
         "value_m": abs(parsed_y[1][1] - parsed_y[0][1]), "dimension_ref": "caller_y_anchors"},
    ])
    calibration_result = calibration["results"][0]

    floors = source.get("floors")
    spaces = source.get("spaces")
    openings = source.get("openings")
    if not isinstance(floors, list) or not isinstance(spaces, list) or not isinstance(openings, list):
        raise ValueError("source requires floors, spaces, and openings lists")
    floor_ids = {row.get("id") for row in floors if isinstance(row, dict)}
    if floor_id not in floor_ids:
        raise ValueError("unknown source floor_id")

    spaces_by_id: dict[str, dict] = {}
    for space in spaces:
        if not isinstance(space, dict) or not isinstance(space.get("id"), str) or not space["id"]:
            raise ValueError("source space requires a nonempty id")
        if space["id"] in spaces_by_id:
            raise ValueError("duplicate source space id")
        if space.get("floor_id") not in floor_ids:
            raise ValueError("source space references unknown floor")
        spaces_by_id[space["id"]] = space
    floor, selected_spaces = select_source_floor(source, floor_id)
    selected_opening_ids = {row["id"] for row in select_source_floor_openings(source, floor, selected_spaces)}

    def project(point: tuple[float, float]) -> tuple[float, float]:
        return ((point[0] - x_offset) / x_slope, (point[1] - y_offset) / y_slope)

    def map_pixel(point: tuple[float, float]) -> tuple[float, float]:
        return (x_slope * point[0] + x_offset, y_slope * point[1] + y_offset)

    def outside(point: tuple[float, float]) -> bool:
        return point[0] < 0 or point[0] >= image.width or point[1] < 0 or point[1] >= image.height

    overlay = _load_rgb(image)
    draw = ImageDraw.Draw(overlay)
    projected_spaces = []
    for space in selected_spaces:
        world_polygon = _plan_points(space.get("polygon"), name=f"space {space['id']} polygon")
        pixel_polygon = [project(point) for point in world_polygon]
        draw.line(pixel_polygon + [pixel_polygon[0]], fill=_MAGENTA, width=3, joint="curve")
        pixel_holes = [[project(point) for point in _plan_points(h, name=f"space {space['id']} hole")]
                       for h in space.get('holes', [])]
        for hole in pixel_holes:
            draw.line(hole + hole[:1], fill=_MAGENTA, width=3, joint='curve')
        projected_spaces.append({
            "id": space["id"],
            **({"plan_geometry": "storey volume XY projection", "projection_z_m": space["plan_projection_z_m"]}
               if "plan_projection_z_m" in space else {}),
            "pixel_polygon": [[round(x, 6), round(y, 6)] for x, y in pixel_polygon],
            **({'pixel_holes': [[[round(x, 6), round(y, 6)] for x, y in h] for h in pixel_holes]} if pixel_holes else {}),
            "out_of_image": any(outside(point) for point in pixel_polygon),
        })

    projected_openings = []
    projected_wall_faces = []
    selected_walls = []
    for wall in source.get("wall_references", []):
        if wall["floor_id"] != floor_id:
            continue
        selected_walls.append(wall)
        faces = wall.get("face_endpoints")
        pixels = [] if faces is None else [[project(tuple(p)) for p in face] for face in faces]
        for face in pixels:
            draw.line([(round(x), round(y)) for x, y in face], fill=(0, 160, 255), width=2)
        projected_wall_faces.append({
            "id": wall["id"], "boundary_ids": wall["boundary_ids"],
            "evidence_status": wall["evidence_status"], "offsets_m": wall.get("offsets_m"),
            "face_status": "unknown_offsets" if faces is None else "derived_from_declared_offsets",
            "pixel_faces": [[[round(x, 6), round(y, 6)] for x, y in face] for face in pixels],
            "out_of_image": any(outside(p) for face in pixels for p in face),
        })
    for opening in openings:
        if not isinstance(opening, dict) or not isinstance(opening.get("id"), str) or not opening["id"]:
            raise ValueError("source opening requires a nonempty id")
        space_ids = opening.get("space_ids")
        if not isinstance(space_ids, list) or not space_ids or any(sid not in spaces_by_id for sid in space_ids):
            raise ValueError(f"source opening {opening['id']} has invalid space_ids")
        if opening["id"] not in selected_opening_ids:
            continue
        endpoints = [project(point) for point in _unique_endpoints(
            _plan_points(opening.get("vertices"), name=f"opening {opening['id']} vertices"))]
        kind = opening.get("kind")
        colour = _LIME if kind == "window" else _ORANGE
        draw.line(endpoints, fill=colour, width=3)
        projected_openings.append({
            "id": opening["id"],
            "kind": kind,
            "space_ids": list(space_ids),
            "pixel_endpoints": [[round(x, 6), round(y, 6)] for x, y in endpoints],
            "out_of_image": any(outside(point) for point in endpoints),
        })

    # Draw evidence last so existing plan/opening lines cannot cover its labels.
    wall_evidence_projection = None
    if image_name is not None:
        wall_evidence_projection = _wall_evidence_projection(
            source=source, image_name=image_name, floor_id=floor_id,
            selected_walls=selected_walls, project=project, map_pixel=map_pixel,
            outside=outside, draw=draw, font=_overlay_font(image))

    metadata = {
        "mode": "source_image_overlay",
        "source_model_sha256": source_hash,
        "floor_id": floor_id,
        "basis": basis,
        "calibration_basis": "caller_supplied_not_independently_verified",
        "calibration_unverified": True,
        "drawing_fidelity": "not_evaluated",
        "anchors": {"x": parsed_x, "y": parsed_y},
        "scale": {
            "world_metres_per_pixel": {"x": x_slope, "y": y_slope},
            "pixels_per_world_metre": {"x": 1.0 / x_slope, "y": 1.0 / y_slope},
            "axis_px_per_m": calibration_result["axis_px_per_m"],
            "warnings": copy.deepcopy(calibration_result["warnings"]),
        },
        "projected_spaces": projected_spaces,
        "projected_openings": projected_openings,
        "projected_wall_faces": projected_wall_faces,
        "legend": {"magenta": "source representative boundary", "blue": "declared wall faces; evidence status in metadata",
                   "green": "window", "orange": "door/open aperture"},
        "out_of_image": {
            "space_ids": [row["id"] for row in projected_spaces if row["out_of_image"]],
            "opening_ids": [row["id"] for row in projected_openings if row["out_of_image"]],
            "wall_reference_ids": [row["id"] for row in projected_wall_faces if row["out_of_image"]],
        },
        "unsupported": ["rotation", "perspective", "nonlinear_distortion"],
        "limits": [
            "Two caller-supplied anchors per axis define the transform but do not independently close a dimension chain.",
            "The overlay does not inspect original pixels or automatically decide positional error.",
        ],
    }
    if wall_evidence_projection is not None:
        metadata["wall_evidence_projection"] = wall_evidence_projection
        metadata["legend"].update({
            "yellow": "W# resolved wall-reference representative segment; full wall mapping in wall_evidence_projection",
            "red": "D#:S original dimension start evidence point",
            "cyan": "D#:E original dimension end evidence point",
        })
    return overlay, metadata
