"""Deterministic geometry kernel: CorrectedGeometry -> building geometry.

Turns corrected room cells (polygon-native, per-floor z) into the EnergyPlus
geometric model — zones + surfaces (walls / floors / ceilings) with split-pairing
(every interzone boundary cut so adjacent faces correspond 1:1, with reciprocal
`Outside Boundary Condition = Surface`) + windows. This is the "建模·几何 + 切配·
仿真" stages: pure deterministic geometry (no LLM, no physics/MEP). Physics
attachment (materials / schedules / loads / HVAC) stays a separate stage.

Polygon-native (shapely): a rectangle is just the simple case, so non-orthogonal /
L-shaped rooms need no rewrite. The model still owns geometric INTENT (room shape,
per-floor footprint, voids, openness) upstream in correction; this kernel only
realizes that intent deterministically.
"""

from src.agent.geometry.build import (
    BuildingGeometry,
    Surface,
    VerifiedWindowHostProof,
    Window,
    build_geometry,
)

__all__ = [
    "BuildingGeometry", "Surface", "VerifiedWindowHostProof", "Window",
    "build_geometry",
]
