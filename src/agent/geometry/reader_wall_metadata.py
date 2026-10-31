"""Carry per-view wall thickness evidence into source BIM without moving walls.

Targets address authored footprint edges or partition polylines. Face labels use
the positive world normal (+X for a vertical plan line, +Y for a horizontal one),
independent of point order. They are observations, not permission to infer offsets.
"""
from __future__ import annotations

import copy
import math

from shapely.geometry import Polygon

from src.agent.geometry.component_attributes import thickness_record, validate_component_attributes
from src.agent.geometry.wall_reference import resolve_wall_references


_FIELDS = {"target", "thickness_m", "reference_side", "evidence_status", "basis", "source_refs"}
_INPUT_FIELDS = _FIELDS | {'expand_direction'}
_SIDES = {"centerline", "negative_face", "positive_face", "unknown"}
_EPS = 1e-7  # Floating-point identity only; never a fitting or snapping tolerance.

# Modelling candidates, not a regional building-code restriction. Explicit
# dimensions may use any positive centimetre value, including thicker walls.
WALL_THICKNESS_PRESETS_CM = (
    6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 18, 19, 20, 22, 24, 25, 26, 28,
    29, 30, 35, 36, 37, 40, 45, 49, 50, 55, 60, 62, 65, 70, 75, 80, 85,
    90, 95, 100, 110, 120, 130, 140, 150, 160, 180, 200,
)


def require_wall_thickness(plan: dict) -> dict:
    """Expand reader batches and adopt 1 cm values on every final wall."""
    from src.agent.geometry.lite_bim_regularization import quantize_m
    result = copy.deepcopy(plan)
    rows = validate_wall_metadata(result)
    missing = [r['target'] for r in rows
               if r['thickness_m'] is None or r['reference_side'] == 'unknown']
    if missing:
        raise ValueError('Every wall needs thickness and reference side; use footprint:* and '
                         'partition:* batches with inferred evidence when necessary. Missing: '
                         + ', '.join(missing))
    for row in rows:
        adopted = quantize_m(row['thickness_m'], .01)
        if adopted <= 0:
            raise ValueError('Wall thickness must remain positive on the 1 cm grid')
        row['thickness_m'] = adopted
    result['wall_metadata'] = rows
    return result


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _targets(plan):
    ring = list(plan["footprint_pixels"])
    if ring and ring[0] == ring[-1]:
        ring.pop()
    targets = {f"footprint:{i}": [(a, b)]
               for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1]))}
    defaults = list(targets)
    partitions = {}
    for partition in plan["partitions"]:
        target = f"partition:{partition['id']}"
        points = partition["points"]
        segments = list(zip(points, points[1:]))
        keys = [target, *(f"{target}:{i}" for i in range(len(segments)))]
        if set(keys).intersection(targets):
            raise ValueError("wall target IDs are ambiguous; use distinct partition IDs")
        targets[target] = segments
        for key, segment in zip(keys[1:], segments):
            targets[key] = [segment]
        partitions[target] = keys[1:]
        defaults.append(target)
    return targets, defaults, partitions


