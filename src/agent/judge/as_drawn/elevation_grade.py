"""⭐ elevation grade — the FACADE dimension of the as-drawn reading judge.

The plan-side grader (``reading_grade.py``) scores a plan view line-by-line
against ``denominator.py``'s machine-derived face-line targets.  This file is
the same D-step for an ELEVATION product: it scores one ``as_drawn_elevation_v0``
document against the openings and structure lines THE ANSWER ITSELF puts on
that facade.

## Where the answer comes from (measured, 2026-09-06 — J §三)

Unlike the plan side (whose face-line targets are re-derived from the source
DXF, because the gt carries no face-line-grade plan answer), the v3 gt carries
the elevation answer as first-class citizens:

  * every opening names the elevation view it appears on
    (``source_refs[role="opening_elevation"].view_id``) with its
    ``world_along_interval`` and ``z_interval`` in world metres and its kind
    (door/window) — sm25: East 13, North 8, South 7, West 6 = 34, each view's
    list spanning BOTH floors;
  * every view declares ``floor_ids`` (all four sm25 elevations: ["F1","F2"]);
  * floor lines derive from ``floors[].z_floor_m`` + ``ceiling_height_m`` and
    the facade's along extent from ``boundary_segments[].world_along_interval``
    filtered by the view's ``facade_family``.

So the elevation targets are DERIVED FROM GT ONLY, through the one legal
reader (the caller loads gt via ``gt.py``; this module never touches files).

## The failure modes scored (mirroring the plan grader's lettering)

  E1 OPENING    is each opening the answer has FOUND on this facade, at the
                answer's along × z position (tolerance band, ⛔ never bitwise)?
                A positional hit with the wrong extent is WRONG_SIZE, not OK.
  E2 EXTRA      openings the product claims that no answer opening explains
                (多画 — the direction a plan-only ruler used to reward).
  E3 STRUCTURE  the floor lines (z = each floor's base + the top) and the two
                facade end lines — drawn where the answer has them?  Structure
                lines beyond these (e.g. the mid-facade chain divider at 6 m on
                sm25 East, a real stroke the drawing has) are LEDGERED, ⛔ not
                punished: the answer owns no list of them, and scoring against
                an answer it does not have is the two-rulers mistake.
  E5 KIND       door-vs-window identity.  ⚠️ today's v0 products do not
                declare it (``ledger.door_window_classified == false``), so the
                score is ``null`` WITH the reason named — an honest gap, not a
                zero.  Promoting the declaration is J-5 and is NOT this file.

## ⛔ A facade that spans two floors is graded WHOLE (F-89's shape, forbidden)

Every sm25 elevation spans F1+F2.  The targets are therefore built per VIEW,
never per floor: a grader that filtered openings by floor would return an
empty half for every sm25 facade — exactly the "one facade crosses two storeys,
the whole sheet is dropped"档 the dispatch bans outright.  The lock holding
this is in ``tests/test_elevation_grade.py``.

## J-1 / J-2 (tolerance band; two declared grids, consumed)

Both sides' coordinates are snapped to their OWN declared grid before
comparison (``resolutions.snap_to_resolution``); the worst-case displacement of
those snaps (``quantization_band_m``) is reported next to the semantic
tolerances.  A semantic tolerance NARROWER than the band cannot be met even by
a perfect product, so it is refused loudly
(:class:`ToleranceBelowQuantizationBand`) — that is the mechanised form of
"comparing with == would go all red".
"""
from __future__ import annotations

from typing import Mapping

from src.agent.judge.as_drawn.resolutions import (
    GT_RESOLUTION_M,
    PIPELINE_OUTPUT_RESOLUTION_M,
    ResolutionDeclaration,
    quantization_band_m,
    read_gt_resolution,
    read_product_resolution,
    snap_to_resolution,
)

ALONG_TOL_M = 0.30       # semantic band: opening centre, along the facade
Z_TOL_M = 0.20           # semantic band: opening centre, vertical
SIZE_TOL_M = 0.30        # semantic band: opening width / height
STRUCT_TOL_M = 0.08      # structure line position — same band the plan side uses
STRUCT_SPAN_MIN = 0.50   # a structure line counts when this much of it is drawn

GRADE_VERSION = "elevation_grade_v1"
RULE_VERSION = "elevation_targets_v1"


