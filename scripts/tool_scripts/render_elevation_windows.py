"""Render the corrected windows back onto each facade as an elevation view (PNG).

The correction stage places every window on a facade in world coordinates (along-
facade span + absolute z [sill, head]). This draws, per facade (South / North /
East / West), the wall envelope split into floor bands plus each window in its
world position, so the result can be eyeballed against the original elevation
drawing: did windows land on the right facade / floor / position? — the elevation
analogue of render_corrected_geometry.py's filled plan (`*_zones.png`).

Output: one panel per facade; legacy `render()` still stacks panels into one PNG.
Pure PIL, no other deps (mirrors render_corrected_geometry.py).

Usage:
    python scripts/tool_scripts/render_elevation_windows.py <correction_geometry_snapped.json> [--out x_elev.png]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw
from src.agent.correction.footprint import floor_footprint_from_payload

SCALE = 45          # px per metre; match render_vector_to_png.py
MARGIN = 68
GAP = 30
HEADER = 44
TITLE = 18

WALL_FILL = "#fafafa"
WALL_EDGE = "#222222"
WINDOW = "#1f77b4"
WINDOW_EDGE = "#0d3b66"
TEXT = "#222222"
SUBTLE = "#777777"
GRID = "#dddddd"

_FACADES = ("South", "North", "East", "West")


def _along_extent(data: dict, facade: str) -> tuple[float, float]:
    """The world extent of the building along the given facade."""
    floor = (data.get("floors") or [None])[0]
    ring = floor_footprint_from_payload(data, floor)
    values = [p[0] for p in ring] if facade in ("South", "North") else [p[1] for p in ring]
    return min(values), max(values)


def _z_extent(data: dict) -> tuple[float, float]:
    floors = data.get("floors") or []
    if not floors:
        return 0.0, 3.0
    zb = min(float(f["z_floor"]) for f in floors)
    zt = max(float(f["z_floor"]) + float(f["ceiling_height"]) for f in floors)
    return zb, zt


def _facade_windows(data: dict, facade: str) -> list[dict]:
    return [w for w in (data.get("windows") or []) if w.get("facade") == facade]


def render_facade(data: dict, facade: str) -> Image.Image:
    a0, a1 = _along_extent(data, facade)
    z0, z1 = _z_extent(data)
    aspan = max(a1 - a0, 0.1)
    zspan = max(z1 - z0, 0.1)
    W = int(aspan * SCALE) + 2 * MARGIN
    H = int(zspan * SCALE) + 2 * MARGIN
    img = Image.new("RGB", (max(W, 360), max(H, 180)), (250, 250, 250))
    d = ImageDraw.Draw(img)

    def ax(world_along: float) -> float:
        return MARGIN + (world_along - a0) * SCALE

    def az(world_z: float) -> float:
        return MARGIN + (z1 - world_z) * SCALE

    gx = int(a0)
    while gx <= a1:
        d.line([(ax(gx), az(z0)), (ax(gx), az(z1))], fill=GRID, width=1)
        gx += 1
    gz = int(z0)
    while gz <= z1:
        d.line([(ax(a0), az(gz)), (ax(a1), az(gz))], fill=GRID, width=1)
        gz += 1

    d.rectangle([ax(a0), az(z1), ax(a1), az(z0)], fill=WALL_FILL, outline=WALL_EDGE, width=4)
    for fl in data.get("floors") or []:
        zf = float(fl["z_floor"])
        zt = zf + float(fl["ceiling_height"])
        d.line([(ax(a0), az(zf)), (ax(a1), az(zf))], fill=WALL_EDGE, width=3)
        d.line([(ax(a0), az(zt)), (ax(a1), az(zt))], fill=WALL_EDGE, width=2)
    for w in _facade_windows(data, facade):
        s = sorted(float(v) for v in w["span"])
        zz = sorted(float(v) for v in w["z"])
        d.rectangle([ax(s[0]), az(zz[1]), ax(s[1]), az(zz[0])],
                    fill=WINDOW, outline=WINDOW_EDGE, width=2)
        d.text((ax(s[0]) + 2, az(zz[1]) + 2), str(w.get("id", "")), fill="white")
    d.text((6, 6), f"correction elevation | {facade}", fill=(60, 60, 60))
    return img


def render_all_to_dir(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for facade in _FACADES:
        out = out_dir / f"elev_{facade}_render.png"
        render_facade(data, facade).save(out)
        paths.append(out)
    return paths


def render(data: dict) -> Image.Image:
    present = [f for f in _FACADES if _facade_windows(data, f)]
    if not present:
        present = list(_FACADES)  # still draw empty envelopes for context

    z0, z1 = _z_extent(data)
    zspan = max(z1 - z0, 0.1)
    panel_h = int(zspan * SCALE) + TITLE + 6

    widths = []
    for f in present:
        a0, a1 = _along_extent(data, f)
        widths.append(int((a1 - a0) * SCALE) + 2 * MARGIN)
    W = max(widths) if widths else 400
    H = HEADER + sum(panel_h + GAP for _ in present)

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((MARGIN, 14), "Corrected elevation — windows per facade", fill=TEXT)

    y = HEADER
    for f in present:
        a0, a1 = _along_extent(data, f)
        aspan = max(a1 - a0, 0.1)
        d.text((MARGIN, y), f"{f} facade  (along {a0:.1f}..{a1:.1f} m, z {z0:.1f}..{z1:.1f} m)",
                fill=SUBTLE)
        oy = y + TITLE

        def ax(world_along: float) -> float:
            return MARGIN + (world_along - a0) * SCALE

        def az(world_z: float) -> float:
            return oy + (z1 - world_z) * SCALE  # z up → smaller pixel y

        # floor-band wall envelope
        for fl in data.get("floors") or []:
            zf = float(fl["z_floor"])
            zt = zf + float(fl["ceiling_height"])
            d.rectangle([ax(a0), az(zt), ax(a1), az(zf)], fill=WALL_FILL, outline=WALL_EDGE)
        # windows
        for w in _facade_windows(data, f):
            s = sorted(float(v) for v in w["span"])
            zz = sorted(float(v) for v in w["z"])
            d.rectangle([ax(s[0]), az(zz[1]), ax(s[1]), az(zz[0])],
                        fill=WINDOW, outline=WINDOW_EDGE)
            d.text((ax(s[0]) + 1, az(zz[1]) + 1), str(w.get("id", "")), fill="white")
        y += panel_h + GAP
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="correction_geometry_snapped.json")
    ap.add_argument("--out", help="output PNG (default: <json>_elev.png)")
    ap.add_argument("--out-dir", help="write per-facade elevation renders to this directory")
    args = ap.parse_args()
    j = Path(args.json)
    data = json.loads(j.read_text(encoding="utf-8"))
    if args.out_dir:
        for p in render_all_to_dir(data, Path(args.out_dir)):
            print(f"wrote {p}")
        return
    out = Path(args.out) if args.out else j.with_name(j.stem + "_elev.png")
    render(data).save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
