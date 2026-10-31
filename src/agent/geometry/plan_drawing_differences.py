"""Report where a plan drawing's ink and a pixel-plan declaration disagree.

Build feedback only: every item is a place to look at the original again, never
a wall or door identification, an approval or an automatic edit. Ink is any
colour far from the drawing's background; what it depicts is the caller's call.
Scope: orthogonal interior double lines (including interrupted pairs), bands,
door gaps and unsupported open separators. Exterior walls, windows, single-line
or oblique wall completeness and heights are not checked.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from PIL import Image, ImageDraw

SCHEMA = "drawing_differences_v2"
MIN_LINE_M = 1.2        # shortest straight ink run treated as wall-like
ALERT_LINE_M = 2.0      # lines counted for the floor-level "no dividers declared" observation
FILLED_MIN_M = 0.05     # a solid band at least this wide is wall-like on its own
PAIR_M = (0.05, 0.45)   # two parallel lines this far apart form a double-line wall
END_TOUCH_M = 0.30      # a wall-like line must reach other walls at both ends
COVER_M = 0.25          # a declared divider this close covers it (thick walls drawn at their centre)
PERIMETER_M = 0.35      # lines along the perimeter are its own faces or window symbols
FACE_REACH_M = 0.30     # wall face lines searched on each side of a declared divider
STRIP_M = 0.22          # fallback half-width when no face line is found
GAP_MIN_M = 0.55        # shortest inkless stretch on a divider treated as a possible doorway
BRIDGE_M = 0.15         # ink shorter than this inside a doorway (arc tips, leaves) is ignored
OFFSET_M = 0.15         # an opening end this far from the matching gap end is reported
WEAK_SUPPORT = 0.5      # a declared divider with less ink over its non-opening length
CONTINUOUS_INK = 0.85   # a declared opening lying on this much ink
FULL_SPAN = 0.85        # an inkless gap covering this much of one junction-bounded segment
FOOTPRINT_M = (2.0, 300.0)
LOOK_MARGIN_M = 0.8
MAX_ITEMS = 10
MEANING = ("Places where the original's ink and this declaration disagree, to check in the "
           "original (look_box): revise only with evidence, or keep the object and say why. "
           "Ink is not identity: furniture, symbols or text also leave ink, and a gap may be a door.")
SCOPE = ("Interior dividers drawn as double lines or filled bands, and gaps in declared dividers; "
         "includes door-interrupted double lines and unsupported open separators; "
         "exterior walls, windows, single-line walls, oblique walls and heights are not checked.")
ORDER = ("no_dividers_declared", "undeclared_wall_line", "opening_on_continuous_ink", "wall_gap_without_opening",
         "declared_divider_with_little_ink", "unsupported_open_separator", "opening_offset_from_gap")
OPENING_OFFSET_CHECK = "The declared opening's ends differ from the ends of the inkless stretch it overlaps."
CONTINUOUS_SPACE_CHECK = "Check whether this is one continuous space rather than a wall with an opening."


def _ink(image):
    rgb = np.asarray(image.convert("RGB")).astype(np.int16)
    quantised = (rgb // 16).reshape(-1, 3)
    step = max(1, len(quantised) // 200000)
    mode = Counter(map(tuple, quantised[::step].tolist())).most_common(1)[0][0]
    background = np.array(mode) * 16 + 8
    return np.linalg.norm(rgb - background, axis=2) > 60


def _axis(anchors):
    (p0, w0), (p1, w1) = anchors
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (p0, w0, p1, w1)):
        raise ValueError("numeric anchors required")
    if p0 == p1 or w0 == w1:
        raise ValueError("distinct anchors required")
    slope = (w1 - w0) / (p1 - p0)
    return (lambda pixel: w0 + (pixel - p0) * slope), abs(slope)


def _runs(flags):
    edges = np.flatnonzero(np.diff(np.concatenate(([0], flags.astype(np.int8), [0]))))
    return list(zip(edges[::2].tolist(), (edges[1::2] - 1).tolist()))


def _segments(points):
    for p, q in zip(points, points[1:]):
        if p[0] == q[0]:
            yield "v", float(p[0]), min(p[1], q[1]), max(p[1], q[1])
        elif p[1] == q[1]:
            yield "h", float(p[1]), min(p[0], q[0]), max(p[0], q[0])


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _fills_junction_span(gap_lo, gap_hi, junctions, along_mpp):
    """Whether a gap fills one whole divider segment between wall junctions.

    Junction coordinates use wall reference lines while ink gaps end at wall
    faces, so each end may differ by up to ``FACE_REACH_M``. Requiring both
    near-junction ends and high span coverage keeps an ordinary doorway inside
    a longer wall segment out of the continuous-space hint.
    """
    for seg_lo, seg_hi in zip(junctions, junctions[1:]):
        span = seg_hi - seg_lo
        if span <= 0:
            continue
        if (_overlap(gap_lo, gap_hi, seg_lo, seg_hi) >= FULL_SPAN * span
                and abs(gap_lo - seg_lo) * along_mpp <= FACE_REACH_M
                and abs(gap_hi - seg_hi) * along_mpp <= FACE_REACH_M):
            return True
    return False


def _touches(point, orient, walls, tolerance):
    """A line end meets a wall at a T/L junction or continues it collinearly; running
    alongside a parallel wall is not a junction."""
    x, y = point
    for wall_orient, at, lo, hi in walls:
        if wall_orient != orient:
            if orient == "v" and abs(y - at) <= tolerance["y"] and lo - tolerance["x"] <= x <= hi + tolerance["x"]:
                return True
            if orient == "h" and abs(x - at) <= tolerance["x"] and lo - tolerance["y"] <= y <= hi + tolerance["y"]:
                return True
        elif orient == "v" and abs(x - at) <= tolerance["x"] / 2 and min(abs(y - lo), abs(y - hi)) <= tolerance["y"]:
            return True
        elif orient == "h" and abs(y - at) <= tolerance["y"] / 2 and min(abs(x - lo), abs(x - hi)) <= tolerance["x"]:
            return True
    return False


def _strokes(mask, orient, minimum):
    """Adjacent long straight runs grouped into strokes (one drawn line or band)."""
    count = mask.shape[1] if orient == "v" else mask.shape[0]
    strokes, active = [], []
    for index in range(count):
        flags = mask[:, index] if orient == "v" else mask[index, :]
        runs = [(s, e) for s, e in _runs(flags) if e - s + 1 >= minimum] if flags.any() else []
        still = []
        for start, end in runs:
            for stroke in active:
                if _overlap(start, end, stroke["start"], stroke["end"]) > 0.5 * min(end - start, stroke["end"] - stroke["start"]):
                    stroke.update(last=index, start=min(start, stroke["start"]), end=max(end, stroke["end"]))
                    still.append(stroke)
                    break
            else:
                stroke = dict(first=index, last=index, start=start, end=end)
                strokes.append(stroke)
                still.append(stroke)
        active = [s for s in still]
    return strokes


def _wall_like(strokes, mpp_cross):
    """Filled bands, and pairs of parallel lines spaced like a wall's two faces."""
    found = []
    for stroke in strokes:
        if (stroke["last"] - stroke["first"] + 1) * mpp_cross >= FILLED_MIN_M:
            found.append(((stroke["first"] + stroke["last"]) / 2, stroke["start"], stroke["end"],
                          (stroke["last"] - stroke["first"] + 1) * mpp_cross))
    ordered = sorted(strokes, key=lambda s: s["first"])
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            spacing = ((b["first"] + b["last"]) - (a["first"] + a["last"])) / 2 * mpp_cross
            if spacing > PAIR_M[1]:
                break
            shared = _overlap(a["start"], a["end"], b["start"], b["end"])
            if spacing >= PAIR_M[0] and shared >= 0.8 * min(a["end"] - a["start"], b["end"] - b["start"]):
                found.append(((a["first"] + b["last"]) / 2, max(a["start"], b["start"]),
                              min(a["end"], b["end"]), spacing))
    found.sort()
    merged = []
    for centre, lo, hi, thickness in found:
        if merged and abs(centre - merged[-1][0]) * mpp_cross <= 0.1 and _overlap(lo, hi, merged[-1][1], merged[-1][2]) > 0:
            c, l, h, t = merged[-1]
            merged[-1] = (c, min(l, lo), max(h, hi), max(t, thickness))
        else:
            merged.append((centre, lo, hi, thickness))
    return merged


