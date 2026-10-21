"""Apply explicit enclosure observations to a frozen source BIM.

The source BIM deliberately keeps logical space volumes closed.  This module
adds observations about whether a logical boundary is actually physical,
open, or unknown without reconstructing geometry or introducing solver
semantics.  It only accepts the v2 source contract and writes v3, so consumers
which have not implemented the distinction cannot silently treat an open side
as a solid wall.
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict

from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry.openings import _frame, _lift, _project
from src.agent.geometry.source_bim import EPS, _on_wall, _parts
from src.agent.geometry.source_model import _digest


_INPUT_SCHEMA = "source_enclosure_input_v1"
_SOURCE_SCHEMA = "source_bim_v2"
_OUTPUT_SCHEMA = "source_bim_v3"
_EVIDENCE_KINDS = {"observed", "manual_annotation", "example"}
_SPACE_ENCLOSURES = {"enclosed", "semi_open", "open"}
_CONDITIONS = {"open", "unknown"}


def _fail(message: str) -> None:
    raise ValueError(f"source enclosure: {message}")


def _digest_matches(source: dict) -> bool:
    return source.get("source_model_sha256") == _digest(
        {key: value for key, value in source.items() if key != "source_model_sha256"}
    )


def _strings(value, *, field: str, target: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(row, str) or not row.strip() for row in value):
        _fail(f"{target}: {field} must be a list of nonblank strings")
    return list(value)


def _evidence(row: dict, *, target: str) -> dict:
    refs = _strings(row.get("source_refs", []), field="source_refs", target=target)
    assumptions = _strings(row.get("assumptions", []), field="assumptions", target=target)
    if not refs and not assumptions:
        _fail(f"{target}: requires source_refs or assumptions")
    kind = row.get("evidence_kind")
    if kind not in _EVIDENCE_KINDS:
        _fail(f"{target}: evidence_kind must be observed, manual_annotation, or example")
    return {"source_refs": refs, "assumptions": assumptions, "evidence_kind": kind}


def _vertices(value, *, target: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) < 3:
        _fail(f"{target}: vertices must contain at least three 3D points")
    result = []
    for point in value:
        if (not isinstance(point, (list, tuple)) or len(point) != 3
                or not all(isinstance(axis, (int, float)) and math.isfinite(axis) for axis in point)):
            _fail(f"{target}: vertices must contain finite [x, y, z] points")
        result.append([float(axis) for axis in point])
    return result


def _axis_aligned(shape: Polygon) -> bool:
    rings = [shape.exterior, *shape.interiors]
    return all(
        abs(a[0] - b[0]) <= EPS or abs(a[1] - b[1]) <= EPS
        for ring in rings
        for a, b in zip(ring.coords, list(ring.coords)[1:])
    )


def _boundary_frame(boundary: dict, *, allow_nonorthogonal=False):
    """Return projection/lift functions for one supported source plane.

    Wall coordinates remain ``(distance along wall, z)``.  Horizontal floor
    and ceiling coordinates are their world ``(x, y)`` values, so applying an
    enclosure declaration never changes or closes the logical source volume.
    """
    vertices = boundary["vertices"]
    geometry_type = boundary.get("geometry_type")
    if geometry_type == "wall":
        if len(vertices) < 4:
            _fail(f"boundary {boundary.get('id')}: wall must have a complete vertical parent polygon")
        origin = vertices[0][:2]
        direction, _ = _frame(origin, vertices[1][:2])
        project = lambda ring: _on_wall(ring, _DictBoundary(boundary))
        lift = lambda ring: _lift(list(ring), origin, direction)
        plane_name = "wall"
    elif geometry_type in {"floor", "ceiling"}:
        if len(vertices) < 3:
            _fail(f"boundary {boundary.get('id')}: floor/ceiling must have a complete horizontal parent polygon")
        z = vertices[0][2]

        def project(ring):
            if (len(ring) < 3 or
                    any(abs(point[2] - z) > EPS for point in ring)):
                return None
            return Polygon([point[:2] for point in ring])

        lift = lambda ring: [[x, y, z] for x, y in ring]
        plane_name = "floor/ceiling"
    else:
        _fail(f"boundary {boundary.get('id')}: unsupported geometry type {geometry_type!r}")

    parent = project(vertices)
    if boundary.get('holes'):
        holes = [project(hole) for hole in boundary['holes']]
        if any(hole is None for hole in holes):
            _fail(f"boundary {boundary.get('id')}: invalid interior ring plane")
        parent = parent.difference(unary_union(holes))
    if parent is None or not parent.is_valid or parent.area <= EPS ** 2:
        _fail(f"boundary {boundary.get('id')}: invalid {plane_name} parent plane")
    if not allow_nonorthogonal and not _axis_aligned(parent):
        _fail(f"boundary {boundary.get('id')}: only orthogonal {plane_name} planes are supported")
    return {"project": project, "lift": lift, "parent": parent, "plane_name": plane_name,
            "allow_nonorthogonal": allow_nonorthogonal}


class _DictBoundary:
    """The source helpers only need ``vertices`` for enclosure dicts."""

    def __init__(self, row: dict):
        self.vertices = row["vertices"]


def _shape_from_vertices(vertices: list[list[float]], frame: dict, *, target: str) -> Polygon:
    shape = frame["project"](vertices)
    if shape is None or not shape.is_valid or shape.area <= EPS ** 2:
        _fail(f"{target}: vertices must form a nonempty planar polygon on its source {frame['plane_name']}")
    if not frame["allow_nonorthogonal"] and not _axis_aligned(shape):
        _fail(f"{target}: only orthogonal {frame['plane_name']}-plane regions are supported")
    if not frame["parent"].buffer(EPS).covers(shape):
        _fail(f"{target}: region lies outside its source boundary")
    return shape


def _lift_shape(shape, frame):
    """Turn a local boundary polygon into its world-coordinate representation."""
    rows = []
    for part in _parts(shape):
        rows.append({
            "vertices": [list(point) for point in frame["lift"](list(part.exterior.coords)[:-1])],
            "holes": [[list(point) for point in frame["lift"](list(ring.coords)[:-1])]
                      for ring in part.interiors],
        })
    return rows


def _opening_shapes(source: dict, boundary: dict, frame: dict) -> list[Polygon]:
    bid = boundary["id"]
    hosts = source.get("opening_hosts", {})
    result = []
    for opening in source.get("openings", []):
        if bid not in hosts.get(opening.get("id"), [opening.get("host_boundary_id")]):
            continue
        shape = frame["project"](opening["vertices"])
        if shape is None or not frame["parent"].buffer(EPS).covers(shape):
            _fail(f"boundary {bid}: source opening {opening.get('id')} lacks a valid host polygon")
        result.append(shape)
    return result


def _relation_regions(source: dict, boundary_id: str):
    for relation in source.get("boundary_relations", []):
        ids = relation.get("boundary_ids", [])
        if boundary_id not in ids or len(ids) != 2:
            continue
        other = ids[1] if ids[0] == boundary_id else ids[0]
        yield other, relation.get("regions", [])


def _region_record(entry: dict) -> dict:
    return {
        "condition": entry["condition"],
        "vertices": entry["vertices"],
        **({'holes': entry['holes']} if entry.get('holes') else {}),
        "source_refs": entry["source_refs"],
        "assumptions": entry["assumptions"],
        "evidence_kind": entry["evidence_kind"],
    }


def _declared_open_shapes(entries: list[dict]):
    return unary_union([entry["shape"] for entry in entries if entry["condition"] == "open"])


def _map_boundary_part(part: Polygon, frame: dict, other: dict) -> tuple[dict, Polygon]:
    """Map one planar polygon, including its holes, to a counterpart boundary."""
    rows = _lift_shape(part, frame)
    if len(rows) != 1:
        _fail(f"boundary {other['id']}: unable to preserve shared-region geometry")
    row = rows[0]
    other_frame = _boundary_frame(other, allow_nonorthogonal=frame["allow_nonorthogonal"])
    exterior = other_frame["project"](row["vertices"])
    holes = [other_frame["project"](ring) for ring in row["holes"]]
    if exterior is None or any(hole is None for hole in holes):
        _fail(f"boundary {other['id']}: counterpart does not share the declared open plane")
    mapped = Polygon(list(exterior.exterior.coords), [list(hole.exterior.coords) for hole in holes])
    if not mapped.is_valid or mapped.area <= EPS ** 2:
        _fail(f"boundary {other['id']}: counterpart open geometry is invalid")
    return row, mapped


def _validate_reciprocal_openings(source: dict, entries_by_boundary: dict[str, list[dict]], boundaries: dict[str, dict]) -> list[dict]:
    """Require each open shared patch to be declared open on both room sides.

    A source relation is the geometric proof that a patch connects a neighbour.
    Any remaining part of an open region is thereby proven exterior.  Unknown
    regions never take part in this relation and cannot establish connectivity.
    """
    connections = []
    for bid, entries in entries_by_boundary.items():
        open_shape = _declared_open_shapes(entries)
        if open_shape.is_empty:
            continue
        boundary = boundaries[bid]
        frame = _boundary_frame(boundary, allow_nonorthogonal=source.get("geometry_capability", {}).get("profile") == "simple_polygon")
        contact_shapes = []
        for other_id, regions in _relation_regions(source, bid):
            other = boundaries.get(other_id)
            if other is None:
                _fail(f"boundary {bid}: relation names unknown counterpart {other_id}")
            other_open = _declared_open_shapes(entries_by_boundary.get(other_id, []))
            for region in regions:
                exterior = frame["project"](region.get("vertices", []))
                holes = [frame["project"](ring) for ring in region.get("holes", [])]
                if exterior is None or any(hole is None for hole in holes):
                    _fail(f"boundary {bid}: relation contains invalid contact geometry")
                patch = Polygon(
                    list(exterior.exterior.coords),
                    [list(hole.exterior.coords) for hole in holes],
                )
                if (not patch.is_valid or patch.area <= EPS ** 2
                        or (not frame["allow_nonorthogonal"] and not _axis_aligned(patch))):
                    _fail(f"boundary {bid}: relation contains invalid contact geometry")
                contact_shapes.append(patch)
                shared_open = open_shape.intersection(patch)
                for part in _parts(shared_open):
                    # Map the exact overlap through world coordinates before
                    # checking the counterpart's declared local-plane polygon.
                    # A union of separate partial regions can contain an inner
                    # ring; that hole is real physical enclosure and must survive.
                    world, other_part = _map_boundary_part(part, frame, other)
                    if not other_open.buffer(EPS).covers(other_part):
                        _fail(
                            f"boundary {bid}: open shared region requires matching open declaration on {other_id}"
                        )
                    connections.append({
                        "boundary_id": bid,
                        "space_ids": [boundary["space_id"], other["space_id"]],
                        "exterior": False,
                        "condition": "open",
                        **world,
                    })
        contacts = unary_union(contact_shapes) if contact_shapes else Polygon()
        exterior = open_shape.difference(contacts)
        for row in _lift_shape(exterior, frame):
            connections.append({
                "boundary_id": bid,
                "space_ids": [boundary["space_id"]],
                "exterior": True,
                "condition": "open",
                **row,
            })
    return connections


def apply_source_enclosure(base_source: dict, declaration: dict | None) -> dict:
    """Return a v3 source BIM with explicit real/virtual/unknown enclosure.

    ``declaration`` is a geometric observation sidecar.  Passing ``None`` is
    intentionally a no-op for callers that did not receive one; any supplied
    declaration must use ``source_enclosure_input_v1`` and bind the exact v2
    source-model digest it describes.
    """
    if not isinstance(base_source, dict):
        _fail("base source must be an object")
    if base_source.get("schema_version") != _SOURCE_SCHEMA or not _digest_matches(base_source):
        _fail("base source must be an intact source_bim_v2")
    if declaration is None:
        return copy.deepcopy(base_source)
    if not isinstance(declaration, dict) or declaration.get("schema_version") != _INPUT_SCHEMA:
        _fail("declaration must use source_enclosure_input_v1")
    if declaration.get("base_source_model_sha256") != base_source["source_model_sha256"]:
        _fail("declaration base_source_model_sha256 does not match source BIM digest")
    space_rows = declaration.get("spaces", [])
    boundary_rows = declaration.get("boundaries", [])
    if not isinstance(space_rows, list) or not isinstance(boundary_rows, list):
        _fail("declaration spaces and boundaries must be lists")
    # A declaration sidecar must make an actual observation; an empty object is
    # not a reason to change the source schema version.
    if not space_rows and not boundary_rows:
        _fail("declaration must contain a space or boundary observation")

    result = copy.deepcopy(base_source)
    spaces = {row["id"]: row for row in result.get("spaces", [])}
    boundaries = {row["id"]: row for row in result.get("boundaries", [])}
    if len(spaces) != len(result.get("spaces", [])) or len(boundaries) != len(result.get("boundaries", [])):
        _fail("base source has duplicate space or boundary identities")

    space_applications = []
    seen_spaces = set()
    for row in space_rows:
        if not isinstance(row, dict) or not isinstance(row.get("space_id"), str):
            _fail("space declaration requires space_id")
        sid = row["space_id"]
        if sid not in spaces:
            _fail(f"space declaration names unknown space {sid}")
        if sid in seen_spaces:
            _fail(f"space {sid} has more than one enclosure declaration")
        enclosure = row.get("enclosure")
        if enclosure not in _SPACE_ENCLOSURES:
            _fail(f"space {sid}: enclosure must be enclosed, semi_open, or open")
        evidence = _evidence(row, target=f"space {sid}")
        spaces[sid]["enclosure"] = enclosure
        spaces[sid]["enclosure_evidence"] = evidence
        space_applications.append({"target_type": "space", "target_id": sid,
                                   "enclosure": enclosure, **evidence})
        seen_spaces.add(sid)

    entries_by_boundary: dict[str, list[dict]] = defaultdict(list)
    for index, row in enumerate(boundary_rows):
        target = f"boundary declaration {index}"
        if not isinstance(row, dict) or not isinstance(row.get("boundary_id"), str):
            _fail(f"{target}: requires boundary_id")
        bid = row["boundary_id"]
        boundary = boundaries.get(bid)
        if boundary is None:
            _fail(f"{target}: names unknown boundary {bid}")
        frame = _boundary_frame(boundary, allow_nonorthogonal=result.get("geometry_capability", {}).get("profile") == "simple_polygon")
        condition = row.get("condition")
        if condition not in _CONDITIONS:
            _fail(f"boundary {bid}: condition must be open or unknown")
        scope = row.get("scope")
        if scope not in {"whole", "partial"}:
            _fail(f"boundary {bid}: scope must be whole or partial")
        evidence = _evidence(row, target=f"boundary {bid}")
        supplied = row.get("vertices")
        if scope == "partial":
            if supplied is None:
                _fail(f"boundary {bid}: partial declaration requires vertices")
            vertices = _vertices(supplied, target=f"boundary {bid}")
            shape = _shape_from_vertices(vertices, frame, target=f"boundary {bid}")
            if frame["parent"].symmetric_difference(shape).area <= EPS ** 2:
                _fail(f"boundary {bid}: a full-parent region must use scope whole")
        else:
            if supplied is not None:
                vertices = _vertices(supplied, target=f"boundary {bid}")
                shape = _shape_from_vertices(vertices, frame, target=f"boundary {bid}")
                if frame["parent"].symmetric_difference(shape).area > EPS ** 2:
                    _fail(f"boundary {bid}: whole declaration vertices must equal its full parent boundary")
            vertices = copy.deepcopy(boundary["vertices"])
            shape = frame["parent"]
        entries_by_boundary[bid].append({"boundary_id": bid, "condition": condition, "scope": scope,
                                         "vertices": vertices, "shape": shape,
                                         **({'holes': copy.deepcopy(boundary['holes'])}
                                            if scope == 'whole' and boundary.get('holes') else {}), **evidence})

    boundary_applications = []
    unknown_boundaries = []
    for bid, entries in entries_by_boundary.items():
        whole = [entry for entry in entries if entry["scope"] == "whole"]
        if whole and len(entries) != 1:
            _fail(f"boundary {bid}: a whole declaration cannot be combined with other regions")
        for left_index, left in enumerate(entries):
            for right in entries[left_index + 1:]:
                if left["shape"].intersection(right["shape"]).area > EPS ** 2:
                    _fail(f"boundary {bid}: enclosure regions overlap")
        boundary = boundaries[bid]
        frame = _boundary_frame(boundary, allow_nonorthogonal=result.get("geometry_capability", {}).get("profile") == "simple_polygon")
        opening_shapes = _opening_shapes(result, boundary, frame)
        for entry in entries:
            for opening in opening_shapes:
                if entry["shape"].intersection(opening).area > EPS ** 2:
                    _fail(f"boundary {bid}: enclosure region overlaps an existing window, door, or opening")
            boundary_applications.append({"target_type": "boundary", "target_id": bid,
                                          "condition": entry["condition"], "scope": entry["scope"],
                                          "vertices": entry["vertices"], "source_refs": entry["source_refs"],
                                          **({'holes': entry['holes']} if entry.get('holes') else {}),
                                          "assumptions": entry["assumptions"], "evidence_kind": entry["evidence_kind"]})
        boundary["enclosure_regions"] = [_region_record(entry) for entry in entries]
        if whole:
            boundary["enclosure"] = whole[0]["condition"]
            boundary["kind"] = "virtual" if whole[0]["condition"] == "open" else "unknown"
        else:
            boundary["enclosure"] = "mixed"
            boundary["kind"] = "physical"
        if any(entry["condition"] == "unknown" for entry in entries):
            unknown_boundaries.append(bid)

    for boundary in result["boundaries"]:
        boundary.setdefault("enclosure", "physical")
        boundary.setdefault("enclosure_regions", [])

    connections = _validate_reciprocal_openings(result, entries_by_boundary, boundaries)
    validation = result.setdefault("validation", {"status": "pass", "findings": []})
    validation.setdefault("findings", [])
    if unknown_boundaries:
        validation["findings"].append({
            "code": "source.enclosure_unknown",
            "severity": "warning",
            "boundary_ids": sorted(unknown_boundaries),
            "message": "Unknown enclosure remains unresolved and is not treated as open.",
        })
        not_evaluated = validation.setdefault("not_evaluated", [])
        marker = "unknown enclosure conditions"
        if marker not in not_evaluated:
            not_evaluated.append(marker)
    if any(row.get("severity") == "severe" for row in validation["findings"]):
        validation["status"] = "severe"
    elif unknown_boundaries:
        validation["status"] = "warning"

    result["schema_version"] = _OUTPUT_SCHEMA
    result["source_enclosure"] = {
        "schema_version": "source_enclosure_applied_v1",
        "base_source_model_sha256": base_source["source_model_sha256"],
        "declaration_sha256": _digest(declaration),
        "declaration": copy.deepcopy(declaration),
        "applications": sorted(space_applications + boundary_applications,
                               key=lambda row: (row["target_type"], row["target_id"],
                                                row.get("scope", ""), row.get("condition", ""))),
        "open_connections": connections,
    }
    from src.agent.geometry.source_vertical_continuity import bind_vertical_continuity_review
    bind_vertical_continuity_review(result)
    result["source_model_sha256"] = _digest({key: value for key, value in result.items()
                                               if key != "source_model_sha256"})
    return result
