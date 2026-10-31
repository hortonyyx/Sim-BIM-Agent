"""⭐ reading grade — 线段对线段, against the machine-defined denominator.

This is the D step: the reading judge the batch guide asked for.  It scores the
as-drawn product against ``denominator.py``'s targets, in BOTH directions, with
one criterion per failure mode the third cross-family review named:

  C1 POSITION   is the drawn face line where the answer's face line is?
                ⭐ and is it a DIFFERENT observation (or a different EDGE of one
                filled band) for each of a wall's two faces?  One thin stroke
                drawn down the middle of a 120 mm wall sits within tolerance of
                BOTH faces; without this rule a centreline reading -- the shape
                the batch guide forbids outright -- scored exactly like the
                honest product (fourth cross-family review, GLM).
  C2 COVERAGE   is the answer's run actually drawn over its whole length?
                (少画 — the failure a "does it look right" glance never sees)
  C3 SEGMENTATION  do the reading's run ENDS land on the answer's run ends?
                (错切 — one stroke drawn straight through a doorway is C1+C2
                 perfect and still wrong; the gt-free ledger says so itself:
                 "it cannot judge whether a stroke should have been split")
  C5 OPENING    is each opening the answer has both FOUND and NAMED right?
                (门窗身份 — ⭐ measured 2026-08-24: swapping every door and window
                 in the reading moved the gt-side reconstruction by 0.0 points,
                 because that check only ever asks "is this stretch an opening")
  C4 EXTRA      length the reading claims as wall that no target explains AND
                that the answer does not even ALLOW.  ⭐ The jamb caps D2 keeps
                out of the targets are real strokes on the drawing: not scored
                for, and (since the fourth review) not scored against either.
                (多画 — ⭐ the direction a gt-only ruler REWARDS: claiming a
                 line runs edge-to-edge raised the old reconstruct score above
                 the honest product's)

⭐ Only face lines PERCEPTION positively called walls are graded.  A face line it
declared ``non_wall`` / ``ambiguous`` is not silently forgiven either: it is
counted in the abstention ledger, and any TARGET left uncovered because of it
still scores as missing.  So abstaining cannot buy points -- it only moves the
loss from C4 to C2.

⛔ Tolerances are declared, reported and swept; ⛔ none of them is invented to
make a number look good.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

POS_TOL_M = 0.08      # same band the reconstruction uses: half the thinnest wall
SPAN_MIN = 0.80       # a target counts as drawn when this much of it is covered
END_TOL_M = 0.30      # how close a run end must be to the answer's run end
EXTRA_MIN_M = 0.10    # ignore slivers shorter than this when counting 多画
WIDTH_COEFF = 1.0     # how wide an observation must be to answer TWO faces; swept

# ⭐ ANNOTATION ONLY -- these two never touch a score.  They label an uncovered
# stretch as "a perpendicular wall lands here", because 2026-08-24 measurement
# showed 40-62% of the honest products' 漏画 sits exactly where a wall meets
# this one at a T and the DRAWING ITSELF breaks the face line (measured: zero
# ink of EVERY family across those stretches).  Whether the denominator should
# stop requiring ink there is a separate, unmade decision; until it is made the
# score is unchanged and the picture simply shows which red is of this kind.
TJ_ALONG_TOL_M = 0.06   # slack at each side of the perpendicular wall's own faces
TJ_REACH_TOL_M = 0.25   # how close its end must come to this face to be "landing"
TJ_MAX_WALL_M = 0.50    # widest thing that can still BE a wall (drawings declare 240/120)


def _union(spans):
    out = []
    for lo, hi in sorted([min(a, b), max(a, b)] for a, b in spans):
        if out and lo <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _uncovered(spans, lo, hi):
    """pieces of [lo, hi] that ``spans`` does not cover -- the 漏画, drawable."""
    out, cur = [], lo
    for a, b in sorted([min(x, y), max(x, y)] for x, y in spans):
        if b <= cur or a >= hi:
            continue
        if a > cur:
            out.append([cur, min(a, hi)])
        cur = max(cur, min(b, hi))
    if cur < hi:
        out.append([cur, hi])
    return [[a, b] for a, b in out if b - a > 1e-6]


def _lands_here(den, axis, const_m, a, b):
    """⭐ annotation only: is [a, b] the footprint of ONE perpendicular wall that
    lands on this face?

    ⛔ First version asked only "does some perpendicular wall fall inside [a,b]",
    which labelled a 3.64 m wholly-missed target as a T-junction because walls
    happen to land inside it -- i.e. it excused a real 漏读.  Over-claiming "this
    red is not your fault" is the dangerous direction, so the test is now
    CONTAINMENT: [a, b] must fit between the two faces of a single perpendicular
    wall, both of which must reach this face.
    """
    consts = sorted({t["const_m"] for t in den["targets"]
                     if t["axis"] != axis
                     and t["lo_m"] - TJ_REACH_TOL_M <= const_m <= t["hi_m"] + TJ_REACH_TOL_M})
    for i, c1 in enumerate(consts):
        for c2 in consts[i + 1:]:
            if c2 - c1 > TJ_MAX_WALL_M:
                break
            if c1 - TJ_ALONG_TOL_M <= a and b <= c2 + TJ_ALONG_TOL_M:
                return True
    return False


def _covered(spans, lo, hi):
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in spans)


def grade(doc: dict, den: dict, *, pos_tol: float = POS_TOL_M,
          span_min: float = SPAN_MIN, end_tol: float = END_TOL_M,
          extra_min: float = EXTRA_MIN_M) -> dict:
    hyp = doc["hypotheses"]
    graded_ids = ({x for p in (hyp.get("pairs") or []) for x in (p["face_a"], p["face_b"])}
                  | set(hyp.get("solid_band_walls") or {})
                  | set(hyp.get("unpaired_wall_faces") or {}))
    abstained = {"non_wall": len(hyp.get("non_wall_face_lines") or {}),
                 "ambiguous": len(hyp.get("ambiguous_face_lines") or {})}

    lines = []
    for f in doc["observations"]["face_lines"]:
        if f["id"] not in graded_ids:
            continue
        axis = "x" if f["constant_world_axis"] == "x" else "y"
        # ⭐ 2026-08-24, fourth cross-family review (GLM): a wall drawn as ONE
        # FILLED BAND (the sm24 dialect) has its two faces at the band's own
        # EDGES, not at its centre.  Matching only on ``pos_m`` scored the honest
        # sm24 product 44.3% and rewarded a re-representation of the very same
        # ink with 85.7% -- the grade was measuring dialect, not correctness.
        # ⚠️ Consuming ``edges_m`` is only safe because
        # ``observations_recomputable_from_own_pixels`` recomputes it from the
        # support columns; ⛔ without that gate this would trust a claim.
        consts = [float(f["pos_m"])] + [float(e) for e in (f.get("edges_m") or [])]
        edges = [float(e) for e in (f.get("edges_m") or [])]
        lines.append({"id": f["id"], "axis": axis, "const": float(f["pos_m"]),
                      "consts": consts,
                      "width_m": (max(edges) - min(edges)) if len(edges) >= 2 else 0.0,
                      "runs": _union([tuple(r) for r in f["runs_m"]])})

    rows, used_len = [], {ln["id"]: 0.0 for ln in lines}
    claimed: dict[tuple[str, float], int] = {}
    tot_required = 0.0
    for t in den["targets"]:
        lo, hi = t["lo_m"], t["hi_m"]
        tot_required += t.get("required_length_m", t["length_m"])
        best = None
        for ln in lines:
            if ln["axis"] != t["axis"]:
                continue
            # which of this observation's own constants answers this target?
            ci = min(range(len(ln["consts"])),
                     key=lambda i: abs(ln["consts"][i] - t["const_m"]))
            if abs(ln["consts"][ci] - t["const_m"]) > pos_tol:
                continue
            # ⛔ One observation may answer a SECOND face only if it is itself as
            # wide as the wall -- i.e. it is a filled band whose own two edges are
            # those faces.  A thin stroke down the middle of a 120 mm wall sits
            # within tolerance of both faces; letting it answer both is exactly
            # the centreline reading the batch guide forbids, and it scored
            # identically to the honest product until this rule (GLM, 4th review).
            prior = [c for (lid, c) in claimed if lid == ln["id"]]
            if any(abs(c - t["const_m"]) > 1e-6 for c in prior):
                need = max(abs(c - t["const_m"]) for c in prior)
                # ⚠️ 0.5 was unscanned and let a HALF-aperture band through
                # (GLM's scan: partial-0.5 fixture scored 100.0).  The honest
                # sm24 bands' own ratio is 1.146-1.261, so 1.0 costs the honest
                # dialects nothing and kills the half-aperture family; the
                # full-span ``band_collapse`` is caught by the support-strip gate.
                if ln["width_m"] < WIDTH_COEFF * need:
                    continue
            cov = _covered(ln["runs"], lo, hi)
            if best is None or cov > best[0]:
                best = (cov, ln, ci)
        row = {"axis": t["axis"], "const_m": t["const_m"], "span": [lo, hi],
               "length_m": t["length_m"], "holes": t.get("holes") or []}
        if best is None or best[0] <= 0:
            row.update({"verdict": "MISSING", "matched": None, "coverage": 0.0,
                        "uncovered": [[lo, hi]],
                        "uncovered_at_tjunction": [_lands_here(den, t["axis"],
                                                               t["const_m"], lo, hi)]})
            rows.append(row)
            continue
        cov, ln, ci = best
        claimed[(ln["id"], t["const_m"])] = ci
        # ⭐ 2026-08-25: a target may carry HOLES -- blanks the answer itself does
        # not fill, because another wall lands there and the DXF is in two
        # fragments across it (measured: zero ink of every family, and the
        # distance merge alone invented 3.36 m of demand not in the answer).
        # Ink there is neither required nor punished; the target stays ONE target
        # so its ENDS remain where a cut really is expected (C3).
        holes = t.get("holes") or []
        req = _uncovered(holes, lo, hi) if holes else [[lo, hi]]
        req_len = sum(b - a for a, b in req)
        cov = sum(_covered(ln["runs"], a, b) for a, b in req)
        frac = cov / max(1e-9, req_len)
        used_len[ln["id"]] += cov
        # C3: does a drawn end sit near each answer end?
        ends = [e for r in ln["runs"] for e in r]
        end_err = [min((abs(e - x) for e in ends), default=9.9) for x in (lo, hi)]
        row.update({"matched": ln["id"], "coverage": round(frac, 4),
                    # ⛔ recorded, never reconstructed: `length_m * coverage` was
                    # wrong the moment `coverage` became a fraction of the
                    # REQUIRED span rather than the raw one (it read 100.4%).
                    "covered_m": round(cov, 4), "required_m": round(req_len, 4),
                    "const_err_m": round(min(abs(c - t["const_m"]) for c in ln["consts"]), 4),
                    "end_err_m": [round(e, 3) for e in end_err],
                    "verdict": ("OK" if frac >= span_min and max(end_err) <= end_tol
                                else "SHORT" if frac < span_min else "BAD_SPLIT")})
        # ⭐ view-only: the exact stretches the reading did not draw, so the grade
        # picture shows WHERE the loss is instead of only how much of it there is.
        unc = [u for a, b in req for u in _uncovered(ln["runs"], a, b)]
        row["uncovered"] = [[round(a, 4), round(b, 4)] for a, b in unc]
        row["uncovered_at_tjunction"] = [_lands_here(den, t["axis"], t["const_m"], a, b)
                                         for a, b in unc]
        rows.append(row)

    extras = []
    for ln in lines:
        drawn = sum(b - a for a, b in ln["runs"])
        # length of this line that no target on its axis+const can explain
        tgt = _union([(t["lo_m"], t["hi_m"]) for t in den["targets"]
                      if t["axis"] == ln["axis"]
                      and min(abs(c - t["const_m"]) for c in ln["consts"]) <= pos_tol]
                     + [(a["lo_m"], a["hi_m"]) for a in den.get("allowed_not_required", [])
                        if a["axis"] == ln["axis"]
                        and min(abs(c - a["const_m"]) for c in ln["consts"]) <= pos_tol]
                     + [tuple(h) for t2 in den["targets"] for h in (t2.get("holes") or [])
                        if t2["axis"] == ln["axis"]
                        and min(abs(c - t2["const_m"]) for c in ln["consts"]) <= pos_tol])
        unexplained = drawn - sum(_covered(tgt, a, b) for a, b in ln["runs"])
        if unexplained >= extra_min:
            # ⭐ view-only: which stretches of this line nothing explains (多画)
            spans = [sp for a, b in ln["runs"] for sp in _uncovered(tgt, a, b)]
            extras.append({"face": ln["id"], "axis": ln["axis"],
                           "const_m": round(ln["const"], 4),
                           "unexplained_m": round(unexplained, 3),
                           "drawn_m": round(drawn, 3),
                           "spans": [[round(a, 4), round(b, 4)] for a, b in spans]})

    # ---- C5: opening identity, against the answer's own resolved openings
    otypes = hyp.get("opening_types") or {}
    cands = {c["id"]: c for c in doc["observations"].get("opening_candidates", [])
             or hyp.get("opening_candidates", [])}
    faces = {f["id"]: f for f in doc["observations"]["face_lines"]}
    o_rows = []
    for t in den.get("opening_targets", []):
        c_lo, c_hi = t["const_range_m"]
        best, best_ov = None, 0.0
        for cid, kind in otypes.items():
            if kind not in ("door", "window"):
                continue
            c = cands.get(cid)
            if c is None:
                continue
            f = faces.get(c["face_line"])
            if f is None or ("x" if f["constant_world_axis"] == "x" else "y") != t["axis"]:
                continue
            if not (c_lo - pos_tol <= float(f["pos_m"]) <= c_hi + pos_tol):
                continue
            lo, hi = sorted(c["span_m"])
            ov = max(0.0, min(hi, t["hi_m"]) - max(lo, t["lo_m"]))
            if ov > best_ov:
                best, best_ov = (cid, kind), ov
        if best is None or best_ov < 0.5 * (t["hi_m"] - t["lo_m"]):
            o_rows.append({**t, "verdict": "NOT_FOUND", "named": None})
        else:
            o_rows.append({**t, "verdict": "OK" if best[1] == t["kind"] else "WRONG_KIND",
                           "named": best[1], "candidate": best[0],
                           "overlap_m": round(best_ov, 3)})
    o_ok = sum(1 for r in o_rows if r["verdict"] == "OK")

    n = len(rows)
    ok = sum(1 for r in rows if r["verdict"] == "OK")
    # ⭐ the answer's REQUIRED length: raw span minus the holes the answer itself
    # leaves blank.  ⛔ Using the raw span would keep demanding ink that is not in
    # the answer (measured 2026-08-25: 3.36 m on sm25 1F alone).
    tot_len = tot_required
    cov_len = sum(r.get("covered_m", 0.0) for r in rows)
    extra_len = sum(e["unexplained_m"] for e in extras)
    return {
        "grade_version": "reading_grade_v1",
        "denominator": {"rule": den["rule_version"], "view": den["view_id"],
                        "targets": n, "length_m": round(tot_len, 3)},
        "params": {"pos_tol_m": pos_tol, "span_min": span_min,
                   "end_tol_m": end_tol, "extra_min_m": extra_min},
        "scores": {
            "C1_C2_targets_drawn_pct": round(100.0 * ok / max(1, n), 1),
            "C2_length_coverage_pct": round(100.0 * cov_len / max(1e-9, tot_len), 1),
            "C3_bad_split": sum(1 for r in rows if r["verdict"] == "BAD_SPLIT"),
            "C4_extra_length_m": round(extra_len, 3),
            "C4_extra_pct_of_answer": round(100.0 * extra_len / max(1e-9, tot_len), 1),
            "C5_openings_named_right_pct": (round(100.0 * o_ok / len(o_rows), 1)
                                            if o_rows else None),
            "C5_openings": {"targets": len(o_rows), "ok": o_ok,
                            "wrong_kind": sum(1 for r in o_rows if r["verdict"] == "WRONG_KIND"),
                            "not_found": sum(1 for r in o_rows if r["verdict"] == "NOT_FOUND")},
        },
        "by_verdict": {v: sum(1 for r in rows if r["verdict"] == v)
                       for v in ("OK", "SHORT", "BAD_SPLIT", "MISSING")},
        "perception": {"face_lines_graded": len(lines), "abstained": abstained,
                       "note": "⛔ abstaining does not forgive a target: an answer run "
                               "left uncovered still scores as MISSING/SHORT."},
        "worst_targets": sorted([r for r in rows if r["verdict"] != "OK"],
                                key=lambda r: -r["length_m"])[:12],
        "extras": sorted(extras, key=lambda e: -e["unexplained_m"])[:12],
        "opening_rows": [r for r in o_rows if r["verdict"] != "OK"][:16],
        # ⭐ 2026-08-24: the complete row set, for the grade PICTURE only.
        # ⛔ It is the same `rows`/`o_rows`/`extras` the scores above were counted
        # from -- the renderer must never recompute anything of its own.
        "detail": {"targets": rows, "openings": o_rows, "extras": extras},
    }


def main(doc_path: str, den_path: str, out_path: str, **kw) -> int:
    g = grade(json.loads(Path(doc_path).read_text()),
              json.loads(Path(den_path).read_text()), **kw)
    Path(out_path).write_text(json.dumps(g, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"view": g["denominator"]["view"], **g["scores"],
                      **g["by_verdict"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    kw = {}
    for i, name in enumerate(("pos_tol", "span_min", "end_tol", "extra_min"), start=4):
        if len(sys.argv) > i:
            kw[{"pos_tol": "pos_tol", "span_min": "span_min",
                "end_tol": "end_tol", "extra_min": "extra_min"}[name]
               + ("_m" if name != "span_min" else "")] = float(sys.argv[i])
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3], **kw))
