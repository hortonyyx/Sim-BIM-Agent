"""Solver-independent source BIM: full boundaries, contact regions and openings.

Contact regions describe actual adjacency. They never split source boundaries
or create EnergyPlus surfaces. The viewer is a separate, disposable projection.
"""
from __future__ import annotations

import hashlib
import math
from itertools import combinations
from urllib.parse import quote

from shapely.affinity import translate
from shapely.geometry import Polygon, box, LineString, Point
from shapely.ops import unary_union

from src.agent.correction.schema import CorrectedGeometry, SourceOpening
from src.agent.correction.cell_geometry import cell_polygon
from src.agent.geometry.build import verify_geometry_input
from src.agent.geometry.capability import require_supported_geometry_contract, schema_supports, FEATURE_CELL_POLYGON, FEATURE_SIMPLE_POLYGON
from src.agent.geometry.modelling import Surface, _FACADE_NORMAL, _legacy_cardinal_window_verts, _newell, _orient
from src.agent.geometry.openings import _frame, _lift, _project
from src.agent.geometry.source_model import _digest, source_primitives

# Numerical comparison only, not a drawing snap distance or minimum EP edge.
EPS = 1e-7


def _coverage_regions(expected, actual):
    """Numerically resolvable missing/outside regions; never alter source shapes."""
    return expected.difference(actual.buffer(EPS)), actual.difference(expected.buffer(EPS))


def _space_overlap_area(a, b):
    """Return original overlap area only for a numerically resolvable intrusion.

    Area alone amplifies a sub-EPS boundary offset along a long shared edge.
    Test whether the intersection retains interior after an EPS/2 inset.
    A long strip must therefore be wider than EPS; for corners and wedges this
    is an interior-radius test, not a maximum penetration-depth test. This
    temporary erosion is only a predicate: source polygons, contacts and
    reported volumes are unchanged.
    Full containment (including duplicate footprints) is always a conflict,
    even when a valid contained space is narrower than the length tolerance.
    """
    overlap = a.intersection(b)
    area = overlap.area
    if area <= EPS ** 2:
        return 0.0
    if a.covers(b) or b.covers(a):
        return area
    # GEOS can collapse even metre-wide polygons when applying a sub-micron
    # inset at large world coordinates. Only the temporary predicate geometry
    # is moved near the origin; retain the original intersection area above.
    x_min, y_min, _, _ = overlap.bounds
    local_overlap = translate(overlap, xoff=-x_min, yoff=-y_min)
    return area if local_overlap.buffer(-EPS / 2).area > 0.0 else 0.0


def _parts(shape):
    if shape.geom_type == "Polygon":
        return [shape] if shape.area > EPS ** 2 else []
    return [p for g in getattr(shape, "geoms", []) for p in _parts(g)]


def _on_wall(vertices, boundary):
    origin = boundary.vertices[0][:2]
    direction, _ = _frame(origin, boundary.vertices[1][:2])
    return _project_on_wall(vertices, origin, direction)


def _project_on_wall(vertices, origin, direction):
    # Do not use the EP realization tolerance for source coplanarity.
    if any(abs((v[0]-origin[0])*direction[1] - (v[1]-origin[1])*direction[0]) > EPS for v in vertices):
        return None
    return _project(vertices, origin, direction)


def _contained(vertices, boundary):
    aperture = _on_wall(vertices, boundary)
    parent = _wall_polygon(boundary)
    return (aperture is not None and aperture.is_valid and aperture.area > EPS ** 2
            and parent.buffer(EPS).covers(aperture))


def _wall_polygon(boundary, *, origin=None, direction=None):
    """Project the complete physical wall, including any shell interior rings."""
    if origin is None:
        origin = boundary.vertices[0][:2]
        direction, _ = _frame(origin, boundary.vertices[1][:2])
    exterior = _project_on_wall(boundary.vertices, origin, direction)
    holes = [_project_on_wall(ring, origin, direction) for ring in boundary.holes]
    if exterior is None or any(hole is None for hole in holes):
        return None
    return exterior.difference(unary_union(holes)) if holes else exterior


