"""Single, gt-blind ownership boundary for correction footprints."""
from __future__ import annotations

import hashlib
import json
from typing import Any

def _schema_version_of(geom: Any) -> str:
    # Keep this leaf helper independent of ``src.agent.geometry``: importing the
    # geometry package eagerly loads its build exports during correction package
    # initialization.  This is the identical legacy defaulting rule.
    return str(getattr(geom, "schema_version", "1") or "1")


def _legacy_ring(geom: Any) -> list[list[float]]:
    fx, fy = geom.footprint_x, geom.footprint_y
    x0, x1 = sorted((float(fx[0]), float(fx[1])))
    y0, y1 = sorted((float(fy[0]), float(fy[1])))
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def floor_footprint(geom: Any, floor: Any) -> list[list[float]]:
    if _schema_version_of(geom) == "3":
        footprint = getattr(floor, "footprint", None)
        if footprint is None:
            raise ValueError("v3 floor footprint is required")
        return [[float(x), float(y)] for x, y in footprint.vertices]
    return _legacy_ring(geom)


def floor_footprint_from_payload(data: dict, floor_payload: dict | None = None) -> list[list[float]]:
    if str(data.get("schema_version", "1")) == "3":
        if not isinstance(floor_payload, dict) or not isinstance(floor_payload.get("footprint"), dict):
            raise ValueError("v3 floor footprint is required")
        vertices = floor_payload["footprint"].get("vertices")
        if not isinstance(vertices, list):
            raise ValueError("v3 footprint vertices are required")
        return [[float(p[0]), float(p[1])] for p in vertices]
    fx, fy = data.get("footprint_x"), data.get("footprint_y")
    if not isinstance(fx, list) or not isinstance(fy, list) or len(fx) != 2 or len(fy) != 2:
        raise ValueError("legacy footprint_x/y are required")
    x0, x1 = sorted((float(fx[0]), float(fx[1])))
    y0, y1 = sorted((float(fy[0]), float(fy[1])))
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def footprint_bbox(geom: Any, floor: Any | None = None) -> tuple[tuple[float, float], tuple[float, float]]:
    if floor is None:
        if _schema_version_of(geom) != "3":
            return tuple(sorted(map(float, geom.footprint_x))), tuple(sorted(map(float, geom.footprint_y)))
        floor = geom.floors[0]
    ring = floor_footprint(geom, floor)
    return (min(p[0] for p in ring), max(p[0] for p in ring)), (min(p[1] for p in ring), max(p[1] for p in ring))


def floor_key(geom: Any, floor: Any) -> str:
    return str(floor.id if _schema_version_of(geom) == "3" else floor.name)


def footprint_fingerprint(vertices: list[list[float]] | list[tuple[float, float]]) -> str:
    """Stable geometry digest for a footprint ring, independent of encoding.

    The digest is intentionally based on the physical open CCW ring after a
    canonical start-vertex rotation, not producer winding/explicit closure.
    It is the v3 FacadeSegment provenance binding, not an artifact-byte hash.
    """
    pts = [(float(x), float(y)) for x, y in vertices]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if not pts:
        raise ValueError("footprint fingerprint requires vertices")
    signed = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    if signed < 0:
        pts.reverse()
    start = min(range(len(pts)), key=lambda index: pts[index])
    canonical = pts[start:] + pts[:start]
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def floor_footprint_fingerprint(geom: Any, floor: Any) -> str:
    return footprint_fingerprint(floor_footprint(geom, floor))
