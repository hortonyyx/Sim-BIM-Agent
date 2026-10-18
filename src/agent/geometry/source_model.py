"""Source BIM projection and explicit links to deterministic display/EP faces.

This is a sidecar to the existing geometry serialization, not another geometry
kernel. Source rooms come from correction; cutting a face never adds a room or
a physical partition here. This projection checks realization, not whether the
correction faithfully read the drawing (that requires independent evidence).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from urllib.parse import quote

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from src.agent.correction.schema import (
    CorrectedGeometry, SourceBoundary, SourceOpening, SourceSpace,
)
from src.agent.geometry.modelling import (
    BuildingGeometry, _cell_polygon, _FACADE_NORMAL,
    _legacy_cardinal_window_verts, _newell,
)


def _ring(polygon: Polygon) -> list[list[float]]:
    # Remove only exactly collinear display vertices; do not snap source geometry.
    points = list(orient(polygon.simplify(0), sign=1).exterior.coords)[:-1]
    start = min(range(len(points)), key=points.__getitem__)
    return [list(p) for p in points[start:] + points[:start]]


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def source_primitives(geom: CorrectedGeometry, *, polygon_builder=_cell_polygon, wall_breakpoints=None):
    """Build source identities and full room boundaries, without derived faces."""
    spaces: list[SourceSpace] = []
    boundaries: dict[str, SourceBoundary] = {}
    polygons: dict[str, Polygon] = {}
    by_space: dict[str, list[str]] = {}
    for floor in geom.floors:
        floor_id = str(getattr(floor, "id", None) or floor.name)
        for cell in floor.cells:
            if cell.id in polygons:
                raise ValueError(f"duplicate source space id: {cell.id}")
            poly = polygon_builder(cell)
            polygons[cell.id] = poly
            ring = _ring(poly)
            breaks = (wall_breakpoints or {}).get(floor_id, [])
            if breaks:
                split_ring = []
                for a, b in zip(ring, ring[1:] + ring[:1]):
                    line = LineString([a, b])
                    interior = [p for p in breaks if line.distance(Point(p)) < 1e-7
                                and 1e-7 < line.project(Point(p)) < line.length - 1e-7]
                    split_ring.extend([a, *[list(p) for p in sorted(
                        interior, key=lambda p: line.project(Point(p)))]])
                ring = split_ring
            z0 = float(floor.z_floor)
            z1 = z0 + float(floor.ceiling_height)
            ref = f"correction:floor/{quote(floor_id, safe='')}/cell/{quote(cell.id, safe='')}"
            space = SourceSpace(
                id=cell.id, floor_id=floor_id, polygon=ring, z_floor=z0,
                height=z1-z0, role=cell.role,
                source_refs=getattr(cell, "source_refs", []),
                assumptions=getattr(cell, "assumptions", []),
                role_evidence=getattr(cell, "role_evidence", None),
            )
            # The correction pointer is traceability, not a replacement for the
            # input's actual drawing observations and explicit hypotheses.
            space.source_refs = list(dict.fromkeys([ref, *space.source_refs]))
            spaces.append(space)
            rows = []
            for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
                rows.append((f"wall/{index}", "wall", [
                    [*a, z0], [*b, z0], [*b, z1], [*a, z1],
                ]))
            rows.extend([
                ("floor", "floor", [[*p, z0] for p in ring]),
                ("ceiling", "ceiling", [[*p, z1] for p in ring]),
            ])
            by_space[cell.id] = []
            for suffix, kind, vertices in rows:
                bid = f"space/{quote(cell.id, safe='')}/{suffix}"
                boundaries[bid] = SourceBoundary(
                    id=bid, space_id=cell.id, kind="physical", geometry_type=kind,
                    vertices=vertices, source_refs=[ref],
                )
                by_space[cell.id].append(bid)

    return spaces, boundaries, polygons, by_space


def materialize_source_model(geom: CorrectedGeometry, bg: BuildingGeometry) -> dict:
    """Project correction objects and verify every derived face has a source.

    IDs use cell identity and canonical boundary order, independent of Z names
    and face subdivision. Topology edits that add/remove source edges need an
    explicit identity migration; dimensional edits preserve boundary order.
    The current correction contract only produces physical, single-ring rooms.
    Explicit wall apertures are checked; unrecorded drawing openings and voids
    remain unevaluated.
    """
    from src.agent.geometry.specs import building_geometry_dict

    spaces, boundaries, polygons, by_space = source_primitives(geom)
    findings: list[dict] = []

    def fail(code: str, **evidence) -> None:
        findings.append({"code": code, "severity": "severe", **evidence})

    zone_map = {z.zone: z.cell_id for z in bg.zone_volumes}
    if len(zone_map) != len(bg.zone_volumes) or set(bg.zones) != set(zone_map):
        fail("source.zone_identity", zones=bg.zones)
    for sid in polygons:
        if sid not in zone_map.values():
            fail("source.space_unrealized", space_id=sid)
    for zone, sid in zone_map.items():
        if sid not in polygons:
            fail("source.unknown_space", zone=zone, space_id=sid)

    surface_map: dict[str, str] = {}
    coverage: dict[str, list[Polygon]] = {bid: [] for bid in boundaries}
    # split_pairing subtracts shared lines with a 1e-6 m buffer and rounds public
    # vertices to six decimals. Accommodate that numerical seam, not drawing noise.
    eps = 2e-6
    for face in bg.surfaces:
        sid = zone_map.get(face.zone)
        candidates = []
        for bid in by_space.get(sid, []):
            boundary = boundaries[bid]
            if face.stype == "Wall" and boundary.geometry_type == "wall":
                a, b = boundary.vertices[:2]
                line = LineString([a[:2], b[:2]])
                if not all(line.distance(Point(v[:2])) <= eps
                           for v in face.verts):
                    continue
                z0, z1 = boundary.vertices[0][2], boundary.vertices[2][2]
                projected = Polygon([(line.project(Point(v[:2])), v[2])
                                     for v in face.verts])
                target = Polygon([(0, z0), (line.length, z0), (line.length, z1), (0, z1)])
            elif ((face.stype == "Floor" and boundary.geometry_type == "floor") or
                  (face.stype in {"Ceiling", "Roof"} and boundary.geometry_type == "ceiling")):
                if not all(abs(v[2] - boundary.vertices[0][2]) <= eps for v in face.verts):
                    continue
                projected = Polygon([v[:2] for v in face.verts])
                target = polygons[sid]
            else:
                continue
            if projected.is_valid and projected.area > eps and target.buffer(eps).covers(projected):
                candidates.append((bid, projected))
        if len(candidates) != 1:
            fail("source.face_without_unique_boundary", surface=face.name,
                 space_id=sid, candidate_boundary_ids=[v[0] for v in candidates])
            continue
        bid, projected = candidates[0]
        if face.name in surface_map:
            fail("source.duplicate_surface", surface=face.name)
        surface_map[face.name] = bid
        coverage[bid].append(projected)

    for bid, parts in coverage.items():
        boundary = boundaries[bid]
        if boundary.geometry_type == "wall":
            a, b, c = boundary.vertices[:3]
            width = LineString([a[:2], b[:2]]).length
            target = Polygon([(0, a[2]), (width, a[2]), (width, c[2]), (0, c[2])])
        else:
            target = polygons[boundary.space_id]
        union = unary_union(parts)
        missing = target.difference(union.buffer(eps)).area
        duplicate = sum(p.area for p in parts) - union.area
        if missing > eps or duplicate > eps:
            fail("source.boundary_coverage", boundary_id=bid,
                 missing_area_m2=missing, duplicate_area_m2=duplicate)

    faces = {s.name: s for s in bg.surfaces}
    for face in bg.surfaces:
        bid = surface_map.get(face.name)
        if not bid or face.obc != "Surface":
            continue
        partner = faces.get(face.obc_obj)
        other = surface_map.get(face.obc_obj)
        if partner is None or partner.obc_obj != face.name or other is None:
            fail("source.unresolved_pair", surface=face.name, partner=face.obc_obj)
            continue
        if boundaries[bid].space_id == boundaries[other].space_id:
            fail("source.physical_wall_within_space", surface=face.name, boundary_id=bid)
        boundaries[bid].adjacent_space_ids.append(boundaries[other].space_id)
        boundaries[bid].counterpart_ids.append(other)

    source_windows = {str(w.id): w for w in geom.windows}
    openings: list[SourceOpening] = []
    window_map: dict[str, str] = {}
    seen_window_names: set[str] = set()
    seen_window_sources: set[str] = set()
    for window in bg.windows:
        wid = window.source_window_id
        if window.name in seen_window_names or wid in seen_window_sources:
            fail("source.duplicate_opening", window=window.name, source_id=wid)
        seen_window_names.add(window.name)
        seen_window_sources.add(wid)
        source = source_windows.get(wid)
        bid = surface_map.get(window.parent)
        if source is None or bid is None:
            fail("source.opening_without_host", window=window.name, source_id=wid)
            continue
        if source.room != boundaries[bid].space_id:
            fail("source.opening_wrong_space", window=window.name, source_id=wid)
        parent = faces[window.parent]
        normal = _newell(parent.verts)
        want = _FACADE_NORMAL[source.facade.lower()]
        if (boundaries[bid].geometry_type != "wall" or parent.obc != "Outdoors"
                or normal[0] * want[0] + normal[1] * want[1] < 0.9):
            fail("source.opening_wrong_host", window=window.name, boundary_id=bid)
        expected_vertices = sorted(_legacy_cardinal_window_verts(source, parent))
        actual_vertices = sorted(window.verts)
        if (len(actual_vertices) != len(expected_vertices) or
                any(abs(a - b) > eps for actual, expected in zip(actual_vertices, expected_vertices)
                    for a, b in zip(actual, expected))):
            fail("source.opening_geometry_changed", window=window.name, source_id=wid,
                 expected_vertices=expected_vertices, actual_vertices=actual_vertices)
        if str(geom.schema_version) == "3" and source.facade_segment_id != window.facade_segment_id:
            fail("source.opening_segment_changed", window=window.name, source_id=wid)
        openings.append(SourceOpening(
            id=wid, kind="window", host_boundary_id=bid,
            space_ids=[boundaries[bid].space_id], exterior=faces[window.parent].obc == "Outdoors",
            vertices=[list(v) for v in window.verts],
            source_refs=[f"correction:window/{quote(wid, safe='')}"],
        ))
        window_map[window.name] = wid
    if len(source_windows) != len(geom.windows) or sorted(window_map.values()) != sorted(source_windows):
        fail("source.opening_completeness", expected_ids=sorted(source_windows),
             actual_ids=sorted(window_map.values()))

    from src.agent.geometry.openings import attach_openings, OpeningBindingError

    opening_map: dict[str, str] = {}
    try:
        expected_apertures = attach_openings(geom, bg)
    except OpeningBindingError as exc:
        expected_apertures = []
        fail("source.opening_binding", message=str(exc))
    expected_by_name = {o.name: o for o in expected_apertures}
    for aperture in bg.openings:
        if aperture.name in opening_map:
            fail("source.duplicate_opening", opening=aperture.name, source_id=aperture.source_opening_id)
        expected = expected_by_name.get(aperture.name)
        if expected is None or asdict(aperture) != asdict(expected):
            fail("source.aperture_realization_changed", opening=aperture.name,
                 source_id=aperture.source_opening_id)
        opening_map[aperture.name] = aperture.source_opening_id
    if set(opening_map) != set(expected_by_name):
        fail("source.aperture_completeness", expected_names=sorted(expected_by_name),
             actual_names=sorted(opening_map))
    connections = []
    for source in geom.openings:
        owned = [o for o in expected_apertures if o.source_opening_id == source.id and o.space_id == source.space_id]
        host_ids = {surface_map.get(o.parent) for o in owned}
        if len(host_ids) != 1 or None in host_ids:
            fail("source.aperture_without_unique_source_boundary", source_id=source.id)
            continue
        bid = next(iter(host_ids))
        linked_spaces = [source.space_id] + ([source.other_space_id] if source.other_space_id is not None else [])
        openings.append(SourceOpening(
            id=source.id, kind=source.kind, host_boundary_id=bid,
            space_ids=linked_spaces, exterior=source.other_space_id is None,
            vertices=[[*source.p1, source.z[0]], [*source.p2, source.z[0]],
                      [*source.p2, source.z[1]], [*source.p1, source.z[1]]],
            connectivity=source.state, source_refs=list(source.source_refs), assumptions=list(source.assumptions),
        ))
        connections.append({
            "opening_id": source.id, "kind": source.kind, "space_ids": linked_spaces,
            "exterior": source.other_space_id is None, "state": source.state,
        })

    for boundary in boundaries.values():
        boundary.adjacent_space_ids = sorted(set(boundary.adjacent_space_ids))
        boundary.counterpart_ids = sorted(set(boundary.counterpart_ids))
    payload = {
        "schema_version": "source_bim_v1",
        "source_geometry_sha256": _digest(geom.model_dump(mode="json")),
        "derived_geometry_sha256": _digest(building_geometry_dict(bg)),
        "spaces": [s.model_dump() for s in sorted(spaces, key=lambda s: s.id)],
        "boundaries": [boundaries[k].model_dump() for k in sorted(boundaries)],
        "openings": [o.model_dump() for o in sorted(openings, key=lambda o: o.id)],
        "derived": {"zones": zone_map, "surfaces": surface_map, "windows": window_map,
                    "openings": opening_map},
        "connections": connections,
        "assumptions": [
            "Correction cell rings represent physical source room boundaries.",
            "Boundary IDs survive dimensions/face subdivision; topology edits require identity migration.",
            "Adjacency does not imply traversability; window connectivity is unknown.",
            "Only explicitly supplied doors/passages are represented; absent records do not prove no opening exists.",
        ],
        "corrections": geom.corrections,
        "conflicts": geom.conflicts,
        "unsupported": geom.unsupported,
        "validation": {
            "status": "severe" if findings else "pass", "findings": findings,
            "realization_tolerance_m": eps,
            "scope": "source-to-derived realization only",
            "not_evaluated": ["drawing partition fidelity", "drawing opening completeness/operating state",
                              "voids and false slabs", "downstream solver acceptance"],
        },
    }
    payload["source_model_sha256"] = _digest(payload)
    return payload
