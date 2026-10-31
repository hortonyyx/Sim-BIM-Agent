"""Render an evaluation ground-truth (gt) JSON to dimension-annotated PNGs.

The gt files under ``case_tests/test_baseline/gt/<case>.json`` are the human-read
EVALUATION answer (true zonification / per-facade window counts / dimension
truths). They are stored as bare coordinates, which is hard to eyeball against the
original CAD drawings. This tool renders the gt back into two annotated views so a
human can verify the gt by *comparison with the original drawings* instead of
reading raw numbers (sm21 backlog #4):

  * ``<case>_gt_plan.png`` — one plan panel per floor: zone rectangles filled by
    role + labelled, footprint + per-band partition dimension chains, windows on
    facade edges (count per facade), doors marked.
  * ``<case>_gt_elev.png`` — one elevation panel per facade: wall envelope split
    into floor bands, window boxes at their [sill, head] z (count per floor),
    doors, and a z dimension chain (sill / window height / top gap / floor height).

The gt fixes layout INTENT, not millimetre coordinates — ``rect_m`` are clear-space
bboxes (±wall thickness) and gt does not fix per-window x. The renders say so: zone
boxes carry their gt extents, but window x-positions within a facade are schematic
(evenly distributed) and captioned as such. Pairs with the executor-output renderers
``render_corrected_geometry.py`` (plan) and ``render_elevation_windows.py`` (elev).

Pure PIL (sized default font, no system TTF needed — Pillow >= 10).

Usage:
    python scripts/tool_scripts/render_gt.py sm21_anchor          # by case name
    python scripts/tool_scripts/render_gt.py path/to/gt/foo.json  # by path
    python scripts/tool_scripts/render_gt.py sm21_anchor --out-dir some/where
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageColor, ImageDraw, ImageFont
from src.agent.judge.gt import DEFAULT_GT_DIR, load_gt_file, load_gt_document
from src.agent.judge.gt_render_model import (GtRenderModel, gt_to_render_model,
                                               render_elevation_model, render_plan_model)
from src.agent.judge.gt_schema import GroundTruthV3

GT_DIR = Path("case_tests/test_baseline/gt")

SCALE = 46          # px per metre (shared by plan + elevation)
HEADER = 70         # px top banner (case + legend)
PANEL_GAP = 40      # px between panels

# plan panel paddings (room for dimension chains drawn outside the footprint box)
P_LEFT = 118        # y-band chain + overall depth dim
P_RIGHT = 34
P_TOP = 104         # floor caption + overall width dim + north x-chain
P_BOTTOM = 92       # south x-chain + facade window summary

# elevation panel paddings
E_LEFT = 86         # floor-height chain
E_RIGHT = 118       # z dimension chain
E_TOP = 92          # caption + width dim
E_BOTTOM = 40

ROLE_FILL = {
    "office": "#cfe3f2",
    "meeting": "#d7ecd2",
    "corridor": "#fdf0c8",
    "lobby": "#fdf0c8",
    "wc": "#e7d8f0",
    "toilet": "#e7d8f0",
    "stair": "#e0ddd6",
    "shaft": "#e0ddd6",
    "core": "#e0ddd6",
}
DEFAULT_FILL = "#e9e9e9"
BG = (252, 252, 251)
CELL_EDGE = "#3a3a3a"
FOOTPRINT = "#111111"
WINDOW = "#1f77b4"
WINDOW_FILL = "#bcd6ec"
DOOR = "#b5651d"
DIM = "#0a7d3c"        # dimension chain colour (green, echoing the CAD source)
TEXT = "#202020"
SUBTLE = "#6f6f6f"
GRID = "#e4e4e4"

_FONTS: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _FONTS:
        _FONTS[size] = ImageFont.load_default(size=size)
    return _FONTS[size]


def _fmt(v: float) -> str:
    """Metre value -> compact label (3.6 not 3.600, 5 not 5.0)."""
    return f"{v:g}"


def _vtext(label: str, size: int, fill: str) -> Image.Image:
    """Render text rotated 90° CCW (for vertical dimension chains)."""
    f = _font(size)
    bb = f.getbbox(label)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    tile = Image.new("RGBA", (w + 4, h + 4), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((2 - bb[0], 2 - bb[1]), label, font=f, fill=fill)
    return tile.rotate(90, expand=True)


def _centre_text(d: ImageDraw.ImageDraw, cx: float, cy: float, label: str,
                 size: int, fill: str) -> None:
    f = _font(size)
    bb = f.getbbox(label)
    d.text((cx - (bb[2] - bb[0]) / 2, cy - (bb[3] - bb[1]) / 2), label, font=f, fill=fill)


def _dim_h(d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, label: str,
           size: int = 15) -> None:
    """Horizontal dimension line between x0..x1 at height y, ticks + centred label."""
    d.line([(x0, y), (x1, y)], fill=DIM, width=1)
    for x in (x0, x1):
        d.line([(x, y - 4), (x, y + 4)], fill=DIM, width=1)
    if abs(x1 - x0) > 14:
        _centre_text(d, (x0 + x1) / 2, y - 10, label, size, DIM)


def _dim_v(img: Image.Image, d: ImageDraw.ImageDraw, y0: float, y1: float, x: float,
           label: str, size: int = 15) -> None:
    """Vertical dimension line between y0..y1 at column x, ticks + rotated label."""
    d.line([(x, y0), (x, y1)], fill=DIM, width=1)
    for y in (y0, y1):
        d.line([(x - 4, y), (x + 4, y)], fill=DIM, width=1)
    if abs(y1 - y0) > 14:
        tile = _vtext(label, size, DIM)
        img.paste(tile, (int(x - 9 - tile.width // 2 + 4), int((y0 + y1) / 2 - tile.height / 2)), tile)


def _wrap_note(note: str, limit: int = 22) -> list[str]:
    out, line = [], ""
    for word in note.split():
        if len(line) + len(word) + 1 > limit:
            if line:
                out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out[:3]


# --------------------------------------------------------------------------- plan


def _plan_panel_size(w_m: float, d_m: float) -> tuple[int, int]:
    return (P_LEFT + int(w_m * SCALE) + P_RIGHT,
            P_TOP + int(d_m * SCALE) + P_BOTTOM)


def _uniq_x(zones: list[dict], y_lo: float, y_hi: float, w_m: float) -> list[float]:
    """X breakpoints of zones whose band overlaps [y_lo, y_hi]."""
    xs = {0.0, w_m}
    for z in zones:
        x0, y0, x1, y1 = z["rect_m"]
        if y1 > y_lo + 1e-6 and y0 < y_hi - 1e-6:
            xs.add(round(x0, 3))
            xs.add(round(x1, 3))
    return sorted(xs)


def _draw_plan_floor(img: Image.Image, d: ImageDraw.ImageDraw, ox: int, oy: int,
                     gt: dict, fl: dict) -> None:
    w_m = float(gt["footprint"]["W_m"])
    d_m = float(gt["footprint"]["D_m"])

    def tx(x: float) -> float:
        return ox + P_LEFT + x * SCALE

    def ty(y: float) -> float:                      # flip: +y (North) points up
        return oy + P_TOP + (d_m - y) * SCALE

    cap = (f"{fl.get('name', '?')}   z={_fmt(float(fl['z_floor']))}  "
           f"h={_fmt(float(fl['ceiling_height']))}m   {fl.get('zone_count')} zones")
    d.text((ox + P_LEFT, oy + HEADER - 50), cap, font=_font(18), fill=TEXT)
    layout = fl.get("layout")
    if layout:
        d.text((ox + P_LEFT, oy + HEADER - 30), layout, font=_font(13), fill=SUBTLE)

    # 1 m grid
    gx = 0
    while gx <= w_m + 1e-6:
        d.line([(tx(gx), ty(d_m)), (tx(gx), ty(0))], fill=GRID, width=1)
        gx += 1
    gy = 0
    while gy <= d_m + 1e-6:
        d.line([(tx(0), ty(gy)), (tx(w_m), ty(gy))], fill=GRID, width=1)
        gy += 1

    # zones
    for z in fl.get("zones", []):
        x0, y0, x1, y1 = z["rect_m"]
        fill = ROLE_FILL.get(str(z.get("role", "")).lower(), DEFAULT_FILL)
        d.rectangle([tx(x0), ty(y1), tx(x1), ty(y0)],
                    fill=ImageColor.getrgb(fill), outline=CELL_EDGE, width=2)
        d.text((tx(x0) + 5, ty(y1) + 4), str(z.get("id", "")), font=_font(15), fill=TEXT)
        d.text((tx(x0) + 5, ty(y1) + 22), str(z.get("role", "")), font=_font(12), fill=SUBTLE)
        for k, line in enumerate(_wrap_note(str(z.get("note", "")))):
            d.text((tx(x0) + 5, ty(y1) + 38 + k * 13), line, font=_font(11), fill=SUBTLE)

    # footprint outline
    d.rectangle([tx(0), ty(d_m), tx(w_m), ty(0)], outline=FOOTPRINT, width=3)

    # ---- dimension chains
    # overall width (outermost top) + north-band x-partitions (inner top)
    _dim_h(d, tx(0), tx(w_m), oy + HEADER - 4, f"{_fmt(w_m)} m")
    north = _uniq_x(fl.get("zones", []), d_m * 0.66, d_m, w_m)
    for a, b in zip(north, north[1:]):
        _dim_h(d, tx(a), tx(b), ty(d_m) - 16, _fmt(b - a))
    # south-band x-partitions (inner bottom)
    south = _uniq_x(fl.get("zones", []), 0.0, d_m * 0.34, w_m)
    for a, b in zip(south, south[1:]):
        _dim_h(d, tx(a), tx(b), ty(0) + 26, _fmt(b - a))

    # overall depth (outermost left) + y-band chain (inner left)
    _dim_v(img, d, ty(d_m), ty(0), ox + P_LEFT - 60, f"{_fmt(d_m)} m")
    ys = sorted({0.0, d_m} | {round(v, 3) for z in fl.get("zones", [])
                              for v in (z["rect_m"][1], z["rect_m"][3])})
    for a, b in zip(ys, ys[1:]):
        _dim_v(img, d, ty(b), ty(a), ox + P_LEFT - 22, _fmt(b - a))

    _draw_plan_openings(d, gt, fl, tx, ty, w_m, d_m)
    _draw_plan_facade_summary(d, gt, fl, ox, ty(0) + 52)


def _draw_plan_facade_summary(d: ImageDraw.ImageDraw, gt: dict, fl: dict,
                              ox: int, y: float) -> None:
    """One authoritative per-facade window/door line under the panel (no edge clutter)."""
    floor_name = fl.get("name")
    parts, sill, head = [], None, None
    for facade in ("North", "South", "East", "West"):
        n = sum(int(w.get("count", 0)) for w in gt.get("windows", [])
                if w.get("facade") == facade and w.get("floor") == floor_name)
        has_door = any(dr.get("facade") == facade and dr.get("floor") == floor_name
                       for dr in gt.get("doors", []))
        parts.append(f"{facade[0]}:{n}{'+door' if has_door else ''}")
        for w in gt.get("windows", []):
            if (w.get("facade") == facade and w.get("floor") == floor_name
                    and w.get("sill_m") is not None):
                sill, head = w["sill_m"], w["head_m"]
    z = f"   sill-head z {_fmt(sill)}-{_fmt(head)}" if sill is not None else ""
    d.text((ox + P_LEFT, y), "windows  " + "  ".join(parts) + z,
           font=_font(13), fill=WINDOW)


def _facade_edge_plan(facade: str, tx, ty, w_m: float, d_m: float):
    """Return (p0, p1, outward) pixel endpoints of a facade edge + label anchor side."""
    f = facade.lower()
    if f.startswith("n"):
        return (tx(0), ty(d_m)), (tx(w_m), ty(d_m)), "h"
    if f.startswith("s"):
        return (tx(0), ty(0)), (tx(w_m), ty(0)), "h"
    if f.startswith("e"):
        return (tx(w_m), ty(0)), (tx(w_m), ty(d_m)), "v"
    return (tx(0), ty(0)), (tx(0), ty(d_m)), "v"      # west


def _door_frac(door: dict) -> float:
    note = str(door.get("note", "")).lower()
    if "left" in note:
        return 0.12
    if "right" in note:
        return 0.88
    return 0.5


def _openings_for(gt: dict, facade: str, floor: str):
    for w in gt.get("windows", []):
        if w.get("facade") == facade and w.get("floor") == floor:
            return w.get("openings")
    return None


def _draw_plan_openings(d: ImageDraw.ImageDraw, gt: dict, fl: dict, tx, ty,
                        w_m: float, d_m: float) -> None:
    floor_name = fl.get("name")
    for facade in ("North", "South", "East", "West"):
        ops = _openings_for(gt, facade, floor_name)
        (x0, y0), (x1, y1), orient = _facade_edge_plan(facade, tx, ty, w_m, d_m)
        if ops:  # exact: draw each window at its true along-facade [x_m, x_m+width]
            for o in ops:
                a, b = o["x_m"], o["x_m"] + o["width_m"]
                if orient == "h":      # facade-local x = world x (from west)
                    d.line([(tx(a), y0), (tx(b), y0)], fill=WINDOW, width=7)
                else:                  # E/W facade-local = world y (from south)
                    d.line([(x0, ty(a)), (x0, ty(b))], fill=WINDOW, width=7)
            continue
        n = sum(int(w.get("count", 0)) for w in gt.get("windows", [])   # fallback: schematic
                if w.get("facade") == facade and w.get("floor") == floor_name)
        for i in range(n):
            c = (i + 1) / (n + 1)
            if orient == "h":
                cx = x0 + (x1 - x0) * c
                d.line([(cx - 9, y0), (cx + 9, y0)], fill=WINDOW, width=6)
            else:
                cy = y0 + (y1 - y0) * c
                d.line([(x0, cy - 9), (x0, cy + 9)], fill=WINDOW, width=6)

    for door in gt.get("doors", []):
        if door.get("floor") != floor_name:
            continue
        (ex0, ey0), (ex1, ey1), orient = _facade_edge_plan(door["facade"], tx, ty, w_m, d_m)
        a = door.get("x_m"); w = door.get("width_m", 0.9)
        if orient == "h":      # facade-local x = world x; draw [a, a+w] on the edge
            xa, xb = (tx(a), tx(a + w)) if a is not None else (ex0 + (ex1 - ex0) * _door_frac(door) - 12,) * 2
            d.line([(xa, ey0), (xb, ey0)], fill=DOOR, width=7)
            d.text(((xa + xb) / 2 - 14, ey0 + (6 if door["facade"].lower().startswith("s") else -18)),
                   "DOOR", font=_font(12), fill=DOOR)
        else:                  # E/W facade-local = world y
            ya, yb = (ty(a), ty(a + w)) if a is not None else (ey0 + (ey1 - ey0) * _door_frac(door) - 12,) * 2
            d.line([(ex0, ya), (ex0, yb)], fill=DOOR, width=7)
            d.text((ex0 + (6 if door["facade"].lower().startswith("e") else -42), (ya + yb) / 2 - 7),
                   "DOOR", font=_font(12), fill=DOOR)


def _render_plan_v2(gt: dict) -> Image.Image:
    w_m = float(gt["footprint"]["W_m"])
    d_m = float(gt["footprint"]["D_m"])
    floors = gt.get("floors", [])
    pw, ph = _plan_panel_size(w_m, d_m)
    total_w = max(540, pw * len(floors) + PANEL_GAP * (len(floors) - 1))
    total_h = HEADER + ph
    img = Image.new("RGB", (total_w, total_h), BG)
    d = ImageDraw.Draw(img)
    d.text((12, 10), f"TYPE 1  GT plan (gt's own rendering)  -  {gt.get('case', '?')}   "
           f"footprint {_fmt(w_m)} x {_fmt(d_m)} m", font=_font(20), fill=TEXT)
    d.text((12, 36), "zone boxes = gt clear-space extents (±wall thickness); "
           "blue = windows (exact x+width from CAD where present, else schematic); "
           "brown = door. Compare against the floor-plan drawings.", font=_font(13), fill=SUBTLE)
    for i, fl in enumerate(floors):
        _draw_plan_floor(img, d, i * (pw + PANEL_GAP), HEADER, gt, fl)
    return img


# ----------------------------------------------------------------------- elevation


def _facade_width(gt: dict, facade: str) -> float:
    f = facade.lower()
    return float(gt["footprint"]["W_m"] if f[0] in "ns" else gt["footprint"]["D_m"])


def _total_height(gt: dict) -> float:
    return max(float(fl["z_floor"]) + float(fl["ceiling_height"]) for fl in gt["floors"])


def _draw_elev_panel(img: Image.Image, d: ImageDraw.ImageDraw, ox: int, oy: int,
                     gt: dict, facade: str) -> None:
    fw = _facade_width(gt, facade)
    ht = _total_height(gt)

    def tx(x: float) -> float:
        return ox + E_LEFT + x * SCALE

    def tz(z: float) -> float:                      # +z points up
        return oy + E_TOP + (ht - z) * SCALE

    d.text((ox + E_LEFT, oy + HEADER - 50), f"{facade} elevation   {_fmt(fw)} m wide",
           font=_font(18), fill=TEXT)

    # wall envelope + floor bands
    d.rectangle([tx(0), tz(ht), tx(fw), tz(0)], fill=(245, 244, 240), outline=FOOTPRINT, width=3)
    for fl in gt["floors"]:
        zf = float(fl["z_floor"])
        if zf > 1e-6:
            d.line([(tx(0), tz(zf)), (tx(fw), tz(zf))], fill=CELL_EDGE, width=2)

    # windows per floor on this facade
    for fl in gt["floors"]:
        name = fl.get("name")
        entry = next((w for w in gt.get("windows", [])
                      if w.get("facade") == facade and w.get("floor") == name), None)
        zf, h = float(fl["z_floor"]), float(fl["ceiling_height"])
        if entry is None or not int(entry.get("count", 0)):
            _centre_text(d, tx(fw / 2), tz(zf + h / 2), "(no window)", 13, SUBTLE)
            continue
        n = int(entry["count"])
        sill, head = float(entry["sill_m"]), float(entry["head_m"])
        ops = entry.get("openings")
        if ops:  # exact: box each window at its true [x_m,x_m+width] × per-opening [sill,head]
            for o in ops:
                os, oh = o.get("sill_m", sill), o.get("head_m", head)
                d.rectangle([tx(o["x_m"]), tz(oh), tx(o["x_m"] + o["width_m"]), tz(os)],
                            fill=ImageColor.getrgb(WINDOW_FILL), outline=WINDOW, width=2)
            _centre_text(d, tx(fw / 2), tz(head) - 12, f"{n} win  (exact x)", 12, WINDOW)
        else:    # fallback: evenly distributed
            slot = fw / n
            bw = min(slot * 0.55, 2.4)
            for i in range(n):
                cx = slot * (i + 0.5)
                d.rectangle([tx(cx - bw / 2), tz(head), tx(cx + bw / 2), tz(sill)],
                            fill=ImageColor.getrgb(WINDOW_FILL), outline=WINDOW, width=2)
            _centre_text(d, tx(fw / 2), tz(head) - 12, f"{n} win  (x schematic)", 12, WINDOW)

    # doors on this facade (at their true along-facade x + width)
    for door in gt.get("doors", []):
        if door.get("facade") != facade:
            continue
        x0 = door.get("x_m", fw * _door_frac(door) - 0.45)
        w = door.get("width_m", 0.9)
        hd, sl = door.get("head_m", 2.1), door.get("sill_m", 0.0)
        d.rectangle([tx(x0), tz(hd), tx(x0 + w), tz(sl)], fill=None, outline=DOOR, width=3)
        _centre_text(d, tx(x0 + w / 2), tz(hd) - 11, "DOOR", 12, DOOR)

    # ---- dimension chains
    _dim_h(d, tx(0), tx(fw), oy + HEADER - 6, f"{_fmt(fw)} m")          # width (top)

    # z chain (right): every distinct breakpoint across floors of this facade
    brks = {0.0, ht}
    for fl in gt["floors"]:
        brks.add(float(fl["z_floor"]))
        brks.add(float(fl["z_floor"]) + float(fl["ceiling_height"]))
        entry = next((w for w in gt.get("windows", [])
                      if w.get("facade") == facade and w.get("floor") == fl.get("name")
                      and w.get("sill_m") is not None), None)
        if entry:
            brks.add(float(entry["sill_m"]))
            brks.add(float(entry["head_m"]))
    zb = sorted(brks)
    win_spans = {(float(w["sill_m"]), float(w["head_m"]))
                 for w in gt.get("windows", [])
                 if w.get("facade") == facade and w.get("sill_m") is not None}
    xcol = tx(fw) + 30
    for a, b in zip(zb, zb[1:]):
        is_win = (round(a, 3), round(b, 3)) in {(round(s, 3), round(h, 3)) for s, h in win_spans}
        d.line([(xcol, tz(a)), (xcol, tz(b))], fill=WINDOW if is_win else DIM, width=1)
        for zz in (a, b):
            d.line([(xcol - 4, tz(zz)), (xcol + 4, tz(zz))], fill=DIM, width=1)
        if (b - a) * SCALE > 13:
            tile = _vtext(_fmt(b - a), 12, WINDOW if is_win else DIM)
            img.paste(tile, (int(xcol + 6), int((tz(a) + tz(b)) / 2 - tile.height / 2)), tile)

    # floor-height chain (left)
    for fl in gt["floors"]:
        zf, h = float(fl["z_floor"]), float(fl["ceiling_height"])
        _dim_v(img, d, tz(zf + h), tz(zf), ox + E_LEFT - 30, _fmt(h))


def _render_elev_v2(gt: dict) -> Image.Image:
    facades = ["South", "North", "East", "West"]
    ht = _total_height(gt)
    ph = E_TOP + int(ht * SCALE) + E_BOTTOM
    widths = {f: E_LEFT + int(_facade_width(gt, f) * SCALE) + E_RIGHT for f in facades}
    # 2 columns x 2 rows
    col_w = max(widths.values())
    total_w = col_w * 2 + PANEL_GAP
    total_h = HEADER + ph * 2 + PANEL_GAP
    img = Image.new("RGB", (total_w, total_h), BG)
    d = ImageDraw.Draw(img)
    d.text((12, 10), f"TYPE 1  GT elevations (gt's own rendering)  -  {gt.get('case', '?')}   "
           f"total height {_fmt(ht)} m", font=_font(20), fill=TEXT)
    d.text((12, 36), "boxes = windows at gt [sill, head] z; x+width exact from CAD where "
           "present, else schematic; brown = door. Compare against the elevation drawings.",
           font=_font(13), fill=SUBTLE)
    for i, facade in enumerate(facades):
        ox = (i % 2) * (col_w + PANEL_GAP)
        oy = HEADER + (i // 2) * (ph + PANEL_GAP)
        _draw_elev_panel(img, d, ox, oy, gt, facade)
    return img


# ---------------------------------------------------------------------------- cli


def _resolve_gt(arg: str) -> tuple[str, dict]:
    p = Path(arg)
    if not p.suffix:                       # treated as a case name -> per-case bundle
        p = GT_DIR / arg / "gt.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    case = data.get("case") or p.parent.name
    return case, data


def _model_for_render(value) -> GtRenderModel | None:
    """Return a typed render model for v3 documents only.

    Raw v2 dictionaries deliberately retain the pixel-stable legacy renderer;
    the CLI loads them through the typed legacy adapter before reaching it.
    """
    if isinstance(value, GtRenderModel):
        return value
    if isinstance(value, GroundTruthV3):
        return gt_to_render_model(value)
    if isinstance(value, dict) and value.get("schema_version") == 3:
        return gt_to_render_model(GroundTruthV3.model_validate(value))
    return None


def render_plan(gt) -> Image.Image:
    model = _model_for_render(gt)
    return render_plan_model(model) if model is not None else _render_plan_v2(gt)


def render_elev(gt) -> Image.Image:
    model = _model_for_render(gt)
    return render_elevation_model(model) if model is not None else _render_elev_v2(gt)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a gt JSON to annotated plan + elevation PNGs.")
    ap.add_argument("gt", help="case name (e.g. sm21_anchor) or path to a gt JSON")
    ap.add_argument("--out-dir", help="output directory (default: <gt_dir>/<case>/renders)")
    ap.add_argument("--review-annotations",
                    help="review-only JSON {zone_id: role}; colours/names plan zones for "
                         "the human reviewer, never written to the gt and never gated")
    args = ap.parse_args()

    path = Path(args.gt)
    is_case = not path.suffix
    if not is_case and args.out_dir is None:
        ap.error("--out-dir is required for an explicit gt JSON path")
    document = load_gt_document(args.gt, gt_dir=DEFAULT_GT_DIR) if is_case else load_gt_file(path)
    if document is None:
        ap.error(f"no gt found for case {args.gt!r}")
    case = document.case
    out_dir = Path(args.out_dir) if args.out_dir else GT_DIR / case / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "gt_plan.png"
    elev_path = out_dir / "gt_elev.png"
    annotations = None
    if args.review_annotations:
        payload = json.loads(Path(args.review_annotations).read_text(encoding="utf-8"))
        annotations = {str(k): str(v) for k, v in payload.get("zone_roles", payload).items()}
    model = gt_to_render_model(document)
    render_plan_model(model, review_annotations=annotations).save(plan_path)
    render_elevation_model(model).save(elev_path)
    print(f"wrote {plan_path}")
    print(f"wrote {elev_path}")


if __name__ == "__main__":
    main()
