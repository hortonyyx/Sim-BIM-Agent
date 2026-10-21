"""Deterministic, auditable local edits for direct legacy geometry proposals."""
from __future__ import annotations

import copy
import math
from types import SimpleNamespace

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.cell_geometry import cell_polygon
from src.agent.correction.schema import RoomRoleEvidence, Window, WallOpening


_PROPOSAL_FIELDS = {"geometry", "assumptions", "unresolved", "enclosure_declaration",
                    "wall_references", "wall_dimensions", "mesh_frame", "component_attributes", "wall_readings"}
_WINDOW_FIELDS = {"facade", "span", "z", "room", "floor", "assumptions"}
_OPENING_FIELDS = {"space_id", "other_space_id", "p1", "p2", "z", "state", "assumptions", "curve_segment"}
_FACADE_REFLECTIONS = {
    "x": {"East": "West", "West": "East", "North": "North", "South": "South"},
    "y": {"North": "South", "South": "North", "East": "East", "West": "West"},
}
_DIRECTION_NOTICE = (
    "Existing assumptions may contain direction language and require review; "
    "use set_notes to replace them."
)
_LOCAL_PARTITION_TOLERANCE_M = 1e-7
_LOCAL_PARTITION_AREA_TOLERANCE_M2 = 1e-7


def _require_fields(row: dict, allowed: set[str], *, operation: str) -> None:
    unknown = set(row) - allowed
    if unknown:
        raise ValueError(f"{operation}: unknown fields {sorted(unknown)}")


def _nonblank_string(value: object, *, field: str, operation: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{operation}: {field} must be a nonblank string")
    return value


def _source_refs(value: object, *, operation: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(row, str) or not row.strip() for row in value):
        raise ValueError(f"{operation}: source_refs must be a nonempty list of nonblank strings")
    return list(value)


def _notes(value: object, *, field: str, operation: str = "set_notes") -> list[str]:
    if not isinstance(value, list) or any(not isinstance(row, str) for row in value):
        raise ValueError(f"{operation}: {field} must be a list of strings")
    return list(value)


def _validate_proposal(proposal: dict) -> dict:
    if not isinstance(proposal, dict):
        raise TypeError("proposal must be an object")
    _require_fields(proposal, _PROPOSAL_FIELDS, operation="proposal")
    missing = {"geometry", "assumptions", "unresolved"} - set(proposal)
    if missing:
        raise ValueError(f"proposal: missing fields {sorted(missing)}")
    geometry = proposal["geometry"]
    if not isinstance(geometry, dict):
        raise TypeError("proposal.geometry must be an object")
    version = str(geometry.get("schema_version", "1"))
    if version not in {"1", "2", "source_polygon_v1"}:
        raise ValueError("proposal geometry must use schema_version 1, 2 or source_polygon_v1")
    _notes(proposal["assumptions"], field="assumptions")
    _notes(proposal["unresolved"], field="unresolved")
    if "mesh_frame" in proposal:
        from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
        validate_mesh_frame(proposal["mesh_frame"])
    enclosure = proposal.get("enclosure_declaration")
    if enclosure is not None and not isinstance(enclosure, dict):
        raise TypeError("proposal.enclosure_declaration must be an object")
    ensure_corrected_geometry(geometry)
    return geometry


def _unique_ids(geometry: dict) -> None:
    identities = []
    for floor in geometry.get("floors", []):
        identities.extend(("space", row.get("id")) for row in floor.get("cells", []))
    identities.extend(("opening", row.get("id")) for row in geometry.get("windows", []))
    identities.extend(("opening", row.get("id")) for row in geometry.get("openings", []))
    seen = set()
    for kind, identity in identities:
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"{kind} identity must be a nonempty string")
        if (kind, identity) in seen:
            raise ValueError(f"duplicate {kind} id: {identity}")
        seen.add((kind, identity))


def _interval_reflected(interval: list[float], midpoint: float) -> list[float]:
    if not isinstance(interval, list) or len(interval) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in interval):
        raise ValueError("reflection requires finite two-value coordinate intervals")
    return [2 * midpoint - interval[1], 2 * midpoint - interval[0]]


def _reflect_point(point: list[float], *, axis: str, midpoint: float) -> list[float]:
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError("reflection requires two-dimensional points")
    x, y = point
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (x, y)):
        raise ValueError("reflection requires finite point coordinates")
    return [2 * midpoint - x, y] if axis == "x" else [x, 2 * midpoint - y]