def validate_wall_metadata(plan: dict, *, resolve_reference_sides: bool = True) -> list[dict]:
    """Validate known readings and explicitly fill unprovided walls as unknown.

    ``footprint:0`` is the first authored edge (not a canonical polygon edge).
    ``partition:P01`` covers all segments, while ``partition:P01:0`` covers just
    its first segment. Whole-partition and individual-segment records cannot mix.
    Raw declaration preflight may defer face conversion while quantities and
    selected profile pixels are unresolved. Saved products and source bindings
    use the default strict conversion after the existing numeric resolvers.
    """
    targets, defaults, partitions = _targets(plan)
    records = plan.get("wall_metadata", [])
    if not isinstance(records, list):
        raise ValueError("wall_metadata must be a list")
    supplied = {}
    for record in records:
        if not isinstance(record, dict) or set(record) - _INPUT_FIELDS:
            raise ValueError("unknown wall_metadata fields")
        target = record.get("target")
        if not isinstance(target, str) or target not in {*targets, 'footprint:*', 'partition:*'}:
            raise ValueError(f"unknown wall_metadata target {target!r}")
        if target in supplied:
            raise ValueError(f"duplicate wall_metadata target {target}")
        row = copy.deepcopy(record)
        thickness = row.get("thickness_m")
        status = row.get("evidence_status", "unknown")
        if status not in {"observed", "inferred", "unknown"}:
            raise ValueError("wall thickness evidence_status must be observed, inferred or unknown")
        if thickness is None:
            if status != "unknown":
                raise ValueError("a null thickness must have unknown evidence_status")
        elif _number(thickness, "thickness_m") <= 0 or status == "unknown":
            raise ValueError("a known thickness must be positive with observed or inferred evidence_status")
        # A category-wide default extrapolates to walls that were not individually
        # observed. Keep the declaration intact; exact target overrides may be observed.
        batch_inference = target.endswith(':*') and status == 'observed'
        if batch_inference:
            status = 'inferred'
        expansion = row.get('expand_direction')
        if 'expand_direction' in row:
            if expansion not in {'up', 'down', 'left', 'right'}:
                raise ValueError('expand_direction must be up, down, left or right in the ORIGINAL image')
            if 'reference_side' in row:
                raise ValueError('Use expand_direction or reference_side, not both')
        side = row.get("reference_side", "outer_face" if target.startswith('footprint:') else "centerline")
        if side in {"outer_face", "inner_face"}:
            if not target.startswith("footprint:"):
                raise ValueError("outer_face and inner_face require a footprint target; "
                                 "partition walls have no unique building interior side")
            if resolve_reference_sides and target != 'footprint:*':
                side = _footprint_face_side(plan, target, side)
        elif side not in _SIDES:
            raise ValueError("reference_side must be centerline, negative_face, positive_face or unknown; "
                             "footprint edges also accept outer_face and inner_face")
        basis = row.get("basis", "No wall thickness was supplied for this line.")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("wall_metadata basis must be nonempty text")
        refs = row.get("source_refs", [])
        if not isinstance(refs, list) or any(not isinstance(s, str) or not s.strip() for s in refs):
            raise ValueError("wall_metadata source_refs must be a list of nonempty strings")
        if thickness is not None and ("basis" not in row or not refs):
            raise ValueError("a known thickness requires explicit basis and source_refs")
        if batch_inference:
            basis += ' Batch default extrapolated to covered walls; adopted as inferred.'
        supplied[target] = {"target": target, "thickness_m": thickness,
                            "reference_side": side, "evidence_status": status,
                            "basis": basis, "source_refs": list(dict.fromkeys(refs)),
                            **({'expand_direction': expansion} if expansion is not None else {})}
    complete = []
    for target in defaults:
        segments = partitions.get(target, [])
        has_segments = any(key in supplied for key in segments)
        for key in segments if has_segments else [target]:
            fallback = supplied.get(target, supplied.get(target.split(':')[0] + ':*'))
            row = copy.deepcopy(supplied.get(key, fallback) or {
                "target": key, "thickness_m": None, "reference_side": "unknown",
                "evidence_status": "unknown", "basis": "No wall thickness was supplied for this line.",
                "source_refs": [],
            })
            row['target'] = key
            if resolve_reference_sides and 'expand_direction' in row:
                row['reference_side'] = _image_expansion_side(plan, targets[key], row.pop('expand_direction'))
            elif resolve_reference_sides and row['reference_side'] in {'outer_face', 'inner_face'}:
                row['reference_side'] = _footprint_face_side(plan, key, row['reference_side'])
            complete.append(row)
    return copy.deepcopy(complete)


