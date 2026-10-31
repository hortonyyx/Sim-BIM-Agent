"""Typed, judge-side render model for ground-truth visualisations.

The renderers in this module deliberately know only these small immutable
primitives.  Wire/schema interpretation lives in :func:`gt_to_render_model`;
this keeps a renderer from becoming a second (and inevitably looser) loader.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont

from .gt_schema import GtDocument, GroundTruthV3, LegacyGroundTruthV2

Point2 = tuple[float, float]


@dataclass(frozen=True)
class RenderPolygon:
    id: str
    label: str
    role: str
    exterior: tuple[Point2, ...]


@dataclass(frozen=True)
class RenderSegment:
    id: str
    floor_id: str
    facade_family: Literal["North", "South", "East", "West"]
    p1: Point2
    p2: Point2
    depth_m: float
    visible_intervals: tuple[tuple[float, float], ...]
    projection_surface_keys: tuple[str, ...]
    wall_thickness_m: float | None = None


@dataclass(frozen=True)
class RenderOpening:
    id: str
    floor_id: str
    kind: Literal["window", "door"]
    segment_id: str
    world_along_interval: tuple[float, float]
    z_interval: tuple[float, float] | None


@dataclass(frozen=True)
class PlanRenderFloor:
    floor_id: str
    z_floor_m: float
    ceiling_height_m: float
    footprint_exterior: tuple[Point2, ...]
    zone_polygons: tuple[RenderPolygon, ...]
    boundary_segments: tuple[RenderSegment, ...]
    openings: tuple[RenderOpening, ...]


@dataclass(frozen=True)
class ElevationRenderSurface:
    key: str
    source_view_id: str
    floor_ids: tuple[str, ...]
    facade_family: Literal["North", "South", "East", "West"]
    view_kind: Literal["full", "partial"]
    direction_semantics: Literal["building_axis", "true_azimuth"]
    azimuth_deg: float | None
    world_along_coverage: tuple[float, float] | None
    segments: tuple[RenderSegment, ...]
    openings: tuple[RenderOpening, ...]


@dataclass(frozen=True)
class GtRenderModel:
    case: str
    source_schema_version: Literal[2, 3]
    north_axis_deg: float | None
    floors: tuple[PlanRenderFloor, ...]
    elevation_surfaces: tuple[ElevationRenderSurface, ...]
    verification_status: Literal["candidate", "human_verified"] = "candidate"
    reviewer_id: str | None = None
    reviewed_on: str | None = None
    content_sha256: str | None = None


def _point(point: list[float]) -> Point2:
    return (float(point[0]), float(point[1]))


def gt_to_render_model(doc: GtDocument) -> GtRenderModel:
    """Adapt a validated typed document; v2 remains an explicit legacy adapter."""
    if isinstance(doc, GroundTruthV3):
        segment_by_id: dict[str, RenderSegment] = {}
        floors: list[PlanRenderFloor] = []
        openings_by_floor: dict[str, list[RenderOpening]] = {}
        for opening in doc.openings:
            item = RenderOpening(opening.id, opening.floor_id, opening.kind,
                                 opening.boundary_segment_id,
                                 (float(opening.world_along_interval.lo), float(opening.world_along_interval.hi)),
                                 None if opening.z_interval is None else
                                 (float(opening.z_interval.lo), float(opening.z_interval.hi)))
            openings_by_floor.setdefault(item.floor_id, []).append(item)
        for floor in doc.floors:
            segments = tuple(RenderSegment(
                item.id, item.floor_id, item.facade_family, _point(item.p1), _point(item.p2),
                float(item.depth), tuple((float(v.lo), float(v.hi)) for v in item.visible_intervals),
                tuple(item.projection_surface_keys),
                None if item.wall_thickness_m is None else float(item.wall_thickness_m))
                for item in floor.boundary_segments)
            segment_by_id.update({item.id: item for item in segments})
            zones = tuple(RenderPolygon(zone.id, zone.name, zone.role,
                                        tuple(_point(point) for point in zone.polygon.exterior.vertices))
                          for zone in floor.zones)
            floors.append(PlanRenderFloor(floor.id, float(floor.z_floor_m), float(floor.ceiling_height_m),
                                          tuple(_point(point) for point in floor.footprint.exterior.vertices), zones,
                                          segments, tuple(sorted(openings_by_floor.get(floor.id, []), key=lambda x: x.id))))
        surfaces: list[ElevationRenderSurface] = []
        for source in doc.sources:
            for view in source.views:
                if view.kind != "elevation":
                    continue
                key = view.projection_surface_key
                assert key is not None  # schema validation has already established this.
                segments = tuple(sorted((segment for segment in segment_by_id.values() if key in segment.projection_surface_keys),
                                        key=lambda x: (x.floor_id, x.id)))
                ids = {segment.id for segment in segments}
                openings = tuple(sorted((opening for opening in doc.openings if opening.boundary_segment_id in ids),
                                        key=lambda x: x.id))
                surfaces.append(ElevationRenderSurface(
                    key, view.id, tuple(view.floor_ids), view.facade_family, view.view_kind,
                    view.direction_semantics, None if view.azimuth_deg is None else float(view.azimuth_deg),
                    None if view.world_along_coverage is None else (float(view.world_along_coverage.lo), float(view.world_along_coverage.hi)),
                    segments, tuple(RenderOpening(item.id, item.floor_id, item.kind, item.boundary_segment_id,
                                                   (float(item.world_along_interval.lo), float(item.world_along_interval.hi)),
                                                   None if item.z_interval is None else (float(item.z_interval.lo), float(item.z_interval.hi)))
                                    for item in openings)))
        return GtRenderModel(doc.case, 3, None if doc.north_axis_deg is None else float(doc.north_axis_deg),
                             tuple(floors), tuple(sorted(surfaces, key=lambda x: (x.key, x.source_view_id))),
                             doc.verification.status, doc.verification.reviewer_id, doc.verification.reviewed_on,
                             doc.content_sha256)
    return _legacy_render_model(doc)


def _legacy_render_model(doc: LegacyGroundTruthV2) -> GtRenderModel:
    """The old rectangular W/D/four-facade interpretation, isolated for v2."""
    width, depth = float(doc.footprint.W_m), float(doc.footprint.D_m)
    edge = {"South": ((0., 0.), (width, 0.)), "North": ((width, depth), (0., depth)),
            "East": ((width, 0.), (width, depth)), "West": ((0., depth), (0., 0.))}
    floors: list[PlanRenderFloor] = []
    surface_data: dict[str, list[RenderSegment]] = {name: [] for name in edge}
    for floor in doc.floors:
        segments: list[RenderSegment] = []
        for family, (p1, p2) in edge.items():
            span = width if family in {"North", "South"} else depth
            item = RenderSegment(f"{floor.name}:{family}", floor.name, family, p1, p2, 0., ((0., span),), (family,))
            segments.append(item); surface_data[family].append(item)
        openings: list[RenderOpening] = []
        for group in doc.windows:
            if group.floor == floor.name:
                openings.extend(RenderOpening(f"{floor.name}:{group.facade}:window:{n}", floor.name, "window",
                                              f"{floor.name}:{group.facade}", (float(item.x_m), float(item.x_m + item.width_m)),
                                              (float(item.sill_m), float(item.head_m))) for n, item in enumerate(group.openings))
        for n, item in enumerate(doc.doors):
            if item.floor == floor.name:
                openings.append(RenderOpening(f"{floor.name}:{item.facade}:door:{n}", floor.name, "door",
                                              f"{floor.name}:{item.facade}", (float(item.x_m), float(item.x_m + item.width_m)),
                                              (float(item.sill_m), float(item.head_m))))
        zones = tuple(RenderPolygon(z.id, z.id, z.role, ((float(z.rect_m[0]), float(z.rect_m[1])),
              (float(z.rect_m[2]), float(z.rect_m[1])), (float(z.rect_m[2]), float(z.rect_m[3])),
              (float(z.rect_m[0]), float(z.rect_m[3])))) for z in floor.zones)
        floors.append(PlanRenderFloor(floor.name, float(floor.z_floor), float(floor.ceiling_height),
                                      ((0., 0.), (width, 0.), (width, depth), (0., depth)), zones,
                                      tuple(segments), tuple(openings)))
    surfaces = tuple(ElevationRenderSurface(key, key, tuple(f.floor_id for f in floors), key, "full", "building_axis", None,
                  None, tuple(value), tuple(item for floor in floors for item in floor.openings
                                            if item.segment_id.endswith(f":{key}")))
                  for key, value in sorted(surface_data.items()))
    return GtRenderModel(doc.case, 2, None, tuple(floors), surfaces, "human_verified" if doc.verified else "candidate",
                         doc.verified_by, doc.verified_on, doc.cad_sha256)


_FONTS: dict[int, ImageFont.FreeTypeFont] = {}
def _font(size: int) -> ImageFont.FreeTypeFont:
    return _FONTS.setdefault(size, ImageFont.load_default(size=size))

def _header(draw: ImageDraw.ImageDraw, model: GtRenderModel, title: str) -> None:
    hash_text = (model.content_sha256 or "unhashed")[:12]
    line = f"GT v{model.source_schema_version}  {model.case}  hash {hash_text}  {model.verification_status}"
    draw.text((12, 8), f"{title} — {line}", fill="#202020", font=_font(18))
    if model.verification_status == "human_verified":
        draw.text((12, 32), f"reviewer {model.reviewer_id}  date {model.reviewed_on}", fill="#202020", font=_font(13))

def _watermark(draw: ImageDraw.ImageDraw, image: Image.Image, model: GtRenderModel) -> None:
    if model.verification_status == "candidate":
        draw.rectangle((0, image.height - 30, image.width, image.height), fill="#500000")
        draw.text((12, image.height - 25), "CANDIDATE — NOT BASELINE", fill="white", font=_font(17))

def _bounds(points: tuple[Point2, ...]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points); return min(xs), min(ys), max(xs), max(ys)


def _world_point_at_along(segment: RenderSegment, along: float) -> Point2:
    """Map an absolute canonical world-along value to the segment's world point."""
    if segment.facade_family in {"North", "South"}:
        return (along, segment.p1[1])
    return (segment.p1[0], along)


