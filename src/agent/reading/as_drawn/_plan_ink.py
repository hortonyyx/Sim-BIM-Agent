"""Prototype: ink-dialect separation + annotation calibration + opening band scan.

Built during the 2026-08-22 orchestrator hands-on run. Purpose is to replace three
reading steps that were previously human judgement:

  1. "which ink is structure / fenestration / annotation"  -> measured, declared
  2. "where does the drawing say 5000 mm is"                -> witness ticks + LSQ fit
  3. "is this band segment a window or a wall pier"         -> ink family inside band

(3) is the F-69 killer. The existing `clean_vector_v1` recipe masks only
R~=G~=B ink, so the entire fenestration layer (12.3% of sm25 1f ink) is
structurally invisible to every existing probe -- the reader could not measure
openings, only infer them from gaps in the wall face lines. It inferred
correctly on the north wall and inverted on the east wall. Not a judgement
failure; a missing observable.

Degradation is explicit, never silent: a monochrome drawing (sm20: 100% gray)
reports mode="monochrome" and marks every band segment polarity_ambiguous, so
the caller knows the colour cue is unavailable rather than getting a confident
wrong answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
from PIL import Image

Axis = Literal["row", "col"]

# Ink family definitions. Kept as plain thresholds on RGB rather than HSV so the
# rule stays readable and each family's miss/steal can be reasoned about directly.
# `other` catches everything unclaimed and is reported, never dropped -- that is
# the consumption ledger (F-65 shape): an unrecognised ink family must show up as
# a named number, not as silence.
INK_MIN = 40           # below this the pixel is background (drawings are dark-bg)
NEUTRAL_SPREAD = 24    # max(RGB)-min(RGB) <= this  => neutral (structure)
CHANNEL_LEAD = 40      # how far a channel must lead to claim a hue family
PAIR_BALANCE = 60      # max difference between the two leading channels


def load_rgb(path: str | Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)


def ink_families(a: np.ndarray) -> dict[str, np.ndarray]:
    """Split ink into semantic layers by colour. Returns boolean masks."""
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    ink = a.max(2) >= INK_MIN
    neutral = ink & ((a.max(2) - a.min(2)) <= NEUTRAL_SPREAD)
    green = ink & (G > R + CHANNEL_LEAD) & (G > B + CHANNEL_LEAD)
    cyan = ink & (G > R + CHANNEL_LEAD) & (B > R + CHANNEL_LEAD) & (abs(G - B) <= PAIR_BALANCE)
    magenta = ink & (R > G + CHANNEL_LEAD) & (B > G + CHANNEL_LEAD) & (abs(R - B) <= PAIR_BALANCE)
    yellow = ink & (R > B + CHANNEL_LEAD) & (G > B + CHANNEL_LEAD) & (abs(R - G) <= PAIR_BALANCE)
    claimed = neutral | green | cyan | magenta | yellow
    return {
        "structure": neutral,
        "annotation": green,
        "fenestration": cyan,
        "furniture": magenta,
        "yellow": yellow,
        "other": ink & ~claimed,
        "ink": ink,
    }


def dialect_report(a: np.ndarray) -> dict[str, Any]:
    """Declare the drawing's ink dialect. Every family is named with a number."""
    fams = ink_families(a)
    total = int(fams["ink"].sum())
    out: dict[str, Any] = {"ink_px": total, "image_size_px": [int(a.shape[1]), int(a.shape[0])], "families": {}}
    for name in ("structure", "annotation", "fenestration", "furniture", "yellow", "other"):
        n = int(fams[name].sum())
        out["families"][name] = {"px": n, "pct_of_ink": round(100.0 * n / total, 2) if total else 0.0}
    fen = out["families"]["fenestration"]["pct_of_ink"]
    ann = out["families"]["annotation"]["pct_of_ink"]
    out["mode"] = "layered" if fen >= 1.0 else "monochrome"
    out["has_annotation_layer"] = ann >= 1.0
    out["notes"] = (
        "fenestration layer present: opening polarity is directly observable"
        if out["mode"] == "layered"
        else "NO fenestration colour layer: opening polarity is NOT directly observable; "
        "band segments will be marked polarity_ambiguous"
    )
    return out


