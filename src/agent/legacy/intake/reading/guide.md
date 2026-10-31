# Reading-stage guide (0_reading) — flow, constraints, and output container

The reading stage turns each architectural drawing into a vector JSON. The model acts like an artist
re-tracing the original image with a set of **semantically labeled pens**, without doing any
spatial-topology reasoning. All topology is left to the correction stage.

This document is the **master guide**: the error budget, the global constraints, the output
container (JSON format), and the processing discipline (door-healing, self-check, downstream
contract). Two sibling docs split off the perception and the action:

- **How to *recognize* what each stroke is** (what walls / doors / windows / dimensions look like,
  across drawing styles) → [`reading_guide.md`](reading_guide.md)
- **What to *do* with a recognized category** (which pen / keep-or-ignore / heal) →
  [`pen_library.md`](pen_library.md)

The three are wired by a shared **semantic-category vocabulary**: the reading guide outputs a
category label ("door", "wall", "furniture", …); the pen library maps that label to a phase-1
action; this guide holds the rules and the container that both feed into.

---

## 0. Mental model

Think of the reading stage as "re-tracing the source image with a set of semantically labeled pens":
- the **wall pen** traces every stroke that drew a wall
- the **window pen** traces every stroke that drew a window
- … plus dimension chains and text annotations

(How to recognize each element across styles → [`reading_guide.md`](reading_guide.md);
the pen each category maps to → [`pen_library.md`](pen_library.md).)

**What the reading stage does**: identify which component type each stroke is, and trace its geometry by type.
**What the reading stage does NOT do**: merge strokes into "one exterior wall" / outline "this is a room" /
say "this window belongs to that wall" / judge "this wall faces outside or inside".

All of that topology reasoning is left to the correction stage.

### 0.1 Error budget (important)

The reading stage and the correction stage are mutually exclusive in the kind of error each can introduce:

| Stage | Can see | Errors it can introduce |
|---|---|---|
| the reading stage | the source image (multimodal) | **perception errors**: misread dimension, missed stroke, offset coordinate, wrong elevation x-axis direction |
| the correction stage | the reading stage JSON + skill rule docs + testdata_prompt metadata (**does not see the image**) | **pure reasoning errors**: wrong grouping, inside/outside misjudged, parent-child mapping wrong, IntakeOutput field wrong |

**Implications**:
- **Read every coordinate as if it is final.** The default is a hard line: a position you place is
  taken downstream as truth — the correction stage cannot see the image to second-guess it. Once the
  reading stage offsets a coordinate, flips an elevation x-axis, or misreads a dimension, the
  correction stage takes what it gets.
- **The one escape hatch is the redundant channel, and it must be earned.** An offset coordinate can
  be recovered by correction *only if* you also emit the dimension chain that pins it in `dimensions[]`
  (literal `text_verbatim` + parsed `value_m`) **and** mark that stroke's `provenance` honestly as
  `dimension_derived`. No surviving dimension chain ⇒ no recovery. And a missed stroke, wrong
  category, or dimension annotation emitted as a `wall` destroys evidence outright and the redundant
  channel cannot save it — it must be re-read. So do not lean on "correction will fix it": either
  transcribe the dimension chain that backs the coordinate, or read the coordinate precisely.
- **Anchor against the testdata totals.** Read the building's total dimensions / floor count / floor
  height from `testdata_prompt.json` and use them to *check* your coordinates (the perimeter should
  span the stated total width/depth; per-floor z should match the floor height). This is a cross-check
  only — never copy testdata content into the JSON in place of reading the image.
- When diffing IntakeOutput, any inconsistency tied to the source image roots 100% in the reading stage; only
  topology / naming / field-format errors are the correction stage's.
- So when writing the JSON, the reading stage **prefers null over guessing** — null means "I couldn't see it",
  whereas a guessed value makes the correction stage treat a wrong number as truth.
- **Two-channel discipline**: wall/window strokes and dimension chains are independent evidence channels.
  A dimension annotation, cumulative tick, extension line, or window/door sub-dimension must NEVER become
  a `wall` stroke. Put dimensions in `dimensions[]`; put uncertainty in provenance/confidence.

### 0.2 Effect of simulation physics