# --- sm21-form annotation helpers (TYPE 1 "GT renders itself") ----------------- #
# Every number these draw is derived from the typed model; none is written down.
_ROLE_FILL = {"office": "#cfe3f2", "meeting": "#d7ecd2", "corridor": "#fdf0c8",
              "reception": "#efd9f0", "lobby": "#d3eeee"}
_NEUTRAL_FILL = "#e9e9e9"
_DIM = "#128a3c"          # dimension chains (green, as in the sm21 baseline)
_ZDIM = "#1f77b4"         # z / opening chains (blue)
_WIN = "#1f77b4"
_DOOR = "#b5651d"
_FACADE_ORDER = {"South": 0, "North": 1, "East": 2, "West": 3}


def _fmt_m(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _chain(values: tuple[float, ...], lo: float, hi: float, tolerance: float = 1e-6) -> list[float]:
    """Ordered, de-duplicated tick positions spanning [lo, hi] (a dimension chain)."""
    ticks = [lo, hi, *[value for value in values if lo + tolerance < value < hi - tolerance]]
    ordered: list[float] = []
    for value in sorted(ticks):
        if not ordered or value - ordered[-1] > tolerance:
            ordered.append(value)
    return ordered


def _dim_row(draw, ticks: list[float], to_pixel, y: float, colour: str, size: int,
             total: bool = True) -> None:
    """Horizontal dimension chain with a per-segment figure and an overall figure."""
    if len(ticks) < 2:
        return
    draw.line((to_pixel(ticks[0]), y, to_pixel(ticks[-1]), y), fill=colour, width=1)
    for value in ticks:
        draw.line((to_pixel(value), y - 5, to_pixel(value), y + 5), fill=colour, width=1)
    for start, end in zip(ticks, ticks[1:]):
        draw.text(((to_pixel(start) + to_pixel(end)) / 2, y - 15), _fmt_m(end - start),
                  fill=colour, font=_font(size), anchor="mm")
    # A single span already IS the overall figure; printing it twice reads as an error.
    if total and len(ticks) > 2:
        draw.text(((to_pixel(ticks[0]) + to_pixel(ticks[-1])) / 2, y - 31),
                  _fmt_m(ticks[-1] - ticks[0]), fill=colour, font=_font(size + 2), anchor="mm")


def _dim_column(draw, ticks: list[float], to_pixel, x: float, colour: str, size: int,
                total: bool = True) -> None:
    """Vertical dimension chain (figures kept horizontal so they stay legible)."""
    if len(ticks) < 2:
        return
    draw.line((x, to_pixel(ticks[0]), x, to_pixel(ticks[-1])), fill=colour, width=1)
    for value in ticks:
        draw.line((x - 5, to_pixel(value), x + 5, to_pixel(value)), fill=colour, width=1)
    for start, end in zip(ticks, ticks[1:]):
        draw.text((x - 8, (to_pixel(start) + to_pixel(end)) / 2), _fmt_m(end - start),
                  fill=colour, font=_font(size), anchor="rm")
    if total and len(ticks) > 2:
        # placed beyond the chain's top end, never mid-height where a span figure sits
        draw.text((x - 8, to_pixel(ticks[-1]) - 14), _fmt_m(ticks[-1] - ticks[0]),
                  fill=colour, font=_font(size + 2), anchor="rm")


def _label_anchor(exterior: tuple[Point2, ...]) -> Point2:
    """A world point GUARANTEED inside the polygon, for its label.

    The bbox centre (and the centroid) fall OUTSIDE the non-rectangular zones this
    project exists to support: sm24's z5 is an 8-vertex C whose bbox centre lands in a
    neighbouring room, so its label was drawn there and then buried by that room's fill
    — the TYPE 1 plan shipped 7 of 8 zone names.  Same defect the overlay renderer fixed
    earlier; the two renderers share no code, so the remedy has to exist in both.
    """
    from shapely.geometry import Polygon
    from shapely.ops import polylabel
    polygon = Polygon(list(exterior))
    try:
        point = polylabel(polygon, tolerance=max(polygon.length / 1000.0, 1e-9))
        if not polygon.contains(point):
            raise ValueError("polylabel escaped the polygon")
    except Exception:
        point = polygon.representative_point()
    return (point.x, point.y)


def _facade_counts(floor: PlanRenderFloor, by_id: dict[str, RenderSegment]) -> str:
    """`N:3 S:3+door E:1 W:0+door` — the per-facade tally shown under the plan."""
    parts = []
    for family in sorted(_FACADE_ORDER, key=lambda name: _FACADE_ORDER[name]):
        on_family = [opening for opening in floor.openings
                     if by_id[opening.segment_id].facade_family == family]
        windows = sum(1 for opening in on_family if opening.kind == "window")
        doors = sum(1 for opening in on_family if opening.kind == "door")
        parts.append(f"{family[0]}:{windows}" + ("+door" * min(doors, 1)))
    return "  ".join(parts)

def render_plan_model(model: GtRenderModel, *, review_annotations=None) -> Image.Image:
    """TYPE 1 plan: the GT rendering itself, annotated for human comparison.

    Layout follows the sm21 baseline (title + legend, per-floor caption, green
    dimension chains on two sides, role-filled zones with a two-line label, thick
    opening bars, per-facade tally).  Every figure is computed from the model.

    ``review_annotations`` maps ``zone_id -> role`` and is REVIEW-ONLY: it colours and
    names zones for the human reviewer when the GT itself records ``unspecified``.  It
    never enters the GT, never reaches a gate, and any zone it omits stays neutral.
    """
    annotations = dict(review_annotations or {})
    pad_left, pad_top, pad_right, pad_bottom = 132, 150, 46, 104
    panels: list[tuple[PlanRenderFloor, tuple[float, float, float, float], float]] = []
    for floor in model.floors:
        x0, y0, x1, y1 = _bounds(floor.footprint_exterior)
        ppm = min(90., max(40., 620. / max(x1 - x0, y1 - y0, .1)))
        panels.append((floor, (x0, y0, x1, y1), ppm))
    widths = [int((box[2] - box[0]) * ppm) + pad_left + pad_right for _, box, ppm in panels]
    body = max([220] + [int((box[3] - box[1]) * ppm) for _, box, ppm in panels])
    height = body + pad_top + pad_bottom
    image = Image.new("RGB", (max(940, sum(widths) + 24 * max(0, len(widths) - 1)), height + 40), (252, 252, 251))
    draw = ImageDraw.Draw(image); _header(draw, model, "TYPE 1  GT plan (gt's own rendering)")
    draw.text((12, 40), "zone boxes = gt clear-space extents (±wall thickness);  "
                        "blue = windows, brown = door;  green chains = gt dimensions.",
              fill="#555555", font=_font(12))
    draw.text((12, 56), "Compare against the floor-plan drawings.  Room use is a review "
                        "annotation, not gt data.", fill="#555555", font=_font(12))
    cursor = 0; primitives: list[dict[str, object]] = []; north_vectors: list[tuple[float, float] | None] = []; opening_primitives: list[dict[str, object]] = []
    for (floor, box, ppm), panel_width in zip(panels, widths):
        x0, y0, x1, y1 = box; ox, oy = cursor + pad_left, pad_top
        def pix(point: Point2) -> tuple[float, float]: return (ox + (point[0] - x0) * ppm, oy + (y1 - point[1]) * ppm)
        def px(value: float) -> float: return ox + (value - x0) * ppm
        def py(value: float) -> float: return oy + (y1 - value) * ppm
        draw.text((cursor + 18, pad_top - 78),
                  f"{floor.floor_id}  z={floor.z_floor_m:g}  h={floor.ceiling_height_m:g}m  "
                  f"{len(floor.zone_polygons)} zones", fill="#202020", font=_font(15))
        # Pass 1: fills and outlines only.  Labels are held back so a later zone's fill
        # cannot bury an earlier zone's name.
        pending_labels: list[tuple[tuple[float, float], str, str]] = []
        for zone in floor.zone_polygons:
            poly = [pix(point) for point in zone.exterior]
            role = annotations.get(zone.id, zone.role)
            draw.polygon(poly, fill=ImageColor.getrgb(_ROLE_FILL.get(role.lower(), _NEUTRAL_FILL)),
                         outline="#3a3a3a")
            draw.line(poly + [poly[0]], fill="#3a3a3a", width=2)
            anchor = _label_anchor(zone.exterior)
            pending_labels.append(((px(anchor[0]), py(anchor[1])), zone.id, role))
            primitives.append({"type":"zone", "id":zone.id,"points":zone.exterior})
        footprint = [pix(point) for point in floor.footprint_exterior]
        draw.line(footprint + [footprint[0]], fill="#111111", width=3); primitives.append({"type":"footprint", "points":floor.footprint_exterior})
        by_id = {item.id: item for item in floor.boundary_segments}
        for seg in floor.boundary_segments:
            for lo, hi in seg.visible_intervals:
                p, q = pix(_world_point_at_along(seg, lo)), pix(_world_point_at_along(seg, hi))
                draw.line((p, q), fill="#404040", width=3)
        for opening in floor.openings:
            seg = by_id[opening.segment_id]
            a, b = opening.world_along_interval
            points = (_world_point_at_along(seg, a), _world_point_at_along(seg, b))
            draw.line(tuple(pix(point) for point in points),
                      fill=_WIN if opening.kind == "window" else _DOOR, width=8)
            if opening.kind == "door":
                # Caption sits BESIDE the door bar, offset along the facade's inward
                # normal.  Drawn centred on the bar it was overprinted by the bar itself
                # and read as "DO OR".
                mid = pix(_world_point_at_along(seg, (a + b) / 2))
                inward = {"North": (0., 22.), "South": (0., -22.),
                          "East": (-30., 0.), "West": (30., 0.)}[seg.facade_family]
                pending_labels.append(((mid[0] + inward[0], mid[1] + inward[1]), "DOOR", ""))
            opening_primitives.append({"id": opening.id, "points": points})
        # dimension chains: ticks are the zone-boundary coordinates the GT actually has
        zone_x = tuple(point[0] for zone in floor.zone_polygons for point in zone.exterior)
        zone_y = tuple(point[1] for zone in floor.zone_polygons for point in zone.exterior)
        # chain sits just above the footprint: its span figures (-15) and overall (-31)
        # then clear the floor caption at pad_top-78 and the two legend lines above it.
        _dim_row(draw, _chain(zone_x, x0, x1), px, oy - 20, _DIM, 11)
        _dim_column(draw, _chain(zone_y, y0, y1), py, ox - 44, _DIM, 11)
        # Pass 2: every zone name and door caption, after all fills, outlines and bars.
        for centre, primary, secondary in pending_labels:
            if primary == "DOOR":
                draw.text(centre, primary, fill=_DOOR, font=_font(10), anchor="mm")
                continue
            draw.text(centre, primary, fill="#202020", font=_font(13), anchor="mm")
            draw.text((centre[0], centre[1] + 15), secondary, fill="#4a4a4a", font=_font(12), anchor="mm")
        summary = _facade_counts(floor, by_id)
        z_spans = sorted({opening.z_interval for opening in floor.openings
                          if opening.kind == "window" and opening.z_interval is not None})
        if z_spans:
            summary += "   sill-head z " + ", ".join(f"{_fmt_m(lo)}-{_fmt_m(hi)}" for lo, hi in z_spans)
        draw.text((cursor + 18, oy + body + 26), f"windows {summary}", fill="#333333", font=_font(12))
        if model.north_axis_deg is None:
            draw.text((cursor + 18, oy + body + 48), "+x / +y axes; true north unset", fill="#555555", font=_font(12))
            north_vectors.append(None)
        else:
            theta = math.radians(model.north_axis_deg); north = (-math.sin(theta), math.cos(theta))
            base = (cursor + 30, oy + body + 66)
            draw.line((base, (base[0], base[1]-20)), fill="#202020", width=2)
            draw.line((base, (base[0] + north[0]*20, base[1] - north[1]*20)), fill="#0070b0", width=2)
            draw.text((cursor + 46, oy + body + 48), f"building +Y up; true/project N ({north[0]:.6f}, {north[1]:.6f})", fill="#555555", font=_font(12))
            north_vectors.append(north)
        cursor += panel_width + 24
    image.info["render_primitives"] = primitives
    image.info["opening_primitives"] = opening_primitives
    image.info["north_vectors"] = north_vectors
    _watermark(draw, image, model); return image

def render_elevation_model(model: GtRenderModel) -> Image.Image:
    """TYPE 1 elevations, sm21 form: grid of panels, green width/floor chains, blue
    sill/head chain, floor split lines, filled window boxes and a per-floor count."""
    # Facade reading order (S/N/E/W) rather than raw key order; ties stay deterministic.
    surfaces = tuple(sorted(model.elevation_surfaces,
                            key=lambda value: (_FACADE_ORDER.get(value.facade_family, 9),
                                               value.key, value.source_view_id)))
    if not surfaces:
        image = Image.new("RGB", (760, 170), (252, 252, 251)); draw = ImageDraw.Draw(image); _header(draw, model, "GT elevations")
        draw.text((25, 95), "NO ELEVATION SOURCE BINDING", fill="#900000", font=_font(20)); _watermark(draw, image, model); return image
    floor_lookup = {floor.floor_id: floor for floor in model.floors}

    def geometry(surface: ElevationRenderSurface) -> tuple[float, float, float, float]:
        axis_of = lambda segment: 0 if segment.facade_family in {"North", "South"} else 1
        spans = [tuple(sorted((segment.p1[axis_of(segment)], segment.p2[axis_of(segment)])))
                 for segment in surface.segments]
        low = min((span[0] for span in spans), default=0.)
        high = max((span[1] for span in spans), default=1.)
        if surface.world_along_coverage is not None:
            low, high = surface.world_along_coverage
        top = max((floor_lookup[item].z_floor_m + floor_lookup[item].ceiling_height_m
                   for item in surface.floor_ids), default=3.)
        return low, high, top, min(64., 620. / max(high - low, .1))

    # One shared scale keeps the four facades visually comparable, and the panel box is
    # sized from the real drawing extent so nothing is clipped and nothing is dead space.
    shared_ppm = min(geometry(surface)[3] for surface in surfaces)
    spans = [geometry(surface) for surface in surfaces]
    panel_width = int(max(high - low for low, high, _top, _ppm in spans) * shared_ppm) + 210
    panel_height = int(max(top for _low, _high, top, _ppm in spans) * shared_ppm) + 190
    columns = 1 if len(surfaces) == 1 else 2
    rows = (len(surfaces) + columns - 1) // columns
    image = Image.new("RGB", (columns * panel_width + (columns - 1) * 20,
                              rows * panel_height + 96 + 34), (252, 252, 251))
    draw = ImageDraw.Draw(image); _header(draw, model, "TYPE 1  GT elevations (gt's own rendering)")
    draw.text((12, 40), "boxes = windows at gt [sill, head] z;  brown = door;  "
                        "green chains = width / floor heights,  blue chain = sill / opening / head.  "
                        "Window x is gt-exact.", fill="#555555", font=_font(12))
    floor_by_id = {floor.floor_id: floor for floor in model.floors}
    primitives: list[dict[str, object]] = []; segment_primitives: list[dict[str, object]] = []
    for index, surface in enumerate(surfaces):
        ox = (index % columns) * (panel_width + 20)
        oy = 96 + (index // columns) * panel_height
        segments = surface.segments
        # along has to follow the schema's facade-local interval, not a bbox coordinate.
        def extent(segment: RenderSegment) -> tuple[float, float]:
            axis = 0 if segment.facade_family in {"North", "South"} else 1
            return tuple(sorted((segment.p1[axis], segment.p2[axis])))
        lo = min((extent(segment)[0] for segment in segments), default=0.)
        hi = max((extent(segment)[1] for segment in segments), default=1.)
        if surface.world_along_coverage is not None: lo, hi = surface.world_along_coverage
        max_z = max((floor_by_id[item].z_floor_m + floor_by_id[item].ceiling_height_m for item in surface.floor_ids), default=3.)
        ppm = shared_ppm
        base_y = oy + 56 + max_z * ppm
        tx = lambda value: ox + 96 + (value - lo) * ppm
        tz = lambda value: base_y - value * ppm
        windows_here = sum(1 for opening in surface.openings
                           if opening.kind == "window" and opening.z_interval is not None)
        draw.text((ox + 14, oy - 4), f"{surface.facade_family} elevation  {_fmt_m(hi - lo)} m wide"
                                     f"   {windows_here} win (gt-exact x)",
                  fill="#202020", font=_font(15))
        draw.text((ox + 14, oy + 15), f"{surface.key} / {surface.source_view_id}",
                  fill="#777777", font=_font(10))
        draw.rectangle((tx(lo), tz(max_z), tx(hi), tz(0)), outline="#111111", width=2)
        floor_levels = sorted({floor_by_id[item].z_floor_m for item in surface.floor_ids} |
                              {floor_by_id[item].z_floor_m + floor_by_id[item].ceiling_height_m
                               for item in surface.floor_ids})
        for level in floor_levels[1:-1]:
            draw.line((tx(lo), tz(level), tx(hi), tz(level)), fill="#8a8a8a", width=1)
        for segment in segments:
            sl, sh = extent(segment)
            if surface.world_along_coverage is not None and (sh <= lo or sl >= hi): continue
            z_floor = floor_by_id[segment.floor_id].z_floor_m
            for visible_lo, visible_hi in segment.visible_intervals:
                if visible_hi > lo and visible_lo < hi:
                    draw.line((tx(max(lo,visible_lo)), tz(z_floor), tx(min(hi,visible_hi)), tz(z_floor)), fill="#404040", width=3)
            segment_primitives.append({"id": segment.id, "z_floor_m": z_floor, "visible_intervals": segment.visible_intervals})
        drawn = []
        for opening in surface.openings:
            if opening.z_interval is None:
                continue
            a,b = opening.world_along_interval
            if b <= lo or a >= hi: continue
            box = (tx(max(a,lo)), tz(opening.z_interval[1]), tx(min(b,hi)), tz(opening.z_interval[0]))
            if opening.kind == "window":
                draw.rectangle(box, fill="#dceaf6", outline=_WIN, width=3)
            else:
                draw.rectangle(box, outline=_DOOR, width=3)
                draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), "DOOR",
                          fill=_DOOR, font=_font(10), anchor="mm")
            drawn.append(opening)
        # green: width chain (opening edges) below, floor-height chain on the left
        _dim_row(draw, _chain(tuple(value for opening in drawn
                                    for value in opening.world_along_interval), lo, hi),
                 tx, tz(0) + 46, _DIM, 10)
        _dim_column(draw, floor_levels, tz, ox + 84, _DIM, 10)
        # blue: sill / opening height / head, from the real window z values
        window_z = sorted({value for opening in drawn if opening.kind == "window"
                           for value in opening.z_interval})
        if window_z:
            _dim_column(draw, _chain(tuple(window_z), 0., max_z), tz,
                        tx(hi) + 54, _ZDIM, 10, total=False)
        # Per-floor counts sit OUTSIDE the envelope (left gutter), so they can never
        # collide with an opening box or the DOOR caption drawn inside it.
        for floor_id in sorted(surface.floor_ids):
            count = sum(1 for opening in drawn
                        if opening.kind == "window" and opening.floor_id == floor_id)
            level = floor_by_id[floor_id].z_floor_m
            draw.text((ox + 14, tz(level) - 16), f"{floor_id}: {count} win",
                      fill="#333333", font=_font(11))
        note_y = tz(0) + 76
        if surface.world_along_coverage is not None:
            draw.text((ox+14, note_y), "PARTIAL — CLIPPED AT COVERAGE", fill="#900000", font=_font(11)); note_y += 16
        if any(value.z_interval is None for value in surface.openings): draw.text((ox+14, note_y), "PLAN-ONLY / Z UNSET", fill="#900000", font=_font(11))
        primitives.append({"type":"surface", "key":surface.key, "segments":len(segments)})
    image.info["render_primitives"] = primitives; image.info["segment_primitives"] = segment_primitives; _watermark(draw, image, model); return image