# --------------------------------------------------------------------------
# Annotation-layer calibration: witness ticks -> px, OCR'd chain -> mm, LSQ fit
# --------------------------------------------------------------------------


def witness_ticks(mask: np.ndarray, *, axis: Axis, strip: tuple[int, int], coverage: float = 0.6) -> list[float]:
    """Positions of the short perpendicular tick marks of one dimension chain.

    `axis="row"` means the chain's baseline is a horizontal row, so ticks are
    vertical and we return column positions. `strip` is the band of rows (or
    columns) to look in, chosen just off the baseline so the baseline itself
    does not saturate the profile.
    """
    lo, hi = strip
    sub = mask[lo:hi, :] if axis == "row" else mask[:, lo:hi]
    prof = sub.sum(0 if axis == "row" else 1)
    idx = np.where(prof >= (hi - lo) * coverage)[0]
    if not len(idx):
        return []
    groups: list[list[int]] = []
    cur = [int(idx[0])]
    for i in idx[1:]:
        if i - cur[-1] <= 2:
            cur.append(int(i))
        else:
            groups.append(cur)
            cur = [int(i)]
    groups.append(cur)
    return [round(float(np.mean(g)), 2) for g in groups]


@dataclass
class ChainFit:
    """px -> mm affine fit for one axis, plus every number that proves it."""

    axis: Axis
    values_mm: list[float]
    cum_mm: list[float]
    matched_px: list[float]
    unmatched_ticks_px: list[float]
    origin_px: float
    mm_per_px: float
    residual_px: list[float]
    rmse_px: float
    max_abs_residual_px: float
    chain_closure_mm: float | None = None
    overall_mm: float | None = None

    def to_m(self, px: float) -> float:
        return (px - self.origin_px) * self.mm_per_px / 1000.0

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["m_per_px"] = self.mm_per_px / 1000.0
        return d


def fit_chain(
    ticks_px: Iterable[float],
    values_mm: Iterable[float],
    *,
    axis: Axis,
    overall_mm: float | None = None,
    match_tol_px: float = 3.0,
) -> ChainFit:
    """Reconcile an OCR'd dimension chain against extracted witness ticks.

    This is where route A (pixels) and route B (dimension arithmetic) are made
    to agree by code rather than by the reader picking one. The chain gives the
    mm; the ticks give the px; the fit gives the scale AND the residual that
    proves neither was transcribed wrong. Extra ticks (a coarser chain bleeding
    into the strip) are reported, never silently dropped.
    """
    ticks = sorted(float(t) for t in ticks_px)
    vals = [float(v) for v in values_mm]
    cum = [0.0]
    for v in vals:
        cum.append(cum[-1] + v)

    # Seed scale from the chain's full span against the tick span.
    span_mm = cum[-1]
    lo_px, hi_px = ticks[0], ticks[-1]
    scale = span_mm / (hi_px - lo_px)
    origin = lo_px

    matched: list[float] = []
    used: set[int] = set()
    for c in cum:
        want = origin + c / scale
        best, best_d = None, None
        for i, t in enumerate(ticks):
            if i in used:
                continue
            d = abs(t - want)
            if best_d is None or d < best_d:
                best, best_d, best_i = t, d, i
        if best_d is not None and best_d <= match_tol_px:
            matched.append(best)
            used.add(best_i)
        else:
            matched.append(float("nan"))

    # Least squares on the matched pairs: px = origin + mm/scale
    ok = [(m, c) for m, c in zip(matched, cum) if not np.isnan(m)]
    if len(ok) < 2:
        raise ValueError(f"chain fit needs >=2 matched ticks, got {len(ok)}")
    P = np.array([p for p, _ in ok], dtype=float)
    Mm = np.array([c for _, c in ok], dtype=float)
    A = np.vstack([Mm, np.ones_like(Mm)]).T
    inv_scale, origin_fit = np.linalg.lstsq(A, P, rcond=None)[0]
    mm_per_px = 1.0 / inv_scale
    pred = origin_fit + Mm * inv_scale
    resid = (P - pred).tolist()

    return ChainFit(
        axis=axis,
        values_mm=vals,
        cum_mm=cum,
        matched_px=matched,
        unmatched_ticks_px=[t for i, t in enumerate(ticks) if i not in used],
        origin_px=float(origin_fit),
        mm_per_px=float(mm_per_px),
        residual_px=[round(r, 3) for r in resid],
        rmse_px=round(float(np.sqrt(np.mean(np.square(resid)))), 3),
        max_abs_residual_px=round(float(np.max(np.abs(resid))), 3),
        chain_closure_mm=round(cum[-1] - overall_mm, 3) if overall_mm is not None else None,
        overall_mm=overall_mm,
    )


