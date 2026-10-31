# CV Toolbox For Reading Evidence

Use the CV toolbox before drawing semantic reading JSON for clean vector CAD PNGs. On noisy scans, hand drawings, or other degraded inputs, defer the required/optional judgment until a robustness profile exists. The tools measure image-local candidates only. They do not classify building elements, write reading JSON, or produce final geometry.

## Tools

- `crop_zoom`: crop a half-open source bbox `[x0,y0,x1,y1)` and record the inverse transform chain so crop-local pixels can be restored to the source image.
- `wall_line_profiler`: apply the clean-vector gray mask, project rows or columns, and return wall-line candidates with prominence strength and FWHM width.
- `px_m_calibrator`: convert dimension spans into `px_per_m` with forced-origin least squares and per-anchor residuals when multiple anchors are supplied. When both x and y anchors are present, their independently fitted scales (`axis_px_per_m`) are checked against the measured clean-vector ceiling (0.30% relative deviation); on disagreement the tool still returns a usable blended `px_per_m` rather than raising — see the cross-axis discipline below for what that combination means and what to do about it.
- `window_cc_detector`: label 8-connected clean-vector components, filter by area/shape, merge nearby boxes, and return window-rectangle candidates.
- `storey_line_profiler`: row-projection wrapper for horizontal storey-line candidates.
- `overlay_logger`: draw accepted, rejected, and undecided candidates and preserve each decision reason.

## Invocation Examples

The entry point is `tools/run_cv_probe.py` and the tool name is passed as `--tool`. Output must land
under `out/` — that is the only writable root, so `--out-dir out/cv` (or any `out/<name>`) works and
anything else is refused.

```bash
python tools/run_cv_probe.py --tool wall_line_profiler \
  --image case_data/1f_view.png \
  --out-dir out/cv \
  --axis col
```

```bash
python tools/run_cv_probe.py --tool crop_zoom \
  --image case_data/1f_view.png \
  --out-dir out/cv \
  --bbox 120,80,620,460 \
  --scale 2
```

```bash
python tools/run_cv_probe.py --tool px_m_calibrator \
  --image case_data/1f_view.png \
  --out-dir out/cv \
  --anchors-json '[{"axis":"x","px_a":100,"px_b":700,"value_m":15.0,"dimension_ref":"overall_width"}]'
```

```bash
python tools/run_cv_probe.py --tool window_cc_detector \
  --image case_data/South_view.png \
  --out-dir out/cv \
  --min-area 30
```

For a sweep, put the requests in a JSON file and run them in one call (maximum 32, all validated
before any of them runs). Name the file yourself (`requests/sweep.json` below is one such name, not a
placeholder to fill in) — angle brackets are shell redirection operators, not a fill-in-the-blank
convention, so a literal `<name>` pasted into a real shell is read as "redirect stdin from a file
called `name`, redirect stdout to a file called `.json`," not as a path:

```bash
python tools/run_cv_probe.py --batch requests/sweep.json
```

Sidecars are written under `<out-dir>/cv_evidence/<image_stem>/NNN_<tool>.json` (e.g.
`out/cv/cv_evidence/1f_view/001_wall_line_profiler.json`) with a matching overlay PNG. In this batch
that directory is a flat-stage audit sidecar only; attempts/report collection may archive it in
future work.

## Source-Frame Anchor

Every sidecar reports the true pixel size of the image the tools actually read. In
`NNN_<tool>.json`, under `source_image`:

```json
{"name": "f51_synth.png", "sha256": "1a05d35c64ae", "width_px": 1200, "height_px": 600}
```

- `width_px` and `height_px` are the **source image's** real dimensions — the file on disk. They are
  never the cropped or rescaled size: a `crop_zoom` sidecar still reports the source dimensions, not
  the crop's.
- The drawing you *saw* may have been rescaled before it reached you, and pixel coordinates you
  eyeballed live in that rescaled frame. Check yourself: compare the image width you had in mind
  against the sidecar's `width_px`. If they differ, every eyeballed pixel coordinate carries that
  same uniform scale error — a calibration built from such anchors is self-consistent yet globally
  wrong, and a crop sent to those coordinates lands in the wrong region.
- Therefore, for any pixel coordinate you hand to a tool — calibration anchors, `--bbox` values —
  prefer numbers read back from a tool (profiler `position_px`, detector `bbox_px`, crop-chain
  coordinates) over eyeballed ones.
