"""Conservative metre-scale checks at declaration boundaries, never a rescaler.

These are mixed-unit tripwires, not architectural size limits. A horizontal
extent must be BOTH at least 5 km AND at least 1,000 times the declared floor
height. That catches an ordinary 5–50 m plan written in mm while
leaving large halls, long wings, tall spaces and survey-coordinate offsets alone.
Consecutive floor placements get the same ratio check with a 1 km minimum gap.
Only lengths/differences count; absolute coordinate magnitudes and prose do not.
Consistently wrong units and smaller mismatches can pass: prefer a missed alarm
to rejecting a plausible building with insufficient evidence.

Existing schema validators own malformed inputs. This module reads only numeric
fields it understands and never coerces, repairs, mutates or converts values.
"""
from __future__ import annotations

import math
from collections.abc import Iterable


MIN_PLAN_SPAN_M = 5000.0
MIN_FLOOR_GAP_M = 1000.0
MIN_SCALE_RATIO = 1000.0


class ScaleMismatchError(ValueError):
    """A declaration has strong, located evidence of incompatible unit scales."""


def _finite(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _largest_height(heights: Iterable[tuple[str, object]]) -> tuple[str, float] | None:
    known = [(path, float(value)) for path, value in heights if _finite(value) and value > 0]
    return max(known, key=lambda row: row[1]) if known else None


def check_planar_scale(
    spans: Iterable[tuple[str, float]], heights: Iterable[tuple[str, object]],
) -> None:
    """Reject only a huge span corroborated by a much smaller explicit height."""
    reference = _largest_height(heights)
    if reference is None:
        return
    height_path, height = reference
    for path, span in spans:
        if (_finite(span) and span >= MIN_PLAN_SPAN_M
                and span / height >= MIN_SCALE_RATIO):
            raise ScaleMismatchError(
                f"{path}: planar span {span:g} m is {span / height:g} times "
                f"{height_path}={height:g} m (checks: span >= {MIN_PLAN_SPAN_M:g} m "
                f"and span/height >= {MIN_SCALE_RATIO:g}). "
                "坐标看起来是毫米，世界坐标要用米。Check the drawing units and resubmit "
                "these world-coordinate fields in metres; no automatic conversion was applied."
            )


def _interval_span(value: object) -> float | None:
    if isinstance(value, list) and len(value) == 2 and all(_finite(v) for v in value):
        return abs(value[1] - value[0])
    return None


def ring_spans(vertices: object, path: str) -> list[tuple[str, float]]:
    if (not isinstance(vertices, list) or len(vertices) < 2
            or any(not isinstance(p, (list, tuple)) or len(p) != 2
                   or not all(_finite(v) for v in p) for p in vertices)):
        return []
    return [(f"{path} ({axis} span)", max(p[i] for p in vertices) - min(p[i] for p in vertices))
            for i, axis in enumerate(("x", "y"))]


def check_floor_placements(rows: Iterable[tuple[str, object, str, object]]) -> None:
    """Use adjacent bases, not absolute elevation or the total building height."""
    known = [(zpath, float(z), hpath, float(h)) for zpath, z, hpath, h in rows
             if _finite(z) and _finite(h) and h > 0]
    known.sort(key=lambda row: row[1])
    for first, second in zip(known, known[1:]):
        gap = second[1] - first[1]
        reference = max((first, second), key=lambda row: row[3])
        height = reference[3]
        if gap >= MIN_FLOOR_GAP_M and gap / height >= MIN_SCALE_RATIO:
            raise ScaleMismatchError(
                f"{second[0]}={second[1]:g} and {first[0]}={first[1]:g}: "
                f"consecutive floor-base gap {gap:g} m is {gap / height:g} times "
                f"{reference[2]}={height:g} m (checks: gap >= {MIN_FLOOR_GAP_M:g} m "
                f"and gap/height >= {MIN_SCALE_RATIO:g}). "
                "坐标看起来是毫米，世界坐标要用米。Check floor placement units; "
                "no automatic conversion was applied."
            )


def check_geometry_scale(geometry: dict, *, path: str = "proposal.geometry") -> None:
    """Check direct/edited v1–v3 proposals before source geometry compilation."""
    floors = geometry.get("floors", [])
    if not isinstance(floors, list):
        return
    placements = []
    for i, floor in enumerate(floors):
        if not isinstance(floor, dict):
            continue
        floor_path = f"{path}.floors[{i}]"
        heights = [(f"{floor_path}.ceiling_height", floor.get("ceiling_height"))]
        cells = floor.get("cells", [])
        cells = cells if isinstance(cells, list) else []
        footprint = floor.get("footprint")
        if isinstance(footprint, dict):
            spans = ring_spans(footprint.get("vertices"), f"{floor_path}.footprint.vertices")
        else:
            spans = [(f"{path}.footprint_{axis}", span) for axis in ("x", "y")
                     if (span := _interval_span(geometry.get(f"footprint_{axis}"))) is not None]
        for j, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            cell_path = f"{floor_path}.cells[{j}]"
            if cell.get("polygon"):
                spans.extend(ring_spans(cell["polygon"], f"{cell_path}.polygon"))
            else:
                spans.extend((f"{cell_path}.{axis}", span) for axis in ("x", "y")
                             if (span := _interval_span(cell.get(axis))) is not None)
        check_planar_scale(spans, heights)
        reference = _largest_height(heights)
        if reference:
            placements.append((f"{floor_path}.z_floor", floor.get("z_floor"), *reference))
    check_floor_placements(placements)