def _opening_height_host_diagnostic(aperture, spaces_by_id):
    """Describe declared opening heights without altering host resolution.

    Wall-host containment is intentionally three-dimensional.  A plan-correct
    upper-storey aperture expressed with storey-relative Z therefore has no
    host.  Keeping this explanation beside the existing unbuilt record makes
    that distinction visible without accepting, translating, or correcting it.
    """
    opening_z = [float(aperture.z[0]), float(aperture.z[1])]
    declared_sides = []
    for label, space_id in (("space", aperture.space_id), ("other_space", aperture.other_space_id)):
        if space_id is None:
            continue
        space = spaces_by_id.get(space_id)
        if space is None:
            declared_sides.append({"side": label, "space_id": space_id, "status": "unknown_source_space"})
            continue
        expected = [float(space.z_floor), float(space.z_floor + space.height)]
        below = max(expected[0] - opening_z[0], 0.0)
        above = max(opening_z[1] - expected[1], 0.0)
        declared_sides.append({
            "side": label,
            "space_id": space_id,
            "floor_id": space.floor_id,
            "expected_z_bounds_m": expected,
            "actual_opening_z_bounds_m": opening_z,
            "below_floor_m": below,
            "above_ceiling_m": above,
            "within_declared_space_z_bounds": below == 0.0 and above == 0.0,
        })
    return {
        "actual_opening_z_bounds_m": opening_z,
        "declared_space_bounds": declared_sides,
        "scope": "diagnostic only; host containment and source geometry are unchanged",
    }


def _contacts(boundaries):
    """Record many-to-many contact patches without changing boundary geometry."""
    ordered = sorted(boundaries.values(), key=lambda b: b.id)
    # Boundaries are immutable during contact discovery. Keep these invariants
    # local to this call, preserving the original projection and intersection
    # operands (including their vertex order) for every surviving pair.
    walls = {}
    horizontal = {}
    for boundary in ordered:
        vertices = boundary.vertices
        if boundary.geometry_type == "wall":
            origin = vertices[0][:2]
            direction, _ = _frame(origin, vertices[1][:2])
            walls[boundary.id] = (
                _newell(vertices), origin, direction,
                _wall_polygon(boundary, origin=origin, direction=direction),
                min(v[2] for v in vertices), max(v[2] for v in vertices),
            )
        elif boundary.geometry_type in {"floor", "ceiling"}:
            polygon = Polygon([v[:2] for v in vertices], [[v[:2] for v in h] for h in boundary.holes])
            horizontal[boundary.id] = (vertices[0][2], polygon, polygon.bounds)
    relations = []
    for a, b in combinations(ordered, 2):
        if a.space_id == b.space_id:
            continue
        regions = []
        if a.geometry_type == b.geometry_type == "wall":
            normal_a, origin, direction, parent, z_min_a, z_max_a = walls[a.id]
            normal_b, _, _, _, z_min_b, z_max_b = walls[b.id]
            # Wall projection leaves Z unchanged. Disjoint or touching Z
            # intervals cannot produce a positive-area contact. Do not use an
            # XY box here: source coplanarity deliberately permits EPS offsets.
            if z_max_a <= z_min_b or z_max_b <= z_min_a:
                continue
            if float(normal_a @ normal_b) > -1 + EPS:
                continue
            projected = _wall_polygon(b, origin=origin, direction=direction)
            if projected is None:
                continue
            contact = parent.intersection(projected)
            lift = lambda ring: _lift(list(ring), origin, direction)
        elif {a.geometry_type, b.geometry_type} == {"floor", "ceiling"}:
            z, polygon_a, bounds_a = horizontal[a.id]
            z_b, polygon_b, bounds_b = horizontal[b.id]
            if abs(z - z_b) > EPS:
                continue
            if (bounds_a[2] <= bounds_b[0] or bounds_b[2] <= bounds_a[0]
                    or bounds_a[3] <= bounds_b[1] or bounds_b[3] <= bounds_a[1]):
                continue
            contact = polygon_a.intersection(polygon_b)
            lift = lambda ring: [[x, y, z] for x, y in ring]
        else:
            continue
        for part in _parts(contact):
            regions.append({"vertices": lift(list(part.exterior.coords)[:-1]),
                            "holes": [lift(list(r.coords)[:-1]) for r in part.interiors],
                            "area_m2": part.area})
        if not regions:
            continue
        a.adjacent_space_ids.append(b.space_id)
        b.adjacent_space_ids.append(a.space_id)
        a.counterpart_ids.append(b.id)
        b.counterpart_ids.append(a.id)
        relations.append({"boundary_ids": [a.id, b.id], "space_ids": [a.space_id, b.space_id],
                          "regions": regions})
    for boundary in boundaries.values():
        boundary.adjacent_space_ids = sorted(set(boundary.adjacent_space_ids))
        boundary.counterpart_ids = sorted(set(boundary.counterpart_ids))
    return relations