# --------------------------------------------------------------------------
# Opening band scan -- the polarity answer
# --------------------------------------------------------------------------


@dataclass
class BandSegment:
    kind: str                 # window | door | pier | void
    start_px: float
    end_px: float
    length_px: float
    fen_px: int
    struct_px: int
    swing_px: int
    polarity_ambiguous: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def scan_band(
    a: np.ndarray,
    *,
    axis: Axis,
    band_px: tuple[float, float],
    along_range: tuple[float, float],
    min_run_px: int = 3,
    hit_px: int = 2,
    swing_depth_px: int = 26,
    door_swing_min_px: int = 40,
) -> dict[str, Any]:
    """Tile one wall band into window / door / pier / void segments.

    `axis="col"` = a vertical wall: the band spans columns band_px and runs
    along rows along_range.

    Classification is by which ink family occupies the band interior at each
    position along the wall -- fenestration ink means an opening, structure ink
    means solid wall. No inference about "gaps between blocks" is involved, so
    the polarity cannot be chosen per wall.

    A door is an opening whose fenestration ink continues well outside the band
    (the swing arc). A window's ink stops at the wall faces.
    """
    fams = ink_families(a)
    fen, struct = fams["fenestration"], fams["structure"]
    b0, b1 = int(round(min(band_px))), int(round(max(band_px)))
    a0, a1 = int(round(min(along_range))), int(round(max(along_range)))
    mode_layered = int(fen.sum()) >= max(1, int(0.01 * fams["ink"].sum()))

    if axis == "col":
        inner = slice(b0 + 1, b1)                     # strictly between the face lines
        fen_line = fen[a0:a1, inner].sum(1)
        str_line = struct[a0:a1, inner].sum(1)
        left = fen[a0:a1, max(0, b0 - swing_depth_px):b0].sum(1)
        right = fen[a0:a1, b1 + 1:b1 + 1 + swing_depth_px].sum(1)
    else:
        inner = slice(b0 + 1, b1)
        fen_line = fen[inner, a0:a1].sum(0)
        str_line = struct[inner, a0:a1].sum(0)
        left = fen[max(0, b0 - swing_depth_px):b0, a0:a1].sum(0)
        right = fen[b1 + 1:b1 + 1 + swing_depth_px, a0:a1].sum(0)
    swing = np.maximum(left, right)

    labels = np.where(fen_line >= hit_px, 2, np.where(str_line >= hit_px, 1, 0))

    # run-length encode, then absorb runs shorter than min_run_px into their
    # left neighbour (anti-alias speckle at jambs, typically 1-2 px)
    runs: list[list[int]] = []
    for i, lab in enumerate(labels):
        if runs and runs[-1][0] == lab:
            runs[-1][2] = i
        else:
            runs.append([int(lab), i, i])
    merged: list[list[int]] = []
    for r in runs:
        if merged and (r[2] - r[1] + 1) < min_run_px:
            merged[-1][2] = r[2]
        else:
            merged.append(r)

    segs: list[BandSegment] = []
    for lab, i0, i1 in merged:
        s0, s1 = a0 + i0, a0 + i1 + 1
        f = int(fen_line[i0:i1 + 1].sum())
        s = int(str_line[i0:i1 + 1].sum())
        sw = int(swing[i0:i1 + 1].max()) if i1 >= i0 else 0
        if lab == 2:
            kind = "door" if sw >= door_swing_min_px else "window"
        elif lab == 1:
            kind = "pier"
        else:
            kind = "void"
        segs.append(
            BandSegment(
                kind=kind, start_px=float(s0), end_px=float(s1), length_px=float(s1 - s0),
                fen_px=f, struct_px=s, swing_px=sw,
                polarity_ambiguous=not mode_layered,
                evidence={"label": int(lab), "swing_depth_px": swing_depth_px},
            )
        )

    covered = sum(s.length_px for s in segs)
    return {
        "axis": axis,
        "band_px": [b0, b1],
        "band_width_px": b1 - b0,
        "along_range_px": [a0, a1],
        "mode": "layered" if mode_layered else "monochrome",
        "segments": [s.__dict__ for s in segs],
        "assertions": {
            "tiles_range": abs(covered - (a1 - a0)) < 1e-6,
            "covered_px": covered,
            "expected_px": float(a1 - a0),
            "window_count": sum(1 for s in segs if s.kind == "window"),
            "door_count": sum(1 for s in segs if s.kind == "door"),
            "pier_count": sum(1 for s in segs if s.kind == "pier"),
        },
    }


