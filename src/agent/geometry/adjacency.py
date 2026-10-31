"""Shared zone-volume adjacency helpers for kernel build/check paths."""

from __future__ import annotations

from src.agent.geometry.modelling import ZoneVolume


def zone_volumes_by_floor(zvs: list[ZoneVolume]) -> dict[int, list[ZoneVolume]]:
    by_floor: dict[int, list[ZoneVolume]] = {}
    for zv in zvs:
        by_floor.setdefault(zv.fi, []).append(zv)
    return by_floor


def expected_internal_interface_area(
    zvs: list[ZoneVolume],
    *,
    min_share_m: float,
    z_tol_m: float,
) -> float:
    expected = 0.0
    by_fi = zone_volumes_by_floor(zvs)

    # Vertical interior walls: same-floor adjacent cells sharing an edge.
    for group in by_fi.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                shared = a.polygon.boundary.intersection(b.polygon.boundary)
                length = getattr(shared, "length", 0.0)
                if length >= min_share_m:
                    height = min(a.zt - a.zf, b.zt - b.zf)
                    expected += length * height

    # Horizontal floor/ceiling: vertically adjacent cells whose polygons overlap.
    fis = sorted(by_fi)
    for lo_fi, hi_fi in zip(fis, fis[1:]):
        for a in by_fi[lo_fi]:
            for b in by_fi[hi_fi]:
                if abs(a.zt - b.zf) > z_tol_m:
                    continue
                ov = a.polygon.intersection(b.polygon).area
                if ov >= min_share_m:
                    expected += ov
    return expected
