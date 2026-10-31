"""Compare plan and elevation opening spans in both axis directions.

Input JSON:
  {"plan": {"axis_anchors": [[pixel_at_0, 0], [pixel_at_L, L]],
            "openings": [{"id": "P1", "pixels": [a, b]}, ...]},
   "elevation": {"axis_anchors": [[pixel_at_0, 0], [pixel_at_L, L]],
                 "openings": [{"id": "E1", "pixels": [a, b]}, ...]}}

Only original-image pixel intervals and independently declared axis anchors are
admitted. No GT, old BIM, metre-only elevation intervals, or image reading.
"""

from __future__ import annotations

import math


def finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def reject_nonfinite(value: object, label: str = "input") -> None:
    """Keep preserved observation metadata valid in strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must contain only finite numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            reject_nonfinite(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_nonfinite(item, f"{label}[{index}]")


def parse_view(raw: object, label: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    anchors = raw.get("axis_anchors")
    if not isinstance(anchors, list) or len(anchors) != 2 or any(
            not isinstance(row, list) or len(row) != 2 for row in anchors):
        raise ValueError(f"{label}.axis_anchors must have exactly [[pixel, 0], [pixel, L]]")
    p0 = finite_number(anchors[0][0], f"{label}.axis_anchors[0].pixel")
    p1 = finite_number(anchors[1][0], f"{label}.axis_anchors[1].pixel")
    m0 = finite_number(anchors[0][1], f"{label}.axis_anchors[0].metres")
    length = finite_number(anchors[1][1], f"{label}.axis_anchors[1].metres")
    if math.isclose(m0, 0.0, abs_tol=1e-9) and length > 0:
        pass
    elif math.isclose(length, 0.0, abs_tol=1e-9) and m0 > 0:
        # Row order is representational: retain each supplied pixel/metre pair.
        # This neither chooses the physical axis direction nor rebases a datum.
        p0, p1, m0, length = p1, p0, length, m0
    else:
        raise ValueError(f"{label}.axis_anchors must contain 0 m and positive L, in either row order")
    if math.isclose(p0, p1, rel_tol=0, abs_tol=1e-12):
        raise ValueError(f"{label}.axis_anchors pixel positions must differ")
    intervals = raw.get("openings")
    if not isinstance(intervals, list):
        raise ValueError(f"{label}.openings must be a complete list")
    normalized, seen = [], set()
    low, high = sorted((p0, p1))
    for index, opening in enumerate(intervals):
        if not isinstance(opening, dict):
            raise ValueError(f"{label}.openings[{index}] must be an object")
        identity = opening.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError(f"{label}.openings[{index}].id must be unique nonempty text")
        seen.add(identity)
        pixels = opening.get("pixels")
        if not isinstance(pixels, list) or len(pixels) != 2:
            raise ValueError(f"{label}.openings[{index}].pixels must be [a, b]")
        a = finite_number(pixels[0], f"{label}.{identity}.pixels[0]")
        b = finite_number(pixels[1], f"{label}.{identity}.pixels[1]")
        if not low <= a <= high or not low <= b <= high:
            raise ValueError(f"{label}.{identity} pixels lie outside its axis anchors")
        if math.isclose(a, b, rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"{label}.{identity} opening has zero pixel span")
        mapped = sorted(((a - p0) * length / (p1 - p0),
                         (b - p0) * length / (p1 - p0)))
        if not all(math.isfinite(value) for value in mapped):
            raise ValueError(f"{label}.{identity} mapped span is not finite")
        normalized.append({"id": identity, "pixels": [a, b], "metres": mapped,
                           "centre_m": sum(mapped) / 2})
    return {"axis_anchors": [[p0, 0.0], [p1, length]], "length_m": length,
            "openings": sorted(normalized, key=lambda item: (item["centre_m"], item["id"]))}


def compare_direction(plan: list[dict], elevation: list[dict], length: float,
                      *, reverse: bool) -> dict:
    transformed = []
    for opening in elevation:
        interval = ([length - opening["metres"][1], length - opening["metres"][0]]
                    if reverse else opening["metres"][:])
        transformed.append({**opening, "compared_metres": interval,
                            "compared_centre_m": sum(interval) / 2})
    transformed.sort(key=lambda item: (item["compared_centre_m"], item["id"]))
    complete = len(plan) == len(transformed)
    # Rank pairing cannot identify a missing mark. With unequal counts, retain
    # every observation as unresolved instead of emitting misleading pairs.
    pair_count = len(plan) if complete else 0
    pairs = []
    absolute_residuals = []
    for first, second in zip(plan, transformed) if complete else ():
        residuals = [second["compared_metres"][i] - first["metres"][i] for i in (0, 1)]
        absolute_residuals.extend(abs(value) for value in residuals)
        pairs.append({"plan_id": first["id"], "elevation_id": second["id"],
                      "plan_span_m": first["metres"],
                      "elevation_span_after_direction_m": second["compared_metres"],
                      "endpoint_residual_m": residuals,
                      "max_abs_endpoint_residual_m": max(map(abs, residuals))})
    return {"direction": "elevation_reverse" if reverse else "elevation_forward",
            "complete_correspondence": complete,
            "paired_count": pair_count,
            "pairs_by_centre": pairs,
            "unpaired_plan": ([{"id": item["id"], "metres": item["metres"]}
                               for item in plan] if not complete else []),
            "unpaired_elevation": ([{"id": item["id"], "metres_after_direction": item["compared_metres"]}
                                    for item in transformed] if not complete else []),
            "max_abs_endpoint_residual_m": max(absolute_residuals) if absolute_residuals else None,
            "mean_abs_endpoint_residual_m": (sum(absolute_residuals) / len(absolute_residuals)
                                             if absolute_residuals else None)}


def compare(raw: object, ambiguity_tolerance_m: float = 0.05) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("input must be a JSON object")
    reject_nonfinite(raw)
    ambiguity_tolerance_m = finite_number(ambiguity_tolerance_m, "ambiguity tolerance")
    if ambiguity_tolerance_m < 0:
        raise ValueError("ambiguity tolerance must be finite and nonnegative")
    plan = parse_view(raw.get("plan"), "plan")
    elevation = parse_view(raw.get("elevation"), "elevation")
    length = plan["length_m"]
    if not math.isclose(length, elevation["length_m"], rel_tol=0, abs_tol=1e-9):
        raise ValueError("plan and elevation axis anchors must declare the same L")
    forward = compare_direction(plan["openings"], elevation["openings"], length, reverse=False)
    reverse = compare_direction(plan["openings"], elevation["openings"], length, reverse=True)
    complete = forward["complete_correspondence"]
    means = (forward["mean_abs_endpoint_residual_m"], reverse["mean_abs_endpoint_residual_m"])
    gap = abs(means[0] - means[1]) if all(value is not None for value in means) else None
    lower = None
    if complete and gap is not None and gap > ambiguity_tolerance_m:
        lower = "elevation_forward" if means[0] < means[1] else "elevation_reverse"
    limits = ["Pairing is by centre order only; matching IDs or drawing meaning are not inferred.",
              "A smaller residual supports an axis direction, not whether either drawing was read correctly.",
              "Absolute fit is not evaluated; inspect both directions' absolute residuals."]
    if not complete:
        limits.append("Opening counts differ: all input records are retained as unresolved; no pairs, residuals or direction are reported.")
    if gap is None:
        limits.append("No paired openings are available to distinguish direction.")
    elif gap <= ambiguity_tolerance_m:
        limits.append("Forward and reverse residuals are tied within the declared tolerance; a symmetric or nearly symmetric layout cannot orient the views.")
    return {"schema_version": "facade_span_direction_probe_v2",
            "axis_length_m": length,
            "input_counts": {"plan": len(plan["openings"]), "elevation": len(elevation["openings"])},
            "normalized_inputs": {"plan": plan, "elevation": elevation},
            "directions": {"elevation_forward": forward, "elevation_reverse": reverse},
            "direction_separation": {"mean_abs_endpoint_residual_gap_m": gap,
                                     "ambiguity_tolerance_m": ambiguity_tolerance_m,
                                     "lower_residual_direction": lower if complete else None,
                                     "relative_error_separated": bool(complete and lower),
                                     "absolute_fit_status": "not_evaluated"},
            "limits": limits}