def dump(obj: Any, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=lambda o: o.__dict__), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# A x B reconciliation: fenestration ink says WHICH segment is an opening,
# dimension witness ticks say WHERE its edges are.
# --------------------------------------------------------------------------


def collect_ticks(mask_annotation: np.ndarray, strips: list[tuple[Axis, tuple[int, int]]]) -> list[float]:
    """Union of witness ticks from every dimension chain on one axis.

    Which chain a tick belongs to does not matter for snapping -- only that the
    draughtsman put a tick there, which means a real geometric division sits at
    that coordinate.
    """
    out: list[float] = []
    for axis, strip in strips:
        out.extend(witness_ticks(mask_annotation, axis=axis, strip=strip))
    return sorted(set(out))


def snap_segments(
    segments: list[dict[str, Any]],
    ticks_px: list[float],
    *,
    tol_px: float = 3.0,
    kinds: tuple[str, ...] = ("window", "door"),
) -> list[dict[str, Any]]:
    """Snap opening edges to the nearest dimension witness tick.

    Route A (fenestration ink) is unambiguous about WHICH run is an opening but
    runs ~1-2 px fat because the glazing symbol includes its frame. Route B
    (ticks) is exact but cannot tell an opening from a pier. Snapping composes
    them. Every snap records its distance, and an edge with no tick in range
    keeps its pixel value and is marked `snapped: false` -- never silently
    moved, never silently kept.
    """
    out = []
    for s in segments:
        s = dict(s)
        if s["kind"] in kinds and ticks_px:
            snapped = {}
            for edge in ("start_px", "end_px"):
                v = s[edge]
                near = min(ticks_px, key=lambda t: abs(t - v))
                if abs(near - v) <= tol_px:
                    snapped[edge] = {"from": v, "to": near, "delta_px": round(near - v, 2), "snapped": True}
                    s[edge] = near
                else:
                    snapped[edge] = {"from": v, "to": v, "delta_px": None, "snapped": False,
                                     "nearest_tick_px": near, "nearest_dist_px": round(abs(near - v), 2)}
            s["length_px"] = s["end_px"] - s["start_px"]
            s["snap"] = snapped
        out.append(s)
    return out


def vertical_runs_mask(mask: np.ndarray, min_run: int) -> np.ndarray:
    """Keep only pixels belonging to a vertical run of at least `min_run`."""
    h = mask.shape[0]
    cnt = np.zeros_like(mask, dtype=np.int32)
    run = np.zeros(mask.shape[1], dtype=np.int32)
    for y in range(h):
        run = np.where(mask[y], run + 1, 0)
        cnt[y] = run
    keep = np.zeros_like(mask)
    run = np.zeros(mask.shape[1], dtype=np.int32)
    for y in range(h - 1, -1, -1):
        run = np.where(mask[y], run + 1, 0)
        keep[y] = (cnt[y] + run - 1) >= min_run
    return keep & mask