def _reflect(geometry: dict, *, axis: str, reason: str) -> dict:
    if axis not in {"x", "y"}:
        raise ValueError("reflect: axis must be x or y")
    interval_name = f"footprint_{axis}"
    interval = geometry.get(interval_name)
    if not isinstance(interval, list) or len(interval) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in interval):
        raise ValueError(f"reflect: {interval_name} must contain two finite coordinates")
    midpoint = (interval[0] + interval[1]) / 2
    geometry[interval_name] = _interval_reflected(interval, midpoint)
    for floor in geometry.get("floors", []):
        if floor.get("footprint"):
            raise ValueError("reflect: nonempty floor.footprint requires an explicit per-floor footprint edit")
        for cell in floor.get("cells", []):
            if cell.get("vertical_sections"):
                raise ValueError("reflect: vertical_sections requires an explicit revised build_bim proposal")
            cell[axis] = _interval_reflected(cell[axis], midpoint)
            polygon = cell.get("polygon")
            if polygon is not None:
                reflected = [_reflect_point(point, axis=axis, midpoint=midpoint) for point in polygon]
                # A reflection reverses winding. Keep the original ring orientation
                # and its first vertex's identity stable for downstream references.
                cell["polygon"] = reflected[:1] + list(reversed(reflected[1:]))
    for window in geometry.get("windows", []):
        facade = Window.model_validate(window).facade
        if (axis == "x" and facade in {"North", "South"}) or (axis == "y" and facade in {"East", "West"}):
            window["span"] = _interval_reflected(window["span"], midpoint)
        window["facade"] = _FACADE_REFLECTIONS[axis][facade]
    for opening in geometry.get("openings", []):
        opening["p1"] = _reflect_point(opening["p1"], axis=axis, midpoint=midpoint)
        opening["p2"] = _reflect_point(opening["p2"], axis=axis, midpoint=midpoint)
    return {
        "operation": "reflect",
        "reason": reason,
        "transform": {"axis": axis, "about_world_coordinate": midpoint},
        "notice": _DIRECTION_NOTICE,
    }


def _find(rows: list[dict], identity: str, *, operation: str) -> dict:
    matches = [row for row in rows if row.get("id") == identity]
    if len(matches) != 1:
        raise ValueError(f"{operation}: expected exactly one existing id {identity!r}, found {len(matches)}")
    return matches[0]


def _update(geometry: dict, operation: dict, *, target: str) -> dict:
    name = operation["op"]
    _require_fields(operation, {"op", "id", "changes", "reason", "source_refs"}, operation=name)
    identity = _nonblank_string(operation.get("id"), field="id", operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    changes = operation.get("changes")
    allowed = _WINDOW_FIELDS if target == "window" else _OPENING_FIELDS
    if not isinstance(changes, dict) or not changes:
        raise ValueError(f"{name}: changes must be a nonempty object")
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"{name}: unsupported changes {sorted(unknown)}")
    if "assumptions" in changes:
        _notes(changes["assumptions"], field="changes.assumptions")
    row = _find(geometry[f"{target}s"], identity, operation=name)
    before = copy.deepcopy(row)
    row.update(copy.deepcopy(changes))
    if 'curve_segment' in changes:
        if geometry.get('schema_version') != 'source_polygon_v1':
            raise ValueError('curve_segment requires source_polygon_v1')
        if changes['curve_segment'] is not None:
            for field in ('p1', 'p2'):
                if field not in changes:
                    row.pop(field, None)
            from src.agent.geometry.source_curves import expand_source_polygon_geometry
            expanded = expand_source_polygon_geometry({'schema_version': 'source_polygon_v1',
                'boundary_curves': geometry.get('boundary_curves', {}), 'openings': [row]})['openings'][0]
            row.clear()
            row.update(expanded)
    # New sources replace the edited object's active basis. The complete prior
    # record, including old sources, stays in the append-only audit below.
    row["source_refs"] = refs
    return {"operation": name, "id": identity, "reason": reason, "source_refs": refs,
            "before": before, "after": copy.deepcopy(row)}


