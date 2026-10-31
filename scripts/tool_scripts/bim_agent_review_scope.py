"""Advisory review scope from material changes to two saved source snapshots.

This compares saved state, not observations or quality. Numerical tolerance only
suppresses coordinate recomputation noise; it is not a modelling tolerance.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math


NUMERIC_TOLERANCE_M = 1e-8
_ACTIONS = {
    "plan_relations": "Recheck affected plan positions and same/separate-space relationships; exterior overlays do not establish them.",
    "identity_and_use": "Recheck affected space identities, room uses and floor membership against their evidence.",
    "height_alignment": "Recheck affected heights, storey alignment and opening elevations.",
    "opening_hosts": "Recheck affected door/window placement, hosts and connected spaces.",
    "enclosure_relations": "Recheck affected physical/open/unknown enclosure regions and their spatial connections.",
    "mesh_registration": "Recheck the changed model-to-mesh frame; its effect spans the saved model.",
}
_SCOPE = ("Advisory saved-state delta only, not completed checks or quality acceptance. "
          "Covers geometry, identity/use, enclosure, membership, opening hosts/connections and mesh frame; "
          "excludes provenance-only changes. No prescribed tool sequence.")


def _same(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(left, right, rel_tol=0.0, abs_tol=NUMERIC_TOLERANCE_M)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _fields(row, names):
    return {name: row.get(name) for name in names}


def _space_xy(row):
    return [row.get("polygon"), row.get("holes", []),
            [[part.get("polygon"), part.get("holes", [])] for part in row.get("vertical_sections", [])]]


def _space_z(row):
    return [row.get("z_floor"), row.get("height"),
            [part.get("z") for part in row.get("vertical_sections", [])]]


def _vertices(row, axes):
    return [[[point[index] for index in axes] for point in ring]
            for ring in [row.get("vertices", []), *row.get("holes", [])]]


def _enclosure(row):
    return [row.get("enclosure", "physical"),
            [_fields(part, ("condition", "vertices", "holes")) for part in row.get("enclosure_regions", [])]]


def _mapping(source, name):
    return {str(row["id"]): row for row in source.get(name, [])}


def _host_ids(source, row):
    hosts = source.get("opening_hosts", {}).get(row["id"])
    return sorted(hosts if isinstance(hosts, list) else [row.get("host_boundary_id") or ""])


def _floors_for_space(source, identity, region=None):
    space = _mapping(source, "spaces").get(identity)
    if space is None:
        return set()
    floors = [row for row in source.get("floors", []) if identity in row.get("spanning_space_ids", [])]
    if not floors:
        return {str(space["floor_id"])} if space.get("floor_id") is not None else set()
    # Apertures require positive overlap; horizontal caps touch a storey at one Z.
    zs = [point[2] for point in (region or {}).get("vertices", [])]
    if zs:
        low, high = min(zs), max(zs)
        horizontal_cap = (region.get("geometry_type") in ("floor", "ceiling")
                          and high - low <= NUMERIC_TOLERANCE_M)
        floors = [row for row in floors if not all(isinstance(row.get(k), (int, float))
                                                  for k in ("z_floor", "height"))
                  or (row["z_floor"] - NUMERIC_TOLERANCE_M <= low
                      <= row["z_floor"] + row["height"] + NUMERIC_TOLERANCE_M if horizontal_cap
                      else min(high, row["z_floor"] + row["height"]) - max(low, row["z_floor"])
                      > NUMERIC_TOLERANCE_M)]
    return {str(row["id"]) for row in floors}


def _overlaps_height(left, right):
    a = [point[2] for point in left.get("vertices", [])]
    b = [point[2] for point in right.get("vertices", [])]
    return not a or not b or min(max(a), max(b)) - max(min(a), min(b)) > NUMERIC_TOLERANCE_M


def review_scope(before, after, *, previous_candidate=None, unavailable_reason=None):
    """Return complete bounded-by-entities advice; compact presentation is separate."""
    result = {"status": "unavailable" if unavailable_reason else "no_baseline",
              "previous_candidate": previous_candidate,
              "numeric_tolerance": {"metres": NUMERIC_TOLERANCE_M, "yaw_degrees": NUMERIC_TOLERANCE_M}, "scope": _SCOPE,
              "actions": [], "affected": {}, "changes": []}
    if unavailable_reason or before is None:
        result["reason"] = unavailable_reason or "Initial source; no verified previous snapshot to localize changes."
        return result

    affected = {name: set() for name in ("floors", "spaces", "boundaries", "openings", "windows")}
    topics = set()
    changed_space_geometry, changed_boundaries = set(), set()
    local_boundary_relations = {}

    def mark(collection, identity, old, new, fields, action_topics):
        if not fields:
            return
        result["changes"].append({"collection": collection, "id": identity,
                                  "change": "added" if old is None else "removed" if new is None else "changed",
                                  "fields": fields})
        topics.update(action_topics)
        affected[collection].add(identity)
        if collection == "boundaries" and identity in local_boundary_relations:
            # Counterpart IDs can be regenerated at one landing of a continuous
            # core. Only the changed counterpart slices invalidate that relation.
            for source, owner, counterpart in local_boundary_relations[identity]:
                for space_id in (owner.get("space_id"), counterpart.get("space_id")):
                    if space_id is not None:
                        affected["spaces"].add(space_id)
                        affected["floors"].update(_floors_for_space(source, space_id, counterpart))
            return
        for source, row in ((before, old), (after, new)):
            if row is None:
                continue
            if collection == "spaces":
                affected["floors"].update(_floors_for_space(source, identity))
            elif collection in ("openings", "windows", "boundaries"):
                space_ids = row.get("space_ids", []) if collection != "boundaries" else [
                    row.get("space_id"), *row.get("adjacent_space_ids", []),
                    *[_mapping(source, "boundaries").get(other_id, {}).get("space_id")
                      for other_id in row.get("counterpart_ids", [])]]
                for space_id in space_ids:
                    if space_id is not None:
                        affected["spaces"].add(space_id)
                        affected["floors"].update(_floors_for_space(source, space_id, row))

    for collection in ("spaces", "boundaries", "openings", "floors"):
        old_rows, new_rows = _mapping(before, collection), _mapping(after, collection)
        for identity in sorted(old_rows.keys() | new_rows.keys()):
            old, new = old_rows.get(identity), new_rows.get(identity)
            left, right = old or {}, new or {}
            fields, actions = [], set()
            identity_change = old is None or new is None
            if identity_change:
                fields.append("identity")
            if collection == "spaces":
                xy = identity_change or not _same(_space_xy(left), _space_xy(right))
                z = identity_change or not _same(_space_z(left), _space_z(right))
                use = identity_change or _fields(left, ("role", "floor_id")) != _fields(right, ("role", "floor_id"))
                if xy:
                    fields.append("plan_geometry"); actions.add("plan_relations")
                if z:
                    fields.append("height"); actions.add("height_alignment")
                if use:
                    fields.append("identity_or_use"); actions.add("identity_and_use")
                if left.get("enclosure") != right.get("enclosure"):
                    fields.append("enclosure"); actions.add("enclosure_relations")
                if xy or z or identity_change:
                    changed_space_geometry.add(identity)
            elif collection == "boundaries":
                xy = identity_change or not _same(_vertices(left, (0, 1)), _vertices(right, (0, 1)))
                z = identity_change or not _same(_vertices(left, (2,)), _vertices(right, (2,)))
                relation = identity_change or any(
                    (sorted(left.get(key, [])) != sorted(right.get(key, [])) if key.endswith("_ids")
                     else left.get(key) != right.get(key))
                    for key in ("space_id", "geometry_type", "kind", "adjacent_space_ids", "counterpart_ids"))
                if xy or relation:
                    fields.append("plan_geometry_or_relation"); actions.add("plan_relations")
                if z:
                    fields.append("height"); actions.add("height_alignment")
                enclosure = not _same(_enclosure(left), _enclosure(right))
                if enclosure:
                    fields.append("enclosure"); actions.add("enclosure_relations")
                if fields:
                    counterpart_only = not (xy or z or enclosure) and all(
                        (sorted(left.get(key, [])) == sorted(right.get(key, [])) if key.endswith("_ids")
                         else left.get(key) == right.get(key))
                        for key in ("space_id", "geometry_type", "kind", "adjacent_space_ids"))
                    changed_refs = set(left.get("counterpart_ids", [])) ^ set(right.get("counterpart_ids", []))
                    slices = [(source, row, _mapping(source, "boundaries").get(other))
                              for source, row in ((before, left), (after, right))
                              for other in set(row.get("counterpart_ids", [])) & changed_refs]
                    if counterpart_only and slices and all(part is not None for _, _, part in slices):
                        local_boundary_relations[identity] = slices
                    else:
                        changed_boundaries.add(identity)
            elif collection == "openings":
                xy = identity_change or not _same(_vertices(left, (0, 1)), _vertices(right, (0, 1)))
                z = identity_change or not _same(_vertices(left, (2,)), _vertices(right, (2,)))
                relation = identity_change or (
                    _fields(left, ("kind", "host_boundary_id", "exterior", "connectivity"))
                    != _fields(right, ("kind", "host_boundary_id", "exterior", "connectivity"))
                    or sorted(left.get("space_ids", [])) != sorted(right.get("space_ids", []))
                    or _host_ids(before, left) != _host_ids(after, right))
                if xy or relation:
                    fields.append("placement_or_host"); actions.add("opening_hosts")
                if z:
                    fields.append("height"); actions.update(("height_alignment", "opening_hosts"))
                if relation and any(row.get("kind") in ("door", "passage") for row in (left, right)):
                    actions.add("plan_relations")
            else:
                if identity_change or not _same(left.get("footprint"), right.get("footprint")):
                    fields.append("footprint"); actions.add("plan_relations")
                if identity_change or not _same(_fields(left, ("z_floor", "height")), _fields(right, ("z_floor", "height"))):
                    fields.append("height"); actions.add("height_alignment")
                if identity_change or sorted(left.get("spanning_space_ids", [])) != sorted(right.get("spanning_space_ids", [])):
                    fields.append("membership"); actions.add("identity_and_use")
                if fields:
                    for source, row in ((before, left), (after, right)):
                        affected["spaces"].update(row.get("spanning_space_ids", []))
                        affected["spaces"].update(space["id"] for space in source.get("spaces", [])
                                                  if str(space.get("floor_id")) == identity)
            target = "windows" if collection == "openings" and (new or old).get("kind") == "window" else collection
            mark(target, identity, old, new, fields, actions)

    # Host/space movement can invalidate placement advice for unchanged apertures.
    for source in (before, after):
        for row in source.get("openings", []):
            if (changed_space_geometry.intersection(row.get("space_ids", []))
                    or changed_boundaries.intersection(_host_ids(source, row))
                    or any(_overlaps_height(row, part)
                           for host in _host_ids(source, row)
                           for _, _, part in local_boundary_relations.get(host, []))):
                target = "windows" if row.get("kind") == "window" else "openings"
                affected[target].add(row["id"])
                topics.add("opening_hosts")
                for space_id in row.get("space_ids", []):
                    affected["spaces"].add(space_id)
                    affected["floors"].update(_floors_for_space(source, space_id, row))

    def connections(source):
        grouped = defaultdict(list)
        for row in source.get("connections", []):
            grouped[row.get("opening_id")].append({
                **_fields(row, ("kind", "exterior", "state")), "space_ids": sorted(row.get("space_ids", []))})
        return {key: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)) for key, rows in grouped.items()}

    old_connections, new_connections = connections(before), connections(after)
    for identity in sorted(old_connections.keys() | new_connections.keys(), key=str):
        if old_connections.get(identity) == new_connections.get(identity):
            continue
        for source, records in ((before, old_connections), (after, new_connections)):
            for connection in records.get(identity, []):
                for space_id in connection["space_ids"]:
                    affected["spaces"].add(space_id)
                    affected["floors"].update(_floors_for_space(
                        source, space_id, _mapping(source, "openings").get(identity)))
        for source in (before, after):
            row = _mapping(source, "openings").get(identity)
            if row:
                target = "windows" if row.get("kind") == "window" else "openings"
                mark(target, identity, row, row, ["connection"], {"opening_hosts", "plan_relations"})
                break
        # Record disconnected/removed references as changes even when no opening survives.
        else:
            result["changes"].append({"collection": "connections", "id": identity,
                                      "change": "changed", "fields": ["connection"]})
            topics.update(("opening_hosts", "plan_relations"))

    frame_fields = ("yaw_degrees", "translation_m", "mesh_sha256")
    if not _same(_fields(before.get("mesh_frame") or {}, frame_fields),
                 _fields(after.get("mesh_frame") or {}, frame_fields)):
        result["changes"].append({"collection": "mesh_frame", "id": "mesh_frame",
                                  "change": "changed", "fields": list(frame_fields)})
        topics.add("mesh_registration")
        for source in (before, after):
            affected["floors"].update(_mapping(source, "floors"))
            affected["spaces"].update(_mapping(source, "spaces"))

    result.update(status="changes_detected" if result["changes"] else "no_material_changes",
                  previous_source_model_sha256=before.get("source_model_sha256"),
                  affected={key: sorted(ids) for key, ids in affected.items()},
                  actions=[{"topic": key, "action": value} for key, value in _ACTIONS.items() if key in topics])
    return result


def compact_review_scope(scope, limit=4):
    """Keep actions visible; complete changed entities live in save_facts.json."""
    result = {key: scope[key] for key in ("status", "numeric_tolerance", "actions")}
    if "reason" in scope:
        result["reason"] = scope["reason"]
    result["affected"] = {key: {"count": len(ids), "ids": ids[:limit],
                                "omitted_count": max(0, len(ids) - limit)}
                          for key, ids in scope["affected"].items() if ids}
    result["changed_entity_count"] = len({(row["collection"], row["id"]) for row in scope["changes"]})
    result["meaning"] = "Advisory delta, not quality acceptance; no_material_changes does not mean prior quality passed."
    return result