def face_lines(mask_structure: np.ndarray, *, axis: Axis, min_run_px: int = 14, min_support: int = 10) -> list[dict[str, Any]]:
    """Detect straight structure lines and their extents.

    `axis="col"` returns vertical lines (constant x). Unlike a whole-image
    projection this keeps short interior partitions: support is counted only
    along runs, so a 1 m long partition is still a peak in its own column.
    """
    m = mask_structure if axis == "col" else mask_structure.T
    keep = vertical_runs_mask(m, min_run_px)
    support = keep.sum(0)
    cols = np.where(support >= min_support)[0]
    groups: list[list[int]] = []
    for c in cols:
        if groups and c - groups[-1][-1] <= 1:
            groups[-1].append(int(c))
        else:
            groups.append([int(c)])
    lines = []
    for g in groups:
        sub = keep[:, g].any(1)
        rows = np.where(sub)[0]
        pos = float(np.average(g, weights=support[g]))
        lines.append({
            "pos_px": round(pos, 2),
            "cols_px": [g[0], g[-1]],
            "support_px": int(support[g].sum()),
            "extent_px": [int(rows.min()), int(rows.max()) + 1],
            "coverage": round(float(sub[rows.min():rows.max() + 1].mean()), 3),
        })
    return lines


def _thickness_tol(t: float, tol_px: float) -> float:
    """Tolerance for matching a measured band width to a declared thickness.

    Anti-aliasing adds roughly one pixel of ink at each edge, which is noise on
    a 240 mm wall drawn 11 px wide (sm25) and a 26% error on the same wall drawn
    8.7 px wide (sm24, a smaller sheet). A flat 2 px tolerance therefore accepts
    every sm25 band and rejects every sm24 one. Scaling with the declared
    thickness keeps one rule for both; the measured value is always reported
    alongside the declared one so the inflation stays visible.
    """
    return max(tol_px, 0.30 * t)


def pair_bands(lines: list[dict[str, Any]], thickness_px: list[float], *, tol_px: float = 2.0) -> list[dict[str, Any]]:
    """Pair face lines into wall bands whose spacing matches a declared thickness.

    Thicknesses come from the drawing's own callouts (sm25 annotates 240), never
    from a hard-coded guess -- an undeclared spacing yields no band and shows up
    in the unpaired ledger instead of being absorbed silently.
    """
    bands, used = [], set()
    # Dialect 2 (sm24): a wall drawn as ONE solid filled band rather than two
    # thin face lines. `face_lines` already returns such a wall as a single
    # entry whose own `cols_px` span IS the thickness, so it becomes a band
    # directly. Without this the whole drawing yields zero bands -- which is
    # what happened the first time these tools met sm24, and is exactly the
    # "do not specialise to one case" constraint biting.
    for i, a in enumerate(lines):
        w = a["cols_px"][1] - a["cols_px"][0] + 1
        hit = [t for t in thickness_px if abs(w - t) <= _thickness_tol(t, tol_px)]
        if hit:
            bands.append({
                "band_px": [float(a["cols_px"][0]), float(a["cols_px"][1] + 1)],
                "width_px": float(w),
                "matched_thickness_px": hit[0],
                "overlap_extent_px": list(a["extent_px"]),
                "lines": [a],
                "representation": "solid_fill",
            })
            used.add(i)
    for i, a in enumerate(lines):
        for j in range(i + 1, len(lines)):
            b = lines[j]
            d = b["pos_px"] - a["pos_px"]
            if d > max(thickness_px) + _thickness_tol(max(thickness_px), tol_px):
                break
            hit = [t for t in thickness_px if abs(d - t) <= _thickness_tol(t, tol_px)]
            if not hit:
                continue
            lo = max(a["extent_px"][0], b["extent_px"][0])
            hi = min(a["extent_px"][1], b["extent_px"][1])
            if hi - lo < 10:
                continue
            bands.append({
                "band_px": [a["pos_px"], b["pos_px"]],
                "width_px": round(d, 2),
                "matched_thickness_px": hit[0],
                "overlap_extent_px": [lo, hi],
                "lines": [a, b],
                "representation": "two_face_lines",
            })
            used.update({i, j})
    return bands