- For this project's own cases, staging now pre-resizes an oversized `case_data/` drawing to the
  vision API's own resize target BEFORE you ever see it (F-51 second cut), so for those images the
  size you had in mind and the sidecar's `width_px` are the same number by construction — there is no
  mismatch left to self-check. Still compare them: a mismatch is possible any time the source is
  already within the tier (nothing to resize) yet the number you eyeballed is simply wrong, or a case
  is staged under a different tier than you expect.

## Writing Your Own Measurement Code

The toolbox above is a convenience, not the limit of what you may compute. You can write your own Python and run it, and you should whenever the shipped recipes do not answer the question you actually have.

```bash
python -c 'import numpy as np; from PIL import Image; print(np.array(Image.open("case_data/1f_view.png").convert("L")).shape)'
```

```bash
# or, for anything longer than a line: write it, then run it
python out/measure_bay_spacing.py
```

- `numpy`, `PIL` (Pillow) and `scipy` are available. Read any image you were given; write any output under `out/`.
- Scripts must live under `out/` or `requests/` — the directories you can write to. Every `.py` file in them is read before anything runs, so keep scratch code you no longer want executed out of those directories.
- No outbound network, and no spawning child processes (`subprocess`, `os.system`): do the work in-process.
- Shell metacharacters are not available: no pipes, no redirection, no `$VAR`, no backticks. Anything you would have piped, do in Python. Quoting a `-c` program in single quotes keeps `>`/`<`/`;` inside it working normally.
- `..` and `~` are refused wherever they appear, including inside your code. Use explicit slicing (`img[:, :, 0]`) rather than `img[..., 0]`, and relative paths from the staging root.
- Numbers you compute this way are measurements, and the same provenance rule applies: record the source pixel coordinate, the origin, the conversion, and enough of your method that the number can be reproduced.

## Disciplines

- Calibrate first. Before any meter coordinate is written, establish px-to-m scale for that drawing. Calibration anchors must be dimension-chain extension lines or ticks located with high-zoom `crop_zoom`, not wall endpoints or text baselines. Target residual is at most 1 px; if residuals exceed that, refine the anchors before writing meter geometry. When the independently fitted x/y scales disagree by more than the calibration ceiling, the tool does not raise and does not withhold a number: it still returns a usable blended `px_per_m`, but sets `axis_calibration_disagreement=true`, forces that candidate's metric confidence to `"low"`, and appends a `cross_axis_disagreement` entry to `warnings[]` (its `guidance` text tells you to re-crop and recalibrate). Treat that combination as a loud signal, not something to blend past: at least one endpoint pair or transcribed dimension is likely wrong, so do not silently trust the blended value as clean — re-measure both axes' anchors at their dimension-chain extension-line intersections and recalibrate; if you must proceed immediately, weight the axis under measurement by preferring its single-axis `axis_px_per_m` over the blended `px_per_m`, and record the disagreement in your notes instead of treating the reading as clean.
- Measure before drawing. Wall lines, window boxes, storey lines, and similar coordinates must come from tool measurement such as projection, connected components, tick candidates, or verified crop measurements. Do not write pure eyeballed coordinates when the clean-vector toolbox applies.
- Use one px-to-m formula and leave enough provenance to reproduce it: `v_m=(px-origin_px)/px_per_m`. Notes for measured strokes should include the source pixel coordinate, origin, converted result, and sidecar/candidate reference. Non-reproducible numbers are invalid data.
- Treat profiler outputs as candidates. Classify semantics with `reading_guide.md`, then log accept/reject decisions with reasons using `overlay_logger` or the reading sidecar. Rejected candidates are evidence, not clutter to delete.
- Verify candidate crops before accepting them. For each material candidate set, inspect crops or overlays, accept only the semantically correct elements, and record rejected candidates with a reason. The recognition and pen rules remain in `guide.md` §0.1 and `pen_library.md`; this file only adds the CV measurement discipline.
- For un-dimensioned elements, calibrated pixel measurement is measurement, not guessing. The `provenance` vocabulary is closed — `seen | dimension_derived | estimated | unknown`, and nothing else validates; there is no separate "pixel-measured" value. Record such strokes as `seen`, leave `dimension_refs` empty, and cite the relevant CV sidecar/candidate in the notes so the number stays reproducible. Reserve `estimated` for coordinates you did not measure.
- `dimensions[].anchor` is a flat pixel bbox list: `[x0,y0,x1,y1]`. Do not replace it with a custom object shape.

This batch does not test weak-VLM dimension transcription or OCR robustness. Dimension text remains a reading responsibility unless a later OCR tool is added.
