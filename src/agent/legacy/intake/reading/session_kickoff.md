# Reading-stage session kickoff (0_reading)

You are running the **reading stage** of the staged intake pipeline: redraw each architectural drawing
with semantic pens. Trace every visible structural stroke by type (wall / window / wall_fill / outline
pen) and the dimension chains. Do **no spatial-topology placement** — assigning rooms, surfaces, zone
adjacency or facade world axes is downstream's job. The optional plan-frame datum, `scale_origin`
(`guide.md` §1/§2), is not an exception to that: it states where that plan's own local (0,0) sits
relative to a reference point drawn in that SAME image, and nothing else — never a cross-image or
cross-floor judgment. Fill it when it is cheap and confident (the common case is `0.00`/`0.00`); leave
it `null` otherwise.

This file is the single kickoff command. The durable rules are NOT duplicated here — they live in the
three rule docs below; the recap section is only a pointer so nothing here can silently drift from them.

## First: read the rule docs (required)

Read all three before tracing anything, then follow the worked-example plan JSON's style.
Canonical worked-example file: `src/agent/execution/isolation_templates/worked_example_plan.json`.
This is a synthetic, deliberately asymmetric format example, not a target-case precedent. Read it
for JSON shape and evidence-field style only; **never copy its coordinates or rewrite it**.

- `src/agent/legacy/intake/reading/guide.md` — error budget, global constraints, JSON schema,
  door-healing, self-check, downstream contract; this is the master rule container
- `src/agent/legacy/intake/reading/reading_guide.md` — how to *recognize* each element across drawing styles;
  it outputs only semantic-category labels
- `src/agent/legacy/intake/reading/pen_library.md` — what to *do* with each recognized category:
  which pen to use, what to keep/log, and when door-healing applies
- CV evidence tools: `src/agent/legacy/intake/reading/cv_toolbox.md` — deterministic pixel probes required before drawing on clean vector CAD PNGs; for noisy scans, hand drawings, or other degraded inputs, see that file's robustness-profile exception

## Non-negotiables — a checklist of WHAT to read, not a second copy of the rules

Do not memorize anything below; the durable text lives in the rule docs and must not be duplicated here
(a duplicated summary is exactly what drifted and degraded before). Each line just tells you which rule
to go read:

- **Calibrate and measure before writing meter coordinates on clean vector CAD PNGs** → `cv_toolbox.md`
  §Disciplines (use dimension-chain extension-line intersections or ticks as calibration anchors; for
  noisy scans, hand drawings, or other degraded inputs, follow that file's robustness-profile
  exception).
- **Error budget — read coordinates as final** → `guide.md` §0.1 (precise reading vs the *earned*
  redundant-dimension-channel escape hatch; prefer `null` over guessing; anchor against the testdata totals).
