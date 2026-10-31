"""Explicit, fail-closed EnergyPlus adapter for source-BIM doors.

The source BIM keeps one aperture and its complete physical hosts.  This
adapter derives the potentially many EnergyPlus subsurfaces only after thermal
surface pairing.  It deliberately models a door as an opaque, closed thermal
subsurface; airflow through a door or an unfilled opening is outside this
adapter's scope.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry.modelling import BuildingGeometry, NameRegistry, Opening, _newell, _orient
from src.agent.geometry.openings import _frame, _lift, _project


# This is only a geometric comparison allowance for the derived EP view.  It
# is not a source-model snap, regularisation, or a minimum door size.
TOL = 2e-6


class EPOpeningError(ValueError):
    """A source opening cannot be represented by this bounded EP adapter."""


def _parts(shape):
    if shape.geom_type == "Polygon":
        return [shape] if shape.area > TOL ** 2 else []
    return [part for geom in getattr(shape, "geoms", []) for part in _parts(geom)]


def _polygon(vertices, boundary):
    """Project a wall polygon into the source boundary's along-wall/z frame."""
    origin = boundary["vertices"][0][:2]
    direction, _ = _frame(origin, boundary["vertices"][1][:2])
    result = _project(vertices, origin, direction)
    if result is None or not result.is_valid or result.area <= TOL ** 2:
        raise EPOpeningError(f"invalid or off-plane opening geometry on source boundary {boundary['id']}")
    return result, origin, direction


def _covered(aperture, pieces, *, opening_id: str, boundary_id: str):
    union = unary_union(pieces)
    if (union.is_empty or not union.buffer(TOL).covers(aperture)
            or not aperture.buffer(TOL).covers(union)
            or sum(piece.area for piece in pieces) - union.area > TOL ** 2):
        raise EPOpeningError(
            f"door {opening_id}: derived parent pieces do not cover source host {boundary_id} exactly"
        )


def _policy_for(opening: dict, policy: dict[str, dict]) -> dict:
    oid = opening["id"]
    if oid not in policy:
        raise EPOpeningError(f"door {oid}: explicit closed-door policy is required")
    item = policy[oid]
    if not isinstance(item, dict):
        raise EPOpeningError(f"door {oid}: policy must be an object")
    if item.get("state") != "closed":
        raise EPOpeningError(f"door {oid}: EP adapter only accepts an explicit closed state")
    construction = item.get("construction")
    reason = item.get("reason")
    if not isinstance(construction, str) or not construction.strip():
        raise EPOpeningError(f"door {oid}: closed-door policy needs a construction")
    if not isinstance(reason, str) or not reason.strip():
        raise EPOpeningError(f"door {oid}: closed-door policy needs a nonempty reason")
    return {"state": "closed", "construction": construction.strip(), "reason": reason.strip()}


def _same_piece(first: Opening, second: Opening) -> bool:
    a = np.asarray(first.verts, dtype=float)
    b = np.asarray(second.verts, dtype=float)
    if len(a) != len(b) or np.linalg.norm(_newell(first.verts)) < .99 or np.linalg.norm(_newell(second.verts)) < .99:
        return False
    if _newell(first.verts) @ _newell(second.verts) > -.999:
        return False
    normal = _newell(first.verts)
    if np.max(np.abs((b - a[0]) @ normal)) > TOL:
        return False
    axis = int(np.argmax(np.abs(normal)))
    pa = Polygon(np.delete(a, axis, axis=1))
    pb = Polygon(np.delete(b, axis, axis=1))
    return pa.symmetric_difference(pb).area <= TOL ** 2


def _check_no_window_overlap(doors: list[Opening], bg: BuildingGeometry) -> None:
    """Check actual derived parent pieces, including a door crossing a cut wall."""
    parents = {surface.name: surface for surface in bg.surfaces}
    rows: dict[str, list[Any]] = defaultdict(list)
    for item in [*bg.windows, *doors]:
        parent = parents.get(item.parent)
        if parent is None:
            raise EPOpeningError(f"derived aperture {item.name} has no parent surface")
        shape, _, _ = _polygon(item.verts, {"id": parent.name, "vertices": [list(v) for v in parent.verts]})
        rows[item.parent].append((item, shape))
    for parent, apertures in rows.items():
        for index, (first, first_shape) in enumerate(apertures):
            for second, second_shape in apertures[index + 1:]:
                if first_shape.intersection(second_shape).area > TOL ** 2:
                    raise EPOpeningError(f"overlapping derived apertures {first.name} and {second.name} on {parent}")


