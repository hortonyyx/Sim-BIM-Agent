# Reading-stage recognition guide (0_reading) — how to recognize what a drawing element *is*

This is the **recognition reference** for the reading stage. It answers one question only: *given this stroke /
mark on the drawing, what is it?* It outputs a **semantic-category label** (`wall`, `door`,
`window`, `furniture`, …). It does **not** decide what to do with that label — which pen to use,
whether to keep or ignore, whether to heal — that is [`pen_library.md`](pen_library.md).
The flow, container, and discipline are in [`guide.md`](guide.md).

```
this guide:        a mark  →(recognize)→  category = "door"
pen library:       category = "door"  →(map)→  action = "don't draw, trigger healing"
```

The category label is the **handoff token** between the two docs (the vocabulary is in §0.3).

---

## 0. How to use this guide

### 0.1 Recognize the stable cue, not one fixed style (important)

Architectural drawings vary enormously — hand-drawn vs CAD, filled vs outline, schematic vs
construction-detail, Chinese GB / US / ISO conventions, scanned/noisy vs clean vector. **Do not
memorize one rendering as "the" way a thing is drawn.** For every element below, anchor on the
**invariant recognition cue** (the property that stays true across styles) and treat the listed
visual variants as *examples, not an exhaustive list*.

- e.g. a door's invariant cue = "an opening in a wall + an indicator of how it operates (swing /
  slide / fold)". The swing arc, the leaf line, the sliding rectangles are all just *variants* of
  that indicator.
- When a mark roughly matches a category's invariant cue but is drawn in a style you haven't seen,
  **prefer the category match** over rejecting it.

### 0.2 When unsure, recognize-enough-to-log (never silently drop)

If a mark does not clearly fit any category, do not guess it into the wrong one. Per the error
budget ([`guide.md` §0.1](guide.md)), a wrong category is contamination the correction stage cannot
undo. Instead:
- give your best category guess **with low confidence in the note**, or
- label it `unknown` and record it in the top-level `uncaptured` list

"Acknowledged unknown" is recoverable at review; a confident miscategorization is not.

### 0.3 Semantic-category vocabulary (the handoff enum)

The reading guide may distinguish finer subtypes for robustness (e.g. swing vs sliding door), but it
hands the pen library a **coarse category** from this fixed set:

The second column is the **identity** (what the thing is) — the handoff label only. What to *do*
with each (which pen / keep / ignore / heal / route to dimensions[] / ocr_texts[]) lives entirely in
[`pen_library.md` §1](pen_library.md); it is deliberately not stated here.

| category | what it is (identity) |
|---|---|
| `wall` | a room-bounding structural line |
| `column` | an isolated structural post |
| `beam` | a hidden/above-cut overhead structural line, not a room boundary |
| `window` | a glazed opening in a wall |
| `door` | a wall opening with an operating leaf |
| `stair` | a stepped vertical-circulation run |
| `vertical-circ` | an elevator / ramp / escalator |
| `wall_fill` | an elevation wall-body area (per floor) |
| `outline` | an elevation overall silhouette |
| `dimension-chain` | a measured span carrying a number |
| `level-marker` | an elevation/height value marker |
| `scale` | a drawing scale ratio / graphic scale bar |
| `grid-axis` | a structural grid line + labelled bubble |
| `north-arrow` | a plan orientation marker |
| `view-marker` | a section-cut / detail / elevation-index marker |
| `text-label` | drawing text (room name / note / leader) |
| `legend-titleblock` | a legend table / title block |
| `material-hatch` | a material fill pattern / poché |
| `furniture` | a movable furnishing |
| `sanitary` | a plumbing fixture (toilet / basin / tub) |
| `equipment` | a kitchen / mechanical fixture |
| `landscape-paving` | greenery / paving texture |
| `vehicle-figure` | a car / scale human figure |
| `shadow` | an elevation shading tone |
| `decoration` | a non-structural facade detail / attachment (moulding, cornice, band, balcony, sun-shade, railing, canopy) |
| `unknown` | could not classify |

_Example image crops for the cards below are pending the v2 corpus (the `example image` field)._

---

## A. Drawing-type identification

