"""Local wall-face evidence and dimension conversion; no fitting or geometry edits.

Offsets use the positive world coordinate normal to an orthogonal wall, not
the room's outward normal. A thickness alone never implies centred faces.
"""
from __future__ import annotations

import copy
import math


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _refs(value):
    if not isinstance(value, list) or not value:
        raise ValueError("source_refs must be nonempty")
    return [_text(v, "source_ref") for v in value]


def resolve_wall_references(source: dict, records: list[dict]) -> list[dict]:
    """Bind optional local evidence to actual source walls and congruent sides.

    Component scope requires congruent sides; explicit boundary scope never
    propagates evidence across partial contacts. Explicit enclosure declarations
    remain unsupported. Missing offsets stay unknown even with total width.
    """
    if not isinstance(records, list):
        raise ValueError("wall_references must be a list")
    if records and source.get("schema_version") != "source_bim_v2":
        raise ValueError("wall references with explicit enclosure declarations are unsupported")
    boundaries = {b["id"]: b for b in source["boundaries"]}
    spaces = {s["id"]: s for s in source["spaces"]}
    seen, used, result = set(), set(), []
    allowed = {"id", "boundary_id", "offsets_m", "thickness_m", "reference_basis",
               "thickness_scope", "evidence_status", "source_refs", "host_scope", "reference_side"}
    for record in records:
        if not isinstance(record, dict) or set(record) - allowed:
            raise ValueError("unknown wall reference fields")
        row = copy.deepcopy(record)
        identity = _text(row.get("id"), "wall id")
        if identity in seen:
            raise ValueError("duplicate wall reference id")
        seen.add(identity)
        boundary = boundaries.get(row.get("boundary_id"))
        if not boundary or boundary["geometry_type"] != "wall" or boundary["kind"] != "physical":
            raise ValueError("wall reference requires a physical source wall boundary")
        vertices = boundary["vertices"]
        axes = [i for i in (0, 1) if max(p[i] for p in vertices) - min(p[i] for p in vertices) < 1e-8]
        if len(axes) != 1:
            raise ValueError("wall reference requires an orthogonal wall")
        axis = axes[0]
        endpoints = sorted({tuple(p[:2]) for p in vertices})
        if len(endpoints) != 2:
            raise ValueError("wall reference requires a straight rectangular wall")
        scope = row.get("host_scope", "component")
        if scope not in {"component", "boundary"}:
            raise ValueError("wall reference host_scope must be component or boundary")
        if "reference_side" in row and row["reference_side"] not in {
                "centerline", "negative_face", "positive_face", "unknown"}:
            raise ValueError("reference_side must use the fixed world normal or be unknown")
        ids = [boundary["id"]] if scope == "boundary" else [boundary["id"], *boundary.get("counterpart_ids", [])]
        for bid in ids:
            other = boundaries[bid]
            if {tuple(p) for p in other["vertices"]} != {tuple(p) for p in vertices}:
                raise ValueError("partial wall contacts require explicit segment support")
            if bid in used:
                raise ValueError("duplicate evidence for the same physical wall; use one reference")
        used.update(ids)
        offsets = row.get("offsets_m")
        thickness = row.get("thickness_m")
        if thickness is not None and _number(thickness, "thickness_m") <= 0:
            raise ValueError("thickness_m must be positive or null")
        if offsets is not None:
            if not isinstance(offsets, list) or len(offsets) != 2:
                raise ValueError("offsets_m must be [negative-side, positive-side] or null")
            lo, hi = [_number(v, "offset") for v in offsets]
            if lo >= hi:
                raise ValueError("wall face offsets must be strictly increasing")
            if thickness is not None and not math.isclose(hi-lo, thickness, abs_tol=1e-8):
                raise ValueError("face offsets disagree with declared thickness")
            row["thickness_m"] = round(hi-lo, 9)
        for field in ("reference_basis", "thickness_scope"):
            _text(row.get(field), field)
        if row.get("evidence_status") not in {"observed", "inferred", "unknown"}:
            raise ValueError("evidence_status must be observed, inferred or unknown")
        row["source_refs"] = _refs(row.get("source_refs"))
        row.update(boundary_ids=ids, floor_id=spaces[boundary["space_id"]]["floor_id"],
                   axis="xy"[axis], coordinate_m=vertices[0][axis],
                   representative_endpoints=[list(p) for p in endpoints],
                   offsets_m=offsets,
                   face_endpoints=None if offsets is None else [
                       [[p[0] + (offset if axis == 0 else 0),
                         p[1] + (offset if axis == 1 else 0)] for p in endpoints]
                       for offset in offsets])
        result.append(row)
    return result


