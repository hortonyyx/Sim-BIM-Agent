"""Rebuild sm24 windows and an explicitly assisted corridor doorway offline.

Creates a new run; never overwrites archived inputs or calls a model/solver.
The doorway comes from archived reading notes, not a new drawing extraction.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.tool_scripts.diagnose_source_partitions import (
    CASES, GROUPS, _viewer, sm24_partition_reference, spaces, write_json,
)
from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.correction.schema import WallOpening
from src.agent.geometry.openings import visible_wall_parts
from src.agent.geometry.to_idf import building_to_idf
from src.agent.judge.source_connections import compare_connections
from src.agent.judge.source_partition import compare_partitions
from src.agent.legacy.pipeline import materialize_kernel_geometry


def run(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    old = ROOT / CASES["sm24"]
    paths = [old / "1_correction/attempts/002/output.json", old / "0_reading/1f_view.json", GROUPS]
    inputs = [{"path": str(p.relative_to(ROOT)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    geom = ensure_corrected_geometry(json.loads(paths[0].read_text()))
    fixture = json.loads(GROUPS.read_text())
    reading = json.loads(paths[1].read_text())
    # This is an explicit replay recipe, tied to the documented reading. Fail if
    # the archived evidence has changed rather than inventing a new placement.
    serialized_reading = json.dumps(reading)
    if "corridor mouth" not in serialized_reading or "double-door arcs" not in serialized_reading:
        raise ValueError("archived corridor-mouth evidence is unavailable")
    candidate, _ = sm24_partition_reference(geom, fixture)
    source_path = str(paths[1].relative_to(ROOT))
    candidate.openings = [WallOpening(
        id="corridor_north_mouth", kind="door", space_id="cell_corridor",
        other_space_id="cell_north_reception", p1=(4.1, 16.1), p2=(5.9, 16.1), z=(0, 2.1),
        state="unknown", source_refs=[source_path + "#S7", source_path + "#S8"],
        assumptions=[
            "Doorway position/width manually transcribed from archived reading; not independently re-read from the image.",
            "Door kind follows the recorded double-door arcs; actual operating state is unknown.",
            "Height assumed 2.1 m from floor; no vertical doorway dimension in the supplied evidence.",
            "Only this doorway is supplied; other healed doorways in the reading remain unmodelled.",
        ],
    )]
    candidate.corrections.append({"kind": "assisted_doorway_replay", "opening_ids": ["corridor_north_mouth"],
                                  "source": source_path, "height_assumed_m": 2.1})

    def materialize(label, corrected, title):
        stage = out / label
        stage.mkdir()
        write_json(stage / "correction.json", corrected.model_dump(mode="json"))
        bg, issues = materialize_kernel_geometry(corrected, stage, capability_profile="orthogonal_polygon")
        if bg is None or issues:
            raise RuntimeError(f"{label} source rebuild failed: {issues}")
        data = json.loads((stage / "building_geometry.json").read_text())
        source = json.loads((stage / "source_model.json").read_text())
        data["source_model"] = source
        _viewer(stage / "viewer.html", data, title)
        return bg, data, source

    legacy_bg, _legacy_data, legacy_source = materialize(
        "historical_rebuild", geom, "历史分房重放：11 空间、11 窗；错误分房仍保留")
    bg, data, source = materialize(
        "assisted_model", candidate, "人工辅助案例：8 空间、11 窗、1 处走廊门洞；门高暂定 2.1 米")
    cuts = visible_wall_parts(data)
    write_json(out / "assisted_model/visible_wall_parts.json", cuts)
    windows_unchanged = {
        w.id: (w.facade, tuple(w.span), tuple(w.z)) for w in candidate.windows
    } == {w.id: (w.facade, tuple(w.span), tuple(w.z)) for w in geom.windows}
    if not windows_unchanged or len(bg.windows) != len(geom.windows):
        raise RuntimeError("replay failed to retain every original window and its span/height")

    # Mutation checks isolate preservation of this explicit connection, not
    # drawing completeness. Closing is tested with a separately declared open
    # reference: the real case's unknown state must not be called known-open.
    doorway_rows = [o for o in source["openings"] if o["kind"] != "window"]
    dropped = copy.deepcopy(source)
    dropped["openings"] = [o for o in dropped["openings"] if o["kind"] == "window"]
    wrong_room = copy.deepcopy(source)
    next(o for o in wrong_room["openings"] if o["kind"] != "window")["space_ids"][-1] = "cell_office_left_upper"
    open_reference = copy.deepcopy(source)
    next(o for o in open_reference["openings"] if o["kind"] != "window")["connectivity"] = "open"
    closed = copy.deepcopy(open_reference)
    next(o for o in closed["openings"] if o["kind"] != "window")["connectivity"] = "closed"
    mutations = {
        "missing_doorway": compare_connections(source, dropped),
        "wrong_neighbour": compare_connections(source, wrong_room),
        "closed_in_synthetic_known_open_variant": compare_connections(open_reference, closed),
    }
    if any(r["status"] != "severe" for r in mutations.values()):
        raise RuntimeError("connection mutation was not detected")
    try:
        building_to_idf(bg)
    except ValueError as exc:
        if "refusing to silently replace" not in str(exc):
            raise
        ep_export = {"status": "blocked_unsupported_openings", "message": str(exc), "solver_calls": 0}
    else:
        raise RuntimeError("EnergyPlus export unexpectedly omitted its unsupported-aperture stop")

    report = {
        "manifest": {
            "mode": "historical_replay_with_assisted_partition_and_doorway",
            "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "working_tree_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
            "inputs": inputs, "model_calls": 0, "solver_calls": 0,
            "interventions": ["two declared source-space unions", "one transcribed doorway and assumed height"],
        },
        "historical_rebuild": {"spaces": len(legacy_source["spaces"]), "surfaces": len(legacy_bg.surfaces),
                               "windows": len(legacy_bg.windows), "source_mapping": legacy_source["validation"],
                               "partition_comparison": compare_partitions(spaces(candidate), legacy_source["spaces"])},
        "assisted_rebuild": {"spaces": len(source["spaces"]), "surfaces": len(bg.surfaces), "windows": len(bg.windows),
                             "source_doorways": len(doorway_rows), "derived_apertures": len(bg.openings),
                             "original_window_spans_and_heights_preserved": windows_unchanged,
                             "source_mapping": source["validation"],
                             "partition_preservation": compare_partitions(spaces(candidate), source["spaces"]),
                             "connections": source["connections"], "doorway_evidence": doorway_rows,
                             "cut_parent_walls": sorted(cuts)},
        "connection_mutations": mutations, "energyplus_export": ep_export,
        "not_evaluated": ["cold-start drawing extraction", "other doors and full drawing completeness",
                          "actual door height/operating state", "EnergyPlus opening adapter and solver run",
                          "human approval, persistent editing and browser screenshot inspection"],
    }
    write_json(out / "report.json", report)
    details = html.escape(json.dumps(report, indent=2, ensure_ascii=False))
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>房间、窗与走廊门洞</title>
<style>body{font:16px/1.8 system-ui;max-width:960px;margin:40px auto;padding:0 24px;color:#183044;background:#f6f8fa}
a{color:#0768a0}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border:1px solid #ccd6dd;text-align:left}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}section{background:white;padding:20px;margin:24px 0}h1{font-size:28px}</style>
<h1>房间、窗与走廊门洞能一起保留了</h1>
<p>这次在同一个案例里保留了 8 个房间/空间、全部 11 扇窗，以及走廊通往北侧接待室的 1 处门洞。窗的位置、宽度和高度沿用原记录；贴墙边的窗不再被误拦。</p>
<table><tr><th>查看结果</th><th>能说明什么</th></tr>
<tr><td><a href="assisted_model/viewer.html">查看修复后的建筑模型</a></td><td>人工合并错误切开的空间，并补入一处有位置记录的门洞。</td></tr>
<tr><td><a href="historical_rebuild/viewer.html">查看历史分房重建结果</a></td><td>11 扇窗已能重建；原有三道多余隔墙仍可对照查看。</td></tr></table>
<section><h2>门洞怎样表达</h2><p>墙面实际留出了宽 1.8 米的洞口，两侧都指向同一个门洞，并记录连接走廊和接待室。两面墙上的显示片不算成两扇门。</p>
<p>旧读图记录提到双开门，所以暂记作门。门高采用 2.1 米假设，实际开闭状态未知。可在查看器中展开房间、降低墙透明度，点选棕色门洞查看连接和状态。</p></section>
<section><h2>检查结果与剩余限制</h2><p>删除门洞、接错房间，以及在另设“已知开放”的对照中将门关闭，都被自动检查判为严重变化。</p>
<p>这份结果复用了历史读图和人工分组，并人工摘录了一处门洞。它没有重新自动读图，也没有补齐图中其他门。模型可查看，但新增门洞尚不能导出仿真；程序会明确停止，避免悄悄把门洞封成墙。</p>
<p>本次没有模型调用或仿真运行；没有实际浏览器截图验收。独立检查和单位测试不能代替整案正确性及人工确认。</p></section>
<details><summary>完整证据、来源与假设</summary><pre>'''+details+'''</pre></details>
<p><a href="report.json">下载完整检查报告</a> · <a href="assisted_model/source_model.json">查看房间、边界与门窗记录</a></p></html>'''
    (out / "index.html").write_text(page, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new independent run directory; must not exist")
    args = parser.parse_args()
    report = run(args.out.resolve())
    result = report["assisted_rebuild"]
    print(json.dumps({key: result[key] for key in ("spaces", "surfaces", "windows", "source_doorways", "derived_apertures")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