In EnergyPlus a zone is enclosed by **surfaces (2D faces)**; a wall has no thickness concept. So:
- a "thick black wall" in plan is just a **centerline** (2D polyline) in simulation; the wall body
  width does not participate in the calculation
- the reading stage need not estimate wall thickness — fill `thickness_m` with `null`
- an elevation `wall_fill` rectangle is only a z-range signal source (which layer's z is where), it
  does not mean "the wall is this thick"
- **a door is simply ignored in energy simulation**: a "wall with a door" is, in its simulation
  reality, **one continuous wall**. So when the reading stage sees a door opening it heals the wall to be
  continuous (door-healing, see §2.1); the door symbol only triggers the heal and does not enter
  `strokes`

---

## 1. Global constraints

- **Units**: meters, two decimals
- **Each image is read in its OWN local 2D frame — the reading stage does no topology placement and
  no cross-image world placement**.
  The optional plan-frame datum is `scale_origin`; when you give it, it declares how the measured
  plan-local frame relates to a shared reference, but it does not assign rooms, surfaces, or facade
  topology, and it is never worth guessing:
  - `image_kind="plan"`: x = horizontal (drawing right), y = vertical (drawing up). A plan is already
    drawn in plan orientation, so these read directly as the building's plan axes. A plan view SHOULD
    also declare `scale_origin` as specified below WHEN it is cheap and confident — the common shape is
    plan-local `(0,0)` already sitting at an easily-seen corner of THIS drawing, which makes
    `world_x_m`/`world_y_m` simply `0.00`. Do not chase this: never try to determine the building's
    overall cross-floor footprint or judge wall thickness to answer it (that is topology/correction's
    job downstream, not this per-image datum) — if a confident per-image answer is not immediately
    available, leave `scale_origin` `null` rather than guess.
  - `image_kind="elevation"`: x = horizontal along that facade (purely in-image), y = height (up
    positive). Record WHICH facade this is and which screen direction local-x increases in the
    image-local `facade` block (§4); do NOT state a world axis / sign / base here — the correction
    stage derives world placement from the plan footprint + this facade block.
  - `image_kind="section"`: defined per image; record what the axes mean in `note`s, image-local only.
  - `image_kind="supplementary"` / `"other"`: use only when the source is not a normal plan/elevation.
    Keep the same image-local discipline, explain the scope/axes in notes, and do not invent topology.
- **Tracing rule**: write what is drawn; fill `null` when not found; never backfill defaults from
  background knowledge
- **OCR text verbatim**, do not translate

---

## 2. JSON Schema

```jsonc
{
  // ===== metadata =====
  "image_label": "Floor 1 plan view",       // use the official label from testdata_prompt.json
  "image_kind": "plan | elevation | section | supplementary | other",
  // scale_origin: OPTIONAL for every plan (non-plan views always use null). When you give it, it
  // records where THIS image's own plan-local (0,0) sits relative to an easily-seen reference point
  // drawn in THIS SAME image — e.g. the outer corner of the drawn footprint, or a dimension-chain
  // terminal. Prefer measuring plan-local (0,0) AT that reference point, which makes
  // world_x_m/world_y_m simply 0.00; if plan-local (0,0) is elsewhere in this image, record its
  // signed measured offset from that reference. Do NOT reason about the overall building's
  // cross-floor footprint or wall thickness to answer this — that is out of scope for a per-image
  // datum (topology/correction's job downstream). Plan drawings carry no z datum, so world_z_m stays
  // null. Do not guess any value — omit (null) the whole object rather than write a number you are
  // not confident of.
  "scale_origin": {
    "world_x_m": 0.00,
    "world_y_m": 0.00,
    "world_z_m": null,
    "note": "plan-local (0,0) measured at this drawing's own SW outer corner"
  },
  // facade: IMAGE-LOCAL elevation orientation (elevation only; plan → null). Records which facade
  // this view shows. Local-x is FIXED by convention (image left edge = x 0, increasing right), so
  // there is no `local_x_positive` to declare — see §4. NO world axis / sign / base either — the
  // correction stage derives world placement from the plan footprint + this block (§4).
  "facade": {
    "view_facade": "South",                 // which facade this elevation shows (from the image label/metadata)
    "mirrored": "false",                    // "true" | "false" | "unknown"
    "orientation_evidence": [
      {"source": "image_name", "detail": "label says 'South elevation'", "confidence": "high"}
    ]
  },

  // ===== strokes =====
  // each stroke = one continuously drawn stroke + its semantic type (pen).
  // Concrete re-trace examples: the wall pen can draw a wall line from (0,0)→(15,0);
  // the window pen can draw an elevation/window rectangle from (1.4,1.0)→(3.8,2.8).
  // how to recognize each element → reading_guide.md; which pen a category maps to → pen_library.md (plan and elevation differ).
  // door handling: a door opening in a wall does **not** break the wall — heal the two
  //   segments split by the door into one continuous wall stroke, and record
  //   "healed door opening at <position>" in that stroke's note (in EP a wall is a continuous
  //   boundary face and a door is ignored, so the continuous wall is faithful to the simulation).
  //   A window opening is NOT healed — keep it as a window pen. Guardrails (only heal openings
  //   with a door symbol, do not heal a doorless open span) see §2.1.
  "strokes": [
    {
      "id": "S1",
      "pen": "wall",                        // pen type — see pen_library.md for the legal set
                                            // (door is never a pen: a door only triggers healing, see §2.1)
      "provenance": "seen",                 // seen | dimension_derived | estimated | unknown
      "confidence": "high",                 // high | medium | low
      "dimension_refs": [],                 // required when provenance="dimension_derived"
      "geometry": {
        "kind": "line",                     // line | rect | polyline
        "p1": [0.00, 0.00],
        "p2": [15.00, 0.00],
        "thickness_m": null                 // plan walls always null (EP zones are enclosed by surfaces, walls have no thickness)
      },
      "note": ""                            // free text, e.g. "south horizontal perimeter wall"
    },
    // rect-fill example (elevation wall body, elevation window)
    {
      "id": "S99",
      "pen": "window",
      "provenance": "seen",
      "confidence": "high",
      "dimension_refs": [],
      "geometry": {
        "kind": "rect",
        "x_range_m": [1.40, 3.80],          // this image's local coordinates
        "y_range_m": [1.00, 2.80]
      },
      "note": "south facade F2 window 1"
    },
    // polyline example (non-straight wall)
    {
      "id": "S100",
      "pen": "wall",
      "provenance": "seen",
      "confidence": "medium",
      "dimension_refs": [],
      "geometry": {
        "kind": "polyline",
        "points": [[0,0],[5,0],[5,3],[8,3]],
        "thickness_m": null,
        "closed": false
      },
      "note": ""
    },
    // door-healing example: a door split this wall in the source; healed into one continuous wall + trace note
    {
      "id": "S101",
      "pen": "wall",
      "provenance": "seen",
      "confidence": "high",
      "dimension_refs": [],
      "geometry": {
        "kind": "line",
        "p1": [5.00, 0.00],
        "p2": [10.00, 0.00],
        "thickness_m": null
      },
      "note": "healed door opening at x≈7.5 (door swing seen in plan); EP wall is continuous"
    }
  ],

  // Provenance mapping into correction's A0 evidence model:
  // - seen = visual existence evidence; its numeric coordinate is estimated_stroke, NOT direct_measurement
  // - dimension_derived = numeric transcribed_dimension and requires non-empty dimension_refs
  // - estimated = low-confidence estimated_stroke
  // - unknown or missing = legacy/unknown; correction must downgrade confidence

  // ===== dimension chains (structured composite primitives) =====
  // visually a "tick + number + tick" chunk is one unit, classified on its own; the correction stage
  // uses it to derive coordinates. Emit the chain STRUCTURED so the closure check (Σ segments == overall)
  // can run and so the redundant channel (see §0.1) actually pins your coordinates. One entry per
  // number; group a chain with a shared chain_id; role = overall | segment | baseline; order = its
  // position along the chain. text_verbatim = the literal characters as drawn (truth); value_m = parsed metres.
  "dimensions": [
    // For a clearly drawn wing-break endpoint only, a segment dimension may
    // additionally carry all three exact keys below. Never infer these from
    // OCR/note wording or ordinary segmentation dimensions.
    // "boundary_kind": "wing_break", "boundary_endpoint": "from" | "to",
    // "boundary_ref": "south-wing-a"
    {
      "id": "D1",
      "text_verbatim": "15000",             // literal OCR string exactly as drawn (truth — keep units/format)
      "value_m": 15.00,                     // parsed metres
      "from": [0.00, 0.00],
      "to":   [15.00, 0.00],
      "axis": "x",                          // x | y | z. On an elevation the vertical dimension axis is "z",
                                            // while elevation STROKES carry the same vertical extent as
                                            // image-local `y_range_m` (§4). Same direction, two names —
                                            // dimensions say z, strokes say y. Do not convert between them here.
      "chain_id": "C_bottom",               // groups the strings of one dimension chain
      "role": "overall",                    // overall | segment | baseline
      "order": 0,                           // position within the chain
      "anchor": null,                       // optional; MUST be a flat list of numbers or null — never an object.
                                            //   [x0, y0, x1, y1] = pixel bbox of the printed number
                                            //   [x, y]           = a single pixel point
                                            // For a chain entry prefer the bbox of the tick-to-tick SPAN;
                                            // for a standalone callout use the bbox of the number itself.
                                            // Rotated (vertical) text: say which of the two you used in `note`.
      "note": "bottom total-length chain"
    },
    {
      "id": "D2",
      "text_verbatim": "5000",
      "value_m": 5.00,
      "from": [0.00, 0.00],
      "to":   [5.00, 0.00],
      "axis": "x",
      "chain_id": "C_bottom",               // same chain as the overall above
      "role": "segment",
      "order": 1,
      "note": "segment 1 of the bottom chain"
    },
    {
      "id": "D3",
      "text_verbatim": "10000",
      "value_m": 10.00,
      "from": [5.00, 0.00],
      "to":   [15.00, 0.00],
      "axis": "x",
      "chain_id": "C_bottom",
      "role": "segment",
      "order": 2,
      "note": "segment 2; Σ segments (5+10) == overall (15) — the closure check the correction stage runs"
    }
  ],

  // ===== text annotations =====
  "ocr_texts": [
    {"id": "T1", "text": "Office 101", "anchor": [3.00, 1.50], "note": ""}
  ],

  // ===== uncaptured: seen but deliberately NOT traced into strokes (canonical TOP-LEVEL carrier) =====
  // Record here (top-level — NOT nested in self_check) anything "seen but not drawn into strokes":
  //   (1) strokes the pen dictionary can't cover (cornice / index arrow ...)
  //   (2) clutter actively excluded by selective extraction (furniture / paving / texture / room text boxes ...)
  //   (3) every healed door opening ("healed door opening at <position> on <stroke>")
  // "acknowledged skip" vs "silent loss" makes a world of difference at review time. A genuinely clean
  // drawing may be []; but if you healed a door or excluded clutter it MUST appear here (the linter
  // flags a healed-door note that has no matching `uncaptured` entry).
  "uncaptured": [
    "F1 plan excluded 8 furniture symbols + 2 paving fills",
    "healed door opening at x≈7.5 on south wall S101"
  ],

  // ===== self check =====
  "self_check": {
    "all_dimensions_transcribed": true,     // are all dimension-chain numbers transcribed
    "all_visible_strokes_captured": true,   // did all visible strokes go into the strokes array
    "no_topology_inferred": true,           // did you resist grouping rooms / judging inside-outside / pairing parent-child
    "pens_used": ["wall"],                  // pen values actually used in this image (deduped)
    "unknowns_noted": [
      "wall thickness not dimensioned -> strokes[*].thickness_m = null"
    ]
  }
}
```

---

## 2.1 Door-healing guardrails

In EP a wall is a continuous boundary face, a window is a sub-face on a wall, and a door is ignored
outright in energy simulation. So a "wall with a door" is, in its simulation reality, **one
continuous wall**. The reading stage can see the door arc / leaf at a glance; the correction stage only has coordinates and
cannot reliably tell apart "door / real opening / two independent walls" — so by the error-budget
principle, healing the door belongs to the reading stage. Effect: the correction stage always receives a clean, closed wall
network (one uniform, image-free, validated regime).

**Healing ≠ assigning rooms**: the reading stage only guarantees the wall network is geometrically continuous
and closed; which walls enclose which room / inside vs outside / naming is still the correction stage's job
(§3 red line).

Guardrails (to stop the reading stage inventing walls):

1. **Only heal openings carrying a door symbol (door leaf / swing arc)** — the door symbol is the trigger
2. **Do not heal a doorless large opening / open span** — that is a real topology signal
   (possibly the same zone / a genuinely open boundary); welding it shut destroys information
   the correction stage needs. A gap alone, with no door symbol, does not count
3. **Do not heal windows** — keep them as a window pen (a window is a sub-face, not a boundary break)
4. **Always leave a trace when healing**: write `healed door opening at <position>` in that wall
   stroke's note, and record it in the top-level `uncaptured` list, so SVG review can verify
   "the heal is correct, no real opening was covered up" (the linter flags a healed-door note with
   no matching `uncaptured` entry)

### 2.1.1 Door z chains on an elevation: transcribe the bottom segment verbatim

A door assembly's vertical chain often reads e.g. `1900 / 2400 / 200`, where the trailing `200` means
the assembly bottom is drawn **200 mm above the ground line** (a threshold / step, or the door leaf
starting above a plinth). Transcribe it as drawn and let it place the assembly where the drawing puts
it. **Do not "correct" a non-zero bottom segment to 0** on the assumption every door starts at the
floor — that silently moves the whole assembly down and, unlike a wrong number, leaves a chain that
still closes. If the non-zero bottom looks like a drawing error rather than a real step, say so in the
`note`; do not resolve it here.

---

## 3. Visual recognition vs spatial topology (the red line)

Judging wall vs window vs wall_fill vs non-structural clutter is **visual recognition** (the strokes look different) —
the reading stage's domain (use [`reading_guide.md`](reading_guide.md) to recognize the element,
then [`pen_library.md`](pen_library.md) to map the category to a pen).

