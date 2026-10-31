"""Coordinate-level reading↔gt scorer — the AUTHORITATIVE reading-quality metric.

User directive (2026-06-24): a reading is judged good/bad ONLY by matching its
**coordinates** against the ground-truth coordinates (walls + windows), element by
element, reporting hit / miss / extra + actual offset. Looking at the rendered
image is auxiliary only and never the basis for a verdict; visual adjudication
belongs to the user. This module institutionalizes that standard so we stop
eyeballing renders to call a reading "clean".

Judge-side only: it reads gt (via `gt.load_gt`), so — like the rest of
`src/agent/judge/` — it must never be imported by gate① checks or stage
executors (enforced by `tests/test_gt_discipline.py`).

What it does, per plan floor image:
  * derives the gt INTERIOR wall lines (vertical x-lines + horizontal y-lines)
    from the gt zone rectangles (an edge is interior iff it is not on the
    footprint boundary), and the gt window spans per facade;
  * extracts the same from a reading JSON's strokes;
  * greedily matches within a tolerance and returns hits/misses/extras + the
    signed coordinate offsets so the precision is visible, not just the count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json

from .gt import load_gt

# A wall counts as "found" if a read line sits within this many metres of the
# gt line (readers legitimately trace the inner face, ~0.12 m off centreline).
DEFAULT_WALL_TOL_M = 0.30
# A window counts as "found" if its centre sits within this of the gt centre.
DEFAULT_WIN_CENTRE_TOL_M = 0.40
DEFAULT_POSITION_TOL_M = DEFAULT_WALL_TOL_M
DEFAULT_EXTENT_TOL_M = 0.30
DEFAULT_COMPLETE_EPS_M = 0.05
# How close to 0 / W / D a coordinate must be to count as "on the boundary".
_BOUNDARY_EPS_M = 0.30
_COLINEAR_EPS_M = 0.05


@dataclass(frozen=True)
class LinearPiece:
    kind: str
    span: tuple[float, float]
    within_tol: bool


@dataclass(frozen=True)
class WallSegment:
    orientation: str
    coord: float
    start: float
    end: float


@dataclass
class WallMatch:
    orientation: str
    status: str
    truth: WallSegment | None
    read: WallSegment | None
    delta: float | None
    lateral_drift: bool = False
    extent_drift: bool = False
    extent_start_drift: bool = False
    extent_end_drift: bool = False
    pieces: list[LinearPiece] = field(default_factory=list)
    truth_intervals: list[WallSegment] = field(default_factory=list)
    read_intervals: list[WallSegment] = field(default_factory=list)


@dataclass
class WindowMatch:
    facade: str
    status: str
    truth: tuple[float, float] | None
    read: tuple[float, float] | None
    centre_delta: float | None
    pieces: list[LinearPiece] = field(default_factory=list)
    truth_intervals: list[tuple[float, float]] = field(default_factory=list)
    read_intervals: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class LineMatch:
    truth: float
    read: float | None          # matched read value, or None if missed
    delta: float | None         # read - truth (signed), or None
    # Three-tier status, same convention as WallMatch/WindowMatch:
    # "complete" (<= complete_eps) / "within_tol" (<= tol, > complete_eps —
    # the previously-unobservable orange tier, F-22) / "miss" (no match
    # found within tol, i.e. read is None).
    status: str = "miss"


@dataclass
class WinMatch:
    truth: tuple[float, float]
    read: tuple[float, float] | None
    centre_delta: float | None


@dataclass
class FloorScore:
    floor: str
    vwalls: list[WallMatch] = field(default_factory=list)
    hwalls: list[WallMatch] = field(default_factory=list)
    extra_vwalls: list[WallMatch] = field(default_factory=list)
    extra_hwalls: list[WallMatch] = field(default_factory=list)
    windows: dict[str, list[WindowMatch]] = field(default_factory=dict)
    extra_windows: dict[str, list[WindowMatch]] = field(default_factory=dict)
    # None means the floor had no reading primitives, so boundary grading is
    # intentionally no-data. A dict means the four footprint sides were graded.
    boundary: dict[str, LineMatch] | None = None

    def wall_hits(self) -> tuple[int, int]:
        ms = self.vwalls + self.hwalls
        return sum(1 for m in ms if m.status in {"complete", "within_tol"}), len(ms)

    def window_hits(self) -> tuple[int, int]:
        hit = tot = 0
        for ms in self.windows.values():
            hit += sum(1 for m in ms if m.status in {"complete", "within_tol"})
            tot += len(ms)
        return hit, tot

    def max_wall_offset(self) -> float:
        ds = [abs(m.delta) for m in self.vwalls + self.hwalls if m.delta is not None]
        return round(max(ds), 3) if ds else 0.0

    def boundary_hits(self) -> tuple[int, int]:
        if self.boundary is None:
            return 0, 0
        return sum(1 for m in self.boundary.values() if m.read is not None), len(self.boundary)


# ---------------------------------------------------------------- gt derivation

def derive_gt_walls(zones: list[dict], W: float, D: float) -> tuple[list[float], list[float]]:
    """Interior vertical x-lines and horizontal y-lines from zone rects.

    Each zone rect is [x0, y0, x1, y1]. A rect edge that does not lie on the
    footprint boundary (x∈{0,W}, y∈{0,D}) is an interior partition line.
    """
    vx: set[float] = set()
    hy: set[float] = set()
    for z in zones:
        x0, y0, x1, y1 = z["rect_m"]
        for x in (x0, x1):
            if _BOUNDARY_EPS_M < x < W - _BOUNDARY_EPS_M:
                vx.add(round(x, 2))
        for y in (y0, y1):
            if _BOUNDARY_EPS_M < y < D - _BOUNDARY_EPS_M:
                hy.add(round(y, 2))
    return sorted(vx), sorted(hy)


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((min(a, b), max(a, b)) for a, b in intervals):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in out if b > a]


def derive_gt_wall_segments(zones: list[dict], W: float, D: float) -> tuple[list[WallSegment], list[WallSegment]]:
    """Interior wall extents from merged gt zone edges.

    Mirrors the renderer's ``_interior_coords``/``_merge`` logic, but lives
    judge-side so scoring and sidecar geometry do not depend on render code.
    """
    v: dict[float, list[tuple[float, float]]] = {}
    h: dict[float, list[tuple[float, float]]] = {}
    for zone in zones:
        vals = zone.get("rect_m") or []
        if len(vals) != 4:
            continue
        x0, y0, x1, y1 = [float(vv) for vv in vals]
        for x in (x0, x1):
            if _BOUNDARY_EPS_M < x < W - _BOUNDARY_EPS_M:
                v.setdefault(round(x, 2), []).append((y0, y1))
        for y in (y0, y1):
            if _BOUNDARY_EPS_M < y < D - _BOUNDARY_EPS_M:
                h.setdefault(round(y, 2), []).append((x0, x1))
    vwalls = [
        WallSegment("v", coord, start, end)
        for coord, segs in sorted(v.items())
        for start, end in _merge(segs)
    ]
    hwalls = [
        WallSegment("h", coord, start, end)
        for coord, segs in sorted(h.items())
        for start, end in _merge(segs)
    ]
    return vwalls, hwalls


def derive_gt_windows(gt: dict, floor_name: str) -> dict[str, list[tuple[float, float]]]:
    """Per-facade window spans for one floor: (start, start+width) along the facade.

    For N/S the span is in world x; for E/W it is the along-facade coordinate
    (world y) exactly as gt stores `x_m`.
    """
    out: dict[str, list[tuple[float, float]]] = {"N": [], "S": [], "E": [], "W": []}
    fac = {"North": "N", "South": "S", "East": "E", "West": "W"}
    for entry in gt.get("windows", []):
        if entry.get("floor") != floor_name:
            continue
        f = fac.get(entry.get("facade"))
        if not f:
            continue
        for op in entry.get("openings", []):
            x = op.get("x_m")
            w = op.get("width_m")
            if x is None or w is None:
                continue
            out[f].append((round(x, 2), round(x + w, 2)))
    return out


# ----------------------------------------------------------- reading extraction

def _strokes(reading: dict) -> list[dict]:
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("strokes"), list):
                return o["strokes"]
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None
    return find(reading) or []


def _dedupe(vals: list[float], tol: float = 0.20) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def _as_segment(g: dict) -> tuple[float, float, float, float] | None:
    """Normalise a stroke geometry to (x1, y1, x2, y2).

    Two reading-schema shapes are both legal: a ``line`` (``p1``/``p2``) and a
    ``rect`` (``x_range_m``/``y_range_m``). A rect is collapsed to its midline
    along the longer axis (a window/wall sub-face spans one direction), so both
    shapes score identically. Returns None when geometry is unusable.
    """
    p1, p2 = g.get("p1"), g.get("p2")
    if p1 and p2:
        return p1[0], p1[1], p2[0], p2[1]
    xr, yr = g.get("x_range_m"), g.get("y_range_m")
    if xr and yr and len(xr) == 2 and len(yr) == 2:
        dx, dy = abs(xr[1] - xr[0]), abs(yr[1] - yr[0])
        if dx >= dy:  # horizontal span at mid-y
            ym = (yr[0] + yr[1]) / 2
            return min(xr), ym, max(xr), ym
        xm = (xr[0] + xr[1]) / 2  # vertical span at mid-x
        return xm, min(yr), xm, max(yr)
    return None


def extract_reading_wall_segments(reading: dict, W: float, D: float) -> tuple[list[WallSegment], list[WallSegment]]:
    vx: list[WallSegment] = []
    hy: list[WallSegment] = []
    for s in _strokes(reading):
        if s.get("pen") != "wall":
            continue
        seg = _as_segment(s.get("geometry", {}))
        if seg is None:
            continue
        x1, y1, x2, y2 = seg
        if abs(x1 - x2) < _COLINEAR_EPS_M and _BOUNDARY_EPS_M < x1 < W - _BOUNDARY_EPS_M:
            vx.append(WallSegment("v", round((x1 + x2) / 2, 2), round(min(y1, y2), 2), round(max(y1, y2), 2)))
        elif abs(y1 - y2) < _COLINEAR_EPS_M and _BOUNDARY_EPS_M < y1 < D - _BOUNDARY_EPS_M:
            hy.append(WallSegment("h", round((y1 + y2) / 2, 2), round(min(x1, x2), 2), round(max(x1, x2), 2)))
    return _dedupe_segments(vx), _dedupe_segments(hy)


def extract_reading_walls(reading: dict, W: float, D: float) -> tuple[list[float], list[float]]:
    vx, hy = extract_reading_wall_segments(reading, W, D)
    return [s.coord for s in vx], [s.coord for s in hy]


def extract_reading_windows(reading: dict, W: float, D: float) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {"N": [], "S": [], "E": [], "W": []}
    for s in _strokes(reading):
        if s.get("pen") != "window":
            continue
        seg = _as_segment(s.get("geometry", {}))
        if seg is None:
            continue
        x1, y1, x2, y2 = seg
        horiz = abs(y1 - y2) < _COLINEAR_EPS_M
        vert = abs(x1 - x2) < _COLINEAR_EPS_M
        if horiz and y1 > D - 1.0:
            out["N"].append((round(min(x1, x2), 2), round(max(x1, x2), 2)))
        elif horiz and y1 < 1.0:
            out["S"].append((round(min(x1, x2), 2), round(max(x1, x2), 2)))
        elif vert and x1 > W - 1.0:
            out["E"].append((round(min(y1, y2), 2), round(max(y1, y2), 2)))
        elif vert and x1 < 1.0:
            out["W"].append((round(min(y1, y2), 2), round(max(y1, y2), 2)))
    return out


def _dedupe_segments(segs: list[WallSegment], tol: float = 0.20) -> list[WallSegment]:
    out: list[WallSegment] = []
    for seg in sorted(segs, key=lambda s: (s.coord, s.start, s.end)):
        if (
            out
            and abs(seg.coord - out[-1].coord) <= tol
            and abs(seg.start - out[-1].start) <= tol
            and abs(seg.end - out[-1].end) <= tol
        ):
            continue
        out.append(seg)
    return out


def _has_reading_primitives(reading: dict) -> bool:
    return any(s.get("pen") in {"wall", "window"} for s in _strokes(reading))


def _boundary_truth(W: float, D: float) -> dict[str, float]:
    return {"S": 0.0, "N": D, "W": 0.0, "E": W}


def extract_reading_boundary(reading: dict, W: float, D: float) -> dict[str, float] | None:
    """Extract product footprint-side coordinates from wall strokes.

    Returns None for a floor with no wall/window primitives at all, preserving
    the renderer's no-data gray behavior. If the floor has primitives but no
    wall stroke on a side, that side is graded as a miss by ``match_boundary``.
    """
    if not _has_reading_primitives(reading):
        return None

    candidates: dict[str, list[float]] = {"S": [], "N": [], "W": [], "E": []}
    for s in _strokes(reading):
        if s.get("pen") != "wall":
            continue
        seg = _as_segment(s.get("geometry", {}))
        if seg is None:
            continue
        x1, y1, x2, y2 = seg
        if abs(x1 - x2) < _COLINEAR_EPS_M:
            x = (x1 + x2) / 2
            if abs(x - 0.0) <= _BOUNDARY_EPS_M:
                candidates["W"].append(round(x, 2))
            if abs(x - W) <= _BOUNDARY_EPS_M:
                candidates["E"].append(round(x, 2))
        elif abs(y1 - y2) < _COLINEAR_EPS_M:
            y = (y1 + y2) / 2
            if abs(y - 0.0) <= _BOUNDARY_EPS_M:
                candidates["S"].append(round(y, 2))
            if abs(y - D) <= _BOUNDARY_EPS_M:
                candidates["N"].append(round(y, 2))

    truth = _boundary_truth(W, D)
    out: dict[str, float] = {}
    for side, vals in candidates.items():
        if vals:
            out[side] = min(vals, key=lambda v: abs(v - truth[side]))
    return out


# ---------------------------------------------------------------------- matching

def _match_lines(
    read: list[float],
    truth: list[float],
    tol: float,
    *,
    complete_eps: float = DEFAULT_COMPLETE_EPS_M,
) -> tuple[list[LineMatch], list[float]]:
    pool = list(read)
    matches: list[LineMatch] = []
    for t in truth:
        cands = [r for r in pool if abs(r - t) <= tol]
        if cands:
            b = min(cands, key=lambda r: abs(r - t))
            # F-22 MINOR-1 (sol cross-review 2026-08-11): the status threshold
            # must compare the RAW (unrounded) difference against
            # `complete_eps` — rounding first and comparing the rounded value
            # silently widens the green tier (e.g. a true 0.054 delta rounds
            # to 0.05 and would wrongly read as "complete" if compared after
            # rounding). `delta` itself is still stored rounded to 2dp for
            # display; only the status decision uses the raw value.
            raw_delta = b - t
            delta = round(raw_delta, 2)
            status = "complete" if abs(raw_delta) <= complete_eps else "within_tol"
            matches.append(LineMatch(t, b, delta, status))
            pool.remove(b)
        else:
            matches.append(LineMatch(t, None, None, "miss"))
    return matches, pool


def match_boundary(
    read: dict[str, float] | None,
    W: float,
    D: float,
    tol: float,
    *,
    complete_eps: float = DEFAULT_COMPLETE_EPS_M,
) -> dict[str, LineMatch] | None:
    if read is None:
        return None
    truth = _boundary_truth(W, D)
    out: dict[str, LineMatch] = {}
    for side in ("S", "N", "W", "E"):
        vals = [read[side]] if side in read else []
        matches, _extra = _match_lines(vals, [truth[side]], tol, complete_eps=complete_eps)
        out[side] = matches[0]
    return out


def _match_windows(read: list[tuple[float, float]], truth: list[tuple[float, float]], tolc: float):
    pool = list(read)
    matches: list[WinMatch] = []
    for ts, te in truth:
        tc = (ts + te) / 2
        cands = [(rs, re) for rs, re in pool if abs((rs + re) / 2 - tc) <= tolc]
        if cands:
            b = min(cands, key=lambda x: abs((x[0] + x[1]) / 2 - tc))
            matches.append(WinMatch((ts, te), b, round((b[0] + b[1]) / 2 - tc, 2)))
            pool.remove(b)
        else:
            matches.append(WinMatch((ts, te), None, None))
    return matches, pool


def _piece(kind: str, a: float, b: float, within_tol: bool) -> LinearPiece | None:
    if b - a <= 1e-6:
        return None
    lo, hi = round(a, 3), round(b, 3)
    return LinearPiece(kind=kind, span=(lo, hi), within_tol=within_tol)


def _segment_pieces(
    product: tuple[float, float],
    truth: tuple[float, float],
    *,
    extent_tol: float,
) -> list[LinearPiece]:
    ps, pe = min(product), max(product)
    ts, te = min(truth), max(truth)
    pieces: list[LinearPiece] = []
    matched = _piece("matched", max(ps, ts), min(pe, te), True)
    if matched is not None:
        pieces.append(matched)
    for a, b in ((ts, min(ps, te)), (max(pe, ts), te)):
        p = _piece("missing", a, b, abs(b - a) <= extent_tol)
        if p is not None:
            pieces.append(p)
    for a, b in ((ps, min(ts, pe)), (max(te, ps), pe)):
        p = _piece("extra", a, b, abs(b - a) <= extent_tol)
        if p is not None:
            pieces.append(p)
    pieces.sort(key=lambda p: (p.span[0], {"matched": 0, "missing": 1, "extra": 2}[p.kind]))
    return pieces


def _intervals(vals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return _merge(vals)


def _interval_intersection(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi - lo > 1e-6:
            out.append((round(lo, 3), round(hi, 3)))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _interval_difference(
    base: list[tuple[float, float]],
    subtract: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in base:
        cursor = start
        for sub_start, sub_end in subtract:
            if sub_end <= cursor:
                continue
            if sub_start >= end:
                break
            if sub_start > cursor:
                out.append((round(cursor, 3), round(min(sub_start, end), 3)))
            cursor = max(cursor, sub_end)
            if cursor >= end:
                break
        if cursor < end:
            out.append((round(cursor, 3), round(end, 3)))
    return [(a, b) for a, b in out if b - a > 1e-6]


def _pieces_from_interval_sets(
    read_union: list[tuple[float, float]],
    truth_union: list[tuple[float, float]],
    *,
    extent_tol: float,
) -> list[LinearPiece]:
    pieces: list[LinearPiece] = []
    for a, b in _interval_intersection(read_union, truth_union):
        p = _piece("matched", a, b, True)
        if p is not None:
            pieces.append(p)
    for a, b in _interval_difference(truth_union, read_union):
        p = _piece("missing", a, b, b - a <= extent_tol)
        if p is not None:
            pieces.append(p)
    for a, b in _interval_difference(read_union, truth_union):
        p = _piece("extra", a, b, b - a <= extent_tol)
        if p is not None:
            pieces.append(p)
    pieces.sort(key=lambda p: (p.span[0], {"matched": 0, "missing": 1, "extra": 2}[p.kind]))
    return pieces


def _status_from_diff_pieces(
    pieces: list[LinearPiece],
    *,
    complete_eps: float,
    extent_tol: float,
) -> str:
    diffs = [p for p in pieces if p.kind in {"missing", "extra"}]
    if not diffs:
        return "complete"
    if all(p.span[1] - p.span[0] <= complete_eps for p in diffs):
        return "complete"
    if all(p.span[1] - p.span[0] <= extent_tol for p in diffs):
        return "within_tol"
    return "miss"


def _wall_extent_flags(
    read_union: list[tuple[float, float]],
    truth_union: list[tuple[float, float]],
    pieces: list[LinearPiece],
    *,
    complete_eps: float,
    extent_tol: float,
) -> tuple[bool, bool, bool]:
    diffs = [p for p in pieces if p.kind in {"missing", "extra"}]
    extent_complete = all(p.span[1] - p.span[0] <= complete_eps for p in diffs)
    extent_within = all(p.span[1] - p.span[0] <= extent_tol for p in diffs)
    extent_drift = bool(diffs) and extent_within and not extent_complete
    return extent_complete, extent_within, extent_drift


def _wall_status_and_drifts(
    pieces: list[LinearPiece],
    read_union: list[tuple[float, float]],
    truth_union: list[tuple[float, float]],
    delta: float | None,
    *,
    position_tol: float,
    complete_eps: float,
    extent_tol: float,
) -> tuple[str, bool, bool, bool, bool]:
    extent_complete, extent_within, extent_drift = _wall_extent_flags(
        read_union,
        truth_union,
        pieces,
        complete_eps=complete_eps,
        extent_tol=extent_tol,
    )
    lateral_abs = abs(delta) if delta is not None else 0.0
    lateral_complete = lateral_abs <= complete_eps
    lateral_within = lateral_abs <= position_tol
    lateral_drift = complete_eps < lateral_abs <= position_tol

    if lateral_complete and extent_complete:
        return "complete", lateral_drift, extent_drift, False, False
    if lateral_within and extent_within:
        if read_union and truth_union:
            read_start = min(a for a, _b in read_union)
            read_end = max(b for _a, b in read_union)
            truth_start = min(a for a, _b in truth_union)
            truth_end = max(b for _a, b in truth_union)
            start_drift = complete_eps < abs(read_start - truth_start) <= extent_tol
            end_drift = complete_eps < abs(read_end - truth_end) <= extent_tol
        else:
            start_drift = end_drift = False
        return "within_tol", lateral_drift, extent_drift, start_drift, end_drift
    return "miss", lateral_drift, extent_drift, False, False


def _linear_status(
    read_start: float,
    read_end: float,
    truth_start: float,
    truth_end: float,
    *,
    complete_eps: float,
    extent_tol: float,
) -> str:
    if abs(read_start - truth_start) <= complete_eps and abs(read_end - truth_end) <= complete_eps:
        return "complete"
    off_start = abs(read_start - truth_start)
    off_end = abs(read_end - truth_end)
    if off_start <= extent_tol and off_end <= extent_tol:
        return "within_tol"
    return "miss"


def _match_wall_segments(
    read: list[WallSegment],
    truth: list[WallSegment],
    *,
    position_tol: float,
    extent_tol: float,
    complete_eps: float,
) -> tuple[list[WallMatch], list[WallMatch]]:
    if not truth and not read:
        return [], []

    gt_coords = _dedupe([s.coord for s in truth], tol=position_tol)
    clusters: list[tuple[list[WallSegment], list[WallSegment]]] = []
    used_read: set[int] = set()
    for coord in gt_coords:
        gt_group = [s for s in truth if abs(s.coord - coord) <= position_tol]
        read_group = [
            s for idx, s in enumerate(read)
            if idx not in used_read and abs(s.coord - coord) <= position_tol
        ]
        for idx, s in enumerate(read):
            if s in read_group:
                used_read.add(idx)
        clusters.append((gt_group, read_group))

    leftover = [s for idx, s in enumerate(read) if idx not in used_read]
    for coord in _dedupe([s.coord for s in leftover], tol=position_tol):
        clusters.append(([], [s for s in leftover if abs(s.coord - coord) <= position_tol]))

    matches: list[WallMatch] = []
    extras: list[WallMatch] = []
    for gt_group, read_group in clusters:
        orientation = (gt_group or read_group)[0].orientation
        gt_union = _intervals([(s.start, s.end) for s in gt_group])
        read_union = _intervals([(s.start, s.end) for s in read_group])
        truth_rep = gt_group[0] if gt_group else None
        read_rep = read_group[0] if read_group else None
        gt_coord = sum(s.coord for s in gt_group) / len(gt_group) if gt_group else None
        read_coord = sum(s.coord for s in read_group) / len(read_group) if read_group else None
        delta = round(read_coord - gt_coord, 2) if read_coord is not None and gt_coord is not None else None

        if gt_group and not read_group:
            pieces = [
                LinearPiece("missing", (round(a, 3), round(b, 3)), False)
                for a, b in gt_union
            ]
            matches.append(
                WallMatch(
                    orientation=orientation,
                    status="miss",
                    truth=truth_rep,
                    read=None,
                    delta=None,
                    pieces=pieces,
                    truth_intervals=gt_group,
                    read_intervals=[],
                )
            )
            continue
        if read_group and not gt_group:
            pieces = [
                LinearPiece("extra", (round(a, 3), round(b, 3)), False)
                for a, b in read_union
            ]
            extras.append(
                WallMatch(
                    orientation=orientation,
                    status="extra",
                    truth=None,
                    read=read_rep,
                    delta=None,
                    pieces=pieces,
                    truth_intervals=[],
                    read_intervals=read_group,
                )
            )
            continue

        pieces = _pieces_from_interval_sets(read_union, gt_union, extent_tol=extent_tol)
        status, lateral_drift, extent_drift, extent_start_drift, extent_end_drift = _wall_status_and_drifts(
            pieces,
            read_union,
            gt_union,
            delta,
            position_tol=position_tol,
            complete_eps=complete_eps,
            extent_tol=extent_tol,
        )
        if status == "complete" or (status == "within_tol" and not extent_drift):
            pieces = []
        matches.append(
            WallMatch(
                orientation=orientation,
                status=status,
                truth=truth_rep,
                read=read_rep,
                delta=delta,
                lateral_drift=lateral_drift,
                extent_drift=extent_drift,
                extent_start_drift=extent_start_drift,
                extent_end_drift=extent_end_drift,
                pieces=pieces,
                truth_intervals=gt_group,
                read_intervals=read_group,
            )
        )
    return matches, extras


def _segment_overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _match_window_segments(
    facade: str,
    read: list[tuple[float, float]],
    truth: list[tuple[float, float]],
    *,
    extent_tol: float,
    complete_eps: float,
) -> tuple[list[WindowMatch], list[WindowMatch]]:
    if not truth and not read:
        return [], []
    gt_union = _intervals([(min(a, b), max(a, b)) for a, b in truth])
    read_union = _intervals([(min(a, b), max(a, b)) for a, b in read])
    truth_rep = gt_union[0] if gt_union else None
    read_rep = read_union[0] if read_union else None
    if truth and not read:
        return [
            WindowMatch(
                facade=facade,
                status="miss",
                truth=truth_rep,
                read=None,
                centre_delta=None,
                pieces=[LinearPiece("missing", (round(a, 3), round(b, 3)), False) for a, b in gt_union],
                truth_intervals=gt_union,
                read_intervals=[],
            )
        ], []
    if read and not truth:
        return [], [
            WindowMatch(
                facade=facade,
                status="extra",
                truth=None,
                read=read_rep,
                centre_delta=None,
                pieces=[LinearPiece("extra", (round(a, 3), round(b, 3)), False) for a, b in read_union],
                truth_intervals=[],
                read_intervals=read_union,
            )
        ]
    pieces = _pieces_from_interval_sets(read_union, gt_union, extent_tol=extent_tol)
    status = _status_from_diff_pieces(pieces, complete_eps=complete_eps, extent_tol=extent_tol)
    if status == "complete":
        pieces = []
    gt_center = sum((a + b) / 2 for a, b in gt_union) / len(gt_union) if gt_union else None
    read_center = sum((a + b) / 2 for a, b in read_union) / len(read_union) if read_union else None
    return [
        WindowMatch(
            facade=facade,
            status=status,
            truth=truth_rep,
            read=read_rep,
            centre_delta=round(read_center - gt_center, 2) if read_center is not None and gt_center is not None else None,
            pieces=pieces,
            truth_intervals=gt_union,
            read_intervals=read_union,
        )
    ], []


def score_floor(
    reading: dict,
    gt: dict,
    floor_name: str,
    *,
    wall_tol: float = DEFAULT_WALL_TOL_M,
    win_tol: float = DEFAULT_WIN_CENTRE_TOL_M,
    position_tol: float | None = None,
    extent_tol: float = DEFAULT_EXTENT_TOL_M,
    complete_eps: float = DEFAULT_COMPLETE_EPS_M,
) -> FloorScore:
    fp = gt["footprint"]
    W, D = float(fp["W_m"]), float(fp["D_m"])
    floor = next(f for f in gt["floors"] if f["name"] == floor_name)

    position_tol = wall_tol if position_tol is None else position_tol
    gvx, ghy = derive_gt_wall_segments(floor["zones"], W, D)
    gwin = derive_gt_windows(gt, floor_name)
    rvx, rhy = extract_reading_wall_segments(reading, W, D)
    rwin = extract_reading_windows(reading, W, D)
    rbnd = extract_reading_boundary(reading, W, D)

    sc = FloorScore(floor=floor_name)
    sc.vwalls, sc.extra_vwalls = _match_wall_segments(
        rvx,
        gvx,
        position_tol=position_tol,
        extent_tol=extent_tol,
        complete_eps=complete_eps,
    )
    sc.hwalls, sc.extra_hwalls = _match_wall_segments(
        rhy,
        ghy,
        position_tol=position_tol,
        extent_tol=extent_tol,
        complete_eps=complete_eps,
    )
    sc.boundary = match_boundary(rbnd, W, D, wall_tol, complete_eps=complete_eps)
    for f in ("N", "S", "E", "W"):
        ms, extra = _match_window_segments(
            f,
            rwin[f],
            gwin[f],
            extent_tol=extent_tol,
            complete_eps=complete_eps,
        )
        sc.windows[f] = ms
        sc.extra_windows[f] = extra
    return sc


def floor_name_for_image(stem: str, gt: dict) -> str | None:
    """Map a reading image stem (e.g. '1f_view') to a gt floor name."""
    digit = "".join(ch for ch in stem if ch.isdigit())[:1]
    if digit:
        idx = int(digit) - 1
        floors = gt.get("floors", [])
        if 0 <= idx < len(floors):
            return floors[idx]["name"]
    return None


def score_reading_dir(
    reading_dir: Path | str,
    case: str,
    *,
    gt_dir: Path | str | None = None,
    wall_tol: float = DEFAULT_WALL_TOL_M,
    win_tol: float = DEFAULT_WIN_CENTRE_TOL_M,
    position_tol: float | None = None,
    extent_tol: float = DEFAULT_EXTENT_TOL_M,
    complete_eps: float = DEFAULT_COMPLETE_EPS_M,
) -> dict[str, FloorScore]:
    """Score every plan `*_view.json` in a dir against the case gt."""
    gt = load_gt(case, gt_dir=gt_dir) if gt_dir else load_gt(case)
    if gt is None:
        raise FileNotFoundError(f"no gt for case {case!r}")
    out: dict[str, FloorScore] = {}
    for p in sorted(Path(reading_dir).glob("*_view.json")):
        reading = json.loads(p.read_text(encoding="utf-8"))
        if reading.get("image_kind") not in (None, "plan"):
            continue
        fname = floor_name_for_image(p.stem, gt)
        if fname is None:
            continue
        out[p.stem] = score_floor(
            reading,
            gt,
            fname,
            wall_tol=wall_tol,
            win_tol=win_tol,
            position_tol=position_tol,
            extent_tol=extent_tol,
            complete_eps=complete_eps,
        )
    return out
