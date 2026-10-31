"""Overlay the DXF-built gt onto the ORIGINAL drawing PNGs — cross-source validation.

The original case_data drawings and the source.dxf are two INDEPENDENT data sources for
the same building. Overlaying gt (built from the DXF) onto the original PNG cross-checks
that the two sources agree: if the gt zones/windows land on the drawing's real
walls/openings, the sources are consistent; if not, there is a real DATA problem (the
DXF and the drawing disagree, or one is wrong). This is the strong human-QA gate — unlike
gt-over-DXF, which (for a deterministic dxf→gt) only re-confirms the code.

Calibration is automatic: the footprint / facade-envelope pixel box is found from
wall-line pixel density per column/row, cross-checked by px-per-metre agreement between
the two axes (a mismatch flags a contaminated edge, corrected from the cleaner axis).

Usage:
    python scripts/tool_scripts/render_gt_overlay.py sm21_anchor
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from shapely.geometry import Polygon
from shapely.ops import polylabel
from src.agent.judge.gt import load_gt_file
from src.agent.judge.gt_manifest import (ElevationViewBindingV1, GtExtractionManifestV1,
                                          PlanViewBindingV1)
from src.agent.judge.gt_render_model import gt_to_render_model
from src.agent.judge.gt_schema import GroundTruthV3

DIM = 0.38     # LEGACY v2 ONLY — original drawing dimmed so the gt overlay stands out.
               # The committed sm21 renders are locked baseline assets, so neither this
               # constant nor overlay_plan/overlay_elev may change (see R-01 lock in
               # tests/test_gt_overlay.py::test_sm21_legacy_overlay_pipeline_is_unchanged).

GT_DIR = Path("case_tests/test_baseline/gt")
CASE_DATA = Path("case_tests/e2e_tests/{case}/case_data")

ROLE = {"office": (90, 140, 220), "meeting": (70, 175, 90), "corridor": (220, 170, 40)}
WIN = (0, 200, 255)
DOOR = (255, 120, 0)

# --- v3 review-render conventions ------------------------------------------- #
# The drawing itself is drawn in cyan (openings), green (dimensions) and white/grey
# (walls), so an overlay that also uses those hues is indistinguishable from the very
# evidence the reviewer must compare against.  v3 therefore renders the base in
# GREYSCALE and draws only saturated hues: every colour in the result belongs to gt.
_REVIEW_BASE_GAIN = 0.75      # ink luma retained (vs 0.38 under the legacy multiply)
_SM21_REFERENCE_WIDTH = 2133  # sm21 plan raster the stroke/label proportions were tuned on
_ENVELOPE = (255, 60, 60)     # footprint / facade envelope
# 2026-08-21 用户令：分区边（= 内墙线）统一一个颜色。此前每个分区用自己的 role 色描边，
# 于是同一道内墙两侧颜色不同、且与 role 填充同色 ⇒ 「有点看不出来」。
# 现在：填充仍按 role（供人认房间类型），描边一律 _ZONE_EDGE（供人认墙位置）。
_ZONE_EDGE = (160, 90, 255)   # 内墙 / 分区边界（紫，与外轮廓红、窗青、门橙均不撞）
_DEPTH_STEP = (60, 230, 120)  # 立面进深台阶（绿）—— 见下方 2026-08-21 注
_DEPTH_STEP_EPS = 1e-6        # 沿墙/进深比较的数值容差（native 世界米）
_NEUTRAL_ROLE = (150, 150, 150)
_V3_ROLE = {**ROLE, "reception": (200, 90, 200), "lobby": (0, 190, 190),
            "unspecified": _NEUTRAL_ROLE}

# FIX-7: openings are recorded on the boundary segment's outer-skin line (the drawing's
# outermost wall edge), but the wall itself is a band `wall_thickness_m` wide running
# INTO the building from there. Drawn on the outer-skin line, the opening bar floats on
# the outside face of the wall instead of sitting on the band. Sign is which way is
# "into the building" for each facade family (North's outer skin is the max-y edge, so
# inward is -y; South's is min-y, so inward is +y; East max-x -> -x; West min-x -> +x).
_INWARD_SIGN = {"North": -1.0, "South": 1.0, "East": -1.0, "West": 1.0}


def _weights(image: Image.Image) -> dict[str, int]:
    """Stroke widths / label size scaled to the raster, in sm21's proportions.

    The 07-24 bundle used absolute constants, so the same 3 px outline was 0.12 % of a
    2434 px elevation (hairline) while a 7 px plan bar was 0.9 % of a 790 px plan
    (fat enough to bury the drawing underneath).  Everything is proportional now.
    """
    scale = image.width / _SM21_REFERENCE_WIDTH
    # `bar` (plan window/door) carries a higher absolute floor than the other strokes:
    # pure proportional scaling put it at 3 px on sm24's 790 px plan raster, which is
    # both too faint to read and too close to the zone outline weight.  6 px is still
    # under the ~9 px the 240 mm wall occupies at that raster's 36.3 px/m, so the bar
    # stays inside the wall band and cannot hide the drawing's own opening lines — the
    # comparison object the reviewer needs.
    return {"line": max(2, round(3 * scale)), "bar": max(6, round(9 * scale)),
            "box": max(2, round(5 * scale)), "font": max(11, round(20 * scale))}


def _review_base(base: Image.Image) -> Image.Image:
    """Greyscale, mildly dimmed compositing base for v3 review overlays.

    Legacy v2 is deliberately NOT routed through here.
    """
    grey = base.convert("L").point(lambda value: int(value * _REVIEW_BASE_GAIN))
    return Image.merge("RGB", (grey, grey, grey)).convert("RGBA")


def _label_anchor(exterior) -> tuple[float, float]:
    """A world point GUARANTEED inside the zone polygon, for its label.

    Neither a bbox corner nor a centroid is safe for the non-rectangular zones this
    project exists to support.  Concretely, in the 07-24/07-25 sm24 bundle the anchor
    was the bbox NW corner, and for z4 (6-vertex L) that corner lands inside z5's
    corridor strip; because zones are painted z0..z7, z5's fill then covered z4's
    label and the delivered plan showed only 7 of 8 zone labels — silently breaking
    the one thing the human sign-off is supposed to confirm.

    ``polylabel`` is the pole of inaccessibility (the most interior point, best for
    text); ``representative_point`` is the guaranteed-inside fallback.
    """
    polygon = Polygon(list(exterior))
    try:
        point = polylabel(polygon, tolerance=max(polygon.length / 1000.0, 1e-9))
        if not polygon.contains(point):
            raise ValueError("polylabel escaped the polygon")
    except Exception:
        point = polygon.representative_point()
    return (point.x, point.y)


def _outline(draw, box, colour, width, dash=0):
    """Four explicit equal-width edges, optionally dashed.

    NOTE (verified 2026-07-25, Pillow 12.2.0): ``ImageDraw.rectangle(..., width=w)``
    does render all four edges at the full width — the "PIL bottom edge is only 1 px"
    premise does NOT hold in this version, so that is not why this helper exists.

    It exists for ``dash``, which ``rectangle`` cannot do: an opening box drawn solid
    sits exactly on top of the drawing's own opening frame and hides it, which is
    precisely the evidence the reviewer must compare against.  Dashes keep the
    coordinates exact while letting the drawing show through the gaps.
    """
    x0, y0, x1, y1 = box
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                 ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        if not dash:
            draw.line([a, b], fill=colour, width=width)
            continue
        length = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        steps = max(1, int(length // dash))
        for step in range(0, steps, 2):
            t0, t1 = step / steps, min((step + 1) / steps, 1.0)
            draw.line([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                       (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)],
                      fill=colour, width=width)


def _label(draw, image, point, text, colour, size, anchor="la"):
    """Draw a label clamped inside the image, with a dark halo for legibility."""
    x = min(max(point[0], 2), max(image.width - 2, 2))
    y = min(max(point[1], 2), max(image.height - 2, 2))
    draw.text((x, y), text, font=_font(size), fill=colour, anchor=anchor,
              stroke_width=max(1, size // 10), stroke_fill=(0, 0, 0, 255))
# elevation x runs west→east for S, south→north for E; N & W are viewed mirrored.
_MIRRORED = {"North", "West"}
_FACADE_PNG = {"South": "South_view", "North": "North_view",
               "East": "East_view", "West": "West_view"}
_FLOOR_PNG = {"Floor 1": "1f_view", "Floor 2": "2f_view"}


def _font(sz):
    return ImageFont.load_default(size=sz)


def _density_box(mask):
    cden, rden = mask.sum(0), mask.sum(1)
    if mask.sum() < 100:
        return None
    cols = np.where(cden > cden.max() * 0.38)[0]
    rows = np.where(rden > rden.max() * 0.38)[0]
    return int(cols.min()), int(cols.max()), int(rows.min()), int(rows.max())


def _box_gray(im):
    r, g, b = im[:, :, 0].astype(int), im[:, :, 1].astype(int), im[:, :, 2].astype(int)
    return _density_box((abs(r - g) < 25) & (abs(g - b) < 25) & (r > 60) & (r < 210))


def _box_white(im):
    r, g, b = im[:, :, 0].astype(int), im[:, :, 1].astype(int), im[:, :, 2].astype(int)
    return _density_box((r > 170) & (g > 170) & (b > 170))


def _calibrate(im, w_m, h_m):
    """Footprint / envelope pixel box. Try the gray-wall and white-envelope detectors;
    use whichever gives px-per-metre AGREEMENT between the two axes (the consistent one
    is the clean calibration — a contaminated edge inflates one axis). The two detectors
    are complementary across views, so one of them is almost always clean."""
    best = None
    for box in (_box_gray(im), _box_white(im)):
        if not box:
            continue
        x0, x1, yt, yb = box
        if x1 <= x0 or yb <= yt:
            continue
        sx, sy = (x1 - x0) / w_m, (yb - yt) / h_m
        err = abs(sx - sy) / max(sx, sy)
        if err < 0.05:
            return box
        if best is None or err < best[0]:
            best = (err, box)
    # fallback: anchor x on the gray box centre, width from the (reliable) y-scale
    x0, x1, yt, yb = best[1] if best else _box_gray(im)
    sy = (yb - yt) / h_m
    cx, half = (x0 + x1) / 2, sy * w_m / 2
    return round(cx - half), round(cx + half), yt, yb


def _load(case):
    gt = json.loads((GT_DIR / case / "gt.json").read_text(encoding="utf-8"))
    cd = Path(str(CASE_DATA).format(case=case))
    return gt, cd


def overlay_plan(case, gt, cd, floor):
    png = cd / f"{_FLOOR_PNG[floor]}.png"
    base = Image.open(png).convert("RGBA")
    im = np.asarray(base.convert("RGB"))               # calibrate on the full-brightness original
    dim = ImageEnhance.Brightness(base.convert("RGB")).enhance(DIM).convert("RGBA")
    W, D = gt["footprint"]["W_m"], gt["footprint"]["D_m"]
    x0, x1, yt, yb = _calibrate(im, W, D)

    def PX(gx):
        return x0 + gx / W * (x1 - x0)

    def PY(gy):
        return yb - gy / D * (yb - yt)         # gy=0 (south) at bottom

    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    fl = next(f for f in gt["floors"] if f["name"] == floor)
    for z in fl["zones"]:
        a, c, e, f = z["rect_m"]
        col = ROLE.get(z["role"], (150, 150, 150))
        d.rectangle([PX(a), PY(f), PX(e), PY(c)], fill=col + (70,), outline=col + (255,), width=3)
        d.text((PX(a) + 5, PY(f) + 4), f"{z['id']} {z['role']}", font=_font(20), fill=col + (255,))
    for w in gt["windows"]:
        if w["floor"] != floor:
            continue
        for o in w.get("openings", []):
            p, q = o["x_m"], o["x_m"] + o["width_m"]
            if w["facade"] in ("North", "South"):
                yy = PY(D) if w["facade"] == "North" else PY(0)
                d.line([(PX(p), yy), (PX(q), yy)], fill=WIN + (255,), width=9)
            else:
                xx = PX(W) if w["facade"] == "East" else PX(0)
                yA = yb - p / D * (yb - yt); yB = yb - q / D * (yb - yt)
                d.line([(xx, yA), (xx, yB)], fill=WIN + (255,), width=9)
    for dr in gt["doors"]:
        if dr["floor"] != floor:
            continue
        a, w = dr.get("x_m", 0.0), dr.get("width_m", 0.9)
        if dr["facade"] in ("North", "South"):           # facade-local x = world x
            yy = PY(D) if dr["facade"] == "North" else PY(0)
            d.line([(PX(a), yy), (PX(a + w), yy)], fill=DOOR + (255,), width=10)
        else:                                            # E/W facade-local = world y
            xx = PX(W) if dr["facade"] == "East" else PX(0)
            yA = yb - a / D * (yb - yt); yB = yb - (a + w) / D * (yb - yt)
            d.line([(xx, yA), (xx, yB)], fill=DOOR + (255,), width=10)
    d.text((10, 8), f"TYPE 2  {floor}:  gt over dimmed original  (is gt faithful to the drawing?)",
           font=_font(22), fill=(255, 255, 255, 255))
    out = Image.alpha_composite(dim, ov).convert("RGB")
    return out


def overlay_elev(case, gt, cd, facade):
    png = cd / f"{_FACADE_PNG[facade]}.png"
    base = Image.open(png).convert("RGBA")
    im = np.asarray(base.convert("RGB"))
    dim = ImageEnhance.Brightness(base.convert("RGB")).enhance(DIM).convert("RGBA")
    fw = gt["footprint"]["W_m"] if facade in ("North", "South") else gt["footprint"]["D_m"]
    ht = max(f["z_floor"] + f["ceiling_height"] for f in gt["floors"])
    x0, x1, yt, yb = _calibrate(im, fw, ht)
    mir = facade in _MIRRORED

    def PX(fx):
        t = (fw - fx) / fw if mir else fx / fw
        return x0 + t * (x1 - x0)

    def PZ(z):
        return yb - z / ht * (yb - yt)

    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for w in gt["windows"]:
        if w["facade"] != facade:
            continue
        for o in w.get("openings", []):
            if "sill_m" not in o:
                continue
            xa, xb = PX(o["x_m"]), PX(o["x_m"] + o["width_m"])
            d.rectangle([min(xa, xb), PZ(o["head_m"]), max(xa, xb), PZ(o["sill_m"])],
                        outline=WIN + (255,), width=4)
    for dr in gt["doors"]:
        if dr["facade"] != facade:
            continue
        xa, xb = PX(dr["x_m"]), PX(dr["x_m"] + dr["width_m"])
        d.rectangle([min(xa, xb), PZ(dr.get("head_m", 2.1)), max(xa, xb), PZ(dr.get("sill_m", 0))],
                    outline=DOOR + (255,), width=4)
    d.text((10, 8), f"TYPE 2  {facade} elevation:  gt over dimmed original  "
           "(do gt windows match the drawing?)", font=_font(20), fill=(255, 255, 255, 255))
    out = Image.alpha_composite(dim, ov).convert("RGB")
    return out


def _inverse_affine(transform, x: float, y: float) -> tuple[float, float]:
    determinant = transform.m00 * transform.m11 - transform.m01 * transform.m10
    if determinant == 0:
        raise ValueError("gt_overlay_singular_affine")
    dx, dy = x - transform.m02, y - transform.m12
    return ((transform.m11 * dx - transform.m01 * dy) / determinant,
            (-transform.m10 * dx + transform.m00 * dy) / determinant)


def _apply_affine(transform, x: float, y: float) -> tuple[float, float]:
    return (transform.m00 * x + transform.m01 * y + transform.m02,
            transform.m10 * x + transform.m11 * y + transform.m12)


def _pixel_for_world_plan(view: PlanViewBindingV1, binding, world: tuple[float, float]) -> tuple[float, float]:
    source = _inverse_affine(view.world_from_source_m, *world)
    return _inverse_affine(binding.pixel_to_source_m, *source)


def _pixel_for_world_elevation(view: ElevationViewBindingV1, binding, along: float, z: float) -> tuple[float, float]:
    source = {view.world_along_from_source_m.source_axis: (along - view.world_along_from_source_m.offset) / view.world_along_from_source_m.scale,
              view.world_z_from_source_m.source_axis: (z - view.world_z_from_source_m.offset) / view.world_z_from_source_m.scale}
    return _inverse_affine(binding.pixel_to_source_m, source["x"], source["y"])


def _along_extent(segment) -> tuple[float, float]:
    axis = 0 if segment.facade_family in {"North", "South"} else 1
    return tuple(sorted((segment.p1[axis], segment.p2[axis])))


def _safe_raster(raster_root: Path, label: str) -> Path:
    if Path(label).name != label or ".." in Path(label).parts:
        raise ValueError("gt_overlay_raster_label_invalid")
    root = raster_root.resolve()
    raw_candidate = root / label
    candidate = raw_candidate.resolve()
    if not candidate.is_relative_to(root) or raw_candidate.is_symlink() or not candidate.is_file():
        raise ValueError("gt_overlay_raster_escape")
    return candidate


def _within(image: Image.Image, point: tuple[float, float], epsilon: float = 1e-6) -> None:
    if not (-epsilon <= point[0] <= image.width - 1 + epsilon and -epsilon <= point[1] <= image.height - 1 + epsilon):
        raise ValueError("gt_overlay_projection_out_of_bounds")


def _candidate_stamp(image: Image.Image, document: GroundTruthV3) -> None:
    size = _weights(image)["font"]
    band = size + 10
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, image.height - band, image.width, image.height), fill=(80, 0, 0, 255))
    draw.text((8, image.height - band + 4),
              f"CANDIDATE — NOT BASELINE  {document.content_sha256[:12]}   "
              f"[gt: cyan=window orange=door red=envelope; base greyscale]",
              fill=(255,255,255,255), font=_font(size))


def _sanitized_view_id(view_id: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", view_id).strip(".-")
    if not result:
        raise ValueError("gt_overlay_view_id_unsanitisable")
    return result


def build_gt_overlay_images_v3(
    doc: GroundTruthV3,
    manifest: GtExtractionManifestV1,
    *,
    raster_root: Path,
    review_annotations: Mapping[str, str] | None = None,
) -> Mapping[str, Image.Image]:
    """Project typed v3 geometry via the manifest's declared affine bindings.

    ``review_annotations`` maps ``zone_id -> role`` and is **review-only**: it tints and
    labels plan zones for the human reviewer.  It is never written to the GT, never
    reaches a gate, and any zone it does not name stays neutral grey (no guessing).
    """
    annotations = dict(review_annotations or {})
    if doc.case != manifest.case:
        raise ValueError("gt_overlay_case_mismatch")
    if doc.generator.manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("gt_overlay_manifest_hash_mismatch")
    model = gt_to_render_model(doc)
    overlays = sorted(manifest.raster_overlays, key=lambda item: (item.view_id, item.id))
    if len({item.view_id for item in overlays}) != len(overlays):
        raise ValueError("gt_overlay_competing_bindings")
    views = {view.id: view for view in manifest.views}
    floors = {floor.floor_id: floor for floor in model.floors}
    images: dict[str, Image.Image] = {}
    for binding in overlays:
        raster = _safe_raster(Path(raster_root), binding.source_label)
        if hashlib.sha256(raster.read_bytes()).hexdigest() != binding.source_sha256:
            raise ValueError("gt_overlay_raster_hash_mismatch")
        view = views.get(binding.view_id)
        if view is None:
            raise ValueError("gt_overlay_view_missing")
        base = Image.open(raster).convert("RGBA")
        image = _review_base(base)
        weight = _weights(image)
        # Everything is drawn into a transparent layer so translucent zone fills really
        # blend with the drawing underneath (ImageDraw replaces rather than blends).
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        if isinstance(view, PlanViewBindingV1):
            floor = floors.get(view.floor_id)
            if floor is None:
                raise ValueError("gt_overlay_floor_missing")
            footprint = [_pixel_for_world_plan(view, binding, point) for point in floor.footprint_exterior]
            for point in footprint: _within(image, point)
            # Pass 1: every zone fill + outline.  Labels are deliberately NOT drawn here
            # — a later zone's translucent fill would paint over an earlier zone's text.
            pending_labels = []
            for zone in floor.zone_polygons:
                role = annotations.get(zone.id, zone.role)
                colour = _V3_ROLE.get(role, _NEUTRAL_ROLE)
                points = [_pixel_for_world_plan(view, binding, point) for point in zone.exterior]
                for point in points: _within(image, point)
                draw.polygon(points, fill=colour + (70,))
                # 深色打底 + 亮色描边：保证在任何底图灰阶上都看得见（同 opening bar 的做法）
                ring = points + [points[0]]
                draw.line(ring, fill=(0, 0, 0, 255), width=weight["line"] + 4)
                draw.line(ring, fill=_ZONE_EDGE + (255,), width=weight["line"])
                anchor = _pixel_for_world_plan(view, binding, _label_anchor(zone.exterior))
                _within(image, anchor)
                text = zone.id if zone.id not in annotations else f"{zone.id} {annotations[zone.id]}"
                pending_labels.append((anchor, text, colour + (255,)))
            segments = {segment.id: segment for segment in floor.boundary_segments}
            for opening in floor.openings:
                segment = segments[opening.segment_id]
                # a, b are the GT-authoritative along-wall interval endpoints — the
                # coordinate a human reviewer checks the opening's plan position
                # against. They are never touched below; only the perpendicular
                # (across-wall) coordinate moves.
                a, b = opening.world_along_interval
                along_is_x = segment.facade_family in {"North", "South"}
                fixed = segment.p1[1] if along_is_x else segment.p1[0]
                if segment.wall_thickness_m is not None:
                    fixed += _INWARD_SIGN[segment.facade_family] * (segment.wall_thickness_m / 2.0)
                # else: no thickness on record — keep drawing on the outer-skin line
                # rather than guess an offset.
                points = [_pixel_for_world_plan(view, binding, (a, fixed) if along_is_x else (fixed, a)),
                          _pixel_for_world_plan(view, binding, (b, fixed) if along_is_x else (fixed, b))]
                for point in points: _within(image, point)
                # Dark casing first, coloured core on top: separates the opening bar
                # from the zone outline (similar weight, adjacent hues) without needing
                # a wider bar that would swallow the drawing's own opening lines.
                draw.line(points, fill=(0, 0, 0, 255), width=weight["bar"] + 4)
                draw.line(points, fill=WIN + (255,) if opening.kind == "window" else DOOR + (255,), width=weight["bar"])
            # last, so the outer-skin reference is never overdrawn by a zone edge that
            # happens to run along it
            draw.line(footprint + [footprint[0]], fill=_ENVELOPE + (255,), width=weight["line"])
            # Pass 2: all zone labels, after every fill/outline/opening, so no zone can
            # bury another zone's name.  Centred on the guaranteed-inside anchor.
            for anchor, text, colour in pending_labels:
                _label(draw, image, anchor, text, colour, weight["font"], anchor="mm")
        elif isinstance(view, ElevationViewBindingV1):
            surface = next((item for item in model.elevation_surfaces if item.key == view.projection_surface_key), None)
            if surface is None:
                raise ValueError("gt_overlay_surface_missing")
            floor_by_id = {item.floor_id: item for item in model.floors}
            along_lo = min(_along_extent(segment)[0] for segment in surface.segments)
            along_hi = max(_along_extent(segment)[1] for segment in surface.segments)
            z_lo = min(floor_by_id[segment.floor_id].z_floor_m for segment in surface.segments)
            z_hi = max(floor_by_id[segment.floor_id].z_floor_m + floor_by_id[segment.floor_id].ceiling_height_m
                       for segment in surface.segments)
            envelope = [_pixel_for_world_elevation(view, binding, along_lo, z_lo),
                        _pixel_for_world_elevation(view, binding, along_hi, z_hi)]
            for point in envelope: _within(image, point)
            ex0, ey0 = envelope[0]; ex1, ey1 = envelope[1]
            _outline(draw, (min(ex0, ex1), min(ey0, ey1), max(ex0, ex1), max(ey0, ey1)),
                     _ENVELOPE + (255,), weight["line"])
            for segment in surface.segments:
                low, high = _along_extent(segment)
                z_floor = next(item.z_floor_m for item in model.floors if item.floor_id == segment.floor_id)
                for step in range(0, 13, 2):
                    a, b = low+(high-low)*step/12, low+(high-low)*min(1., (step+1)/12)
                    points = [_pixel_for_world_elevation(view,binding,a,z_floor), _pixel_for_world_elevation(view,binding,b,z_floor)]
                    for point in points: _within(image, point)
                    draw.line(points, fill=(255,80,80,255), width=max(1, weight["line"] // 2))
                for visible_low, visible_high in segment.visible_intervals:
                    points = [_pixel_for_world_elevation(view,binding,visible_low,z_floor), _pixel_for_world_elevation(view,binding,visible_high,z_floor)]
                    for point in points: _within(image, point)
                    draw.line(points, fill=(255,210,0,255), width=weight["line"])
            # ── 立面「前后关系」轮廓线（2026-08-21 用户提）────────────────────────
            # C2 落地后，非方形建筑的同一立面族由**不同进深**的分段拼成，图纸上因此
            # 出现一条竖的进深台阶线（sm25 西立面距左端 6 m 那条 = 西向墙面从 y=14 处
            # 退进 15 m）。这条信息 gt 里本来就有（每段各带自己的垂直坐标），只是从未
            # 画出来 ⇒ 人没法核对读图器画的那条线到底对不对。
            # ⚠️ 这里只是**把已有数据画出来**；gt 里尚无显式的「进深台阶」元素，
            #    判卷也还不对它计分（登记 R-4，归 C2/判卷专项）。
            perp = 1 if view.facade_family in {"North", "South"} else 0
            for floor_id in sorted({segment.floor_id for segment in surface.segments}):
                floor_segments = sorted((segment for segment in surface.segments
                                         if segment.floor_id == floor_id),
                                        key=lambda item: _along_extent(item)[0])
                item = floor_by_id[floor_id]
                band_lo, band_hi = item.z_floor_m, item.z_floor_m + item.ceiling_height_m
                for left, right in zip(floor_segments, floor_segments[1:]):
                    boundary = _along_extent(right)[0]
                    if abs(_along_extent(left)[1] - boundary) > _DEPTH_STEP_EPS:
                        continue                       # 两段不相接，不是台阶
                    near, far = left.p1[perp], right.p1[perp]
                    if abs(near - far) <= _DEPTH_STEP_EPS:
                        continue                       # 同一进深，没有台阶
                    points = [_pixel_for_world_elevation(view, binding, boundary, band_lo),
                              _pixel_for_world_elevation(view, binding, boundary, band_hi)]
                    for point in points: _within(image, point)
                    draw.line(points, fill=(0, 0, 0, 255), width=weight["line"] + 4)
                    draw.line(points, fill=_DEPTH_STEP + (255,), width=weight["line"])
                    _label(draw, image,
                           (points[0][0], (points[0][1] + points[1][1]) // 2),
                           f"depth {near:.2f}\u2192{far:.2f}", _DEPTH_STEP + (255,),
                           weight["font"], anchor="mm")
            for opening in surface.openings:
                if opening.z_interval is None: continue
                a,b = opening.world_along_interval; z0,z1 = opening.z_interval
                corners = [_pixel_for_world_elevation(view,binding,a,z0), _pixel_for_world_elevation(view,binding,b,z1)]
                for point in corners: _within(image, point)
                x0, y0 = corners[0]; x1, y1 = corners[1]
                colour = WIN + (255,) if opening.kind == "window" else DOOR + (255,)
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                _outline(draw, box, colour, weight["box"], dash=weight["box"] * 4)
                # spec §7.4 [S]: each elevation overlay carries the opening ID, its plan
                # along interval and its z interval, so the audit table can be checked
                # against the picture without a second lookup.
                _label(draw, image, (box[0], box[1] - weight["font"] - weight["box"]),
                       f"{opening.id}  along=[{a:.2f},{b:.2f}]  z=[{z0:.2f},{z1:.2f}]",
                       colour, weight["font"])
        else:  # pragma: no cover - strict manifest union protects this.
            raise ValueError("gt_overlay_view_kind_invalid")
        image = Image.alpha_composite(image, layer)
        if doc.verification.status == "candidate": _candidate_stamp(image, doc)
        images[binding.view_id] = image.convert("RGB")
    return dict(sorted(images.items()))


def write_gt_overlay_images_v3(images: Mapping[str, Image.Image], out_dir: Path) -> tuple[Path, ...]:
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError("gt_overlay_output_exists")
    names = [_sanitized_view_id(view_id) for view_id in images]
    if len(set(names)) != len(names):
        raise ValueError("gt_overlay_sanitized_name_collision")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.tmp-", dir=out_dir.parent))
    try:
        written: list[Path] = []
        for view_id, name in sorted(zip(images, names), key=lambda item: item[1]):
            path = temporary / f"overlay_{name}.png"; images[view_id].save(path); written.append(path)
        os.replace(temporary, out_dir)
        return tuple(out_dir / item.name for item in written)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main():
    ap = argparse.ArgumentParser(description="Overlay DXF-built gt onto the original drawing PNGs.")
    ap.add_argument("case", nargs="?", help="legacy v2 case")
    ap.add_argument("--gt-file")
    ap.add_argument("--manifest")
    ap.add_argument("--raster-root")
    ap.add_argument("--out-dir")
    ap.add_argument("--review-annotations",
                    help="review-only JSON {zone_id: role} used to tint/label plan zones; "
                         "never written to the GT and never consulted by a gate")
    args = ap.parse_args()
    v3 = any((args.gt_file, args.manifest, args.raster_root, args.out_dir, args.review_annotations))
    if v3:
        if args.case or not all((args.gt_file, args.manifest, args.raster_root, args.out_dir)):
            ap.error("v3 requires --gt-file --manifest --raster-root --out-dir and no positional case")
        document = load_gt_file(Path(args.gt_file), allow_legacy=False)
        if not isinstance(document, GroundTruthV3):
            ap.error("--gt-file must be schema v3")
        manifest = GtExtractionManifestV1.model_validate(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
        annotations = None
        if args.review_annotations:
            payload = json.loads(Path(args.review_annotations).read_text(encoding="utf-8"))
            annotations = {str(k): str(v) for k, v in payload.get("zone_roles", payload).items()}
        for path in write_gt_overlay_images_v3(build_gt_overlay_images_v3(document, manifest, raster_root=Path(args.raster_root), review_annotations=annotations), Path(args.out_dir)):
            print(f"wrote {path}")
        return
    if not args.case:
        ap.error("legacy positional case is required")
    gt, cd = _load(args.case)
    out_dir = GT_DIR / args.case / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    for floor in ("Floor 1", "Floor 2"):
        p = out_dir / f"overlay_{_FLOOR_PNG[floor]}.png"
        overlay_plan(args.case, gt, cd, floor).save(p)
        print(f"wrote {p}")
    for facade in ("South", "North", "East", "West"):
        p = out_dir / f"overlay_{_FACADE_PNG[facade]}.png"
        overlay_elev(args.case, gt, cd, facade).save(p)
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
