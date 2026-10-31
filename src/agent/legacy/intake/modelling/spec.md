# 2_modelling — building geometry realization (deterministic)

Stage **建模·几何** of the pipeline. Deterministic code, no LLM, no physics. Turns
corrected room cells into a building geometry model: one thermal zone per cell, plus
the vocabulary to realize each face's vertices with correct outward orientation. It
makes **no** topology decision about which faces exist or how they pair — that is
[3_split_pairing](../split_pairing/spec.md).

Implementation: [`src/agent/geometry/modelling.py`](../../../../../src/agent/geometry/modelling.py).

## Input

`CorrectedGeometry` (the snapped phase-2a output): per-floor room cells (rectangle
`x`/`y` or explicit `polygon`), each floor's `z_floor` + `ceiling_height`, and windows
(facade + along-facade world span + world z + owning room). **Coordinates are
authoritative** — modelling never re-derives, re-snaps, or "improves" them.

## Output

- **Zone volumes** — one per cell: EP-safe zone name, footprint polygon (CCW), z range
  `[z_floor, z_floor + ceiling_height]`, floor membership. This is the leg-agnostic unit
  3_split_pairing consumes.
- **Face realization vocabulary** — functions that turn a boundary segment / footprint
  polygon / window spec into oriented vertices, used by 3_split_pairing and window
  attachment.

## Rules

1. **Cell = one thermal zone.** Granularity (faithful rooms vs grouped) is decided
   upstream (correction / zonification); modelling realizes whatever cells it is given.
2. **Outward orientation.** Every face's vertices are ordered CCW *seen from outside the
   zone*, decided by the polygon's Newell normal vs the desired outward direction —
   walls point away from the zone interior, floors point down, ceilings/roofs point up.
   This supersedes any hand-written vertex-order table.
3. **Wall vertices** come from a vertical extrusion of a footprint boundary segment over
   the zone's z range; **floor/ceiling vertices** come from the footprint polygon ring at
   the floor/ceiling z.
4. **Window vertices** lie on the parent wall's plane (`y=const` for N/S, `x=const` for
   E/W), spanning the given world span × world z, oriented to match the parent wall.
5. **Polygon-native.** A rectangle is the simple case; L/U-shaped or non-orthogonal cells
   need no special handling.
6. **EP-safe names** (letters / digits / `_` only), unique across the whole build.
7. **Tiling guard.** Same-floor cells must not overlap. An overlap (e.g. a corridor
   placed over the rooms it should sit between) is a phase-2a/correction defect — it is
   **flagged in notes, not papered over**, because overlapping cells produce same-side
   walls the InterZone gate rejects.

## Boundary

Modelling owns geometry realization only. Which faces are interior vs exterior, how
interzone faces are cut and paired, and reciprocal boundary conditions all belong to
3_split_pairing. Materials / constructions / loads / schedules belong to
[4_mep](../mep/mep.md) and the assembly stage.