Judging wall ext vs int / which wall a window belongs to / which walls enclose which room / which
floor a wall_fill maps to ←—— these are **spatial topology** judgments, all left to the correction stage.

The reading stage must resist the second category even when it "looks obvious". The error budget only works if
the reading stage stays purely perceptual.

---

## 4. Elevation orientation (image-local)

An elevation is read in its own image frame. Record orientation in the image-local `facade` block —
NOT a world-axis claim:

- `view_facade` — which facade this view shows (North/South/East/West), from the image label/metadata.
- Local-x is fixed: every elevation uses the image's left edge as `x = 0`,
  increasing to the right. This is a purely in-image convention, **never**
  "east"/"west". Do not emit `facade.local_x_positive`; it is not a reader
  declaration and the judge does not use it.
- `mirrored` — `true` | `false` | `unknown`.
- `orientation_evidence` — where the facade identity came from (`image_name` / `ocr_label` / `metadata`).

Do **not** state which world axis the facade maps to, or its sign / base elevation — that is world
placement, which the correction stage derives from the plan footprint + this facade block (the
per-facade world-axis convention lives in 1_correction `A1 §2.2`, not here).

Elevation window strokes use `geometry.kind="rect"` + `x_range_m` / `y_range_m` (this image's local
coordinates); the correction stage maps them into the world frame.