def _image_expansion_side(plan, segments, direction):
    """Convert a reader's image direction to the material side, without moving it."""
    axis = 0 if direction in {'left', 'right'} else 1
    world = _world_converter(plan, None)
    if any(_span(world(a), world(b))[0] != axis for a, b in segments):
        raise ValueError('expand_direction must be perpendicular to every selected wall segment; '
                         'use an exact segment target for a turning polyline')
    anchors = plan['x_anchors' if axis == 0 else 'y_anchors']
    (p0, v0), (p1, v1) = anchors
    positive = ((v1 - v0) / (p1 - p0)) * (1 if direction in {'right', 'down'} else -1) > 0
    return 'negative_face' if positive else 'positive_face'


def _world_converter(plan, metadata):
    calibration = (metadata or {}).get("calibration", plan)
    transforms = []
    for axis in ("x", "y"):
        anchors = calibration[f"{axis}_anchors"]
        if len(anchors) != 2:
            raise ValueError("wall metadata needs two numeric anchors per axis")
        p0, v0 = [_number(v, "anchor") for v in anchors[0]]
        p1, v1 = [_number(v, "anchor") for v in anchors[1]]
        if p0 == p1 or v0 == v1:
            raise ValueError("wall metadata anchors must span a nonzero distance")
        slope = (v1 - v0) / (p1 - p0)
        transforms.append((slope, v0 - slope * p0))
    # Match the compiler's serialized micrometre coordinates exactly. This is
    # decimal representation, not a looser matching tolerance or geometry edit.
    return lambda p: [round(transforms[i][0] * p[i] + transforms[i][1], 6) for i in (0, 1)]


def _footprint_face_side(plan, target, side):
    """Translate a perimeter-relative face into the existing world-axis label.

    Interior is on the left of every directed edge of a counterclockwise simple
    polygon, including concave edges. Determine that orientation in world space
    so reversed image axes and reversed point order need no special cases. The
    submitted plan remains the original observation; only returned metadata is
    canonicalized, beside its saved raw plan/mapping record.
    """
    ring = list(plan["footprint_pixels"])
    if ring and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3 or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in ring):
        raise ValueError("footprint face aliases require a simple nonzero polygon")
    world = _world_converter(plan, None)
    points = [world([_number(value, "footprint coordinate") for value in p]) for p in ring]
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.area <= 0:
        raise ValueError("footprint face aliases require a simple nonzero polygon")
    index = int(target.split(":")[1])
    a, b = points[index], points[(index + 1) % len(points)]
    normal, _, _, _ = _span(a, b)
    # Translating before the signed-area sum avoids cancellation at large world
    # origins. This calculation classifies a face; it never moves a coordinate.
    origin = points[0]
    signed_area = math.fsum(
        (a[0] - origin[0]) * (b[1] - origin[1]) - (b[0] - origin[0]) * (a[1] - origin[1])
        for a, b in zip(points, points[1:] + points[:1]))
    outward = (b[1] - a[1], a[0] - b[0])
    direction = outward[normal] * (1 if signed_area > 0 else -1)
    if side == "inner_face":
        direction = -direction
    return "positive_face" if direction > 0 else "negative_face"


def _span(a, b):
    axes = [i for i in (0, 1) if abs(a[i] - b[i]) <= _EPS]
    if len(axes) != 1:
        raise ValueError("wall metadata requires nonzero orthogonal segments")
    normal = axes[0]
    return normal, (a[normal] + b[normal]) / 2, min(a[1-normal], b[1-normal]), max(a[1-normal], b[1-normal])


def _covered_length(intervals):
    end, length = -math.inf, 0.0
    for lo, hi in sorted(intervals):
        if hi > end:
            length += hi - max(lo, end)
            end = hi
    return length


