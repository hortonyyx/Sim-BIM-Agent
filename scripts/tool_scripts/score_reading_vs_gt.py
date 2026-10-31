#!/usr/bin/env python3
"""CLI: score a reading (0_reading plan JSONs) against the case ground truth.

The AUTHORITATIVE reading-quality metric (user directive 2026-06-24): match the
reading's wall/window COORDINATES against gt, element by element — counts AND
offsets. Renders are auxiliary; this table is what calls a reading good/bad.

Usage:
  python scripts/tool_scripts/score_reading_vs_gt.py <reading_dir> --case <case>
  python scripts/tool_scripts/score_reading_vs_gt.py <one_view.json> --case <case> --floor "Floor 1"

  <reading_dir>  a dir containing 1f_view.json / 2f_view.json (plan views)
  --case         gt case name under case_tests/test_baseline/gt/<case>/gt.json
  --wall-tol     metres; a gt wall line counts as found within this (default 0.30)
  --win-tol      metres; a gt window counts as found if its centre is within this (default 0.40)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agent.judge.gt import load_gt  # noqa: E402
from src.agent.judge import reading_score as rs  # noqa: E402
from src.agent.judge.score_service import score_attempt_service  # noqa: E402


def _fmt_walls(matches):
    cells = []
    for m in matches:
        if m.read is None:
            cells.append(f"{m.truth}→MISS")
        else:
            cells.append(f"{m.truth}→{m.read}(Δ{m.delta:+})")
    return ", ".join(cells) if cells else "—"


def _print_floor(stem, sc):
    wh, wt = sc.wall_hits()
    nh, nt = sc.window_hits()
    print(f"\n## {stem}  ({sc.floor})")
    print(f"  walls   {wh}/{wt} hit   (max offset {sc.max_wall_offset()} m)")
    print(f"    vert x : {_fmt_walls(sc.vwalls)}" + (f"  | EXTRA {sc.extra_vwalls}" if sc.extra_vwalls else ""))
    print(f"    horiz y: {_fmt_walls(sc.hwalls)}" + (f"  | EXTRA {sc.extra_hwalls}" if sc.extra_hwalls else ""))
    print(f"  windows {nh}/{nt} hit")
    for f in ("N", "S", "E", "W"):
        ms = sc.windows.get(f, [])
        if not ms and not sc.extra_windows.get(f):
            continue
        parts = []
        for m in ms:
            ts, te = m.truth
            parts.append(f"{ts}-{te}:" + ("MISS" if m.read is None else f"OK(Δc{m.centre_delta:+})"))
        ex = sc.extra_windows.get(f, [])
        extra = f"  EXTRA {ex}" if ex else ""
        print(f"    {f}: {', '.join(parts) if parts else '—'}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="reading dir, or a single *_view.json")
    ap.add_argument("--case", required=True)
    ap.add_argument("--floor", help="gt floor name (single-file mode)")
    ap.add_argument("--gt-dir")
    ap.add_argument("--wall-tol", type=float, default=rs.DEFAULT_WALL_TOL_M)
    ap.add_argument("--win-tol", type=float, default=rs.DEFAULT_WIN_CENTRE_TOL_M)
    ap.add_argument("--json", action="store_true", help="also emit machine-readable summary after human rows")
    ap.add_argument("--json-only", action="store_true", help="emit only the machine-readable summary")
    ap.add_argument("--typed-elevation-json", help="v3 product boundary payload; projected only through --bindings")
    ap.add_argument("--bindings", help="reviewed JudgeScoreViewBindingsV1 JSON for v3 elevation inputs")
    ap.add_argument("--gt-file", help="typed v3 GT file (required for the C2 route)")
    ap.add_argument("--view-manifest", help="base ViewManifest JSON (required for the C2 route)")
    ap.add_argument("--judge-config", default="src/configs/judge_score.yaml", help="strict judge score config for C2")
    ap.add_argument("--completeness-overlay", help="optional reviewed C2 completeness overlay")
    ap.add_argument("--attempt", type=int, default=0, help="attempt identity for C2 sidecar")
    ap.add_argument("--out-dir", help="write C2 score_vs_gt.json + grade.png atomically")
    ap.add_argument(
        "--run-profile",
        default="exploratory",
        choices=("exploratory", "dev", "golden", "regression"),
    )
    args = ap.parse_args()

    target = Path(args.target)
    # Production v3 normalization boundary.  Product-provided mirror/local-x
    # declarations are not read; projection is entirely from reviewed bindings.
    if bool(args.typed_elevation_json) != bool(args.bindings):
        ap.error("--typed-elevation-json and --bindings must be supplied together")
    if args.typed_elevation_json:
        try:
            if not args.gt_file or not args.view_manifest:
                ap.error("C2 route requires --gt-file and --view-manifest")
            from src.agent.execution.view_manifest import ViewManifest
            from src.agent.judge.score_config import load_judge_score_config
            from src.agent.judge.score_inputs import load_completeness_overlay, load_score_view_bindings
            from src.agent.judge.score_schema import (build_product_identity, commit_score_artifacts,
                                                       load_score_gt_identity)
            payload_text = Path(args.typed_elevation_json).read_text(encoding="utf-8")
            payload = json.loads(payload_text)
            gt_identity, gt_document = load_score_gt_identity(args.gt_file)
            if gt_document is None:
                raise ValueError("unsupported GT profile")
            base = ViewManifest.model_validate_json(Path(args.view_manifest).read_text(encoding="utf-8"))
            bindings = load_score_view_bindings(args.bindings, expected_case_id=gt_document.case,
                expected_gt_content_sha256=gt_identity.content_sha256,
                expected_case_metadata_sha256=base.case_metadata_sha256,
                expected_base_view_manifest_sha256=base.content_sha256)
            overlay = load_completeness_overlay(args.completeness_overlay, expected_case_id=gt_document.case,
                expected_gt_content_sha256=gt_identity.content_sha256,
                expected_base_view_manifest_sha256=base.content_sha256)
            from src.agent.execution.manifest import hash_text
            from src.agent.judge.reading_typed_adapter import (
                identify_reading_contract,
            )
            from src.agent.judge.score_service import TopLevelNotApplicableError
            product_identity = build_product_identity(stage="reading", attempt=args.attempt,
                output_sha256=hash_text(payload_text),
                output_schema=identify_reading_contract(payload).contract_id,
                source="attempt_output", accepted_stage_record=None)
            result = score_attempt_service(typed_request={"gt_identity": gt_identity, "gt": gt_document,
                "stage": "reading", "product_payload": payload, "product_identity": product_identity,
                "base_view_manifest": base, "score_bindings": bindings, "completeness_overlay": overlay,
                "c2_config": load_judge_score_config(args.judge_config),
                "run_profile": args.run_profile})
            if args.out_dir:
                out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
                commit_score_artifacts(sidecar_path=out / "score_vs_gt.json", grade_path=out / "grade.png",
                    sidecar=result.sidecar, grade_png=result.grade_png)
            print(result.sidecar.model_dump_json(indent=2))
            from src.agent.judge.score_service import (
                strict_payload_violation_reason,
            )
            strict_violation = strict_payload_violation_reason(result.payload)
            if strict_violation is not None and args.run_profile in {
                "golden", "regression"
            }:
                raise TopLevelNotApplicableError(strict_violation)
            return 0
        except Exception as exc:  # boundary prints no raw exception details
            print(f"typed elevation rejected: {getattr(exc, 'code', 'score_product_identity_invalid')}", file=sys.stderr)
            return 2

    def legacy_cli_evaluator(_stage, _output, _gt, *, grade):
        if target.is_dir():
            return rs.score_reading_dir(target, args.case, gt_dir=args.gt_dir,
                                        wall_tol=args.wall_tol, win_tol=args.win_tol)
        loaded_gt = load_gt(args.case, gt_dir=args.gt_dir) if args.gt_dir else load_gt(args.case)
        if loaded_gt is None:
            raise LookupError("no gt")
        reading = json.loads(target.read_text(encoding="utf-8"))
        fname = args.floor or rs.floor_name_for_image(target.stem, loaded_gt)
        if fname is None:
            raise ValueError("floor mapping")
        return {target.stem: rs.score_floor(reading, loaded_gt, fname,
                                             wall_tol=args.wall_tol, win_tol=args.win_tol)}
    try:
        scores = score_attempt_service(stage="0_reading", output={}, gt={}, grade=None,
                                       legacy_evaluator=legacy_cli_evaluator)
    except LookupError:
        print(f"no gt for case {args.case!r}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # F-76: this handler used to translate EVERY ValueError into a floor-
        # mapping hint. `load_gt` raises GtValidationError (a ValueError) with
        # `gt_v3_requires_typed_consumer` for every v3 answer, so the real
        # message -- "this scorer cannot read a v3 gt at all" -- was replaced by
        # advice to pass a flag that does not help. Report the raised reason and
        # keep the hint only for the case it was written for.
        if str(exc) == "floor mapping":
            print("could not map image to a gt floor; pass --floor", file=sys.stderr)
        else:
            print(f"legacy reading scorer refused this input: {exc}", file=sys.stderr)
        return 2

    tot_wh = tot_wt = tot_nh = tot_nt = 0
    summary = {}
    for stem, sc in scores.items():
        wh, wt = sc.wall_hits(); nh, nt = sc.window_hits()
        tot_wh += wh; tot_wt += wt; tot_nh += nh; tot_nt += nt
        summary[stem] = {"walls": [wh, wt], "windows": [nh, nt], "max_wall_offset_m": sc.max_wall_offset()}

    if args.json_only:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"# reading↔gt score — case {args.case}  (wall_tol={args.wall_tol}m, win_tol={args.win_tol}m)")
    for stem, sc in scores.items():
        _print_floor(stem, sc)
    print(f"\n=== TOTAL: walls {tot_wh}/{tot_wt}, windows {tot_nh}/{tot_nt} ===")
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
