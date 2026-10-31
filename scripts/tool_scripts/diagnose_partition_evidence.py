"""Evaluate archived source partitions through the J1 evidence service; offline."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.tool_scripts.diagnose_source_partitions import write_json
from src.agent.judge.gt import gt_path, load_gt_document
from src.agent.judge.partition_evidence import attempt_partition_evidence

CASES = {
    "sm21_anchor": ("case_tests/e2e_tests/sm21_anchor/run_2026-07-02_sonnet_flow_e2e", "001"),
    "sm24_anchor": ("case_tests/e2e_tests/sm24_anchor/run_2026-06-24_opus_reading", "002"),
    "sm25-L_anchor": ("AI_agent/logs/experiments/2026-09-09_m0_endpoint_connections_run01", "001"),
}


def trace_candidate_gaps(run_dir, attempt, evidence):
    """Explain flagged lines from the candidate's recipe, not from a reference.

    This is diagnostic attribution only. Its compilations never become inputs
    to the independent partition or reading-support comparison.
    """
    from shapely import wkt
    from shapely.geometry import LineString
    from src.agent.correction.chain_provenance import AsDrawnChainProvenanceV1
    from src.agent.correction.wall_compiler import WallCompilationV1
    from src.agent.correction.projection_bridge import cut_lines_from_wall_compilation, close_collinear_gaps
    from src.agent.correction.window_sources import verify_window_resolver_inputs_artifact

    path = attempt / "chain_provenance.json"
    if not path.exists():
        return {"status": "not_evaluated", "reason": "candidate_chain_provenance_unavailable"}
    provenance = AsDrawnChainProvenanceV1.model_validate_json(path.read_bytes())
    marker = verify_window_resolver_inputs_artifact((attempt / "window_resolver_inputs.json").read_bytes())
    raw_readings = dict(marker.raw_reading_artifacts)
    partition = evidence["reference_partition"]
    rows = []
    for floor in provenance.floors:
        mapped = partition["floor_mapping"].get(floor.floor_ref)
        differences = next((r for r in partition["internal_boundary_comparison"] if r["floor_id"] == mapped), None)
        if differences is None:
            continue
        extra = wkt.loads(differences["extra_wkt"])
        comp = WallCompilationV1.model_validate_json(floor.compilation_bytes)
        lines, _ = cut_lines_from_wall_compilation(comp.walls)
        _, gaps = close_collinear_gaps(lines, resolution_m=0.)
        walls = {w.wall_id: w for w in comp.walls}
        reading = json.loads(raw_readings[floor.input_id])
        hypotheses = reading.get("hypotheses", {})
        for gap in gaps:
            line = LineString([(gap.from_m, gap.pos_m), (gap.to_m, gap.pos_m)] if gap.axis == "x"
                              else [(gap.pos_m, gap.from_m), (gap.pos_m, gap.to_m)])
            origin = gap.half_thickness_from_origin
            half = max(l.half_thickness_m for l in lines if l.origin_id == origin)
            if extra.intersection(line.buffer(half, cap_style=2)).length <= 1e-9:
                continue
            observation_ids = {r.observation_id for r in walls[origin].source_refs}
            classifications = [{"id": c["id"], "span_m": c["span_m"],
                                "type": hypotheses.get("opening_types", {}).get(c["id"], "unknown")}
                               for c in hypotheses.get("opening_candidates", [])
                               if c.get("face_line") in observation_ids
                               and min(c["span_m"][1], gap.to_m) > max(c["span_m"][0], gap.from_m)]
            rows.append({"floor_id": floor.floor_ref, "wall_id": origin,
                         "observation_ids": sorted(observation_ids), "filled_gap_wkt": line.wkt,
                         "original_opening_classifications": classifications})
    return {"status": "traced", "scope": "candidate_derivation_only_not_independent_reference", "filled_gaps": rows}


def overlay(reference, candidate, floor_id):
    reference = [s for s in reference if s["floor_id"] == floor_id]
    candidate = [s for s in candidate if s["floor_id"] == floor_id]
    points = [p for space in reference + candidate for p in space["polygon"]]
    if not points:
        return ""
    x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
    x1, y1 = max(p[0] for p in points), max(p[1] for p in points)
    paths = []
    for name, spaces, color, width in (("参照", reference, "#1263ad", .045), ("候选", candidate, "#cf5426", .028)):
        for space in spaces:
            coords = " ".join(f"{x-x0+.3},{y1-y+.3}" for x, y in space["polygon"])
            paths.append(f'<polygon points="{coords}" fill="none" stroke="{color}" stroke-width="{width}"><title>{name} {html.escape(space["id"])}</title></polygon>')
    return (f'<figure><figcaption>{html.escape(floor_id)}：参照 {len(reference)} / 候选 {len(candidate)} 空间；蓝线为参照，橙线为候选</figcaption>'
            f'<svg role="img" aria-label="源分区对照" viewBox="0 0 {x1-x0+.6} {y1-y0+.6}">{"".join(paths)}</svg></figure>')


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    results, sections = {}, []
    for case, (relative, attempt_index) in CASES.items():
        run_dir = ROOT / relative
        attempt = run_dir / "1_correction/attempts" / attempt_index
        report = attempt_partition_evidence(run_dir, attempt, document=load_gt_document(case), reference_path=gt_path(case))
        dest = out / case
        dest.mkdir()
        write_json(dest / "source_partition_evidence.json", report)
        write_json(dest / "candidate_gap_trace.json", trace_candidate_gaps(run_dir, attempt, report))
        partition = report["reference_partition"]
        reading = report["reading_boundary_support"]
        summary = {
            "source_run": relative, "source_attempt": attempt_index,
            "reference_spaces": partition["comparison"]["reference_count"],
            "candidate_spaces": partition["comparison"]["candidate_count"],
            "reference_status": partition["status"],
            "topology_findings": len(partition["topology_findings"]),
            "reading_support_status": reading["status"],
            "unsupported_boundary_pairs": len(reading["findings"]),
            "candidate_acceptance": report["identity"]["candidate_acceptance"],
        }
        results[case] = summary
        figures = "".join(overlay(partition["reference_spaces"], partition["candidate_spaces"], fid)
                          for fid in sorted({s["floor_id"] for s in partition["reference_spaces"]}))
        status = {"severe": "发现严重分区差异，需按具体空间核对", "not_evaluated": "仍需核对参考面/尺寸，未判为分区通过", "pass": "本项通过", "minor": "存在容差内差异"}[partition["status"]]
        reading_status = "无可用的独立墙线适配，未评价" if reading["status"] == "not_evaluated" else f"{len(reading['findings'])} 对内部边界存在未被读图墙线支持的部分，需结合门洞判断"
        sections.append(f'<section><h2>{case}</h2><p>{status}。参照 {summary["reference_spaces"]}，候选 {summary["candidate_spaces"]} 个空间。</p>'
                        f'<p>读图证据：{reading_status}。</p><p><a href="{case}/source_partition_evidence.json">空间对应、边界位置与输入来源</a></p>{figures}</section>')
    manifest = {
        "mode": "historical_candidate_independent_judge_evidence", "model_calls": 0, "solver_calls": 0,
        "manual_room_groups_added": 0,
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
    }
    write_json(out / "report.json", {"manifest": manifest, "cases": results})
    (out / "index.html").write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>源分区自动证据</title>
<style>body{font:16px/1.75 system-ui;max-width:1100px;margin:36px auto;padding:0 20px;background:#f5f7fa;color:#183044}section{background:white;padding:24px;margin:24px 0}figure{display:inline-block;width:46%;vertical-align:top;margin:1%}svg{width:100%;max-height:540px}a{color:#1263ad}@media(max-width:650px){figure{width:98%}}</style>
<h1>源分区自动证据</h1><p>复用已有读图和模型，自动读取独立参照；本次没有人工指定合并哪几个房间，没有改动候选或 GT。</p>
<p>原始坐标差异完整保留；外边界参考面偏移与内部拆并证据分别报告。读图墙线只提供局部支持，不能把门口的缺线直接当成应该并房。图中每条线的原始来源见报告。</p>
''' + "".join(sections) + '''<p>本次未从原图重新读取、未调用判图模型、未执行 EnergyPlus、未完成全图人工验收。证据服务已接正常 J1 评价包；模型判定及运行路由仍由既有 judge 流程负责。</p><a href="report.json">汇总报告</a></html>''', encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().out.resolve()), ensure_ascii=False))