def derive_ep_doors(
    source: dict,
    bg: BuildingGeometry,
    surface_mapping: dict[str, str],
    door_policy: dict[str, dict],
) -> tuple[list[Opening], dict]:
    """Derive closed Door subsurfaces from a frozen ``source_bim_v2``.

    ``surface_mapping`` maps every derived EP base-surface name to its complete
    source boundary identity.  The returned audit carries every non-source
    choice needed by :func:`write_ep_doors`; callers can persist it verbatim.
    """
    if source.get("schema_version") != "source_bim_v2":
        raise EPOpeningError("EP doors require source_bim_v2")
    if not isinstance(door_policy, dict):
        raise EPOpeningError("door policy must map source opening IDs to objects")
    boundaries = {boundary["id"]: boundary for boundary in source.get("boundaries", [])}
    spaces = {space["id"] for space in source.get("spaces", [])}
    faces = {surface.name: surface for surface in bg.surfaces}
    source_space_by_zone = {volume.zone: volume.cell_id for volume in bg.zone_volumes}
    if len(boundaries) != len(source.get("boundaries", [])) or len(faces) != len(bg.surfaces):
        raise EPOpeningError("duplicate source boundary or derived surface identity")
    if set(surface_mapping) != set(faces):
        raise EPOpeningError("surface mapping must cover every derived EP surface exactly once")
    if any(boundary_id not in boundaries for boundary_id in surface_mapping.values()):
        raise EPOpeningError("surface mapping refers to an unknown source boundary")

    openings = source.get("openings", [])
    if len({opening.get("id") for opening in openings}) != len(openings):
        raise EPOpeningError("duplicate source opening identity")
    door_sources = [opening for opening in openings if opening.get("kind") == "door"]
    source_ids = {opening["id"] for opening in door_sources}
    # An empty passage, and a door whose source state is known open, cannot be
    # silently converted to an opaque element.  A source ``unknown`` is only
    # accepted through the explicit, recorded backend policy below.
    for opening in openings:
        if opening.get("kind") == "open" or opening.get("connectivity") == "open":
            raise EPOpeningError(f"opening {opening.get('id')}: open passages/open doors have no EP door strategy")
    unknown_policy = set(door_policy).difference(source_ids)
    if unknown_policy:
        raise EPOpeningError(f"door policy names no source door: {sorted(unknown_policy)}")

    registry = NameRegistry()
    registry.used.update(faces)
    registry.used.update(window.name for window in bg.windows)
    output: list[Opening] = []
    audit_doors: dict[str, dict] = {}
    hosts_by_opening = source.get("opening_hosts", {})

    for opening in sorted(door_sources, key=lambda row: row["id"]):
        oid = opening["id"]
        policy = _policy_for(opening, door_policy)
        hosts = hosts_by_opening.get(oid)
        interior = not bool(opening.get("exterior"))
        expected_host_count = 2 if interior else 1
        if (not isinstance(hosts, list) or len(hosts) != expected_host_count or len(set(hosts)) != len(hosts)
                or any(host not in boundaries for host in hosts)):
            raise EPOpeningError(f"door {oid}: source opening_hosts must contain {expected_host_count} unique wall host(s)")
        if opening.get("host_boundary_id") != hosts[0]:
            raise EPOpeningError(f"door {oid}: primary source host disagrees with opening_hosts")
        space_ids = opening.get("space_ids")
        if (not isinstance(space_ids, list) or len(space_ids) != expected_host_count
                or len(set(space_ids)) != len(space_ids) or any(space not in spaces for space in space_ids)):
            raise EPOpeningError(f"door {oid}: source space identities disagree with exterior flag")
        if interior and set(space_ids) != {boundaries[host]["space_id"] for host in hosts}:
            raise EPOpeningError(f"door {oid}: source host spaces disagree with opening spaces")
        if not interior and boundaries[hosts[0]]["space_id"] != space_ids[0]:
            raise EPOpeningError(f"door {oid}: exterior source host space disagrees with opening space")

        derived: list[Opening] = []
        for host_index, bid in enumerate(hosts):
            boundary = boundaries[bid]
            if boundary.get("geometry_type") != "wall" or boundary.get("kind") != "physical":
                raise EPOpeningError(f"door {oid}: source host {bid} must be a physical wall")
            aperture, origin, direction = _polygon(opening["vertices"], boundary)
            candidates = []
            for name, face in sorted(faces.items()):
                if surface_mapping[name] != bid:
                    continue
                if face.stype != "Wall" or source_space_by_zone.get(face.zone) != boundary["space_id"]:
                    raise EPOpeningError(f"door {oid}: mapped parent {name} disagrees with source host {bid}")
                parent, _, _ = _polygon(face.verts, boundary)
                overlap = parent.intersection(aperture)
                for piece in _parts(overlap):
                    vertices = _orient(_lift(list(piece.exterior.coords)[:-1], origin, direction), _newell(face.verts))
                    candidates.append((face, piece, vertices))
            _covered(aperture, [piece for _face, piece, _vertices in candidates], opening_id=oid, boundary_id=bid)
            for piece_index, (face, _piece, vertices) in enumerate(candidates):
                other_space = next((space for space in space_ids if space != boundary["space_id"]), None)
                if interior:
                    partner_face = faces.get(face.obc_obj)
                    if face.obc != "Surface" or partner_face is None or partner_face.obc_obj != face.name:
                        raise EPOpeningError(f"door {oid}: parent {face.name} lacks a reciprocal interzone base surface")
                    if (source_space_by_zone.get(partner_face.zone) != other_space
                            or surface_mapping.get(partner_face.name) not in hosts):
                        raise EPOpeningError(f"door {oid}: parent {face.name} connects a source space not declared by the door")
                elif face.obc != "Outdoors":
                    raise EPOpeningError(f"door {oid}: exterior parent {face.name} is not Outdoors")
                derived.append(Opening(
                    name=registry.uname(f"EP_Door_{oid}_{host_index + 1}_{piece_index + 1}"),
                    parent=face.name, verts=vertices, source_opening_id=oid, kind="door",
                    space_id=boundary["space_id"], other_space_id=other_space, state="closed",
                ))

        if interior:
            by_parent: dict[str, list[Opening]] = defaultdict(list)
            for item in derived:
                by_parent[item.parent].append(item)
            for item in derived:
                reciprocal_parent = faces[item.parent].obc_obj
                matches = [candidate for candidate in by_parent[reciprocal_parent] if _same_piece(item, candidate)]
                if len(matches) != 1:
                    raise EPOpeningError(f"door {oid}: no one-to-one reciprocal derived door for {item.parent}")
                item.partner = matches[0].name
        _check_no_window_overlap(derived, bg)
        output.extend(derived)
        audit_doors[oid] = {
            **policy,
            "source_connectivity": opening.get("connectivity"),
            "source_hosts": list(hosts),
            "source_spaces": list(space_ids),
            "exterior": not interior,
            "surfaces": [item.name for item in derived],
        }

    _check_no_window_overlap(output, bg)
    audit = {"source_doors": len(door_sources), "door_surfaces": len(output), "doors": audit_doors}
    return output, audit


