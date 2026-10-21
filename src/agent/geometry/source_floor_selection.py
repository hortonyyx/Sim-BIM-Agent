"""Select the source spaces and openings physically present on one floor.

Some source spaces span several storeys and belong to a separate floor group.
Each storey explicitly names those spaces in ``spanning_space_ids``. Views and
reviews must use that membership, then limit openings to the storey's height.
"""
from __future__ import annotations

from shapely.ops import unary_union

from src.agent.geometry.source_volume import section_polygon, space_sections

_Z_EPS = 1e-7  # Display membership only; source validation owns geometry tolerances.


def _vertical_interval(row):
    if "z_floor" not in row or "height" not in row:
        return None
    bottom = float(row["z_floor"])
    return bottom, bottom + float(row["height"])


def select_source_floor(source: dict, floor_id: str) -> tuple[dict, list[dict]]:
    """Return the floor and its local plus explicitly referenced spanning spaces."""
    floor = next((row for row in source["floors"] if row["id"] == floor_id), None)
    if floor is None:
        raise ValueError("unknown source floor_id")
    local_ids = {row["id"] for row in source["spaces"] if row["floor_id"] == floor_id}
    spanning_ids = set(floor.get("spanning_space_ids", []))
    selected_ids = local_ids | spanning_ids
    floor_interval = _vertical_interval(floor)
    spaces = []
    for row in source["spaces"]:
        if row["id"] not in selected_ids:
            continue
        interval = _vertical_interval(row)
        if floor_interval is not None and interval is not None:
            if min(floor_interval[1], interval[1]) - max(floor_interval[0], interval[0]) <= _Z_EPS:
                continue
        if row.get("vertical_sections") and floor_interval is not None:
            # A spanning space can have a different footprint on each storey.
            # Views use only the volume present in this storey, not its full
            # building-height XY projection. Identity and source stay intact.
            sections = [section for section in space_sections(row)
                        if min(floor_interval[1], section["z"][1])
                        - max(floor_interval[0], section["z"][0]) > _Z_EPS]
            projection = unary_union([section_polygon(section) for section in sections])
            if projection.is_empty:
                continue
            if projection.geom_type != "Polygon":
                raise ValueError(f"floor {floor_id}: space {row['id']} has a disconnected storey projection")
            row = {**row, "polygon": [list(p) for p in list(projection.exterior.coords)[:-1]],
                   "holes": [[list(p) for p in list(ring.coords)[:-1]] for ring in projection.interiors],
                   "plan_projection_z_m": list(floor_interval)}
        spaces.append(row)
    missing = selected_ids - {row["id"] for row in spaces}
    if missing:
        raise ValueError(f"floor {floor_id} references missing or nonoverlapping spaces: {sorted(missing)}")
    return floor, spaces


def select_source_floor_openings(source: dict, floor: dict, spaces: list[dict]) -> list[dict]:
    """Return openings touching a selected space and intersecting this floor's z span."""
    ids = {row["id"] for row in spaces}
    floor_interval = _vertical_interval(floor)
    selected = []
    for opening in source["openings"]:
        owners = opening["space_ids"]
        if not ids.intersection(owners):
            continue
        if floor_interval is not None:
            heights = [float(vertex[2]) for vertex in opening["vertices"]]
            if min(floor_interval[1], max(heights)) - max(floor_interval[0], min(heights)) <= _Z_EPS:
                continue
        selected.append(opening)
    return selected