def classify_doors(
    a: np.ndarray,
    scan: dict[str, Any],
    *,
    min_leaf_m: float = 0.6,
    max_leaf_m: float = 1.8,
    mm_per_px: float = 21.636,
    arc_hit_px: int = 25,
) -> dict[str, Any]:
    """Re-label band voids that carry a swing arc as doors.

    A plan door is NOT drawn like a window: the wall band is simply interrupted
    (nothing between the face lines) and the fenestration layer carries a leaf
    plus a quarter-circle arc that reaches into the room. So the door signal
    lives OUTSIDE the band, over a depth of roughly one leaf width -- which is
    why scanning only the band interior finds windows and misses doors, and why
    the first cut of this scan reported doors as zero-length window slivers.
    """
    fams = ink_families(a)
    fen = fams["fenestration"]
    axis = scan["axis"]
    b0, b1 = scan["band_px"]
    out = []
    for s in scan["segments"]:
        s = dict(s)
        length_m = s["length_px"] * mm_per_px / 1000.0
        if s["kind"] in ("void", "window") and min_leaf_m <= length_m <= max_leaf_m:
            d = int(round(s["length_px"]))
            i0, i1 = int(s["start_px"]), int(s["end_px"])
            if axis == "col":
                near = fen[i0:i1, max(0, int(b0) - d):int(b0)].sum()
                far = fen[i0:i1, int(b1) + 1:int(b1) + 1 + d].sum()
            else:
                near = fen[max(0, int(b0) - d):int(b0), i0:i1].sum()
                far = fen[int(b1) + 1:int(b1) + 1 + d, i0:i1].sum()
            arc = int(max(near, far))
            if arc >= arc_hit_px and s["fen_px"] < 0.5 * arc:
                s["kind"] = "door"
                s["evidence"] = dict(s.get("evidence") or {}, arc_px=arc, leaf_depth_px=d)
        out.append(s)
    scan = dict(scan)
    scan["segments"] = out
    scan["assertions"] = dict(scan["assertions"])
    scan["assertions"]["window_count"] = sum(1 for s in out if s["kind"] == "window")
    scan["assertions"]["door_count"] = sum(1 for s in out if s["kind"] == "door")
    return scan


def render_overlay(
    a: np.ndarray,
    findings: list[dict[str, Any]],
    out_path: str | Path,
    *,
    dim: float = 0.45,
) -> Path:
    """Draw detected openings back onto the drawing for human review.

    The overlay is the only artefact a person can check at a glance; every
    number in it comes from the scan, so a wrong band or a stolen segment shows
    up as a mark in the wrong place rather than as a plausible number.
    """
    from PIL import ImageDraw

    img = Image.fromarray((np.asarray(a) * dim).astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(img)
    colour = {"window": (255, 60, 60), "door": (255, 200, 0), "pier": (0, 120, 255)}
    for f in findings:
        axis, (b0, b1) = f["axis"], f["band_px"]
        for s in f["segments"]:
            if s["kind"] not in colour:
                continue
            c = colour[s["kind"]]
            x0, x1 = s["start_px"], s["end_px"]
            if axis == "col":
                d.rectangle([b0 - 3, x0, b1 + 3, x1], outline=c, width=2)
            else:
                d.rectangle([x0, b0 - 3, x1, b1 + 3], outline=c, width=2)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p)
    return p


def merge_adjacent_openings(scan: dict[str, Any]) -> dict[str, Any]:
    """Two openings that touch with no pier between them are one opening.

    A zero-width pier is not a pier. sm24's third 4.8 m window came out as
    1.08 + 3.72 because the glazing symbol has an internal division; both halves
    cleared the domain minimum, so the sliver rule left them apart and the
    reading claimed two windows where the drawing has one.
    """
    segs = [dict(s) for s in scan["segments"]]
    out: list[dict[str, Any]] = []
    for s in segs:
        if (out and s["kind"] in ("window", "door") and out[-1]["kind"] == s["kind"]
                and abs(out[-1]["end_px"] - s["start_px"]) < 1e-6):
            out[-1]["end_px"] = s["end_px"]
            out[-1]["length_px"] = out[-1]["end_px"] - out[-1]["start_px"]
            out[-1]["fen_px"] += s["fen_px"]
            out[-1].setdefault("merged_with", []).append(s["length_px"])
        else:
            out.append(s)
    scan = dict(scan)
    scan["segments"] = out
    scan["assertions"] = dict(scan["assertions"])
    scan["assertions"]["window_count"] = sum(1 for s in out if s["kind"] == "window")
    scan["assertions"]["door_count"] = sum(1 for s in out if s["kind"] == "door")
    return scan


