"""Self-verification for the **v2** three-layer as-drawn product — ⛔ needs no gt.

This is the v2 port of ``checks_as_drawn.py``.  The v1 file consumes the old
``wall_bands`` shape, so every reverse/forward-ledger number in the experiment
README was produced by the v1 product — exactly the mixed-evidence problem the
second cross-family review rejected the design draft for.  This file re-asks the
same three questions of the shape that actually exists now.

⛔ It still does NOT render the product to an image and compare images: a
renderer sharing the reader's wrong assumption would agree with it.  Every
element is turned into PIXEL INDICES and the ORIGINAL image is sampled there.

⭐ What changed with the three-layer shape:

* **The ink families are discovered, not written down here.**  Which discovered
  family is structure / furniture is read out of the product's own
  ``hypotheses.family_roles.assignment`` — a perception output.  Point it at the
  wrong family and these checks go red, which is the point: the naming has to be
  falsifiable, or the model could have said anything.
* **Pairing is a hypothesis now**, so it is reconciled rather than assumed: do
  the referenced face lines exist, is any face line claimed twice, are the two
  supports disjoint.  Spacing-vs-callout is a separate, declaration-level check
  because an unexplained spacing is a *finding about the drawing's callouts*
  (sm24's undeclared 120 mm walls), not proof the observation is wrong.

Known blind spots (must stay stated, or the check gets trusted for more):
  * it cannot judge SEMANTICS beyond the role assignment — a correctly placed
    but mistyped opening reads green;
  * it cannot judge "should this stroke have been split in two" — that is the
    reading grade's endpoint criterion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from src.agent.reading.as_drawn.as_drawn_v2 import _family_masks, _profile
from src.agent.reading.as_drawn._plan_ink import vertical_runs_mask
from src.agent.reading.as_drawn._plan_ink import load_rgb

MIN_RUN_COVERAGE = 0.80   # a stroke must sit on real ink; swept in the README
SAMPLE_HALF_PX = 1        # anti-alias margin around a face line's own columns


def _strip(face: dict) -> tuple[int, int]:
    c0, c1 = face["support_cols_px"]
    return max(0, c0 - SAMPLE_HALF_PX), c1 + SAMPLE_HALF_PX


def _sample_runs(structure: np.ndarray, *, axis: str, face: dict,
                 hole_floor_px: int = 6) -> list[dict[str, Any]]:
    """Ink under each claimed run -- as an average AND as its longest HOLE.

    ⚠️ 2026-08-24, fourth cross-family review (GLM): the average alone let a
    product draw straight through a real 0.6 m window and still pass, because a
    28 px hole inside a 190 px run only pulls the mean to 0.85 -- above the 0.80
    threshold.  A local hole is a local fact and must be reported as one.
    """
    lo_p, hi_p = _strip(face)
    out = []
    for a, b in face["runs_px"]:
        a, b = int(min(a, b)), int(max(a, b))
        strip = structure[a:b, lo_p:hi_p] if axis == "col" else structure[lo_p:hi_p, a:b]
        along = strip.any(axis=1 if axis == "col" else 0)
        hole = cur = 0
        for v in along:
            cur = 0 if v else cur + 1
            hole = max(hole, cur)
        out.append({"run_px": [a, b], "length_px": b - a,
                    "ink_coverage": round(float(along.mean()) if along.size else 0.0, 4),
                    "longest_hole_px": int(hole),
                    "hole_floor_px": hole_floor_px})
    return out


# ------------------------------------------------------------------ checks ---
def check_reverse(doc: dict, structure: np.ndarray) -> dict:
    """MULTI-DRAW: every stretch the product claims must sit on real ink."""
    rows, worst, n = [], 1.0, 0
    for f in doc["observations"]["face_lines"]:
        for s in _sample_runs(structure, axis=f["axis"], face=f):
            n += 1
            s["face"] = f["id"]
            worst = min(worst, s["ink_coverage"])
            if s["ink_coverage"] < MIN_RUN_COVERAGE or s["longest_hole_px"] > s["hole_floor_px"]:
                rows.append(s)
    return {"check": "reverse_ledger_no_phantom_ink", "threshold": MIN_RUN_COVERAGE,
            "runs_sampled": n, "worst_coverage": round(worst, 4),
            "longest_hole_px": max((r["longest_hole_px"] for r in rows), default=0),
            "violations": rows[:40], "violation_count": len(rows),
            "status": "red" if rows else "green"}


def check_self_consistency(doc: dict) -> dict:
    """⭐ Every metre the product states must be recomputable from its own pixels.

    Two false-greens motivated this.  (1) A neuter that rewrote ``edges_m`` to
    claim every thin stroke is a 0.24 m filled band was invisible to both other
    checks, because both recompute the edges and therefore never read the claim
    -- an unread field is a place to hide.  (2) Two face lines resting on the
    SAME ink ("one stroke read twice") is what the second review's 0.2 mm twin
    exploited; disjoint support is a property of the extractor, so state it as a
    check rather than trusting the constructor to keep being right.
    """
    mm_px = doc["observations"]["calibration"]["mm_per_px"]
    m_px = mm_px / 1000.0
    tol = m_px * 1.5          # one and a half pixels
    fl = doc["observations"]["face_lines"]
    bad_edges, bad_runs, overlaps = [], [], []
    for f in fl:
        c0, c1 = f["support_cols_px"]
        # ⚠️ Recompute along the producer's OWN affine map -- pos_m = w(pos_px) --
        # ⛔ never as "pos_m +- half the support width".  That symmetric form
        # assumes the centre of the ink is the centre of the support columns; it
        # is not (pos_px is the ink centroid) and on sm24's 9 px filled bands the
        # difference reached 1.48 px, i.e. this gate nearly false-reddened an
        # honest product on an assumption the producer never made.
        sign = 1.0 if f["constant_world_axis"] == "x" else -1.0
        want = sorted((f["pos_m"] + sign * (c0 - f["pos_px"]) * m_px,
                       f["pos_m"] + sign * (c1 - f["pos_px"]) * m_px))
        got = f.get("edges_m")
        if got and max(abs(got[0] - want[0]), abs(got[1] - want[1])) > tol:
            bad_edges.append({"face": f["id"], "stated_m": [round(v, 4) for v in got],
                              "recomputed_m": [round(v, 4) for v in want]})
        for (pa, pb), (ma, mb) in zip(f["runs_px"], f["runs_m"]):
            if abs(abs(pb - pa) * m_px - abs(mb - ma)) > tol:
                bad_runs.append({"face": f["id"], "run_px": [pa, pb],
                                 "stated_len_m": round(abs(mb - ma), 4),
                                 "recomputed_len_m": round(abs(pb - pa) * m_px, 4)})
    for i, A in enumerate(fl):
        for B in fl[i + 1:]:
            if A["axis"] != B["axis"]:
                continue
            (a0, a1), (b0, b1) = A["support_cols_px"], B["support_cols_px"]
            if min(a1, b1) > max(a0, b0):
                overlaps.append({"faces": [A["id"], B["id"]],
                                 "support_cols_px": [[a0, a1], [b0, b1]]})
    bad = len(bad_edges) + len(bad_runs) + len(overlaps)
    return {"check": "observations_recomputable_from_own_pixels",
            "tolerance_m": round(tol, 4), "stated_edges_wrong": bad_edges[:20],
            "stated_runs_wrong": bad_runs[:20],
            "face_lines_sharing_ink": overlaps[:20],
            "violation_count": bad, "status": "red" if bad else "green"}


def check_gaps_recomputable(doc: dict, masks: dict, tol_px: float = 1.5) -> dict:
    """⭐ Every number describing a GAP must be recomputable from the original image.

    Added 2026-08-24 after the THIRD cross-family review (sol) walked straight
    through everything else with this chain:

      1. really fail to read 1.2 m of a wall (both faces of L012+L013),
      2. call that stretch a doorway by writing a fabricated
         ``gaps[*].ink_by_family[<opening family>]`` profile,
      3. the gt-side reconstruction believes the fabricated profile and bridges
         the hole -> the score goes back from 89.2 to the honest 94.6, and all
         six gt-free gates stayed green.

    The earlier fix only made the THRESHOLD immune to one stray pixel; the
    QUANTITY itself was still whatever the product said it was.  ⛔ A check that
    trusts a number it could recompute is not a check -- the same lesson as
    ``edges_m``, one level deeper, and I had to be shown it twice.

    ⚠️ The recomputation must mirror the producer's own axis handling: for a
    ``col`` face line the masks are used as-is, for a ``row`` one they are
    transposed (that is what ``_ink_groups`` does).
    """
    m_px = doc["observations"]["calibration"]["mm_per_px"] / 1000.0
    tol_m = m_px * tol_px
    bad_profile, bad_span, n = [], [], 0
    by_axis = {"col": masks, "row": {k: v.T for k, v in masks.items()}}
    for f in doc["observations"]["face_lines"]:
        fam_t = by_axis[f["axis"]]
        c0, c1 = f["support_cols_px"]
        for g in f.get("gaps", []):
            n += 1
            if g.get("lo_px") is None or g.get("hi_px") is None:
                # ⛔ A gap with no pixel indices cannot be recomputed at all --
                # that is a fabricated claim, not a measurement.  (The third
                # review's cheat produced exactly this shape.)
                bad_profile.append({"face": f["id"], "gap_px": None,
                                    "problem": "gap carries no pixel indices; "
                                               "nothing about it can be recomputed"})
                continue
            lo, hi = int(g["lo_px"]), int(g["hi_px"])
            if g.get("len_px") != hi - lo:
                bad_span.append({"face": f["id"], "gap_px": [lo, hi],
                                 "stated_len_px": g.get("len_px"),
                                 "recomputed_len_px": hi - lo})
            if g.get("span_m") is not None:
                stated = abs(g["span_m"][1] - g["span_m"][0])
                if abs(stated - (hi - lo) * m_px) > tol_m:
                    bad_span.append({"face": f["id"], "gap_px": [lo, hi],
                                     "stated_len_m": round(stated, 4),
                                     "recomputed_len_m": round((hi - lo) * m_px, 4)})
            for fid, stated_prof in (g.get("ink_by_family") or {}).items():
                if fid not in fam_t:
                    bad_profile.append({"face": f["id"], "gap_px": [lo, hi],
                                        "family": fid, "problem": "no such ink family"})
                    continue
                want = _profile(fam_t[fid], lo, hi, c0, c1)
                for key in ("on_line", "span_ratio", "nearest_px"):
                    if abs((stated_prof.get(key) or 0) - (want.get(key) or 0)) > 1e-4:
                        bad_profile.append({"face": f["id"], "gap_px": [lo, hi],
                                            "family": fid, "field": key,
                                            "stated": stated_prof.get(key),
                                            "recomputed": want.get(key)})
                        break
    # ⭐ 2026-08-24 (GLM Finding 2): the naming gate reads
    # ``hypotheses.opening_candidates[*].ink_by_family`` -- a SECOND copy of the
    # same measurement that nothing recomputed.  Measured A/B: naming a
    # zero-ink 2.185 m gap a door is caught when the copy stays honest, and
    # passes every gate once the copy is fabricated.  Same data, same function.
    faces = {f["id"]: f for f in doc["observations"]["face_lines"]}
    bad_candidates = []
    for c in doc["hypotheses"].get("opening_candidates", []) or []:
        f = faces.get(c.get("face_line"))
        if f is None:
            bad_candidates.append({"candidate": c.get("id"), "problem": "unknown face line"})
            continue
        gs = f.get("gaps") or []
        gi = c.get("gap_index")
        if not isinstance(gi, int) or gi >= len(gs):
            bad_candidates.append({"candidate": c.get("id"), "problem": "gap_index out of range"})
            continue
        g = gs[gi]
        for fid, stated in (c.get("ink_by_family") or {}).items():
            truth = (g.get("ink_by_family") or {}).get(fid) or {}
            for key in ("on_line", "span_ratio", "nearest_px"):
                if abs((stated.get(key) or 0) - (truth.get(key) or 0)) > 1e-4:
                    bad_candidates.append({"candidate": c.get("id"), "family": fid,
                                           "field": key, "stated": stated.get(key),
                                           "recomputed": truth.get(key)})
                    break

    bad = len(bad_profile) + len(bad_span) + len(bad_candidates)
    return {"check": "gap_evidence_recomputable_from_original_image",
            "fabricated_candidate_profile": bad_candidates[:20],
            "gaps_checked": n, "tolerance_px": tol_px,
            "fabricated_ink_profile": bad_profile[:20],
            "inconsistent_span": bad_span[:20],
            "violation_count": bad, "status": "red" if bad else "green"}


def check_face_span_accounted(doc: dict, masks: dict, roles: dict,
                              floor_px: int = 3) -> dict:
    """⭐ Inside a face line's own span, every inked row must be a run or a gap.

    Added 2026-08-24 with the gap-recompute gate.  The third cross-family review
    deleted 1.2 m from the middle of two real wall faces and declared nothing:
    the reverse ledger only samples the runs that remain, and the forward ledger
    measures unclaimed ink as a fraction of the WHOLE drawing (2.77% -> 3.72%
    under an 8% threshold), so a localised miss of a whole wall stretch was
    invisible to all six gates.

    ⛔ Threshold-free by construction: the product itself asserts a line runs
    from A to B, so ink under its own strip that it neither claims (run) nor
    accounts for (gap) is a stretch it dropped.  ``floor_px`` only absorbs
    anti-alias specks.

    ⚠️ DECLARED BLIND SPOT (swept 2026-08-24 against GLM's ``skip_unscored_tails``):
    on sm25 2F that cheat trims 1-2 px off each line end -- 1.16 m in total -- and
    NO floor separates it from the honest product: at floor 2 the cheat shows 14
    violations but the honest product shows 1; at floor 3 and above both are
    clean.  So under-reading by a pixel per line end is below this instrument's
    resolution.  It also costs ~0 on the grade (98.1 either way), so nothing is
    being bought by it -- but the limit is stated rather than hidden.
    """
    st = masks[roles["structure"]]
    m_px = doc["observations"]["calibration"]["mm_per_px"] / 1000.0
    by_axis = {"col": st, "row": st.T}
    rows, worst = [], 0
    for f in doc["observations"]["face_lines"]:
        runs = [(int(min(r)), int(max(r))) for r in f["runs_px"]]
        if not runs:
            continue
        m = by_axis[f["axis"]]
        c0, c1 = f["support_cols_px"]
        # ⚠️ An earlier version walked OUTWARD from the first/last run while ink
        # continued, to catch tail trimming.  On sm24's filled-band dialect that
        # walk runs straight into every perpendicular wall at a T-junction and
        # false-reddened 24 honest face lines.  Tail trimming is now caught by
        # ``check_runs_match_the_strip`` instead -- by recomputing the runs with
        # the producer's own extractor, which needs no outward guess at all.
        lo, hi = min(a for a, _ in runs), max(b for _, b in runs)
        inked = m[lo:hi, max(0, c0 - SAMPLE_HALF_PX):c1 + SAMPLE_HALF_PX].any(axis=1)
        accounted = [False] * (hi - lo)
        for a, b in runs:
            for i in range(max(lo, a) - lo, min(hi, b) - lo):
                accounted[i] = True
        for g in f.get("gaps", []):
            if g.get("lo_px") is None:
                continue
            for i in range(max(lo, int(g["lo_px"])) - lo, min(hi, int(g["hi_px"])) - lo):
                accounted[i] = True
        start = None
        for i in range(hi - lo):
            unaccounted = bool(inked[i]) and not accounted[i]
            if unaccounted and start is None:
                start = i
            elif not unaccounted and start is not None:
                if i - start >= floor_px:
                    rows.append({"face": f["id"], "px": [lo + start, lo + i],
                                 "length_m": round((i - start) * m_px, 3)})
                worst = max(worst, i - start)
                start = None
        if start is not None and (hi - lo) - start >= floor_px:
            rows.append({"face": f["id"], "px": [lo + start, hi],
                         "length_m": round(((hi - lo) - start) * m_px, 3)})
            worst = max(worst, (hi - lo) - start)
    return {"check": "face_span_fully_accounted_by_runs_or_gaps",
            "floor_px": floor_px, "longest_unaccounted_px": worst,
            "longest_unaccounted_m": round(worst * m_px, 3),
            "violations": sorted(rows, key=lambda r: -r["length_m"])[:20],
            "violation_count": len(rows), "status": "red" if rows else "green"}


def check_runs_match_the_strip(doc: dict, masks: dict, roles: dict, cfg: dict) -> dict:
    """⭐ Are the declared runs the ones the strip actually carries?

    Mirrors the producer end-to-end: re-run ``_ink_groups`` on the original image
    with this drawing's own settings and compare, group by group, with what the
    product declares.  ⛔ No threshold, no outward guessing.

    This replaces an outward-walk heuristic that caught tail trimming on one
    dialect and false-reddened 24 honest face lines on the other (sm24's filled
    bands run into a perpendicular wall at every T-junction).

    ⚠️ Stated limit: for a DETERMINISTIC observation layer this is close to a
    tautology -- it re-runs the extractor.  Its value is for the layer this batch
    is heading to, where the observations arrive from somewhere else (a model, an
    outside product, a hand-built fixture); there it is the only thing that ties
    the declared runs back to the pixels.  Differences are REPORTED per face
    line, not summarised away.
    """
    from src.agent.reading.as_drawn.as_drawn_v2 import _ink_groups
    st = masks[roles["structure"]].copy()
    r0, r1, c0b, c1b = doc["declarations"]["drawing_box_px"]
    st[:r0, :] = False
    st[r1:, :] = False
    st[:, :c0b] = False
    st[:, c1b:] = False
    truth: dict[tuple[str, int, int], list] = {}
    for axis in ("col", "row"):
        for g in _ink_groups(st, masks, axis=axis,
                             min_run_px=cfg.get("min_run_px", 14),
                             min_support=cfg.get("min_support", 10)):
            truth[(axis, g["support_cols_px"][0], g["support_cols_px"][1])] = g["runs_px"]
    rows, matched = [], 0
    for f in doc["observations"]["face_lines"]:
        key = (f["axis"], f["support_cols_px"][0], f["support_cols_px"][1])
        want = truth.get(key)
        if want is None:
            rows.append({"face": f["id"], "problem": "no ink group with these support columns",
                         "support_cols_px": f["support_cols_px"]})
            continue
        matched += 1
        got = [[int(a), int(b)] for a, b in f["runs_px"]]
        if [[int(a), int(b)] for a, b in want] != got:
            rows.append({"face": f["id"], "declared_runs": got[:6],
                         "recomputed_runs": [[int(a), int(b)] for a, b in want][:6],
                         "declared_total_px": sum(b - a for a, b in got),
                         "recomputed_total_px": sum(int(b) - int(a) for a, b in want)})
    return {"check": "runs_match_the_strip", "face_lines": len(doc["observations"]["face_lines"]),
            "matched_a_recomputed_group": matched, "violations": rows[:20],
            "violation_count": len(rows), "status": "red" if rows else "green"}


def check_support_strip_is_one_stroke(doc: dict, masks: dict, roles: dict, cfg: dict, *,
                                      min_run_px: int | None = None,
                                      min_support: int | None = None) -> dict:
    """⭐ Is the support strip a face line declares actually ONE stroke of ink?

    Added 2026-08-24 after the FIFTH cross-family review (GLM) built
    ``band_collapse``: two real ink lines of a wall declared as ONE filled band,
    with support columns = the two real strokes, runs = their union, every metre
    recomputed by the producer's own affine, and every gap profile honestly
    re-measured over the widened strip.  ⛔ NOTHING in that product is fabricated,
    and it beat the honest product on two criteria (C2 98.7 > 98.6, C4 0.473 <
    0.722) with all eight gates green and the gt-side score unchanged.

    Why every other gate was blind: they all anchor on the aperture the PRODUCT
    chose.  ``edges_m`` is recomputed from the product's own support columns --
    declare a wider strip and the recomputation simply agrees.  The reverse
    ledger asks whether ANY column of the strip has ink.  Nothing asked the one
    question that separates a real filled band from two lines with white space
    between them: **is the strip one stroke?**

    ⛔ Threshold-free: it mirrors ``_ink_groups``'s OWN grouping rule (a column
    counts when it carries >= ``min_support`` rows of a run at least
    ``min_run_px`` long; groups split on a gap wider than 1 px).  Measured:
    honest sm25 1F all 49 face lines -> 1 group; honest sm24 all 98 -> 1 group
    (its four genuine solid bands included); every collapsed band -> 2.
    """
    # ⚠️ mirror the producer exactly: it groups on the structure mask CLIPPED to
    # the drawing box.  Without the clip, ink outside the plan frame joins the
    # strip and an honest face line reads as two groups.
    # ⚠️ read the SAME cfg keys the producer reads (sixth review note): hardcoding
    # the defaults is equivalent today because no cfg overrides them, but it would
    # fork silently the first time one does.
    min_run_px = cfg.get("min_run_px", 14) if min_run_px is None else min_run_px
    min_support = cfg.get("min_support", 10) if min_support is None else min_support
    st = masks[roles["structure"]].copy()
    r0, r1, c0b, c1b = doc["declarations"]["drawing_box_px"]
    st[:r0, :] = False
    st[r1:, :] = False
    st[:, :c0b] = False
    st[:, c1b:] = False
    by_axis = {"col": st, "row": st.T}
    rows = []
    hist: dict[int, int] = {}
    for f in doc["observations"]["face_lines"]:
        m = by_axis[f["axis"]]
        c0, c1 = f["support_cols_px"]
        keep = vertical_runs_mask(m, min_run_px)
        support = keep[:, c0:c1].sum(0)
        cols = [int(c) for c in np.flatnonzero(support >= min_support)]
        groups = 0
        prev = None
        for c in cols:
            if prev is None or c - prev > 1:
                groups += 1
            prev = c
        hist[groups] = hist.get(groups, 0) + 1
        if groups != 1:
            rows.append({"face": f["id"], "support_cols_px": [c0, c1],
                         "ink_column_groups": groups,
                         "why": ("a wall face is ONE stroke; two groups means this "
                                 "'band' spans two separate strokes with white space "
                                 "between them" if groups > 1 else
                                 "no column of the declared strip carries a stroke")})
    return {"check": "support_strip_is_one_stroke",
            "mirrors": "_ink_groups(min_run_px=%d, min_support=%d, gap>1px)" % (min_run_px, min_support),
            "ink_column_groups_histogram": {str(k): v for k, v in sorted(hist.items())},
            "violations": rows[:20], "violation_count": len(rows),
            "status": "red" if rows else "green"}


def check_opening_role_matches_ink(doc: dict, min_share: float = 0.50) -> dict:
    """⭐ Is the family called "openings" the one whose ink sits IN the wall gaps?

    Added 2026-08-24 after a perception neuter that simply swapped the openings
    and furniture role names passed all four other gt-free gates while costing
    43 points against gt (94.6 -> 51.4).  A naming nobody can check is a naming
    the model could have invented.

    ⛔ Not a semantic rule about colour: it is where the ink physically lies.
    Windows and doors are drawn INSIDE the breaks of a wall; desks are not.
    Both quantities are already measured per gap in the observation layer, so a
    consumer can recompute this from the product alone.
    """
    roles = doc["hypotheses"]["family_roles"]["assignment"]
    tot: dict[str, int] = {}
    n = 0
    for f in doc["observations"]["face_lines"]:
        for g in f.get("gaps", []):
            n += 1
            for fid, prof in g.get("ink_by_family", {}).items():
                tot[fid] = tot.get(fid, 0) + prof.get("on_line", 0)
    grand = sum(tot.values())
    named = roles.get("fenestration")
    out = {"check": "opening_role_matches_where_the_ink_sits", "gaps_measured": n,
           "ink_in_gaps_by_family": dict(sorted(tot.items())),
           "named_opening_family": named, "min_share": min_share}
    if named is None:
        out.update({"status": "degraded", "violation_count": 0,
                    "degraded_reason": "no family carries the openings role "
                                       "(monochrome drawings land here, F-69's rule)"})
        return out
    share = tot.get(named, 0) / max(1, grand)
    top = max(tot, key=tot.get) if tot else None
    out.update({"share_of_gap_ink": round(share, 4), "family_with_most_gap_ink": top,
                "violation_count": 0 if (share >= min_share and named == top) else 1,
                "status": "green" if (share >= min_share and named == top) else "red"})
    return out


def check_opening_naming_supported(doc: dict) -> dict:
    """⭐ Every stretch perception CALLS an opening must have opening ink on it.

    Added 2026-08-24 with F-87.  Measured first: naming every blank stretch a
    door costs NOTHING on the gt-side reconstruction (94.6 either way), because
    bridging only ever helps coverage.  So over-claiming openings is free there
    and has to be caught here, where the ink can be recomputed from the image.

    ⛔ Deliberately asymmetric, and the asymmetry is stated:
      * "you called it an opening but there is no opening ink"  -> RED. Provable.
      * "you called it not-an-opening but there IS opening ink" -> reported as a
        CONTRADICTION, not scored: a door leaf drawn across a wall junction can
        put opening ink on a stretch that is not itself an opening, so absence of
        an opening is not provable from ink alone.
    """
    types = doc["hypotheses"].get("opening_types")
    cands = {c["id"]: c for c in doc["hypotheses"].get("opening_candidates", [])}
    if types is None:
        return {"check": "opening_naming_supported_by_ink", "status": "degraded",
                "violation_count": 0, "candidates": len(cands),
                "degraded_reason": "perception supplied no opening_types; ⛔ the scorer "
                                   "bridges nothing (F-87) -- naming is perception's call"}
    fen = doc["hypotheses"]["family_roles"]["assignment"].get("fenestration")
    dangling = [k for k in types if k not in cands]
    unnamed = [k for k in cands if k not in types]
    no_ink, contradictions = [], []
    for k, kind in types.items():
        c = cands.get(k)
        if c is None:
            continue
        prof = (c.get("ink_by_family") or {}).get(fen) or {}
        ink = (prof.get("on_line") or 0) + (prof.get("span_ratio") or 0.0)
        if kind in ("door", "window") and ink <= 0:
            no_ink.append({"candidate": k, "named": kind, "len_m": c["len_m"],
                           "opening_ink_on_line": prof.get("on_line"),
                           "span_ratio": prof.get("span_ratio")})
        if kind in ("not_opening", "passage") and (prof.get("span_ratio") or 0.0) >= 0.5:
            contradictions.append({"candidate": k, "len_m": c["len_m"],
                                   "span_ratio": prof.get("span_ratio")})
    bad = len(dangling) + len(unnamed) + len(no_ink)
    return {"check": "opening_naming_supported_by_ink",
            "candidates": len(cands), "named": len(types),
            "by_kind": {k: sum(1 for v in types.values() if v == k)
                        for k in sorted(set(types.values()))},
            "named_but_no_opening_ink": no_ink[:20],
            "dangling_reference": dangling[:20], "unnamed_candidates": unnamed[:20],
            # ⛔ reported, NOT scored -- see the docstring
            "contradiction_called_not_opening_but_ink_is_there": contradictions[:20],
            "violation_count": bad, "status": "red" if bad else "green"}


def check_pair_reconciliation(doc: dict) -> dict:
    """RECONCILIATION of the model's pairing hypothesis against the observations.

    ⛔ Not "is this pairing right" — that is the grade's job.  Only the three
    things code can settle: do the cited observations exist, is one observation
    sold twice, and do the two faces rest on disjoint ink.
    """
    faces = {f["id"]: f for f in doc["observations"]["face_lines"]}
    pairs = doc["hypotheses"].get("pairs")
    if pairs is None:
        # ⛔ No selection to reconcile. A gate with nothing to check is NOT green:
        # green here would read as "the pairing is sound" (F-69's rule).
        return {"check": "pair_hypothesis_reconciles_with_observations",
                "status": "degraded", "violation_count": 0,
                "pairs": None,
                "pair_candidates": len(doc["hypotheses"].get("pair_candidates", [])),
                "degraded_reason": doc["hypotheses"].get(
                    "pairs_note", "no wall pairing selected; nothing to reconcile")}
    dangling, reused, overlapping = [], [], []
    claimed: dict[str, list[str]] = {}
    for i, p in enumerate(pairs):
        pid = p.get("id", f"P{i}")
        a, b = p.get("face_a"), p.get("face_b")
        for ref in (a, b):
            if ref not in faces:
                dangling.append({"pair": pid, "missing_face": ref})
            else:
                claimed.setdefault(ref, []).append(pid)
        if a in faces and b in faces:
            (a0, a1), (b0, b1) = faces[a]["support_cols_px"], faces[b]["support_cols_px"]
            if min(a1, b1) > max(a0, b0):
                overlapping.append({"pair": pid, "faces": [a, b],
                                    "support_cols_px": [[a0, a1], [b0, b1]]})
    for ref, ps in sorted(claimed.items()):
        if len(ps) > 1:
            reused.append({"face": ref, "claimed_by": ps})
    # ⭐ COMPLETENESS: a face line perception says nothing about is not "fine",
    # it is unaccounted for.  Silence is how the five callout-text strokes got
    # quietly paired into walls before anybody looked.
    # ⛔ 2026-08-24, third review: the buckets' reason strings were never read at
    # all -- only their keys went into a set.  "not a wall, and here is what it
    # is" was documented but ungated.  This is a SYNTACTIC requirement only, and
    # is labelled as such: it forces a statement, it cannot judge one.
    reasonless = [f"{b}:{k}" for b in ("non_wall_face_lines", "unpaired_wall_faces",
                                       "ambiguous_face_lines", "solid_band_walls")
                  for k, v in (doc["hypotheses"].get(b) or {}).items()
                  if not (isinstance(v, str) and v.strip())]
    non_wall = set(doc["hypotheses"].get("non_wall_face_lines", {}))
    lone = set(doc["hypotheses"].get("unpaired_wall_faces", {}))
    band = set(doc["hypotheses"].get("solid_band_walls", {}))
    ambiguous = set(doc["hypotheses"].get("ambiguous_face_lines", {}))
    accounted = ({x for p in pairs for x in (p.get("face_a"), p.get("face_b"))}
                 | non_wall | lone | band | ambiguous)
    unaccounted = sorted(set(faces) - accounted)
    # ⚠️ A reference the producer could not resolve is dropped before it reaches
    # this list, so the dropped-on-the-floor case is only visible in the status
    # the producer itself recorded.  Reading only the surviving rows made this
    # gate green on a perception that cited a face line that does not exist.
    status = doc["hypotheses"].get("pairs_status")
    bad = (len(dangling) + len(reused) + len(overlapping) + len(unaccounted)
           + len(reasonless)
           + (0 if status in (None, "SELECTED") else 1))
    # ⭐ ZERO-WALL (threshold-free, F-69's rule applied to perception itself).
    # The third review walked through by declaring ALL 49 face lines not-a-wall
    # -- six gates green, gt unchanged -- and again by declaring them all
    # ambiguous.  A drawing whose reading positively identifies NO wall at all is
    # a confident zero, and that must be loud whatever the reason.
    # ⚠️ A GRADED abstention budget ("no more than X% ambiguous") is a domain
    # parameter and needs a signature + cold-start data; ⛔ it is deliberately
    # NOT invented here, because the only samples available were produced by a
    # perception that had already seen the gt-side scores.
    positive = {x for p in pairs for x in (p.get("face_a"), p.get("face_b"))} | band
    coverage = {"face_lines": len(faces), "in_a_wall": len(positive),
                "non_wall": len(non_wall), "ambiguous": len(ambiguous),
                "unpaired_wall_face": len(lone)}
    if not positive and faces:
        return {"check": "pair_hypothesis_reconciles_with_observations",
                "status": "red", "violation_count": 1, "coverage": coverage,
                "zero_wall": ("perception identified no wall at all on a drawing with "
                              f"{len(faces)} face lines -- a confident zero")}
    if not bad and ambiguous:
        # ⛔ NOT green: perception declined on some lines, and a reader must see
        # that rather than infer "everything checked out".
        return {"check": "pair_hypothesis_reconciles_with_observations",
                "status": "degraded", "violation_count": 0, "pairs": len(pairs),
                "coverage": coverage, "declared_ambiguous": sorted(ambiguous),
                "degraded_reason": f"perception declined on {len(ambiguous)} of "
                                   f"{len(faces)} face lines; the rest reconcile"}
    return {"check": "pair_hypothesis_reconciles_with_observations",
            "pairs": len(pairs), "coverage": coverage,
            "status_from_product": doc["hypotheses"].get("pairs_status"),
            "face_lines_unaccounted_for": unaccounted,
            "bucket_entries_without_a_reason": reasonless[:20],
            "declared_non_wall": sorted(non_wall),
            "declared_unpaired_wall_face": sorted(lone),
            "declared_solid_band_wall": sorted(band),
            "declared_ambiguous": sorted(ambiguous),
            "dangling_reference": dangling,
            "face_claimed_twice": reused, "supports_overlap": overlapping,
            "violation_count": bad, "status": "red" if bad else "green"}


def check_spacing_declared(doc: dict) -> dict:
    """DECLARATION cross-check: is each paired spacing explicable by a callout?

    ⛔ Does NOT snap — snapping is a conversion and belongs to 1_correction.  An
    inexplicable spacing is NAMED here instead of travelling downstream disguised
    as a thickness (F-78).  ⚠️ A red here can mean the drawing's callouts are
    incomplete (sm24's undeclared 120 mm walls), not that the measurement is wrong.
    """
    declared = [t / 1000.0 for t in doc["declarations"].get("thickness_callouts_mm", [])]
    if doc["hypotheses"].get("pairs") is None:
        return {"check": "pair_spacing_explicable_by_callouts", "status": "degraded",
                "violation_count": 0, "declared_mm":
                    doc["declarations"].get("thickness_callouts_mm", []),
                "degraded_reason": "no wall pairing selected; spacing has no owner yet"}
    rows = []
    for i, p in enumerate(doc["hypotheses"].get("pairs") or []):
        s = p.get("spacing_m")
        if s is None:
            continue
        if not declared:
            rows.append({"pair": p.get("id", f"P{i}"), "spacing_m": s,
                         "nearest_declared_m": None})
            continue
        nearest = min(declared, key=lambda t: abs(s - t))
        gap = abs(s - nearest)
        tol = max(0.30 * nearest, 0.02)
        if gap > tol:
            rows.append({"pair": p.get("id", f"P{i}"), "spacing_m": round(s, 4),
                         "nearest_declared_m": nearest, "excess_m": round(gap, 4),
                         "tolerance_m": round(tol, 4)})
    return {"check": "pair_spacing_explicable_by_callouts",
            "declared_mm": doc["declarations"].get("thickness_callouts_mm", []),
            "violations": rows, "violation_count": len(rows),
            "status": "red" if rows else "green"}


def check_forward(doc: dict, masks: dict, roles: dict, box: list[int],
                  min_component_px: int, max_unclaimed_pct: float = 8.0) -> dict:
    """MISSED-DRAW: structural ink the product never claimed."""
    from scipy import ndimage

    r0, r1, c0, c1 = box
    st = masks[roles["structure"]].copy()
    st[:r0, :] = False
    st[r1:, :] = False
    st[:, :c0] = False
    st[:, c1:] = False

    claimed = np.zeros_like(st)
    for f in doc["observations"]["face_lines"]:
        lo_p, hi_p = _strip(f)
        for a, b in f["runs_px"]:
            a, b = int(min(a, b)), int(max(a, b))
            if f["axis"] == "col":
                claimed[a:b, lo_p:hi_p] = True
            else:
                claimed[lo_p:hi_p, a:b] = True
    residue = st & ~claimed
    lab, n = ndimage.label(residue)
    sizes = ndimage.sum(residue, lab, range(1, n + 1))
    blobs = []
    for i, s in enumerate(sizes):
        if s < min_component_px:
            continue
        ys, xs = np.where(lab == i + 1)
        blobs.append({"px": int(s), "bbox_px": [int(xs.min()), int(ys.min()),
                                                int(xs.max()), int(ys.max())]})
    blobs.sort(key=lambda d: -d["px"])

    ink = sum(m.sum() for m in masks.values())
    fur_fid = roles.get("furniture")
    fur_pct = 100.0 * masks[fur_fid].sum() / max(1, ink) if fur_fid in masks else 0.0
    out = {"check": "forward_ledger_structural_ink_claimed",
           "structure_family": roles["structure"],
           "structure_px_in_box": int(st.sum()), "unclaimed_px": int(residue.sum()),
           "unclaimed_pct": round(100.0 * residue.sum() / max(1, st.sum()), 2),
           "component_floor_px": min_component_px,
           "large_unclaimed_components": blobs[:20],
           "furniture_family": fur_fid,
           "furniture_layer_pct_of_ink": round(fur_pct, 2)}
    # DIALECT GATING (same rule as the monochrome polarity downgrade, F-69):
    # the residue only measures wall coverage when furniture has its own layer.
    if fur_fid in masks and fur_pct >= 1.0:
        out["status"] = "green" if out["unclaimed_pct"] <= max_unclaimed_pct else "red"
        out["threshold_pct"] = max_unclaimed_pct
    else:
        out["status"] = "degraded"
        out["degraded_reason"] = (
            "no separate furniture ink family in this drawing's role assignment: the "
            "structural family carries furniture too, so unclaimed structural ink is "
            "NOT a wall-coverage measure here")
    return out


# ----------------------------------------------------------------- neuters ---
def _mutate(doc: dict, cfg: dict, kind: str) -> str:
    fl = doc["observations"]["face_lines"]
    if kind == "merge_runs":
        # the pre-2026-08-23 shape: every face line collapsed to [min, max]
        for f in fl:
            rs = f["runs_px"]
            f["runs_px"] = [[min(r[0] for r in rs), max(r[1] for r in rs)]]
        return "MUTATED: face-line runs collapsed to [min,max]"
    if kind == "extend_runs_full":
        # ⭐ The cheat the gt-side check cannot see: claim every face line is
        # drawn edge to edge, so every gt target is fully covered for free.
        r0, r1, c0, c1 = doc["declarations"]["drawing_box_px"]
        for f in fl:
            f["runs_px"] = [[r0, r1] if f["axis"] == "col" else [c0, c1]]
        return "MUTATED: every face line extended across the whole drawing box"
    if kind == "fabricate_pair_from_midline":
        # ⭐ "collapse to midline, then regenerate two faces at ±t/2" — the shape
        # a legacy centreline representation produces when asked for faces.
        mm_px = doc["observations"]["calibration"]["mm_per_px"]
        t_mm = max(doc["declarations"].get("thickness_callouts_mm") or [240])
        half = (t_mm / mm_px) / 2.0
        by_id = {f["id"]: f for f in fl}
        src = doc["hypotheses"].get("pairs")
        if src is None:      # no selection yet: use the callout-matching candidates
            src = [c for c in doc["hypotheses"].get("pair_candidates", [])
                   if c.get("matched_declared_mm")]
        keep, n = [], 0
        drop = set()
        for p in src:
            a, b = by_id.get(p.get("face_a")), by_id.get(p.get("face_b"))
            if not a or not b:
                continue
            mid = (a["pos_px"] + b["pos_px"]) / 2.0
            for sign, src in ((-1, a), (+1, b)):
                c = json.loads(json.dumps(a))          # both faces cloned from ONE
                c["id"] = src["id"]
                c["pos_px"] = mid + sign * half
                c["support_cols_px"] = [int(round(mid + sign * half)),
                                        int(round(mid + sign * half)) + 1]
                keep.append(c)
                n += 1
            drop |= {a["id"], b["id"]}
        doc["observations"]["face_lines"] = [f for f in fl if f["id"] not in drop] + keep
        return (f"MUTATED: {n} faces regenerated at +-{t_mm/2:.0f} mm around each pair's "
                "midline, both cloned from face_a's runs")
    if kind == "duplicate_face":
        # the twin 0.2 mm away, carried over from the gt-side neuter set so one
        # fixture can be scored by both checks
        twins = []
        for f in fl:
            t = json.loads(json.dumps(f))
            t["id"] = f["id"] + "_TWIN"
            t["pos_px"] = f["pos_px"] + 0.01
            twins.append(t)
        doc["observations"]["face_lines"] = fl + twins
        return f"MUTATED: {len(twins)} twin face lines added on the same ink"
    if kind == "widen_all":
        # claims every stroke is a 240 mm filled band, by rewriting edges_m only
        mm_px = doc["observations"]["calibration"]["mm_per_px"]
        for f in fl:
            f["edges_m"] = [f["pos_m"] - 0.12, f["pos_m"] + 0.12]
            f["support_width_m"] = 0.24
        return f"MUTATED: every face line claims a 0.24 m band in edges_m (mm/px={mm_px:.3f})"
    if kind == "swap_structure_role":
        a = doc["hypotheses"]["family_roles"]["assignment"]
        other = next((v for k, v in a.items() if k != "structure"), None)
        a["structure"] = other
        return f"MUTATED: structure role re-pointed to family {other}"
    raise SystemExit(f"unknown mutation {kind!r}")


def main(doc_path: str, cfg_path: str, out_path: str, *, mutate: str | None = None) -> int:
    doc = json.loads(Path(doc_path).read_text())
    cfg = json.loads(Path(cfg_path).read_text())
    note = _mutate(doc, cfg, mutate) if mutate else None

    a = load_rgb(cfg["image"])
    masks = _family_masks(a)
    roles = doc["hypotheses"]["family_roles"]["assignment"]
    for need in ("structure",):
        if roles.get(need) not in masks:
            raise SystemExit(f"role {need!r} points at family {roles.get(need)!r}, "
                             f"which this drawing does not have: {sorted(masks)}")
    box = doc["declarations"]["drawing_box_px"]
    st = masks[roles["structure"]].copy()
    r0, r1, c0, c1 = box
    st[:r0, :] = False
    st[r1:, :] = False
    st[:, :c0] = False
    st[:, c1:] = False

    report = {"source": doc_path, "image": cfg["image"], "schema": "as_drawn_plan_v2",
              "mutation": note, "role_assignment": roles,
              "checks": [check_reverse(doc, st),
                         check_self_consistency(doc),
                         check_gaps_recomputable(doc, masks),
                         check_face_span_accounted(doc, masks, roles),
                         check_support_strip_is_one_stroke(doc, masks, roles, cfg),
                         check_runs_match_the_strip(doc, masks, roles, cfg),
                         check_opening_role_matches_ink(doc),
                         check_opening_naming_supported(doc),
                         check_pair_reconciliation(doc),
                         check_spacing_declared(doc),
                         check_forward(doc, masks, roles, box,
                                       cfg.get("component_floor_px", 400),
                                       cfg.get("max_unclaimed_pct", 8.0))]}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    for c in report["checks"]:
        extra = f" worst={c['worst_coverage']}" if "worst_coverage" in c else ""
        extra += f" unclaimed={c['unclaimed_pct']}%" if "unclaimed_pct" in c else ""
        extra += " [degraded]" if c["status"] == "degraded" else ""
        print(f"  {c['status']:>8}  {c['check']:<48} violations={c['violation_count'] if 'violation_count' in c else 0}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3],
                          mutate=(sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None)))
