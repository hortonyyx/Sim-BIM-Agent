#!/usr/bin/env python3
"""Offline process metrics for a reading artifact — no gt, no LLM, no run.

``reading_regression.py`` answers "did this run score well?", which requires a
paid run to have happened first. This answers a different question that until
now had no answer at all:

    Looking only at the reader's own output, did it MEASURE, or did it eyeball?

Every metric here is computed from ``0_reading/*_view.json`` alone. That makes
it usable in three places a score cannot reach:

  1. as a gate on a fresh reading, before anything downstream runs;
  2. as a discriminating-power test for any NEW reading rule — run it over
     ``case_tests/test_baseline/reading_fixtures.json`` and require that every
     ``good`` fixture stays green while at least one ``bad`` fixture goes red;
  3. as a diagnosis, because each metric names the specific discipline that
     slipped rather than reporting one undifferentiated bad score.

The metric set comes from dissecting the five good historical readings against
the nine bad ones (2026-08-21). Note deliberately that NO single metric
separates all good from all bad: 07-02 scored 9/9 with zero pixel anchors, and
D1 failed with 100% chain-explainable coordinates. Good readings are several
disciplines holding at once, so the value is in the profile, not any one number.

    python scripts/tool_scripts/reading_process_metrics.py <run_dir>
    python scripts/tool_scripts/reading_process_metrics.py --fixtures
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "case_tests" / "test_baseline" / "reading_fixtures.json"

# A stroke endpoint counts as chain-explained when it coincides with an endpoint
# the reader itself transcribed from a printed dimension. 30 mm: tighter than
# any judge tolerance in the repo, loose enough for honest rounding.
CHAIN_TOL_M = 0.03

# No window or door in a building is narrower than this. It is a domain floor,
# not a threshold fitted to the fixtures: the five good fixtures bottom out at
# 1.19-1.20 m and gt openings elsewhere in the repo go down to 0.90 m, so 0.60 m
# sits below anything real while still catching 0.20-0.52 m artefacts. Declared
# here rather than buried in a comparison, because a silent domain constant is
# how "otherwise it must be X" conclusions get made without anyone signing off.
MIN_PLAUSIBLE_OPENING_M = 0.60

# Evidence density = printed dimensions the reader transcribed, per stroke it
# then drew. Over the 14 fixtures this separates good from bad perfectly
# (good 2.37-3.23, bad 0.61-2.14), which is why it is reported prominently --
# and why the threshold below is PROVISIONAL, not a gate: 14 samples, 13 of them
# the same building, and only 0.23 of margin at the boundary. It earns gate
# status by holding on fixtures added later, not by looking clean today.
EVIDENCE_DENSITY_PROVISIONAL_MIN = 2.2

# Polarity: if the GAPS between reported windows on one wall are far more
# regular than the reported windows themselves, the reader probably reported
# the solid piers and called them windows (F-69).
POLARITY_MIN_OPENINGS = 3
POLARITY_GAP_CV_MAX = 0.05
POLARITY_WIDTH_CV_MIN = 0.20

# Chain placement closure. The existing gate in src/validator/checks/reading.py
# asks "Sigma segment VALUES == overall value", which cannot separate the two
# things a reader does with an unlabelled residual. 07-07 and 07-08 transcribed
# sm21's top chain identically (segments 14.76, overall 15.00, residual 0.24):
# 07-07 placed that residual at the two unlabelled 120 mm partition bands and
# closed on 15.00; 07-08 butted the segments end to end and closed on 14.76,
# drifting every window after the first gap by -0.12 then -0.24 and costing it
# a window. Values identical, placement different -- so the check has to be on
# where the chain LANDS, not on what it sums to. An unlabelled residual is
# normal; losing it is the defect.
CHAIN_PLACEMENT_TOL_M = 0.02

def _opening_width(stroke: dict) -> float | None:
    """Along-wall width of a plan opening, for both the rect and line conventions."""
    geom = stroke.get("geometry") or {}
    if geom.get("kind") == "rect":
        xr, yr = geom.get("x_range_m"), geom.get("y_range_m")
        if xr and yr:
            return max(xr[1] - xr[0], yr[1] - yr[0])
    if geom.get("kind") == "line":
        p1, p2 = geom.get("p1"), geom.get("p2")
        if p1 and p2:
            return math.dist(p1, p2)
    return None


_PIXEL_NOTE = re.compile(r"\bpx\b|\bpixel", re.I)
_ARITHMETIC_NOTE = re.compile(r"=\s*-?\d+\.\d+|\d+\s*/\s*\d+\.\d")


def _cv(values: list[float]) -> float | None:
    """Coefficient of variation; None when it is not defined."""
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    if mean <= 0:
        return None
    return statistics.pstdev(values) / mean


def _line_endpoints(stroke: dict) -> list[tuple[str, float]]:
    geom = stroke.get("geometry") or {}
    if geom.get("kind") != "line":
        return []
    out: list[tuple[str, float]] = []
    for key in ("p1", "p2"):
        point = geom.get(key)
        if point and len(point) == 2:
            out.append(("x", float(point[0])))
            out.append(("y", float(point[1])))
    return out


def _transcribed_endpoints(view: dict) -> tuple[set[float], set[float]]:
    xs: set[float] = set()
    ys: set[float] = set()
    for dim in view.get("dimensions") or []:
        for key in ("from", "to"):
            point = dim.get(key)
            if point and len(point) == 2:
                xs.add(round(float(point[0]), 4))
                ys.add(round(float(point[1]), 4))
    return xs, ys


def _polarity_findings(view: dict) -> list[dict]:
    """Walls whose reported openings look like the piers between the openings."""
    walls = [
        s for s in view.get("strokes") or []
        if s.get("pen") == "wall" and (s.get("geometry") or {}).get("kind") == "line"
    ]
    windows = [
        s for s in view.get("strokes") or []
        if s.get("pen") == "window" and (s.get("geometry") or {}).get("kind") == "rect"
    ]
    findings = []
    for wall in walls:
        geom = wall["geometry"]
        p1, p2 = geom["p1"], geom["p2"]
        vertical = abs(p1[0] - p2[0]) < 1e-6
        if not vertical and abs(p1[1] - p2[1]) >= 1e-6:
            continue  # skew wall: along-axis reasoning below does not apply
        fixed = p1[0] if vertical else p1[1]
        lo, hi = sorted([p1[1], p2[1]] if vertical else [p1[0], p2[0]])
        intervals = set()
        for win in windows:
            wg = win["geometry"]
            across = wg["x_range_m"] if vertical else wg["y_range_m"]
            along = wg["y_range_m"] if vertical else wg["x_range_m"]
            on_this_wall = (
                abs((across[0] + across[1]) / 2 - fixed) < 0.35
                and along[0] >= lo - 0.5
                and along[1] <= hi + 0.5
            )
            if on_this_wall:
                intervals.add((float(along[0]), float(along[1])))
        ordered = sorted(intervals)
        if len(ordered) < POLARITY_MIN_OPENINGS:
            continue
        widths = [b - a for a, b in ordered]
        gaps = [ordered[i + 1][0] - ordered[i][1] for i in range(len(ordered) - 1)]
        width_cv, gap_cv = _cv(widths), _cv(gaps)
        if width_cv is None or gap_cv is None:
            continue
        if gap_cv < POLARITY_GAP_CV_MAX and width_cv > POLARITY_WIDTH_CV_MIN:
            findings.append({
                "wall": f"{'V' if vertical else 'H'}@{fixed:.2f}",
                "openings": len(ordered),
                "width_cv": round(width_cv, 3),
                "gap_cv": round(gap_cv, 3),
                "reading": "gaps far more regular than openings — openings may be the piers between the real openings",
            })
    return findings


def _chain_placement_findings(view: dict) -> list[dict]:
    """Chains whose placed extent does not reach the overall they declare.

    Ordering-agnostic on purpose: sm21's left chain is transcribed top-to-bottom,
    so "first.from -> last.to" reads as 2.0 m on a chain that closes perfectly at
    8.0 m. Take the extent across every segment endpoint instead.
    """
    chains: dict[tuple[str, str], dict] = {}
    for dim in view.get("dimensions") or []:
        chain_id, axis, role = dim.get("chain_id"), dim.get("axis"), dim.get("role") or ""
        if not chain_id or axis not in ("x", "y") or dim.get("value_m") is None:
            continue
        entry = chains.setdefault((chain_id, axis), {"overall": [], "segments": []})
        entry["overall" if role in ("overall", "baseline") else "segments"].append(dim)

    findings = []
    for (chain_id, axis), entry in sorted(chains.items()):
        if not entry["overall"] or len(entry["segments"]) < 2:
            continue
        index = 0 if axis == "x" else 1
        try:
            coords = [seg[key][index] for seg in entry["segments"] for key in ("from", "to")]
        except (KeyError, TypeError, IndexError):
            continue  # endpoints not placed: nothing to check placement against
        placed = max(coords) - min(coords)
        declared = abs(entry["overall"][0]["value_m"])
        gap = abs(placed - declared)
        if gap > CHAIN_PLACEMENT_TOL_M:
            findings.append({
                "chain_id": chain_id,
                "axis": axis,
                "declared_overall_m": round(declared, 3),
                "placed_extent_m": round(placed, 3),
                "gap_m": round(gap, 3),
            })
    return findings


def measure(view_paths: list[Path]) -> dict:
    """Measure the views at these paths."""
    return measure_views([json.loads(p.read_text(encoding="utf-8")) for p in view_paths],
                         [p.stem for p in view_paths])


def measure_views(loaded: list[dict], names: list[str] | None = None) -> dict:
    """Measure already-loaded views. Split out from ``measure`` so a synthetic
    view can prove a flag goes red without a file on disk."""
    names = names or [f"view{i}" for i in range(len(loaded))]
    views = 0
    dims_total = 0
    strokes_total = 0
    pens: dict[str, int] = {}
    endpoints = chain_hits = 0
    notes = notes_pixel = notes_arith = 0
    prov: dict[str, int] = {}
    views_without_windows: list[str] = []
    views_without_dimensions: list[str] = []
    min_span: float | None = None
    narrow_openings: list[dict] = []
    polarity: list[dict] = []
    chain_placement: list[dict] = []

    for view, name in zip(loaded, names):
        views += 1
        dims = view.get("dimensions") or []
        dims_total += len(dims)
        if not dims:
            views_without_dimensions.append(name)

        xs, ys = _transcribed_endpoints(view)
        is_plan = view.get("image_kind") == "plan"
        strokes = view.get("strokes") or []
        window_count = 0
        coords: list[float] = []
        for stroke in strokes:
            strokes_total += 1
            pen = stroke.get("pen", "?")
            pens[pen] = pens.get(pen, 0) + 1
            if pen == "window":
                window_count += 1
            prov_key = stroke.get("provenance", "?")
            prov[prov_key] = prov.get(prov_key, 0) + 1
            note = stroke.get("note") or ""
            if note:
                notes += 1
                if _PIXEL_NOTE.search(note):
                    notes_pixel += 1
                if _ARITHMETIC_NOTE.search(note):
                    notes_arith += 1
            for axis, value in _line_endpoints(stroke):
                endpoints += 1
                coords.append(value)
                pool = xs if axis == "x" else ys
                if any(abs(value - c) <= CHAIN_TOL_M for c in pool):
                    chain_hits += 1
            geom = stroke.get("geometry") or {}
            for key in ("x_range_m", "y_range_m"):
                rng = geom.get(key)
                if rng:
                    coords.extend(float(v) for v in rng)
            if is_plan and pen == "window":
                width = _opening_width(stroke)
                # Compare in whole millimetres. Drawings declare openings to the
                # millimetre, and a coordinate pair taken straight off a
                # dimension chain does not survive binary floating point:
                # 10.9 - 10.3 is 0.5999999999999996, so a real 600 mm window
                # tripped this flag as "narrower than anything real". Rounding
                # to the unit the drawing actually uses removes the artefact
                # without moving the domain floor by one millimetre.
                if width is not None and round(width * 1000.0) < round(MIN_PLAUSIBLE_OPENING_M * 1000.0):
                    narrow_openings.append({
                        "view": name,
                        "stroke": stroke.get("id", "?"),
                        "width_m": round(width, 3),
                    })

        if is_plan:
            if window_count == 0:
                views_without_windows.append(name)
            if coords:
                span = max(coords) - min(coords)
                min_span = span if min_span is None else min(min_span, span)
        polarity.extend(_polarity_findings(view))
        if is_plan:
            for finding in _chain_placement_findings(view):
                chain_placement.append({"view": name, **finding})

    def pct(hit: int, total: int) -> float | None:
        return round(hit / total, 3) if total else None

    return {
        "views_read": views,
        "dimensions_transcribed": dims_total,
        "strokes_total": strokes_total,
        "strokes_by_pen": pens,
        "evidence_density": round(dims_total / strokes_total, 2) if strokes_total else None,
        "chain_explained_pct": pct(chain_hits, endpoints),
        "provenance": prov,
        "notes_with_pixel_anchor_pct": pct(notes_pixel, notes),
        "notes_with_arithmetic_pct": pct(notes_arith, notes),
        "plan_views_without_windows": views_without_windows,
        "views_without_dimensions": views_without_dimensions,
        "min_plan_coordinate_span_m": round(min_span, 3) if min_span is not None else None,
        "implausibly_narrow_openings": narrow_openings,
        "polarity_suspects": polarity,
        "chain_placement_gaps": chain_placement,
    }


def flags(metrics: dict) -> list[str]:
    """The subset of metrics that is safe to read as 'something is wrong'.

    Deliberately narrow. Every flag here fires on at least one known-bad fixture
    and on none of the known-good ones; anything that could not clear that bar
    stays a reported number rather than a flag.
    """
    out = []
    narrow = metrics["implausibly_narrow_openings"]
    if narrow:
        worst = min(n["width_m"] for n in narrow)
        where = ", ".join(f"{n['view']}:{n['stroke']}" for n in narrow[:4])
        out.append(
            f"NARROW-OPENING: {len(narrow)} plan opening(s) under "
            f"{MIN_PLAUSIBLE_OPENING_M} m, narrowest {worst} m ({where}) — "
            f"no window or door in a building is that narrow"
        )
    if metrics["plan_views_without_windows"]:
        out.append(
            "ZERO-PRODUCT: plan view(s) with no windows at all: "
            + ", ".join(metrics["plan_views_without_windows"])
        )
    if metrics["views_without_dimensions"]:
        out.append(
            "NO-TRANSCRIPTION: view(s) with no dimension transcribed: "
            + ", ".join(metrics["views_without_dimensions"])
        )
    for finding in metrics["chain_placement_gaps"]:
        out.append(
            f"CHAIN-PLACEMENT: {finding['view']}:{finding['chain_id']} declares an overall of "
            f"{finding['declared_overall_m']} m but its segments only span "
            f"{finding['placed_extent_m']} m ({finding['gap_m']} m unplaced) — an unlabelled "
            f"residual is normal, dropping it off the end is not"
        )
    for finding in metrics["polarity_suspects"]:
        out.append(
            f"POLARITY: wall {finding['wall']} — {finding['openings']} openings, "
            f"width CV {finding['width_cv']} vs gap CV {finding['gap_cv']}; "
            f"{finding['reading']}"
        )
    return out


def provisional_signals(metrics: dict) -> list[str]:
    """Separators that look strong but have not earned gate status yet.

    Kept apart from ``flags`` on purpose. This repo has twice written a
    document-diff correlation straight into the plan and had to retract it, so
    a separator that has only ever been checked against the sample it was found
    in stays advisory until new fixtures test it.
    """
    out = []
    density = metrics["evidence_density"]
    if density is not None and density < EVIDENCE_DENSITY_PROVISIONAL_MIN:
        out.append(
            f"EVIDENCE-DENSITY: {density} transcribed dimensions per stroke "
            f"(the five good fixtures run 2.37-3.23, the nine bad 0.61-2.14) — "
            f"the reader drew more than the drawing's own numbers support"
        )
    return out


def flag_code(flag: str) -> str:
    """The stable code a flag string starts with, e.g. "CHAIN-PLACEMENT"."""
    return flag.split(":", 1)[0].strip()


def unexpected_flags(flags_found: list[str], known_defects: list[str]) -> list[str]:
    """Flags a fixture raises that its known_defects does not account for."""
    allowed = set(known_defects or ())
    return [f for f in flags_found if flag_code(f) not in allowed]


def _view_paths(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "0_reading").glob("*_view.json"))


def _run_fixtures() -> int:
    spec = json.loads(FIXTURES.read_text(encoding="utf-8"))
    rows = []
    for fixture in spec["fixtures"]:
        run_dir = REPO / fixture["run"]
        paths = _view_paths(run_dir)
        if not paths:
            print(f"MISSING fixture artifacts: {fixture['id']} ({fixture['run']})", file=sys.stderr)
            return 2
        metrics = measure(paths)
        rows.append((fixture, metrics, flags(metrics), provisional_signals(metrics)))

    head = (
        f"{'fixture':34}{'':4}{'views':6}{'dims':6}{'strokes':8}"
        f"{'ev/stroke':11}{'chain%':8}{'px-note%':10}  flags"
    )
    print(head)
    print("-" * len(head))
    for fixture, m, fl, prov in rows:
        mark = "OK " if fixture["label"] == "good" else "BAD"
        chain = "-" if m["chain_explained_pct"] is None else f"{m['chain_explained_pct']:.0%}"
        px = "-" if m["notes_with_pixel_anchor_pct"] is None else f"{m['notes_with_pixel_anchor_pct']:.0%}"
        dens = "-" if m["evidence_density"] is None else f"{m['evidence_density']:.2f}"
        print(
            f"{fixture['id']:34}{mark:4}{m['views_read']:<6}{m['dimensions_transcribed']:<6}"
            f"{m['strokes_total']:<8}{dens:<11}{chain:<8}{px:<10}  {len(fl)}"
        )
        known = set(fixture.get("known_defects") or ())
        for line in fl:
            mark = "~known~" if flag_code(line) in known else "!"
            print(f"      {mark} {line}")
        for line in prov:
            print(f"      ~ {line}")

    good_red = [
        f["id"] for f, _, fl, _ in rows
        if f["label"] == "good" and unexpected_flags(fl, f.get("known_defects"))
    ]
    bad_red = [f["id"] for f, _, fl, _ in rows if f["label"] == "bad" and fl]
    good_prov = [f["id"] for f, _, _, pr in rows if f["label"] == "good" and pr]
    bad_prov = [f["id"] for f, _, _, pr in rows if f["label"] == "bad" and pr]
    print()
    print(f"flags       — good flagged UNEXPECTEDLY (must be none): {good_red or 'none'}")
    print(f"              bad  flagged (must be >=1):  {len(bad_red)}/9 {bad_red or 'none'}")
    print(f"provisional — good signalled (must be none): {good_prov or 'none'}")
    print(f"              bad  signalled:                {len(bad_prov)}/9")
    return 0 if not good_red and bad_red else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", nargs="?", help="run directory containing 0_reading/")
    parser.add_argument("--fixtures", action="store_true",
                        help="measure every fixture in reading_fixtures.json and check discriminating power")
    parser.add_argument("--json", action="store_true", help="emit raw metrics as JSON")
    args = parser.parse_args()

    if args.fixtures:
        return _run_fixtures()
    if not args.run_dir:
        parser.error("give a run directory, or --fixtures")

    run_dir = Path(args.run_dir)
    paths = _view_paths(run_dir)
    if not paths:
        print(f"no 0_reading/*_view.json under {run_dir}", file=sys.stderr)
        return 2
    metrics = measure(paths)
    found = flags(metrics)
    if args.json:
        print(json.dumps({"metrics": metrics, "flags": found}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        print()
        for line in found or ["no flags"]:
            print(f"! {line}" if found else line)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
