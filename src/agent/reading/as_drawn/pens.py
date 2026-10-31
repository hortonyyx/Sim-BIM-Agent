"""Discover a drawing's ink families **without naming them**.

⭐ Why this file exists (2026-08-23, user's ratification): the project's
division of labour (invariant #1) puts *perception* with the model and
*geometry* with code.  The prototype violated that in its very first step --
``plan_ink.ink_families`` hard-codes ``neutral -> structure``,
``cyan -> fenestration``, ``magenta -> furniture``, ``green -> annotation``.
That mapping is a **drawing convention**, not a fact: it holds on the cases
drawn so far and fails completely on sm20 (measured: zero cyan pixels, so the
only mask the project had was blind to the layer the windows live on -- F-69).
Baking it in makes every future drawing style a silent failure.

⛔ Equally rejected: "declare the mapping per drawing in the config."  That is
still a human filling a box, and it generalises no better.

So this module measures what is measurable and stops there:
  * which colour families the ink actually falls into, keyed by CHROMATICITY
    (anti-aliasing blends toward the background, which scales brightness and
    leaves the channel ratio alone, so the hundreds of observed RGB values
    collapse onto a handful of directions);
  * how much ink each family carries;
  * what each family's marks LOOK like -- component counts, sizes, how
    line-like they are.

⛔ No family is called a wall, a window or a note.  Naming them is perception,
and perception is the model's job; this output is the evidence it needs, and
the thing a consumption ledger later checks the naming against.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from src.agent.reading.as_drawn._plan_ink import INK_MIN, load_rgb

# Chromaticity is quantised this finely before clustering. Anti-aliased pixels
# of one pen keep their channel ratio, so they land in the same cell; a cell is
# reported only if it carries at least MIN_SHARE of the ink.
CHROMA_STEPS = 8
MIN_SHARE = 0.005
# Anti-aliased edges of one pen blend toward whatever is behind them, so a pen
# shows up as a bright core direction plus a fringe of neighbouring directions.
# Cells within this chromaticity distance of an existing family centre join it.
# ⚠️ A parameter, therefore swept and reported: family counts are unchanged
# across 0.20-0.45 on all five drawings measured, and every merge distance
# actually used is written into the output.
MERGE_DIST = 0.30
# A family's marks are called line-like by measuring, per component, how much
# thinner it is than it is long. ⛔ This is a shape statistic, not a claim about
# what the marks mean.
LINE_ASPECT = 4.0


def _chroma_key(px: np.ndarray) -> np.ndarray:
    mx = px.max(axis=1, keepdims=True)
    mx[mx == 0] = 1
    q = np.round(px / mx * CHROMA_STEPS).astype(int)
    return (q[:, 0] * (CHROMA_STEPS + 1) + q[:, 1]) * (CHROMA_STEPS + 1) + q[:, 2]


def _shape_stats(mask: np.ndarray) -> dict[str, Any]:
    from scipy import ndimage
    lab, n = ndimage.label(mask)
    if n == 0:
        return {"components": 0}
    areas, aspects, longest = [], [], 0
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        a = int((lab[sl] == i).sum())
        if a < 4:
            continue
        areas.append(a)
        aspects.append(max(h, w) / max(1, min(h, w)))
        longest = max(longest, max(h, w))
    if not areas:
        return {"components": 0}
    areas_s = sorted(areas)
    return {
        "components": len(areas),
        "area_px_median": areas_s[len(areas_s) // 2],
        "area_px_max": areas_s[-1],
        "longest_extent_px": longest,
        # fraction of components that are at least LINE_ASPECT times longer
        # than they are wide -- a shape statistic, ⛔ not "these are lines"
        "elongated_fraction": round(
            sum(1 for x in aspects if x >= LINE_ASPECT) / len(aspects), 3),
        "fill_ratio_median": round(
            float(np.median([a / max(1, x) for a, x in zip(areas, aspects)])) /
            max(1.0, float(np.median(areas))), 4),
    }


def _merge_cells(cells: list[tuple[int, int, np.ndarray]], merge_dist: float):
    """Group quantised chromaticity cells into families, brightest-first.

    ⛔ No colour is named. A family is just "cells whose channel ratios point
    the same way", which is what an anti-aliased pen actually produces.
    """
    fams: list[dict[str, Any]] = []
    for key, n, chroma in cells:                       # cells arrive count-sorted
        best, bd = None, 1e9
        for f in fams:
            d = float(np.abs(f["centre"] - chroma).max())
            if d < bd:
                best, bd = f, d
        if best is not None and bd <= merge_dist:
            best["cells"].append((key, n, chroma, round(bd, 3)))
            best["px"] += n
        else:
            fams.append({"centre": chroma, "px": n,
                         "cells": [(key, n, chroma, 0.0)]})
    return fams


def palette(a: np.ndarray, *, box: list[int] | None = None,
            merge_dist: float = MERGE_DIST) -> dict[str, Any]:
    ink = a.max(2) >= INK_MIN
    if box:
        r0, r1, c0, c1 = box
        keep = np.zeros_like(ink)
        keep[r0:r1, c0:c1] = True
        ink &= keep
    idx = np.flatnonzero(ink.ravel())
    px = a.reshape(-1, 3)[idx]
    if px.size == 0:
        return {"ink_px": 0, "families": [], "achromatic_only": True}
    keys = _chroma_key(px)
    counts = Counter(keys.tolist())
    total = len(keys)

    cells = []
    for k, n in counts.most_common():
        if n / total < MIN_SHARE:
            continue
        rep = px[keys == k]
        mx = rep.max(axis=1, keepdims=True)
        mx[mx == 0] = 1
        cells.append((k, n, (rep / mx).mean(axis=0)))

    # ⭐ Two different jobs, and only the first one uses a threshold:
    #   DISCOVERY  -- which chromaticity directions carry enough ink to be a
    #                 pen at all (MIN_SHARE);
    #   ASSIGNMENT -- every ink pixel then goes to its NEAREST discovered
    #                 direction, with the distance recorded.
    # Keeping them separate means no pixel is silently dropped for being rare:
    # measured on sm25 1f, thresholded assignment left 8.22% of the ink in no
    # family at all and cost the information-not-lost check 4 points.
    merged = _merge_cells(cells, merge_dist)
    centres = np.array([f["centre"] for f in merged]) if merged else np.zeros((0, 3))
    all_mx = px.max(axis=1, keepdims=True).astype(float)
    all_mx[all_mx == 0] = 1
    all_chroma = px / all_mx
    if len(centres):
        dist = np.abs(all_chroma[:, None, :] - centres[None, :, :]).max(axis=2)
        nearest = dist.argmin(axis=1)
        nearest_d = dist.min(axis=1)
    else:
        nearest = np.zeros(len(px), dtype=int)
        nearest_d = np.zeros(len(px))

    families = []
    for fi, f in enumerate(merged):
        member = nearest == fi
        rep = px[member]
        m = np.zeros(a.shape[:2], dtype=bool)
        m.ravel()[idx[member]] = True
        mx = rep.max(axis=1, keepdims=True)
        mx[mx == 0] = 1
        families.append({
            "id": f"F{len(families)}",
            "chromaticity": [round(float(v), 3) for v in (rep / mx).mean(axis=0)],
            "core_chromaticity": [round(float(v), 3) for v in f["centre"]],
            "cells_merged": len(f["cells"]),
            "max_merge_distance": max(c[3] for c in f["cells"]),
            "spread": round(float((rep.max(axis=1) - rep.min(axis=1)).mean()), 1),
            "brightness_median": int(np.median(rep.max(axis=1))),
            "px": int(member.sum()),
            "pct_of_ink": round(100.0 * int(member.sum()) / total, 2),
            # ⭐ how far the family's members sit from its centre: a blend
            # between two pens shows up here instead of vanishing.
            "assign_distance_p50": round(float(np.median(nearest_d[member])), 3) if member.any() else None,
            "assign_distance_p95": round(float(np.quantile(nearest_d[member], 0.95)), 3) if member.any() else None,
            "shape": _shape_stats(m),
        })
    covered = sum(f["px"] for f in families)   # == total by construction now
    return {
        "ink_px": total,
        "distinct_rgb_values": len(set((px[:, 0] << 16 | px[:, 1] << 8 | px[:, 2]).tolist())),
        "chroma_steps": CHROMA_STEPS, "min_share": MIN_SHARE,
        "merge_dist": merge_dist,
        "families": families,
        "unassigned_pct": round(100.0 * (total - covered) / total, 2),
        # ⭐ A drawing whose ink is all one colour direction cannot have its
        # layers told apart by colour AT ALL. Saying so is the honest output;
        # returning a confident partition would be F-69 all over again.
        "achromatic_only": all(max(f["chromaticity"]) - min(f["chromaticity"]) < 0.15
                               for f in families),
    }


def main(image: str, out_path: str | None = None) -> int:
    p = palette(load_rgb(image))
    print(f"{Path(image).parent.parent.name}/{Path(image).stem}: "
          f"ink={p['ink_px']} rgb_values={p['distinct_rgb_values']} "
          f"families={len(p['families'])} unassigned={p['unassigned_pct']}% "
          f"achromatic_only={p['achromatic_only']}")
    for f in p["families"]:
        s = f["shape"]
        print(f"   {f['id']} chroma={f['chromaticity']} {f['pct_of_ink']:6.2f}%  "
              f"comps={s.get('components', 0):5d} median_area={s.get('area_px_median', 0):5d} "
              f"longest={s.get('longest_extent_px', 0):5d} elongated={s.get('elongated_fraction', 0)}")
    if out_path:
        Path(out_path).write_text(json.dumps(p, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
