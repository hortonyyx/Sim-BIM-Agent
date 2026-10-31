"""⭐ The elevation grade PICTURE for ``elevation_grade_v1`` -- view only.

Same contract as ``render_reading_grade.py`` (the plan picture): ⛔ this is a
VIEW, never a source of score.  Every box and line it draws comes out of
``grade["detail"]`` -- the very rows the printed percentages were counted
from.  Nothing here recomputes geometry, re-matches a target or applies a
tolerance.

One axis subtlety the plan picture does not have: the grader may have judged
the product under the MIRROR hypothesis (the product's along axis points the
other way, reported in ``grade["along_axis"]["assumed"]``).  The picture draws
on the PRODUCT'S OWN image, so ANSWER targets are reflected into the product's
frame when (and only when) the grader assumed the mirror -- the same fixed
reflection the grader used, taken from the report, ⛔ never recomputed here.

Colour language matches the plan picture: GREEN found & placed, AMBER placed
but wrong size, RED not found, MAGENTA an opening no answer explains.  Floor
lines and end lines draw green/red by their verdict.  ⛔ ASCII labels only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DIM = 0.30
C_OK = (54, 200, 108)          # answer opening, placed
C_SIZE = (255, 176, 32)        # placed, wrong size
C_MISS = (255, 62, 62)         # answer opening, not found
C_EXTRA = (240, 60, 235)       # product opening no answer explains
C_STRUCT_OK = (54, 200, 108)
C_STRUCT_MISS = (255, 62, 62)
BG = (14, 14, 18)
FG = (238, 238, 240)
MUTED = (150, 150, 158)
W_OK, W_BAD = 2, 4


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _mapper(doc: dict):
    """world metres -> pixels, inverting the product's OWN calibration."""
    cal = doc["calibration"]
    x0, z0 = cal["world_zero_px"]
    mx = cal["x"]["m_per_px"]
    mz = cal["z"]["m_per_px"]
    return (lambda m: x0 + m / mx), (lambda m: z0 - m / mz)


def _flip(value: float, extent) -> float:
    return float(extent[0]) + float(extent[1]) - value


def render(doc_path: str, grade_path: str, out_path: str) -> dict:
    doc = json.loads(Path(doc_path).read_text())
    g = json.loads(Path(grade_path).read_text())
    detail = g.get("detail")
    if detail is None:
        raise SystemExit("⛔ grade report predates the detail block; re-run"
                         " elevation_grade so the picture draws the SAME rows"
                         " the scores were counted from")

    base = Image.open(doc["image"]).convert("RGB")
    im = Image.eval(base, lambda v: int(v * DIM)).convert("RGB")
    d = ImageDraw.Draw(im)
    to_x, to_z = _mapper(doc)

    # the product's own frame: under the mirror hypothesis, answer coordinates
    # arrive in the ANSWER's frame and must be reflected to land on this image
    mirrored = g.get("along_axis", {}).get("assumed") == "mirror"
    extent = None
    if mirrored:
        # the reflection axis the grader used, read from the report's own
        # denominator rows (target along extents), ⛔ never recomputed here
        lo = min(r["along_m"][0] for r in detail["openings"])
        hi = max(r["along_m"][1] for r in detail["openings"])
        extent = (lo, hi)

    def to_px_along(m: float) -> float:
        return to_x(_flip(m, extent) if mirrored else m)

    tally = {"OK": 0, "WRONG_SIZE": 0, "OUT_OF_BAND": 0, "NOT_FOUND": 0}
    for row in detail["openings"]:
        tally[row["verdict"]] = tally.get(row["verdict"], 0) + 1
        colour = {"OK": C_OK, "WRONG_SIZE": C_SIZE,
                  "OUT_OF_BAND": C_SIZE}.get(row["verdict"], C_MISS)
        width = W_OK if row["verdict"] == "OK" else W_BAD
        x0p, x1p = sorted((to_px_along(row["along_m"][0]),
                           to_px_along(row["along_m"][1])))
        z0p, z1p = sorted((to_z(row["z_m"][0]), to_z(row["z_m"][1])))
        d.rectangle([x0p, z0p, x1p, z1p], outline=colour, width=width)
        d.text((x0p + 3, z1p + 3), row.get("kind") or row.get("id", "?"),
               fill=colour, font=_font(12))

    for extra in detail.get("extras") or []:
        x0p, x1p = sorted((to_x(extra["along_m"][0]), to_x(extra["along_m"][1])))
        z0p, z1p = sorted((to_z(extra["z_m"][0]), to_z(extra["z_m"][1])))
        d.rectangle([x0p, z0p, x1p, z1p], outline=C_EXTRA, width=W_BAD)

    for line in detail.get("structure") or []:
        colour = C_STRUCT_OK if line["verdict"] == "OK" else C_STRUCT_MISS
        if line["what"] == "floor_line":                 # z const, runs along
            z = to_z(line["const_m"])
            d.line([(to_px_along(line["span_m"][0]), z),
                    (to_px_along(line["span_m"][1]), z)],
                   fill=colour, width=W_OK + 1)
        else:                                            # along const, runs in z
            x = to_px_along(line["const_m"])
            d.line([(x, to_z(line["span_m"][0])), (x, to_z(line["span_m"][1]))],
                   fill=colour, width=W_OK + 1)

    s = g["scores"]
    den = g["denominator"]
    axis = g.get("along_axis", {})
    f_big, f_sm = _font(28), _font(15)
    rows = [
        (f"OPENINGS placed+sized {s['E1_openings_placed_and_sized_pct']}%"
         f"   found {s['E1_openings_found_pct']}%", C_OK, f_big),
        (f"NOT FOUND {tally.get('NOT_FOUND', 0)} of {den['openings']}"
         f"   wrong size {tally.get('WRONG_SIZE', 0) + tally.get('OUT_OF_BAND', 0)}"
         f"   extra {s['E2_extra_openings']}", C_MISS, f_big),
        (f"STRUCTURE {s['E3_structure_lines_pct']}%   floors"
         f" {'+'.join(den['floors'])}   KIND {'declared' if s['E5_opening_kind_pct'] is not None else 'not declared by product (J-5)'}",
         FG, f_sm),
        (f"axis assumed: {axis.get('assumed')} (identity {axis.get('identity_placed')}"
         f" / mirror {axis.get('mirror_placed')} placed)"
         f"   band {g['params']['quantization_band_m']} m"
         f"   product grid {g['params']['product_resolution_m']} m"
         f" ({'declared' if g['params']['product_resolution_declared'] else 'default'})",
         MUTED, f_sm),
    ]
    bar = 16 + 36 * 2 + 20 * len(rows) + 12
    canvas = Image.new("RGB", (im.width, im.height + bar), BG)
    canvas.paste(im, (0, 0))
    dd = ImageDraw.Draw(canvas)
    y = im.height + 16
    for text, colour, font in rows[:2]:
        dd.text((16, y), text, fill=colour, font=font)
        y += 36
    for text, colour, font in rows[2:]:
        dd.text((16, y), text, fill=colour, font=font)
        y += 20

    canvas.save(out_path)
    return {"out": out_path, "view": den["view"], "scores": s,
            "openings": tally, "axis": axis.get("assumed")}


if __name__ == "__main__":
    print(json.dumps(render(*sys.argv[1:4]), ensure_ascii=False))