---

## 5. Counter-examples (recognition discipline)

- ❌ `"pen": "wall", "is_exterior": true` —— is_exterior is the correction stage's call, do not add the field
- ❌ stuffing a room polygon into strokes —— a room is not a drawn stroke
- ❌ `"pen": "wall", "parent_window_ids": [...]` —— parent-child is the correction stage's inference
- ❌ splitting one continuous wall into 10 small strokes —— one stroke per continuous stroke
  (e.g. the south perimeter from (0,0) to (15,0) is **ONE** wall stroke, not 3; a window or door on it
  is a sub-face/heal, not a break)
- ❌ splitting a wall with a door into two wall strokes on either side —— heal it into one continuous wall + note (§2.1)
- ❌ welding a doorless open span into a continuous wall —— that is a real topology signal; only heal openings with a door symbol
- ❌ tracing a dimension-chain cumulative position or tick drawn outside the building outline as an interior `wall`
  —— dimension annotations are not walls; they go to `dimensions[]`
- ❌ turning a **window-hole edge** (the short jamb line bounding a window opening) into an interior `wall`
  —— the window is a sub-face, its opening edges are not partitions; over-segmentation here is the #1
  reading failure on cluttered plans (do not manufacture partitions from window jambs / dimension ticks / furniture)
- ❌ leaving the top-level `uncaptured` empty when furniture was excluded / a door was healed —— actively excluded items + heals must be acknowledged there (a clean drawing with nothing skipped may be [])
- ❌ `"text": "办公室"` for an image that says "Office 101" —— OCR does not translate
- ❌ `"thickness_m": 0.20` —— plan walls always null (simulation does not need wall thickness, see §0.2)