def _remove_opening(geometry: dict, operation: dict) -> dict:
    name = "remove_opening"
    _require_fields(operation, {"op", "id", "reason", "source_refs"}, operation=name)
    identity = _nonblank_string(operation.get("id"), field="id", operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    matches = [(collection, row) for collection in ("windows", "openings")
               for row in geometry.get(collection, []) if row.get("id") == identity]
    if len(matches) != 1:
        raise ValueError(f"{name}: expected exactly one existing id {identity!r}, found {len(matches)}")
    collection, row = matches[0]
    before = copy.deepcopy(row)
    geometry[collection].remove(row)
    return {"operation": name, "id": identity, "reason": reason, "source_refs": refs,
            "target_collection": collection, "before": before, "after": None}


def _set_space_role(geometry: dict, operation: dict) -> dict:
    """Change use and its basis without replacing any physical room geometry."""
    name = "set_space_role"
    _require_fields(operation, {"op", "space_id", "role", "basis", "assumptions",
                                "reason", "source_refs"}, operation=name)
    identity = _nonblank_string(operation.get("space_id"), field="space_id", operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    role = _nonblank_string(operation.get("role"), field="role", operation=name)
    evidence = RoomRoleEvidence.model_validate({
        "role": role, "basis": operation.get("basis"),
        "source_refs": _source_refs(operation.get("source_refs"), operation=name),
        "assumptions": _notes(operation.get("assumptions", []), field="assumptions", operation=name),
    }).model_dump(mode="json")
    cells = [cell for floor in geometry.get("floors", []) for cell in floor.get("cells", [])]
    row = _find(cells, identity, operation=name)
    before = copy.deepcopy(row)
    row["role"] = evidence["role"]
    # Preserve geometry sources/assumptions; replace only the current use basis.
    # Previous function assignments remain in the normal edit audit history.
    row["role_evidence"] = evidence
    return {"operation": name, "space_id": identity, "reason": reason,
            "source_refs": evidence["source_refs"], "before": before, "after": copy.deepcopy(row)}


def _add_opening(geometry: dict, operation: dict) -> dict:
    name = "add_opening"
    _require_fields(operation, {"op", "opening", "reason", "source_refs"}, operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    row = copy.deepcopy(operation.get("opening"))
    if not isinstance(row, dict):
        raise ValueError("add_opening: opening must be an object")
    row["source_refs"] = refs
    if geometry.get("schema_version") == "source_polygon_v1":
        from src.agent.geometry.source_schema import SourceWallOpening
        from src.agent.geometry.source_curves import expand_source_polygon_geometry
        row = expand_source_polygon_geometry({'schema_version': 'source_polygon_v1',
            'boundary_curves': geometry.get('boundary_curves', {}), 'openings': [row]})['openings'][0]
        row = SourceWallOpening.model_validate(row).model_dump(mode="json")
    else:
        row = WallOpening.model_validate(row).model_dump(mode="json")
    if any(o["id"] == row["id"] for field in ("windows", "openings") for o in geometry.get(field, [])):
        raise ValueError("add_opening: opening id already exists")
    geometry.setdefault("openings", []).append(row)
    # Exact host, overlap and world-Z validation remain in the source builder.
    return {"operation": name, "id": row["id"], "reason": reason,
            "source_refs": refs, "before": None, "after": copy.deepcopy(row)}


def _resolve_unbuilt_observation(geometry: dict, operation: dict) -> dict:
    name = "resolve_unbuilt_observation"
    _require_fields(operation, {"op", "input_id", "observation_ids", "opening_ids", "reason", "source_refs"}, operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    observations = _source_refs(operation.get("observation_ids"), operation=name)
    replacements = _source_refs(operation.get("opening_ids"), operation=name)
    matches = [r for r in geometry.get("unsupported", [])
               if r.get("kind") == "as_drawn_opening_unbuilt" and r.get("input_id") == operation.get("input_id")
               and r.get("observation_ids") == observations]
    if len(matches) != 1:
        raise ValueError("resolve_unbuilt_observation: expected one exact unbuilt observation record")
    before = copy.deepcopy(matches[0])
    for identity in replacements:
        opening = _find(geometry.get("openings", []), identity, operation=name)
        floor = next((f for f in geometry["floors"] if any(c["id"] == opening["space_id"] for c in f["cells"])), None)
        if floor is None or (floor.get("id") or floor["name"]) != before["floor_id"]:
            raise ValueError("resolve_unbuilt_observation: replacement must be on the observation floor")
    geometry["unsupported"].remove(matches[0])
    return {"operation": name, "reason": reason, "source_refs": refs,
            "before": before, "opening_ids": replacements,
            "drawing_fidelity": "not_evaluated"}


def _finite_coordinate(value: object, *, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{operation}: coordinate_m must be a finite number")
    return float(value)


def _polygon_ring_and_bbox(value: object, *, operation: str, space_id: str, minimum_vertices=4) -> tuple[list[list[float]], list[float], list[float]]:
    """Validate an edit ring enough to derive its legacy bbox without repairing it.

    Full source-space validity (orthogonality, winding, coverage and opening
    hosts) deliberately remains the source-BIM builder's responsibility.  In
    particular, this helper never closes, reorders, snaps, or otherwise
    changes a supplied ring.
    """
    if not isinstance(value, list) or len(value) < minimum_vertices:
        raise ValueError(f"{operation}: space {space_id!r} polygon must contain at least {minimum_vertices} [x, y] vertices")
    ring: list[list[float]] = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{operation}: space {space_id!r} polygon vertex {index} must be [x, y]")
        if any(isinstance(coordinate, bool) or not isinstance(coordinate, (int, float))
               or not math.isfinite(coordinate) for coordinate in point):
            raise ValueError(f"{operation}: space {space_id!r} polygon vertex {index} must be finite")
        ring.append([float(point[0]), float(point[1])])
    return (ring,
            [min(point[0] for point in ring), max(point[0] for point in ring)],
            [min(point[1] for point in ring), max(point[1] for point in ring)])


def _reshape_spaces(proposal: dict, geometry: dict, operation: dict) -> dict:
    """Replace complete source-space rings without inferring any related edits.

    ``spaces`` is a nonempty list of ``{"id", "polygon"}`` rows.  Each
    supplied polygon replaces exactly one existing source space, including a
    rectangle (which is represented as a four-point ring).  The code derives
    that cell's legacy ``x``/``y`` bbox from the ring.  It does not move, scale,
    rehost, or otherwise alter openings and windows; callers may place an
    explicit ``update_opening`` alongside this operation when evidence calls
    for it.
    """
    name = "reshape_spaces"
    _require_fields(operation, {"op", "spaces", "reason", "source_refs"}, operation=name)
    # These declarations carry a wall/enclosure interpretation which a ring
    # replacement cannot faithfully migrate.  Do not leave stale declarations
    # behind or pretend that arbitrary polygons have updated them.
    if proposal.get("enclosure_declaration") is not None:
        raise ValueError("reshape_spaces: explicit enclosure_declaration requires an explicit revised proposal")
    if proposal.get("wall_references") or proposal.get("wall_dimensions"):
        raise ValueError("reshape_spaces: wall references/dimensions require an explicit revised proposal")
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    rows = operation.get("spaces")
    if not isinstance(rows, list) or not rows:
        raise ValueError("reshape_spaces: spaces must be a nonempty list")

    replacements: list[tuple[str, list[list[float]], list[float], list[float]]] = []
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("reshape_spaces: each spaces entry must be an object")
        _require_fields(row, {"id", "polygon"}, operation=name)
        identity = _nonblank_string(row.get("id"), field="spaces[].id", operation=name)
        if identity in seen_ids:
            raise ValueError(f"reshape_spaces: duplicate space id {identity!r}")
        seen_ids.add(identity)
        ring, x, y = _polygon_ring_and_bbox(row.get("polygon"), operation=name, space_id=identity,
            minimum_vertices=3 if geometry.get("schema_version") == "source_polygon_v1" else 4)
        replacements.append((identity, ring, x, y))

    cells_by_id = {
        cell.get("id"): cell
        for floor in geometry.get("floors", [])
        for cell in floor.get("cells", [])
    }
    missing = [identity for identity, *_ in replacements if identity not in cells_by_id]
    if missing:
        raise ValueError(f"reshape_spaces: unknown existing space ids {missing!r}")

    before = {}
    after = {}
    for identity, ring, x, y in replacements:
        cell = cells_by_id[identity]
        if cell.get("vertical_sections"):
            raise ValueError("reshape_spaces: vertical_sections requires an explicit revised build_bim proposal, including every section")
        before[identity] = copy.deepcopy(cell)
        cell["polygon"] = ring
        cell["x"] = x
        cell["y"] = y
        after[identity] = copy.deepcopy(cell)
    return {
        "operation": name,
        "space_ids": [identity for identity, *_ in replacements],
        "reason": reason,
        "source_refs": refs,
        "before": {"cells": before},
        "after": {"cells": after},
        "opening_policy": "openings and windows are unchanged; use explicit update_opening for an opening edit",
    }


def _cell_shape(cell: dict, *, operation: str, require_orthogonal=True) -> Polygon:
    """Return one existing cell's complete plan shape under the shared contract."""
    if cell.get("vertical_sections"):
        raise ValueError(f"{operation}: vertical_sections requires an explicit revised build_bim proposal")
    try:
        shape = cell_polygon(SimpleNamespace(
            id=cell.get("id"), polygon=cell.get("polygon"), x=cell.get("x"), y=cell.get("y"),
            holes=cell.get('holes', []),
        ), require_orthogonal=require_orthogonal)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{operation}: source space {cell.get('id')!r} has invalid geometry: {exc}") from exc
    if shape.geom_type != "Polygon" or shape.interiors:
        raise ValueError(f"{operation}: source space {cell.get('id')!r} must be one hole-free polygon")
    return shape


def _single_hole_free_polygon(shape: object, *, operation: str, name: str) -> Polygon:
    if (getattr(shape, "geom_type", None) != "Polygon" or shape.is_empty or not shape.is_valid
            or shape.area <= _LOCAL_PARTITION_AREA_TOLERANCE_M2 or shape.interiors):
        raise ValueError(f"{operation}: {name} must be one valid polygon without holes")
    return shape


def _ring_from_polygon(shape: Polygon) -> list[list[float]]:
    canonical = orient(shape, sign=1.0)
    return [[float(x), float(y)] for x, y in list(canonical.exterior.coords)[:-1]]


def _set_cell_polygon(cell: dict, shape: Polygon) -> None:
    ring = _ring_from_polygon(shape)
    cell["polygon"] = ring
    cell["x"] = [float(shape.bounds[0]), float(shape.bounds[2])]
    cell["y"] = [float(shape.bounds[1]), float(shape.bounds[3])]


def _replace_space_region(proposal: dict, geometry: dict, operation: dict) -> dict:
    """Replace one source room and derive its adjacent room's exact remainder.

    This is deliberately a two-space conservation edit.  It changes neither
    openings nor any third space, so an aperture whose host moves must be
    supplied separately as an explicit ``update_opening`` operation.
    """
    name = "replace_space_region"
    _require_fields(operation, {"op", "space_id", "neighbor_space_id", "polygon", "reason", "source_refs"}, operation=name)
    if proposal.get("enclosure_declaration") is not None:
        raise ValueError("replace_space_region: explicit enclosure_declaration requires an explicit revised proposal")
    if proposal.get("wall_references") or proposal.get("wall_dimensions"):
        raise ValueError("replace_space_region: wall references/dimensions require an explicit revised proposal")
    space_id = _nonblank_string(operation.get("space_id"), field="space_id", operation=name)
    neighbor_id = _nonblank_string(operation.get("neighbor_space_id"), field="neighbor_space_id", operation=name)
    if space_id == neighbor_id:
        raise ValueError("replace_space_region: space_id and neighbor_space_id must differ")
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)

    matches = []
    for floor in geometry.get("floors", []):
        cells = {cell.get("id"): cell for cell in floor.get("cells", [])}
        if space_id in cells and neighbor_id in cells:
            matches.append((floor, cells[space_id], cells[neighbor_id]))
    if len(matches) != 1:
        raise ValueError("replace_space_region: spaces must occur together on exactly one floor")
    floor, target_cell, neighbor_cell = matches[0]
    require_orthogonal = geometry.get("schema_version") != "source_polygon_v1"
    old_target = _cell_shape(target_cell, operation=name, require_orthogonal=require_orthogonal)
    old_neighbor = _cell_shape(neighbor_cell, operation=name, require_orthogonal=require_orthogonal)
    if old_target.intersection(old_neighbor).area > _LOCAL_PARTITION_AREA_TOLERANCE_M2:
        raise ValueError("replace_space_region: source spaces overlap instead of sharing a partition")
    if old_target.boundary.intersection(old_neighbor.boundary).length <= _LOCAL_PARTITION_TOLERANCE_M:
        raise ValueError("replace_space_region: source spaces must share an actual boundary edge")
    old_region = unary_union([old_target, old_neighbor])
    old_region = _single_hole_free_polygon(old_region, operation=name, name="source two-space union")

    ring, x, y = _polygon_ring_and_bbox(operation.get("polygon"), operation=name, space_id=space_id,
        minimum_vertices=4 if require_orthogonal else 3)
    new_target = _cell_shape({"id": space_id, "polygon": ring, "x": x, "y": y}, operation=name,
                           require_orthogonal=require_orthogonal)
    new_target = _single_hole_free_polygon(new_target, operation=name, name="replacement polygon")
    if new_target.difference(old_region).area > _LOCAL_PARTITION_AREA_TOLERANCE_M2:
        raise ValueError("replace_space_region: replacement polygon extends outside the original two-space region")
    new_neighbor = _single_hole_free_polygon(
        old_region.difference(new_target), operation=name, name="neighbor remainder",
    )
    if unary_union([new_target, new_neighbor]).symmetric_difference(old_region).area > _LOCAL_PARTITION_AREA_TOLERANCE_M2:
        raise ValueError("replace_space_region: replacement does not preserve the original two-space coverage")

    before_cells = {space_id: copy.deepcopy(target_cell), neighbor_id: copy.deepcopy(neighbor_cell)}
    _set_cell_polygon(target_cell, new_target)
    _set_cell_polygon(neighbor_cell, new_neighbor)
    return {
        "operation": name,
        "space_id": space_id,
        "neighbor_space_id": neighbor_id,
        "floor": floor.get("name"),
        "reason": reason,
        "source_refs": refs,
        "before": {"cells": before_cells},
        "after": {"cells": {space_id: copy.deepcopy(target_cell), neighbor_id: copy.deepcopy(neighbor_cell)}},
        "opening_policy": "openings and windows are unchanged; use explicit update_opening for an opening edit",
    }


def _shared_rectangular_wall(left: dict, right: dict, *, operation: str) -> tuple[str, float, list[float], dict, dict]:
    """Return a complete common side, oriented from lower to higher coordinate."""
    if left.get("polygon") is not None or right.get("polygon") is not None:
        raise ValueError(f"{operation}: requires two rectangular cells without polygon")
    for cell in (left, right):
        for axis in ("x", "y"):
            interval = cell.get(axis)
            if (not isinstance(interval, list) or len(interval) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in interval)):
                raise ValueError(f"{operation}: rectangular cell {cell.get('id')!r} has invalid {axis} interval")

    # A local move is deliberately limited to one complete shared side.  Partial
    # contacts and polygon rings need a topology edit, rather than guessing which
    # vertices and neighbouring contacts should follow this wall.
    if left["y"] == right["y"]:
        if left["x"][1] == right["x"][0]:
            return "x", left["x"][1], list(left["y"]), left, right
        if right["x"][1] == left["x"][0]:
            return "x", right["x"][1], list(left["y"]), right, left
    if left["x"] == right["x"]:
        if left["y"][1] == right["y"][0]:
            return "y", left["y"][1], list(left["x"]), left, right
        if right["y"][1] == left["y"][0]:
            return "y", right["y"][1], list(left["x"]), right, left
    raise ValueError(f"{operation}: spaces must share one complete axis-aligned rectangular side")


def _opening_on_wall(opening: dict, *, axis: str, coordinate: float, span: list[float]) -> bool:
    points = [opening.get("p1"), opening.get("p2")]
    if any(not isinstance(point, (list, tuple)) or len(point) != 2 for point in points):
        return False
    normal = 0 if axis == "x" else 1
    along = 1 - normal
    try:
        return (all(point[normal] == coordinate for point in points)
                and all(span[0] <= point[along] <= span[1] for point in points))
    except TypeError:
        return False


def _move_shared_wall(proposal: dict, geometry: dict, operation: dict) -> dict:
    """Move one full rectangular interior side while preserving the source topology."""
    name = "move_shared_wall"
    _require_fields(operation, {"op", "space_ids", "coordinate_m", "reason", "source_refs"}, operation=name)
    if proposal.get("enclosure_declaration") is not None:
        raise ValueError("move_shared_wall: explicit enclosure_declaration requires an explicit enclosure edit")
    space_ids = operation.get("space_ids")
    if (not isinstance(space_ids, list) or len(space_ids) != 2
            or any(not isinstance(identity, str) or not identity.strip() for identity in space_ids)
            or space_ids[0] == space_ids[1]):
        raise ValueError("move_shared_wall: space_ids must contain two distinct nonblank space ids")
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    coordinate = _finite_coordinate(operation.get("coordinate_m"), operation=name)

    matches = []
    space_floors = {}
    for floor in geometry.get("floors", []):
        cells = {cell.get("id"): cell for cell in floor.get("cells", [])}
        space_floors.update({identity: floor for identity in cells})
        if all(identity in cells for identity in space_ids):
            matches.append((floor, cells[space_ids[0]], cells[space_ids[1]]))
    if len(matches) != 1:
        raise ValueError("move_shared_wall: spaces must occur together on exactly one floor")
    floor, first, second = matches[0]
    axis, old_coordinate, span, lower, higher = _shared_rectangular_wall(first, second, operation=name)
    interval_name = f"footprint_{axis}"
    footprint = geometry.get(interval_name)
    if (not isinstance(footprint, list) or len(footprint) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in footprint)
            or not footprint[0] < old_coordinate < footprint[1]):
        raise ValueError(f"move_shared_wall: shared side is not an interior {axis} wall in the declared footprint")
    if not lower[axis][0] < coordinate < higher[axis][1]:
        raise ValueError("move_shared_wall: coordinate_m would collapse or invert a source space")

    before_cells = {cell["id"]: copy.deepcopy(cell) for cell in (lower, higher)}
    lower[axis][1] = coordinate
    higher[axis][0] = coordinate

    pair = set(space_ids)
    moved_openings = []
    for opening in geometry.get("openings", []):
        # The proposal has no opening-level floor field.  Its owning space is
        # the authoritative floor binding, so matching XY coordinates on a
        # different storey must never make this local edit reject or move it.
        if space_floors.get(opening.get("space_id")) is not floor:
            continue
        opening_pair = {opening.get("space_id"), opening.get("other_space_id")}
        on_old_wall = _opening_on_wall(opening, axis=axis, coordinate=old_coordinate, span=span)
        if opening_pair == pair:
            if not on_old_wall:
                raise ValueError(f"move_shared_wall: opening {opening.get('id')!r} between selected spaces is not fully hosted on their shared wall")
            before = copy.deepcopy(opening)
            normal = 0 if axis == "x" else 1
            opening["p1"][normal] = coordinate
            opening["p2"][normal] = coordinate
            moved_openings.append({"id": opening.get("id"), "before": before, "after": copy.deepcopy(opening)})
        elif on_old_wall:
            raise ValueError(f"move_shared_wall: opening {opening.get('id')!r} on the moved wall has different spaces")

    return {
        "operation": name,
        "space_ids": list(space_ids),
        "floor": floor.get("name"),
        "axis": axis,
        "from_coordinate_m": old_coordinate,
        "coordinate_m": coordinate,
        "shared_span_m": span,
        "reason": reason,
        "source_refs": refs,
        "before": {"cells": before_cells},
        "after": {"cells": {cell["id"]: copy.deepcopy(cell) for cell in (lower, higher)}},
        "moved_openings": moved_openings,
    }


def _set_notes(proposal: dict, operation: dict) -> dict:
    _require_fields(operation, {"op", "assumptions", "unresolved"}, operation="set_notes")
    assumptions = _notes(operation.get("assumptions"), field="assumptions")
    unresolved = _notes(operation.get("unresolved"), field="unresolved")
    before = {"assumptions": copy.deepcopy(proposal["assumptions"]), "unresolved": copy.deepcopy(proposal["unresolved"])}
    proposal["assumptions"] = assumptions
    proposal["unresolved"] = unresolved
    return {"operation": "set_notes", "before": before,
            "after": {"assumptions": copy.deepcopy(assumptions), "unresolved": copy.deepcopy(unresolved)}}


def _replace_note(proposal: dict, operation: dict) -> dict:
    _require_fields(operation, {"op", "field", "old", "replacement", "reason", "source_refs"}, operation="replace_note")
    field = operation.get("field")
    if field not in {"assumptions", "unresolved"}:
        raise ValueError("replace_note: field must be assumptions or unresolved")
    old = _nonblank_string(operation.get("old"), field="old", operation="replace_note")
    replacement = _notes(operation.get("replacement"), field="replacement")
    reason = _nonblank_string(operation.get("reason"), field="reason", operation="replace_note")
    refs = _source_refs(operation.get("source_refs"), operation="replace_note")
    indices = [index for index, note in enumerate(proposal[field]) if note == old]
    if not indices:
        raise ValueError("replace_note: old must exactly match a current note")
    # Assembly can retain the same global statement from several floors. It
    # remains one textual claim: replace all exact copies, inserting the new
    # statement once at the first position. Distinct notes keep their order.
    first = indices[0]
    proposal[field] = [*proposal[field][:first], *replacement,
                       *(note for note in proposal[field][first + 1:] if note != old)]
    return {"operation": "replace_note", "field": field, "before": old,
            "after": replacement, "matched_count": len(indices), "matched_indices": indices,
            "reason": reason, "source_refs": refs}


def _update_wall_evidence(proposal: dict, operation: dict) -> dict:
    """Patch one raw evidence record; keep other records and endpoint fields intact."""
    name = operation["op"]
    _require_fields(operation, {"op", "id", "changes", "reason", "source_refs"}, operation=name)
    identity = _nonblank_string(operation.get("id"), field="id", operation=name)
    reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
    refs = _source_refs(operation.get("source_refs"), operation=name)
    dimension = name == "update_wall_dimension"
    field = "wall_dimensions" if dimension else "wall_references"
    allowed = ({"axis", "direction", "value", "unit", "start", "end"} if dimension else
               {"boundary_id", "offsets_m", "thickness_m", "reference_basis", "thickness_scope", "evidence_status"})
    changes = operation.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ValueError(f"{name}: changes must be a nonempty object")
    _require_fields(changes, allowed, operation=name)
    row = _find(proposal.get(field, []), identity, operation=name)
    before = copy.deepcopy(row)
    for key, value in changes.items():
        if dimension and key in {"start", "end"}:
            if not isinstance(value, dict) or not value:
                raise ValueError(f"{name}: {key} must be a nonempty endpoint patch")
            _require_fields(value, {"wall_id", "side", "image", "pixel"}, operation=name)
            row[key].update(copy.deepcopy(value))
        else:
            row[key] = copy.deepcopy(value)
    row["source_refs"] = refs
    return {"operation": name, "id": identity, "reason": reason, "source_refs": refs,
            "before": before, "after": copy.deepcopy(row)}


def _reflect_wall_readings(proposal: dict, audit: dict, edit_index: int) -> None:
    """Rebind thickness through the explicitly requested coordinate transform."""
    before = copy.deepcopy(proposal.get("wall_readings", []))
    if not before:
        return
    from src.agent.geometry.reader_wall_metadata import _span

    axis = audit["transform"]["axis"]
    midpoint = audit["transform"]["about_world_coordinate"]
    normal = 0 if axis == "x" else 1
    opposite = {"negative_face": "positive_face", "positive_face": "negative_face"}
    for reading in proposal["wall_readings"]:
        rows = []
        for row in reading["walls"]:
            by_side = {}
            for segment in row["segments_m"]:
                side = row["reference_side"]
                if _span(*segment)[0] == normal:
                    side = opposite.get(side, side)
                by_side.setdefault(side, []).append([
                    _reflect_point(point, axis=axis, midpoint=midpoint) for point in segment
                ])
            for index, (side, segments) in enumerate(by_side.items()):
                rebound = dict(row, reference_side=side, segments_m=segments)
                if len(by_side) > 1:
                    # One polyline can have both world normals. Its side cannot
                    # remain one value when only one normal is reflected.
                    rebound["target"] = f"{row['target']}:reflect:{edit_index}:{index}"
                    rebound["source_refs"] = [*row["source_refs"],
                        f"plan-wall:{reading['floor_id']}/{row['target']}"]
                rows.append(rebound)
        reading["walls"] = rows
    audit["wall_readings"] = {
        "action": "explicit_coordinate_rebinding", "before": before,
        "after": copy.deepcopy(proposal["wall_readings"]),
        "basis": "Thickness is invariant; wall coordinates and world-normal sides follow the recorded reflection. Original observations remain in this audit.",
    }


def _rebind_edited_wall_readings(proposal: dict, audit: dict, edit_index: int) -> None:
    """Keep evidence on unchanged lines; new or partly unsupported walls are unknown.

    Full source edges can overlap at a T junction. All overlapping edges must
    share one evidence value, because the source kernel does not split them.
    This only changes metadata and never moves or identifies geometry.
    """
    if not proposal.get("wall_readings"):
        return
    from src.agent.geometry.reader_wall_metadata import _span, _covered_length, _EPS
    from src.agent.geometry.source_model import source_primitives

    changed_ids = set(audit["before"]["cells"])
    spaces, boundaries, _, _ = source_primitives(ensure_corrected_geometry(proposal["geometry"]))
    floor_by_space = {space.id: space.floor_id for space in spaces}
    changed_floors = {floor_by_space[identity] for identity in changed_ids}
    before, after = [], []
    for reading in proposal["wall_readings"]:
        floor_id = reading["floor_id"]
        if floor_id not in changed_floors:
            continue
        before.append(copy.deepcopy(reading))
        spans = sorted({_span(*sorted({tuple(p[:2]) for p in wall.vertices}))
                        for wall in boundaries.values() if wall.geometry_type == "wall"
                        and floor_by_space[wall.space_id] == floor_id})
        # Merge positive overlaps, but not merely touching edges. This is the
        # same physical line coverage required by the normal evidence binder.
        groups = []
        for axis, coordinate, lo, hi in spans:
            matches = [group for group in groups if group[0] == axis
                       and abs(group[1] - coordinate) <= _EPS
                       and min(group[3], hi) - max(group[2], lo) > _EPS]
            for group in matches:
                groups.remove(group)
                lo, hi = min(lo, group[2]), max(hi, group[3])
            groups.append((axis, coordinate, lo, hi))
        parsed = [(row, [_span(*segment) for segment in row["segments_m"]])
                  for row in reading["walls"]]
        retained, unknown = {}, []
        for index, (axis, coordinate, lo, hi) in enumerate(sorted(groups)):
            covers = []
            for row, old_spans in parsed:
                overlaps = [(max(lo, start), min(hi, end))
                            for normal, position, start, end in old_spans
                            if normal == axis and abs(position-coordinate) <= _EPS
                            and min(hi, end) - max(lo, start) > _EPS]
                if overlaps:
                    covers.append((row, overlaps))
            kinds = {(row["thickness_m"], row["reference_side"], row["evidence_status"])
                     for row, _ in covers}
            intervals = [span for _, overlaps in covers for span in overlaps]
            supported = len(kinds) == 1 and abs(_covered_length(intervals) - (hi-lo)) <= _EPS
            if supported:
                for row, overlaps in covers:
                    entry = retained.setdefault(row["target"], dict(row, segments_m=[]))
                    entry["segments_m"].extend([
                        [[coordinate, start], [coordinate, end]] if axis == 0
                        else [[start, coordinate], [end, coordinate]] for start, end in overlaps
                    ])
            else:
                unknown.append({
                    "target": f"edited-wall:{edit_index}:{index}", "thickness_m": None,
                    "reference_side": "unknown", "evidence_status": "unknown",
                    "basis": f"{audit['operation']} changed wall support or combined differing evidence; thickness requires renewed observation. Original evidence is retained in the edit audit.",
                    "source_refs": [],
                    "segments_m": [[[coordinate, lo], [coordinate, hi]] if axis == 0
                                   else [[lo, coordinate], [hi, coordinate]]],
                })
        reading["walls"] = [*retained.values(), *unknown]
        after.append(copy.deepcopy(reading))
    audit["wall_readings"] = {
        "action": "preserve_unchanged_lines_invalidate_changed_support",
        "before": before, "after": after,
        "basis": "No old observation is moved to a new wall line. Unchanged support retains its thickness and sources; unsupported unsplit walls become unknown.",
    }


def apply_proposal_edits(proposal: dict, operations: list[dict]) -> dict:
    """Return an independently editable legacy proposal after explicit local edits.

    The input proposal is never changed.  This does not claim any edit is
    faithful to a drawing; it records the newly supplied reason/source evidence
    and leaves source-BIM validation to the normal deterministic builder.
    """
    _validate_proposal(proposal)
    if not isinstance(operations, list):
        raise TypeError("operations must be a list")
    result = copy.deepcopy(proposal)
    if result['geometry'].get('schema_version') == 'source_polygon_v1':
        from src.agent.geometry.source_curves import expand_source_polygon_geometry
        result['geometry'] = expand_source_polygon_geometry(result['geometry'])
    geometry = result["geometry"]
    _unique_ids(geometry)
    corrections = geometry.setdefault("corrections", [])
    if not isinstance(corrections, list):
        raise ValueError("geometry.corrections must be a list")
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
            raise ValueError("each operation must be an object with an op")
        name = operation["op"]
        if name == "reflect":
            _require_fields(operation, {"op", "axis", "reason"}, operation=name)
            if result.get("wall_references") or result.get("wall_dimensions"):
                raise ValueError("reflect: wall reference sides and dimension frame require an explicit revised proposal")
            if result.get("enclosure_declaration"):
                raise ValueError("reflect: nonempty enclosure_declaration requires an explicit enclosure edit")
            if geometry.get("north_axis") is not None or geometry.get("facade_segments"):
                raise ValueError("reflect: explicit north_axis/facade_segments require an explicit directional metadata edit")
            audit = _reflect(geometry, axis=operation.get("axis"),
                             reason=_nonblank_string(operation.get("reason"), field="reason", operation=name))
            if _DIRECTION_NOTICE not in result["unresolved"]:
                result["unresolved"].append(_DIRECTION_NOTICE)
        elif name == "update_window":
            audit = _update(geometry, operation, target="window")
        elif name == "update_opening":
            audit = _update(geometry, operation, target="opening")
        elif name == "set_space_role":
            audit = _set_space_role(geometry, operation)
        elif name == "add_opening":
            audit = _add_opening(geometry, operation)
        elif name == "resolve_unbuilt_observation":
            audit = _resolve_unbuilt_observation(geometry, operation)
        elif name == "remove_opening":
            audit = _remove_opening(geometry, operation)
        elif name == "move_shared_wall":
            audit = _move_shared_wall(result, geometry, operation)
        elif name == "reshape_spaces":
            audit = _reshape_spaces(result, geometry, operation)
        elif name == "replace_space_region":
            audit = _replace_space_region(result, geometry, operation)
        elif name == "set_notes":
            audit = _set_notes(result, operation)
        elif name == "replace_note":
            audit = _replace_note(result, operation)
        elif name in {"update_wall_dimension", "update_wall_reference"}:
            audit = _update_wall_evidence(result, operation)
        elif name == "set_wall_references":
            _require_fields(operation, {"op", "wall_references", "wall_dimensions", "reason"}, operation=name)
            reason = _nonblank_string(operation.get("reason"), field="reason", operation=name)
            before = {field: copy.deepcopy(result.get(field, [])) for field in ("wall_references", "wall_dimensions")}
            for field in before:
                value = operation.get(field)
                if not isinstance(value, list):
                    raise ValueError(f"{name}: {field} must be a list")
                result[field] = copy.deepcopy(value)
            audit = {"op": name, "reason": reason, "before": before,
                     "after": {field: copy.deepcopy(result[field]) for field in before}}
        else:
            raise ValueError(f"unknown proposal edit operation {name!r}")
        if name == "reflect":
            _reflect_wall_readings(result, audit, len(corrections))
        elif name in {"move_shared_wall", "reshape_spaces", "replace_space_region"}:
            _rebind_edited_wall_readings(result, audit, len(corrections))
        corrections.append(audit)
        _unique_ids(geometry)
    ensure_corrected_geometry(geometry)
    return result
