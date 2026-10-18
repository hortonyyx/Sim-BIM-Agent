"""Deterministic reconciliation of supplied drawing opening observations.

This module deliberately does not inspect pixels or infer openings.  A model (or
person) supplies compact, reviewable marks from a named input image; the code
only checks whether those marks correspond one-to-one with the explicit
openings already present in a source BIM.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from types import SimpleNamespace

from shapely.errors import GEOSException
from shapely.ops import unary_union

from src.agent.geometry.source_bim import EPS, _on_wall


_SOURCE_TO_REVIEW_KIND = {"door": "door", "window": "window", "open": "passage"}
_REVIEW_KINDS = frozenset(_SOURCE_TO_REVIEW_KIND.values())
_REVIEW_FIELDS = frozenset({"floor_id", "kind", "image", "coverage", "marks", "facade"})
_MARK_FIELDS = frozenset({"mark_id", "box", "opening_ids", "space_ids", "basis", "note"})
_FACADES = ("North", "South", "East", "West")
_FACADE_NORMALS = {
    "North": (0.0, 1.0), "South": (0.0, -1.0),
    "East": (1.0, 0.0), "West": (-1.0, 0.0),
}
_FACADE_EPS = 1e-7


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def _source_hash(source: dict) -> str:
    if not isinstance(source, dict):
        raise ValueError("source must be a source_model.json object")
    payload = {key: value for key, value in source.items() if key != "source_model_sha256"}
    digest = _canonical_sha256(payload)
    declared = source.get("source_model_sha256")
    if declared is not None and declared != digest:
        raise ValueError("source_model_sha256 does not match source content")
    return digest


def revalidate_saved_review(parent: dict, source: dict, saved: dict, images: dict,
                            *, plan_calibration: dict | None = None) -> dict:
    """Recompute saved observations after a physically unchanged note/use edit.

    The old verdict is never reused. Unknown source fields remain in the equality
    check; only explicit note/provenance and room-use fields are excluded.
    """
    parent_hash = _source_hash(parent)
    _source_hash(source)
    if saved.get("source_model_sha256") != parent_hash:
        raise ValueError("saved opening review does not belong to the parent source")

    def physical(value):
        ignored = {"source_model_sha256", "source_geometry_sha256", "generation",
                   "assumptions", "corrections", "public_names"}
        result = {k: v for k, v in value.items() if k not in ignored}
        result["spaces"] = [{k: v for k, v in row.items() if k not in {"role", "role_evidence"}}
                            for row in value.get("spaces", [])]
        return result

    if physical(parent) != physical(source):
        raise ValueError("opening review revalidation requires unchanged physical source and hosts")
    observations = saved.get("observations")
    if not isinstance(observations, dict):
        raise ValueError("saved opening review has no original observations")
    name = observations.get("image")
    image = images.get(name, {})
    if saved.get("image") != {"name": name, "sha256": image.get("sha256"), "size": image.get("size")}:
        raise ValueError("saved opening review original image identity changed")
    report = review_openings(source, observations, images, plan_calibration=plan_calibration)
    # A new calibration is a new observation context, even if it happens to pass.
    # Record IDs may change while identical anchors retain the same meaning.
    fields = ("status", "reason", "x_anchors", "y_anchors")
    before, after = saved.get("location_check", {}), report["location_check"]
    if not isinstance(before, dict):
        raise ValueError("saved opening review has invalid calibration evidence")
    if any(before.get(k) != after.get(k) for k in fields):
        raise ValueError("saved opening review calibration changed")
    return report


def _source_index(source: dict, *, logical=True) -> tuple[str, dict, dict, dict]:
    """Return immutable-source indexes needed by both public functions."""
    source_hash = _source_hash(source)
    if not isinstance(source.get("floors"), list) or not isinstance(source.get("spaces"), list):
        raise ValueError("source requires floors and spaces lists")
    if not isinstance(source.get("openings"), list):
        raise ValueError("source requires an openings list")

    floors: dict[str, dict] = {}
    for row in source["floors"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("source floor requires a nonempty id")
        if row["id"] in floors:
            raise ValueError(f"duplicate source floor id: {row['id']}")
        floors[row["id"]] = row
    spaces: dict[str, dict] = {}
    for row in source["spaces"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("floor_id"), str):
            raise ValueError("source space requires id and floor_id")
        if row["id"] in spaces or row["floor_id"] not in floors:
            raise ValueError("source space ids and floor references must be unique and valid")
        spaces[row["id"]] = row

    openings: dict[str, dict] = {}
    from src.agent.geometry.opening_segments import logical_openings
    for row in logical_openings(source) if logical else source['openings']:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("source opening requires a nonempty id")
        if row["id"] in openings or row.get("kind") not in _SOURCE_TO_REVIEW_KIND:
            raise ValueError("source opening ids and kinds must be unique and supported")
        space_ids = row.get("space_ids")
        if not isinstance(space_ids, list) or not space_ids or any(sid not in spaces for sid in space_ids):
            raise ValueError(f"source opening {row['id']} has invalid space_ids")
        floor_ids = {spaces[sid]["floor_id"] for sid in space_ids}
        vertices = row.get("vertices")
        if not isinstance(vertices, list) or len(vertices) < 2:
            raise ValueError(f"source opening {row['id']} requires vertices")
        try:
            xy_points = [[float(vertex[0]), float(vertex[1])] for vertex in vertices]
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"source opening {row['id']} has invalid vertices") from exc
        if not all(math.isfinite(value) for point in xy_points for value in point):
            raise ValueError(f"source opening {row['id']} has non-finite vertices")
        endpoints = []
        for point in xy_points:
            if point not in endpoints:
                endpoints.append(point)
            if len(endpoints) == 2:
                break
        if len(endpoints) != 2:
            raise ValueError(f"source opening {row['id']} requires two distinct plan endpoints")
        if len(floor_ids) > 1:
            # Floor IDs can denote same-level wings or a tall circulation
            # volume. Identity differences alone do not invalidate a doorway.
            # Its actual aperture must still lie within BOTH source spaces.
            try:
                z_values = [float(vertex[2]) for vertex in vertices]
                if not all(math.isfinite(z) for z in z_values):
                    raise ValueError("non-finite opening height")
                if max(z_values) <= min(z_values):
                    raise ValueError("non-positive opening height")
                for sid in space_ids:
                    base = float(spaces[sid]["z_floor"])
                    height = float(spaces[sid]["height"])
                    if not math.isfinite(base) or not math.isfinite(height) or height <= 0:
                        raise ValueError("invalid space height")
                    if min(z_values) < base - 1e-7 or max(z_values) > base + height + 1e-7:
                        raise ValueError("aperture lies outside a connected space")
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise ValueError(f"source opening {row['id']} has invalid cross-instance heights: {error}") from error
        openings[row["id"]] = {
            "id": row["id"],
            **({'segment_ids': row['segment_ids']} if 'segment_ids' in row else {}),
            "kind": _SOURCE_TO_REVIEW_KIND[row["kind"]],
            "space_ids": list(row["space_ids"]),
            "floor_id": spaces[space_ids[0]]["floor_id"],
            **({"floor_ids": sorted(floor_ids)} if len(floor_ids) > 1 else {}),
            "plan_endpoints": endpoints,
            "vertices": copy.deepcopy(vertices),
            # These are intentionally retained from the immutable source
            # record.  They are needed only when a reviewer explicitly limits
            # a review to one exterior facade; the legacy plan-review path
            # never depends on them.
            "host_boundary_id": row.get("host_boundary_id"),
            "exterior": row.get("exterior"),
        }
    return source_hash, floors, spaces, openings


def opening_inventory(source: dict) -> dict:
    """Expose actual explicit source openings by floor and source space.

    A cross-instance opening appears in every associated floor/volume group,
    with explicit floor_ids, but is counted once in the global inventory.
    Group counts must not be summed to count unique building openings.
    The inventory is evidence for a later visual review, never a count inferred
    from room names, free text, or the review itself.
    """
    source_hash, floors, spaces, openings = _source_index(source)
    by_floor = {floor_id: [] for floor_id in floors}
    by_space = {space_id: [] for space_id in spaces}
    for opening in openings.values():
        for floor_id in opening.get("floor_ids", [opening["floor_id"]]):
            by_floor[floor_id].append(opening)
        for space_id in opening["space_ids"]:
            by_space[space_id].append(opening)

    def summarize(rows: list[dict], *, include_openings: bool) -> dict:
        ordered = sorted(rows, key=lambda row: row["id"])
        counts = Counter(row["kind"] for row in ordered)
        summary = {
            "opening_count": len(ordered),
            "counts": {kind: counts[kind] for kind in sorted(_REVIEW_KINDS)},
            "opening_ids": [row["id"] for row in ordered],
        }
        if include_openings:
            summary["openings"] = [copy.deepcopy(row) for row in ordered]
        return summary

    unbuilt = source.get("unbuilt_openings", [])
    unsupported = source.get("unsupported", [])
    if not isinstance(unbuilt, list) or not isinstance(unsupported, list):
        raise ValueError("source unbuilt_openings and unsupported must be lists when present")

    return {
        "schema_version": "opening_inventory_v1",
        "source_model_sha256": source_hash,
        "opening_count": len(openings),
        "counts": {kind: sum(row["kind"] == kind for row in openings.values()) for kind in sorted(_REVIEW_KINDS)},
        "floors": [
            {"floor_id": floor_id, **summarize(by_floor[floor_id], include_openings=True)}
            for floor_id in sorted(floors)
        ],
        "spaces": [
            {"space_id": space_id, "floor_id": spaces[space_id]["floor_id"],
             **summarize(by_space[space_id], include_openings=False)}
            for space_id in sorted(spaces)
        ],
        "scope": {
            "actual_openings": "only explicit built source openings are inventoried",
            "unbuilt_opening_ids": [row.get("id") for row in unbuilt if isinstance(row, dict) and row.get("id")],
            "unbuilt_opening_count": len(unbuilt),
            "unsupported_observation_count": len(unsupported),
        },
    }


def _finite_xy(vertex: object) -> tuple[float, float] | None:
    if not isinstance(vertex, (list, tuple)) or len(vertex) < 2:
        return None
    try:
        x, y = float(vertex[0]), float(vertex[1])
    except (TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _signed_area(points: list[tuple[float, float]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(points, points[1:] + points[:1])) / 2


def _facade_for_boundary(boundary: object, space: object) -> tuple[str | None, str | None]:
    """Return a cardinal outward facade or an explicit reason it is unavailable.

    Source boundaries use their owning source-space ring.  The ring's winding,
    rather than an image name or an opening identifier, determines which side
    of the directed wall is outside.
    """
    if not isinstance(boundary, dict) or boundary.get("geometry_type") != "wall":
        return None, "host_not_wall"
    vertices = boundary.get("vertices")
    if not isinstance(vertices, list) or len(vertices) < 2:
        return None, "host_geometry_invalid"
    a, b = _finite_xy(vertices[0]), _finite_xy(vertices[1])
    if a is None or b is None:
        return None, "host_geometry_invalid"
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= _FACADE_EPS:
        return None, "host_geometry_invalid"
    if not isinstance(space, dict) or not isinstance(space.get("polygon"), list):
        return None, "host_space_geometry_missing"
    ring = [_finite_xy(point) for point in space["polygon"]]
    if any(point is None for point in ring) or len(ring) < 3:
        return None, "host_space_geometry_missing"
    area = _signed_area(ring)  # type: ignore[arg-type]
    if abs(area) <= _FACADE_EPS:
        return None, "host_space_geometry_invalid"
    # For a CCW ring space is on the left of each directed edge, so its
    # exterior is the right-hand normal.  Reverse that for a CW ring.
    nx, ny = ((dy / length, -dx / length) if area > 0
              else (-dy / length, dx / length))
    for facade, expected in _FACADE_NORMALS.items():
        if abs(nx - expected[0]) <= _FACADE_EPS and abs(ny - expected[1]) <= _FACADE_EPS:
            return facade, None
    return None, "host_direction_unsupported"


def _wall_exposure(boundary: dict, relations: list[dict]):
    """Return wall area not shared with another space, in along-wall/Z space.

    A boundary relation may cover only part of the wall's length or height.
    Invalid/missing contact patches make exposure unknown rather than exterior.
    The source kernel's coplanarity check and EPS are used only for numerical
    geometry, never to enlarge a drawing opening or erase a physical contact.
    """
    wall = SimpleNamespace(vertices=boundary.get("vertices"))
    try:
        parent = _on_wall(wall.vertices, wall)
        if parent is None or not parent.is_valid or parent.area <= EPS ** 2:
            raise ValueError("invalid wall area")
        patches = []
        for relation in relations:
            regions = relation.get("regions")
            if not isinstance(regions, list) or not regions:
                raise ValueError("missing contact regions")
            for region in regions:
                if not isinstance(region, dict) or not isinstance(region.get("holes"), list):
                    raise ValueError("invalid contact region")
                patch = _on_wall(region.get("vertices"), wall)
                if patch is None or not patch.is_valid or patch.area <= EPS ** 2:
                    raise ValueError("invalid contact region")
                for hole_vertices in region["holes"]:
                    hole = _on_wall(hole_vertices, wall)
                    if hole is None or not hole.is_valid or hole.area <= EPS ** 2:
                        raise ValueError("invalid contact hole")
                    if not patch.buffer(EPS).covers(hole):
                        raise ValueError("contact hole outside region")
                    patch = patch.difference(hole)
                if patch.area <= EPS ** 2 or not patch.is_valid or not parent.buffer(EPS).covers(patch):
                    raise ValueError("contact region outside wall")
                patches.append(patch)
        if boundary.get("adjacent_space_ids") and not relations:
            raise ValueError("missing contact relation")
        contact = unary_union(patches)
        return parent.difference(contact), contact, None
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, GEOSException):
        return None, None, "contact_geometry_unavailable"


def facade_inventory(source: dict) -> dict:
    """Expose source-derived exterior-facade scope without guessing from images.

    A row is emitted for every cardinal exterior wall direction on each floor,
    including directions with zero built openings.  That makes an explicit
    empty-facade review necessary before a collection of facade reviews can
    describe a whole floor/kind scope.
    """
    source_hash, floors, spaces, openings = _source_index(source, logical=False)
    from src.agent.geometry.opening_segments import logical_openings
    logical_openings(source)  # Check group geometry before aggregating individual host checks.
    raw_boundaries = source.get("boundaries", [])
    if not isinstance(raw_boundaries, list):
        raw_boundaries = []
    boundaries = {
        row.get("id"): row for row in raw_boundaries
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }
    raw_relations = source.get("boundary_relations")
    relations_valid = isinstance(raw_relations, list) and all(
        isinstance(relation, dict) and isinstance(relation.get("boundary_ids"), list)
        and len(relation["boundary_ids"]) == 2
        and all(isinstance(bid, str) for bid in relation["boundary_ids"])
        for relation in raw_relations
    )
    by_boundary: dict[str, list[dict]] = {}
    if relations_valid:
        for relation in raw_relations:
            for boundary_id in relation["boundary_ids"]:
                by_boundary.setdefault(boundary_id, []).append(relation)
    source_spaces = {
        row.get("id"): row for row in source.get("spaces", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }
    exterior_boundaries: dict[tuple[str, str], list[str]] = {}
    unsupported_exterior_boundaries: dict[str, list[dict]] = {}
    exposure: dict[str, tuple[object, object, str | None]] = {}
    for boundary_id, boundary in boundaries.items():
        if boundary.get("geometry_type") != "wall":
            continue
        space_id = boundary.get("space_id")
        space = spaces.get(space_id)
        if space is None:
            continue
        exposed, contact, exposure_reason = (
            _wall_exposure(boundary, by_boundary.get(boundary_id, []))
            if relations_valid else (None, None, "contact_geometry_unavailable")
        )
        exposure[boundary_id] = exposed, contact, exposure_reason
        if exposure_reason is not None:
            unsupported_exterior_boundaries.setdefault(space["floor_id"], []).append({
                "boundary_id": boundary_id, "reason": exposure_reason,
            })
            continue
        if exposed.area <= EPS ** 2:
            continue
        facade, reason = _facade_for_boundary(boundary, source_spaces.get(space_id))
        if facade is not None:
            exterior_boundaries.setdefault((space["floor_id"], facade), []).append(boundary_id)
        else:
            # A wall with no currently built opening still matters: otherwise
            # a missing whole facade could vanish from the required coverage.
            unsupported_exterior_boundaries.setdefault(space["floor_id"], []).append({
                "boundary_id": boundary_id,
                "reason": reason or "host_direction_unsupported",
            })

    classifications: dict[str, dict] = {}
    for opening_id, opening in openings.items():
        result = {"opening_id": opening_id, "kind": opening["kind"],
                  "floor_id": opening["floor_id"], "facade": None,
                  **({"floor_ids": opening["floor_ids"]} if "floor_ids" in opening else {})}
        if opening.get("exterior") is not True:
            result["reason"] = "not_exterior"
        else:
            host_id = opening.get("host_boundary_id")
            boundary = boundaries.get(host_id)
            if boundary is None:
                result["reason"] = "unknown_host_boundary"
            elif boundary.get("space_id") not in opening["space_ids"]:
                result["reason"] = "host_space_mismatch"
            else:
                exposed, contact, exposure_reason = exposure.get(
                    host_id, (None, None, "contact_geometry_unavailable"))
                if exposure_reason is not None:
                    result["reason"] = exposure_reason
                else:
                    try:
                        aperture = _on_wall(opening["vertices"], SimpleNamespace(vertices=boundary["vertices"]))
                    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                        aperture = None
                    if (aperture is None or not aperture.is_valid or aperture.area <= EPS ** 2
                            or contact.intersection(aperture).area > EPS ** 2
                            or not exposed.buffer(EPS).covers(aperture)):
                        result["reason"] = "host_not_exterior"
                    else:
                        facade, reason = _facade_for_boundary(
                            boundary, source_spaces.get(boundary.get("space_id")))
                        if reason is not None:
                            result["reason"] = reason
                        elif host_id not in exterior_boundaries.get((opening["floor_id"], facade), []):
                            result["reason"] = "host_not_exterior"
                        else:
                            result["facade"] = facade
        classifications[opening_id] = result

    for identity, ids in source.get('opening_segment_groups', {}).items():
        parts = [classifications.pop(key) for key in ids]
        result = {**parts[0], 'opening_id': identity, 'segment_ids': list(ids)}
        if len({p.get('facade') for p in parts}) != 1 or any(p.get('reason') for p in parts):
            result.update(facade=None, reason='fragment_hosts_do_not_share_one_exterior_facade')
        classifications[identity] = result
    by_floor = []
    for floor_id in sorted(floors):
        facades = []
        for facade in _FACADES:
            boundary_ids = sorted(exterior_boundaries.get((floor_id, facade), []))
            if not boundary_ids:
                continue
            opening_ids = sorted(opening_id for opening_id, row in classifications.items()
                                 if floor_id in row.get("floor_ids", [row["floor_id"]]) and row.get("facade") == facade)
            facades.append({"facade": facade, "exterior_boundary_ids": boundary_ids,
                            "opening_ids": opening_ids})
        unresolved = [copy.deepcopy(row) for row in classifications.values()
                      if floor_id in row.get("floor_ids", [row["floor_id"]]) and row.get("facade") is None]
        by_floor.append({"floor_id": floor_id, "facades": facades,
                         "non_facade_openings": sorted(unresolved, key=lambda row: row["opening_id"]),
                         "unsupported_exterior_boundaries": sorted(
                             unsupported_exterior_boundaries.get(floor_id, []),
                             key=lambda row: row["boundary_id"]),
                         })
    return {"schema_version": "opening_facade_inventory_v1", "source_model_sha256": source_hash,
            "floors": by_floor, "opening_classifications": classifications}


def _reject(message: str) -> None:
    raise ValueError(f"invalid opening review: {message}")


def _image_info(images: dict, image_name: str) -> tuple[int, int, str]:
    if not isinstance(images, dict) or image_name not in images:
        _reject(f"unknown image: {image_name}")
    row = images[image_name]
    if not isinstance(row, dict) or set(row) != {"size", "sha256"}:
        _reject(f"image {image_name} requires only size and sha256")
    size = row["size"]
    if (not isinstance(size, list) or len(size) != 2 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in size)):
        _reject(f"image {image_name} has invalid size")
    if any(float(v) != int(v) for v in size) or not isinstance(row["sha256"], str) or not row["sha256"]:
        _reject(f"image {image_name} has invalid metadata")
    return int(size[0]), int(size[1]), row["sha256"]


def _validate_review(review: dict, images: dict) -> tuple[int, int, str]:
    if (not isinstance(review, dict) or not set(review) in
            {_REVIEW_FIELDS, _REVIEW_FIELDS - {"facade"}}):
        _reject("review fields must be floor_id, kind, image, coverage, marks, and optional facade")
    if not isinstance(review["floor_id"], str) or not review["floor_id"]:
        _reject("floor_id must be a nonempty string")
    if review["kind"] not in _REVIEW_KINDS or review["coverage"] not in {"complete", "partial"}:
        _reject("kind or coverage is invalid")
    if not isinstance(review["image"], str) or not isinstance(review["marks"], list):
        _reject("image and marks have invalid types")
    if "facade" in review and review["facade"] not in _FACADES:
        _reject("facade must be North, South, East, or West when supplied")
    width, height, image_hash = _image_info(images, review["image"])
    mark_ids: set[str] = set()
    for index, mark in enumerate(review["marks"]):
        location = f"marks[{index}]"
        if not isinstance(mark, dict):
            _reject(f"{location} must be an object")
        missing = sorted((_MARK_FIELDS - {"note"}) - set(mark))
        extra = sorted(set(mark) - _MARK_FIELDS)
        if missing or extra:
            _reject(f"{location} (mark_id={mark.get('mark_id')!r}) has "
                    f"missing fields {missing} and unsupported fields {extra}")
        mark_id = mark["mark_id"]
        if not isinstance(mark_id, str) or not mark_id or mark_id in mark_ids:
            _reject("mark_id must be nonempty and unique")
        mark_ids.add(mark_id)
        if (not isinstance(mark["box"], list) or len(mark["box"]) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in mark["box"])):
            _reject(f"mark {mark_id} has a non-finite box")
        x0, y0, x1, y1 = map(float, mark["box"])
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x0 >= x1 or y0 >= y1:
            _reject(f"mark {mark_id} box is outside image bounds")
        if (not isinstance(mark["opening_ids"], list) or
                not all(isinstance(value, str) and value for value in mark["opening_ids"])):
            _reject(f"mark {mark_id} opening_ids must be a list of nonempty strings")
        if (not isinstance(mark["space_ids"], list) or not mark["space_ids"] or
                not all(isinstance(value, str) and value for value in mark["space_ids"])):
            _reject(f"mark {mark_id} space_ids must be a nonempty list of strings")
        if mark["basis"] not in {"visible", "inferred", "uncertain"}:
            _reject(f"mark {mark_id} basis is invalid")
        if not isinstance(mark.get("note", ""), str):
            _reject(f"{location} (mark_id={mark_id!r}) note must be a string when supplied")
    return width, height, image_hash


def review_openings(source: dict, review: dict, images: dict, *, plan_calibration: dict | None = None) -> dict:
    """Check one floor/kind image review against explicit source openings.

    A ``complete`` review is a completeness claim only for its stated floor and
    kind.  A ``partial`` review is retained as useful evidence but never gets a
    full-coverage verdict.  Neither verdict evaluates drawing fidelity.
    """
    source_hash, floors, spaces, openings = _source_index(source)
    width, height, image_hash = _validate_review(review, images)
    projection = None
    location_check = dict(status="not_checked", reason="no_registered_plan_calibration")
    if "facade" in review:
        location_check["reason"] = "elevation_marks_do_not_use_plan_xy_calibration"
    elif plan_calibration is not None:
        from src.agent.geometry.source_image_overlay import _axis_anchors
        if (plan_calibration.get("image"), plan_calibration.get("floor_id"), plan_calibration.get("image_sha256")) != (
                review["image"], review["floor_id"], image_hash):
            _reject("plan calibration must match the exact image, floor and image hash")
        sx, ix, _ = _axis_anchors(plan_calibration["x_anchors"], axis="x", size=width)
        sy, iy, _ = _axis_anchors(plan_calibration["y_anchors"], axis="y", size=height)
        projection = lambda point: [(point[0] - ix) / sx, (point[1] - iy) / sy]
        location_check = dict(status="checked_against_supplied_plan_boxes",
            calibration_id=plan_calibration.get("calibration_id"),
            x_anchors=copy.deepcopy(plan_calibration["x_anchors"]),
            y_anchors=copy.deepcopy(plan_calibration["y_anchors"]),
            note="Both source aperture endpoints must fit the observed original-pixel box. Calibration and marks are caller supplied; containment does not prove complete width or drawing truth.")
    findings: list[dict] = []
    def finding(code: str, **evidence) -> None:
        findings.append({"code": code, **evidence})

    if review["floor_id"] not in floors:
        finding("unknown_floor", floor_id=review["floor_id"])
    facade_index = facade_inventory(source) if "facade" in review else None
    facade = review.get("facade")
    floor_kind_ids = {opening_id for opening_id, opening in openings.items()
                      if review["floor_id"] in opening.get("floor_ids", [opening["floor_id"]]) and opening["kind"] == review["kind"]}
    if facade_index is None:
        scope = floor_kind_ids
        exclusions = []
        exterior_boundary_ids = []
    else:
        classifications = facade_index["opening_classifications"]
        scope = {opening_id for opening_id in floor_kind_ids
                 if classifications[opening_id].get("facade") == facade}
        floor_row = next((row for row in facade_index["floors"]
                          if row["floor_id"] == review["floor_id"]), None)
        facade_row = next((row for row in (floor_row or {}).get("facades", [])
                           if row["facade"] == facade), None)
        exterior_boundary_ids = list((facade_row or {}).get("exterior_boundary_ids", []))
        exclusions = [copy.deepcopy(classifications[opening_id]) for opening_id in sorted(floor_kind_ids - scope)]
        if review["floor_id"] in floors and facade_row is None:
            finding("facade_not_present", floor_id=review["floor_id"], facade=facade)
    matched_ids: set[str] = set()
    referenced_ids: set[str] = set()
    reused_ids: set[str] = set()
    seen_ids: set[str] = set()

    mark_boxes: dict[tuple[float, float, float, float], str] = {}
    for mark in review["marks"]:
        mark_id = mark["mark_id"]
        box = tuple(map(float, mark["box"]))
        if box in mark_boxes:
            finding("duplicate_mark_box", mark_id=mark_id, other_mark_id=mark_boxes[box], box=list(box))
        else:
            mark_boxes[box] = mark_id
        ids = mark["opening_ids"]
        if not ids:
            finding("unmodeled_observed_mark", mark_id=mark_id, space_ids=list(mark["space_ids"]))
            if mark["basis"] in {"inferred", "uncertain"}:
                finding("observation_pending", mark_id=mark_id, basis=mark["basis"], opening_ids=[])
            continue
        if len(ids) != 1:
            finding("mark_multiple_opening_ids", mark_id=mark_id, opening_ids=list(ids))
        valid_mark = len(ids) == 1
        for opening_id in ids:
            if opening_id in seen_ids:
                reused_ids.add(opening_id)
            seen_ids.add(opening_id)
            opening = openings.get(opening_id)
            if opening is None:
                finding("unknown_opening_id", mark_id=mark_id, opening_id=opening_id)
                valid_mark = False
                continue
            referenced_ids.add(opening_id)
            if review["floor_id"] not in opening.get("floor_ids", [opening["floor_id"]]):
                finding("mark_floor_mismatch", mark_id=mark_id, opening_id=opening_id,
                        actual_floor_id=opening["floor_id"], review_floor_id=review["floor_id"])
                valid_mark = False
            if opening["kind"] != review["kind"]:
                finding("mark_kind_mismatch", mark_id=mark_id, opening_id=opening_id,
                        actual_kind=opening["kind"], review_kind=review["kind"])
                valid_mark = False
            if facade_index is not None:
                classification = facade_index["opening_classifications"][opening_id]
                if classification.get("facade") != facade:
                    finding("mark_facade_mismatch", mark_id=mark_id, opening_id=opening_id,
                            review_facade=facade, actual_facade=classification.get("facade"),
                            reason=classification.get("reason"))
                    valid_mark = False
            if set(mark["space_ids"]) != set(opening["space_ids"]) or len(mark["space_ids"]) != len(opening["space_ids"]):
                finding("mark_space_ids_mismatch", mark_id=mark_id, opening_id=opening_id,
                        actual_space_ids=list(opening["space_ids"]), marked_space_ids=list(mark["space_ids"]))
                valid_mark = False
            if projection is not None:
                pixels = [projection(point) for point in opening["plan_endpoints"]]
                x0, y0, x1, y1 = box
                if any(not (x0 - 1e-6 <= p[0] <= x1 + 1e-6 and y0 - 1e-6 <= p[1] <= y1 + 1e-6)
                       for p in pixels):
                    finding("mark_source_location_mismatch", mark_id=mark_id,
                        opening_id=opening_id, observed_box=list(box), source_endpoints_pixels=pixels)
                    valid_mark = False
        if valid_mark:
            matched_ids.add(ids[0])
        if mark["basis"] in {"inferred", "uncertain"}:
            finding("observation_pending", mark_id=mark_id, basis=mark["basis"], opening_ids=list(ids))

    for opening_id in sorted(reused_ids):
        finding("opening_id_reused", opening_id=opening_id)
        matched_ids.discard(opening_id)

    if review["coverage"] == "complete":
        for opening_id in sorted(scope - matched_ids):
            finding("unaccounted_model_opening", opening_id=opening_id)
    else:
        finding("partial_review_not_complete", floor_id=review["floor_id"], kind=review["kind"])

    room_coverage = []
    for space_id, space in sorted(spaces.items()):
        if space["floor_id"] != review["floor_id"]:
            continue
        actual_ids = sorted(opening_id for opening_id in scope if space_id in openings[opening_id]["space_ids"])
        matched = sorted(opening_id for opening_id in actual_ids if opening_id in matched_ids)
        room_coverage.append({
            "space_id": space_id,
            "actual_opening_ids": actual_ids,
            "actual_opening_count": len(actual_ids),
            "matched_opening_ids": matched,
            "matched_opening_count": len(matched),
            "missing_opening_ids": sorted(set(actual_ids) - set(matched)),
        })

    conclusion = "consistent_with_supplied_observations" if not findings else "observations_require_follow_up"
    review_scope = {"floor_id": review["floor_id"], "kind": review["kind"], "coverage": review["coverage"]}
    if facade is not None:
        review_scope["facade"] = facade
    return {
        "schema_version": "opening_review_v1",
        "source_model_sha256": source_hash,
        "image": {"name": review["image"], "sha256": image_hash, "size": [width, height]},
        "review_scope": review_scope,
        "model_opening_ids": sorted(scope),
        "matched_opening_ids": sorted(matched_ids),
        "referenced_opening_ids": sorted(referenced_ids),
        "room_coverage": room_coverage,
        "findings": findings,
        "conclusion": conclusion,
        "drawing_fidelity": "not_evaluated",
        "location_check": location_check,
        "source_scope": {
            "actual_openings": "only explicit built source openings were reviewed",
            "unbuilt_opening_count": len(source.get("unbuilt_openings", [])),
            "unsupported_observation_count": len(source.get("unsupported", [])),
        },
        **({"facade_scope": {"facade": facade,
                               "exterior_boundary_ids": exterior_boundary_ids,
                               "excluded_openings": exclusions,
                               "coverage_requires_explicit_empty_review": True}}
           if facade is not None else {}),
    }