def absorb_slivers(
    scan: dict[str, Any],
    *,
    mm_per_px: float = 21.636,
    min_opening_m: float = 0.60,
    tol_m: float = 0.03,
) -> dict[str, Any]:
    """Fold sub-minimum opening fragments into the neighbour that explains them.

    A plan door contributes a few fenestration pixels at its frame, which the
    band scan sees as a 20-100 mm "opening". 0.60 m is the domain lower bound
    already used by the NARROW-OPENING process gate, so anything under it is not
    a real opening in this building type. A fragment touching a door is the
    door's frame and is absorbed; one that touches nothing is recorded in
    `slivers` and NEVER emitted as an opening -- silence here is what let the
    first cut report 37 windows when the drawing has far fewer.
    """
    segs = [dict(s) for s in scan["segments"]]
    # Apply the domain bound with the measurement's own noise budget: sm25's
    # north wall carries a real 600 mm opening, so a bare `< 0.60` test deletes
    # a true window. Anything that survives but sits within the tolerance of the
    # bound is flagged rather than trusted silently.
    min_px = (min_opening_m - tol_m) * 1000.0 / mm_per_px
    flag_px = (min_opening_m + tol_m) * 1000.0 / mm_per_px
    slivers = []
    for i, s in enumerate(segs):
        if s["kind"] not in ("window", "door"):
            continue
        if s["length_px"] >= min_px:
            if s["length_px"] <= flag_px:
                s["near_domain_bound"] = True
            continue
        # A sub-minimum fragment is never a standalone opening, so it belongs to
        # whichever opening it touches -- a door frame OR a window whose glazing
        # line dropped out for a pixel or two. Only absorbing into doors split
        # sm25's south 4000 mm window into 389 + 3602 and then deleted the 389.
        nb = [j for j in (i - 1, i + 1)
              if 0 <= j < len(segs) and segs[j]["kind"] in ("door", "window")]
        if nb:
            j = max(nb, key=lambda k: segs[k]["length_px"])
            segs[j]["start_px"] = min(segs[j]["start_px"], s["start_px"])
            segs[j]["end_px"] = max(segs[j]["end_px"], s["end_px"])
            segs[j]["length_px"] = segs[j]["end_px"] - segs[j]["start_px"]
            segs[j].setdefault("absorbed", []).append({"length_px": s["length_px"], "fen_px": s["fen_px"]})
            s["kind"] = "absorbed_frame"
        else:
            s["kind"] = "sliver"
            slivers.append({"start_px": s["start_px"], "end_px": s["end_px"],
                            "length_m": round(s["length_px"] * mm_per_px / 1000.0, 3),
                            "fen_px": s["fen_px"]})
    scan = dict(scan)
    scan["segments"] = segs
    scan["slivers"] = slivers
    scan["assertions"] = dict(scan["assertions"])
    scan["assertions"]["window_count"] = sum(1 for s in segs if s["kind"] == "window")
    scan["assertions"]["door_count"] = sum(1 for s in segs if s["kind"] == "door")
    scan["assertions"]["sliver_count"] = len(slivers)
    return scan


def find_chain_baselines(mask_annotation: np.ndarray, *, coverage: float = 0.30,
                         strip_px: int = 22, offset_px: int = 6) -> dict[str, list[dict[str, Any]]]:
    """Locate every dimension chain's baseline and a strip to read its ticks in.

    Hand-supplying strip coordinates was the last step of the calibration that
    still needed a person to look at the drawing and type numbers. A dimension
    baseline is the one annotation feature that runs across the whole sheet, so
    it is findable by projection; the tick strip is then a fixed offset off it,
    on the side facing the building.
    """
    H, W = mask_annotation.shape
    out: dict[str, list[dict[str, Any]]] = {"row": [], "col": []}
    for axis, length, other in (("row", W, H), ("col", H, W)):
        prof = mask_annotation.sum(1 if axis == "row" else 0)
        idx = np.where(prof > length * coverage)[0]
        groups: list[list[int]] = []
        for i in idx:
            if groups and i - groups[-1][-1] <= 2:
                groups[-1].append(int(i))
            else:
                groups.append([int(i)])
        for g in groups:
            base = float(np.mean(g))
            towards = 1 if base < other / 2 else -1        # step toward the sheet centre
            lo = int(base + towards * offset_px) if towards > 0 else int(base - offset_px - strip_px)
            out[axis].append({
                "baseline_px": round(base, 1),
                "strip": [lo, lo + strip_px],
                "side": "inward",
                "support_px": int(prof[g].sum()),
            })
    return out