def _opaque_construction(idf, construction: str) -> str:
    matches = [obj for obj in idf.idfobjects["CONSTRUCTION"] if str(obj.Name).casefold() == construction.casefold()]
    if len(matches) != 1:
        raise EPOpeningError(f"door construction {construction!r} is absent or ambiguous")
    layers = [str(getattr(matches[0], field, "")).strip() for field in matches[0].fieldnames if "Layer" in field]
    if not any(layers):
        raise EPOpeningError(f"door construction {construction!r} has no material layer")
    opaque_materials = {
        str(obj.Name).casefold()
        for key in ("MATERIAL", "MATERIAL:NOMASS", "MATERIAL:AIRGAP")
        for obj in idf.idfobjects[key]
    }
    glazing = {
        str(obj.Name).casefold()
        for key, objects in idf.idfobjects.items() if key.startswith("WINDOWMATERIAL:")
        for obj in objects
    }
    unknown = [layer for layer in layers if layer and layer.casefold() not in opaque_materials]
    if unknown:
        kind = "not opaque" if any(layer.casefold() in glazing for layer in unknown) else "has an unknown/non-opaque layer"
        raise EPOpeningError(f"door construction {construction!r} {kind}: {unknown}")
    return matches[0].Name


def _audit_rows(doors: list[Opening], audit: dict) -> dict[str, dict]:
    if not isinstance(audit, dict) or not isinstance(audit.get("doors"), dict):
        raise EPOpeningError("door audit must contain per-source-door records")
    grouped: dict[str, list[Opening]] = defaultdict(list)
    for door in doors:
        if door.kind != "door" or door.state != "closed":
            raise EPOpeningError(f"derived opening {door.name} is not a closed Door")
        grouped[door.source_opening_id].append(door)
    if audit.get("source_doors") != len(grouped) or audit.get("door_surfaces") != len(doors) or set(audit["doors"]) != set(grouped):
        raise EPOpeningError("door audit counts or source identities disagree with derived doors")
    for oid, items in grouped.items():
        row = audit["doors"][oid]
        if not isinstance(row, dict) or set(row.get("surfaces", [])) != {item.name for item in items}:
            raise EPOpeningError(f"door audit for {oid} does not cover its derived surfaces")
        _policy_for({"id": oid}, {oid: row})
    return audit["doors"]