def build_source_bim(geom: CorrectedGeometry, *, capability_profile="rectangular", window_host_proof=None,
                     enclosure_declaration: dict | None = None, wall_readings=None) -> dict:
    """Generate geometric source objects without cut/pair, physics or IDF calls.

    Invalid base space geometry fails explicitly. Unresolved openings remain in
    the candidate's unbuilt inventory and block source readiness, while allowing
    its valid space geometry to be inspected.
    """
    require_supported_geometry_contract(geom, capability_profile)
    simple_polygon = schema_supports(geom, FEATURE_SIMPLE_POLYGON)
    geom, proof_artifact = verify_geometry_input(geom, window_host_proof)
    findings = []

    def fail(code, **evidence):
        findings.append({"code": code, "severity": "severe", **evidence})

    if not geom.floors or not any(f.cells for f in geom.floors):
        raise ValueError("source BIM requires at least one source space")
    floor_ids = [str(getattr(f, "id", None) or f.name) for f in geom.floors]
    if any(not fid.strip() for fid in floor_ids) or len(set(floor_ids)) != len(floor_ids):
        raise ValueError("source floor identities must be nonempty and unique")
    for floor in geom.floors:
        if not math.isfinite(floor.z_floor) or not math.isfinite(floor.ceiling_height) or floor.ceiling_height <= 0:
            raise ValueError(f"floor {floor.name}: height/base must be finite and height positive")
        for cell in floor.cells:
            if getattr(cell, 'holes', []) and not simple_polygon:
                raise ValueError('space holes require source_polygon_v1')
            if getattr(cell, 'vertical_sections', []) and not simple_polygon:
                raise ValueError('vertical sections require source_polygon_v1')
            if not cell.id.strip():
                raise ValueError("source space identity must not be empty")
            if cell.polygon and not schema_supports(geom, FEATURE_CELL_POLYGON):
                raise ValueError(f"space {cell.id}: polygon requires a polygon-capable schema")
            if not cell.polygon and (len(cell.x) != 2 or len(cell.y) != 2 or
                                     cell.x[0] >= cell.x[1] or cell.y[0] >= cell.y[1]):
                raise ValueError(f"space {cell.id}: rectangle intervals must be positive")
            coords = cell.polygon or [[cell.x[0], cell.y[0]], [cell.x[1], cell.y[0]],
                                      [cell.x[1], cell.y[1]], [cell.x[0], cell.y[1]]]
            if not all(len(p) == 2 and all(math.isfinite(v) for v in p) for p in coords):
                raise ValueError(f"space {cell.id}: non-finite or malformed coordinates")
            poly = Polygon(coords)
            if not poly.is_valid or poly.area <= EPS ** 2:
                raise ValueError(f"space {cell.id}: invalid or empty footprint")
            if not simple_polygon and any(abs(a[0]-b[0]) > EPS and abs(a[1]-b[1]) > EPS for a,b in zip(coords, coords[1:]+coords[:1])):
                raise ValueError(f"space {cell.id}: non-orthogonal source geometry is not supported yet")

    # Reuse polygon validity/bounds checks, without the legacy EP kernel's
    # configurable minimum edge length. Small valid source spaces stay present.
    from src.agent.geometry.reader_wall_metadata import thickness_breakpoints
    wall_breakpoints = thickness_breakpoints(wall_readings or [])
    spaces, boundaries, polygons, by_space = source_primitives(
        geom, polygon_builder=lambda cell: cell_polygon(cell, require_orthogonal=not simple_polygon),
        wall_breakpoints=wall_breakpoints)
    from src.agent.roles import require_role
    for space in spaces:
        space.role = require_role(space.role)
    source_spaces_by_id = {space.id: space for space in spaces}
    for floor, fid in zip(geom.floors, floor_ids):
        footprint = getattr(floor, "footprint", None)
        expected = Polygon(footprint.vertices, getattr(footprint, 'holes', [])) if footprint else box(geom.footprint_x[0], geom.footprint_y[0], geom.footprint_x[1], geom.footprint_y[1])
        if not expected.is_valid or expected.area <= EPS ** 2:
            raise ValueError(f"floor {fid}: invalid declared footprint")
        spanning = floor.spanning_space_ids
        if len(spanning) != len(set(spanning)):
            raise ValueError(f"floor {fid}: duplicate spanning space references")
        for sid in spanning:
            space = source_spaces_by_id.get(sid)
            if space is None:
                raise ValueError(f"floor {fid}: unknown spanning space {sid}")
            if space.floor_id == fid:
                raise ValueError(f"floor {fid}: spanning space {sid} is already a local member")
            if space.z_floor > floor.z_floor + EPS or space.z_floor + space.height < floor.z_floor + floor.ceiling_height - EPS:
                raise ValueError(f"floor {fid}: spanning space {sid} does not cover the whole storey height")
        spanning_polygons = []
        for sid in spanning:
            space = source_spaces_by_id[sid]
            if space.vertical_sections:
                from src.agent.geometry.source_volume import section_polygon, space_sections
                spanning_polygons.append(unary_union([section_polygon(row) for row in space_sections(space)
                    if min(row['z'][1], floor.z_floor + floor.ceiling_height) - max(row['z'][0], floor.z_floor) > EPS]))
            else:
                spanning_polygons.append(polygons[sid])
        actual = unary_union([polygons[c.id] for c in floor.cells] + spanning_polygons)
        missing_region, outside_region = _coverage_regions(expected, actual)
        missing, outside = missing_region.area, outside_region.area
        if missing > EPS ** 2 or outside > EPS ** 2:
            fail("source.floor_coverage", floor_id=fid, missing_area_m2=missing, outside_area_m2=outside)
    for boundary in boundaries.values():
        if boundary.geometry_type == "floor":
            boundary.vertices.reverse()  # source space outward normal is down
            for hole in boundary.holes:
                hole.reverse()
    for a, b in combinations(spaces, 2):
        height = min(a.z_floor+a.height, b.z_floor+b.height) - max(a.z_floor, b.z_floor)
        if height > EPS:
            if a.vertical_sections or b.vertical_sections:
                from src.agent.geometry.source_volume import space_overlap_volume
                volume = space_overlap_volume(a, b, area_function=_space_overlap_area)
            else:
                volume = _space_overlap_area(polygons[a.id], polygons[b.id]) * height
            if volume:
                fail("source.space_overlap", space_ids=[a.id, b.id], volume_m3=volume)
    relations = _contacts(boundaries)
    openings, unbuilt, connections = [], [], []
    bindings = {}
    seen_ids = set()
    spaces_by_id = {space.id: space for space in spaces}

    def hosts(vertices, sid):
        return [boundaries[bid] for bid in by_space.get(sid, [])
                if boundaries[bid].geometry_type == "wall" and _contained(vertices, boundaries[bid])]

    def contact_shapes(boundary, other=None):
        return [_on_wall(region["vertices"], boundary).difference(unary_union([
                    _on_wall(hole, boundary) for hole in region.get("holes", [])]))
                for rel in relations if boundary.id in rel["boundary_ids"]
                and (other is None or other in rel["space_ids"]) for region in rel["regions"]]

    def exterior(vertices, boundary):
        aperture = _on_wall(vertices, boundary)
        return not any(aperture.intersection(p).area > EPS ** 2 for p in contact_shapes(boundary))

    def reject(source, message, *, diagnostic=None):
        row = {"id": source.id, "record": source.model_dump(mode="json"), "reason": message}
        if diagnostic is not None:
            row["height_host_diagnostic"] = diagnostic
        unbuilt.append(row)
        fail("source.opening_unbuilt", opening_id=source.id, reason=message,
             **({"height_host_diagnostic": diagnostic} if diagnostic is not None else {}))

    resolutions = {r.window_id: r for r in proof_artifact.claims.resolutions} if proof_artifact else {}
    opening_segment_groups = {}
    used_aperture_ids = {o.id for o in geom.openings} | {str(w.id) for w in geom.windows}
    for window in geom.windows:
        wid = str(window.id)
        if wid in seen_ids:
            reject(window, "duplicate source opening identity")
            continue
        seen_ids.add(wid)
        owner = next((s for s in spaces if s.id == window.room), None)
        owner_floor = next((f for f in geom.floors if str(getattr(f, "id", None) or f.name) == owner.floor_id), None) if owner else None
        if owner_floor is None or (getattr(window, "floor_id", None) or window.floor) not in {owner.floor_id, owner_floor.name}:
            reject(window, "window floor and source room disagree")
            continue
        if (len(window.span) != 2 or len(window.z) != 2 or
                not all(math.isfinite(v) for v in [*window.span, *window.z]) or
                window.span[0] >= window.span[1] or window.z[0] >= window.z[1]):
            reject(window, "window span/height must be positive finite intervals")
            continue
        candidates, partials = [], []
        resolution = resolutions.get(wid)
        want = resolution.segment_outward_normal if resolution else _FACADE_NORMAL[window.facade.lower()]
        for bid in by_space.get(window.room, []):
            boundary = boundaries[bid]
            if boundary.geometry_type != "wall":
                continue
            normal = _newell(boundary.vertices)
            if normal[0]*want[0] + normal[1]*want[1] < 1-EPS:
                continue
            if resolution:
                vertices = [[p.x, p.y, p.z] for p in resolution.clamped_vertices]
            else:
                vertices = _legacy_cardinal_window_verts(window, Surface(bid, boundary.space_id, "Wall", boundary.vertices, "Outdoors"))
            if _contained(vertices, boundary) and exterior(vertices, boundary):
                candidates.append((boundary, vertices))
            elif wall_breakpoints.get(owner.floor_id):
                shape = _on_wall(vertices, boundary)
                if shape is not None:
                    overlap = shape.intersection(_on_wall(boundary.vertices, boundary))
                    origin = boundary.vertices[0][:2]
                    direction, _ = _frame(origin, boundary.vertices[1][:2])
                    for part in _parts(overlap):
                        clipped = _lift(list(part.exterior.coords)[:-1], origin, direction)
                        if exterior(clipped, boundary):
                            partials.append((boundary, clipped, vertices))
        if not candidates and partials:
            # Several thickness-uniform hosts may cover one logical window.
            # Accept exactly one complete coplanar cover, with no omitted area.
            covers = []
            for boundary, _, expected in partials:
                parts = [(b, v) for b, v, _ in partials if _on_wall(v, boundary) is not None]
                actual = unary_union([_on_wall(v, boundary) for _, v in parts])
                wanted = _on_wall(expected, boundary)
                if actual.symmetric_difference(wanted).area <= EPS ** 2:
                    identities = frozenset(b.id for b, _ in parts)
                    if not any(ids == identities for ids, _ in covers):
                        covers.append((identities, parts))
            if len(covers) == 1:
                candidates = covers[0][1]
                opening_segment_groups[wid] = []
        if len(candidates) != 1 and wid not in opening_segment_groups:
            reject(window, f"expected one complete exterior host; found {len(candidates)}")
            continue
        for index, (boundary, vertices) in enumerate(candidates):
            identity = wid if index == 0 else wid + f'::thickness-{index + 1}'
            while index and identity in used_aperture_ids:
                identity += '-2'
            used_aperture_ids.add(identity)
            openings.append(SourceOpening(id=identity, kind="window", host_boundary_id=boundary.id,
                            space_ids=[boundary.space_id], exterior=True, vertices=vertices,
                            source_refs=[f"correction:window/{quote(wid, safe='')}"]))
            bindings[identity] = [boundary.id]
            if wid in opening_segment_groups:
                opening_segment_groups[wid].append(identity)

    source_apertures = []
    for aperture in geom.openings:
        owner = spaces_by_id.get(aperture.space_id)
        line = LineString([aperture.p1, aperture.p2])
        breaks = wall_breakpoints.get(owner.floor_id, []) if owner else []
        cuts = sorted({line.project(Point(p)) for p in breaks
                       if line.distance(Point(p)) < EPS and EPS < line.project(Point(p)) < line.length-EPS})
        if not cuts:
            source_apertures.append(aperture)
            continue
        positions, ids = [0, *cuts, line.length], []
        for index, (lo, hi) in enumerate(zip(positions, positions[1:])):
            identity = aperture.id if index == 0 else aperture.id + f'::thickness-{index + 1}'
            while index and identity in used_aperture_ids:
                identity += '-2'
            used_aperture_ids.add(identity)
            ids.append(identity)
            source_apertures.append(aperture.model_copy(update={
                'id': identity, 'p1': tuple(line.interpolate(lo).coords[0]),
                'p2': tuple(line.interpolate(hi).coords[0]),
                'source_refs': [*aperture.source_refs, f'opening:{aperture.id}'],
            }))
        opening_segment_groups[aperture.id] = ids

    for aperture in source_apertures:
        if aperture.id in seen_ids:
            reject(aperture, "duplicate source opening identity")
            continue
        seen_ids.add(aperture.id)
        vertices = [[*aperture.p1, aperture.z[0]], [*aperture.p2, aperture.z[0]],
                    [*aperture.p2, aperture.z[1]], [*aperture.p1, aperture.z[1]]]
        owners = hosts(vertices, aperture.space_id)
        others = hosts(vertices, aperture.other_space_id) if aperture.other_space_id is not None else []
        if len(owners) != 1 or (aperture.other_space_id is not None and len(others) != 1):
            reject(aperture, "opening requires one complete source boundary on each declared side",
                   diagnostic=_opening_height_host_diagnostic(aperture, spaces_by_id))
            continue
        owner = owners[0]
        if others:
            shared = unary_union(contact_shapes(owner, aperture.other_space_id))
            valid = shared.buffer(EPS).covers(_on_wall(vertices, owner))
        else:
            valid = exterior(vertices, owner)
        if not valid:
            reject(aperture, "opening does not connect the declared spaces/outdoors over its full area")
            continue
        sids = [aperture.space_id] + ([aperture.other_space_id] if others else [])
        openings.append(SourceOpening(id=aperture.id, kind=aperture.kind, host_boundary_id=owner.id,
                        space_ids=sids, exterior=not others, vertices=vertices, connectivity=aperture.state,
                        source_refs=aperture.source_refs, assumptions=aperture.assumptions))
        bindings[aperture.id] = [owner.id] + [b.id for b in others]
        if aperture.kind != "window":
            connections.append({"opening_id": aperture.id, "kind": aperture.kind, "space_ids": sids,
                                "exterior": not others, "state": aperture.state})

    by_host = {}
    for opening in openings:
        for bid in bindings[opening.id]:
            by_host.setdefault(bid, []).append(opening)
    for bid, rows in by_host.items():
        for a, b in combinations(rows, 2):
            overlap = _on_wall(a.vertices, boundaries[bid]).intersection(_on_wall(b.vertices, boundaries[bid])).area
            if overlap > EPS ** 2:
                fail("source.opening_overlap", opening_ids=[a.id, b.id], boundary_id=bid, area_m2=overlap)
    if geom.unsupported:
        fail("source.unresolved_observations", count=len(geom.unsupported))

    payload = {
        "schema_version": "source_bim_v2",
        "source_geometry_sha256": _digest(geom.model_dump(mode="json")),
        "coordinate_system": {"units": "m", "frame": "corrected building coordinates", "up_axis": "Z",
                              "north_axis": getattr(geom, "north_axis", None).model_dump(mode="json") if getattr(geom, "north_axis", None) else None},
        "floors": [{"id":fid, "name":f.name, "z_floor":f.z_floor, "height":f.ceiling_height,
                    "footprint": list(getattr(f, "footprint").vertices) if getattr(f, "footprint", None)
                    else [[geom.footprint_x[0],geom.footprint_y[0]], [geom.footprint_x[1],geom.footprint_y[0]],
                          [geom.footprint_x[1],geom.footprint_y[1]], [geom.footprint_x[0],geom.footprint_y[1]]],
                    "footprint_basis": "per_floor_correction" if getattr(f,"footprint",None) else "legacy_common_footprint",
                    **({"footprint_holes": [[list(point) for point in ring] for ring in f.footprint.holes]}
                       if getattr(getattr(f, 'footprint', None), 'holes', []) else {}),
                    **({"footprint_boundary_path": [use.model_dump(mode='json') for use in f.footprint.boundary_path]}
                       if getattr(getattr(f, 'footprint', None), 'boundary_path', None) else {}),
                    **({"spanning_space_ids": list(f.spanning_space_ids)} if f.spanning_space_ids else {})}
                   for f,fid in zip(geom.floors,floor_ids)],
        "spaces": [s.model_dump(mode="json") for s in sorted(spaces, key=lambda s: s.id)],
        "boundaries": [boundaries[k].model_dump(mode="json") for k in sorted(boundaries)],
        "boundary_relations": relations,
        "openings": [o.model_dump(mode="json") for o in sorted(openings, key=lambda o: o.id)],
        "opening_hosts": bindings,
        "connections": connections,
        "unbuilt_openings": unbuilt,
        "assumptions": ["Correction cell rings represent physical source spaces with horizontal floor/ceiling boundaries.",
                        "Contacts describe adjacency, not traversability or EnergyPlus surface pairing.",
                        "Only explicit openings are represented; absent observations do not establish a solid wall.",
                        "Source boundary identity follows cell identity and ring order; topology edits need identity migration."],
        "corrections": geom.corrections, "conflicts": geom.conflicts, "unsupported": geom.unsupported,
        "validation": {"status": "severe" if findings else "pass", "findings": findings,
                       "numerical_tolerance_m": EPS,
                       "scope": "source geometry, contacts and explicit opening hosts",
                       "not_evaluated": ["drawing partition fidelity and completeness", "unobserved openings",
                                         "voids and false slabs", "thermal properties and solver acceptance"]},
    }
    if opening_segment_groups:
        payload['opening_segment_groups'] = opening_segment_groups
    if simple_polygon:
        payload['geometry_capability'] = {
            'profile': 'simple_polygon', 'input_schema': geom.schema_version,
            'scope': 'polygon prisms with explicit interior rings; declared circular arcs approximated by tolerance-bound chords; storey coverage and space identities are preserved',
            'not_supported': ['analytic curved surfaces', 'sloping or continuously height-varying spaces',
                              'one opening spanning multiple curve chords'],
        }
        if any(space.vertical_sections for space in spaces):
            payload['geometry_capability']['scope'] += '; explicit contiguous horizontal sections form one connected space with stepped enclosure'
            payload['geometry_capability']['vertical_sections'] = 'absolute_z_contiguous_sections_v1'
            payload['assumptions'][0] = 'Each correction cell represents one physical source space; optional horizontal sections define its stepped volume, while its polygon/holes are the full XY projection.'
        if geom.boundary_curves:
            from src.agent.geometry.source_curves import source_curve_origin
            payload['boundary_curves'] = {key: value.model_dump(mode='json') for key, value in geom.boundary_curves.items()}
            payload['curve_discretization'] = geom.curve_discretization
            cells = {cell.id: cell for floor in geom.floors for cell in floor.cells}
            for space in payload['spaces']:
                path = cells[space['id']].boundary_path
                if path:
                    space['boundary_path'] = [use.model_dump(mode='json') for use in path]
            for boundary in payload['boundaries']:
                if boundary['geometry_type'] != 'wall':
                    continue
                origins = source_curve_origin(boundary['vertices'], cells[boundary['space_id']].boundary_path,
                                              geom.curve_discretization)
                if origins:
                    boundary['curve_origins'] = origins
                    boundary['source_refs'] = list(dict.fromkeys([*boundary['source_refs'],
                        *[ref for origin in origins for ref in origin['source_refs']]]))
            explicit = {opening.id: opening.curve_segment for opening in geom.openings if opening.curve_segment}
            for opening in payload['openings']:
                if opening['id'] in explicit:
                    use = explicit[opening['id']]
                    record = geom.curve_discretization[use.curve_id]
                    opening['curve_origin'] = {**use.model_dump(mode='json'),
                        'curve_parameter': [(use.segment_index + v) / record['segment_count'] for v in use.span_fraction],
                        'maximum_chord_deviation_m': record['maximum_chord_deviation_m'],
                        'scope': 'one planar aperture on one declared curve chord; no bent or split opening'}
    from src.agent.geometry.source_naming import build_public_names
    from src.agent.roles import CATALOG, CATALOG_PATH
    payload["public_names"] = build_public_names(payload)
    payload["room_type_catalog"] = {"schema_version": CATALOG["schema_version"],
                                    "sha256": hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest(),
                                    "source": CATALOG["source"]}
    if geom.vertical_separations:
        payload["vertical_separations"] = [row.model_dump(mode="json") for row in geom.vertical_separations]
    from src.agent.geometry.source_vertical_continuity import bind_vertical_continuity_review
    bind_vertical_continuity_review(payload)
    payload["source_model_sha256"] = _digest(payload)
    if enclosure_declaration is not None:
        if (isinstance(enclosure_declaration, dict)
                and enclosure_declaration.get("schema_version") == "source_enclosure_intent_v1"):
            from src.agent.geometry.source_enclosure_intent import apply_source_enclosure_intent
            return apply_source_enclosure_intent(payload, enclosure_declaration)
        from src.agent.geometry.source_enclosure import apply_source_enclosure
        return apply_source_enclosure(payload, enclosure_declaration)
    return payload


