"""Public BIM names separate from source identities; June 23 naming contract."""
from __future__ import annotations

from collections import Counter, defaultdict
import re

from shapely.geometry import LineString, Point, Polygon

from src.agent.geometry.modelling import _canonical_ring, _geometry_fingerprint, _zone_centroid_key, _zone_quadrant
from src.agent.roles import ROOM_TYPES, normalize


# This is naming comparison precision, not a geometry snapping tolerance.
ROW_TOLERANCE_M = 1e-6
SCHEME_VERSION = "bim_names_v3"


def _ordered_spaces(spaces, polys, floor_order):
    """Group north-to-south rows before sorting west-to-east within each row.

    Anchor each row to its northernmost centroid to avoid transitive chains
    spanning more than the tolerance. Sorting first makes input order irrelevant
    and avoids rounded-bin boundaries splitting numerical equals (run99).
    """
    by_floor = defaultdict(list)
    for space in spaces:
        by_floor[space['floor_id']].append(space)
    ordered = []
    for fid in sorted(by_floor, key=floor_order.__getitem__):
        north_to_south = sorted(by_floor[fid], key=lambda s: (
            -polys[s['id']].centroid.y, polys[s['id']].centroid.x,
            _geometry_fingerprint(polys[s['id']]), s['id']))
        rows = []
        for space in north_to_south:
            y = polys[space['id']].centroid.y
            if not rows or rows[-1][0] - y > ROW_TOLERANCE_M:
                rows.append((y, []))
            rows[-1][1].append(space)
        for _, row in rows:
            ordered.extend(sorted(row, key=lambda s: (
                round(polys[s['id']].centroid.x, 6),
                _geometry_fingerprint(polys[s['id']]), s['id'])))
    return ordered


def geometry_key(vertices):
    """Winding/start-independent ordering for fragments and edge names."""
    return tuple(sorted(tuple(round(float(c), 6) for c in v) for v in vertices))