def wall_reading_input(plan: dict, metadata: dict | None = None) -> dict:
    """Keep a compact, reusable per-floor observation in a generated proposal."""
    rows = validate_wall_metadata(plan)
    targets, _, _ = _targets(plan)
    world = _world_converter(plan, metadata)
    segments = {row["target"]: [_span(world(a), world(b)) for a, b in targets[row["target"]]]
                for row in rows}
    # The compiler's adopted world lines are authoritative after regularization.
    for partition in (metadata or {}).get("partition_mapping", []):
        target = f"partition:{partition['partition_id']}"
        points = partition["world_points_m"]
        spans = [_span(a, b) for a, b in zip(points, points[1:])]
        if target in segments:
            segments[target] = spans
        for i, span in enumerate(spans):
            if f"{target}:{i}" in segments:
                segments[f"{target}:{i}"] = [span]
    return {"floor_id": plan["floor_id"], "walls": [dict(row, segments_m=[
        [[position if axis == 0 else start, start if axis == 0 else position],
         [position if axis == 0 else end, end if axis == 0 else position]]
        for axis, position, start, end in segments[row["target"]]]) for row in rows]}


def thickness_breakpoints(readings):
    """Only attribute changes split source walls; same-valued fragments coalesce."""
    by_floor = {}
    for reading in readings:
        segments = []
        for row in reading['walls']:
            for a, b in row['segments_m']:
                normal, coordinate, lo, hi = _span(a, b)
                segments.append((normal, coordinate, lo, hi,
                                 (row['thickness_m'], row['reference_side'], row['evidence_status'])))
        points = set()
        for i, (axis, coordinate, lo, hi, attrs) in enumerate(segments):
            for axis2, coordinate2, lo2, hi2, attrs2 in segments[i + 1:]:
                if axis != axis2 or abs(coordinate-coordinate2) > _EPS or attrs == attrs2:
                    continue
                for value in (lo, hi):
                    if lo2 - _EPS <= value <= hi2 + _EPS:
                        points.add((coordinate, value) if axis == 0 else (value, coordinate))
                for value in (lo2, hi2):
                    if lo - _EPS <= value <= hi + _EPS:
                        points.add((coordinate, value) if axis == 0 else (value, coordinate))
        by_floor[reading['floor_id']] = sorted(points)
    return by_floor


