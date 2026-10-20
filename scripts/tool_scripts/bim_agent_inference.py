"""Saved architectural interpretations and deterministic source-BIM facts."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path


_BASES = {"observed", "inferred", "simplified"}
_OBJECT_KINDS = {"space", "boundary", "opening", "floor"}
_DECLARATION_FIELDS = {
    "statement", "basis", "reason", "source_refs", "object_refs", "missing_information",
}
_CENTERED_TOLERANCE_M = 1e-6
_SCOPE = (
    "Deterministic facts from the saved source_model.json only. This does not establish "
    "architectural truth, drawing fidelity, code compliance, geometry acceptance, or "
    "delivery acceptance."
)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _source(toolkit, candidate: str) -> tuple[dict, Path, str]:
    path = toolkit.candidate_path(candidate) / "source_model.json"
    raw = path.read_bytes()
    source = json.loads(raw)
    claimed = source.get("source_model_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ValueError("candidate source has no valid source_model_sha256")
    unhashed = {key: value for key, value in source.items() if key != "source_model_sha256"}
    actual = hashlib.sha256(_canonical(unhashed)).hexdigest()
    if claimed != actual:
        raise ValueError("candidate source_model.json changed without a matching source hash")
    for collection in ("spaces", "boundaries", "openings"):
        rows = source.get(collection)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"candidate source has invalid {collection}")
        ids = [row.get("id") for row in rows]
        if any(not isinstance(identity, str) or not identity for identity in ids):
            raise ValueError(f"candidate source has invalid {collection} IDs")
        if len(ids) != len(set(ids)):
            raise ValueError(f"candidate source has duplicate {collection} IDs")
    return source, path, hashlib.sha256(raw).hexdigest()


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(row, str) or not row.strip() for row in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return value


def _validate_declaration(value, source: dict | None) -> dict:
    if not isinstance(value, dict) or set(value) - _DECLARATION_FIELDS:
        raise ValueError("declaration must use statement/basis/reason/source_refs/object_refs/missing_information")
    for field in ("statement", "reason"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"declaration requires a non-empty {field}")
    if value.get("basis") not in _BASES:
        raise ValueError("basis must be observed, inferred, or simplified")
    refs = _strings(value.get("source_refs"), "source_refs")
    missing = _strings(value.get("missing_information", []), "missing_information")
    if value["basis"] == "observed" and not refs:
        raise ValueError("observed declarations require at least one source_ref")
    if value["basis"] != "observed" and not refs and not missing:
        raise ValueError("inferred/simplified declarations need source_refs or explicit missing_information")
    object_refs = value.get("object_refs", [])
    if not isinstance(object_refs, list):
        raise ValueError("object_refs must be a list")
    if object_refs and source is None:
        raise ValueError("object_refs require a candidate binding")
    inventories = {}
    if source is not None:
        inventories = {
            "space": {row["id"] for row in source["spaces"]},
            "boundary": {row["id"] for row in source["boundaries"]},
            "opening": {row["id"] for row in source["openings"]},
            "floor": ({str(row["id"]) for row in source.get("floors", [])}
                      | {str(row.get("floor_id")) for row in source["spaces"]}),
        }
    for ref in object_refs:
        if (not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                or ref.get("kind") not in _OBJECT_KINDS
                or not isinstance(ref.get("id"), str) or not ref["id"]):
            raise ValueError("each object_ref requires exactly kind and id")
        if ref["id"] not in inventories[ref["kind"]]:
            raise ValueError(f"unknown {ref['kind']} object_ref: {ref['id']}")
    return {"statement": value["statement"], "basis": value["basis"],
            "reason": value["reason"], "source_refs": refs,
            "missing_information": missing, "object_refs": object_refs}


def _save_numbered(folder: Path, stem: str, value: dict) -> Path:
    folder.mkdir(exist_ok=True)
    for number in range(1, 1000000):
        path = folder / f"{stem}_{number:03d}.json"
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"unable to allocate {stem} record")


def _prepare_declarations(toolkit, submitted: list, candidate: str | None) -> tuple[list, dict | None]:
    source = path = file_hash = None
    if candidate is not None:
        source, path, file_hash = _source(toolkit, candidate)
    declarations = []
    for index, value in enumerate(submitted):
        try:
            declarations.append(_validate_declaration(value, source))
        except ValueError as error:
            raise ValueError(f"declaration[{index}]: {error}") from error
    binding = None if source is None else {
        "candidate": candidate,
        "source_model_sha256": source["source_model_sha256"],
        "source_file_sha256": file_hash,
        "source_file": path.relative_to(toolkit.run).as_posix(),
    }
    return declarations, binding


def _write_declaration(toolkit, declaration: dict, binding: dict | None) -> dict:
    record = {"schema_version": "bim_inference_record_v1", "declaration": declaration,
              "candidate_binding": binding, "claim_scope": "model_interpretation_not_factual_truth"}
    saved = _save_numbered(toolkit.run / "inferences", "inference", record)
    record = {"inference_id": saved.stem, "record_file": saved.relative_to(toolkit.run).as_posix(), **record}
    toolkit.log("record_inference", record)
    return record


def record_inference(toolkit, declaration_json: str, candidate: str | None = None) -> dict:
    try:
        submitted = json.loads(declaration_json)
    except json.JSONDecodeError as error:
        raise ValueError(f"declaration_json is invalid JSON: {error.msg}") from error
    declarations, binding = _prepare_declarations(toolkit, [submitted], candidate)
    return _write_declaration(toolkit, declarations[0], binding)


def record_inferences(toolkit, declarations_json: str, candidate: str | None = None) -> dict:
    """Save bundled interpretations without an extra model round.

    The complete batch is validated against one current candidate binding before
    any record is created. Existing single-record identities and inspection stay
    compatible; the batch is not a quality or delivery declaration.
    """
    try:
        submitted = json.loads(declarations_json)
    except json.JSONDecodeError as error:
        raise ValueError(f"inferences_json is invalid JSON: {error.msg}") from error
    if not isinstance(submitted, list) or not submitted:
        raise ValueError("inferences_json must be a non-empty list of declarations")
    declarations, binding = _prepare_declarations(toolkit, submitted, candidate)
    records = [_write_declaration(toolkit, row, binding) for row in declarations]
    return {"schema_version": "bim_inference_batch_v1", "count": len(records),
            "candidate_binding": binding, "records": records,
            "claim_scope": "model_interpretation_not_factual_truth"}


def _binding_status(toolkit, binding) -> dict:
    if binding is None:
        return {"status": "unbound"}
    try:
        source, _, file_hash = _source(toolkit, binding["candidate"])
    except (ValueError, OSError, json.JSONDecodeError) as error:
        return {"status": "unavailable_or_invalid", "reason": str(error)}
    current = source["source_model_sha256"] == binding["source_model_sha256"]
    same_file = file_hash == binding["source_file_sha256"]
    return {"status": "current" if current and same_file else "changed",
            "source_model_sha256_matches": current, "source_file_sha256_matches": same_file}


def inspect_inference(toolkit, inference_id: str | None = None) -> dict:
    folder = toolkit.run / "inferences"
    paths = sorted(folder.glob("inference_*.json")) if folder.exists() else []
    if inference_id is not None:
        allowed = {path.stem: path for path in paths}
        if inference_id not in allowed:
            raise ValueError("unknown inference_id")
        paths = [allowed[inference_id]]
    records = []
    for path in paths:
        row = json.loads(path.read_text())
        records.append({"inference_id": path.stem, "record_file": path.relative_to(toolkit.run).as_posix(),
                        **row, "binding_status": _binding_status(toolkit, row.get("candidate_binding"))})
    result = records[0] if inference_id is not None else {
        "schema_version": "bim_inference_index_v1", "count": len(records), "records": records}
    toolkit.log("inspect_inference", {"inference_id": inference_id, "count": len(records)})
    return result


def _area(points) -> float:
    if not isinstance(points, list) or len(points) < 3:
        return 0.0
    return abs(sum(a[0] * b[1] - b[0] * a[1]
                   for a, b in zip(points, points[1:] + points[:1]))) / 2


def _range(values) -> dict | None:
    return {"min": min(values), "max": max(values)} if values else None


def _segment(vertices) -> tuple[list[float], list[float]] | None:
    points = []
    for vertex in vertices if isinstance(vertices, list) else []:
        if (isinstance(vertex, list) and len(vertex) >= 2
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in vertex[:2])):
            point = [float(vertex[0]), float(vertex[1])]
            if point not in points:
                points.append(point)
    if len(points) < 2:
        return None
    return max(((a, b) for index, a in enumerate(points) for b in points[index + 1:]),
               key=lambda pair: math.dist(*pair))


def _vertical_extent(vertices) -> tuple[float, float] | None:
    rows = vertices if isinstance(vertices, list) else []
    zs = [float(vertex[2]) for vertex in rows if isinstance(vertex, list) and len(vertex) >= 3
          and isinstance(vertex[2], (int, float)) and math.isfinite(vertex[2])]
    return (min(zs), max(zs)) if zs else None


def _door_fact(opening: dict, boundaries: dict, connections: dict) -> dict:
    result = {key: opening.get(key) for key in
              ("id", "host_boundary_id", "space_ids", "exterior", "connectivity")}
    aperture = _segment(opening.get("vertices"))
    vertical = _vertical_extent(opening.get("vertices", []))
    if vertical is not None:
        result.update(bottom_z_m=vertical[0], top_z_m=vertical[1], height_m=vertical[1] - vertical[0])
    host = _segment(boundaries.get(opening.get("host_boundary_id"), {}).get("vertices"))
    if aperture is None or host is None:
        return {**result, "geometry_status": "missing_representative_segment",
                "connection_records": connections.get(opening["id"], [])}
    a, b = aperture
    h0, h1 = host
    length = math.dist(h0, h1)
    if length == 0:
        return {**result, "geometry_status": "zero_length_host",
                "connection_records": connections.get(opening["id"], [])}
    unit = [(h1[i] - h0[i]) / length for i in range(2)]
    projections = [sum((point[i] - h0[i]) * unit[i] for i in range(2)) for point in (a, b)]
    offsets = [abs((point[0] - h0[0]) * unit[1] - (point[1] - h0[1]) * unit[0])
               for point in (a, b)]
    low, high = sorted(projections)
    midpoint = (low + high) / 2
    midpoint_distance = abs(midpoint - length / 2)
    return {**result, "geometry_status": "measured_from_saved_vertices",
            "width_m": math.dist(a, b), "endpoints_xy": [a, b],
            "host_representative_segment_xy": [h0, h1], "host_length_m": length,
            "host_end_clearance_m": [low, length - high],
            "midpoint_along_host_from_start_m": midpoint,
            "normalized_midpoint_position_from_host_start": midpoint / length,
            "midpoint_distance_from_host_midpoint_m": midpoint_distance,
            "exactly_centered_numerically": midpoint_distance <= _CENTERED_TOLERANCE_M,
            "endpoint_perpendicular_offset_m": offsets,
            "connection_records": connections.get(opening["id"], [])}


def _window_fact(opening: dict, spaces: dict) -> dict:
    result = {key: opening.get(key) for key in ("id", "host_boundary_id", "space_ids")}
    segment = _segment(opening.get("vertices"))
    vertical = _vertical_extent(opening.get("vertices", []))
    if segment is not None:
        result["width_m"] = math.dist(*segment)
    if vertical is not None:
        result.update(bottom_z_m=vertical[0], top_z_m=vertical[1], height_m=vertical[1] - vertical[0])
    sills = []
    for space_id in opening.get("space_ids", []):
        space = spaces.get(space_id)
        z_floor = space.get("z_floor") if space else None
        row = {"space_id": space_id, "source_space_z_floor_m": z_floor,
               "reference": "source_space_base_not_assumed_storey"}
        if vertical is not None and isinstance(z_floor, (int, float)) and math.isfinite(z_floor):
            row["sill_above_source_space_base_m"] = vertical[0] - z_floor
        else:
            row["sill_above_source_space_base_m"] = None
            row["status"] = "missing_source_space_or_vertical_coordinate"
        sills.append(row)
    result["sills_by_source_space"] = sills
    result["measurement_basis"] = "saved opening vertices and each referenced source space z_floor"
    return result


def _entities(source: dict) -> dict[str, dict]:
    openings = source["openings"]
    return {
        "spaces": {row["id"]: row for row in source["spaces"]},
        "boundaries": {row["id"]: row for row in source["boundaries"]},
        "openings": {row["id"]: row for row in openings if row.get("kind") != "window"},
        "windows": {row["id"]: row for row in openings if row.get("kind") == "window"},
    }


def _diff(before: dict, after: dict) -> dict:
    result = {"entity_scope": {"openings": "all non-window source openings",
                                "windows": "source openings whose kind is window"}}
    for kind, current in _entities(after).items():
        old = _entities(before)[kind]
        shared = set(old) & set(current)
        changed = {identity for identity in shared if _canonical(old[identity]) != _canonical(current[identity])}
        result[kind] = {"preserved_ids": sorted(shared - changed), "changed_ids": sorted(changed),
                        "added_ids": sorted(set(current) - set(old)),
                        "removed_ids": sorted(set(old) - set(current))}
    return result


def audit_inference_candidate(toolkit, candidate: str,
                              previous_candidate: str | None = None) -> dict:
    source, path, file_hash = _source(toolkit, candidate)
    spaces, boundaries, openings = source["spaces"], source["boundaries"], source["openings"]
    roles, floors = defaultdict(list), defaultdict(list)
    widths, depths, heights, areas = [], [], [], []
    for space in spaces:
        polygon = space.get("polygon", [])
        xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
        if xs and ys:
            widths.append(max(xs) - min(xs)); depths.append(max(ys) - min(ys)); areas.append(_area(polygon))
        if isinstance(space.get("height"), (int, float)):
            heights.append(space["height"])
        roles[str(space.get("role"))].append(space["id"])
        floors[str(space.get("floor_id"))].append(space["id"])
    window_openings = [row for row in openings if row.get("kind") == "window"]
    window_counts = Counter({space["id"]: 0 for space in spaces})
    for window in window_openings:
        for space_id in window.get("space_ids", []):
            window_counts[space_id] += 1
    space_by_id = {row["id"]: row for row in spaces}
    windows = [_window_fact(row, space_by_id) for row in window_openings]
    declared_floors = {str(row["id"]): row for row in source.get("floors", [])}
    floor_ids = sorted(set(floors) | set(declared_floors))
    membership = [{"floor_id": floor_id, "space_ids": sorted(floors[floor_id]),
                   "declared_spanning_space_ids": sorted(declared_floors.get(floor_id, {}).get(
                       "spanning_space_ids", []))} for floor_id in floor_ids]
    by_boundary = {row["id"]: row for row in boundaries}
    connection_rows = defaultdict(list)
    for row in source.get("connections", []):
        if isinstance(row, dict) and isinstance(row.get("opening_id"), str):
            connection_rows[row["opening_id"]].append(row)
    doors = [_door_fact(row, by_boundary, connection_rows)
             for row in openings if row.get("kind") == "door"]
    measured_doors = [row for row in doors if row["geometry_status"] == "measured_from_saved_vertices"]
    centered_doors = [row for row in measured_doors if row["exactly_centered_numerically"]]
    counts = {"spaces": len(spaces), "boundaries": len(boundaries), "openings": len(openings),
              "windows": len(windows), "doors": len(doors),
              "other_openings": len(openings) - len(windows) - len(doors)}
    audit = {
        "schema_version": "bim_inference_candidate_audit_v1", "candidate": candidate,
        "source_model_sha256": source["source_model_sha256"], "source_file_sha256": file_hash,
        "source_file": path.relative_to(toolkit.run).as_posix(), "scope": _SCOPE, "counts": counts,
        "space_dimension_ranges": {"bbox_x_span_m": _range(widths), "bbox_y_span_m": _range(depths),
                                   "height_m": _range(heights), "footprint_area_m2": _range(areas)},
        "room_roles": {role: {"count": len(ids), "space_ids": sorted(ids)}
                       for role, ids in sorted(roles.items())},
        "window_counts": {"by_space": dict(sorted(window_counts.items())),
                          "distribution": dict(sorted(Counter(window_counts.values()).items()))},
        "windows": windows, "floor_membership": membership, "doors": doors,
        "door_midpoint_check": {
            "numerical_tolerance_m": _CENTERED_TOLERANCE_M,
            "definition": "Opening endpoint projections have a midpoint within tolerance of the host representative segment midpoint.",
            "interpretation": "Coordinate symmetry only; not a door-placement, usability, code-compliance, or acceptance criterion and no edit is applied.",
        },
    }
    if previous_candidate is not None:
        previous, _, _ = _source(toolkit, previous_candidate)
        audit["comparison"] = {"previous_candidate": previous_candidate,
                               "previous_source_model_sha256": previous["source_model_sha256"],
                               "entities": _diff(previous, source)}
    saved = _save_numbered(toolkit.run / "inference_audits", "audit", audit)
    diff_counts = None
    if "comparison" in audit:
        diff_counts = {kind: {key.removesuffix("_ids") + "_count": len(value)
                              for key, value in rows.items()}
                       for kind, rows in audit["comparison"]["entities"].items()
                       if isinstance(rows, dict) and kind != "entity_scope"}
    summary = {
        "schema_version": "bim_inference_candidate_audit_summary_v1", "candidate": candidate,
        "source_model_sha256": source["source_model_sha256"],
        "audit_file": saved.relative_to(toolkit.run).as_posix(), "scope": _SCOPE, "counts": counts,
        "space_dimension_ranges": audit["space_dimension_ranges"],
        "room_role_counts": {role: row["count"] for role, row in audit["room_roles"].items()},
        "window_count_distribution": audit["window_counts"]["distribution"],
        "window_dimension_ranges": {
            "width_m": _range([row["width_m"] for row in windows if "width_m" in row]),
            "height_m": _range([row["height_m"] for row in windows if "height_m" in row]),
            "sill_above_source_space_base_m": _range([
                sill["sill_above_source_space_base_m"] for row in windows
                for sill in row["sills_by_source_space"]
                if sill["sill_above_source_space_base_m"] is not None]),
            "sill_reference": "each referenced source space z_floor; not an assumed storey datum",
        },
        "floor_membership_counts": [{"floor_id": row["floor_id"],
                                     "space_count": len(row["space_ids"]),
                                     "declared_spanning_space_count": len(row["declared_spanning_space_ids"])}
                                    for row in membership],
        "door_summary": {"count": len(doors), "measured_count": len(measured_doors),
                         "width_m": _range([row["width_m"] for row in measured_doors]),
                         "height_m": _range([row["height_m"] for row in doors if "height_m" in row]),
                         "host_end_clearance_m": _range([clearance for row in measured_doors
                                                          for clearance in row["host_end_clearance_m"]]),
                         "midpoint_distance_from_host_midpoint_m": _range([
                             row["midpoint_distance_from_host_midpoint_m"] for row in measured_doors]),
                         "normalized_midpoint_position_from_host_start": _range([
                             row["normalized_midpoint_position_from_host_start"] for row in measured_doors]),
                         "midpoint_measured_count": len(measured_doors),
                         "exactly_centered_count": len(centered_doors),
                         "exactly_centered_id_sample": sorted(row["id"] for row in centered_doors)[:6],
                         "exactly_centered_sample_truncated": len(centered_doors) > 6,
                         "exactly_centered_numerical_tolerance_m": _CENTERED_TOLERANCE_M,
                         "midpoint_interpretation": "Numerical coordinate symmetry only; not an architectural acceptance threshold or automatic edit.",
                         "connectivity": dict(sorted(Counter(str(row.get("connectivity"))
                                                             for row in doors).items()))},
        "comparison_counts": diff_counts,
    }
    toolkit.log("audit_inference_candidate", summary)
    return summary


def register_inference_tools(server, toolkit):
    """Register inference records/audits only in the coordinator's writable run."""
    if toolkit.readonly:
        return

    @server.tool()
    def record_inference(declaration_json: str, candidate: str | None = None) -> dict:
        """Save one immutable observed/inferred/simplified architectural interpretation.

        This records model reasoning with explicit evidence gaps; it does not certify truth.
        Object references require a candidate and must name actual saved source objects.
        """
        return globals()["record_inference"](toolkit, declaration_json, candidate)

    @server.tool()
    def inspect_inference(inference_id: str | None = None) -> dict:
        """Read one saved inference or list all records, including current binding status."""
        return globals()["inspect_inference"](toolkit, inference_id)

    @server.tool()
    def audit_inference_candidate(candidate: str, previous_candidate: str | None = None) -> dict:
        """Save deterministic source quantities, dimensions, membership and opening facts.

        The bounded response points to the complete audit. Supplying previous_candidate also
        records exact preserved/changed/added/removed source entity IDs. No thresholds or
        code-compliance, drawing-truth, geometry-acceptance, or delivery verdicts are applied.
        """
        return globals()["audit_inference_candidate"](toolkit, candidate, previous_candidate)
