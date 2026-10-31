# J1 — 1_correction VLM judge rubric (gate ②)

Runs **after** the deterministic checks (`correction_checks.json`) pass. The judge
sees **[original drawings + role-coloured zone plans `zones_<floor>.png` + gt-overlay
`grade.png` + machine gt reconciliation `score_vs_gt` + reference answer]** (the
`judge_packet.json` the flow assembles) and rules each criterion `pass | minor | severe
| fatal | not_applicable | insufficient_evidence` with **structured evidence, not a
numeric score**.

Unlike J0, this judge **sees the original drawings** — it is the external check
that covers 1_correction's own image-blind blind spot, and it can attribute a
defect back to **0_reading** (recognition) vs **1_correction** (topology / world
placement).

Question this stage answers: **"Can the building be redrawn from this corrected
geometry with no qualitative error? Are zone / window counts right (there is a
reference answer)?"** — this is the most human-verification-dependent stage.

## Criteria (redraw fidelity)
1. **zonification fidelity** — filled zone plan vs original plan: no rooms wrongly
   merged / split / missing / invented. Read `source_partition_evidence` and
   `source_partition_criterion` in the judge packet first. The former includes
   independent GT polygon correspondence and partial upstream wall-stroke
   support, with per-object findings and input hashes. Known extra physical
   partitions, lost walls and false source splits/merges are severe; good
   counts, overall area or historical EP success cannot offset them.
2. **cross-floor consistency** — the same wall aligns across floors (no ~5 cm
   unexplained jitter). Retain offsets supported by the input: a small shift
   that loses a junction is a topology failure; a real floor-to-floor offset
   must not be erased merely to align the floors.
3. **window position fidelity** — each window lands on the correct facade / floor /
   position vs the original elevation.
4. **count vs reference** — zone count and window count match the reference
   (testdata + per-case `gt.json`); any difference has a sound explanation. This
   double-covers the deterministic zone-count tripwire: tripwire = fast count
   check, judge = layout-level ruling on whether a merge/split is legitimate.
5. **overall redraw** — holistically, can the original be redrawn with no
   qualitative error?

## Disposition
- `source_partition_evidence` is judge evidence, not an automatic whole-BIM
  approval. Boundary-offset-only discrepancies require coordinate/reference-
  plane review, and the raw geometric result remains in the report. Partial
  reading wall support cannot define room identities: a doorway gap does not
  merge the source rooms. Inspect unsupported spans against doors and original
  drawings before calling them invented walls. No usable reference means
  insufficient evidence, never a count-based pass. If these findings contradict
  a proposed pass/minor, resolve the specific objects in the verdict evidence.
- **fatal / severe** → route to the **attributed root stage** (`0_reading` or
  `1_correction`), 3 attempts → terminate. 1_correction is `stochastic`, so the
  retry is a **blind resample** — judge commentary is logged out-of-band, never
  injected into the prompt.
- **minor** → flag and pass.
- Attribute `root_stage` only with sufficient evidence + confidence; otherwise
  leave `root_stage=null` (→ human triage, not auto-routed).
- J1 is the confirm gate for any J0 severe/fatal finding passed through as
  `correction_recoverable`. Re-judge independently against the originals and
  reference answer; no `correction_obligations[]` hand-off is required.
- If a J0 pass-through defect is not fixed, attribute the root by evidence:
  surviving channel insufficient or perception/category error visible in originals
  → `root_stage="0_reading"` (re-read); channels sufficient but correction failed
  the arbitration → `root_stage="1_correction"` (blind resample).

## Output
A `StageVerdict` (verdict schema v2). Reference answer source: testdata (zone
count / floors / footprint) + lightweight per-case `gt.json` (window count,
optional zone layout).
