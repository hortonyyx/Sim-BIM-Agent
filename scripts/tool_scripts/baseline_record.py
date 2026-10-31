"""Baseline run record initializer.

Auto-fills the deterministic parts of a test_baseline/runs/<id>/ directory so
that Claude (at session end) and the user (after OpenStudio inspection) only
need to fill the remaining <FILL_ME> fields.

Usage
-----
    python scripts/tool_scripts/baseline_record.py <case> <tag> [--idf <path>] [--date <YYYY-MM-DD>]

Examples
--------
    python scripts/tool_scripts/baseline_record.py sm_15 post_p0
    python scripts/tool_scripts/baseline_record.py sm_15 post_p0 --idf custom/path/foo.idf

Behavior
--------
- run_id   = "<date>_<case>_<tag>"  (date defaults to today)
- run_dir  = "case_tests/test_baseline/runs/<run_id>/"
- Refuses to overwrite an existing run_dir (safer than silent merge).
- IDF discovery:
    * --idf override wins
    * else for case == "sm_<N>" tries backup/tests_history/SmallOffice/smalloffice_<N>/output/smalloffice_<N>.idf
    * if no IDF found, counts are left null and a warning is printed.
- IDF parsing is regex-based (no eppy dependency, no IDD load) — fast, robust
  for top-level object counting.

What gets auto-filled
---------------------
geometry.json :  counts, counts_expected (mirror of counts), dimensions_check="pending"
meta.json     :  timestamp, case, p0_flags=[]; the rest = "<FILL_ME>"
tokens.json   :  empty skeleton with by_phase / by_tool dicts
notes.md      :  section stubs

What Claude / user must fill afterward — see case_tests/test_baseline/README.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# IDF discovery + counting
# --------------------------------------------------------------------------- #

def find_idf(case: str, override: str | None) -> Path | None:
    if override:
        p = Path(override)
        return p if p.is_absolute() else (PROJECT_ROOT / p)
    m = re.match(r"^sm_(\d+)$", case)
    if m:
        n = m.group(1)
        candidate = (
            PROJECT_ROOT / "backup" / "tests_history" / "SmallOffice"
            / f"smalloffice_{n}" / "output" / f"smalloffice_{n}.idf"
        )
        if candidate.exists():
            return candidate
    return None


def count_idf_objects(idf_path: Path) -> dict:
    """Count top-level IDF objects via regex.

    IDF object syntax: lines starting (after optional whitespace) with
    "<ObjectName>," begin a new object. Comments use "!" so this is safe.
    """
    text = idf_path.read_text(encoding="utf-8", errors="ignore")

    def count(name: str) -> int:
        return len(re.findall(
            rf"^\s*{re.escape(name)}\s*,",
            text,
            re.IGNORECASE | re.MULTILINE,
        ))

    return {
        "zones": count("Zone"),
        "surfaces": count("BuildingSurface:Detailed"),
        "fenestration": count("FenestrationSurface:Detailed"),
    }


# --------------------------------------------------------------------------- #
# Skeleton writers
# --------------------------------------------------------------------------- #

def write_meta(run_dir: Path, case: str, timestamp: str) -> None:
    meta = {
        "timestamp": timestamp,
        "case": case,
        "model": "<FILL_ME>",
        "skill_version": "<FILL_ME>",
        "mcp_tool_count": "<FILL_ME>",
        "pipeline_version": "<FILL_ME>",
        "p0_flags": [],
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_tokens(run_dir: Path) -> None:
    tokens = {
        "total": None,
        "session_count": 1,
        "by_phase": {},
        "by_tool": {},
    }
    (run_dir / "tokens.json").write_text(
        json.dumps(tokens, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_geometry(run_dir: Path, counts: dict, idf_rel: str | None) -> None:
    geometry = {
        "counts": counts,
        "counts_expected": dict(counts),
        "dimensions_check": "pending",
        "openstudio_screenshot": None,
        "anomalies": [],
        "_source_idf": idf_rel,
    }
    (run_dir / "geometry.json").write_text(
        json.dumps(geometry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_notes(run_dir: Path, run_id: str) -> None:
    body = (
        f"# {run_id}\n\n"
        "## 本次目的\n\n<FILL_ME>\n\n"
        "## 异常 / 失败点\n\n<FILL_ME 或写 '无'>\n\n"
        "## 与上一 anchor 的差异观察\n\n<FILL_ME>\n\n"
        "## 下次改进候选\n\n<FILL_ME 或写 '无'>\n"
    )
    (run_dir / "notes.md").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", help='Case identifier, e.g. "sm_15"')
    parser.add_argument("tag", help='Run tag, e.g. "post_p0" or "function_pkg_v1"')
    parser.add_argument("--idf", default=None,
                        help="Override IDF path (relative to project root or absolute).")
    parser.add_argument("--date", default=None,
                        help="Override run date in YYYY-MM-DD; defaults to today.")
    args = parser.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    run_id = f"{date}_{args.case}_{args.tag}"
    run_dir = PROJECT_ROOT / "case_tests" / "test_baseline" / "runs" / run_id

    if run_dir.exists():
        print(f"[error] {run_dir} already exists. Refusing to overwrite.", file=sys.stderr)
        return 1
    run_dir.mkdir(parents=True)

    idf_path = find_idf(args.case, args.idf)
    if idf_path and idf_path.exists():
        counts = count_idf_objects(idf_path)
        idf_rel = str(idf_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    else:
        counts = {"zones": None, "surfaces": None, "fenestration": None}
        idf_rel = None

    timestamp = datetime.now().isoformat(timespec="seconds")
    write_meta(run_dir, args.case, timestamp)
    write_tokens(run_dir)
    write_geometry(run_dir, counts, idf_rel)
    write_notes(run_dir, run_id)

    rel_run = run_dir.relative_to(PROJECT_ROOT)
    print(f"[ok] Created {rel_run}")
    print("[ok] Files: meta.json / tokens.json / geometry.json / notes.md")
    if idf_path and idf_path.exists():
        print(f"[ok] IDF parsed: {idf_rel}")
        print(f"[ok] Counts: zones={counts['zones']} / "
              f"surfaces={counts['surfaces']} / fenestration={counts['fenestration']}")
    else:
        print("[warn] No IDF found; counts left null. "
              "Pass --idf if your IDF lives outside the default path.")
    print("")
    print("Next steps:")
    print("  1. Claude fills <FILL_ME> in meta.json + tokens.json + notes.md from session memory.")
    print("  2. User opens IDF in OpenStudio, sets geometry.json.dimensions_check, drops screenshot.")
    print("  Reference: case_tests/test_baseline/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
