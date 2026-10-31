# 3_split_pairing — split-pairing (deterministic, leg-agnostic)

Stage **切配·仿真** of the pipeline. Deterministic code, no LLM. Decides every face's
existence and outside-boundary condition, cutting adjacent faces so each interzone face
corresponds 1:1 with exactly one opposite face (reciprocal `Outside Boundary
Condition = Surface`). EnergyPlus requires this per-face one-to-one matching; geometry
modeling alone produces one-to-many adjacencies.

**Leg-agnostic.** It consumes a list of zone volumes (footprint polygon + z range +
floor membership) and is indifferent to how zonification produced them — faithful rooms
or re-topologized thermal blocks only change the input count, never the algorithm.

Implementation: [`src/agent/geometry/split_pairing.py`](../../../../../src/agent/geometry/split_pairing.py).
Spec of "correct" = the InterZone gate
([`src/validator/interzone.py`](../../../../../src/validator/interzone.py)), which judges this
stage's output post-hoc.

## Input / Output

- **In**: `list[ZoneVolume]` from [2_modelling](../modelling/spec.md).
- **Out**: `list[Surface]` — each with type (Wall / Floor / Ceiling / Roof), oriented
  vertices, OBC (`Outdoors` / `Ground` / `Surface`), and for interzone faces a reciprocal
  `obc_obj` pointing at its exact partner.

## Rules

### Vertical (interior walls vs exterior)
For each **same-floor** cell pair, the shared footprint-boundary segment becomes a
**reciprocal wall pair** (`Surface` ↔ `Surface`, each pointing at the other). The
remainder of every cell's boundary (boundary minus all shared segments) becomes
**exterior** walls (`Outdoors`).

### Horizontal (floor / ceiling / roof / ground)
- **Bottom floor** (`Ground`): a cell on the lowest floor gets a `Ground` floor.
- **Interfloor pairing**: an upper cell's floor is cut against each **stacked** lower
  cell (one whose ceiling z equals the upper floor z within tolerance) it overlaps; each
  overlap piece becomes a reciprocal **floor ↔ ceiling** pair. The pairing is created once
  from the upper side; the lower cell's matching ceiling piece is created with it.
- **Exposed underside** (`Outdoors`): any part of an upper cell's floor not over a lower
  cell (a cantilever) is exposed.
- **Roof** (`Outdoors`): any part of a cell's ceiling not under a stacked upper cell (top
  floor, or a setback) is a roof.

### Cutting
Faces are cut at the **union of both sides' break points** so each resulting interzone
piece pairs exactly one opposite piece — never one-to-many.

### Tolerances
Reuse the InterZone gate thresholds so a clean build passes by construction: minimum edge
length 0.10 m (shorter segments dropped as slivers), z-stack match 0.02 m, minimum face /
overlap area 0.05 m².

## Boundary

Split-pairing decides geometry and reciprocity only. It does **not** assign constructions.
The construction-symmetry rule (paired interzone faces must share / reverse the same
construction, e.g. `Cons_InterFloor`) is satisfied downstream by the assembly stage
giving both paired faces the same construction name — that is a physics/assembly concern,
not this stage's.
