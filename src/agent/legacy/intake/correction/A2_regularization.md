# A2 — Regularization and snapping

Deterministic transforms over `A1` output and the typed evidence graph: build a
per-building canonical axis set, snap to it, close dimension chains, quantize,
and prevent slivers. A2 merges axes **only** on `topology_identity` evidence;
coordinate proximity alone is never enough. Over-tolerance ambiguity escalates
to `A3`.

```
Consumes:        A1 output (world-frame, centerline axes, reconciled z);
                 A0 topology_identity evidence (same axis / same wall); dimension chains
Produces:        per-building canonical axis set; snapped + quantized coordinates;
                 closed dimension chains; sliver-free pieces
Emit corrections[] when: an axis is snapped to a canonical value, a chain is
                 closed, a coordinate is quantized beyond OUTPUT_PRECISION, or a
                 sliver is absorbed
Emit conflicts[] / unsupported when: an offset falls in GAP_CONFLICT_BAND / exceeds
                 AXIS_JITTER_TOL, snapping would merge entities that topology or
                 semantic evidence says are distinct, or a sub-MIN_EDGE_LENGTH
                 sliver cannot be safely absorbed
May change topology: yes, **only** for logged sub-MIN_EDGE_LENGTH degenerate
                 sliver absorption; all other A2 operations (snap / close /
                 quantize) preserve topology
```

Applies under **all** profiles. Under `perimeter_core`, internal-axis
canonicalization is needed only for declared exception spaces + attribution;
envelope/facade axes are always canonicalized.

---

## 1. Canonical axis set (per building)

- Build a per-building set of canonical x-axes, y-axes, and z-levels that all
  references snap to.
Axis identity is governed by `AXIS_JITTER_TOL` **only** — the gap-closing bands
(`GAP_CLOSE_THRESHOLD` / `GAP_CONFLICT_BAND` / `GAP_UNSUPPORTED`) are a separate
operation (connectivity, not identity; A0 §4 precedence note).

- An evidence item joins an existing canonical axis only when **both**:
  (a) `topology_identity` evidence supports "same intended axis / wall", **and**
  (b) the offset ≤ `AXIS_JITTER_TOL`.
- Coordinate proximity alone (a `numeric` argument) is **not** sufficient to
  merge (A0 §1.4).
- **Offset > `AXIS_JITTER_TOL`**: never merge on proximity → emit
  `reference_or_identity_ambiguity` (or `cross_floor_axis_jitter` for the
  pure-numeric subtype) → A3, unless A3 has already resolved that identity.
- Topology / semantic evidence says distinct (shaft, stair, genuine stagger) →
  do not merge regardless of offset → conflict → A3.
- **Cross-floor unification**: the same intended wall across floors snaps to one
  canonical axis. This is the primary guard against cross-floor jitter slivers.

## 2. Snapping

- The canonical axis **value** is chosen from authoritative evidence (or an A3
  decision), **not** by rounding to `SNAP_GRID`. Do **not** round canonical
  values to `SNAP_GRID` before dimension-chain closure — that would degrade
  authoritative dimensions and then "close" the damage.
- `SNAP_GRID` is a candidate regularization grid for **low-confidence,
  stroke-only** geometry that has no dimension backing; it is not pre-closure
  rounding for dimensioned axes.
- Snap joined coordinates to their (evidence-chosen) canonical axis value. Each
  snap that moves a coordinate beyond `OUTPUT_PRECISION` is a `correction`.

## 3. Dimension-chain closure

- Per axis, the inner segment chain must sum to the outer total within
  `DIMCHAIN_CLOSE_TOL`.
- Within tolerance → distribute the residual to close (e.g. normalize nominally
  equal bays); log as `correction`.
- Beyond tolerance → `checksum_failure` → A3 to choose the authoritative chain;
  do not guess.

## 4. Quantization

- **After** canonicalization + closure, quantize coordinates to
  `OUTPUT_PRECISION` for output. This is `normalization` (not logged) unless it
  changes a value beyond precision.
- Never quantize **before** canonicalization — it manufactures spurious distinct
  axes.

## 5. Sliver prevention

- No generated edge / piece below `MIN_EDGE_LENGTH`. This is the direct guard
  against the degenerate-fragment EnergyPlus crash class.
- A sub-`MIN_EDGE_LENGTH` piece almost always means two axes were not unified:
  first re-snap to the canonical axis (§1). If that resolves it → `correction`
  (no topology change).
- If a genuine sub-threshold feature remains, absorption is the **only**
  topology-changing A2 operation and must be logged (`changes_topology = true`).
  Mark `unsupported` instead of absorbing when the sliver is semantically
  meaningful, belongs to a declared exception space (shaft / stair / WC / service),
  or cannot be absorbed without changing source attribution.

## 6. Runtime feedback

A2 runs as **detect → A3-resolve (+A4) → apply** (A0 README runtime). On
detecting an over-tolerance conflict, A2 emits the conflict and hands to A3;
after A3's decision, A2 deterministically rebuilds the canonical set and snaps.