class ElevationTargetsUnavailable(RuntimeError):
    """No scoreable answer for this view — loud, ⛔ never an empty denominator.

    Mirrors ``denominator.DenominatorUnavailable`` (F-126): an empty target set
    returned normally would be indistinguishable, in the artifact, from "the
    product is perfect".
    """

    def __init__(self, view_id: str, reason: str) -> None:
        self.view_id = view_id
        super().__init__(
            f"elevation_targets_unavailable view={view_id}: {reason}")


class ToleranceBelowQuantizationBand(RuntimeError):
    """A semantic tolerance the declared grids make unmeetable.

    With both sides snapped to their declared grids, coordinates can differ by
    up to :func:`quantization_band_m` BY CONSTRUCTION; a tighter tolerance would
    fail a perfect product.  Loud refusal, never a quiet all-red (J-1/J-2).
    """

    def __init__(self, *, band_m: float, along_tol_m: float, z_tol_m: float,
                 product_resolution: ResolutionDeclaration,
                 gt_resolution: ResolutionDeclaration) -> None:
        self.band_m = band_m
        super().__init__(
            f"tolerance_below_quantization_band: band={band_m:.6f} m exceeds a"
            f" semantic tolerance (along={along_tol_m}, z={z_tol_m})."
            f" gt grid={gt_resolution.value_m} ({gt_resolution.source});"
            f" product grid={product_resolution.value_m}"
            f" ({product_resolution.source}). A product declaring a grid this"
            " coarse cannot be graded against these bands (J-2).")


def _view_record(gt: Mapping, view_id: str) -> dict:
    for source in gt.get("sources") or []:
        for view in source.get("views") or []:
            if view.get("id") == view_id and view.get("kind") == "elevation":
                return view
    raise ElevationTargetsUnavailable(
        view_id, "no elevation view with this id in gt.sources[].views[]")


def _floors_by_id(gt: Mapping) -> dict:
    return {floor.get("id"): floor for floor in gt.get("floors") or []}


def elevation_targets(gt: Mapping, view_id: str) -> dict:
    """Derive ONE facade's answer book from gt: its openings WHOLE (all floors),
    its floor lines and its two end lines.

    ⛔ Never filters openings by floor — see the module docstring (F-89).
    """
    view = _view_record(gt, view_id)
    floor_ids = list(view.get("floor_ids") or [])
    if not floor_ids:
        raise ElevationTargetsUnavailable(view_id, "view declares no floor_ids")
    floors = _floors_by_id(gt)
    missing = [fid for fid in floor_ids if fid not in floors]
    if missing:
        raise ElevationTargetsUnavailable(
            view_id, f"floor_ids not found in gt.floors: {missing}")

    openings = []
    for opening in gt.get("openings") or []:
        ref = next((r for r in opening.get("source_refs") or []
                    if r.get("role") == "opening_elevation"
                    and r.get("view_id") == view_id), None)
        if ref is None:
            continue
        along = opening["world_along_interval"]
        z = opening["z_interval"]
        openings.append({
            "id": opening.get("id"),
            "kind": opening.get("kind"),
            "floor_id": opening.get("floor_id"),
            "along_m": [float(along["lo"]), float(along["hi"])],
            "z_m": [float(z["lo"]), float(z["hi"])],
        })
    if not openings:
        raise ElevationTargetsUnavailable(
            view_id,
            "gt carries no openings for this view — an empty denominator is"
            " never a denominator")

    # floor lines: each floor's base, plus the top of the topmost floor
    zs = sorted(float(floors[fid]["z_floor_m"]) for fid in floor_ids)
    zs.append(zs[-1] + float(floors[floor_ids[-1]]["ceiling_height_m"]))
    deduped: list[float] = []
    for z in zs:
        if not deduped or abs(z - deduped[-1]) > 1e-6:
            deduped.append(z)

    # end lines: the facade family's along extent, across this view's floors
    spans = [seg["world_along_interval"]
             for floor in (floors[fid] for fid in floor_ids)
             for seg in floor.get("boundary_segments") or []
             if seg.get("facade_family") == view.get("facade_family")]
    if not spans:
        raise ElevationTargetsUnavailable(
            view_id,
            f"no boundary segment with facade_family={view.get('facade_family')!r}")
    along_extent = [min(float(s["lo"]) for s in spans),
                    max(float(s["hi"]) for s in spans)]

    return {
        "rule_version": RULE_VERSION,
        "view_id": view_id,
        "facade_family": view.get("facade_family"),
        # ⭐ the WHOLE floor span, stated so a grader can never lose half of it
        "floor_ids": floor_ids,
        "openings": openings,
        "floor_line_z_m": deduped,
        "along_extent_m": along_extent,
        "ledger": {"openings": len(openings),
                   "openings_by_floor": {
                       fid: sum(1 for o in openings if o["floor_id"] == fid)
                       for fid in floor_ids}},
    }