def _bind_wall_reading(source, reading):
    if not isinstance(reading, dict) or set(reading) != {"floor_id", "walls"}:
        raise ValueError("wall_readings require floor_id and walls")
    floor_id = reading["floor_id"]
    if not isinstance(floor_id, str) or not floor_id.strip():
        raise ValueError("wall_readings floor_id must be nonempty text")
    rows = reading["walls"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("wall_readings walls must be a nonempty list")
    segments = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS | {"segments_m"}:
            raise ValueError("invalid wall_readings wall record")
        target = row["target"]
        if not isinstance(target, str) or not target or target in segments:
            raise ValueError("wall_readings targets must be unique nonempty strings")
        raw_segments = row["segments_m"]
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("wall_readings segments_m must be nonempty")
        parsed = []
        for segment in raw_segments:
            if (not isinstance(segment, list) or len(segment) != 2
                    or any(not isinstance(p, list) or len(p) != 2 for p in segment)):
                raise ValueError("wall_readings segments_m need two 2D points each")
            parsed.append(_span(*[[_number(v, "wall coordinate") for v in p] for p in segment]))
        segments[target] = parsed
        thickness = row["thickness_m"]
        if row["reference_side"] not in _SIDES or row["evidence_status"] not in {"observed", "inferred", "unknown"}:
            raise ValueError("invalid wall reading side or evidence_status")
        if thickness is None:
            if row["evidence_status"] != "unknown":
                raise ValueError("a null thickness must have unknown evidence_status")
        elif _number(thickness, "thickness_m") <= 0 or row["evidence_status"] == "unknown":
            raise ValueError("a known thickness must have positive observed or inferred evidence")
        if not isinstance(row["basis"], str) or not row["basis"].strip():
            raise ValueError("wall_readings basis must be nonempty")
        refs = row["source_refs"]
        if (not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs)
                or (thickness is not None and not refs)):
            raise ValueError("known wall_readings require source_refs")
    spaces = {row["id"]: row for row in source["spaces"]}
    boundaries = {row["id"]: row for row in source["boundaries"]}
    matched, raw_references, attributes, used = set(), [], [], set()
    for boundary in source["boundaries"]:
        bid = boundary["id"]
        if (bid in used or boundary["geometry_type"] != "wall" or boundary["kind"] != "physical"
                or spaces[boundary["space_id"]]["floor_id"] != floor_id):
            continue
        points = sorted({tuple(p[:2]) for p in boundary["vertices"]})
        if len(points) != 2:
            raise ValueError(f"wall metadata requires a straight source wall: {bid}")
        axis, coordinate, lo, hi = _span(*points)
        covers = []
        intervals = []
        for row in rows:
            overlaps = [(max(lo, start), min(hi, end))
                        for normal, position, start, end in segments[row["target"]]
                        if normal == axis and abs(position-coordinate) <= _EPS
                        and min(hi, end) - max(lo, start) > _EPS]
            if overlaps:
                intervals.extend(overlaps)
                covers.append(row)
        if abs(_covered_length(intervals) - (hi-lo)) > _EPS:
            raise ValueError(f"source wall {bid} is not fully covered by this plan's wall lines")
        kinds = {(r["thickness_m"], r["reference_side"], r["evidence_status"]) for r in covers}
        if len(kinds) != 1:
            raise ValueError(f"source wall {bid} spans different wall metadata; explicit source segmentation is required")
        matched.update(r["target"] for r in covers)
        thickness, side, status = kinds.pop()
        basis = "; ".join(dict.fromkeys(r["basis"] for r in covers))
        refs = list(dict.fromkeys(ref for r in covers for ref in r["source_refs"]))
        refs.extend(f"plan-wall:{floor_id}/{r['target']}" for r in covers)
        congruent = all({tuple(p) for p in boundaries[other]["vertices"]}
                        == {tuple(p) for p in boundary["vertices"]}
                        for other in boundary.get("counterpart_ids", []))
        scope = "component" if congruent else "boundary"
        ids = [bid, *boundary.get("counterpart_ids", [])] if congruent else [bid]
        used.update(ids)
        reference = {"id": f"reader-wall:{bid}", "boundary_id": bid,
                     "offsets_m": None, "thickness_m": thickness,
                     "reference_basis": basis, "reference_side": side,
                     "thickness_scope": "entire declared plan line at this floor",
                     "evidence_status": status, "source_refs": refs}
        if scope == "boundary":
            reference["host_scope"] = "boundary"
        raw_references.append(reference)
        if thickness is not None:
            attributes.append(thickness_record(source, bid, thickness, basis=basis,
                                               source_refs=refs, host_scope=scope))
    if missing := set(segments) - matched:
        raise ValueError(f"wall metadata targets have no source wall on this floor: {sorted(missing)}")
    return {"wall_references": resolve_wall_references(source, raw_references),
            "component_attributes": validate_component_attributes(source, attributes)}


def bind_wall_readings(source: dict, readings: list[dict]) -> dict:
    """Bind saved per-floor evidence to regenerated source identities.

    Physical walls need an exact collinear cover. Different values within one
    unsplit source boundary are rejected. Partial contacts bind independently;
    no cross-floor wall recognition or geometric modification occurs.
    """
    if not isinstance(readings, list):
        raise ValueError("wall_readings must be a list")
    result = {"component_attributes": [], "wall_references": []}
    seen = set()
    for reading in readings:
        patch = _bind_wall_reading(source, reading)
        floor = reading["floor_id"]
        if floor in seen:
            raise ValueError(f"duplicate wall_readings floor {floor}")
        seen.add(floor)
        for field in result:
            result[field].extend(patch[field])
    return result


def bind_wall_metadata(source: dict, plan: dict, metadata: dict | None = None) -> dict:
    """Return a source-field patch without mutating source, plan or metadata."""
    return bind_wall_readings(source, [wall_reading_input(plan, metadata)])