Decide *what kind of drawing* first — the element dictionaries differ by type. **Plan** and
**elevation** are the primary traced views. `section`, `supplementary`, and `other` are allowed
image kinds for ancillary sources; recognize their content image-locally and explain scope/axes in
notes instead of forcing them into a plan/elevation interpretation.

- **Plan** (floor plan): a horizontal cut viewed from above (cut roughly at window height). Cues:
  rooms enclosed by walls, a roughly rectangular/orthogonal layout, in-plane dimension chains on the
  outside, room-name text inside, a north arrow, door swings. Coordinates (image-local): x =
  horizontal (drawing right), y = vertical (drawing up).
- **Elevation**: an orthographic view of one exterior face. Cues: a flat outer silhouette, floor
  lines stacked vertically, windows as a regular grid of rectangles, height/level markers up the
  side, ground line at the bottom, no room interiors. Coordinates (image-local): x = horizontal
  along the facade, y = height (up); orientation is image-local — see `guide.md` §4 (no world axis here).
- **Axonometric / perspective / 3D render**: recognize it as a **non-orthographic view** (out of
  scope — the reading stage only traces orthographic plans and elevations; what to do with it is the pen
  library's call).
- If a sheet has several drawings, segment by title text / frame and treat each as its own image.

---

## B. Line types and weights (the underlying grammar)

Line weight and style carry meaning before you even name an element. These are *tendencies*, not
guarantees (many drawings have no weight differentiation at all):

- **Heavy / thick lines** → things cut by the section plane: walls, columns. (In plan, the wall
  outline is usually the heaviest.)
- **Medium lines** → visible edges not cut (e.g. stair treads, fixtures).
- **Thin lines** → dimensions, extension lines, hatching, leaders, text.
- **Line styles**: solid = visible/cut; **dashed** = hidden or above the cut plane (e.g. an overhead
  beam, upper-floor outline); **dash-dot (center line)** = grid axes / centerlines; **double-dash-dot**
  = secondary; **freehand zig-zag / "S" break** = break line (drawing truncated here).
- **Color** (when present) is a weak extra cue, not reliable: e.g. blue often = glazing/water, green
  = landscape — but never classify on color alone.

### Dashed lines, hidden observations, and windows

`line_style` and `visibility` record what is observed in this image; they do not assert topology or
world geometry. Use `line_style="solid"` and `visibility="visible"` for a visible, solid-line window
observation (either value may be left unset when the drawing makes it unambiguous). Use
`line_style="dashed"` and `visibility="hidden"` for a dashed window-like line, hidden feature, or
projection above the cut plane. A hidden observation is **not** an entity window for downstream use:
record the marked stroke faithfully, or add an `uncaptured` entry with `source_id`,
`kind: "hidden_window_candidate"`, and `reason` for conflict/audit.

Apply these negative-case guardrails independently:

1. A solid visible window is a normal visible-window observation.
2. A dashed hidden window or upper-level projection stays hidden; do not make it an entity window.
3. Never silently coerce a dashed line into `line_style="solid"`.
4. At the same location, a solid line and a dashed line are two independent strokes with unique ids:
   the solid observation is visible and the dashed observation is hidden. Do not merge them or let one
   consume the other.

---

## C. Style and medium variation (read this as a lens over every card)

The same element looks different across:

- **Medium**: hand-drawn (irregular lines, sketchy arcs) vs CAD vector (crisp, exact) vs scanned
  (speckle, broken lines, skew) vs photo of a drawing (perspective distortion, glare).
- **Fill convention**: outline-only (hollow double lines) vs solid poché (filled black) vs hatched
  (pattern fill) — all can mean the same wall.
- **Detail level**: schematic/diagram (single-line walls, no dimensions) vs design drawing vs
  full construction detail (every layer, every callout).
- **Standard**: Chinese GB, US (AIA), ISO — symbols for the same thing differ (e.g. dimension
  terminators are 45° ticks in GB/architectural practice, arrowheads in mechanical/US practice).

Consequence for recognition: **lead with the invariant cue (what the element *does* / where it
sits), let visual style be secondary.** A wall is "a long boundary line bounding rooms" whether it is
one thick line, two thin lines, or a black bar.

---

## D. Structural & circulation elements

### Card · `wall`
- **Appears in**: P (and E as `wall_fill`, see §E)
- **Stable cue**: a long, straight-ish boundary line that, together with others, encloses rooms;
  drawn with the heaviest weight; meets other walls at corners to close a region.
- **Variants (non-exhaustive)**: single thick black line · two thin parallel lines (hollow) · two lines with
  solid-black poché fill · two lines with hatch/material fill · schematic single line. Curved/angled
  walls appear as arcs/polylines.
- **Confusable with**: vs furniture/cabinet outline (thinner, small rectangular blocks with internal
  subdivision, not forming room boundaries) · vs dimension extension lines (thin, outside the plan,
  don't close) · vs grid axis (dash-dot, passes *through* the wall center and extends past it).
- **Positive test**: an interior wall bounds rooms and joins the perimeter / corridor wall network.
  Cumulative dimension positions or ticks outside the outline do not bound rooms and do not become walls.

#### Plan outer envelope: non-rectangular polyline

- **Stable cue**: the complete ordered outer ring of a non-rectangular plan. The decisive cue is a
  **concave corner** (a reflex vertex): rectangles have none, while L- and U-shaped plans do. Preserve
  every such vertex rather than flattening the envelope into a rectangle.
- **Representation**: use the existing `wall` strokes as connected ring segments, or use one `wall`
  stroke with `geometry.kind="polyline"` and `points=[[x, y], ...]`. Both are existing reading-schema
  forms. The polyline/ring must include each concave vertex truthfully.
- **Scope**: this records the overall envelope; it does not replace tracing individual room-bounding
  walls.

#### Wing division: explicit evidence only

- **Stable cue**: an explicit drawing annotation that divides the two wings where the legs of an L- or
  U-shaped plan meet: a labelled division, dimension, or axis line.
- **Representation**: only when that annotation is present, record it with an existing `dimension`,
  `grid-axis`-style stroke, or note. Do not invent a wing division from geometry alone; the concave
  envelope already implies it. An explicit wing annotation may later corroborate or correct an elevation.

### Card · `column`
- **Appears in**: P (and E occasionally)
- **Stable cue**: an isolated small solid/filled shape (square, rectangle, circle) at a structural
  grid intersection, heavier than surrounding lines, repeated on a grid.
- **Variants**: solid black square · hatched square · circle (round column) · square with an inscribed
  circle.
- **Confusable with**: vs a wall junction (a column stands alone at a grid node) · vs furniture (column sits
  on grid axes and repeats regularly).

### Card · `window`
- **Appears in**: P and E
- **Stable cue (plan)**: a break in the wall line spanned by **thin parallel line(s)** running along
  the wall (the glazing), with **no swing arc**. **(elevation)**: a rectangle on the facade, often in
  a regular repeating grid, often with internal mullion divisions.
- **Variants**: plan — 1 / 2 / 3 thin parallel lines (single/double/triple glazing convention) · bay
  window projecting outward. Elevation — plain rectangle · rectangle with mullions/grid · with an
  opening-direction symbol (dashed triangle) · tinted/blue fill.
- **Confusable with**: vs door (door has a swing arc or sliding rectangles; window has parallel glazing lines
  and no operation arc) · vs plain opening (opening has nothing spanning it).

### Card · `door`
- **Appears in**: P (and E)
- **Stable cue**: an opening in a wall + an **operation indicator** (how the leaf moves). The opening
  + indicator together is the invariant — the indicator's exact shape varies.
- **Variants (non-exhaustive)**: single swing = a leaf line + a 90° arc · double swing = two leaves + two arcs ·
  sliding = parallel rectangles in/over the opening, no arc · folding = zig-zag leaves · revolving =
  circle with cross · pocket = dashed leaf into wall. In elevation: a rectangle (often floor-height),
  sometimes with a dashed swing-direction "V".
- **Confusable with**: vs window (window = parallel glazing lines, no operation arc) · vs a plain doorless
  opening / open span (no leaf, no arc — a real gap, **must not be treated as a door**; see the
  healing guardrails in [`guide.md` §2.1](guide.md)) · vs a stair/handrail arc.

### Card · `stair`
- **Appears in**: P (and E/section)
- **Stable cue (plan)**: a run of evenly spaced **parallel lines (treads)** within a shaft, with an
  **up/down direction arrow** (often labeled UP / DN / 上 / 下) and frequently a diagonal **break
  line** cutting the run.
- **Variants**: straight run · L-shaped / U-shaped with a landing · spiral (treads radiating from a
  center) · with/without a center handrail line.
- **Confusable with**: vs hatching/paving (stairs have the direction arrow + are bounded by a shaft) · vs a
  ramp (ramp has a slope arrow + few/no treads) · **vs wall** (tread lines can resemble parallel wall
  lines / hatching — the direction arrow + shaft disambiguate; recognizing the stair is what keeps
  its treads from being traced as walls).

### Card · `vertical-circ` (elevator / ramp / escalator)
- **Appears in**: P
- **Stable cue**: a bounded shaft with a characteristic internal symbol — elevator = box with an "X"
  diagonal cross and a door gap; ramp = a long strip with a slope/direction arrow and a slope ratio
  label; escalator = parallel treads with a long direction arrow spanning two levels.
- **Confusable with**: elevator-X vs a column-with-inscribed-shape (elevator is a room-sized shaft, not a
  point) · ramp vs stair (ramp = slope label, sparse treads).

---

## E. Elevation-specific elements

An elevation carries **z (height)** information; the plan carries x/y. The two views are
complementary, but *combining* them (matching a plan edge to an elevation) is the correction stage's job — here,
just read each elevation on its own.

### E.0 How to read an elevation (suggested order)

1. **Outline** — the outer silhouette (top = roof/parapet, sides, bottom = ground line). Sets the
   overall z extent.
2. **Storey / floor lines** — the horizontal lines (or the level dimension chain) splitting the
   facade into floors. They delimit the per-floor `wall_fill` and give each floor's z band.
3. **Per-floor `wall_fill`** — one fill stroke per floor, bounded by those storey lines.
4. **Window grid + openings** — windows usually repeat as a regular grid; record each as a rect
   (x_range along the facade, y_range = its z band). Read each facade × floor's window chain
   independently; do **not** copy a "typical floor" pattern from another facade or floor. An opening
   that fully breaks the wall body splits that floor's `wall_fill`.
5. **Level / height markers** — ▽ ±0.000 etc.: the authoritative z values; transcribe verbatim.
6. **Attachments** (balcony / sun-shade / railing / canopy) — recognize them so they are **not
   mistaken for windows or storey lines**; they are non-structural (see the `decoration` card, §H).

Stage-1 companion TODO: correction should later cross-check per-facade/per-floor window counts and
blank floors, but the reading stage's job is only to record each visible chain independently.

### Card · `wall_fill`
- **Appears in**: E
- **Stable cue**: the wall-body area of the facade between two storey/floor lines — the "solid wall"
  as opposed to windows/openings. **Find the storey/floor lines (or the level dimension chain)
  first**, then read **one fill per floor** delimited by them.
- **Variants**: light-gray solid fill · hatched fill · or merely the area enclosed by the outline
  minus the openings (no explicit fill at all — infer it from outline + floor lines + openings).
- **Confusable with**: vs shadow (offset, one-sided, often outside the outline) · vs a material-hatch
  band (a material callout, not the whole wall body) · vs a decorative banding line (a `decoration`
  line, not a fill boundary).

### Card · `outline`
- **Appears in**: E
- **Stable cue**: the overall heavy silhouette of the facade. Its three meaningful edges: **top**
  (roof / parapet / eave — sets top z), **sides**, and **bottom** (ground / grade line — the z base,
  usually the ±0.000 datum).
- **Variants**: a separate heavy frame · or simply the outer edges of the wall_fill (no separate
  line). Top edge varies: flat roof with a parapet (horizontal top) · pitched/sloped roof (gable
  triangle or slope) · stepped/setback top. Bottom may be a distinct grade line with hatching below.
- **Confusable with**: only record `outline` as its own element if it is drawn *separately* from the
  wall_fill edges; otherwise it coincides and is not a distinct stroke. A pitched roofline is part of
  the outline top, **not** a `decoration` line.

---

## F. Annotation and reference symbols

### Card · `dimension-chain`
- **Appears in**: P and E
- **Stable cue**: a measurement line carrying a **number**, bounded by two **terminators**, with thin
  **extension lines** linking it back to the measured feature. The "extension–terminator–number–
  terminator–extension" group is one unit.
- **Variants**: terminators = 45° ticks (architectural/GB) or arrowheads (mechanical/US) or dots ·
  chained (segment dimensions in a row) vs overall (one long total) vs baseline (all from one datum) ·
  stacked in multiple rows (detail / axis / overall).
- **Confusable with**: vs grid axis (axis is dash-dot with a bubble, no number-between-ticks) · vs a leader
  note (leader points at one feature with text, not a measured span). Transcribe the **number
  verbatim**; watch units (mm vs m).

### Card · `level-marker` (elevation / height)
- **Appears in**: E (and sometimes P for floor levels)
- **Stable cue**: a height value attached to a **filled/hollow triangle** (▽) or a level symbol,
  marking a z height; ±0.000 = the datum.
- **Variants**: triangle pointing to a line · a circle/flag with a number · relative (±0.000, 3.600) vs
  absolute (site elevation).
- **Confusable with**: vs north arrow (north arrow has an N / points up-ish, no height number) · vs a
  dimension number (level has the triangle/datum symbol).

### Card · `scale`
- **Appears in**: P and E (usually near the title)
- **Stable cue**: a ratio text like `1:100` / `1:50`, or a **graphic scale bar** (a ruler with marked
  intervals).
- **Confusable with**: vs a dimension number (scale is a ratio `1:N` or a labeled bar).

### Card · `grid-axis`
- **Appears in**: P (and E)
- **Stable cue**: a **dash-dot center line** extending beyond the building, terminating in a **bubble**
  (circle) containing an axis label (numbers one way, letters the other, by convention).
- **Confusable with**: vs a wall (axis is dash-dot, thin, runs through centers and extends past edges) · vs a
  dimension line (axis ends in a labeled bubble, not ticks+number).

### Card · `north-arrow`
- **Appears in**: P
- **Stable cue**: an arrow/compass symbol marked **N / 北**, indicating plan orientation.
- **Variants**: simple arrow · compass rose · wind rose (combined with a wind-frequency diagram).
- **Confusable with**: vs a direction arrow on a stair/ramp (those sit inside a shaft and say UP/DN/slope).

### Card · `view-marker` (section-cut / detail-callout / elevation-index)
- **Appears in**: P
- **Stable cue**: a marker pointing into a region with an **identifier** and (for sections) a **sight
  direction**: a cut line with flags + number, or a circle split into detail-number / sheet-number,
  or an elevation triangle/arrow with a label.
- **Confusable with**: vs grid bubble (callout references another drawing; grid labels an axis). These mark
  *where another drawing is taken* — **recognize so as not to mistake the cut line for geometry**;
  they carry no wall/window geometry themselves.

### Card · `text-label`
- **Appears in**: P and E
- **Stable cue**: any text — room names/numbers, component annotations on **leader lines**, drawing
  title, general notes.
- **Confusable with**: distinguish a room *name* (inside a room) from a dimension *number* (on a dimension
  line) from a level *value* (on a triangle). Transcribe **verbatim, do not translate**.

### Card · `legend-titleblock`
- **Appears in**: P and E (edges of the sheet)
- **Stable cue**: a bordered table — a **legend** (hatch/symbol → meaning mapping) or the **title
  block** (project / sheet / scale / date in the corner frame).
- **Confusable with**: vs a real room (title block sits in the sheet frame, contains metadata text in a
  grid). Use the legend to *interpret* hatches; it is not building geometry.

---

## G. Material and fill

### Card · `material-hatch`
- **Appears in**: P and E
- **Stable cue**: a repeating pattern filling an area to denote a material.
- **Variants (per legend; non-exhaustive)**: reinforced concrete = diagonal lines + dots · brick / masonry
  = 45° hatch · insulation = wavy or cross-hatch · earth/compacted = specific GB symbol · sand /
  mortar = stipple · metal · wood (grain) · glass · water (horizontal lines). The drawing's own
  **legend** is the authority — read it rather than assuming.
- **Confusable with**: a hatch *inside the wall double-lines* = wall material (the wall itself) vs a hatch on
  the **floor area** = paving/finish (clutter, see §H). The location decides, not the pattern.

---

## H. Non-structural elements

Recognizing these matters so they are **not mistaken for structural elements** (a furniture outline
read as a wall is the worst error). Identify the category honestly; what to do with each is the pen
library's call.

### Card · `furniture`
- **Stable cue**: thin-line outlines of movable items (desks, chairs, beds, sofas), often grouped,
  internally subdivided, smaller than a room.
- **Confusable with**: built-in cabinets / bay windows can look like furniture — check whether it is flush
  with and continuous with the wall line (then it may be window/wall, not furniture).

### Card · `sanitary`
- **Stable cue**: fixed plumbing fixture symbols — toilet, basin, bathtub, shower — standardized
  rounded shapes, usually against a wall in small rooms.

### Card · `equipment`
- **Stable cue**: kitchen/mechanical fixture symbols (stove, sink, HVAC units, electrical symbols) —
  schematic icons, often with a legend entry.

### Card · `landscape-paving`
- **Stable cue**: greenery (tree circles, plant symbols), paving texture (tiling grid, stippling) over
  outdoor/floor areas — texture covering a *region*, not a bounding line.
- **Confusable with**: paving texture vs material-hatch on a wall — paving fills floor area between/outside
  walls; wall material sits within the wall double-lines.

### Card · `vehicle-figure`
- **Stable cue**: car outlines (in parking/site context) and scale human figures (for size reference).

### Card · `shadow`
- **Appears in**: E
- **Stable cue**: a tone/fill offset to one side of the building/openings, suggesting depth/light;
  often gray, one-directional, may spill outside the outline.
- **Confusable with**: vs wall_fill (shadow is offset and directional; wall_fill sits within the facade body
  per floor).

### Card · `decoration`
- **Appears in**: E (and P)
- **Stable cue**: non-structural facade lines **and attachments** — mouldings, cornices, string
  courses, facade banding, reveal lines, purely-cosmetic floor-divider lines; on elevations also
  projecting attachments: **balconies, sun-shades / louvres / fins, railings / balustrades, canopies
  / marquees, downpipes**.
- **Confusable with**:
  - a **floor-divider line** marking a real storey boundary is information (it delimits `wall_fill`
    per floor) — note it; a purely decorative band is clutter. When unsure, log it.
  - a **sun-shade / louvre** (slats projecting over a window) vs the **window** itself or a **storey
    line** — the shade projects and has repeated slats; do not merge it into the window rect.
  - a **balcony** vs a **floor slab** or a **window band** — a balcony projects and carries a
    railing; it is not a glazed opening.
  - a **railing / balustrade** vs **window mullions** — a railing sits at a balcony/edge, not inside
    a glazed rectangle.

---

## I. Recognition self-check (perception only)

- [ ] decided the drawing type (plan vs elevation) before applying a dictionary
- [ ] for each kept mark, matched it to a category by its **invariant cue**, not one assumed style
- [ ] resolved the high-confusion pairs deliberately: door vs window vs plain-opening; wall vs
      grid-axis vs dimension-extension; stair vs paving; wall_fill vs shadow; wall-material vs floor-paving
- [ ] on elevations: read outline → storey lines → per-floor wall_fill → window grid → level markers;
      kept attachments (sun-shade / balcony / railing / canopy) out of the window/wall_fill (§E.0, §H)
- [ ] anything not clearly classifiable → labeled `unknown` / low-confidence note, **not** forced
      into a wrong category
- [ ] text transcribed verbatim, not translated
- [ ] did **not** decide actions here (which pen / keep / heal) — that is the pen library's job

> This guide stops at *identity*. What to draw, with which pen, what to ignore, and how to heal a
> door — all live in [`pen_library.md`](pen_library.md) and [`guide.md`](guide.md).