def source_view_geometry(source: dict) -> dict:
    """Project full source boundaries into the existing viewer's display schema.

    Display names map straight back to source IDs. No thermal boundary condition
    or reciprocal EP pair is synthesized. Coincident display overlap is handled
    by the renderer, never by changing the authoritative source.
    """
    expected = _digest({k:v for k,v in source.items() if k != "source_model_sha256"})
    if source.get("schema_version") not in {"source_bim_v2", "source_bim_v3"} or source.get("source_model_sha256") != expected:
        raise ValueError("invalid source BIM schema or digest")
    boundaries = {b["id"]: b for b in source["boundaries"]}
    surfaces = [{"name": b["id"], "zone": b["space_id"],
                 "type": {"wall":"Wall", "floor":"Floor", "ceiling":"Ceiling"}[b["geometry_type"]],
                 "verts": b["vertices"]} for b in source["boundaries"]]
    windows, apertures, window_map, aperture_map = [], [], {}, {}
    for opening in source["openings"]:
        if opening["kind"] == "window":
            name = f"window/{quote(opening['id'], safe='')}"
            windows.append({"name": name, "parent": opening["host_boundary_id"], "verts": opening["vertices"]})
            window_map[name] = opening["id"]
            continue
        hosts = source["opening_hosts"][opening["id"]]
        names = [f"opening/{quote(opening['id'], safe='')}/{quote(bid, safe='')}" for bid in hosts]
        for i, bid in enumerate(hosts):
            owner = boundaries[bid]["space_id"]
            apertures.append({"name": names[i], "parent": bid,
                              "verts": _orient(opening["vertices"], _newell(boundaries[bid]["vertices"])),
                              "source_opening_id": opening["id"], "kind": opening["kind"], "space_id": owner,
                              "other_space_id": next((s for s in opening["space_ids"] if s != owner), None),
                              "state": opening["connectivity"], "partner": names[1-i] if len(names)==2 else ""})
            aperture_map[names[i]] = opening["id"]
    display = {"zones": [s["id"] for s in source["spaces"]], "surfaces": surfaces, "windows": windows,
               "openings": apertures, "roles": {s["id"]:s["role"] for s in source["spaces"]}}
    display["display_surface_parts"] = _display_parts(source)
    if source["schema_version"] == "source_bim_v3":
        display["enclosure_regions"] = [
            {"boundary_id": b["id"], "space_id": b["space_id"],
             "condition": r["condition"], "verts": r["vertices"],
             **({'holes': r['holes']} if r.get('holes') else {}),
             "source_refs": r.get("source_refs", []), "assumptions": r.get("assumptions", []),
             "evidence_kind": r.get("evidence_kind")}
            for b in source["boundaries"] for r in b.get("enclosure_regions", [])]
    display["source_model"] = {**source, "derived": {
        "zones": {s["id"]:s["id"] for s in source["spaces"]},
        "surfaces": {b["id"]:b["id"] for b in source["boundaries"]},
        "windows": window_map, "openings": aperture_map}}
    return display