def _pair_openings(target_rows, product_rows, *, along_tol, z_tol):
    """Greedy one-to-one pairing by overlap area — a product opening answers at
    most one answer opening and vice versa (the plan grader's claimed-map rule,
    elevation shape)."""
    pairs = []
    for ti, t in enumerate(target_rows):
        for oi, o in enumerate(product_rows):
            alo, ahi = t["along_m"]
            zlo, zhi = t["z_m"]
            olo, ohi = o["along_m"]
            qlo, qhi = o["z_m"]
            ov = (max(0.0, min(ahi, ohi) - max(alo, olo))
                  * max(0.0, min(zhi, qhi) - max(zlo, qlo)))
            if ov > 0.0:
                ca = abs((alo + ahi) / 2.0 - (olo + ohi) / 2.0)
                cz = abs((zlo + zhi) / 2.0 - (qlo + qhi) / 2.0)
                pairs.append((ov, ti, oi, ca, cz))
    pairs.sort(key=lambda p: -p[0])
    taken_t: set[int] = set()
    taken_o: set[int] = set()
    matched: dict[int, tuple[int, float, float]] = {}
    for ov, ti, oi, ca, cz in pairs:
        if ti in taken_t or oi in taken_o:
            continue
        taken_t.add(ti)
        taken_o.add(oi)
        matched[ti] = (oi, ca, cz)
    return matched, taken_o


def _flip_interval(interval, extent) -> list[float]:
    """Reflect one along interval about the answer's own facade extent.

    ⭐ The mirror is NOT a free parameter: it is the one fixed reflection
    ``x -> (lo + hi) - x`` about the extent the ANSWER itself declares for this
    facade — no shift, no scale, no fitting.  A product whose along axis points
    the other way (the prototype's North/West products, measured 2026-09-06)
    lines up with gt one-to-one under exactly this reflection and under no
    other candidate.
    """
    lo, hi = float(extent[0]), float(extent[1])
    a, b = float(interval[0]), float(interval[1])
    return sorted((lo + hi - a, lo + hi - b))


#: how much better one axis hypothesis must place before the grader switches
#: to it (⭐ deliberately ≥ 2: one coincidental hit never flips the axis, and
#: an honest product with a REAL misdrawn direction stays visible as a low
#: identity score instead of being laundered by a marginal mirror win)
AXIS_SWITCH_MARGIN = 2