def _interrupted_pairs(mask, orient, along_mpp, cross_mpp):
    """Join bounded doorway gaps for *paired* faces only, never infer a wall.

    sm24 run100 omitted the corridor side: its two faces stop at doors and meet
    another omitted wall, so neither uninterrupted-stroke/junction test fired.
    """
    closed = mask.copy()
    count = mask.shape[1] if orient == 'v' else mask.shape[0]
    for index in range(count):
        flags = closed[:, index] if orient == 'v' else closed[index, :]
        runs = [(a, b) for a, b in _runs(flags) if (b-a+1)*along_mpp >= .25]
        for (_, end), (start, _) in zip(runs, runs[1:]):
            gap_m = (start-end-1)*along_mpp
            if GAP_MIN_M <= gap_m <= 1.4 or gap_m <= .10 or start-end-1 <= 2:
                flags[end+1:start] = True
    strokes = _strokes(closed, orient, MIN_LINE_M/along_mpp)
    candidates = []
    for i, a in enumerate(strokes):
        for b in strokes[i+1:]:
            ac, bc = (a['first']+a['last'])/2, (b['first']+b['last'])/2
            spacing = abs(bc-ac)*cross_mpp
            if not PAIR_M[0] <= spacing <= PAIR_M[1]:
                continue
            lo, hi = max(a['start'], b['start']), min(a['end'], b['end'])
            if (hi-lo)*along_mpp < MIN_LINE_M:
                continue
            lengths = [a['end']-a['start'], b['end']-b['start']]
            if min(lengths) < .6*max(lengths) or hi-lo < .8*min(lengths):
                continue
            support = []
            for stroke in (a,b):
                sample = (mask[lo:hi+1,stroke['first']:stroke['last']+1].any(axis=1) if orient=='v'
                          else mask[stroke['first']:stroke['last']+1,lo:hi+1].any(axis=0))
                support.append(float(sample.mean()))
            if min(support) >= .55:
                candidates.append(((ac+bc)/2, lo, hi, spacing))
    return candidates


