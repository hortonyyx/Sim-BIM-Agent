"""Render reading-stage vector JSON to SVG for manual side-by-side validation against source PNG.

Usage:
    python scripts/tool_scripts/render_vector_to_svg.py <vector_json> [--out <svg_path>]
    python scripts/tool_scripts/render_vector_to_svg.py --dir backup/tests_history/SmallOffice/smalloffice_20_redraw/0_reading

Reads the schema defined in backup/tests_history/SmallOffice/smalloffice_20_redraw/vector_schema_v0.md.
Handles both image_kind='plan' (walls/rooms in world x/y) and 'elevation'
(elevation_walls/elevation_windows/elevation_doors in x_local/z).

No external deps — pure SVG string assembly. Open SVG in browser to compare with PNG.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PALETTE = {
    "wall": "#222",
    "wall_fill": "#999",
    "window": "#1f77b4",
    "outline": "#000",
    "door": "#d62728",
    "stair": "#8c564b",
    "other": "#aaa",
    "dim": "#2ca02c",
    "ocr": "#9467bd",
    "axis": "#ccc",
}

MARGIN_M = 2.0       # extra padding around bbox in meters
SCALE_PX_PER_M = 25  # 25 px = 1 m → a 30 m × 20 m plan renders ~750 × 500 px


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _stroke_points(g: dict) -> list[tuple[float, float]]:
    kind = g.get("kind")
    if kind == "line":
        return [tuple(g["p1"]), tuple(g["p2"])]
    if kind == "rect":
        x0, x1 = g.get("x_range_m", [None, None])
        y0, y1 = g.get("y_range_m", [None, None])
        if None in (x0, x1, y0, y1):
            return []
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if kind == "polyline":
        return [tuple(p) for p in (g.get("points") or [])]
    if kind == "arc":
        cx, cy = g.get("center", [None, None])
        r = g.get("radius")
        if None in (cx, cy, r):
            return []
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return []


def _collect_points(data: dict) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for s in data.get("strokes") or []:
        pts.extend(_stroke_points(s.get("geometry") or {}))
    for d in data.get("dimensions") or []:
        if d.get("from"):
            pts.append(tuple(d["from"]))
        if d.get("to"):
            pts.append(tuple(d["to"]))
    for o in data.get("ocr_texts") or []:
        if o.get("anchor"):
            pts.append(tuple(o["anchor"]))
    return pts


def render(data: dict) -> str:
    pts = _collect_points(data)
    if not pts:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="80">' \
               '<text x="10" y="40" font-family="monospace">empty vector</text></svg>'

    minx, miny, maxx, maxy = _bbox(pts)
    minx -= MARGIN_M
    miny -= MARGIN_M
    maxx += MARGIN_M
    maxy += MARGIN_M
    w_m = maxx - minx
    h_m = maxy - miny
    w_px = int(w_m * SCALE_PX_PER_M)
    h_px = int(h_m * SCALE_PX_PER_M)

    # World → SVG: flip Y so +y goes up (SVG y grows down). Origin at bottom-left of bbox.
    def tx(x: float) -> float:
        return (x - minx) * SCALE_PX_PER_M

    def ty(y: float) -> float:
        return h_px - (y - miny) * SCALE_PX_PER_M

    out: list[str] = []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_px}" height="{h_px}" '
               f'viewBox="0 0 {w_px} {h_px}" font-family="monospace" font-size="10">')
    out.append(f'<rect width="100%" height="100%" fill="#fafafa"/>')

    image_kind = data.get("image_kind", "plan")
    label = data.get("image_label", "")
    out.append(f'<text x="6" y="14" fill="#333">image_kind={image_kind} | {label}</text>')

    # Axis grid every 1 m
    for gx in range(int(minx), int(maxx) + 1):
        x_px = tx(gx)
        out.append(f'<line x1="{x_px}" y1="0" x2="{x_px}" y2="{h_px}" stroke="{PALETTE["axis"]}" '
                   f'stroke-width="{0.4 if gx % 5 else 0.8}"/>')
    for gy in range(int(miny), int(maxy) + 1):
        y_px = ty(gy)
        out.append(f'<line x1="0" y1="{y_px}" x2="{w_px}" y2="{y_px}" stroke="{PALETTE["axis"]}" '
                   f'stroke-width="{0.4 if gy % 5 else 0.8}"/>')

    # Render strokes — color by pen, geometry by kind
    # Order: fills first (rects), then strokes (lines/polylines/arcs) on top
    strokes = data.get("strokes") or []
    rect_strokes = [s for s in strokes if (s.get("geometry") or {}).get("kind") == "rect"]
    other_strokes = [s for s in strokes if (s.get("geometry") or {}).get("kind") != "rect"]

    for s in rect_strokes:
        g = s["geometry"]
        color = PALETTE.get(s.get("pen", "other"), PALETTE["other"])
        x0, x1 = g.get("x_range_m", [None, None])
        y0, y1 = g.get("y_range_m", [None, None])
        if None in (x0, x1, y0, y1):
            continue
        out.append(f'<rect x="{tx(x0)}" y="{ty(y1)}" width="{(x1-x0)*SCALE_PX_PER_M}" '
                   f'height="{(y1-y0)*SCALE_PX_PER_M}" fill="{color}" fill-opacity="0.45" '
                   f'stroke="{color}" stroke-width="1"/>')
        out.append(f'<text x="{tx(x0)+2}" y="{ty(y1)+10}" fill="{color}" font-size="9">'
                   f'{s.get("id","")} ({s.get("pen","")})</text>')

    for s in other_strokes:
        g = s.get("geometry") or {}
        kind = g.get("kind")
        color = PALETTE.get(s.get("pen", "other"), PALETTE["other"])
        t_m = g.get("thickness_m")
        thickness_px = max(1.5, (t_m or 0.10) * SCALE_PX_PER_M * 0.6) if t_m else 1.5
        if kind == "line":
            p1, p2 = g.get("p1"), g.get("p2")
            if not (p1 and p2):
                continue
            out.append(f'<line x1="{tx(p1[0])}" y1="{ty(p1[1])}" x2="{tx(p2[0])}" y2="{ty(p2[1])}" '
                       f'stroke="{color}" stroke-width="{thickness_px}" stroke-linecap="round"/>')
            midx, midy = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            out.append(f'<text x="{tx(midx)}" y="{ty(midy)-3}" fill="#666" font-size="8">'
                       f'{s.get("id","")}</text>')
        elif kind == "polyline":
            pts_list = g.get("points") or []
            if len(pts_list) < 2:
                continue
            pts_str = " ".join(f"{tx(p[0])},{ty(p[1])}" for p in pts_list)
            fill = "none" if not g.get("closed") else f'{color}'
            out.append(f'<polyline points="{pts_str}" fill="{fill}" fill-opacity="0.2" '
                       f'stroke="{color}" stroke-width="{thickness_px}" stroke-linejoin="round"/>')
            mp = pts_list[len(pts_list)//2]
            out.append(f'<text x="{tx(mp[0])}" y="{ty(mp[1])-3}" fill="#666" font-size="8">'
                       f'{s.get("id","")}</text>')
        elif kind == "arc":
            cx, cy = g.get("center", [None, None])
            r = g.get("radius")
            if None in (cx, cy, r):
                continue
            # Approximate with SVG ellipse outline (full circle); arc trig left as TODO
            out.append(f'<circle cx="{tx(cx)}" cy="{ty(cy)}" r="{r*SCALE_PX_PER_M}" '
                       f'fill="none" stroke="{color}" stroke-width="{thickness_px}" '
                       f'stroke-dasharray="4,2"/>')
            out.append(f'<text x="{tx(cx)}" y="{ty(cy)-r*SCALE_PX_PER_M-3}" fill="#666" '
                       f'font-size="8" text-anchor="middle">{s.get("id","")}</text>')

    # Dimensions
    for d in data.get("dimensions") or []:
        f_, t_ = d.get("from"), d.get("to")
        if not (f_ and t_):
            continue
        out.append(f'<line x1="{tx(f_[0])}" y1="{ty(f_[1])}" x2="{tx(t_[0])}" y2="{ty(t_[1])}" '
                   f'stroke="{PALETTE["dim"]}" stroke-width="0.6" stroke-dasharray="3,2"/>')
        midx, midy = (f_[0] + t_[0]) / 2, (f_[1] + t_[1]) / 2
        out.append(f'<text x="{tx(midx)}" y="{ty(midy)}" fill="{PALETTE["dim"]}">{d.get("text","")}</text>')

    # OCR labels
    for o in data.get("ocr_texts") or []:
        anchor = o.get("anchor")
        if not anchor:
            continue
        out.append(f'<text x="{tx(anchor[0])}" y="{ty(anchor[1])}" fill="{PALETTE["ocr"]}" '
                   f'font-style="italic">{o.get("text","")}</text>')

    # Legend
    out.append(f'<g transform="translate(6,{h_px-110})">')
    legend = [
        ("wall (plan)", PALETTE["wall"]),
        ("wall_fill (elev)", PALETTE["wall_fill"]),
        ("window", PALETTE["window"]),
        ("outline", PALETTE["outline"]),
        ("stair", PALETTE["stair"]),
        ("other", PALETTE["other"]),
        ("dimension", PALETTE["dim"]),
        ("ocr", PALETTE["ocr"]),
    ]
    for i, (lbl, c) in enumerate(legend):
        out.append(f'<rect x="0" y="{i*12}" width="10" height="10" fill="{c}"/>')
        out.append(f'<text x="14" y="{i*12+9}" fill="#333">{lbl}</text>')
    out.append('</g>')

    out.append('</svg>')
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", nargs="?", help="single vector JSON to render")
    ap.add_argument("--dir", help="render every *.json under this directory")
    ap.add_argument("--out", help="output SVG path (single-file mode only)")
    args = ap.parse_args()

    if args.dir:
        d = Path(args.dir)
        targets = sorted(d.glob("*.json"))
        if not targets:
            print(f"no *.json in {d}")
            return
        for j in targets:
            data = json.loads(j.read_text(encoding="utf-8"))
            svg = render(data)
            out = j.with_suffix(".svg")
            out.write_text(svg, encoding="utf-8")
            print(f"  {j.name} → {out.name}")
        return

    if not args.json_path:
        ap.error("provide json_path or --dir")
    j = Path(args.json_path)
    data = json.loads(j.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else j.with_suffix(".svg")
    out.write_text(render(data), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
