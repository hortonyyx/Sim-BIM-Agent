"""Controlled enclosure scenarios on frozen sm21 geometry; no model/solver calls.

The open and unknown regions are explicit examples, NOT observations of sm21.
Each scenario uses the normal source-target CLI with a bound geometry declaration.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import html
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PRIOR = ROOT/"AI_agent/logs/experiments/2026-09-09_source_bim_run04"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def declaration(base, west_condition, *, partial=False):
    evidence = {"evidence_kind": "example", "source_refs": [],
                "assumptions": ["人为构造的灰空间表达测试；不是对 sm21 原图的识读或修正。"]}
    return {"schema_version": "source_enclosure_input_v1", "base_source_model_sha256": base["source_model_sha256"],
            "spaces": [{"space_id": "F1_corridor", "enclosure": "semi_open", **evidence}] if west_condition == "open" else [],
            "boundaries": [{"boundary_id": "space/F1_corridor/wall/3", "condition": west_condition,
                            "scope": "partial" if partial else "whole", **evidence,
                            **({"vertices": [[.1,5,1.1],[.1,3,1.1],[.1,3,3],[.1,5,3]]} if partial else {})}]}


def plot_corridor(display, out):
    """Orthographic inspection of the declared west side; not a WebGL screenshot."""
    from PIL import Image, ImageDraw, ImageFont

    bid = "space/F1_corridor/wall/3"
    surface = next(s for s in display["surfaces"] if s["name"] == bid)
    canvas = Image.new("RGB", (850, 670), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=19)
    point = lambda v: (int(230+(v[1]-3)*150), int(530-v[2]*150))
    for part in display["display_surface_parts"][bid]:
        color = "#eeaa33" if part.get("enclosure_condition") == "unknown" else "#7393a9"
        draw.polygon([point(v) for v in part["verts"]], fill=color, outline="#445566", width=2)
        for hole in part["holes"]:
            draw.polygon([point(v) for v in hole], fill="white", outline="#445566", width=2)
    def dashed(ring, color):
        pts = [point(v) for v in ring]
        for a, b in zip(pts, pts[1:]+pts[:1]):
            length = max(abs(b[0]-a[0]), abs(b[1]-a[1]))
            for start in range(0, length, 14):
                t, u = start/length, min(start+8,length)/length
                draw.line([(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t),
                           (a[0]+(b[0]-a[0])*u,a[1]+(b[1]-a[1])*u)],fill=color,width=3)
    dashed(surface["verts"], "#596b86")
    for region in display["enclosure_regions"]:
        if region["boundary_id"] == bid:
            dashed(region["verts"], "#00a6a6" if region["condition"] == "open" else "#d47b12")
    for y, label in [(80,"3.0 m"),(365,"1.1 m"),(530,"0.0 m")]:
        draw.text((135,y-10),label,fill="#445566",font=font)
    for xy, label in [((80,20),"Controlled example: corridor west side (2 m wide)"),
                      ((80,580),"Blue-gray = physical; amber = unknown; white = open"),
                      ((80,615),"Dashed = logical extent / declared region; not a browser capture")]:
        draw.text(xy,label,fill="#223344",font=font)
    canvas.save(out)


def run(out):
    from src.agent.execution.source_bim import export_source_bim
    from src.agent.execution.ep_branch import derive_ep_geometry

    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    run_dir = out/"upstream_run"
    shutil.copytree(PRIOR/"flow_sm21_run", run_dir)
    before = {stage:sha256((run_dir/stage/"attempts/001/output.json").read_bytes()).hexdigest()
              for stage in ("0_reading","1_correction")}
    export = export_source_bim(run_dir, out/"baseline", capability_profile="rectangular")
    assert export["source_geometry_ready"], export
    base = json.loads((out/"baseline/source_model.json").read_bytes())
    assert (out/"baseline/source_model.json").read_bytes() == (PRIOR/"flow_sm21_bim/source_model.json").read_bytes()
    reports = {}
    for name, condition, partial in (("open_corridor", "open", False), ("parapet_corridor", "open", True), ("unknown_side", "unknown", False)):
        declared = out/f"{name}_input.json"
        write_json(declared, declaration(base, condition, partial=partial))
        argv = [sys.executable, str(ROOT/"scripts/tool_scripts/run_stage.py"), "flow", "sm21_anchor", str(run_dir),
                "--target", "source-bim", "--bim-out", str(out/name), "--enclosure-input", str(declared), "--judge", "off"]
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
        (out/f"{name}_stdout.txt").write_text(result.stdout+result.stderr)
        report = {"command": argv, "returncode": result.returncode}
        write_json(out/f"{name}_command.json", report)
        if result.returncode:
            raise RuntimeError(f"source scenario failed; inspect {out/name/'report.json'}")
        source = json.loads((out/name/"source_model.json").read_bytes())
        assert source["schema_version"] == "source_bim_v3"
        assert source["openings"] == base["openings"]
        assert {s["id"]:s["polygon"] for s in source["spaces"]} == {s["id"]:s["polygon"] for s in base["spaces"]}
        try:
            derive_ep_geometry(source, {s["id"]:s["id"] for s in source["spaces"]})
        except ValueError as exc:
            report["ep_backend"] = {"status": "unsupported", "reason": str(exc), "solver_calls": 0}
        else:
            raise AssertionError("legacy EP must not silently seal new enclosure semantics")
        report["counts"] = json.loads((out/name/"report.json").read_bytes())["counts"]
        reports[name] = report
        plot_corridor(json.loads((out/name/"display_geometry.json").read_bytes()), out/name/"geometry_inspection.png")
    # Invalid declarations must fail without altering the preserved baseline.
    bad = declaration(base, "open")
    bad["base_source_model_sha256"] = "0"*64
    write_json(out/"stale_input.json",bad)
    rejected = export_source_bim(run_dir,out/"stale_rejected",capability_profile="rectangular",enclosure_input_path=out/"stale_input.json")
    assert not rejected["source_geometry_ready"]
    after = {stage:sha256((run_dir/stage/"attempts/001/output.json").read_bytes()).hexdigest() for stage in before}
    assert before == after
    assert not any((run_dir/stage).exists() for stage in ("2_modelling","3_split_pairing","4_mep","5_intakeoutput"))
    write_json(out/"report.json", {"mode": "controlled examples on historical sm21 geometry", "model_calls":0,"solver_calls":0,
                                  "upstream_hashes_unchanged":after,"scenarios":reports,
                                  "stale_declaration_rejected":True,"browser_rendering":"not_verified; orthographic geometry images only"})
    cards = ''.join(f'<article><h2>{html.escape(name)}</h2><p><a href="{name}/viewer.html">交互查看</a> · <a href="{name}/source_model.json">源 BIM</a> · <a href="{name}/report.json">质量报告</a></p><img width="900" src="{name}/geometry_inspection.png"></article>' for name in reports)
    (out/"index.html").write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>源 BIM 灰空间表达</title><style>body{font:16px system-ui;max-width:1000px;margin:35px auto;padding:20px}img{max-width:100%}article{margin:30px 0}</style><h1>灰空间：逻辑范围与实际围护</h1><p>受控示例，基于历史 sm21 几何；不是原图灰空间识读。蓝灰表示实际围护，琥珀表示未知围护，虚线表示逻辑范围。图为西侧围护的正投影检查图，非 WebGL 截图。</p>'+cards+'</html>')
    print(out)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    run(parser.parse_args().out)
