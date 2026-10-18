"""Collect plan-reader format errors without changing the shared compiler."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping

from jsonschema import Draft202012Validator, validators

from scripts.tool_scripts.bim_agent_guidance import REFERENCES
from src.agent.correction.schema import RoomRoleEvidence
from src.agent.geometry.plan_partition import (
    _OPENING_FIELDS, _PARTITION_FIELDS, _PLAN_FIELDS, _REQUIRED_PLAN_FIELDS, _SEED_FIELDS, _seed_role,
)
from src.agent.roles import ROOM_TYPES, normalize


# Use the actual reference example, so guidance and repair feedback cannot drift.
_REFERENCE = REFERENCES["plan_partition"]
PLAN_EXAMPLE, _ = json.JSONDecoder().raw_decode(_REFERENCE[_REFERENCE.index('{"floor_id"'):])


def _reader_example(example):
    """The same plan at image scale. The shared example's single-digit pixels read
    like metres, and a reader copied that into world-metre points (10-06 probe)."""
    def pixel(value, offset):
        value = offset + 40 * value
        return int(value) if float(value).is_integer() else value

    def point(row):
        return [pixel(row[0], 100), pixel(row[1], 80)]

    value = copy.deepcopy(example)
    value["x_anchors"] = [[pixel(px, 100), metres] for px, metres in value["x_anchors"]]
    value["y_anchors"] = [[pixel(px, 80), metres] for px, metres in value["y_anchors"]]
    value["footprint_pixels"] = [point(row) for row in value["footprint_pixels"]]
    for row in value["partitions"]:
        row["points"] = [point(item) for item in row["points"]]
    for row in value["openings"]:
        row["p1"], row["p2"] = point(row["p1"]), point(row["p2"])
    for row in value["space_seeds"]:
        row["point"] = point(row["point"])
    value["reading_type"] = "plan"
    value.pop("z_floor", None)
    value.pop("ceiling_height", None)
    for row in value["openings"]:
        row.pop("z", None)
        row.pop("source_refs", None)
    for row in value["partitions"]:
        row.pop("source_refs", None)
    value["wall_metadata"] = [
        {"target": "footprint:*", "thickness_m": 0.24,
         "evidence_status": "inferred", "basis": "Synthetic example exterior construction estimate.",
         "source_refs": ["plan.png: exterior wall band"]},
        {"target": "partition:*", "thickness_m": 0.12,
         "evidence_status": "inferred", "basis": "Synthetic example partition construction estimate.",
         "source_refs": ["plan.png: interior wall bands"]},
    ]
    value["assumptions"] = ["Synthetic dimensions only; replace example observations with this task's evidence."]
    return value


READER_PLAN_EXAMPLE = _reader_example(PLAN_EXAMPLE)


def _object(properties, required):
    return {"type": "object", "properties": properties,
            "required": sorted(required), "additionalProperties": False}


def _array(items, minimum=0, maximum=None):
    return {"type": "array", "items": items, "minItems": minimum,
            **({"maxItems": maximum} if maximum is not None else {})}


TEXT = {"type": "string", "pattern": r"\S"}
NUMBER = {"type": "number"}
STRINGS = _array(TEXT)
REFS = _array(TEXT, 1)
PROFILE = _object({"profile": {"type": "string", "pattern": r"^profile_[0-9]{3,}$"},
                   "candidate": TEXT, "at": {"enum": ["peak", "start", "end"]}},
                  {"profile", "candidate"})
PIXEL = {"anyOf": [NUMBER, PROFILE, _object({"midpoint": _array(PROFILE, 2, 2)}, {"midpoint"})]}
LENGTH = {"anyOf": [NUMBER, _object({"value": NUMBER, "unit": {"enum": ["m", "cm", "mm"]}},
                                   {"value", "unit"})]}
POINT = _array(PIXEL, 2, 2)
ANCHOR = {"type": "array", "prefixItems": [PIXEL, LENGTH], "items": False,
          "minItems": 2, "maxItems": 2}
PARTITION = _object({"id": TEXT, "points": _array(POINT, 2), "source_refs": REFS}, _PARTITION_FIELDS)
PARTITION["required"] = [field for field in PARTITION["required"] if field != "source_refs"]
SEED = _object({"id": TEXT, "point": POINT, "role": TEXT, "source_refs": STRINGS,
                "role_evidence": RoomRoleEvidence.model_json_schema()}, {"id", "point"})
OPENING = _object({"id": TEXT, "kind": {"enum": ["window", "door", "open"]},
                   "p1": POINT, "p2": POINT, "z": _array(LENGTH, 2, 2), "source_refs": REFS,
                   "state": {"enum": [None, "unknown", "open", "closed"]}, "assumptions": STRINGS},
                  {"id", "kind", "p1", "p2", "z", "source_refs"})
POSITIVE_NUMBER = {"type": "number", "exclusiveMinimum": 0}
DIMENSION_CHAIN = _object({
    "id": TEXT,
    "axis": {"enum": ["x", "y"]},
    "segments_mm": _array(POSITIVE_NUMBER, 1),
    "total_mm": POSITIVE_NUMBER,
    "tick_pixels": _array(NUMBER, 2),
    "start_world_m": NUMBER,
    "source_refs": REFS,
}, {"id", "axis", "segments_mm", "tick_pixels", "source_refs"})
PLAN_FORMAT = _object({
    "reading_type": {"const": "plan"}, "wall_metadata": {"type": "array", "items": {"type": "object"}},
    "floor_id": TEXT, "z_floor": LENGTH, "ceiling_height": LENGTH, "basis": TEXT,
    "x_anchors": _array(ANCHOR, 2, 2), "y_anchors": _array(ANCHOR, 2, 2),
    "footprint_pixels": _array(POINT, 4), "partitions": _array(PARTITION),
    "space_seeds": _array(SEED), "openings": _array(OPENING),
    "assumptions": STRINGS, "unresolved": STRINGS,
    # dimension_chains is a reader-trial input.  It is checked and consumed
    # before strict compilation; it never becomes an extra modeling language.
    "dimension_chains": _array(DIMENSION_CHAIN),
    # Q1 kernel owns the strict nested validation.  The reader format admits
    # these inert audit/priority records so an aligned draft can be replayed.
    "regularization_inputs": {"type": "object"},
    "regularization": {"type": "object"},
}, _REQUIRED_PLAN_FIELDS - {"z_floor", "ceiling_height", "reading_type", "wall_metadata"})
PLAN_FORMAT["allOf"] = [{"if": {"required": ["reading_type"], "properties": {"reading_type": {"const": "plan"}}},
    "else": {"required": ["z_floor", "ceiling_height"]}}]
OPENING["required"] = [field for field in OPENING["required"] if field not in {"z", "source_refs"}]
assert set(PLAN_FORMAT["properties"]) == set(_PLAN_FIELDS) | {"dimension_chains"}
assert set(OPENING["properties"]) == _OPENING_FIELDS
assert set(SEED["properties"]) == _SEED_FIELDS

_Validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    "number", lambda checker, value: isinstance(value, (int, float))
    and not isinstance(value, bool) and math.isfinite(value)))


def _path(parts):
    return "plan" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)


def plan_format_errors(plan):
    """Check every entry and alias; never infer missing fields or repair geometry."""
    value = copy.deepcopy(plan)
    errors = []
    if isinstance(value, Mapping):
        for collection, field in (("partitions", "points"), ("space_seeds", "point")):
            rows = value.get(collection, [])
            for index, row in enumerate(rows if isinstance(rows, list) else []):
                if isinstance(row, dict) and "pixels" in row:
                    if field in row:
                        errors.append({"path": f"plan.{collection}[{index}]",
                                       "message": f"both pixels and {field} supplied; use only {field}"})
                        row.pop("pixels")
                    else:
                        row[field] = row.pop("pixels")
    for error in _Validator(PLAN_FORMAT).iter_errors(value):
        errors.append({"path": _path(error.absolute_path), "message": error.message})
    if isinstance(value, Mapping):
        if value.get("reading_type") != "plan":
            for i, row in enumerate(value.get("openings", []) if isinstance(value.get("openings"), list) else []):
                if isinstance(row, Mapping) and "z" not in row:
                    errors.append({"path": f"plan.openings[{i}].z", "message": "required for a 3D plan; use reading_type='plan' for a 2D reading"})
        if "wall_metadata" in value:
            from src.agent.geometry.reader_wall_metadata import validate_wall_metadata
            try:
                # Face aliases depend on numeric geometry. Quantities and
                # selected profile pixels are resolved by the trial before
                # saved products/source bindings run strict wall validation.
                validate_wall_metadata(value, resolve_reference_sides=False)
            except (ValueError, TypeError, KeyError) as error:
                errors.append({"path": "plan.wall_metadata", "message": str(error)})
    if isinstance(value, Mapping):
        for index, chain in enumerate(value.get("dimension_chains", [])
                                      if isinstance(value.get("dimension_chains", []), list) else []):
            if isinstance(chain, Mapping):
                segments = chain.get("segments_mm")
                ticks = chain.get("tick_pixels")
                if isinstance(segments, list) and isinstance(ticks, list) and len(ticks) != len(segments) + 1:
                    errors.append({
                        "path": f"plan.dimension_chains[{index}].tick_pixels",
                        "message": "must contain one tick more than segments_mm",
                    })
    if isinstance(value, Mapping):
        for collection in ("partitions", "openings", "space_seeds"):
            seen = set()
            rows = value.get(collection, [])
            for index, row in enumerate(rows if isinstance(rows, list) else []):
                if not isinstance(row, Mapping):
                    continue
                identity = row.get("id")
                if isinstance(identity, str):
                    if identity in seen:
                        errors.append({"path": f"plan.{collection}[{index}].id", "message": f"duplicate id {identity!r}"})
                    seen.add(identity)
                if collection == "openings" and row.get("state") is not None:
                    if row.get("kind") == "window" or (row.get("kind") == "open" and row["state"] != "open"):
                        errors.append({"path": f"plan.openings[{index}].state",
                                       "message": "window state must be omitted; open passage state must be open or omitted"})
                role = row.get("role") if collection == "space_seeds" else None
                if isinstance(role, str) and (normalize(role) or "unknown") not in ROOM_TYPES:
                    errors.append({"path": f"plan.space_seeds[{index}].role",
                                   "message": f"{role!r} is not a room_types code; choose from the full shared table "
                                              "in the reader guide or get_bim_reference('room_types')"})
                if collection == "space_seeds" and "role_evidence" in row:
                    try:
                        _seed_role(row, seed_id=identity)
                    except (TypeError, ValueError) as error:
                        errors.append({"path": f"plan.space_seeds[{index}].role_evidence",
                                       "message": str(error)})
        errors.extend(_points_outside_footprint(value))
    return errors


def _pixel_point(value):
    return (isinstance(value, list) and len(value) == 2 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value))


def _points_outside_footprint(plan):
    """Name objects whose numeric points fall clearly outside the footprint's pixel box.

    Plan points are original pixels; a divider written in world metres lands near the
    image origin. Profile references are not resolved here and are skipped.
    """
    ring = plan.get("footprint_pixels")
    if not isinstance(ring, list) or len(ring) < 3 or not all(_pixel_point(row) for row in ring):
        return []
    xs, ys = [row[0] for row in ring], [row[1] for row in ring]
    margin = 0.05 * max(max(xs) - min(xs), max(ys) - min(ys), 1)
    left, top, right, bottom = min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin
    found = []
    for collection, fields in (("partitions", ("points",)), ("openings", ("p1", "p2")), ("space_seeds", ("point",))):
        rows = plan.get(collection)
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, Mapping):
                continue
            points = []
            for field in fields:
                value = row.get(field)
                points.extend(value if field == "points" and isinstance(value, list) else [value])
            outside = [point for point in points if _pixel_point(point)
                       and not (left <= point[0] <= right and top <= point[1] <= bottom)]
            if outside:
                found.append({"path": f"plan.{collection}[{index}]",
                              "message": f"points {outside[:2]} lie outside the footprint pixels "
                                         f"(x {min(xs):g}-{max(xs):g}, y {min(ys):g}-{max(ys):g}); plan points are "
                                         "original-image pixels, never world metres"})
    return found


def format_failure(errors):
    return {"status": "failed", "source_geometry_ready": False, "error_type": "plan_format",
            "format_errors": errors, "reason": f"{len(errors)} plan format problems; correct all listed fields",
            "repair_hint": {"example": copy.deepcopy(READER_PLAN_EXAMPLE),
                            "note": "Complete minimum format (plan_partition example at image scale); synthetic values are not observations."}}


def declaration_editability(plan):
    """Local editing needs stable object identities, not valid compiled geometry.

    Missing/invalid values remain compiler errors. Ambiguous identities or
    contradictory aliases require a complete replacement; never guess a row.
    """
    from src.agent.geometry.plan_input import normalize_plan_fields

    try:
        normalized, _ = normalize_plan_fields(dict(plan))
        json.dumps(normalized, allow_nan=False)
    except (ValueError, TypeError, KeyError):
        return {"editable": False, "reason": "Declaration is not unambiguous finite JSON; supply a corrected complete plan."}
    if not isinstance(normalized.get("floor_id"), str) or not normalized["floor_id"].strip():
        return {"editable": False, "reason": "A stable floor_id is required before local edits."}
    if set(normalized) - set(PLAN_FORMAT["properties"]):
        return {"editable": False, "reason": "Unknown top-level fields require a corrected complete plan."}
    for field in ("partitions", "openings", "space_seeds"):
        rows = normalized.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("id"), str) or not row["id"].strip() for row in rows):
            return {"editable": False, "reason": f"{field} needs a list of objects with stable IDs."}
        if len({row["id"] for row in rows}) != len(rows):
            return {"editable": False, "reason": f"{field} has duplicate IDs; local edits would be ambiguous."}
    return {"editable": True,
            "reason": "Saved declaration supports explicit local edits; geometry and submission still require a passed trial."}


PLAN_NOTE_FIELDS = frozenset({"basis", "assumptions", "unresolved"})
ROW_NOTE_FIELDS = frozenset({"source_refs", "assumptions"})


def audit_plan_replacement(before, after, *, allowed_targets=None):
    """Check full-plan and operation edits by their actual effect, with one scope rule.

    Notes remain in the audit and the hashed plan. They do not consume a pointed
    geometry target, and cannot authorize a coordinate, topology or role change.
    """
    collections = ("partitions", "openings", "space_seeds")
    if before["floor_id"] != after["floor_id"]:
        raise ValueError("a plan revision cannot change floor_id")
    changes, notes, unchanged_ids = [], [], {}

    def compare(field, identity, old, new):
        if old == new:
            return
        item = "plan." + field + (":" + identity if identity is not None else "")
        row = {"item": item, "field": field, "id": identity,
               "before": copy.deepcopy(old), "after": copy.deepcopy(new)}
        note_only = field in PLAN_NOTE_FIELDS
        if identity is not None and isinstance(old, dict) and isinstance(new, dict):
            note_only = ({key: value for key, value in old.items() if key not in ROW_NOTE_FIELDS}
                         == {key: value for key, value in new.items() if key not in ROW_NOTE_FIELDS})
        if note_only:
            notes.append(row)
        else:
            if allowed_targets is not None and item not in allowed_targets:
                raise ValueError("Rework changed items outside the coordinator's pointed issues: " + item)
            changes.append(row)

    for field in sorted(set(before) | set(after)):
        if field not in collections:
            compare(field, None, before.get(field), after.get(field))
            continue
        old = {row["id"]: row for row in before.get(field, [])}
        new = {row["id"]: row for row in after.get(field, [])}
        unchanged_ids[field] = [identity for identity in old if old[identity] == new.get(identity)]
        for identity in sorted(set(old) | set(new)):
            compare(field, identity, old.get(identity), new.get(identity))
    return {"actual_changes": changes, "annotation_changes": notes,
            "unchanged_ids": unchanged_ids,
            "unchanged_fields": [key for key in before if before[key] == after.get(key)],
            "scope": "Actual declaration changes; notes are retained separately. Geometry and topology still require a successful trial."}
