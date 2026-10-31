"""Explicit wall apertures, their two-sided hosts, and visible wall cut-outs.

This module never infers a missing door from a room name or invents a neighbour.
Source apertures retain their identity when a paired wall has several pieces.
The EnergyPlus adapter must explicitly support these objects before exporting.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from src.agent.correction.schema import CorrectedGeometry, WallOpening
from src.agent.geometry.modelling import BuildingGeometry, NameRegistry, Opening, _newell, _orient

# Match the existing kernel's 1 micrometre shared-line subtraction and rounding.
REALIZATION_EPS_M = 2e-6


class OpeningBindingError(ValueError):
    pass


def _frame(p1, p2):
    length = math.dist(p1, p2)
    if not math.isfinite(length) or length <= 0:
        raise OpeningBindingError("opening wall line must have positive finite length")
    return ((p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length), length


def _project(vertices, origin, direction):
    result = []
    for x, y, z in vertices:
        dx, dy = x - origin[0], y - origin[1]
        if abs(dx * direction[1] - dy * direction[0]) > REALIZATION_EPS_M:
            return None
        result.append((dx * direction[0] + dy * direction[1], z))
    return Polygon(result)


def _lift(ring, origin, direction):
    return [(origin[0] + t * direction[0], origin[1] + t * direction[1], z) for t, z in ring]


def _host_pieces(source: WallOpening, space_id: str, other_id: str | None,
                 bg: BuildingGeometry):
    direction, width = _frame(source.p1, source.p2)
    aperture = box(0, source.z[0], width, source.z[1])
    cells = {z.zone: z.cell_id for z in bg.zone_volumes}
    faces = {s.name: s for s in bg.surfaces}
    result = []
    for face in sorted(bg.surfaces, key=lambda s: s.name):
        if cells.get(face.zone) != space_id or face.stype != "Wall":
            continue
        projected = _project(face.verts, source.p1, direction)
        if projected is None:
            continue
        overlap = projected.intersection(aperture)
        if overlap.area <= REALIZATION_EPS_M ** 2:
            continue
        if other_id is None:
            valid_host = face.obc == "Outdoors"
        else:
            partner = faces.get(face.obc_obj)
            valid_host = (face.obc == "Surface" and partner is not None
                          and partner.obc_obj == face.name and cells.get(partner.zone) == other_id)
        if not valid_host:
            raise OpeningBindingError(
                f"opening {source.id}: wall {face.name} does not connect {space_id} to {other_id or 'outdoors'}"
            )
        if overlap.geom_type != "Polygon" or overlap.interiors:
            raise OpeningBindingError(f"opening {source.id}: unsupported non-simple wall intersection")
        result.append((face, overlap))
    union = unary_union([part for _face, part in result])
    if not aperture.difference(union.buffer(REALIZATION_EPS_M)).is_empty:
        raise OpeningBindingError(f"opening {source.id}: no complete wall host in source space {space_id}")
    if sum(part.area for _face, part in result) - union.area > REALIZATION_EPS_M ** 2:
        raise OpeningBindingError(f"opening {source.id}: overlapping or ambiguous wall hosts")
    return result, direction


def attach_openings(geom: CorrectedGeometry, bg: BuildingGeometry) -> list[Opening]:
    """Bind every explicit aperture to its full physical host on both sides."""
    source_ids = [o.id for o in geom.openings]
    window_ids = {str(w.id) for w in geom.windows}
    if len(source_ids) != len(set(source_ids)) or window_ids.intersection(source_ids):
        raise OpeningBindingError("source opening IDs must be unique, including window IDs")
    spaces = {z.cell_id for z in bg.zone_volumes}
    registry = NameRegistry()
    registry.used.update(s.name for s in bg.surfaces)
    registry.used.update(w.name for w in bg.windows)
    output = []
    for source in sorted(geom.openings, key=lambda o: o.id):
        if source.space_id not in spaces or (source.other_space_id is not None and source.other_space_id not in spaces):
            raise OpeningBindingError(f"opening {source.id}: unknown source space")
        sides = [(source.space_id, source.other_space_id)]
        if source.other_space_id is not None:
            sides.append((source.other_space_id, source.space_id))
        derived = []
        for space_id, other_id in sides:
            pieces, direction = _host_pieces(source, space_id, other_id, bg)
            for index, (face, part) in enumerate(pieces):
                vertices = _orient(_lift(list(part.exterior.coords)[:-1], source.p1, direction), _newell(face.verts))
                derived.append(Opening(
                    name=registry.uname(f"Opening_{source.id}_{space_id}_{index}"),
                    parent=face.name, verts=vertices, source_opening_id=source.id,
                    kind=source.kind, space_id=space_id, other_space_id=other_id, state=source.state,
                ))
        by_parent = {o.parent: o for o in derived}
        faces = {s.name: s for s in bg.surfaces}
        for opening in derived:
            if opening.other_space_id is not None:
                partner = by_parent.get(faces[opening.parent].obc_obj)
                if partner is None:
                    raise OpeningBindingError(f"opening {source.id}: missing reciprocal opening side")
                opening.partner = partner.name
        output.extend(derived)

    # Apertures must not overlap each other or an existing window on a host.
    by_parent = {}
    for opening in [*bg.windows, *output]:
        by_parent.setdefault(opening.parent, []).append(opening)
    faces = {s.name: s for s in bg.surfaces}
    for parent, rows in by_parent.items():
        if len(rows) < 2 or not any(isinstance(row, Opening) for row in rows):
            continue
        points = sorted(set(tuple(v[:2]) for v in faces[parent].verts))
        direction, _ = _frame(points[0], points[-1])
        polygons = [_project(row.verts, points[0], direction) for row in rows]
        for i, first in enumerate(polygons):
            for j in range(i + 1, len(polygons)):
                if first.intersection(polygons[j]).area > REALIZATION_EPS_M ** 2:
                    raise OpeningBindingError(f"overlapping apertures {rows[i].name} and {rows[j].name}")
    return output


def visible_wall_parts(data: dict) -> dict[str, list[dict]]:
    """Subtract explicit apertures for display, retaining authoritative parents.

    Door openings touching a floor become notches; raised openings become inner
    rings. Parts carry their original parent name so selecting a display piece
    still refers to the same source wall. Empty walls stay empty (full passage).
    """
    by_parent = {}
    for opening in data.get("openings", []):
        by_parent.setdefault(opening["parent"], []).append(opening)
    result = {}
    for face in data.get("surfaces", []):
        apertures = by_parent.get(face["name"])
        if not apertures:
            continue
        points = sorted(set(tuple(v[:2]) for v in face["verts"]))
        direction, _ = _frame(points[0], points[-1])
        parent = _project(face["verts"], points[0], direction)
        holes = [_project(o["verts"], points[0], direction) for o in apertures]
        if parent is None or any(h is None for h in holes):
            raise OpeningBindingError(f"opening display geometry is not on wall {face['name']}")
        difference = parent.difference(unary_union(holes))
        if difference.is_empty:
            parts = []
        elif difference.geom_type == "Polygon":
            parts = [difference]
        elif difference.geom_type == "MultiPolygon":
            parts = list(difference.geoms)
        else:
            raise OpeningBindingError(f"unsupported cut wall {face['name']}")
        result[face["name"]] = [{
            "verts": _lift(list(part.exterior.coords)[:-1], points[0], direction),
            "holes": [_lift(list(ring.coords)[:-1], points[0], direction) for ring in part.interiors],
        } for part in parts]
    return result


def require_supported_ep_openings(bg: BuildingGeometry) -> None:
    if bg.openings:
        raise ValueError(
            "EnergyPlus export of source doors/open passages is not implemented; "
            "refusing to silently replace them with solid walls: "
            + ", ".join(sorted({o.source_opening_id for o in bg.openings}))
        )