- **One stroke per continuous wall; no over-segmentation** → `guide.md` §5 (window jambs / dimension
  ticks / furniture are NOT partitions — the #1 failure mode on cluttered plans).
- **No topology placement; plans may declare `scale_origin`** → `guide.md` §1/§2/§3/§4 (forbidden
  fields; a plan MAY state `scale_origin.world_x_m` + `world_y_m` when cheap and confident, measured
  against a reference point in that SAME image only — never guessed, never a cross-floor judgment,
  `null` is fine; for elevations fill the facade-orientation fields exactly as the schema + `guide.md`
  define them — do not invent your own world-axis convention here).
- **Pens & healing** → `pen_library.md` (legal pen set per image kind; heal door openings into one
  continuous wall; one `wall_fill` per floor; recognize-then-log clutter in `uncaptured`).
- **Verbatim & null** → `guide.md` §1 (OCR verbatim; plan walls `thickness_m` = null; `null` when not found).

## Images for this case

Produce one JSON per source drawing, named **exactly `<expected_output_id>.json`**. The
`expected_output_id` for every source image is listed in the staging `input_inventory.json` (one
row per image: `input_id`, `file`, `view_type`, `expected_output_id`). **Do NOT derive the output
name from the source PNG by appending `_view`**: a stem that already ends in `_view` (e.g.
`1f_view`, `South_view`) IS its own `expected_output_id`, so its output file is `1f_view.json` —
not `1f_view_view.json`. A supplementary plan whose stem does not end in `_view` (e.g.
`supp_plan`) has `expected_output_id = supp_plan_view`. Merge refuses any output file whose stem is
not an `expected_output_id` in the manifest, so a mis-named file is a loud error, not a silent
substitute. Fill this manifest for the current case before tracing; every source image must have
exactly one output row or an explicit skip reason in `reading_summary.md`.

| source PNG | output JSON | image_kind | status / note |
|---|---|---|---|
| `1f_view.png` | `0_reading/1f_view.json` | plan | worked example if already supplied; otherwise pilot candidate |
| `2f_view.png` | `0_reading/2f_view.json` | plan | fill/delete row for this case |
| `3f_view.png` | `0_reading/3f_view.json` | plan | fill/delete row for this case |
| `South_view.png` | `0_reading/South_view.json` | elevation | fill/delete row for this case |
| `North_view.png` | `0_reading/North_view.json` | elevation | fill/delete row for this case |
| `East_view.png` | `0_reading/East_view.json` | elevation | fill/delete row for this case |
| `West_view.png` | `0_reading/West_view.json` | elevation | fill/delete row for this case |
| `supp_plan.png` | `0_reading/supp_plan_view.json` | supplementary / plan | choose by content; explain in note |
| `<section/detail/other>.png` | `0_reading/<expected_output_id>.json` | section / supplementary / other | include only if present; explain axes/scope in notes |

The `output JSON` column above is a **non-normative worked example** of the two shapes an
`expected_output_id` takes (identity for `1f_view` / `South_view` / …; `stem + "_view"` for
`supp_plan`). The authoritative name for THIS case is the `expected_output_id` in
`input_inventory.json` — re-derive it nowhere.

## Workflow

**This run has one pilot approval gate, after the pilot image.** The gate may contain one or more
rework turns; every rework stops at the same gate until external approval. A method error caught on
the first image costs one image's worth of work; the same error found at the end costs all of them.

1. Read the three rule docs + the worked-example JSON (understand the style).
2. Do **one** pilot image first (a plan) and finish it completely.
3. Run `guide.md` §6 self-check against that finished file, item by item, and fix what the
   self-check finds. This is your own pass over the pilot, not the review.
4. **Stop and wait for review of that pilot. Do not start the remaining images yet.** Ending your
   turn here is the correct move, not a failure to finish — write what you have, say the pilot is
   ready for review, and stop. You will be resumed. **If `feedback.md` is present in your workspace
   root, that is a rework request for the pilot**: read it, revise only the pilot, run its self-check
   again, and **stop again**. Feedback is not approval to batch.
5. Start the remaining images only after a later launcher turn explicitly says that external review
   approved the current pilot artifact. Do not rewrite the approved pilot. Then do the remaining
   plans + elevations + supplemental / section images, applying the same §6 self-check to each.
6. When all are done, write `0_reading/reading_summary.md`: per-image confidence self-assessment
   (high / medium / low, with reasons), which fields were repeatedly null / unknown, and your schema feedback.

⛔ **Do not ask questions mid-run and do not wait for an answer to one.** Rework and approval are
one-way state transitions, not a conversation. Stop only at the pilot gate; after explicit approval,
run the remaining work to completion on your own.

## Boundaries

- Do not modify anything under `src/`, `skills/`, `AI_agent/`.
- Do not modify the worked-example JSON (it is the reference).
- Do not run `run_full_pipeline.py` or any EnergyPlus tool.
- Do not produce IntakeOutput fields (zone_specs / surface_specs / fenestration_specs / …) — that is
  the downstream stages' job.

Do the pilot first, self-check it, then stop and wait for review. Rework feedback means revise the
pilot and stop again; only explicit external approval releases the remaining images and summary.