def write_ep_doors(idf, doors: list[Opening], audit: dict) -> None:
    """Write opaque Door subsurfaces and validate their source-derived pairing.

    ``audit`` is intentionally the result of :func:`derive_ep_doors`; this
    prevents a caller from changing a construction or the reason between
    geometry derivation and IDF materialisation.
    """
    rows = _audit_rows(doors, audit)
    parents = {surface.Name: surface for surface in idf.idfobjects["BUILDINGSURFACE:DETAILED"]}
    existing = {surface.Name for surface in idf.idfobjects["FENESTRATIONSURFACE:DETAILED"]}
    if len(existing) != len(idf.idfobjects["FENESTRATIONSURFACE:DETAILED"]):
        raise EPOpeningError("duplicate existing FenestrationSurface:Detailed names")
    if any(door.name in existing for door in doors) or len({door.name for door in doors}) != len(doors):
        raise EPOpeningError("derived door names collide with existing IDF fenestration")
    constructions = {oid: _opaque_construction(idf, row["construction"]) for oid, row in rows.items()}

    by_name = {door.name: door for door in doors}
    for door in doors:
        parent = parents.get(door.parent)
        if parent is None:
            raise EPOpeningError(f"door {door.name} has no IDF base surface {door.parent}")
        if door.partner:
            partner = by_name.get(door.partner)
            if partner is None or partner.partner != door.name:
                raise EPOpeningError(f"door {door.name} lacks a reciprocal derived door")
            if (parent.Outside_Boundary_Condition != "Surface"
                    or parent.Outside_Boundary_Condition_Object != partner.parent):
                raise EPOpeningError(f"door {door.name} parent is not reciprocal with {partner.parent}")
        elif parent.Outside_Boundary_Condition != "Outdoors":
            raise EPOpeningError(f"exterior door {door.name} parent is not Outdoors")
        obj = idf.newidfobject(
            "FenestrationSurface:Detailed", Name=door.name, Surface_Type="Door",
            Construction_Name=constructions[door.source_opening_id], Building_Surface_Name=door.parent,
            Outside_Boundary_Condition_Object=door.partner or "",
        )
        obj.Number_of_Vertices = len(door.verts)
        for index, vertex in enumerate(door.verts, 1):
            setattr(obj, f"Vertex_{index}_Xcoordinate", round(vertex[0], 4))
            setattr(obj, f"Vertex_{index}_Ycoordinate", round(vertex[1], 4))
            setattr(obj, f"Vertex_{index}_Zcoordinate", round(vertex[2], 4))

    written = {obj.Name: obj for obj in idf.idfobjects["FENESTRATIONSURFACE:DETAILED"] if obj.Name in by_name}
    if set(written) != set(by_name):
        raise EPOpeningError("not every derived door was written to the IDF")
    for name, door in by_name.items():
        obj = written[name]
        if (obj.Surface_Type != "Door" or obj.Building_Surface_Name != door.parent
                or str(obj.Construction_Name).casefold() != constructions[door.source_opening_id].casefold()):
            raise EPOpeningError(f"IDF Door {name} disagrees with derived audit")
        parent = parents[door.parent]
        parent_boundary = {"id": parent.Name, "vertices": [list(vertex) for vertex in parent.coords]}
        parent_shape, _, _ = _polygon(parent.coords, parent_boundary)
        door_shape, _, _ = _polygon(obj.coords, parent_boundary)
        if not parent_shape.buffer(TOL).covers(door_shape):
            raise EPOpeningError(f"IDF Door {name} is not fully covered by its parent {door.parent}")
        if _newell(obj.coords) @ _newell(parent.coords) < .999:
            raise EPOpeningError(f"IDF Door {name} winding disagrees with its parent {door.parent}")
        if door.partner:
            partner = written[door.partner]
            if obj.Outside_Boundary_Condition_Object != partner.Name or partner.Outside_Boundary_Condition_Object != obj.Name:
                raise EPOpeningError(f"IDF Door {name} is not reciprocal")
            first = Opening(name, door.parent, [tuple(vertex) for vertex in obj.coords], door.source_opening_id,
                            "door", door.space_id, door.other_space_id, "closed", door.partner)
            second = Opening(partner.Name, partner.Building_Surface_Name,
                             [tuple(vertex) for vertex in partner.coords], door.source_opening_id,
                             "door", door.other_space_id or door.space_id, door.space_id, "closed", name)
            if not _same_piece(first, second):
                raise EPOpeningError(f"IDF Door {name} and {partner.Name} differ geometrically")
        elif obj.Outside_Boundary_Condition_Object:
            raise EPOpeningError(f"exterior IDF Door {name} must not have a boundary object")