def _face_flags(ink, orient, at, start, stop, reach, fallback):
    """Ink presence along a declared divider, sampled on its drawn face lines."""
    centre = int(round(at))
    offsets = range(-reach, reach + 1)
    length = stop - start + 1
    if orient == "v":
        columns = [ink[start:stop + 1, c] if 0 <= c < ink.shape[1] else np.zeros(length, bool) for c in (centre + o for o in offsets)]
    else:
        columns = [ink[r, start:stop + 1] if 0 <= r < ink.shape[0] else np.zeros(length, bool) for r in (centre + o for o in offsets)]
    faces = [i for i, column in enumerate(columns) if column.sum() >= 0.35 * length]
    if not faces:
        middle = reach
        faces = range(max(0, middle - fallback), min(len(columns), middle + fallback + 1))
        return np.any([columns[i] for i in faces], axis=0), False
    chosen = set()
    for i in faces:
        chosen.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(columns))
    return np.any([columns[i] for i in sorted(chosen)], axis=0), True


def drawing_differences(image, plan, *, image_name=None, image_sha256=None, plan_sha256=None):
    """Compare one resolved pixel declaration with its original plan image."""
    to_x, mpp_x = _axis(plan["x_anchors"])
    to_y, mpp_y = _axis(plan["y_anchors"])
    ring = [tuple(map(float, p)) for p in plan["footprint_pixels"]]
    spans = ((max(p[0] for p in ring) - min(p[0] for p in ring)) * mpp_x,
             (max(p[1] for p in ring) - min(p[1] for p in ring)) * mpp_y)
    base = dict(schema=SCHEMA, image=image_name, image_sha256=image_sha256, plan_sha256=plan_sha256,
                floor_id=plan.get("floor_id"), meaning=MEANING, scope=SCOPE,
                coverage=dict(footprint_pixels=ring, declared_divider_count=len(plan.get('partitions', [])),
                    checked='orthogonal interior ink/declaration differences within this calibrated footprint',
                    not_checked=['exterior walls/windows', 'single-line or oblique wall completeness',
                                 'heights', 'room identity/use', 'whole-building fidelity']))
    if not all(FOOTPRINT_M[0] <= span <= FOOTPRINT_M[1] for span in spans):
        return dict(base, status="not_checked", total=0, counts={}, items=[],
                    reason=f"calibration gives an implausible footprint of {spans[0]:.3g} x {spans[1]:.3g} m; "
                           "check the scale and units first")
    ink = _ink(image)
    height, width = ink.shape
    px = lambda metres, axis: metres / (mpp_x if axis == "x" else mpp_y)
    inside = Image.new("L", (width, height), 0)
    ImageDraw.Draw(inside).polygon(ring, fill=1)
    inside = np.asarray(inside, dtype=bool)
    perimeter = list(_segments(ring + ring[:1]))
    dividers = [(row["id"], *segment) for row in plan.get("partitions", [])
                for segment in _segments([tuple(map(float, p)) for p in row["points"]])]
    declared_walls = perimeter + [segment for _, *segment in dividers]
    openings = []
    for row in plan.get("openings", []):
        (x0, y0), (x1, y1) = row["p1"], row["p2"]
        if x0 == x1:
            openings.append((row["id"], row.get("kind"), "v", float(x0), min(y0, y1), max(y0, y1)))
        elif y0 == y1:
            openings.append((row["id"], row.get("kind"), "h", float(y0), min(x0, x1), max(x0, x1)))
    items = []

    def look_box(orient, at, lo, hi):
        mx, my = px(LOOK_MARGIN_M, "x"), px(LOOK_MARGIN_M, "y")
        x0, x1, y0, y1 = (at, at, lo, hi) if orient == "v" else (lo, hi, at, at)
        return [max(0, int(x0 - mx)), max(0, int(y0 - my)), min(width, int(x1 + mx) + 1), min(height, int(y1 + my) + 1)]

    def where(orient, at, lo, hi):
        if orient == "v":
            return dict(axis="vertical", x_px=round(at), y_px=[round(lo), round(hi)],
                        x_m=round(to_x(at), 2), y_m=sorted([round(to_y(lo), 2), round(to_y(hi), 2)]))
        return dict(axis="horizontal", y_px=round(at), x_px=[round(lo), round(hi)],
                    y_m=round(to_y(at), 2), x_m=sorted([round(to_x(lo), 2), round(to_x(hi), 2)]))

    # 1. Wall-like ink inside the footprint that no declared divider follows.
    tolerance = {"x": px(END_TOUCH_M, "x"), "y": px(END_TOUCH_M, "y")}
    mask = ink & inside
    candidates = []
    for orient in ("v", "h"):
        along, cross = ("y", "x") if orient == "v" else ("x", "y")
        mpp_cross = mpp_x if orient == "v" else mpp_y
        for centre, lo, hi, thickness in _wall_like(_strokes(mask, orient, px(MIN_LINE_M, along)), mpp_cross):
            candidates.append(dict(orient=orient, at=centre, lo=lo, hi=hi, thickness_m=round(thickness, 2)))
    interrupted = []
    for orient in ('v','h'):
        along_mpp, cross_mpp = (mpp_y,mpp_x) if orient=='v' else (mpp_x,mpp_y)
        for at, lo, hi, thickness in _interrupted_pairs(mask, orient, along_mpp, cross_mpp):
            interrupted.append(dict(orient=orient, at=at, lo=lo, hi=hi, thickness_m=round(thickness,2)))
    if not dividers:
        # A floor-level observation, not an object item: free-standing furniture cannot
        # reach the outer walls, and room walls are longer than most furniture edges.
        reaching = []
        for c in candidates:
            along = mpp_y if c["orient"] == "v" else mpp_x
            ends = ([(c["at"], c["lo"]), (c["at"], c["hi"])] if c["orient"] == "v"
                    else [(c["lo"], c["at"]), (c["hi"], c["at"])])
            if ((c["hi"] - c["lo"]) * along >= ALERT_LINE_M
                    and any(_touches(point, c["orient"], perimeter, tolerance) for point in ends)
                    and not any(p == c["orient"] and abs(a - c["at"]) < px(PERIMETER_M, "x" if p == "v" else "y")
                                and _overlap(c["lo"], c["hi"], l, h) > 0.5 * (c["hi"] - c["lo"])
                                for p, a, l, h in perimeter)):
                reaching.append(c)
        if len(reaching) >= 3:
            reaching.sort(key=lambda c: c["lo"] - c["hi"])
            items.append(dict(type="no_dividers_declared", scope="floor", ink_lines=len(reaching),
                              longest=[where(c["orient"], c["at"], c["lo"], c["hi"]) for c in reaching[:5]],
                              look_box=[0, 0, width, height],
                              check=(f"No interior divider is declared, while {len(reaching)} double or banded ink "
                                     f"lines of {ALERT_LINE_M:g} m or more inside the outline reach the outer walls.")))
    # Each reported line meets walls at both ends and at least one end is a declared wall
    # or the perimeter, so furniture outlines cannot support each other.
    accepted, support = [], list(declared_walls)
    changed = True
    while changed:
        changed = False
        for candidate in candidates:
            if candidate in accepted:
                continue
            o, at, lo, hi = candidate["orient"], candidate["at"], candidate["lo"], candidate["hi"]
            ends = [(at, lo), (at, hi)] if o == "v" else [(lo, at), (hi, at)]
            if (all(_touches(point, o, support, tolerance) for point in ends)
                    and any(_touches(point, o, declared_walls, tolerance) for point in ends)):
                accepted.append(candidate)
                support.append((o, at, lo, hi))
                changed = True
    # Two omitted walls can meet each other, with each anchored to a declared
    # wall at the far end. Requiring that anchor excludes free-standing furniture.
    anchored = []
    pair_tolerance = {axis: max(2., px(.10,axis)) for axis in ('x','y')}
    for c in interrupted:
        o, at, lo, hi = c['orient'], c['at'], c['lo'], c['hi']
        ends = [(at,lo),(at,hi)] if o=='v' else [(lo,at),(hi,at)]
        if any(_touches(p, o, declared_walls, pair_tolerance) for p in ends):
            anchored.append(c)
    network = support + [(c['orient'],c['at'],c['lo'],c['hi']) for c in anchored]
    for c in anchored:
        o, at, lo, hi = c['orient'], c['at'], c['lo'], c['hi']
        ends = [(at,lo),(at,hi)] if o=='v' else [(lo,at),(hi,at)]
        # A candidate cannot provide its own endpoint support.
        others = [line for line in network if line != (o,at,lo,hi)]
        if all(_touches(p,o,others,pair_tolerance) for p in ends):
            accepted.append(c)
    emitted = []
    for candidate in accepted:
        o, at, lo, hi = candidate["orient"], candidate["at"], candidate["lo"], candidate["hi"]
        cross = "x" if o == "v" else "y"
        if any(p == o and abs(a - at) < px(PERIMETER_M, cross) and _overlap(lo, hi, l, h) > 0.5 * (hi - lo)
               for p, a, l, h in perimeter):
            continue
        covered = sum(_overlap(lo, hi, l, h) for _, p, a, l, h in dividers if p == o and abs(a - at) <= px(COVER_M, cross))
        if covered >= 0.6 * (hi - lo):
            continue
        if any(p==o and abs(a-at)<=px(.1,cross) and _overlap(lo,hi,l,h)>=.8*min(hi-lo,h-l)
               for p,a,l,h in emitted):
            continue
        emitted.append((o,at,lo,hi))
        items.append(dict(type="undeclared_wall_line", **where(o, at, lo, hi),
                          length_m=round((hi - lo) * (mpp_y if o == "v" else mpp_x), 2),
                          drawn_thickness_m=candidate["thickness_m"], look_box=look_box(o, at, lo, hi),
                          check="A double or banded ink line meeting walls at both ends; no declared divider lies along it."))

    # 2. Declared dividers: ink on their drawn faces, and gaps against declared openings.
    for divider_id, orient, at, lo, hi in dividers:
        cross = "x" if orient == "v" else "y"
        along_mpp = mpp_y if orient == "v" else mpp_x
        start, stop = int(round(lo)), int(round(hi))
        if stop - start < 2:
            continue
        flags, on_faces = _face_flags(ink, orient, at, start, stop, max(1, int(round(px(FACE_REACH_M, cross)))),
                                      max(1, int(round(px(STRIP_M, cross)))))
        hosted = [(oid, kind, l, h) for oid, kind, o, a, l, h in openings
                  if o == orient and abs(a - at) <= px(FACE_REACH_M, cross) and l >= lo - 2 and h <= hi + 2]
        # A full-width open connection can hide a made-up partition: deducting
        # the opening leaves no wall to sample (sm25 candidate_07, D_core).
        junctions = [lo,hi]
        for o,a,l,h in declared_walls:
            if o != orient and l-1 <= at <= h+1 and lo < a < hi:
                junctions.append(a)
        junctions = sorted(set(junctions))
        unsupported_ids = set()
        for seg_lo, seg_hi in zip(junctions,junctions[1:]):
            span_m = (seg_hi-seg_lo)*along_mpp
            if span_m < GAP_MIN_M:
                continue
            for oid,kind,l,h in hosted:
                if kind not in {'open','passage'} or _overlap(seg_lo,seg_hi,l,h) < .85*(seg_hi-seg_lo):
                    continue
                margin = max(1, int(round(.12/along_mpp)))
                segment = flags[max(0,int(round(seg_lo))-start+margin):max(0,int(round(seg_hi))-start-margin)]
                if segment.size and float(segment.mean()) < .15:
                    unsupported_ids.add(oid)
                    items.append(dict(type='unsupported_open_separator', divider=divider_id, opening=oid,
                        **where(orient,at,seg_lo,seg_hi), length_m=round(span_m,2),
                        ink_fraction=round(float(segment.mean()),2), look_box=look_box(orient,at,seg_lo,seg_hi),
                        check='An open connection fills this partition segment with almost no ink support. '
                              'Check whether this is one continuous space rather than a wall with an opening.'))
        free = np.ones(flags.size, dtype=bool)
        for _, _, l, h in hosted:
            free[max(0, int(round(l)) - start):max(0, int(round(h)) - start + 1)] = False
        if free.sum() * along_mpp >= 0.5 and flags[free].mean() < WEAK_SUPPORT:
            items.append(dict(type="declared_divider_with_little_ink", divider=divider_id,
                              **where(orient, at, lo, hi), ink_fraction=round(float(flags[free].mean()), 2),
                              look_box=look_box(orient, at, lo, hi),
                              check="Outside its declared openings, most of this declared divider has no ink along its sampled lines."))
            continue
        raw = _runs(~flags)
        gaps = []
        for s, e in raw:
            if gaps and (s - gaps[-1][1] - 1) * along_mpp < BRIDGE_M:
                gaps[-1] = (gaps[-1][0], e)
            else:
                gaps.append((s, e))
        gaps = [(s + start, e + start) for s, e in gaps if (e - s + 1) * along_mpp >= GAP_MIN_M]
        matched = set()
        for gap_lo, gap_hi in gaps:
            overlapping = [(oid, kind, l, h) for oid, kind, l, h in hosted
                           if _overlap(gap_lo, gap_hi, l, h) > 0.5 * min(gap_hi - gap_lo, h - l)]
            if not overlapping:
                items.append(dict(type="wall_gap_without_opening", divider=divider_id,
                                  **where(orient, at, gap_lo, gap_hi),
                                  gap_m=round((gap_hi - gap_lo + 1) * along_mpp, 2),
                                  look_box=look_box(orient, at, gap_lo, gap_hi),
                                  check="A stretch of this declared divider without ink that no declared opening occupies."))
                continue
            for oid, kind, l, h in overlapping:
                matched.add(oid)
                if oid in unsupported_ids:
                    continue
                offsets = [round((l - gap_lo) * along_mpp, 2), round((h - gap_hi) * along_mpp, 2)]
                if max(abs(v) for v in offsets) > OFFSET_M:
                    check = OPENING_OFFSET_CHECK
                    if _fills_junction_span(gap_lo, gap_hi, junctions, along_mpp):
                        check += " " + CONTINUOUS_SPACE_CHECK
                    items.append(dict(type="opening_offset_from_gap", opening=oid, divider=divider_id,
                                      declared=where(orient, at, l, h), gap=where(orient, at, gap_lo, gap_hi),
                                      end_offsets_m=offsets, look_box=look_box(orient, at, min(l, gap_lo), max(h, gap_hi)),
                                      check=check))
        for oid, kind, l, h in hosted:
            if oid in matched or kind == "window":
                continue
            span = flags[max(0, int(round(l)) - start):max(0, int(round(h)) - start + 1)]
            if span.size and span.mean() >= CONTINUOUS_INK:
                items.append(dict(type="opening_on_continuous_ink", opening=oid, divider=divider_id,
                                  **where(orient, at, l, h), ink_fraction=round(float(span.mean()), 2),
                                  look_box=look_box(orient, at, l, h),
                                  check="Ink along this declared divider is continuous across the declared opening."))

    rank = {name: index for index, name in enumerate(ORDER)}
    items.sort(key=lambda row: (rank[row["type"]], -row.get("length_m", row.get("gap_m", 0))))
    base['coverage']['tested_divider_segments'] = len(dividers)
    base['coverage']['zero_means'] = 'No differences found by these scoped ink tests; not complete drawing verification.'
    return dict(base, status="reported", total=len(items), counts=dict(Counter(row["type"] for row in items)),
                items=items)


def compact_differences(report, *, limit=MAX_ITEMS):
    """Short build-response form: counts, first items and whether more exist."""
    if report.get("status") != "reported":
        return {k: report[k] for k in ("status", "reason", "meaning", "scope", "coverage") if k in report}
    return dict(status="reported", total=report["total"], counts=report["counts"],
                items=report["items"][:limit], truncated=report["total"] > limit,
                meaning=report["meaning"], scope=report["scope"],
                coverage=report.get('coverage', {}),
                full_list="inspect_plan_draft(draft_id) returns every item of that draft")