(Pen-vocabulary counter-examples — wrong pen for the image kind, inventing pen values, etc. — are in
[`pen_library.md`](pen_library.md).)

---

## 6. Self-check list

- [ ] picked the right pen dictionary by image_kind (see [`pen_library.md`](pen_library.md): plan vs elevation differ)
- [ ] every visible wall/window/wall_fill stroke is in the strokes array with the right pen field
- [ ] every wall/window/wall_fill/outline stroke carries `provenance` + `confidence`; `dimension_derived`
      strokes have non-empty `dimension_refs`; estimated/unknown strokes are marked honestly
- [ ] no dimension-chain cumulative position / tick / extension line was emitted as a wall stroke
      or window stroke; dimension ticks outside the building outline are not partitions
- [ ] elevation wall bodies split as "one wall_fill per floor" (see pen library)
- [ ] no rooms[] / is_exterior / parent relations or other topology fields
- [ ] no standalone door / stair / furniture / decoration strokes — recognized and logged (doors trigger healing), never traced as a pen (there is no `other` pen)
- [ ] door openings healed into continuous walls (only openings with a door symbol; doorless open spans kept), wall stroke note says `healed door opening at ...`
- [ ] every dimension-chain number is in the dimensions array
- [ ] text labels transcribed verbatim
- [ ] not-found fields filled with null
- [ ] if this plan declares `scale_origin`, it is a per-image reference only (not guessed, no
      cross-floor/wall-thickness reasoning); `world_z_m` = null. Fine to omit (`null`) when not
      cheaply confident — omitting it is not itself a self-check failure, and guessing a number you
      are not confident of is worse than leaving it out. It is not free either, though: a plan with
      no usable `scale_origin` is recorded as a failing check (`reading.plan_scale_origin_usable`) —
      non-blocking on `exploratory`/`dev` runs, but blocking on `golden`/`regression` acceptance runs
      — and in scored (v3 typed) grading it makes this view's entire plan channel (every wall segment
      and opening on it) score as a miss, on any run profile. Omit it only when you are genuinely not
      confident; do not guess just to dodge that cost.
