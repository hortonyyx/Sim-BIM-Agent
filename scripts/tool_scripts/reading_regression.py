#!/usr/bin/env python3
"""Reading regression gate — the check that did not exist.

Between 2026-07-07 and 2026-08-18 the repo took 146 files / ~47.6k insertions,
including ~2445 lines of entirely new machinery sitting between the reader and
the drawing. Every one of those changes passed its own gate. NONE of them passed
"does reading still work", because no such gate existed: the suite's only
9/9 assertions score a FROZEN 07-07 artifact, i.e. they prove the scorer still
works, not that the pipeline can still PRODUCE a 9/9 reading.

Result: breakage accumulated with zero signal for six weeks and then presented
as one undifferentiated mass, which is why nine single-variable investigations
all came back empty — the causes went in together.

This script closes that loop. It does NOT run a reading (that needs an LLM and
does not belong in the unit suite); it scores an ALREADY-COMPLETED run against
the recorded good baselines and says red or green. Run it at every milestone.

    python scripts/tool_scripts/reading_regression.py <case> <run_dir>
    python scripts/tool_scripts/reading_regression.py --list

⛔ The baselines are three runs across TWO model families (Sonnet 5, Haiku 4.5,
gpt-5.4-mini). That is the point: the target is the FORM that repeatedly scored
well, not any single run's configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "case_tests" / "test_baseline" / "reading_regression_baseline.json"


def totals(score_path: Path) -> dict:
    data = json.loads(score_path.read_text(encoding="utf-8"))
    wall_hits = wall_total = win_hits = win_total = 0
    worst_offset = 0.0
    for view in data.get("scores", {}).values():
        if "wall_total" not in view:
            continue
        wall_hits += view["wall_hits"]
        wall_total += view["wall_total"]
        win_hits += view["window_hits"]
        win_total += view["window_total"]
        worst_offset = max(worst_offset, view.get("max_wall_offset_m") or 0.0)
    return {
        "wall_hits": wall_hits, "wall_total": wall_total,
        "window_hits": win_hits, "window_total": win_total,
        "max_wall_offset_m": round(worst_offset, 3),
    }


def latest_score(run_dir: Path) -> Path | None:
    hits = sorted(run_dir.glob("0_reading/attempts/*/score_vs_gt.json"))
    return hits[-1] if hits else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", nargs="?")
    ap.add_argument("run_dir", nargs="?", type=Path)
    ap.add_argument("--list", action="store_true", help="show the recorded baselines and exit")
    args = ap.parse_args(argv)

    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.is_file() else {}

    if args.list or not args.case:
        print(json.dumps(base, indent=2, ensure_ascii=False))
        return 0

    case_base = base.get(args.case)
    if not case_base:
        print(f"⛔ no recorded baseline for case {args.case!r} — cannot judge regression")
        return 2

    score_path = latest_score(args.run_dir)
    if score_path is None:
        print(f"⛔ no score_vs_gt.json under {args.run_dir}/0_reading/attempts/*/")
        return 2

    got = totals(score_path)
    floor = case_base["floor"]
    refs = case_base["reference_runs"]

    try:
        shown = score_path.resolve().relative_to(REPO)
    except ValueError:
        shown = score_path
    print(f"case {args.case} · {shown}")
    print(f"  参照（{len(refs)} 次成功，跨 {len({r['family'] for r in refs})} 个模型家族）:")
    for r in refs:
        print(f"    {r['run']:42} {r['family']:10} 墙 {r['wall_hits']}/{r['wall_total']}"
              f"  窗 {r['window_hits']}/{r['window_total']}")
    print(f"  本次: 墙 {got['wall_hits']}/{got['wall_total']}"
          f"  窗 {got['window_hits']}/{got['window_total']}"
          f"  最大偏移 {got['max_wall_offset_m']} m")
    print(f"  门槛: 墙 ≥{floor['wall_hits']}  窗 ≥{floor['window_hits']}")

    bad = []
    if got["wall_hits"] < floor["wall_hits"]:
        bad.append(f"墙 {got['wall_hits']} < {floor['wall_hits']}")
    if got["window_hits"] < floor["window_hits"]:
        bad.append(f"窗 {got['window_hits']} < {floor['window_hits']}")

    if bad:
        print(f"\n❌ REGRESSED — {' · '.join(bad)}")
        return 1
    print("\n✅ PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
