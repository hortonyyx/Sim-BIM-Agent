# J0 — 0_reading VLM judge rubric (gate ②)

Runs **after** the deterministic linter (`reading_checks.json`) passes. The judge
sees **[original drawing + wireframe render + reading JSON]** and rules each
criterion below `pass | minor | severe | fatal | not_applicable |
insufficient_evidence` **with structured evidence — never a numeric score**.

This is a **per-image perception** check: "what did this drawing show, and was it
traced faithfully?" Do NOT reason about topology, cross-image consistency, or
world placement — those are 1_correction's (J1).

## Criteria (seven classes of obvious recognition error)
1. **missed real element** — a real wall or window in the drawing has no stroke.
2. **clutter traced as structure** — furniture / paving / text / grid axes /
   stairs / dimension-chain ticks traced as a wall/window stroke (should be
   `uncaptured` or `dimensions[]`, not a stroke). When `testdata_prompt.json`
   carries per-floor `thermal_zones`, also sanity-check plan band/cell counts for
   over-segmentation (e.g. north rooms + corridor + south rooms) before passing.
3. **pen misclassified** — a wall traced as window (or vice-versa); on elevations,
   `wall_fill` vs `outline` vs `window` confusion, including an elevation door
   emitted as `window`.
4. **number copied wrong** — a dimension's `text_verbatim` does not match the
   number printed in the drawing. **This is the transcription-truth check** (the
   deterministic linter only checks internal consistency; the real-number truth
   lives here / independent OCR).
5. **image-local orientation self-consistent** — `view_facade` / `mirrored`
   declarations are consistent with what the elevation shows. **Declaration-level
   only**; the world translation is judged in J1.
6. **door healing wrong** — a door opening was not healed into a continuous wall
   (or a real opening was wrongly healed shut).
7. **whole-region missing / misplaced** — an entire room / wing / floor band is
   absent or grossly displaced.

## Disposition
- **Disposition = severity x recoverability**, not severity alone:

  | severity | correction_recoverable | unrecoverable / unknown |
  |---|---|---|
  | severe / fatal | pass-through to correction; J1 confirms | halt -> re-read |
  | minor | flag + pass | flag + pass |

- A severe/fatal finding is `correction_recoverable` only when all four conditions hold:
  1. it is a value/coordinate error, not an existence or identity/category error;
  2. an independent surviving channel pins the truth (faithfully emitted dimension
     chain, footprint closure, cross-floor twin, or facade-plan cross-reference);
  3. the conflict is in correction's deterministically-resolvable set (`stroke_vs_dimension`,
     `cross_floor_axis_jitter`, or dominant-channel `checksum_failure`);
  4. the defect is honestly surfaced by provenance/confidence or a gate1 CROSS_CHECK flag.
- If any condition fails, set `recoverability="unrecoverable"` or `"unknown"` and halt.
  Default on uncertainty is unrecoverable. A false green costs more than a wasted re-read.
- Runtime disposition: `0_reading` is manual by default, so an unrecoverable/unknown
  severe or fatal finding stops at `human_redraw_required`. When
  `RunPolicy.reading_runner_available` is enabled and the root-stage budget remains,
  the orchestrator returns non-terminal `awaiting_reread`; the main Agent then runs a
  blind cold-start sub-agent re-read. The sub-agent receives only original case images,
  `testdata_prompt.json`, and the 0_reading skill — no prior strokes, judge
  commentary, prior attempts, or gt — and writes/replaces the flat `0_reading/*_view.json`
  working copy before `run_stage resample ... 0_reading --force` records the next
  attempt. At `per_stage_draws`, quarantine/human triage.
- Set `root_stage="0_reading"` when the defect is a recognition error; leave
  `root_stage=null` + low `root_confidence` when you cannot attribute confidently
  (do **not** force a routing decision).

## Output
A `StageVerdict` (verdict schema v2): `criteria[]` with `status`,
`recoverability`, and evidence + `root_stage` + `root_confidence` + `retriable`.
Commentary is **out-of-band only** — it is never injected back into the reading prompt.
