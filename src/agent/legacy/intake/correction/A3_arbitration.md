# A3 — Conflict arbitration and completion

The judgment stage. A1/A2 hand A3 every conflict they could not resolve
deterministically; A3 chooses among plausible interpretations, completes missing
values, applies `A4` priors under hard gating, and hands decisions back to A2 to
re-apply deterministically. A3 is **mode-aware** (A0 §5).

```
Consumes:        A0 conflicts[] from A1 / A2-detect; A0 evidence items; A4 priors
Produces:        resolved values + decisions handed back to A2; corrections[];
                 conflicts[] (still-unresolved); unsupported[]
Emit corrections[] when: a conflict is resolved, a missing value is completed, or
                 a prior is applied
Emit conflicts[] / unsupported when: a conflict cannot be resolved safely
May change topology: only via a resolved identity decision (e.g. "these are the
                 same wall") that A2 then applies; A3 itself records the decision
```

---

## 1. Channel arbitration (by conflict type)

Resolve each `conflict_type` (A0 §3) using the claim-type authority ladders
(A0 §1.4). Every resolution logs `corrections[]` with all candidate `source_ids`.

| conflict_type | resolution |
|---|---|
| `stroke_vs_dimension` | trust the dimension chain over the stroke (numeric ladder); the stroke is a tracing offset |
| `cross_floor_axis_jitter` | pick the canonical axis from the most authoritative floor's evidence; unify all floors to it |
| `checksum_failure` | choose the authoritative chain (total vs segments) by grade + confidence; if tied, keep measured and either close within `DIMCHAIN_CLOSE_TOL` or mark `unsupported` |
| `facade_plan_mismatch` | reconcile via the higher-authority source; if neither dominates → `unsupported` |
| `reference_or_identity_ambiguity` | decide same-vs-distinct from `topology_identity` + `semantic` evidence; if undecidable → `unsupported` (never merge on doubt) |
| `semantic_size_prior` | see §3 (prior gating) |

Corridor/circulation identity: if adjacent corridor fragments are supported by
continuous circulation evidence (same label/use, open passage, continuous
centerline/width, and no physical wall/door partition between them), prefer one
continuous corridor zone. Do not merge across a drawn wall, door-controlled
partition, fire/stair/core boundary, floor break, or unresolved identity
ambiguity; when evidence is insufficient, keep distinct cells or mark
`unsupported` rather than merging on doubt.

## 2. Completion (missing values)

Fill an `unknown` from, in order: higher-authority same-entity evidence >
`inferred_topology` > `prior` (A4). Every completion logs `corrections[]` with
`source_ids` (and `prior_id` if a prior was used).

## 3. Prior usage (hard gating)

- `A4` priors are **advisory** (`prior_score` / `warning`); A3 decides, A4 never
  self-acts.
- A prior-driven correction is allowed **only** when evidence is missing,
  contradictory, or below the confidence threshold.
- A prior must **never** override consistent measured evidence.
- Priors are **typed by building / space type**; never a global value.
- **Semantic exception**: a small space carrying a high-confidence label
  (shaft / WC / storage / service / equipment) is kept even if its size is
  implausible against an office prior — mark `conflict` if needed, do not
  normalize it away.
- Every prior use logs `prior_id` + original + proposed + reason.

## 4. Mode-aware posture (A0 §5)

- `perimeter_core`: **conservative** — arbitrate envelope / facade / window and
  declared exceptions only; leave non-exception internal detail alone.
- `use_grouped_rooms`: **semantic + closure heavy** — resolve room-cell closure,
  adjacency, and use/schedule grouping; relax exact wall thickness.
- `room_identity`: **full** — resolve every internal boundary.

## 5. Unsupported policy

When a conflict cannot be resolved safely, emit `unsupported[]` with `reason` +
`regime_assumption_violated`. Never silently fix.

## 6. Confidence downgrade

A resolved-but-uncertain value carries **downgraded** confidence into the output,
so downstream stages and validation see the reduced certainty.

## 7. Hand back to A2

After resolving an identity / axis / checksum conflict, return the decision so A2
deterministically rebuilds the canonical axis set and snaps (A0 README runtime:
`A2-detect → A3-resolve → A2-apply`).