def convert_wall_dimensions(walls: list[dict], dimensions: list[dict]) -> dict:
    """Convert labelled spans at explicit wall sides into representative spans.

    Each endpoint keeps its original-image location and semantic side. Chain
    joins compare wall AND side: two faces of one wall are not the same point.
    """
    if not isinstance(dimensions, list):
        raise ValueError("wall_dimensions must be a list")
    by_id = {w["id"]: w for w in walls}
    rows, seen, findings = [], set(), []
    allowed = {"id", "axis", "direction", "value", "unit", "start", "end", "source_refs"}
    for dimension in dimensions:
        if not isinstance(dimension, dict) or set(dimension) - allowed:
            raise ValueError("unknown wall dimension fields")
        d = copy.deepcopy(dimension)
        identity = _text(d.get("id"), "dimension id")
        if identity in seen:
            raise ValueError("duplicate dimension id")
        seen.add(identity)
        if d.get("axis") not in {"x", "y"} or type(d.get("direction")) is not int or d["direction"] not in {-1, 1}:
            raise ValueError("dimension requires world axis x/y and direction +1/-1")
        if d.get("unit") not in {"mm", "m"}:
            raise ValueError("dimension unit must be mm or m")
        value = _number(d.get("value"), "dimension value")
        if value <= 0:
            raise ValueError("dimension value must be positive")
        _refs(d.get("source_refs"))
        offsets, coordinates, floors = [], [], []
        for name in ("start", "end"):
            endpoint = d.get(name)
            if not isinstance(endpoint, dict) or set(endpoint) != {"wall_id", "side", "image", "pixel"}:
                raise ValueError("dimension endpoint needs wall_id, side, image and pixel")
            _text(endpoint["image"], "endpoint image")
            pixel = endpoint["pixel"]
            if not isinstance(pixel, list) or len(pixel) != 2 or any(_number(p, "pixel") < 0 for p in pixel):
                raise ValueError("endpoint pixel must be two nonnegative coordinates")
            wall = by_id.get(endpoint["wall_id"])
            if wall is None or wall["axis"] != d["axis"]:
                raise ValueError("endpoint wall must exist and have the dimension normal axis")
            side = endpoint["side"]
            if side not in {"representative", "negative", "positive", "unknown"}:
                raise ValueError("endpoint side must be representative, negative, positive or unknown")
            offset = None
            if side == "representative":
                offset = 0.0
            elif side != "unknown" and wall.get("offsets_m") is not None:
                offset = wall["offsets_m"][0 if side == "negative" else 1]
            offsets.append(offset)
            coordinates.append(wall["coordinate_m"])
            floors.append(wall["floor_id"])
        if floors[0] != floors[1] or d["start"]["image"] != d["end"]["image"]:
            raise ValueError("dimension endpoints must share a floor and original image")
        raw = value * (0.001 if d["unit"] == "mm" else 1)
        correction = None if None in offsets else d["direction"] * (offsets[0] - offsets[1])
        converted = None if correction is None else raw + correction
        model_span = d["direction"] * (coordinates[1] - coordinates[0])
        endpoint_world = [None if offset is None else coordinate + offset
                          for coordinate, offset in zip(coordinates, offsets)]
        if d["start"]["wall_id"] == d["end"]["wall_id"] and correction is not None:
            declared_span = d["direction"] * (offsets[1] - offsets[0])
            if declared_span <= 0:
                findings.append({"code": "same_wall_endpoint_order", "dimension_id": identity,
                    "declared_direction": d["direction"], "start_side": d["start"]["side"],
                    "end_side": d["end"]["side"], "endpoint_world_m": endpoint_world,
                    "message": "The declared sides run opposite to the dimension direction or refer to the same face. Check endpoint side labels and direction against the image; moving the wall cannot fix a same-wall span."})
            elif not math.isclose(raw, declared_span, abs_tol=1e-8):
                findings.append({"code": "same_wall_thickness_mismatch", "dimension_id": identity,
                    "raw_length_m": raw, "declared_face_span_m": declared_span,
                    "message": "The dimension and declared face offsets disagree on this one wall. Recheck their identity/units; do not move room geometry to close this span."})
        d.update(raw_length_m=round(raw, 9), endpoint_offsets_m=offsets,
                 conversion_m=None if correction is None else round(correction, 9),
                 representative_length_m=None if converted is None else round(converted, 9),
                 model_representative_length_m=round(model_span, 9),
                 residual_m=None if converted is None else round(model_span-converted, 9),
                 endpoint_world_m=endpoint_world,
                 status="unknown_basis" if correction is None else "converted_not_visually_verified")
        rows.append(d)
    joins = []
    for a, b in zip(rows, rows[1:]):
        end, start = a["end"], b["start"]
        same = (a["axis"] == b["axis"] and a["direction"] == b["direction"]
                and end["wall_id"] == start["wall_id"] and end["side"] == start["side"]
                and end["side"] != "unknown" and end["image"] == start["image"])
        joins.append({"from": a["id"], "to": b["id"], "connected": same,
                      "reason": "same declared wall and side" if same else "gap, mixed basis or unconfirmed join; no automatic closure"})
    return {"review_status": "declared_evidence_inconsistent" if findings else "not_visually_verified",
            "findings": findings, "dimensions": rows, "joins": joins,
            "raw_sum_m": round(math.fsum(r["raw_length_m"] for r in rows), 9),
            "chain_connected": bool(rows) and all(j["connected"] for j in joins),
            "scope": "caller-supplied endpoint semantics; no image verdict, calibration fitting or geometry mutation"}
