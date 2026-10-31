"""Hints for declared exterior windows, using an already observed wall-face pair.

Two separate short parallel strokes may locate a frame span. They never establish
window identity or completeness, and cannot authorize an automatic geometry edit.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from .plan_drawing_differences import _runs


def append_window_span_checks(report, mask, plan, wall_pairs):
    """Append located hints to a private report; leave image and geometry untouched."""
    coverage = {"declared_windows": 0, "compared_spans": 0, "unavailable_or_ambiguous": 0,
                "meaning": "Local parallel-stroke comparison inside an observed exterior wall pair; "
                           "no hint is not verification of a window, its identity or completeness."}
    report.setdefault("coverage", {})["exterior_window_spans"] = coverage
    if "not_checked" in report["coverage"]:
        report["coverage"]["not_checked"] = [
            "exterior wall/window completeness and identity" if value == "exterior walls/windows" else value
            for value in report["coverage"]["not_checked"]]
    slopes = [(a[1][1] - a[0][1]) / (a[1][0] - a[0][0])
              for a in (plan["x_anchors"], plan["y_anchors"])]
    ring = plan["footprint_pixels"]
    found = []
    for opening in plan["openings"]:
        if opening["kind"] != "window":
            continue
        coverage["declared_windows"] += 1
        coverage["unavailable_or_ambiguous"] += 1
        p, q = opening["p1"], opening["p2"]
        axes = [i for i in (0, 1) if abs(p[i] - q[i]) < 1e-7]
        if len(axes) != 1:
            continue
        normal, along = axes[0], 1 - axes[0]
        lo, hi = sorted((p[along], q[along]))
        at = (p[normal] + q[normal]) / 2
        hosts = [(f"footprint:{i}", v, w) for i, (v, w) in enumerate(zip(ring, ring[1:] + ring[:1]))
                 if abs(v[normal] - at) < 1e-7 and abs(w[normal] - at) < 1e-7
                 and min(v[along], w[along]) <= lo + 1e-7 and max(v[along], w[along]) >= hi - 1e-7]
        if len(hosts) != 1 or hosts[0][0] not in wall_pairs:
            continue
        target, v, w = hosts[0]
        faces = wall_pairs[target]
        # Trim the observed wall-face strokes themselves. Inferred thickness
        # and permitted normal grid shifts must not define an artificial frame.
        c0, c1 = math.ceil(faces[0]) + 1, math.floor(faces[1]) - 1
        mpp = abs(slopes[along])
        s0 = math.floor(max(min(v[along], w[along]), lo - .6 / mpp))
        s1 = math.ceil(min(max(v[along], w[along]), hi + .6 / mpp))
        cross_size, along_size = (mask.shape[1], mask.shape[0]) if normal == 0 else mask.shape
        if c1 - c0 < 3 or c0 < 0 or c1 >= cross_size or s0 < 0 or s1 >= along_size:
            continue
        stripe = mask[s0:s1 + 1, c0:c1 + 1] if normal == 0 else mask[c0:c1 + 1, s0:s1 + 1].T
        groups = []
        for column in range(stripe.shape[1]):
            flags = stripe[:, column].copy()
            flags[1:-1] |= flags[:-2] & flags[2:]  # One raster hole only.
            for start, end in _runs(flags):
                if start == 0 or end == len(flags) - 1 or (end - start) * mpp < .4:
                    continue
                start, end = start + s0, end + s0
                if max(0, min(hi, end) - max(lo, start)) < .5 * min(hi - lo, end - start):
                    continue
                group = next((g for g in groups if abs(g[0][0] - start) <= 2
                              and abs(g[0][1] - end) <= 2), None)
                if group is None:
                    groups.append([(start, end, column)])
                else:
                    group.append((start, end, column))
        candidates = []
        for group in groups:
            columns = np.zeros(stripe.shape[1], dtype=bool)
            for _, _, column in group:
                columns[column] = True
            if len(_runs(columns)) < 2:  # A solid stroke is not two rails.
                continue
            candidates.append([float(np.median([row[i] for row in group])) for i in (0, 1)])
        if len(candidates) != 1:
            continue
        # A raster bridge must not absorb a neighboring declared aperture.
        if any(other["id"] != opening["id"]
               and abs(other["p1"][normal] - at) < 1e-7
               and abs(other["p2"][normal] - at) < 1e-7
               and min(candidates[0][1], max(other["p1"][along], other["p2"][along]))
                   - max(candidates[0][0], min(other["p1"][along], other["p2"][along])) > 1e-7
               for other in plan["openings"]):
            continue
        coverage["compared_spans"] += 1
        coverage["unavailable_or_ambiguous"] -= 1
        residual = [abs(a - b) * mpp for a, b in zip((lo, hi), candidates[0])]
        if max(residual) <= max(.15, 3 * mpp):
            continue
        margin = max(2, math.ceil(.1 / abs(slopes[normal])))
        cross_lo, cross_hi = max(0, c0 - margin), min(cross_size - 1, c1 + margin)
        box = ([cross_lo, s0, cross_hi, s1] if normal == 0 else [s0, cross_lo, s1, cross_hi])
        found.append({"type": "window_span_ink_mismatch", "opening_id": opening["id"],
                      "wall_target": target, "span_axis": "xy"[along], "look_box": box,
                      "adopted_span_px": [round(lo, 3), round(hi, 3)],
                      "ink_span_px": candidates[0], "endpoint_residual_m": [round(r, 4) for r in residual],
                      "check": "Two separate parallel strokes inside the observed wall faces have matching ends "
                               "that differ from this adopted window span. Inspect the original frame and any "
                               "bracketed width annotation; correct wrong aperture endpoints, or explain a different "
                               "feature. These strokes are a local hint, not a window identity or an automatic edit."})
    report["items"] = [*report.get("items", []), *found]
    report["total"] = len(report["items"])
    report["counts"] = dict(Counter(row["type"] for row in report["items"]))
    report["scope"] = "Base checks: " + report.get("scope", "") + " A separate scoped check compares declared exterior-window " \
        "spans with unambiguous short parallel strokes inside observed wall faces; other exterior windows remain unchecked."
    return report
