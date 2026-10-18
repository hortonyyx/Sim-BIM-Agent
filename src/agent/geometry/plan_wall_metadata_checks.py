"""Located ink hints for a declared wall reference/width/face; never a wall recognizer.

Only two unambiguous, persistent parallel strokes are compared. Sparse, clipped,
unknown and busy neighborhoods are left for the reader. No geometry is changed
and neither agreement nor absence constitutes independent drawing acceptance.
"""
from __future__ import annotations

import copy
from collections import Counter

import numpy as np

from .plan_drawing_differences import _ink, _runs
from .reader_wall_metadata import wall_reading_input


def append_wall_metadata_checks(report, original, plan, metadata):
    result = copy.deepcopy(report)
    coverage = {"compared_segments": 0, "unresolved_or_ambiguous_segments": 0,
                "meaning": "Two persistent parallel ink strokes only; not wall identification or complete coverage."}
    result.setdefault("coverage", {})["wall_metadata"] = coverage
    if report.get("status") != "reported":
        return result
    mask = _ink(original)
    if float(mask.mean()) > .5:
        coverage["reason"] = "No sparse drawing background; no negative ink inference."
        return result
    anchors = [plan[axis + "_anchors"] for axis in ("x", "y")]
    slopes = [(w1 - w0) / (p1 - p0) for (p0, w0), (p1, w1) in anchors]

    def pixel(value, axis):
        (p0, w0), _ = anchors[axis]
        return p0 + (value - w0) / slopes[axis]

    found = []
    wall_pairs = {}
    for wall in wall_reading_input(plan, metadata)["walls"]:
        thickness, side = wall["thickness_m"], wall["reference_side"]
        for a, b in wall["segments_m"]:
            coverage["unresolved_or_ambiguous_segments"] += 1
            normal = 0 if abs(a[0] - b[0]) < 1e-7 else 1
            along = 1 - normal
            position = (a[normal] + b[normal]) / 2
            lo, hi = sorted((pixel(a[along], along), pixel(b[along], along)))
            at = pixel(position, normal)
            mpp, along_mpp = abs(slopes[normal]), abs(slopes[along])
            radius = max(.45, (thickness or 0) + .1) / mpp
            cross_lo, cross_hi = int(np.floor(at - radius)), int(np.ceil(at + radius))
            cross_size, along_size = (mask.shape[1], mask.shape[0]) if normal == 0 else mask.shape
            # Cropping must not turn unavailable pixels into negative evidence.
            if cross_lo < 0 or cross_hi >= cross_size or lo < 0 or hi >= along_size:
                continue
            trim = .2 / along_mpp
            sample = np.arange(int(np.ceil(lo + trim)), int(np.floor(hi - trim)) + 1)
            for opening in metadata.get("opening_hosts", []):
                p, q = opening["p1_world_m"], opening["p2_world_m"]
                if max(abs(p[normal] - position), abs(q[normal] - position)) <= 1e-7:
                    start, end = sorted((pixel(p[along], along), pixel(q[along], along)))
                    margin = .1 / along_mpp
                    sample = sample[(sample < start - margin) | (sample > end + margin)]
            if sample.size < 12 or sample.size * along_mpp < .4:
                continue
            stripe = (mask[sample, cross_lo:cross_hi + 1] if normal == 0
                      else mask[cross_lo:cross_hi + 1, sample].T)
            support = stripe.mean(axis=0)
            strokes = _runs(support >= .70)
            if len(strokes) != 2:
                continue
            lines = [(s + e) / 2 + cross_lo for s, e in strokes]
            if (lines[1] - lines[0]) < 2:
                continue
            if wall["target"].startswith("footprint:"):
                wall_pairs[wall["target"]] = lines
            # Unknown thickness or side cannot define two declared faces, but
            # the reference still belongs within the observed pair. Unknown
            # metadata is not itself an error and is never filled from ink.
            offsets = None if side == "unknown" or thickness is None else {
                "centerline": (-thickness / 2, thickness / 2),
                "negative_face": (0, thickness), "positive_face": (-thickness, 0)}[side]
            faces = None if offsets is None else sorted(pixel(position + offset, normal) for offset in offsets)
            outside = max(lines[0] - at, at - lines[1], 0) * mpp
            residual = None if faces is None else [abs(a - b) * mpp for a, b in zip(faces, lines)]
            spacing = (lines[1] - lines[0]) * mpp
            coverage["compared_segments"] += 1
            coverage["unresolved_or_ambiguous_segments"] -= 1
            # Position allows the already configured 10 cm coordinate grid and
            # raster sampling. Width is an attribute, not a quantized coordinate.
            position_error = outside if residual is None else max(residual)
            width_agrees = thickness is None or abs(spacing - thickness) <= max(.05, 2 * mpp)
            if position_error <= max(.06, 2 * mpp) and width_agrees:
                continue
            box = ([cross_lo, int(lo), cross_hi, int(hi)] if normal == 0
                   else [int(lo), cross_lo, int(hi), cross_hi])
            found.append({"type": "wall_metadata_ink_mismatch", "wall_target": wall["target"],
                          "axis": "x" if normal == 0 else "y", "look_box": box,
                          "comparison": "placement_only" if width_agrees else "thickness_and_placement",
                          "image_expansion": ("unknown" if offsets is None else "both" if side == "centerline"
                              else ("right" if normal == 0 else "down") if sum(offsets) / slopes[normal] > 0
                              else ("left" if normal == 0 else "up")),
                          "declared_faces_px": None if faces is None else [round(p, 3) for p in faces],
                          "declared_reference_px": round(at, 3), "reference_side": side,
                          "reference_outside_ink_m": round(outside, 4),
                          "nearby_parallel_ink_px": [round(p, 3) for p in lines],
                          "face_residual_m": None if residual is None else [round(p, 4) for p in residual],
                          "declared_thickness_m": thickness, "ink_spacing_m": round(spacing, 4),
                          "sampled_length_m": round(sample.size * along_mpp, 4),
                          "sample_pixels": int(sample.size),
                          "check": "The declared wall reference, faces or width differ from two persistent nearby ink lines. "
                                   "Check the original pair, selected reference side and thickness basis; "
                                   "these strokes may be other drawing features. A placement_only hint may be an "
                                   "audited grid/merge shift: correct a wrong feature or side, not a deliberate Lite BIM offset."})
    result["items"] = [*result.get("items", []), *found]
    result["total"] = len(result["items"])
    result["counts"] = dict(Counter(row["type"] for row in result["items"]))
    result["scope"] = result.get("scope", "") + " Also compares declared wall references and known thickness/side " \
                      "against unambiguous local parallel ink; unknown metadata is not an error or inferred here. " \
                      "This adds hints, not wall identification."
    from .plan_window_span_checks import append_window_span_checks
    return append_window_span_checks(result, mask, plan, wall_pairs)