def grade(doc: Mapping, targets: Mapping, *, gt: Mapping | None = None,
          along_axis: str | None = None,
          along_tol_m: float = ALONG_TOL_M, z_tol_m: float = Z_TOL_M,
          size_tol_m: float = SIZE_TOL_M, struct_tol_m: float = STRUCT_TOL_M,
          struct_span_min: float = STRUCT_SPAN_MIN) -> dict:
    """Score one elevation product against one facade's answer book.

    ``gt`` (optional) supplies the gt-side grid declaration; without it the
    signed default stands in, named as such.  The product-side grid is always
    read from ``doc`` (J-2: a declaration in the product moves the grade).

    ``along_axis``: ``"identity"`` / ``"mirror"`` forces the along-axis
    hypothesis (what an orientation binding passes in once one exists);
    ``None`` picks between the TWO fixed hypotheses by which PLACES more
    openings, and the choice rides out in ``along_axis`` on the report —
    ⛔ never silently.  See ``_flip_interval`` for why mirror is not a fitted
    parameter.
    """
    # ---- J-2: read both declarations, snap both sides onto their own grid ----
    if gt is not None:
        gt_res = read_gt_resolution(gt)
    else:
        gt_res = ResolutionDeclaration(GT_RESOLUTION_M, "default:"
                                       f"{GT_RESOLUTION_M}", declared=False)
    prod_res = read_product_resolution(doc)
    band = quantization_band_m(gt_res.value_m, prod_res.value_m)
    if band > min(along_tol_m, z_tol_m):
        raise ToleranceBelowQuantizationBand(
            band_m=band, along_tol_m=along_tol_m, z_tol_m=z_tol_m,
            product_resolution=prod_res, gt_resolution=gt_res)

    def _snap_interval(interval, grid):
        lo, hi = snap_to_resolution(float(interval[0]), grid), \
            snap_to_resolution(float(interval[1]), grid)
        return [min(lo, hi), max(lo, hi)]

    target_rows = [{
        **t,
        "along_m": _snap_interval(t["along_m"], gt_res.value_m),
        "z_m": _snap_interval(t["z_m"], gt_res.value_m),
    } for t in targets["openings"]]

    extent = targets["along_extent_m"]
    base_rows = [{
        "id": o.get("id"),
        "kind": o.get("kind"),
        "along_m": _snap_interval(o["x_range_m"], prod_res.value_m),
        "z_m": _snap_interval(o["z_range_m"], prod_res.value_m),
    } for o in doc.get("openings") or []]
    # mirror = reflect the product's OWN snapped coordinates about the answer's
    # facade extent — the reflection is pure geometry (axis alignment), so it
    # happens AFTER the product's own grid snap and is never re-snapped.
    mirrored_rows = [{**o, "along_m": _flip_interval(o["along_m"], extent)}
                     for o in base_rows]

    def _attempt(rows):
        matched, taken_o = _pair_openings(
            target_rows, rows, along_tol=along_tol_m, z_tol=z_tol_m)
        o_rows = []
        for ti, (t, row) in enumerate(zip(targets["openings"], target_rows)):
            out = {**t, "verdict": "NOT_FOUND", "matched": None}
            hit = matched.get(ti)
            if hit is not None:
                oi, ca, cz = hit
                o = rows[oi]
                placed = ca <= along_tol_m and cz <= z_tol_m
                t_along, t_z = row["along_m"], row["z_m"]
                size_ok = (abs((t_along[1] - t_along[0]) - (o["along_m"][1] - o["along_m"][0])) <= size_tol_m
                           and abs((t_z[1] - t_z[0]) - (o["z_m"][1] - o["z_m"][0])) <= size_tol_m)
                out.update({
                    "matched": o["id"],
                    "along_centre_err_m": round(ca, 4),
                    "z_centre_err_m": round(cz, 4),
                    "verdict": ("OK" if placed and size_ok
                                else "WRONG_SIZE" if placed else "OUT_OF_BAND"),
                })
            o_rows.append(out)
        placed = sum(1 for r in o_rows if r["verdict"] in ("OK", "WRONG_SIZE"))
        return placed, o_rows, taken_o

    identity_placed, identity_rows, identity_taken = _attempt(base_rows)
    mirror_placed, mirror_rows, mirror_taken = _attempt(mirrored_rows)

    axis_explicit = along_axis is not None
    if along_axis is None:
        if mirror_placed - identity_placed >= AXIS_SWITCH_MARGIN:
            along_axis, ambiguous = "mirror", False
        elif identity_placed - mirror_placed >= AXIS_SWITCH_MARGIN:
            along_axis, ambiguous = "identity", False
        else:
            along_axis, ambiguous = "identity", True
    else:
        ambiguous = False
    if along_axis == "mirror":
        o_rows, taken_o = mirror_rows, mirror_taken
    else:
        o_rows, taken_o = identity_rows, identity_taken
    axis_report = {
        "assumed": along_axis,
        "explicit": axis_explicit,
        "identity_placed": identity_placed,
        "mirror_placed": mirror_placed,
        "ambiguous": ambiguous,
        "rule": "two fixed hypotheses (identity; reflection about the answer's"
                " own along extent) — ⛔ no shift or scale is fitted; switch"
                f" needs a margin of {AXIS_SWITCH_MARGIN} placed openings",
    }

    # ---- E2: product openings no answer opening explains ----
    product_rows = mirrored_rows if along_axis == "mirror" else base_rows
    extras = [product_rows[oi] for oi in range(len(product_rows))
              if oi not in taken_o]

    # ---- E3: structure lines (floor lines + the two end lines) ----
    # ⭐ axis-IMMUNE by construction: the end-line targets are the extent's two
    # ends (a reflection maps that set onto itself) and floor lines live on z.
    struct_targets = ([{"what": "floor_line", "axis": "z", "const_m": z,
                        "span_m": extent}
                       for z in targets["floor_line_z_m"]]
                      + [{"what": "end_line", "axis": "along", "const_m": c,
                          "span_m": [min(targets["floor_line_z_m"]),
                                     max(targets["floor_line_z_m"])]}
                         for c in extent])
    drawn = doc.get("structure_lines") or []
    s_rows = []
    used_lines: set[str] = set()
    for target in struct_targets:
        best = None
        for line in drawn:
            if line.get("axis") != ("row" if target["axis"] == "z" else "col"):
                continue
            const = float(line.get("pos_m"))
            if abs(const - target["const_m"]) > struct_tol_m:
                continue
            runs = [tuple(sorted((float(r[0]), float(r[1]))))
                    for r in line.get("runs_m") or []]
            covered = sum(max(0.0, min(b, target["span_m"][1])
                              - max(a, target["span_m"][0]))
                          for a, b in runs)
            span = target["span_m"][1] - target["span_m"][0]
            frac = covered / max(1e-9, span)
            if best is None or frac > best[0]:
                best = (frac, line)
        if best is not None and best[0] >= struct_span_min:
            used_lines.add(best[1].get("id"))
            s_rows.append({**target, "verdict": "OK", "matched": best[1].get("id"),
                           "coverage": round(best[0], 4)})
        else:
            s_rows.append({**target, "verdict": "MISSING",
                           "matched": best[1].get("id") if best else None,
                           "coverage": round(best[0], 4) if best else 0.0})
    # ⛔ LEDGER ONLY: structure strokes the answer has no target for (e.g. the
    # mid-facade chain divider).  Not scored, not punished — the answer owns
    # no list of them; scoring against an answer it does not have is the
    # two-rulers mistake.
    unexplained = [line.get("id") for line in drawn
                   if line.get("id") not in used_lines]

    # ---- E5: door/window identity, ONLY when the product declares it ----
    kinds_named = all(o.get("kind") in ("door", "window") for o in product_rows)
    kind_rows = []
    if kinds_named and product_rows:
        for row in o_rows:
            if row["verdict"] in ("OK", "WRONG_SIZE") and row["matched"] is not None:
                named = next(o["kind"] for o in product_rows
                             if o["id"] == row["matched"])
                kind_rows.append(named == row["kind"])
    kind_pct = (round(100.0 * sum(kind_rows) / len(kind_rows), 1)
                if kind_rows else None)

    n = len(o_rows)
    found = sum(1 for r in o_rows if r["verdict"] != "NOT_FOUND")
    ok = sum(1 for r in o_rows if r["verdict"] == "OK")
    s_ok = sum(1 for r in s_rows if r["verdict"] == "OK")
    return {
        "grade_version": GRADE_VERSION,
        "denominator": {"rule": targets["rule_version"],
                        "view": targets["view_id"],
                        "floors": targets["floor_ids"],   # ⭐ the whole span
                        "openings": n,
                        "structure_lines": len(struct_targets)},
        # ⭐ J-2: both grids AS CONSUMED, with their provenance — a report never
        # launders a default into the product's own claim.
        "params": {
            "along_tol_m": along_tol_m, "z_tol_m": z_tol_m,
            "size_tol_m": size_tol_m, "struct_tol_m": struct_tol_m,
            "gt_resolution_m": gt_res.value_m,
            "gt_resolution_source": gt_res.source,
            "product_resolution_m": prod_res.value_m,
            "product_resolution_source": prod_res.source,
            "product_resolution_declared": prod_res.declared,
            "quantization_band_m": band,
        },
        "scores": {
            "E1_openings_found_pct": round(100.0 * found / max(1, n), 1),
            "E1_openings_placed_and_sized_pct": round(100.0 * ok / max(1, n), 1),
            "E2_extra_openings": len(extras),
            "E3_structure_lines_pct": round(
                100.0 * s_ok / max(1, len(struct_targets)), 1),
            "E5_opening_kind_pct": kind_pct,
        },
        "by_verdict": {v: sum(1 for r in o_rows if r["verdict"] == v)
                       for v in ("OK", "WRONG_SIZE", "OUT_OF_BAND", "NOT_FOUND")},
        "along_axis": axis_report,
        "openings_by_floor": dict(targets["ledger"]["openings_by_floor"]),
        "structure_unexplained": unexplained,
        "detail": {"openings": o_rows, "structure": s_rows,
                   "extras": [{"id": e["id"], "along_m": e["along_m"],
                               "z_m": e["z_m"]} for e in extras]},
    }
