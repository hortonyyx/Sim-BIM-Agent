"""Render correction-stage CorrectedGeometry JSON to PNG for visual validation.

the correction stage does NOT emit a drawing — it emits structured geometry primitives
(rectangular room cells + windows + per-floor z + audit). This renders that
abstraction to a plan view (one panel per floor) so the correction result can be
eyeballed: are cells tiling the footprint? did cross-floor axes unify? do windows
sit on the right facade? — the correction-stage analogue of render_vector_to_png.py for
the reading stage.

Usage:
    python scripts/tool_scripts/render_corrected_geometry.py <corrected_geometry.json> [--out x.png]
    python scripts/tool_scripts/render_corrected_geometry.py <corrected_geometry.json> --out-dir 1_correction

Pure PIL, no other deps (mirrors render_vector_to_png.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageColor, ImageDraw
from src.agent.correction.footprint import floor_footprint_from_payload

SCALE = 45          # px per metre; match render_vector_to_png.py
MARGIN_M = 1.5
MARGIN = int(MARGIN_M * SCALE)
GAP = 28            # px between floor panels
HEADER = 46         # px top title bar
FLOOR_TITLE = 18    # px per-floor caption height

ROLE_FILL = {
    "office": "#cfe3f2",
    "corridor": "#fdf0c8",
    "lobby": "#fdf0c8",
    "wc": "#e7d8f0",
    "toilet": "#e7d8f0",
    "stair": "#e0ddd6",
    "shaft": "#e0ddd6",
    "core": "#e0ddd6",
}
DEFAULT_FILL = "#e9e9e9"
CELL_EDGE = "#222222"
FOOTPRINT = "#111111"
WINDOW = "#1f77b4"
GRID = "#e1e1e1"
DIM = "#2ca02c"
TEXT = "#222222"
SUBTLE = "#777777"


def _ring(c: dict) -> list[tuple[float, float]]:
    poly = c.get("polygon")
    if isinstance(poly, list) and len(poly) >= 3:
        return [(float(p[0]), float(p[1])) for p in poly]
    x = c.get("x") or [0.0, 0.0]
    y = c.get("y") or [0.0, 0.0]
    return [
        (float(x[0]), float(y[0])),
        (float(x[1]), float(y[0])),
        (float(x[1]), float(y[1])),
        (float(x[0]), float(y[1])),
    ]


def _floor_bounds(data: dict, floor: dict) -> tuple[float, float, float, float]:
    pts: list[tuple[float, float]] = []
    for c in floor.get("cells") or []:
        pts.extend(_ring(c))
    try:
        pts.extend((float(x), float(y)) for x, y in floor_footprint_from_payload(data, floor))
    except ValueError:
        raise
    if not pts:
        return 0.0, 0.0, 1.0, 1.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in str(value).strip())
    return "_".join(part for part in out.split("_") if part) or "floor"


def _floor_canvas(data: dict, floor: dict):
    minx, miny, maxx, maxy = _floor_bounds(data, floor)
    minx -= MARGIN_M
    miny -= MARGIN_M
    maxx += MARGIN_M
    maxy += MARGIN_M
    W = max(1, int((maxx - minx) * SCALE))
    H = max(1, int((maxy - miny) * SCALE))

    def tx(x: float) -> float:
        return (x - minx) * SCALE

    def ty(y: float) -> float:
        return H - (y - miny) * SCALE

    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)
    gx = int(minx)
    while gx <= maxx:
        d.line([(tx(gx), 0), (tx(gx), H)], fill=GRID, width=1)
        gx += 1
    gy = int(miny)
    while gy <= maxy:
        d.line([(0, ty(gy)), (W, ty(gy))], fill=GRID, width=1)
        gy += 1
    return img, d, tx, ty


def _draw_windows_on_plan(d, data: dict, floor: dict, tx, ty) -> None:
    ring = floor_footprint_from_payload(data, floor)
    minx, maxx = min(p[0] for p in ring), max(p[0] for p in ring)
    miny, maxy = min(p[1] for p in ring), max(p[1] for p in ring)
    floor_name = floor.get("name", "")
    for w in _floor_windows(data, floor_name, floor.get("id")):
        facade = str(w.get("facade", "")).lower()
        span = sorted(float(v) for v in (w.get("span") or [0, 0]))
        if facade.startswith("n"):
            p = [(tx(span[0]), ty(maxy)), (tx(span[1]), ty(maxy))]
        elif facade.startswith("s"):
            p = [(tx(span[0]), ty(miny)), (tx(span[1]), ty(miny))]
        elif facade.startswith("e"):
            p = [(tx(maxx), ty(span[0])), (tx(maxx), ty(span[1]))]
        else:
            p = [(tx(minx), ty(span[0])), (tx(minx), ty(span[1]))]
        d.line(p, fill=ImageColor.getrgb(WINDOW), width=4)


def render_plan_floor(data: dict, floor: dict) -> Image.Image:
    """Reading-aligned plan render: wall centerlines + blue windows, no role fills."""
    img, d, tx, ty = _floor_canvas(data, floor)
    for c in floor.get("cells") or []:
        pts = [(tx(x), ty(y)) for x, y in _ring(c)]
        if len(pts) >= 2:
            d.line(pts + [pts[0]], fill=ImageColor.getrgb(CELL_EDGE), width=6)
    footprint = [(tx(x), ty(y)) for x, y in floor_footprint_from_payload(data, floor)]
    d.line(footprint + [footprint[0]], fill=ImageColor.getrgb(FOOTPRINT), width=3)
    _draw_windows_on_plan(d, data, floor, tx, ty)
    d.text((6, 6), f"correction plan | {floor.get('name', '')}", fill=(60, 60, 60))
    return img


def render_roles_floor(data: dict, floor: dict) -> Image.Image:
    """Role-coloured plan render: correction-only room typing view."""
    img, d, tx, ty = _floor_canvas(data, floor)
    for c in floor.get("cells") or []:
        pts = [(tx(x), ty(y)) for x, y in _ring(c)]
        fill = ROLE_FILL.get(str(c.get("role", "")).lower(), DEFAULT_FILL)
        if len(pts) >= 3:
            d.polygon(pts, fill=ImageColor.getrgb(fill), outline=ImageColor.getrgb(CELL_EDGE))
            d.line(pts + [pts[0]], fill=ImageColor.getrgb(CELL_EDGE), width=2)
            x0 = min(p[0] for p in pts)
            y0 = min(p[1] for p in pts)
            d.text((x0 + 4, y0 + 4), str(c.get("id", "")), fill=TEXT)
            d.text((x0 + 4, y0 + 16), str(c.get("role", "")), fill=SUBTLE)
    _draw_windows_on_plan(d, data, floor, tx, ty)
    footprint = [(tx(x), ty(y)) for x, y in floor_footprint_from_payload(data, floor)]
    d.line(footprint + [footprint[0]], fill=ImageColor.getrgb(FOOTPRINT), width=3)
    d.text((6, 6), f"correction roles | {floor.get('name', '')}", fill=(60, 60, 60))
    return img


def render_all_to_dir(data: dict, out_dir: Path) -> list[Path]:
    """Per-floor `zones_<floor>.png`: cells + role colours + labels in one image.

    User-stamped artifact set (2026-07-08): correction ships grade (vs gt, for
    side-by-side with reading's grade) + zones only — per-view plan/elevation
    renders were dropped the same day they were added (grade covers the
    coordinate-accuracy eyeball; zones covers the zoning/role judgment)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for idx, floor in enumerate(data.get("floors") or [], start=1):
        label = _safe_name(floor.get("name") or f"floor_{idx}")
        zones = out_dir / f"zones_{label}.png"
        render_roles_floor(data, floor).save(zones)
        paths.append(zones)
    return paths


def _floor_windows(data: dict, floor_name: str, floor_id: str | None = None) -> list[dict]:
    return [w for w in (data.get("windows") or [])
            if (w.get("floor_id") == floor_id if floor_id is not None else w.get("floor") == floor_name)]


def render(data: dict) -> Image.Image:
    fx = data.get("footprint_x") or [0, 1]
    fy = data.get("footprint_y") or [0, 1]
    minx, maxx = float(fx[0]), float(fx[1])
    miny, maxy = float(fy[0]), float(fy[1])
    w_m, h_m = maxx - minx, maxy - miny
    floors = data.get("floors") or []
    n = max(1, len(floors))

    panel_w = int(w_m * SCALE) + 2 * MARGIN
    panel_h = int(h_m * SCALE) + 2 * MARGIN
    total_w = panel_w * n + GAP * (n - 1)
    total_h = HEADER + FLOOR_TITLE + panel_h + MARGIN

    img = Image.new("RGB", (max(total_w, 520), total_h), (252, 252, 252))
    d = ImageDraw.Draw(img)

    title = (
        f"correction stage CorrectedGeometry  |  footprint {w_m:g}×{h_m:g} m  |  "
        f"corrections={len(data.get('corrections') or [])} "
        f"conflicts={len(data.get('conflicts') or [])} "
        f"unsupported={len(data.get('unsupported') or [])}"
    )
    d.text((10, 8), title, fill=TEXT)
    d.text((10, 24), "plan view per floor — cells filled by role, windows = blue on facade",
           fill=SUBTLE)

    for i, fl in enumerate(floors):
        ox = i * (panel_w + GAP)
        oy = HEADER + FLOOR_TITLE

        def tx(x: float) -> float:
            return ox + MARGIN + (x - minx) * SCALE

        def ty(y: float) -> float:
            # flip Y so +y (world) points up
            return oy + MARGIN + (maxy - y) * SCALE

        cap = f"{fl.get('name','?')}   z_floor={fl.get('z_floor')} h={fl.get('ceiling_height')}"
        d.text((ox + MARGIN, HEADER), cap, fill=TEXT)

        # 1 m grid
        gx = int(minx)
        while gx <= maxx + 1e-6:
            d.line([(tx(gx), ty(maxy)), (tx(gx), ty(miny))], fill=GRID, width=1)
            gx += 1
        gy = int(miny)
        while gy <= maxy + 1e-6:
            d.line([(tx(minx), ty(gy)), (tx(maxx), ty(gy))], fill=GRID, width=1)
            gy += 1

        # cells (legacy combined role-coloured overview)
        for c in fl.get("cells") or []:
            ring = _ring(c)
            pts = [(tx(x), ty(y)) for x, y in ring]
            fill = ROLE_FILL.get(str(c.get("role", "")).lower(), DEFAULT_FILL)
            d.polygon(pts, fill=ImageColor.getrgb(fill), outline=CELL_EDGE)
            d.line(pts + [pts[0]], fill=CELL_EDGE, width=2)
            label = c.get("id", "")
            x0 = min(p[0] for p in pts)
            y0 = min(p[1] for p in pts)
            d.text((x0 + 4, y0 + 4), str(label), fill=TEXT)
            d.text((x0 + 4, y0 + 16), str(c.get("role", "")), fill=SUBTLE)

        # footprint outline on top
        d.rectangle([tx(minx), ty(maxy), tx(maxx), ty(miny)], outline=FOOTPRINT, width=3)

        # windows on facade edges
        for w in _floor_windows(data, fl.get("name", "")):
            facade = str(w.get("facade", "")).lower()
            span = w.get("span") or [0, 0]
            z = w.get("z") or [0, 0]
            if facade.startswith("n"):
                p = [(tx(span[0]), ty(maxy)), (tx(span[1]), ty(maxy))]
            elif facade.startswith("s"):
                p = [(tx(span[0]), ty(miny)), (tx(span[1]), ty(miny))]
            elif facade.startswith("e"):
                p = [(tx(maxx), ty(span[0])), (tx(maxx), ty(span[1]))]
            else:  # west
                p = [(tx(minx), ty(span[0])), (tx(minx), ty(span[1]))]
            d.line(p, fill=ImageColor.getrgb(WINDOW), width=5)
            mx, my = (p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2
            d.text((mx - 8, my - 10), f"{w.get('id','')} z{z[0]:g}-{z[1]:g}", fill=WINDOW)

    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", help="CorrectedGeometry JSON (correction stage_geometry[_snapped].json)")
    ap.add_argument("--out", help="output PNG path (default: <json>.png)")
    ap.add_argument("--out-dir", help="write per-floor plan/role renders to this directory")
    args = ap.parse_args()
    j = Path(args.json_path)
    data = json.loads(j.read_text(encoding="utf-8"))
    if args.out_dir:
        for p in render_all_to_dir(data, Path(args.out_dir)):
            print(f"wrote {p}")
        return
    out = Path(args.out) if args.out else j.with_suffix(".png")
    render(data).save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
