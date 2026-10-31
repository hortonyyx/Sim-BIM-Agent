"""Compare saved BIM candidates with a reference after generation has ended.

Reference data never enters the generating agent. For partial inference, the
reference layout is diagnostic only: unprovided interiors are not reconstruction
targets. This reuses the existing partition/window evaluators without changing
their raw tolerances. Documented convention differences are reported separately;
raw readings never inherit a delivery-quality pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.correction.parse import ensure_corrected_geometry
from src.agent.judge.gt import load_gt_document, gt_path
from src.agent.judge.gt_schema import LegacyGroundTruthV2
from src.agent.judge.partition_evidence import reference_partition
from src.agent.judge.elevation_score import score_correction_elevation_windows
from scripts.tool_scripts.diagnose_partition_evidence import overlay
from src.agent.bim_inputs import digest, dump
from src.agent.judge.conventions import load_policy, classify_conventions, reading_report
from src.agent.judge.plan_inventory import compare_plan_inventory, compare_exterior_heights


def evaluate(run: Path, reference_case: str, *, modelling_task: str,
             reference_scope: str, out: Path | None = None) -> dict:
    run = run.resolve()
    if (run / "bim/inputs.json").is_file():
        run = run / "bim"
    completion = run / "summary.json"
    receipt_path = run.parent / "receipt.json"
    if not completion.is_file():
        # Historical runtime runs predate the common summary. Consume their
        # saved terminal receipt directly; never copy or patch inputs.json.
        if not receipt_path.is_file():
            raise RuntimeError("evaluation must wait until generator finishes")
        receipt = json.loads(receipt_path.read_bytes())
        if not (receipt.get("status") == "completed" or receipt.get("finalization")):
            raise RuntimeError("evaluation must wait until generator finishes")
        completion = receipt_path
    manifest = json.loads((run / "inputs.json").read_bytes())
    target = out.resolve() if out is not None else run / "evaluation"
    gt = load_gt_document(reference_case)
    if gt is None:
        raise ValueError(f"no verified reference available for {reference_case}")
    reference_path = gt_path(reference_case).resolve()
    reference_before = digest(reference_path)
    policy, observations = load_policy(reference_case, reference_path, manifest)
    target.mkdir(parents=True, exist_ok=False)
    candidates = ([run / "seed"] if (run / "seed/source_model.json").is_file() else [])
    candidates += sorted(p for p in run.glob("candidate_*") if (p / "source_model.json").is_file())
    rows, sections = [], []
    for candidate in candidates:
        proposal = json.loads((candidate / "proposal.json").read_text())
        source = json.loads((candidate / "source_model.json").read_text())
        geom = ensure_corrected_geometry(proposal["geometry"])
        report = reference_partition(geom, gt, source_spaces=source["spaces"])
        dump(target / f"{candidate.name}_partition.json", report)
        built_window_ids = {o["id"] for o in source["openings"] if o["kind"] == "window"}
        built_geom = geom.model_copy(deep=True)
        built_geom.windows = [w for w in geom.windows if w.id in built_window_ids]
        if isinstance(gt, LegacyGroundTruthV2):
            window_score = score_correction_elevation_windows(
                built_geom, gt.model_dump(mode="json"), floor_map=report.get("floor_mapping", {}), evidence=[])
            dump(target / f"{candidate.name}_windows.json", asdict(window_score))
            window_summary = window_score.summary()
        else:
            # The old elevation scorer accepts v2 only. Do not flatten a typed
            # v3 reference or manufacture an empty denominator from its shape.
            window_summary = {
                "status": "not_evaluated",
                "reason": "typed_v3_window_comparison_not_integrated_in_this_diagnostic",
                "reference_window_count": sum(o.kind == "window" for o in gt.openings),
                "built_window_count": len(built_window_ids),
                "count_is_not_a_match_score": True,
            }
            dump(target / f"{candidate.name}_windows.json", window_summary)
        inventory = (compare_plan_inventory(source, observations, report.get("floor_mapping", {}))
                     if observations else {"status": "not_evaluated", "findings": [],
                         "reason": "independent_original_image_inventory_unavailable_for_input_scope"})
        heights = compare_exterior_heights(source, gt, report)
        quality = classify_conventions(report, source, policy if modelling_task == "reconstruction" else {},
                                       inventory=inventory, heights=heights)
        if modelling_task != "reconstruction":
            # An unprovided interior layout is not a fidelity target for
            # inference. Retain the measurements without issuing a verdict.
            quality["reference_layout_diagnostic_status"] = quality["status"]
            quality["status"] = "not_evaluated"
            quality.setdefault("limits", []).append(
                "For inference, the full reference layout is diagnostic only; evaluate provided constraints and inference plausibility separately.")
        dump(target / f"{candidate.name}_delivery_quality.json", quality)
        row = {
            "candidate": candidate.name, "is_recovery_seed": candidate.name == "seed",
            "source_sha256": source["source_model_sha256"],
            "partition_status": report["status"],
            "delivery_quality_status": quality["status"],
            "convention_difference_count": len(quality["convention_differences"]),
            "delivery_quality_report": f"{candidate.name}_delivery_quality.json",
            "reference_spaces": len(report.get("reference_spaces", [])),
            "candidate_spaces": len(source["spaces"]),
            "openings": dict(Counter(o["kind"] for o in source["openings"])),
            "built_window_comparison": window_summary,
            "topology_findings": report.get("topology_findings", []),
            "internal_boundary_comparison": report.get("internal_boundary_comparison", []),
        }
        rows.append(row)
        figures = "".join(overlay(report["reference_spaces"], report["candidate_spaces"], fid)
                          for fid in sorted({s["floor_id"] for s in report.get("reference_spaces", [])}))
        sections.append(f'<section><h2>{html.escape(candidate.name)}</h2>'
                        f'<pre>{html.escape(json.dumps(row, ensure_ascii=False, indent=2))}</pre>'
                        f'{figures}</section>')
    readings = reading_report(run, reference_spaces=report.get("reference_spaces", []) if candidates else [])
    dump(target / "reading_readings.json", readings)
    # A second aggregate report links every saved candidate's full quality
    # result; neither report overwrites historical scores or original evidence.
    dump(target / "delivery_quality.json", {"candidates": [{key: row[key] for key in
        ("candidate", "source_sha256", "partition_status", "delivery_quality_status",
         "convention_difference_count", "delivery_quality_report")} for row in rows],
        "reference_sha256": reference_before, "reference_unchanged": digest(reference_path) == reference_before})
    caution = (
        "部分推理建模：完整参照只用于事后诊断。未提供的内部格局不能按还原任务判漏房或错分区；"
        "另行检查输入约束、推断合理性、源简化说明和几何可用性。"
        if modelling_task == "partial_inference" else
        "还原建模：以下为独立参照差异，不自动等于原图保真或整案验收。")
    result = {
        "mode": "post_generation_evaluation_only", "modelling_task": modelling_task,
        "reference_scope": reference_scope, "interpretation": caution,
        "input_manifest_sha256": digest(run / "inputs.json"),
        "completion_evidence": {"path": str(completion), "sha256": digest(completion)},
        "reading_report": "reading_readings.json", "delivery_quality_report": "delivery_quality.json",
        "reference_case": reference_case,
        "reference_path": str(reference_path.relative_to(ROOT)),
        "reference_sha256": digest(reference_path), "candidates": rows,
        "overall_fidelity": "not_automatically_decided",
        "limits": [
            "Reference completeness and original pixel truth require separate evidence.",
            "Independent plan inventory checks openings/hosts/connections where supplied; interior heights and vertical voids remain unverified.",
            "Raw partition differences are unchanged; only documented, bounded conventions are separated in delivery quality.",
            "No evaluation feedback supplied to the generating model.",
        ],
    }
    dump(target / "summary.json", result)
    (target / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>BIM 生成后的独立参照诊断</title><style>body{font:16px system-ui;margin:30px;max-width:1200px}'
        'pre{white-space:pre-wrap;font-size:13px}figure{max-width:850px}section{margin-bottom:40px}</style>'
        '<h1>BIM 生成后的独立参照诊断</h1>'
        f'<p>{html.escape(caution)}</p><p>参照范围：{html.escape(reference_scope)}</p>'
        '<p>本页在生成结束后计算，未送回生成模型。蓝线为参照，橙线为候选。'
        '详情与限制见 <a href="summary.json">summary.json</a>。'+
        '<a href="reading_readings.json">规整前读数</a> · '+
        '<a href="delivery_quality.json">交付稿质量</a>；交付通过不等于读图准确。</p>'
        + ''.join(sections) + '</html>', encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--reference-case", required=True)
    parser.add_argument("--modelling-task", choices=["reconstruction", "partial_inference"], required=True)
    parser.add_argument("--reference-scope", required=True,
                        help="Describe supplied vs withheld evidence and legitimate reference comparisons")
    parser.add_argument("--out", type=Path, help="New output directory; defaults to RUN/evaluation")
    args = parser.parse_args()
    result = evaluate(args.run, args.reference_case, modelling_task=args.modelling_task,
                      reference_scope=args.reference_scope, out=args.out)
    print(json.dumps({"modelling_task": result["modelling_task"],
                      "candidates": [{k: r[k] for k in ("candidate", "partition_status", "built_window_comparison")}
                                     for r in result["candidates"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