- [ ] elevation `facade` block filled image-local (view_facade + mirrored + orientation_evidence); no world axis / sign here, and no `local_x_positive` (§4: local-x is fixed by convention, not a reader declaration)
- [ ] elevation outline: not drawn separately if it coincides with wall_fill edges (see pen library); confirmed for this image
- [ ] self_check.pens_used lists the pen set used in this image
- [ ] top-level `uncaptured` records everything "seen but not drawn" — out-of-dictionary strokes + actively excluded clutter + healed doors (a genuinely clean drawing may be [], but any heal / exclusion must appear)

---

## 7. Contract with downstream

The correction stage receives a set of these JSONs (one per image) + testdata_prompt.json + skill rule docs, and
rebuilds topology:
- recognize closed regions enclosed by multiple wall strokes as rooms / zones
- judge each wall's is_exterior (whether it sits on the perimeter)
- map each window stroke to its parent wall
- translate elevation strokes into world coordinates using the image-local `facade` block + the plan
  footprint (the per-facade world-axis convention lives in 1_correction `A1 §2.2`), cross-check plan ↔ elevation consistency
- output the IntakeOutput Pydantic

the reading stage's output is not IntakeOutput, and **should not align directly with IntakeOutput fields**.
the reading stage's product is just "the image, re-traced".