def _display_parts(source):
    """Remove holes and hide only overlapping display patches at rest.

    These are display meshes, with the entire parent/source boundary preserved.
    At exploded views both sides reappear. Handles partial/many-to-one contacts
    without inventing a reciprocal surface or hiding exposed parts of a wall.
    """
    result = {}
    for boundary in source["boundaries"]:
        bid, vertices = boundary["id"], boundary["vertices"]
        # A materialized v2 source can already explicitly declare a wholly
        # virtual boundary. Keep its logical surface and opening identities,
        # but never manufacture opaque physical fill merely because it lacks
        # the more expressive v3 enclosure_regions sidecar.
        if source["schema_version"] == "source_bim_v2" and boundary.get("kind") == "virtual":
            result[bid] = []
            continue
        if boundary["geometry_type"] == "wall":
            origin = vertices[0][:2]
            direction, _ = _frame(origin, vertices[1][:2])
            project = lambda ring: _project(ring, origin, direction)
            lift = lambda ring: _lift(list(ring), origin, direction)
        else:
            z = vertices[0][2]
            project = lambda ring: Polygon([v[:2] for v in ring])
            lift = lambda ring: [[x,y,z] for x,y in ring]
        parent = project(vertices)
        if boundary.get('holes'):
            parent = parent.difference(unary_union([project(h) for h in boundary['holes']]))
        holes = [project(o["vertices"]) for o in source["openings"]
                 if bid in source["opening_hosts"][o["id"]]]
        wall = parent.difference(unary_union(holes))
        regions = boundary.get("enclosure_regions", [])
        open_area = unary_union([project(r["vertices"]) for r in regions if r["condition"] == "open"])
        unknown_area = unary_union([project(r["vertices"]) for r in regions if r["condition"] == "unknown"])
        physical = wall.difference(open_area.union(unknown_area)) if regions else wall
        unknown = wall.intersection(unknown_area)
        hidden = []
        for relation in source["boundary_relations"]:
            if bid not in relation["boundary_ids"] or bid == min(relation["boundary_ids"]):
                continue
            for region in relation["regions"]:
                patch = project(region["vertices"])
                if region["holes"]:
                    patch = patch.difference(unary_union([project(r) for r in region["holes"]]))
                hidden.append(patch)
        mask = unary_union(hidden)
        rows = []
        for area, condition in ((physical, "physical"), (unknown, "unknown")):
            for shape, duplicate in ((area.difference(mask), False), (area.intersection(mask), True)):
                for part in _parts(shape):
                    row = {"verts": lift(list(part.exterior.coords)[:-1]),
                           "holes": [lift(list(r.coords)[:-1]) for r in part.interiors],
                           "duplicate_at_rest": duplicate}
                    if source["schema_version"] == "source_bim_v3":
                        row["enclosure_condition"] = condition
                    rows.append(row)
        result[bid] = rows
    return result
