# Reading-stage pen library (0_reading) — what to do with a recognized element

This maps a **semantic category** (recognized via [`reading_guide.md`](reading_guide.md))
to a **phase-1 action**: trace it with a pen, route it to dimensions/text, ignore-and-log it, or
trigger healing. It does *not* describe what elements look like (that is the reading guide); it only
says what to do once you know what something is.

The output container and the healing/discipline rules are in [`guide.md`](guide.md).

**Pick the action set by `image_kind`** — plan and elevation use different pen sets. Do not
cross-use.

---

## 1. Category → action map

| category | plan action | elevation action |
|---|---|---|
| `wall` | `wall` pen (one stroke per continuous wall; `thickness_m`=null) | part of `wall_fill` (see §3) |
| `column` | recognize → log (not a zone boundary; not traced) | same; if embedded in a wall it is part of that `wall_fill` |
| `window` | `window` pen — record the window's position; which wall it belongs to is the correction stage's call | `window` pen — **the authoritative source of window z**; read each facade × floor's window chain independently, do not copy a "typical floor" |
| `door` | **not drawn** → trigger wall-healing ([`guide.md` §2.1](guide.md)) | not drawn (note only) |
| `stair` | recognize → **do not trace treads**; log treads in `uncaptured`; the stairwell is defined by its bounding `wall`s; any `楼梯`/stair label → `ocr_texts[]` | same |
| `vertical-circ` | recognize → log (region tag; not traced); shaft defined by bounding `wall`s | same |
| `wall_fill` | — (plan has no wall_fill) | `wall_fill` pen, **one per floor** (§3) |
| `outline` | — | `outline` pen **only when it adds z that wall_fill + levels don't**: no fill is drawn (outline = the wall extent), or a parapet/setback top; otherwise redundant → log |
| `dimension-chain` | → `dimensions[]` (verbatim number) | → `dimensions[]` |
| `level-marker` | → `dimensions[]` (z) if it carries a height | → `dimensions[]` (z) |
| `text-label` | → `ocr_texts[]` (verbatim) | → `ocr_texts[]` |
| `scale` | not a stroke; note in `reading_summary.md` | same |
| `grid-axis` | not geometry; ignore → log | same |
| `beam` / overhead hidden line | recognize → log (not a plan/elevation keep pen; do not trace hidden/above-cut lines as walls) | same |
| `north-arrow` | not geometry; ignore → log | — |
| `view-marker` | not geometry (it points at another drawing); ignore → log | ignore → log |
| `legend-titleblock` | not geometry; ignore → log (use legend to interpret hatches) | same |
| `material-hatch` | not a separate stroke; it informs wall-vs-paving classification; log | same |
| `furniture` | **ignore → log** in top-level `uncaptured` | same |
| `sanitary` | **ignore → log** | same |
| `equipment` | **ignore → log** | same |
| `landscape-paving` | **ignore → log** | same |
| `vehicle-figure` | **ignore → log** | same |
| `shadow` | **ignore → log** | same |
| `decoration` | **ignore → log** | same |
| `unknown` | best-guess a real pen (wall/window) with a low-confidence note **only if clearly geometric**; otherwise ignore → log | same (wall_fill/outline/window) |

**Keep-set vs ignore-set**: the keep-set (what the correction stage turns into geometry/coordinates) is just
walls, windows, wall_fill, outline, dimensions, levels, and text. Everything else is the ignore-set —
**recognized, then logged, never silently dropped** ([`guide.md` §2 self_check](guide.md)): the
clutter (furniture / sanitary / … / decoration) **and** `stair` / `column` / `vertical-circ` /
grid-axis / north-arrow / view-marker / legend, whose geometry the correction stage does not consume (a stairwell
reaches the correction stage as its bounding walls + a label, not as traced treads). `door` is the special case:
recognized but not drawn, it triggers healing instead.

For elevation windows, record the visible chain on each facade and each floor independently. Do not
reuse a typical-floor chain from another floor/facade just because the drawing looks repetitive.
Stage-1 companion TODO: correction should later run count/blank cross-checks across facade × floor.

For every plan, calibration also has an optional container action: write `scale_origin` in the
top-level reading product when it is cheap and confident. `scale_origin.world_x_m/world_y_m` record
the metre offset of plan-local `(0,0)` from an easily-seen reference point drawn in THIS SAME image
(e.g. the drawing's own outer corner) — never the result of reasoning about another floor's footprint
or about wall thickness. Use `0.00/0.00` when plan-local `(0,0)` already IS that reference point, keep
plan `world_z_m` null, and never guess the field — omit it (`null`) instead. See `guide.md` §1/§2.

---

## 2. Legal pen values

- **plan**: `wall` · `window`
- **elevation**: `wall_fill` · `window` · `outline`
- **section / supplementary / other**: no fixed pen set yet; keep the image-local trace only when it
  clearly uses the same structural pens, explain axes/scope in notes, and log uncertain marks

That is the whole set — kept minimal to exactly what the correction stage turns into geometry. Anything that is
not one of these (column, beam / overhead hidden line, stair, grid line, north arrow, decoration, furniture, …) is **not traced
as a stroke**: recognize it and record it in the top-level `uncaptured` list. There is no `other` pen
and no `door` pen.

---

## 3. Elevation wall_fill convention (one per floor)

Record the elevation wall-body fill as **one `wall_fill` stroke per floor**:

- a 3-story facade → 3 `wall_fill` strokes, each covering one floor's fill rectangle (split by y/z range)
- even if the fill looks like one continuous block, split it per floor by the level dimension chain
- if a floor's fill is **completely broken** by an opening (white gap around the frame), record each
  broken segment as its own stroke; when windows merely overlay the fill (no break), keep one fill
  per floor

Per-floor `wall_fill` lets the correction stage map directly to each floor's wall surface z_floor / z_top.

---

## 4. Pen-vocabulary counter-examples

- ❌ `"pen": "wall_fill"` on a plan — wall_fill is elevation-only
- ❌ `"pen": "wall"` on an elevation — elevation walls use wall_fill; `wall` is plan-only
- ❌ one wall_fill for an entire elevation wall — should be one per floor (§3)
- ❌ inventing a pen value like `"furniture"` / `"cornice"` / `"column"` / `"stair"` — there is no
  catch-all pen; recognize it and ignore → log
- ❌ tracing stair treads as `wall` (or any pen) — recognize the stair, but treads are not geometry;
  log them and let the bounding walls define the stairwell
- ❌ `"pen": "door"` — door is never a pen; it triggers wall-healing ([`guide.md` §2.1](guide.md))
- ❌ tracing an elevation floor-height door as `"pen": "window"` because it breaks `wall_fill` — a
  door is recognized and logged, never emitted with the window pen, even when it is a real opening
