"""Assemble independently compiled single-floor plans without inventing geometry.

Every item supplies one ``compile_plan_partition`` proposal, a new floor identity,
and its target absolute floor elevation. Apertures move by the same vertical
offset as their floor; their dimensions and all plan coordinates remain intact.
"""
from __future__ import annotations

import copy
import math
from decimal import Decimal

from src.agent.geometry.input_scale import check_floor_placements, check_geometry_scale


_ITEM_FIELDS = {"proposal", "floor_id", "z_floor", "height", "source_ref"}
_PROPOSAL_FIELDS = {"geometry", "assumptions", "unresolved"}
_GEOMETRY_FIELDS = {
    "schema_version", "footprint_x", "footprint_y", "floors", "windows",
    "openings", "notes",
}


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _identifier(value: object, path: str, *, namespace: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    if namespace and ":" in value:
        raise ValueError(f"{path} must not contain ':'")
    return value


def _interval(value: object, path: str, base: float, height: float) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{path} must be a two-number z interval")
    low, high = (_number(v, f"{path}[{i}]") for i, v in enumerate(value))
    if not (base <= low < high <= base + height):
        raise ValueError(f"{path} must lie within source floor [{base}, {base + height}]")
    return [low, high]


def _bounds(value: object, path: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{path} must be a two-number footprint interval")
    low, high = (_number(v, f"{path}[{i}]") for i, v in enumerate(value))
    if low >= high:
        raise ValueError(f"{path} must have positive extent")
    return [low, high]


def _strings(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise TypeError(f"{path} must be a list of strings")
    return value


def assemble_plan_proposals(items: list[dict]) -> dict:
    """Return one geometry-v2 proposal from explicit single-floor proposals.

    ``height`` is an optional assertion against the compiled ceiling height,
    never a request to resize. ``source_ref`` is recorded in geometry notes.
    All source objects are copied; input proposals remain unchanged.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a nonempty list")

    floors: list[dict] = []
    windows: list[dict] = []
    openings: list[dict] = []
    assumptions: list[str] = []
    unresolved: list[str] = []
    notes: list[str] = ["Deterministic assembly of explicit single-floor plan proposals."]
    floor_ids: set[str] = set()
    global_x: list[float] = []
    global_y: list[float] = []
    placements = []
    wall_readings = []

    for index, raw_item in enumerate(items):
        path = f"items[{index}]"
        if not isinstance(raw_item, dict):
            raise TypeError(f"{path} must be an object")
        missing = {"proposal", "floor_id", "z_floor"} - raw_item.keys()
        unknown = raw_item.keys() - _ITEM_FIELDS
        if missing or unknown:
            raise ValueError(f"{path} missing {sorted(missing)}; unknown {sorted(unknown)}")
        floor_id = _identifier(raw_item["floor_id"], f"{path}.floor_id", namespace=True)
        if floor_id in floor_ids:
            raise ValueError(f"duplicate floor_id {floor_id!r}")
        floor_ids.add(floor_id)
        target_z = _number(raw_item["z_floor"], f"{path}.z_floor")
        source_ref = raw_item.get("source_ref")
        if source_ref is not None:
            _identifier(source_ref, f"{path}.source_ref")

        original = raw_item["proposal"]
        if (not isinstance(original, dict) or not _PROPOSAL_FIELDS <= set(original)
                or set(original) - _PROPOSAL_FIELDS - {"wall_readings"}):
            raise ValueError(f"{path}.proposal needs geometry, assumptions, unresolved; wall_readings is optional")
        proposal = copy.deepcopy(original)
        geometry = proposal["geometry"]
        if not isinstance(geometry, dict) or set(geometry) - _GEOMETRY_FIELDS:
            raise ValueError(f"{path}.proposal.geometry has unsupported fields")
        if geometry.get("schema_version") != "2":
            raise ValueError(f"{path}.proposal must use geometry schema_version 2")
        source_floors = geometry.get("floors")
        if not isinstance(source_floors, list) or len(source_floors) != 1:
            raise ValueError(f"{path}.proposal must contain exactly one compiled floor")
        floor = source_floors[0]
        if not isinstance(floor, dict):
            raise TypeError(f"{path}.proposal floor must be an object")
        original_floor_id = _identifier(floor.get("name"), f"{path}.proposal floor.name")
        if "wall_readings" in proposal:
            readings = proposal["wall_readings"]
            if (not isinstance(readings, list) or len(readings) != 1
                    or not isinstance(readings[0], dict)
                    or readings[0].get("floor_id") != original_floor_id):
                raise ValueError(f"{path}.wall_readings must refer to its one source floor")
            readings[0]["floor_id"] = floor_id
            wall_readings.extend(readings)
        source_z = _number(floor.get("z_floor"), f"{path}.proposal floor.z_floor")
        height = _number(floor.get("ceiling_height"), f"{path}.proposal floor.ceiling_height")
        if height <= 0:
            raise ValueError(f"{path}.proposal floor.ceiling_height must be positive")
        if not math.isfinite(source_z + height) or not math.isfinite(target_z + height):
            raise ValueError(f"{path}: source or target ceiling elevation must be finite")
        if "height" in raw_item and _number(raw_item["height"], f"{path}.height") != height:
            raise ValueError(f"{path}.height differs from compiled ceiling_height {height}; revise the plan explicitly")
        if floor.get("spanning_space_ids"):
            raise ValueError(f"{path}: spanning_space_ids are outside the single-floor compiled plan contract")
        x = _bounds(geometry.get("footprint_x"), f"{path}.proposal.geometry.footprint_x")
        y = _bounds(geometry.get("footprint_y"), f"{path}.proposal.geometry.footprint_y")
        check_geometry_scale(geometry, path=f"{path}.proposal.geometry")
        placements.append((f"{path}.z_floor", target_z,
                           f"{path}.proposal.geometry.floors[0].ceiling_height", height))
        global_x.extend(x)
        global_y.extend(y)

        cells = floor.get("cells")
        if not isinstance(cells, list) or not cells:
            raise ValueError(f"{path}.proposal floor requires compiled cells")
        space_ids: set[str] = set()
        for cell in cells:
            if not isinstance(cell, dict):
                raise TypeError(f"{path}.proposal cell must be an object")
            old_id = _identifier(cell.get("id"), f"{path}.proposal cell.id")
            if old_id in space_ids:
                raise ValueError(f"{path}: duplicate source space id {old_id!r}")
            space_ids.add(old_id)
            cell["id"] = f"{floor_id}:{old_id}"

        source_windows = geometry.get("windows")
        source_openings = geometry.get("openings")
        if not isinstance(source_windows, list) or not isinstance(source_openings, list):
            raise TypeError(f"{path}.proposal windows and openings must be lists")
        aperture_ids: set[str] = set()
        offset = Decimal(str(target_z)) - Decimal(str(source_z))
        for collection, expected_kind in ((source_windows, "window"), (source_openings, None)):
            for aperture in collection:
                if not isinstance(aperture, dict):
                    raise TypeError(f"{path}.proposal aperture must be an object")
                old_id = _identifier(aperture.get("id"), f"{path}.proposal aperture.id")
                if old_id in aperture_ids:
                    raise ValueError(f"{path}: duplicate source aperture id {old_id!r}")
                aperture_ids.add(old_id)
                if expected_kind and aperture.get("kind") != expected_kind:
                    raise ValueError(f"{path}: window {old_id!r} has conflicting kind")
                low, high = _interval(aperture.get("z"), f"{path}.proposal aperture {old_id}.z", source_z, height)
                shifted_z = [float(Decimal(str(v)) + offset) for v in (low, high)]
                if not all(math.isfinite(v) for v in shifted_z):
                    raise ValueError(f"{path}: translated aperture {old_id!r} z is non-finite")
                aperture["z"] = shifted_z
                aperture["id"] = f"{floor_id}:{old_id}"
                if expected_kind:
                    if aperture.get("floor") != original_floor_id:
                        raise ValueError(f"{path}: window {old_id!r} references another floor")
                    room = aperture.get("room")
                    if room not in space_ids:
                        raise ValueError(f"{path}: window {old_id!r} references unknown room {room!r}")
                    aperture["floor"] = floor_id
                    aperture["room"] = f"{floor_id}:{room}"
                else:
                    owner = aperture.get("space_id")
                    peer = aperture.get("other_space_id")
                    if owner not in space_ids or (peer is not None and peer not in space_ids):
                        raise ValueError(f"{path}: opening {old_id!r} references an unknown or cross-floor space")
                    aperture["space_id"] = f"{floor_id}:{owner}"
                    aperture["other_space_id"] = None if peer is None else f"{floor_id}:{peer}"

        floor["name"] = floor_id
        if "id" in floor:
            floor["id"] = floor_id
        floor["z_floor"] = target_z
        # A compiled rectangular floor has no ring. Materialize its declared
        # rectangle so another floor's wider bounds do not change its footprint.
        if "footprint" not in floor:
            floor["footprint"] = {"vertices": [
                [x[0], y[0]], [x[1], y[0]], [x[1], y[1]], [x[0], y[1]],
            ]}
        floors.append(floor)
        windows.extend(source_windows)
        openings.extend(source_openings)
        assumptions.extend(_strings(proposal["assumptions"], f"{path}.proposal.assumptions"))
        unresolved.extend(_strings(proposal["unresolved"], f"{path}.proposal.unresolved"))
        original_note = geometry.get("notes")
        if original_note is not None and not isinstance(original_note, str):
            raise TypeError(f"{path}.proposal.geometry.notes must be a string")
        notes.append(
            f"Floor {floor_id!r} from {original_floor_id!r}; source_ref={source_ref!r}; "
            f"source z_floor={source_z}, target z_floor={target_z}; note={original_note!r}"
        )

    check_floor_placements(placements)
    result = {
        "geometry": {
            "schema_version": "2",
            "footprint_x": [min(global_x), max(global_x)],
            "footprint_y": [min(global_y), max(global_y)],
            "floors": floors,
            "windows": windows,
            "openings": openings,
            "notes": "\n".join(notes),
        },
        "assumptions": assumptions,
        "unresolved": unresolved,
    }
    if wall_readings:
        result["wall_readings"] = wall_readings
    return result
