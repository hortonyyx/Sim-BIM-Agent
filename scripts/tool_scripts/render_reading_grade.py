"""⭐ The grade PICTURE for ``reading_grade_v1`` -- the thing the user reads.

Why this file exists (2026-08-24, user): "对我来说我主要是看 grade 的图，所以这个
分数的值不是太重要，主要是有一个大概的评估是画崩了还是画的一般还是画的好".
The old ``render_grade.py`` predates the new grader: it reads the v1
``wall_bands`` schema and a ``reconstruct`` report, neither of which the
denominator/grade pair produces.  So the number the user cares least about had a
picture and the number they care most about had none.

⛔ **This is a VIEW, never a source of score.** Every span it draws comes out of
``grade["detail"]`` -- the very rows the printed percentages were counted from.
Nothing here recomputes geometry, re-matches a target, or applies a tolerance.
(2026-08-24: the grader was extended to *emit* those rows; the scores were
verified byte-identical before and after, per view.)

What a glance has to answer, in the user's words -- 画崩了 / 一般 / 画得好:

  GREEN   the answer's wall, and the reading drew it            (画对)
  RED     the answer's wall, and the reading did NOT draw it    (漏画)
  AMBER   a blank the ANSWER ITSELF leaves: another wall lands here and the gt
          DXF is in two fragments across it (measured: zero ink of every family).
          ⭐ 2026-08-25 the user ruled it is NOT deducted -- ink here is neither
          required (C2) nor punished (C4).  It is still DRAWN, because a reader
          has to be able to see where the answer stopped asking.
  MAGENTA ink the reading claims as wall that no target explains  (多画)
  Openings: box per opening the answer has -- green named right, amber named the
          wrong kind, red not found at all.

⛔ ASCII labels only: this container has no CJK font (checked -- zero .ttf/.otf
with Han glyphs anywhere on the filesystem).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DIM = 0.30                       # background drawing, dimmed so overlays read
C_OK = (54, 200, 108)            # 画对
C_MISS = (255, 62, 62)           # 漏画
C_TJ = (255, 176, 32)            # 漏画, but at a T-junction the drawing itself breaks
C_EXTRA = (240, 60, 235)         # 多画
# ⭐ An opening the reading named RIGHT must not compete with the wall colour --
# on sm25 a green box on a green wall is just noise.  Right = thin and muted so
# you can still see where the openings are; wrong = thick, so any loud box on
# the picture is a problem.
C_OPEN_OK = (70, 150, 175)
C_OPEN_KIND = (255, 176, 32)
C_OPEN_LOST = (255, 62, 62)
W_OPEN_OK, W_OPEN_BAD = 1, 4
BG = (14, 14, 18)
FG = (238, 238, 240)
MUTED = (150, 150, 158)

W_OK, W_BAD = 3, 7               # a miss must be fatter than a hit, or it hides


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                   # very old Pillow
        return ImageFont.load_default()


def _mapper(doc: dict):
    """world metres -> pixel, inverting the document's OWN calibration.

    ⛔ Not a fit of our own: ``world_zero_px`` and ``m_per_px`` are what the
    as-drawn build wrote.  Validated 2026-08-24 by re-projecting every face line
    back to its recorded ``pos_px``: max error 0.0028 px across all three views.
    """
    cal = doc["observations"]["calibration"]
    x0, y0 = cal["world_zero_px"]
    mx, my = cal["x"]["m_per_px"], cal["y"]["m_per_px"]
    return (lambda m: x0 + m / mx), (lambda m: y0 - m / my)


def _seg(draw, axis, const_m, lo_m, hi_m, to_x, to_y, colour, width):
    """One stretch of wall. ``axis`` names the world axis held CONSTANT."""
    if axis == "x":                                     # x fixed, runs along y
        px = to_x(const_m)
        draw.line([(px, to_y(lo_m)), (px, to_y(hi_m))], fill=colour, width=width)
    else:                                               # y fixed, runs along x
        py = to_y(const_m)
        draw.line([(to_x(lo_m), py), (to_x(hi_m), py)], fill=colour, width=width)


def render(doc_path: str, grade_path: str, out_path: str) -> dict:
    doc = json.loads(Path(doc_path).read_text())
    g = json.loads(Path(grade_path).read_text())
    detail = g.get("detail")
    if detail is None:
        raise SystemExit("⛔ this grade report predates the detail block; re-run "
                         "reading_grade.py so the picture draws the SAME rows the "
                         "scores were counted from")

    base = Image.open(doc["image"]).convert("RGB")
    im = Image.eval(base, lambda v: int(v * DIM)).convert("RGB")
    d = ImageDraw.Draw(im)
    to_x, to_y = _mapper(doc)

    # ---- layer 1: every target the answer has, hit first then miss on top ----
    n_miss = n_tj = 0
    miss_m = tj_m = 0.0
    for r in detail["targets"]:
        lo, hi = r["span"]
        _seg(d, r["axis"], r["const_m"], lo, hi, to_x, to_y, C_OK, W_OK)
    hole_m = 0.0
    for r in detail["targets"]:                       # blanks the answer itself leaves
        for a, b in r.get("holes") or []:
            _seg(d, r["axis"], r["const_m"], a, b, to_x, to_y, C_TJ, W_BAD)
            hole_m += b - a
    # ⛔ One colour, one meaning: AMBER = the answer leaves it blank (not scored),
    # RED = the reading missed it (scored).  The grader still carries a vestigial
    # `uncovered_at_tjunction` flag from when amber WAS deducted; ignoring it here
    # keeps 0.013 m of leftovers from painting "not scored" over a real miss.
    for r in detail["targets"]:
        for a, b in r.get("uncovered") or []:
            _seg(d, r["axis"], r["const_m"], a, b, to_x, to_y, C_MISS, W_BAD)
            n_miss += 1
            miss_m += b - a

    # ---- layer 2: ink claimed as wall that no target explains (多画) --------
    n_extra = 0
    for e in detail["extras"]:
        for a, b in e.get("spans") or []:
            _seg(d, e["axis"], e["const_m"], a, b, to_x, to_y, C_EXTRA, W_BAD)
            n_extra += 1

    # ---- layer 3: the answer's openings, by whether reading named them -----
    tally = {"OK": 0, "WRONG_KIND": 0, "NOT_FOUND": 0}
    for o in detail["openings"]:
        tally[o["verdict"]] = tally.get(o["verdict"], 0) + 1
        col = {"OK": C_OPEN_OK, "WRONG_KIND": C_OPEN_KIND}.get(o["verdict"], C_OPEN_LOST)
        c0, c1 = o["const_range_m"]
        pad = 0.06                       # so a 120 mm wall's box is still visible
        if o["axis"] == "x":
            box = [to_x(c0 - pad), to_y(o["lo_m"]), to_x(c1 + pad), to_y(o["hi_m"])]
        else:
            box = [to_x(o["lo_m"]), to_y(c0 - pad), to_x(o["hi_m"]), to_y(c1 + pad)]
        d.rectangle([min(box[0], box[2]), min(box[1], box[3]),
                     max(box[0], box[2]), max(box[1], box[3])], outline=col,
                    width=W_OPEN_OK if o["verdict"] == "OK" else W_OPEN_BAD)

    # ---- the numbers, verbatim from the report -----------------------------
    s, den = g["scores"], g["denominator"]
    f_big, f_mid, f_sm = _font(30), _font(20), _font(16)
    rows = [
        (f"DRAWN {s['C2_length_coverage_pct']:.1f}%", C_OK, f_big),
        (f"MISSED {100 - s['C2_length_coverage_pct']:.1f}%"
         f"  ({miss_m + tj_m:.2f} m of {den['length_m']:.0f} m)", C_MISS, f_big),
        (f"OVERDRAWN {s['C4_extra_length_m']:.2f} m"
         f"  = {s['C4_extra_pct_of_answer']:.1f}% of the answer", C_EXTRA, f_big),
    ]
    sub = [
        (f"openings {tally['OK']}/{len(detail['openings'])} named right"
         + (f"   wrong kind {tally['WRONG_KIND']}" if tally["WRONG_KIND"] else "")
         + (f"   not found {tally['NOT_FOUND']}" if tally["NOT_FOUND"] else ""), FG),
        (f"amber = {hole_m:.2f} m the ANSWER leaves blank where another wall lands; "
         f"NOT required, NOT punished", C_TJ),
        (f"{den['view']}   {den['targets']} targets   "
         f"perception graded {g['perception']['face_lines_graded']} face lines, "
         f"abstained {sum(g['perception']['abstained'].values())}", MUTED),
        ("EXPLORATORY -- perception hand-authored by the orchestrator, which had "
         "already seen gt-side results. Not a score.", MUTED),
    ]
    # ⛔ sm24's drawing is 790 px wide and the first version simply ran the text
    # off the edge -- the T-junction line and the "not a score" disclaimer were
    # both truncated mid-word.  Wrap to the actual image width instead.
    def _wrap(text, font, width):
        words, out, cur = text.split(" "), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and dd0.textlength(trial, font=font) > width:
                out.append(cur)
                cur = w
            else:
                cur = trial
        return out + ([cur] if cur else [])

    dd0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    avail = im.width - 32
    laid = [(ln, col, font) for text, col, font in rows
            for ln in _wrap(text, font, avail)]
    laid_sub = [(ln, col, f_mid if col is not MUTED else f_sm)
                for text, col in sub
                for ln in _wrap(text, f_mid if col is not MUTED else f_sm, avail)]

    pad_top, line_big, line_sm = 14, 40, 24
    bar = pad_top + line_big * len(laid) + line_sm * len(laid_sub) + 12
    canvas = Image.new("RGB", (im.width, im.height + bar), BG)
    canvas.paste(im, (0, 0))
    dd = ImageDraw.Draw(canvas)
    y = im.height + pad_top
    for text, col, font in laid:
        dd.text((16, y), text, fill=col, font=font)
        y += line_big
    for text, col, font in laid_sub:
        dd.text((16, y), text, fill=col, font=font)
        y += line_sm

    # legend, top-right
    key = [("answer drawn", C_OK), ("answer MISSED", C_MISS),
           ("answer leaves blank", C_TJ), ("OVERDRAWN", C_EXTRA),
           ("opening ok", C_OPEN_OK), ("opening WRONG", C_OPEN_KIND)]
    bw = 210
    dd.rectangle([im.width - bw - 10, 8, im.width - 8, 12 + 22 * len(key)],
                 fill=(0, 0, 0))
    for i, (lab, col) in enumerate(key):
        dd.rectangle([im.width - bw - 2, 14 + 22 * i, im.width - bw + 16, 28 + 22 * i],
                     fill=col)
        dd.text((im.width - bw + 24, 13 + 22 * i), lab, fill=FG, font=f_sm)

    canvas.save(out_path)
    return {"out": out_path, "view": den["view"],
            "drawn_pct": s["C2_length_coverage_pct"],
            "missed_m": round(miss_m + tj_m, 3),
            "missed_at_tjunction_m": round(tj_m, 3), "tjunction_places": n_tj,
            "overdrawn_m": s["C4_extra_length_m"], "overdrawn_spans": n_extra,
            "openings": tally}


if __name__ == "__main__":
    print(json.dumps(render(*sys.argv[1:4]), ensure_ascii=False))