# --------------------------------------------------------------------------
# Elevations: same ink dialect, different geometry.
#
# A plan wall is a band and its openings are runs along it; an elevation face
# has no band at all -- each opening is a closed rectangle drawn on the face.
# So the polarity principle is unchanged (measure the fenestration layer, never
# infer from gaps in the structure lines) while the extractor changes from
# "tile a band" to "connected components".
# --------------------------------------------------------------------------


def fenestration_boxes(
    a: np.ndarray,
    *,
    min_area_px: int = 40,
    nest_tol_px: int = 2,
) -> list[dict[str, Any]]:
    """Opening outlines on an elevation, one box per opening.

    Each opening is drawn as a NESTED PAIR -- an outer frame and an inner
    glazing line -- which labels as two components. Taking every component
    would double every opening, so a component fully contained in another
    (within `nest_tol_px`) is folded into it and reported as its `inner_box`
    rather than dropped, since the pair itself is evidence that this is a
    drawn opening and not a stray mark.
    """
    from scipy import ndimage

    fen = ink_families(a)["fenestration"]
    labels, count = ndimage.label(fen, structure=np.ones((3, 3), dtype=np.uint8))
    raw: list[dict[str, Any]] = []
    for comp in range(1, count + 1):
        ys, xs = np.nonzero(labels == comp)
        if len(xs) < min_area_px:
            continue
        raw.append({
            "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
            "area_px": int(len(xs)),
        })
    raw.sort(key=lambda b: -( (b["bbox_px"][2] - b["bbox_px"][0]) * (b["bbox_px"][3] - b["bbox_px"][1]) ))

    out: list[dict[str, Any]] = []
    for box in raw:
        x0, y0, x1, y1 = box["bbox_px"]
        host = None
        for kept in out:
            k0, m0, k1, m1 = kept["bbox_px"]
            if x0 >= k0 - nest_tol_px and y0 >= m0 - nest_tol_px and x1 <= k1 + nest_tol_px and y1 <= m1 + nest_tol_px:
                host = kept
                break
        if host is None:
            out.append({**box, "inner_boxes": []})
        else:
            host["inner_boxes"].append(box["bbox_px"])
    out.sort(key=lambda b: (b["bbox_px"][0], b["bbox_px"][1]))
    return out


def snap_box(
    box: dict[str, Any],
    ticks_x: list[float],
    ticks_y: list[float],
    *,
    tol_px: float = 4.0,
) -> dict[str, Any]:
    """Snap an opening box's four edges to dimension witness ticks.

    Same A x B reconciliation as the plan band scan: the ink says WHICH
    rectangle is an opening, the chains say where its edges are. An edge with
    no tick in range keeps its pixel value and is marked `snapped: false`.
    """
    x0, y0, x1, y1 = box["bbox_px"]
    edges = {}
    vals = []
    for name, v, pool in (("x0", x0, ticks_x), ("x1", x1, ticks_x),
                          ("y0", y0, ticks_y), ("y1", y1, ticks_y)):
        if pool:
            near = min(pool, key=lambda t: abs(t - v))
            if abs(near - v) <= tol_px:
                edges[name] = {"from": float(v), "to": near, "delta_px": round(near - v, 2), "snapped": True}
                vals.append(near)
                continue
            edges[name] = {"from": float(v), "to": float(v), "snapped": False,
                           "nearest_tick_px": near, "nearest_dist_px": round(abs(near - v), 2)}
        else:
            edges[name] = {"from": float(v), "to": float(v), "snapped": False}
        vals.append(float(v))
    return {**box, "snapped_px": vals, "snap": edges,
            "snap_count": sum(1 for e in edges.values() if e.get("snapped"))}