def build_public_names(source: dict) -> dict:
    floors = sorted(source["floors"], key=lambda f: (f["z_floor"], f["id"]))
    # F# is an ordinal, including annex/vertical groups; source labels stay
    # separate and do not imply geographic/architectural storey identity.
    floor_names = {f["id"]: f"F{i}" for i, f in enumerate(floors, 1)}
    floor_order = {f["id"]: i for i, f in enumerate(floors)}
    polys = {s["id"]: Polygon(s["polygon"], s.get('holes', [])) for s in source["spaces"]}
    points = [p for f in floors for p in f["footprint"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    ordered = _ordered_spaces(source["spaces"], polys, floor_order)
    width = max(2, len(str(len(ordered))))
    handles, spaces, boundaries, openings, sides = {}, {}, {}, {}, {}
    room_groups = {}
    for space in ordered:
        role = ROOM_TYPES.get(normalize(space.get("role")), ROOM_TYPES["unknown"])
        quadrant = _zone_quadrant(polys[space["id"]], [min(xs), max(xs)], [min(ys), max(ys)])
        room_groups[space["id"]] = (space["floor_id"], role["name_token"], quadrant)
    group_sizes = Counter(room_groups.values())
    group_ordinals = Counter()
    for i, space in enumerate(ordered, 1):
        sid = space["id"]
        handle = handles[sid] = f"Z{i:0{width}d}"
        group = room_groups[sid]
        fid, token, quadrant = group
        group_ordinals[group] += 1
        suffix = str(group_ordinals[group]) if group_sizes[group] > 1 else ""
        spaces[sid] = f"{handle}_{floor_names[fid]}_{token}_{quadrant}{suffix}"

    walls = {}
    by_space = defaultdict(list)
    for b in source["boundaries"]:
        by_space[b["space_id"]].append(b)
    for sid, rows in by_space.items():
        ring = list(_canonical_ring(polys[sid]))
        path = LineString(ring + [ring[0]])
        def wall_key(b):
            xy = list({tuple(v[:2]) for v in b["vertices"]})
            mid = Point(sum(p[0] for p in xy)/len(xy), sum(p[1] for p in xy)/len(xy))
            return round(path.project(mid), 6), geometry_key(b["vertices"]), b["id"]
        for i, b in enumerate(sorted((b for b in rows if b["geometry_type"] == "wall"), key=wall_key), 1):
            bid = b["id"]
            boundaries[bid] = f"{handles[sid]}_W{i}"
            # Find the canonical ring segment containing this wall's midpoint.
            xy = list({tuple(v[:2]) for v in b["vertices"]})
            mid = Point(sum(p[0] for p in xy)/len(xy), sum(p[1] for p in xy)/len(xy))
            walls[bid] = min(zip(ring, ring[1:]+ring[:1]), key=lambda ab: LineString(ab).distance(mid))
        for kind, token in (("floor", "Floor"), ("ceiling", "Ceiling"), ("roof", "Roof")):
            rows_kind = sorted((b for b in rows if b["geometry_type"] == kind), key=lambda b: (
                _zone_centroid_key(Polygon([v[:2] for v in b["vertices"]])), geometry_key(b["vertices"]), b["id"]))
            for i, b in enumerate(rows_kind, 1):
                boundaries[b["id"]] = f"{handles[sid]}_{token}" + (str(i) if len(rows_kind) > 1 else "")

    groups = defaultdict(list)
    for opening in source["openings"]:
        for bid in source["opening_hosts"][opening["id"]]:
            groups[(bid, opening["kind"])].append(opening)
    for (bid, kind), rows in groups.items():
        a, b = walls[bid]
        dx, dy = b[0]-a[0], b[1]-a[1]
        def opening_key(o):
            vs = o["vertices"]
            along = min((v[0]-a[0])*dx + (v[1]-a[1])*dy for v in vs)
            return round(along, 6), min(v[2] for v in vs), geometry_key(vs), o["id"]
        token = {"window": "Win", "door": "Door", "open": "Opening"}[kind]
        for i, o in enumerate(sorted(rows, key=opening_key), 1):
            sides.setdefault(o["id"], {})[bid] = f"{boundaries[bid]}_{token}{i}"
    for oid, hosts in sides.items():
        # One shared opening with two room-side aliases, not two doors.
        openings[oid] = min(hosts.values())
    return {"scheme_version": SCHEME_VERSION, "row_tolerance_m": ROW_TOLERANCE_M,
            "direction_frame": "model XY: +X=E, +Y=N; not a geographic north assertion",
            "source_floor_names": {f['id']: f.get('name') or f['id'] for f in floors},
            "floors": floor_names, "spaces": spaces, "boundaries": boundaries,
            "openings": openings, "opening_sides": sides}


def public_names_for_display(source: dict) -> dict:
    """Refresh old public maps for display only; never mutate a saved source."""
    if (source.get("floors") and all(f.get("footprint") for f in source["floors"])
            and all(s.get("polygon") for s in source.get("spaces", []))):
        return build_public_names(source)
    return source.get("public_names") or {}


def public_reference_map(source: dict, names: dict) -> dict:
    """Names for prose references, including a saved older public spelling."""
    references = {}
    old = source.get("public_names") or {}
    for kind in ("floors", "spaces", "boundaries", "openings"):
        for identity, name in names.get(kind, {}).items():
            previous = old.get(kind, {}).get(identity)
            if previous and previous != name:
                references[previous] = name
            references[identity] = name
    return references


def public_reference_text(value: object, references: dict) -> str:
    """Replace complete object references in prose, leaving paths/URLs intact."""
    text = str(value)
    keys = sorted((key for key, name in references.items() if key != name), key=lambda key: (-len(key), key))
    if not keys:
        return text
    token_chars = r"A-Za-z0-9_./:%\\-"
    pattern = rf"(?<![{token_chars}])(?:{'|'.join(re.escape(key) for key in keys)})(?![A-Za-z0-9_/%\\-]|[.:][A-Za-z0-9_])"
    return re.sub(pattern, lambda match: references[match[0]], text)


def viewer_names(data: dict, parts: dict) -> dict:
    """Name disposable clickable fragments without rewriting IDs or geometry."""
    source = data.get("source_model") or {}
    names = public_names_for_display(source)
    derived = source.get("derived", {})
    surfaces = {s["name"]: names.get("boundaries", {}).get(derived.get("surfaces", {}).get(s["name"], s["name"]), s["name"])
                for s in data.get("surfaces", [])}
    objects = dict(surfaces)
    for kind in ("windows", "openings"):
        for o in data.get(kind, []):
            oid = derived.get(kind, {}).get(o["name"], o.get("source_opening_id", o["name"]))
            bid = derived.get("surfaces", {}).get(o["parent"], o["parent"])
            objects[o["name"]] = names.get("opening_sides", {}).get(oid, {}).get(bid, names.get("openings", {}).get(oid, o["name"]))
    fragment_names = {}
    for sid, rows in parts.items():
        ranks = {i: rank for rank, i in enumerate(sorted(range(len(rows)), key=lambda i: (
            geometry_key(rows[i]["verts"]), tuple(sorted(geometry_key(h) for h in rows[i].get("holes", []))))), 1)}
        fragment_names[sid] = [surfaces.get(sid, sid) + (f"_Part{ranks[i]}" if len(rows)>1 else "") for i in range(len(rows))]
    regions = data.get("enclosure_regions", [])
    region_names = [""] * len(regions)
    grouped = defaultdict(list)
    for i, r in enumerate(regions):
        grouped[(r["boundary_id"], r["condition"])].append(i)
    for (bid, condition), indices in grouped.items():
        for rank, i in enumerate(sorted(indices, key=lambda i: geometry_key(regions[i]["verts"])), 1):
            region_names[i] = names.get("boundaries", {}).get(bid, bid) + f"_{condition.title()}{rank}"
    edges = {}
    for obj in data.get("surfaces", []) + data.get("windows", []) + data.get("openings", []):
        vs = obj["verts"]
        order = sorted(range(len(vs)), key=lambda i: geometry_key([vs[i], vs[(i+1)%len(vs)]]))
        rank = {i:n for n,i in enumerate(order, 1)}
        edges[obj["name"]] = [f"{objects[obj['name']]}_Edge{rank[i]}" for i in range(len(vs))]
    floors = sorted(source.get("floors", []), key=lambda f: (f["z_floor"], f["id"]))
    return {"floors": [{"id": f["id"], "name": f"F{i}",
                       "source_name": f.get("name") or f["id"], "z_floor": f["z_floor"]}
                      for i, f in enumerate(floors, 1)],
            "spaces": names.get("spaces", {}), "objects": objects, "parts": fragment_names,
            "regions": region_names, "edges": edges,
            "references": public_reference_map(source, names)}
