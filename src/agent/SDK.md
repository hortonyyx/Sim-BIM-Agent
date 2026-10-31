# Python BIM tools: inspect → build and view → review and deliver

Use ordinary Python with `BimSession(run_directory)`. The run already contains
the admitted original inputs. This library starts no model and uses the existing
domain build, review and finish rules. Use one writer per run. Read this entry
first; the tool catalog and long references are for questions that remain.

## 1. Inspect the originals and construct a plan

```python
from src.agent.sdk import BimSession
with BimSession(run_directory) as bim:
    print(bim.image("plan.png"))
    # For textured mesh, use bim.call("view_mesh", ...).
```

Open the returned image paths with your image viewer. Pixel labels refer to the
original image, even when its display is reduced. Keep `view_id` for reference
reviews when required. Read text files by explicit name; do not read an entire
mixed image/text directory as text. Batch independent reads when useful.

Use metres, a shared XY coordinate frame, and Python variables/loops for repeated
floors, rooms and openings. This small synthetic example shows the normal format;
its dimensions and arrangement are not a building prescription:

```python
plan = {
    "templates": {"typical": {
        "footprint": [[0, 0], [12, 0], [12, 8], [0, 8]],
        "spaces": [
            {"id": "office", "role": "office", "rect": [0, 0, 9, 8],
             "source_refs": ["synthetic layout hypothesis"]},
            {"id": "hall", "role": "corridor", "rect": [9, 0, 12, 8],
             "source_refs": ["synthetic layout hypothesis"]}],
        "doors": [{"id": "access", "space": "office", "other_space": "hall",
                   "p1": [9, 3], "p2": [9, 4], "z": [0, 2.1],
                   "source_refs": ["synthetic shared boundary"]}],
        "window_rows": [{"id": "north", "facade": "North", "plane": 8,
                         "spans": [[1, 3], [5, 7]], "z": [1, 2.5],
                         "source_refs": ["synthetic opening observation"]}]}},
    "instances": [{"id": "L1", "template": "typical", "z": 0, "height": 3}],
    "assumptions": ["Synthetic example; actual use must follow input evidence."],
    "unresolved": []
}
```

Construction choices, using the same format:

* `rect` is `[xmin, ymin, xmax, ymax]`; a space can instead use `polygon` (XY
  vertices, no repeated closing point). Spaces must cover the intended footprint
  without unintended overlap. Do not partition a connected open space merely to
  make rectangular records. Internal boundaries and shared identities are derived.
* For rotated/polygonal construction set top-level
  `"schema_version": "source_polygon_v1"`. Define
  `"primitive_frames": {"local": {"origin": [10, 20], "yaw_degrees": 17}}`.
  A space can use `"primitive": {"frame": "local", "rects": [[0,0,6,2],
  [0,2,2,5]]}` instead of `rect`/`polygon`. A footprint can be
  `{"primitive": {...}}`. The rectangles form one connected region; overlaps in
  this explicit union are not extra spaces or walls. For holes/curves, consult the
  parametric reference. Use one transform for all related geometry.
* A door needs `id`, `space`, `other_space` (null for outdoors), `p1`, `p2`, `z`,
  `source_refs`. Its full segment must lie on the actual shared/exterior boundary.
  In a primitive frame, replace `p1`/`p2` with
  `"local_baseline": {"frame": "local", "axis": "u", "fixed": 2,
  "range": [3, 4]}`; axis `v` is also supported. This uses local coordinates.
  Do not independently round or shrink an opening to force a match.
* Template opening heights `z` are relative to the instance base. Instances name
  a template and supply world `z` and `height`. Expanded space IDs are
  `INSTANCE:SPACE`; expanded door IDs are `INSTANCE:door:DOOR`. Cross-floor
  connections use expanded IDs; consult the reference for their exact format.
* `window_rows` above are for cardinal facades. Rotated walls use `opening_rows`
  with `kind: "window"`, `space`, `p1`/`p2` or `local_baseline`, `z`, `id`,
  `source_refs`, and `spans` (metre intervals measured along the baseline from its
  first endpoint). For example, a length-6 baseline with `spans: [[1, 2]]`
  makes one width-1 opening starting 1 metre from that endpoint.
  Keep evidence, assumptions and unresolved questions honest. Preserve useful
  detail already understood; infer necessary functions and circulation from the
  available evidence. Efficiency is not a reason to merge correct known rooms.

For an explicitly open overhead boundary, add the existing enclosure intent to
the plan. For example, if `R:deck` actually exists:

```python
plan["enclosure_declaration"] = {
    "schema_version": "source_enclosure_intent_v1",
    "spaces": [{"space_id": "R:deck", "enclosure": "semi_open",
                "evidence_kind": "inferred", "source_refs": ["actual roof evidence"],
                "assumptions": ["Explain the inference from that evidence."]}],
    "boundaries": [{"boundary": {"space_id": "R:deck", "geometry_type": "ceiling"},
                    "condition": "open", "scope": "whole", "evidence_kind": "inferred",
                    "source_refs": ["actual roof evidence"],
                    "assumptions": ["Explain why the overhead boundary is open."]}]
}
```

This resolves a unique actual ceiling; it does not remove physical side walls.
Unknown enclosure is not open enclosure. Ambiguous boundaries require an actual
boundary ID from the source, not a guessed hash or nearest-wall substitution.

## 2. Save and request the actual views together

```python
with BimSession(run_directory) as bim:
    saved = bim.build(plan, drawings=[{
        "image": "plan.png", "floor_id": "L1",
        "x_anchors": [[pixel_x0, world_x0], [pixel_x1, world_x1]],
        "y_anchors": [[pixel_y0, world_y0], [pixel_y1, world_y1]],
        "basis": "Describe the actual dimensional evidence used for these anchors."
    }]).require_ready()
    print(saved)
```

Each anchor maps an **original image pixel** to world metres, not local-frame
metres. Usually image Y increases downwards, so its world Y anchors decrease.
Calibration is explicit, not automatic. Supply each relevant image/floor once;
these are full-image overlays of the saved source. For mesh, omit `drawings` and
pass the existing `mesh_observations` to `build` when applicable.

The result distinguishes `candidate`, `source_geometry_ready` and
`requested_views_complete`. `require_ready()` checks construction and requested
views only. If a view fails after saving, retain the candidate and retry only
that `overlay_candidate` call. An interrupted write has an unknown outcome:
inspect native receipts before retrying. No wrapper automatically repairs,
reviews, retries or finishes.

Open the returned current source plans/overlays and compare them with all relevant
original evidence. Check dimensions, space relationships, required access,
vertical organization, facade alignment and enclosure as applicable. A clear
machine check does not establish architectural quality. Repair concrete problems
together, then recheck affected relationships. Do not run optional diagnostics
solely to remove uncertainty notes; required quality and evidence remain binding.

## 3. Submit actual judgments and deliver once

```python
with BimSession(run_directory) as bim:
    print(bim.finish_to(new_output_directory, saved.candidate, reviews=[
        {"kind": "plan", "image": "plan.png", "floor_id": "L1",
         "outcome": "consistent", "summary": actual_plan_findings, "issues": []},
        {"kind": "building", "outcome": "consistent",
         "summary": actual_building_findings, "issues": []}
    ]))
```

Only submit `consistent` after actual inspection. Use `uncertain` with concrete
nonempty `issues` for unresolved points; `needs_revision` does not establish new
completed coverage. Review each required image/floor scope; calibration alone
does not establish coverage. Changes to source, original image, calibration or
overlay invalidate affected evidence. When the run enables `drawing_review_policy`,
the response includes the native missing review scopes; an auxiliary image needs
its own `reference` review with `image` and the actual inspected `view_id`. For
mesh, supply the current `mesh_review` with inspected overlay IDs. Check any
reported missing scope; never manufacture a judgment to finish.

`finish_to` calls normal `finish_bim`, verifies its exact completed handoff, and
publishes the same source/viewer bytes to a **new directory outside the run**.
It returns paths for `source_model.json`, `delivery.html`, `publication.json`.
No additional model-authored copy, validator or hash script is needed. Publication
is still distinct from independent architectural acceptance. Admission, rendering
and publication time all belong in end-to-end measurements.

## Only when needed

`reply.data` and the receipt at `reply.receipt_file` retain the full native result;
printing shows current findings and image paths with repeated material condensed.
Image bytes are unchanged. `reply.require_ok()` checks tool errors only.

Use `bim.describe(tool_name)` for an exact argument schema or
`bim.call("get_bim_reference", topic="parametric")` for advanced plan syntax.
`bim.check_plan(plan)` is an optional construction-only check; doing it before
every build compiles twice. [SDK_ADVANCED.md](SDK_ADVANCED.md) covers geometric
helpers and CLI usage. The caller owns filesystem permissions and execution
budgets; this library is not a sandbox. The delivery target can be shorter than
the hard run limit: remaining allowance is not an optional refinement budget.
